#!/usr/bin/env python3
"""
en_maps.py - the EN client's own map files (world-portal-table-en, world-template-map)
=====================================================================================
Everything the server needs to know about a map's topology comes from the files the
client loads itself (roadmap F7: EN assets are the authority for topology):

    hs/stageAA_BB.hmi   map XML (asset cipher, en_content.decode_hs): SpriteLists,
                        LayerLists/Layer/Tile with pos_x/pos_y, event, value_num, NpcId
    hs/**/*.hsi         sprite index (same cipher, ASCII): per sprite a list of
                        `line: <type> <x1> <y1> <x2> <y2>` collision lines

    import en_maps
    m = en_maps.load_map(102)                 # MapData (cached)
    m.lines[31]                               # Line(type 0, (0,812)-(100,812), event 1, value 101)
    rows, report = en_maps.build_portal_table()
    python en_maps.py                         # writes server/portals_en.json + asserts

Collision lines (FUN_00406170, called at the end of the map loader FUN_00407190)
---------------------------------------------------------------------------------
The client walks the layer list in XML order, each layer's tiles in XML order (both lists
are CPList::AddTail, FUN_00459df0), and for each tile every line of its sprite
(CSpriteControl::GetLineCount/GetLineInfor on sprite id `hsi base - 1 + sprite`). Each
line becomes one entry of the map's list +0x58 with the absolute coordinates (sprite line
+ tile pos) and the tile's `event` / `value_num`. A tile whose .hsi failed to load gets
sprite base 0 and is never added (FUN_00408390 refuses base 0): it contributes no lines,
which is also what a missing file gives here.

C2S 0x7E (FUN_0042cf20 -> FUN_00406df0) sends the 0-based position in that list of the
FIRST entry with `type == 0 && event != 0 && y1 == trunc(y) && x1 <= trunc(x) <= x2`, and
only when that entry's event is 1. `value_num` is read but never sent (world doc 1.5), so
the table maps (map, index) -> value_num of the tile that owns the line. Line types:
0 floor, 1/2 slopes, 3/4 walls (hs/tile/tile001.hsi sprites 1 and 4).

Live-captured anchors (world_movement_npc#03/#04/#06, spec correction C17, live P2 note):
101_23 -> 102, 102_31 -> 101, 102_17 -> 103, 201_192 -> 9701. `main()` asserts all four.

Arrival point
-------------
The destination's REVERSE portal (the event-1 tile whose value_num is the source map):
x = the middle of its first floor line, y = 100 px above that line. The player spawns in
the air and gravity settles it on the portal floor (live: arrival (1411,714) on 101 falls
to 814). This is the rule the KR `gamedef.maps` table follows: every KR row whose arrival
sits at an EN reverse portal has dy = (floor line - tile pos_y) - 100 exactly (-88 for
tile001, -85 tile007, -73 tile006, -80 tile003/004 ...) and dx inside the line; 293 of the
354 KR rows that have an EN reverse portal reproduce within 3 px, the other 61 are the
rows the world doc calls out as KR-only layouts (e.g. 202_6 -> 201 @ (895,1406) while the
EN 201 portal to 202 is the tile at (1100,1200)). The design's "(hsi, sprite) offset,
default (+55,-85)" is this rule measured per sprite.

A destination with no reverse portal (the nine towns -> 9701 flea market, 9701 -> 101,
720 -> 409, 1014 -> 1015) arrives at the destination's first portal line instead; the
row says so in `arrival`.

Monster placement (world-template-map, world doc Q7)
---------------------------------------------------
Event-2 tiles carry `NpcId` (the hni template index). FUN_00444100 creates a client-side
entity (uid 33000000+) for them only when the template `type` (record +0x644) is <= 2
(town NPCs); the monster templates (type 3..7) are skipped, so those tiles are the
server's spawn points - map 102's eight NpcId=1 tiles are exactly the eight Pupu points
the old hand-written MAP_SPAWNS held. `spawn_point()` puts the monster on the middle of
its tile's first floor line, because a 0x1A with server_controlled = 1 runs no physics
(spec correction C29) and a mob spawned at the tile's pos_y would hover above the floor.
"""
import argparse
import glob
import json
import logging
import os
import re
import sys
import threading
import xml.etree.ElementTree as ET
from collections import namedtuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import en_content as EC  # noqa: E402

log = logging.getLogger('WS')

PORTALS_EN_PATH = EC.PORTALS_EN_PATH

# hsi line types (hs/tile/tile001.hsi: sprite 1 = wall 3 + slope 2 + floor 0, sprite 4 =
# floor 0 + slope 1 + wall 4). Only type 0 can be a portal line (FUN_00406df0).
LINE_FLOOR = 0
FLOOR_TYPES = (0, 1, 2)             # lines a body can stand on (walls are 3/4)
EVENT_PORTAL = 1                    # tile event="1": C2S 0x7E, value_num = destination map
EVENT_NPC = 2                       # tile event="2": NpcId = hni template index
# Arrival: 100 px above the reverse portal's floor line (module docstring).
ARRIVAL_ABOVE_LINE = 100.0
# The live captures the generated table must reproduce (world_movement_npc#03/#04/#06,
# spec correction C17 and the P2 live note for the 201 flea-market gate).
LIVE_PORTALS = {(101, 23): 102, (102, 31): 101, (102, 17): 103, (201, 192): 9701}
# EN 2009 (client-2009-world): no portal has been captured live on the 2009 client yet. Its
# own map files still reproduce the three 101/102 anchors (the portal test route of the
# 2009 live checks), so those are asserted; map 201's lines moved (201_192 is no portal in
# the 2009 files), so that one is not. Replace with live 2009 captures when they exist.
LIVE_PORTALS_BY_BUILD = {
    '2008': LIVE_PORTALS,
    '2009': {(101, 23): 102, (102, 31): 101, (102, 17): 103},
}


def live_portals(build=None):
    """The anchors the portal table of a client build must reproduce."""
    return LIVE_PORTALS_BY_BUILD[build or EC.client_build()]


Line = namedtuple('Line', 'index type x1 y1 x2 y2 event value tile')
Tile = namedtuple('Tile', 'layer no hsi hsi_path sprite x y event value npc_id')


class MapError(RuntimeError):
    pass


# --------------------------------------------------------------------- .hsi ---
def parse_hsi(text):
    """[[(type, x1, y1, x2, y2), ...] per sprite] in FILE order. The DLL's sprite vector
    is indexed by load position (CSpriteControl lookup 0x10004230: vector[id - 1]), so the
    N-th sprite block is sprite N whatever number its line starts with."""
    sprites = []
    for raw in text.splitlines():
        tokens = raw.split()
        if not tokens:
            continue
        if tokens[0] == 'line:':
            if not sprites:
                continue
            try:
                sprites[-1].append(tuple(int(v) for v in tokens[1:6]))
            except ValueError:
                continue
        elif len(tokens) > 1 and tokens[1] == 'image:':
            sprites.append([])
    return sprites


# --------------------------------------------------------------------- .hmi ---
class MapData:
    """One decoded map: its tiles in the client's order and the collision-line list."""

    def __init__(self, code, name_text, tiles, lines, missing_hsi):
        self.code = code
        self.name_text = name_text          # MapInfo name = MapLngKo.lng text id
        self.tiles = tiles
        self.lines = lines
        self.missing_hsi = missing_hsi

    def portal_lines(self):
        """The type-0 lines of event-1 tiles: every index a C2S 0x7E can carry here."""
        return [ln for ln in self.lines if ln.type == LINE_FLOOR and ln.event == EVENT_PORTAL]

    def portal_tiles(self):
        return [t for t in self.tiles if t.event == EVENT_PORTAL]

    def npc_tiles(self):
        return [t for t in self.tiles if t.event == EVENT_NPC and t.npc_id]

    def tile_lines(self, tile):
        return [ln for ln in self.lines if ln.tile is tile]

    def floor_line(self, tile):
        """The first floor (type 0) line of a tile, or None."""
        for ln in self.tile_lines(tile):
            if ln.type == LINE_FLOOR:
                return ln
        return None

    def floor_below(self, x, y):
        """The y of the nearest standable line at or below (x, y), or None."""
        best = None
        for ln in self.lines:
            if ln.type not in FLOOR_TYPES:
                continue
            lo, hi = min(ln.x1, ln.x2), max(ln.x1, ln.x2)
            if not lo <= x <= hi:
                continue
            if ln.x2 == ln.x1:
                at = min(ln.y1, ln.y2)
            else:
                at = ln.y1 + (ln.y2 - ln.y1) * (x - ln.x1) / (ln.x2 - ln.x1)
            if at >= y and (best is None or at < best):
                best = at
        return best

    def floor_near(self, x, y, rise=0.0):
        """The y of the standable line under x that is nearest to y, among the lines at or
        below y - rise, or None (world-position-estimate). A walker on a floor stays on it
        even with a platform just above his head (nearest wins over "first above"), a slope
        up to `rise` px higher is followed, and walking off a ledge lands on the next floor
        down. floor_below would snap to that overhead platform as soon as rise > 0."""
        best = best_d = None
        for ln in self.lines:
            if ln.type not in FLOOR_TYPES or ln.x1 == ln.x2:
                continue
            lo, hi = min(ln.x1, ln.x2), max(ln.x1, ln.x2)
            if not lo <= x <= hi:
                continue
            at = ln.y1 + (ln.y2 - ln.y1) * (x - ln.x1) / (ln.x2 - ln.x1)
            if at < y - rise:
                continue
            d = abs(at - y)
            if best_d is None or d < best_d:
                best, best_d = at, d
        return best

    def x_bounds(self):
        """(min x, max x) over every collision line: the span a walker can reach (the live
        left edge of 101/102 is x 16, world_movement_npc#20). None for a map with no lines."""
        if not self.lines:
            return None
        xs = [v for ln in self.lines for v in (ln.x1, ln.x2)]
        return float(min(xs)), float(max(xs))

    def spawn_point(self, tile):
        """Where a monster placed by an event-2 tile stands: the middle of the tile's
        floor line (server_controlled mobs run no physics, C29), else the floor under the
        tile's origin, else the tile's origin itself."""
        ln = self.floor_line(tile)
        if ln is not None:
            return (ln.x1 + ln.x2) / 2.0, float(ln.y1)
        floor = self.floor_below(tile.x, tile.y)
        return float(tile.x), float(tile.y if floor is None else floor)

    def __repr__(self):
        return f'<MapData {self.code} tiles={len(self.tiles)} lines={len(self.lines)}>'


def _int_attr(el, name, default=0):
    try:
        return int(el.get(name, default))
    except (TypeError, ValueError):
        return default


class _Cache:
    def __init__(self):
        self.lock = threading.RLock()
        self.hsi = {}
        self.maps = {}


_cache = _Cache()


def clear_cache():
    """Drop every parsed map and sprite index (en_content.reload / GM /update)."""
    with _cache.lock:
        _cache.hsi.clear()
        _cache.maps.clear()


def _client_path(client_dir, rel):
    rel = rel.replace('\\', '/')
    if rel.startswith('./'):
        rel = rel[2:]
    return os.path.join(client_dir, *rel.split('/'))


def _hsi_sprites(client_dir, rel):
    path = os.path.normcase(os.path.abspath(_client_path(client_dir, rel)))
    with _cache.lock:
        if path in _cache.hsi:
            return _cache.hsi[path]
    try:
        sprites = parse_hsi(EC.decode_hs_text(path))
    except (OSError, EC.ContentError):
        sprites = None                  # OpenFromFile fails -> base 0 -> tile dropped
    with _cache.lock:
        _cache.hsi[path] = sprites
    return sprites


def map_path(code, client_dir=None):
    code = int(code)
    return os.path.join(client_dir or EC.client_dir(), 'hs', f'stage{code // 100:02d}_{code % 100:02d}.hmi')


def parse_map(code, xml_bytes, client_dir=None):
    """MapData from the decoded .hmi bytes (the sprite indexes are read from client_dir)."""
    client_dir = client_dir or EC.client_dir()
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise MapError(f'map {code}: {e}') from e
    info = root.find('MapInfo')
    name_text = _int_attr(info, 'name') if info is not None else 0
    sprite_list = root.find('SpriteLists')
    hsi_files = [s.get('name', '') for s in (sprite_list if sprite_list is not None else [])]
    tiles, lines, missing = [], [], set()
    layers = root.find('LayerLists')
    for layer in (layers if layers is not None else []):
        layer_no = _int_attr(layer, 'no')
        for el in layer:
            hsi, sprite = _int_attr(el, 'hsi'), _int_attr(el, 'sprite')
            rel = hsi_files[hsi - 1] if 1 <= hsi <= len(hsi_files) else ''
            tile = Tile(layer_no, _int_attr(el, 'no'), hsi, rel, sprite,
                        _int_attr(el, 'pos_x'), _int_attr(el, 'pos_y'),
                        _int_attr(el, 'event'), _int_attr(el, 'value_num'), _int_attr(el, 'NpcId'))
            tiles.append(tile)
            sprites = _hsi_sprites(client_dir, rel) if rel else None
            if sprites is None:
                if rel:
                    missing.add(rel)
                continue
            if not 1 <= sprite <= len(sprites):
                continue
            for (ltype, x1, y1, x2, y2) in sprites[sprite - 1]:
                lines.append(Line(len(lines), ltype, x1 + tile.x, y1 + tile.y, x2 + tile.x,
                                  y2 + tile.y, tile.event, tile.value, tile))
    return MapData(int(code), name_text, tiles, lines, sorted(missing))


def load_map(code, client_dir=None):
    """The MapData of a map code (cached), or None when its .hmi is not installed."""
    client_dir = client_dir or EC.client_dir()
    key = (os.path.normcase(os.path.abspath(client_dir)), int(code))
    with _cache.lock:
        if key in _cache.maps:
            return _cache.maps[key]
    try:
        data = parse_map(code, EC.decode_hs(map_path(code, client_dir)), client_dir)
    except (OSError, EC.ContentError, MapError) as e:
        log.debug(f'[MAPS] map {code}: {e}')
        data = None
    with _cache.lock:
        _cache.maps[key] = data
    return data


def map_codes(client_dir=None):
    """Every map code with an .hmi in the client (stageAA_BB.hmi -> AA*100 + BB)."""
    out = []
    for path in glob.glob(os.path.join(client_dir or EC.client_dir(), 'hs', 'stage*.hmi')):
        m = re.fullmatch(r'stage(\d{2})_(\d{2})\.hmi', os.path.basename(path), re.I)
        if m:
            out.append(int(m.group(1)) * 100 + int(m.group(2)))
    return sorted(out)


# ------------------------------------------------------ instance dungeons ---
# EN 2009 (client-2009-world): instance-dungeon stages are their own map files,
# ./hs/indunAA_BB.hmi, loaded in place by S2C 0xA5 {u16 stage_code} (spec_2009 0xA5:
# code 105 -> indun01_05.hmi). Their codes are a SEPARATE namespace from the stage map codes
# of S2C 0x03/0x08 (indun 105 is not stage01_05 = map 105), so they are cached under their
# own key and never reach the portal table. The 2008 client has none.
INDUN_PREFIX = 'indun'


def indun_path(code, client_dir=None):
    code = int(code)
    return os.path.join(client_dir or EC.client_dir(), 'hs', f'{INDUN_PREFIX}{code // 100:02d}_{code % 100:02d}.hmi')


def indun_codes(client_dir=None):
    """Every instance-dungeon stage code with an .hmi (indunAA_BB.hmi -> AA*100 + BB)."""
    out = []
    for path in glob.glob(os.path.join(client_dir or EC.client_dir(), 'hs', f'{INDUN_PREFIX}*.hmi')):
        m = re.fullmatch(r'indun(\d{2})_(\d{2})\.hmi', os.path.basename(path), re.I)
        if m:
            out.append(int(m.group(1)) * 100 + int(m.group(2)))
    return sorted(out)


def load_indun(code, client_dir=None):
    """The MapData of an instance-dungeon stage (cached apart from the stage maps), or None."""
    client_dir = client_dir or EC.client_dir()
    key = (os.path.normcase(os.path.abspath(client_dir)), INDUN_PREFIX, int(code))
    with _cache.lock:
        if key in _cache.maps:
            return _cache.maps[key]
    try:
        data = parse_map(code, EC.decode_hs(indun_path(code, client_dir)), client_dir)
    except (OSError, EC.ContentError, MapError) as e:
        log.debug(f'[MAPS] indun {code}: {e}')
        data = None
    with _cache.lock:
        _cache.maps[key] = data
    return data


def hmi_inventory(client_dir=None):
    """{'stage': [...], 'indun': [...], 'other': [...]} file names of every .hmi a client
    install carries (2009: 266 stage + 18 indun + main99_01/02/04 + preview99_01 = 288; the
    'other' four are the select-screen / preview scenes, not world maps)."""
    out = {'stage': [], 'indun': [], 'other': []}
    for path in sorted(glob.glob(os.path.join(client_dir or EC.client_dir(), 'hs', '*.hmi'))):
        name = os.path.basename(path)
        if re.fullmatch(r'stage\d{2}_\d{2}\.hmi', name, re.I):
            out['stage'].append(name)
        elif re.fullmatch(r'indun\d{2}_\d{2}\.hmi', name, re.I):
            out['indun'].append(name)
        else:
            out['other'].append(name)
    return out


# ------------------------------------------------------------ portal table ---
def arrival_point(dest, source_code):
    """(x, y, how) for a player arriving in `dest` (MapData) from map `source_code`."""
    reverse = [ln for ln in dest.portal_lines() if ln.value == source_code]
    how = f'reverse portal {dest.code}_{reverse[0].index}' if reverse else None
    candidates = reverse or dest.portal_lines()
    if not candidates:
        return None
    if how is None:
        how = f'no reverse portal: first portal line {dest.code}_{candidates[0].index}'
    ln = candidates[0]
    return (ln.x1 + ln.x2) / 2.0, float(ln.y1) - ARRIVAL_ABOVE_LINE, how


def build_portal_table(client_dir=None, codes=None):
    """({"<map>_<index>": row}, report) from every installed EN map.

    row = {dest, x, y, tile: [pos_x, pos_y], line: [x1, y1, x2], arrival: how}."""
    client_dir = client_dir or EC.client_dir()
    codes = map_codes(client_dir) if codes is None else list(codes)
    maps = {c: load_map(c, client_dir) for c in codes}
    rows, report = {}, {'maps': 0, 'unreadable_maps': [], 'portal_tiles': 0,
                         'tiles_without_line': [], 'unknown_destination': [], 'missing_hsi': {}}
    for code in codes:
        data = maps[code]
        if data is None:
            report['unreadable_maps'].append(code)
            continue
        report['maps'] += 1
        if data.missing_hsi:
            report['missing_hsi'][code] = data.missing_hsi
        tiles = data.portal_tiles()
        report['portal_tiles'] += len(tiles)
        with_line = {id(ln.tile) for ln in data.portal_lines()}
        for t in tiles:
            if id(t) not in with_line:
                # No type-0 line: the client can never send an index for this tile.
                report['tiles_without_line'].append([code, t.x, t.y, t.value])
        for ln in data.portal_lines():
            dest_code = ln.value
            dest = maps.get(dest_code) if dest_code in maps else load_map(dest_code, client_dir)
            arrival = arrival_point(dest, code) if dest is not None and dest_code else None
            if arrival is None:
                report['unknown_destination'].append([code, ln.index, dest_code])
                continue
            x, y, how = arrival
            rows[f'{code}_{ln.index}'] = {'dest': dest_code, 'x': x, 'y': y,
                                          'tile': [ln.tile.x, ln.tile.y],
                                          'line': [ln.x1, ln.y1, ln.x2], 'arrival': how}
    report['rows'] = len(rows)
    report['index_0'] = sorted(int(k.split('_')[0]) for k in rows if k.endswith('_0'))
    return rows, report


def check_live_portals(rows, build=None):
    """The live-captured indexes the table must reproduce; returns the mismatches."""
    bad = []
    for (code, index), dest in live_portals(build).items():
        row = rows.get(f'{code}_{index}')
        if row is None or row['dest'] != dest:
            bad.append(f'{code}_{index}: expected -> {dest}, table has {row and row["dest"]}')
    return bad


def write_portal_table(path=None, client_dir=None):
    """Generate the active build's portal table (EC.portals_en_path) from client_dir."""
    path = path or EC.portals_en_path()
    rows, report = build_portal_table(client_dir)
    bad = check_live_portals(rows)
    if bad:
        raise MapError('generated portal table contradicts the live captures: ' + '; '.join(bad))
    build = EC.client_build()
    doc = {
        '_comment': ('Generated by en_maps.py from the EN client hs/stageAA_BB.hmi + .hsi files '
                     '(world-portal-table-en). Key "<map>_<collision line index>" = the u32 of '
                     'C2S 0x7E; dest = the tile value_num; x/y = arrival point. Do not edit: '
                     're-run `python en_maps.py'
                     + (f' --build {build}' if build != '2008' else '')
                     + '`.'),
        'source': 'EN client hs/*.hmi + .hsi (FUN_00406170 line order)',
        **({'client_build': build} if build != '2008' else {}),
        'live_asserted': {f'{c}_{i}': d for (c, i), d in sorted(live_portals(build).items())},
        'report': report,
        'portals': dict(sorted(rows.items(), key=lambda kv: tuple(int(p) for p in kv[0].split('_')))),
    }
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(doc, f, indent=1)
        f.write('\n')
    os.replace(tmp, path)
    return rows, report


def main(argv=None):
    ap = argparse.ArgumentParser(description='Generate server/portals_en.json from the EN map files '
                                             '(--build 2009: portals_en_2009.json from the 2009 install).')
    ap.add_argument('--client-dir', default=None, help='client install holding hs/ (default: .. or, '
                                                       'with --build 2009, config CLIENT_DIR_2009)')
    ap.add_argument('--build', default='2008', choices=sorted(LIVE_PORTALS_BY_BUILD))
    ap.add_argument('--out', default=None, help='default: the build\'s table (en_content.portals_en_path)')
    ap.add_argument('--check', action='store_true', help='only compare against the existing file')
    args = ap.parse_args(argv)
    client_dir = args.client_dir
    if client_dir is None and args.build != '2008':
        import config as cfgmod
        client_dir = cfgmod.DEFAULTS['CLIENT_DIR_2009']
    EC.configure(client_dir, args.build)
    args.out = args.out or EC.portals_en_path(args.build)
    if args.check:
        rows, _ = build_portal_table()
        with open(args.out, encoding='utf-8') as f:
            stored = json.load(f)['portals']
        same = stored == json.loads(json.dumps(rows))
        print(f'{args.out}: {"up to date" if same else "STALE - re-run without --check"} '
              f'({len(rows)} generated, {len(stored)} stored)')
        return 0 if same else 1
    rows, report = write_portal_table(args.out)
    print(f'{args.out}: {len(rows)} portals from {report["maps"]} maps '
          f'({report["portal_tiles"]} portal tiles, {len(report["tiles_without_line"])} without a '
          f'floor line, {len(report["index_0"])} genuine index-0 portals); live anchors OK: '
          f'{", ".join(f"{c}_{i}->{d}" for (c, i), d in sorted(live_portals(args.build).items()))}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
