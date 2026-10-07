#!/usr/bin/env python3
"""
bosses.py - field bosses and drop rolls, both client builds (P13 stages 3 and 4)
===============================================================================
Roadmap items (re_tools/docs/ROADMAP_2009_ADDENDUM.md P13 + 3.0 arch09-channel-key; design
re_tools/docs/systems_2009/events_bosses.md B1-B8, B10, C, E-B1..E-B4):

    boss-b1   the boss ledger: one entry per field-boss tile, keyed (channel, map, tile), with
              the respawn after value_num * BOSS_RESPAWN_SCALE seconds; persisted in a small
              JSON next to accounts.json, so it holds across a map discard (MOB_MAP_KEEP_SECS),
              the last player leaving and a server restart
    boss-b2   the drop roll per hni Drop entry at rate / DROP_RATE_UNIT, the DROP_MODE switch;
              the loot keeps the P4 ground-loot ownership (the killer's for 15 s)
    boss-b3   boss combat: attack A / attack B / dash from AI[0] / AI[8] / AI[3] through the
              P5 chase (0x2A), B and the dash behind MOB_ATTACK_B / MOB_DASH; a victim's
              swing event hurts with Weak_Atk (7/8) or Strong_Atk (4/5/9/10) only after the
              server commanded that kind of swing (section "Boss combat" below)
    boss-b4   boss quests: the kill quests (181/182/184/165/205/215) take the P2 ReqPro path
              (S2C 0x59 on the kill), the trophy quests (6/64/104/137/144/210) the P2 Demand
              path (pickup -> 0x59 re-check, C2S 0x17 -> 0x27 + the 0x21 of award_exp);
              `!boss quests` lists them, `!boss quest <id>` puts one in a GM's log
    boss-b5 / boss-b6 (announcements, pack assist: optional, not retail-verified) are NOT
              implemented: the client has no boss text and no assist (evb B4, B8).

What a field boss is
--------------------
Nothing in the client marks one (evb B1, VERIFIED): the hni template has no boss column and a
boss is an ordinary S2C 0x1A monster (evb B10). The map data does: exactly 11 event-2 monster
tiles of the 2009 maps carry value_num 300 - Rynx (Foothill 218), Monkey King (409), 2 x
Wasablanca (511), Leo Wolf (619), Drill Mole (819), Wook (920), King Frog / Waterfrog /
Firefrog (1017 / 1018 / 1021) and Blue Shark (1111) - and each of them drops a quest trophy at
rate 99990. The 2008 maps have 15 such tiles (Rynx on 208, plus a second Wasablanca, Leo Wolf,
Drill Mole and Wook map and the Atomic Ball on 709). The client never reads a monster tile's
value_num (FUN_00445970 only copies it for town NPCs), so it is server data; the server reads
it as the respawn time in seconds (INFERRED, config BOSS_RESPAWN_SCALE). Config
BOSS_TILE_VALUES ([300]) is the rule; the one Crow tile at 901 is not a boss (evb B2).

Ledger
------
    key   (channel, map_code, (tile_x, tile_y))           arch09-channel-key: the channel is
          the map instance's (MapMonsters.channel once P12 keys maps by channel), else the
          first CHANNELS entry - one game server is one channel today
    row   npc, name, value, alive, next_spawn_at (UTC epoch s), last_kill_at, last_killer, kills

The GameServer's monster lifecycle stays what P5 made it (world-shared-monsters: one Monster per
tile and map, the same uid on every client there; 0x29 on the kill, 0x06 after
MOB_CORPSE_SECS, a fresh 0x1A at the respawn). Only the respawn timer of a boss changes:
  - a kill writes `next_spawn_at = now + value * BOSS_RESPAWN_SCALE` and the corpse's respawn
    timer is set to that (instead of MOB_RESPAWN_SECS);
  - a map (re)built after a discard, or after a restart, spawns a boss only when the ledger
    has it alive or due; a boss still down is built dead and gone (no client gets its 0x1A),
    with a respawn timer for the rest of its time - so leaving and re-entering, or restarting
    the server, is no free respawn (evb B5, C);
  - the respawn's 0x1A reaches every client on the map, like any respawn, and marks it alive.

Saving follows the livefix store (store.py "Saving", livetest bug 7): the kill runs under the
killer's combat lock and the map's monster lock, so it only changes the ledger in memory (its
own leaf lock) and asks the tick thread for a save; the save takes a snapshot under the leaf
lock and writes it OUTSIDE it (store.atomic_write: temp file + fsync + os.replace), one writer
at a time, an older snapshot never replacing a newer one; a failed write stays dirty and is
retried RETRY_SECS later. The tick thread's saves (the scheduled save, its retry, the
maintenance save: Bosses.tick_flush) get only store.QUICK_REPLACE_DELAYS of replace
backoff and never wait for another writer, like store.Store.tick_flush (livefix review of bug
7: the ~3 s backoff held the world lock); only the shutdown flush takes the full ~3 s. The file:

    {"version": 1, "bosses": [{"channel": 1, "map": 218, "tile": [1609, 1835], "npc": 12,
      "name": "Rynx", "value": 300, "alive": false, "next_spawn_at": 1790000300.0,
      "last_kill_at": 1790000000.0, "last_killer": "TestHero", "kills": 1}, ...]}

A malformed file is moved aside (<file>.bad-<time>) and the server starts with an empty
ledger (every boss up). A file that cannot be READ (permissions, a lock held by another
process) - or a malformed one that cannot be moved aside - stops the server start with a
LedgerError instead: starting empty would let the first save overwrite every boss timer and
kill count in it, as Store.load refuses to replace an accounts file it cannot read.

Drops (boss-b2)
---------------
`item:` (120 x %04d ids) and `Drop:` (120 x %06d rates) are parallel columns: entry i of Drop
is the rate of item i (evb B6, template+0x288 / +0x468). The client never rolls a drop. The
unit is 1/100000 (INFERRED: every trophy is 99990 = 99.99 %, cards 10-50). DROP_MODE:
  'rates'   (default, evb E-B2) every entry rolls on its own: rng.random() < rate / unit -
            a boss trophy 99.99 %, Wasablanca's two items 50 % / 49.99 % each (both 25 %);
  'single'  one draw over the same column: the same chance per entry, at most one item a kill.
            Data note: every monster table of both builds sums to <= 100000 and all 12 boss
            tables to exactly 100000 (Rynx 99990 + card 10), which is what a one-draw table
            looks like - evb open question G2 (independent or pick-one) stays open;
  'legacy'  the P0 roll: 60 %, then one entry picked uniformly (rates ignored).
The loot goes where it always went (GameServer._kill_monster): on the ground at the corpse,
owned by the killer (S2C 0x12 owner_uid; for 15 s every other client refuses the pickup with
"You don't have ownership of this item.", FUN_0043d5c0), or with GROUND_LOOT false into the
killer's bag with one S2C 0x18 per item.

Boss combat (boss-b3)
---------------------
A boss fights with the chase every aggroed monster runs (mobai.decide, GameServer MONSTER
AGGRO): the template's AI ints are copied onto the entity by the 0x1A handler, and the server
ships its decisions as the 16-byte S2C 0x2A self-form {mob uid, lo, hi 0, receiver's own uid}
to EVERY client holding the mob (world-shared-monsters), so both clients see each swing. The
words (lo = direction | motion << 2, MONSTER_AGGRO_RE; mobai):

    attack A   05 left / 06 right   AI[0] (+0xE50), state 4/0xE, the victim reports event 7/8
    attack B   15 left / 16 right   AI[8] (+0xE70), state 1/0xF, event 9/10 (4/5 into a guard)
    dash       19 left / 1A right   AI[3] (+0xE5C), state 6 while held (0x4150F0..0x415115)

e.g. the Monkey King (uid 0x000F0123) swinging attack B to the right, sent to uid 1:
`23 01 0F 00 16 00 00 00 00 00 00 00 01 00 00 00`. Attack A is live-verified (Monkey Soldier,
LIVE_TEST_LOG 2026-09-24); attack B and the dash are NOT (evb B10, T-B3), so command_flags
masks them per config MOB_ATTACK_B / MOB_DASH ('all' / 'bosses' / 'off'). The field bosses'
AI (evb B2): Monkey King 1101001010000 = A + dash + counter-jump + B; Rynx 1100001010000 = A +
B; Wasablanca and the 2009 sea/frog bosses A + B (+ dash). Monkey Lord (hni 140, "Monkey
Farmer" in the 2008 names) is a Lv 12 regular monkey, 1100001000000 = attack A only, Strong_Atk
0 (evb B3): it never gets 15/16/19/1A in any mode.

The damage of a boss swing is the victim's report (GameServer._monster_contact): the event
picks the stat (1/6 Body_Atk, 7/8 Weak_Atk, 4/5/9/10 Strong_Atk; 2/3 nothing) and the client's
formula does the rest (damage.py). Monkey King: Body 29 / Weak 24 / Strong 35. A swing event
hurts only when the server commanded THAT kind of swing within MOB_ATTACK_EVENT_SECS
(swing_age): an event 9 from a mob only ever told to swing attack A - Monkey Lord, or any mob
with MOB_ATTACK_B off - is the client's own wander (an unpatched exe) and does nothing, and so
is an event 7 from a mob that only swung attack B. A mismatch is logged ("swing event 9 with no
attack B commanded ..."), which is also what shows a wrong live assumption about lo 15/16.

Boss quests (boss-b4)
---------------------
No boss-specific quest packet exists (evb B10). The 2009 hqi has (the 2008 one the same ids,
plus 102 / 183 for its Atomic Ball on 709):
  - kill quests (hqi NPC = a boss npccode, ReqPro): 181 Rynx x1, 182 Monkey King x1, 184
    Wasablanca x1 (Way of the Assassin 1/2/4), 165 / 205 / 215 Wasablanca x2 (Berserker /
    Elemental Magic / Bishop 4). GameServer._kill_monster credits the killer (QuestState.
    credit_kill: the first held slot that wants the npccode and is not full) and sends S2C 0x59
    {slot, progress}; at progress >= ReqPro the client prints "[<title>] You are ready to
    complete the quest." (FUN_00426500);
  - trophy quests (hqi Demand = a boss drop at >= TROPHY_MIN_RATE): 6 Rynx's Chest Fur 124, 64
    Monkey King's Gold Diadem 1291 (Mei; reward 1290 Yellow Stripe Hat, 100 exp), 104 / 210
    Horseradish 1559 / Sunflower 1582, 137 Drill 167, 144 Leo Wolf Mane 168. The trophy drops
    owned by the killer (boss-b2), its pickup re-sends the slot's 0x59 (the client re-checks
    the bag), and C2S 0x17 turns it in: 0x27 (the client removes the Demand, grants the
    Reward) + the quest exp through award_exp (arch09-exp-pipeline).
boss_quests() derives both lists from the data, so they follow the build's hqi / hni.

GM
--
`!boss [list]`: every boss tile of the map data with its ledger state (alive, the live uid
and HP when its map is loaded; down with the time left and the killer). `!boss respawn [all |
<map> | <name>]` (default: the GM's map): a down boss is due now - on a loaded map its 0x1A
goes out at once, else it spawns on the next arrival. `!boss quests`: the boss quests above;
`!boss quest <id>`: a GM puts one of them into his own quest log (S2C 0x26 + 0x59, the event
push without the level / job / PrevQuest gates - a live-test shortcut to "a Lv 30 Rogue
holding quest 182").
"""
import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass

import config as cfgmod
import en_content as EC
import gm
import mobai
import quests as questmod
import store as storemod

log = logging.getLogger('WS')

LEDGER_VERSION = 1
# A failed ledger write is retried this long after (store.Store.RETRY_SECS).
RETRY_SECS = 1.0
# DROP_MODE 'legacy': the P0 roll (windslayer_server.py before P13: 60 %, one entry).
LEGACY_DROP_CHANCE = 0.6
DEFAULT_DROP_RATE_UNIT = cfgmod.DEFAULTS['DROP_RATE_UNIT']
DEFAULT_TILE_VALUES = tuple(cfgmod.DEFAULTS['BOSS_TILE_VALUES'])


# ====================================================================== drops ===
def roll_drops(mob, mode='rates', unit=DEFAULT_DROP_RATE_UNIT, rng=None):
    """The item ids one kill of `mob` drops (each x1), by DROP_MODE (module docstring).
    'rates' / 'single' read mob.drop_table [(item, rate)]; 'legacy' reads mob.drop_items.
    `rng` has .random() / .choice() (the random module by default), so a test can force it."""
    rng = rng or random
    if mode == 'legacy':
        if mob.drop_items and rng.random() < LEGACY_DROP_CHANCE:
            return [rng.choice(mob.drop_items)]
        return []
    unit = max(1, int(unit))
    table = [(int(i), int(r)) for i, r in (getattr(mob, 'drop_table', None) or ()) if i and int(r) > 0]
    if mode == 'single':
        roll = rng.random() * unit
        for item, rate in table:
            if roll < rate:
                return [item]
            roll -= rate
        return []
    return [item for item, rate in table if rng.random() < rate / unit]


# ==================================================================== catalog ===
@dataclass(frozen=True)
class BossSpawn:
    """One field-boss tile of the map data (evb B2 catalog row)."""
    map_code: int
    tile: tuple
    npc: int
    name: str
    value: int
    x: float
    y: float


_catalog = {}
_catalog_lock = threading.Lock()
# One map tile element of a decoded .hmi (`<Tile no=.. pos_x=.. event="2" value_num="300"
# NpcId="12" />`, the attributes FUN_00407800 reads) and its attributes.
_TILE_TAG = re.compile(rb'<Tile\s[^>]*>')
_ATTR = re.compile(rb'(\w+)="([^"]*)"')


def _has_boss_tile(code, values):
    """True when map `code`'s .hmi holds an event-2 tile with value_num in `values` - a cheap
    text scan (every map in ~0.3 s), so only those maps are fully loaded (floors, spawns)."""
    import en_maps                      # late: en_maps imports en_content
    try:
        data = EC.decode_hs(en_maps.map_path(code))
    except (OSError, EC.ContentError):
        return False
    for tag in _TILE_TAG.findall(data):
        attrs = dict(_ATTR.findall(tag))
        if attrs.get(b'event') != b'2':
            continue
        try:
            if int(attrs.get(b'value_num', b'0')) in values:
                return True
        except ValueError:
            continue
    return False


def catalog(tile_values=DEFAULT_TILE_VALUES):
    """[BossSpawn] of every map of the configured client install (en_content.configure): the
    monster tiles whose value_num is in `tile_values`, in map order. The first call scans every
    .hmi as text and loads only the maps with such a tile (en_content.map_spawns: a monster
    template, the spawn point on the floor); cached per install. Only `!boss` and the tests
    need the whole list - the server classifies each map's spawns when it builds them."""
    import en_maps                      # late: en_maps imports en_content
    values = frozenset(int(v) for v in tile_values)
    key = (EC.client_dir(), EC.client_build(), values)
    with _catalog_lock:
        cached = _catalog.get(key)
    if cached is not None:
        return cached
    out = []
    npcs = EC.npcs()
    for code in en_maps.map_codes():
        if not _has_boss_tile(code, values):
            continue
        for sp in EC.map_spawns(code) or ():
            if int(sp.value) not in values:
                continue
            tpl = npcs.get(sp.npc)
            out.append(BossSpawn(int(code), tuple(int(v) for v in sp.tile), int(sp.npc),
                                 tpl.name if tpl is not None else f'npc {sp.npc}', int(sp.value),
                                 float(sp.x), float(sp.y)))
    with _catalog_lock:
        _catalog[key] = out
    return out


# ================================================================ boss combat ===
# boss-b3 (module docstring "Boss combat"). The victim events of each swing kind: 7/8 attack A
# (8 = on an airborne victim), 9/10 attack B, 4/5 attack B into the victim's guard (FUN_00416ab0
# 0x417B12/0x417B1E); 2/3 (attack A into a guard) hurt nobody. GameServer.MOB_HIT_STATS maps
# the same events to Weak_Atk / Strong_Atk.
SWING_A_EVENTS = frozenset((7, 8))
SWING_B_EVENTS = frozenset((4, 5, 9, 10))


def is_boss(mob):
    """A field boss: a Monster built on a BOSS_TILE_VALUES tile (mob.boss = its ledger key)."""
    return getattr(mob, 'boss', None) is not None


def scope_allows(scope, mob):
    """MOB_ATTACK_B / MOB_DASH scope `scope` lets the chase command that word to `mob`."""
    if scope == 'all':
        return True
    if scope == 'bosses':
        return is_boss(mob)
    return False


def command_flags(mob, cfg):
    """The chase flags the server commands `mob` with (boss-b3): the template's (mob.flags:
    AI[0] attack A, AI[8] attack B, AI[3] dash, ...) with attack B and the dash kept only where
    config MOB_ATTACK_B / MOB_DASH allow them - the two words not yet live-verified (evb B10).
    A mob without the flag never gets the word (Monkey Lord: attack A only); attack A is never
    masked. With attack B masked, a mob with both kinds always picks A (mobai.decide)."""
    flags = mob.flags
    attack_b = flags.attack_b and scope_allows(cfg.get('MOB_ATTACK_B', 'all'), mob)
    skill = flags.skill and scope_allows(cfg.get('MOB_DASH', 'all'), mob)
    if attack_b == flags.attack_b and skill == flags.skill:
        return flags
    return flags._replace(attack_b=attack_b, skill=skill)


def note_command(mob, lo, now):
    """GameServer._mob_command sent `lo` to the mob at `now`: remember when each kind of swing
    was last commanded (swing_age)."""
    if not mobai.is_attack(lo):
        return
    if mobai.motion_of(lo) == mobai.MOTION_ATTACK_B:
        mob.ai_attack_b_t = now
    else:
        mob.ai_attack_a_t = now


def swing_kind(action):
    """'A' / 'B' for a victim swing event (C2S 0x0D action), None for anything else."""
    if action in SWING_A_EVENTS:
        return 'A'
    if action in SWING_B_EVENTS:
        return 'B'
    return None


def swing_age(mob, action, now):
    """Seconds since the server last commanded the swing kind that victim event `action`
    reports (inf when never since the mob's aggro state was cleared, or for a non-swing)."""
    kind = swing_kind(action)
    if kind is None:
        return float('inf')
    t = getattr(mob, 'ai_attack_b_t' if kind == 'B' else 'ai_attack_a_t', 0.0) or 0.0
    return now - t if t else float('inf')


# ================================================================ boss quests ===
# boss-b4 (module docstring "Boss quests"). A boss drop at this rate or more is a trophy: every
# boss trophy is 99990 and Wasablanca's pair 50000 / 49990, while the monster cards on the same
# tables are 10 (evb B2 / B6).
TROPHY_MIN_RATE = 1000


@dataclass(frozen=True)
class BossQuest:
    """One quest a field boss feeds (evb B7)."""
    quest: int
    kind: str                # 'kill' (hqi NPC + ReqPro) or 'trophy' (hqi Demand)
    npc: int                 # the boss npccode
    boss: str                # its EN name
    items: tuple = ()        # 'trophy': the demanded trophy ids
    need: int = 1            # 'kill': ReqPro; 'trophy': the demanded count of the first trophy


def boss_quests(tile_values=DEFAULT_TILE_VALUES):
    """[BossQuest] of the configured build's hqi, in quest order: every quest whose kill
    counter a field boss feeds (NPC = a boss npccode with ReqPro > 0) and every quest that
    demands a boss trophy (a drop of a boss template at >= TROPHY_MIN_RATE). Both builds carry
    the same ids: 181/182/184/165/205/215 kill, 6/64/104/137/144/210 trophy (2008 adds its own
    extra boss tiles' quests, if any)."""
    npcs = EC.npcs()
    bosses = {}
    for b in catalog(tile_values):
        bosses.setdefault(b.npc, b.name)
    trophies = {}
    for npc in bosses:
        tpl = npcs.get(npc)
        for item, rate in (tpl.drops if tpl is not None else ()):
            if item and int(rate) >= TROPHY_MIN_RATE:
                trophies.setdefault(int(item), npc)
    out = []
    catalog_q = EC.quests()
    for quest_id in sorted(catalog_q.defs):
        q = catalog_q.defs[quest_id]
        if q.reqpro and q.npc in bosses:
            out.append(BossQuest(quest_id, 'kill', q.npc, bosses[q.npc], need=int(q.reqpro)))
            continue
        wanted = [(item, need) for item, need in q.demand if item in trophies]
        if wanted:
            npc = trophies[wanted[0][0]]
            out.append(BossQuest(quest_id, 'trophy', npc, bosses[npc], tuple(i for i, _ in wanted),
                                 int(wanted[0][1])))
    return out


# ===================================================================== ledger ===
@dataclass
class BossEntry:
    channel: int
    map_code: int
    tile: tuple
    npc: int
    name: str = ''
    value: int = 0
    alive: bool = True
    next_spawn_at: float = 0.0       # UTC epoch s: when a boss that is down may come back
    last_kill_at: float = 0.0
    last_killer: str = ''
    kills: int = 0

    @property
    def key(self):
        return (self.channel, self.map_code, self.tile)

    def to_json(self):
        return {'channel': self.channel, 'map': self.map_code, 'tile': list(self.tile), 'npc': self.npc,
                'name': self.name, 'value': self.value, 'alive': self.alive,
                'next_spawn_at': round(float(self.next_spawn_at), 3),
                'last_kill_at': round(float(self.last_kill_at), 3), 'last_killer': self.last_killer,
                'kills': self.kills}

    @classmethod
    def from_json(cls, row):
        tile = row['tile']
        if not isinstance(tile, (list, tuple)) or len(tile) != 2:
            raise ValueError(f'tile {tile!r} is not [x, y]')
        return cls(channel=int(row['channel']), map_code=int(row['map']), tile=(int(tile[0]), int(tile[1])),
                   npc=int(row['npc']), name=str(row.get('name', '')), value=int(row.get('value', 0)),
                   alive=bool(row.get('alive', True)), next_spawn_at=float(row.get('next_spawn_at', 0.0)),
                   last_kill_at=float(row.get('last_kill_at', 0.0)),
                   last_killer=str(row.get('last_killer', '')), kills=int(row.get('kills', 0)))


class LedgerError(storemod.StoreError):
    """The boss ledger file exists but cannot be read or moved aside: the server refuses to
    start rather than replace it with an empty ledger (BossLedger.load)."""


def make_key(channel, map_code, tile):
    return (int(channel), int(map_code), (int(tile[0]), int(tile[1])))


class BossLedger:
    """The persisted boss ledger (boss-b1, evb C). `lock` is a leaf: nothing is sent, no other
    lock is taken and no file is written while it is held (the world.py ladder ends in it)."""

    def __init__(self, path, clock=time.time):
        self.path = path or ''                  # '' = memory only
        self.clock = clock                      # UTC wall clock: next_spawn_at survives a restart
        self.lock = threading.Lock()
        self.entries = {}
        self.dirty = False
        self.saves = 0
        self._changes = 0                       # mutations; a snapshot covers a count of them
        self._gen = 0                           # generation of the last snapshot taken
        self._disk_gen = 0                      # generation on disk
        self._save_mutex = threading.Lock()     # one writer at a time (store._save_mutex)

    # ---------------------------------------------------------------- file ---
    def load(self):
        """Read the file (a missing one is an empty ledger). Returns the number of entries.
        A malformed file is moved aside and the ledger starts empty; LedgerError when the file
        cannot be read, or a malformed one cannot be moved aside - nothing is changed then, and
        the caller (the server start) stops rather than overwrite it at the first save."""
        if not self.path or not os.path.exists(self.path):
            return 0
        try:
            with open(self.path, 'rb') as f:
                raw = f.read()
        except OSError as e:
            log.error(f'[BOSS] cannot read the boss ledger {self.path}: {e}; not starting over it')
            raise LedgerError(f'{self.path}: cannot be read ({e}); fix it or point BOSS_LEDGER_FILE '
                              f'elsewhere - the server will not replace it with an empty ledger') from e
        try:
            data = json.loads(raw.decode('utf-8'))
            rows = data.get('bosses') if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise ValueError('no "bosses" list')
        except ValueError as e:                     # UnicodeDecodeError / JSONDecodeError too
            bad = f'{self.path}.bad-{int(time.time())}'
            try:
                os.replace(self.path, bad)
            except OSError as moved:
                log.error(f'[BOSS] boss ledger {self.path} is malformed ({e}) and cannot be moved aside '
                          f'({moved}); not starting over it')
                raise LedgerError(f'{self.path}: malformed ({e}) and cannot be moved aside ({moved}); '
                                  f'the server will not replace it with an empty ledger') from e
            log.error(f'[BOSS] boss ledger {self.path} is malformed ({e}); moved to {bad}, every boss starts up')
            return 0
        entries = {}
        for row in rows:
            try:
                entry = BossEntry.from_json(row)
            except (KeyError, TypeError, ValueError) as e:
                log.warning(f'[BOSS] boss ledger row {row!r} skipped: {e}')
                continue
            entries[entry.key] = entry
        with self.lock:
            self.entries = entries
        down = sum(1 for e in entries.values() if not e.alive)
        log.info(f'[BOSS] boss ledger {self.path}: {len(entries)} boss tile(s), {down} down')
        return len(entries)

    def _mark(self):
        """Caller holds self.lock."""
        self.dirty = True
        self._changes += 1

    def snapshot(self):
        with self.lock:
            return {'version': LEDGER_VERSION,
                    'bosses': [e.to_json() for e in sorted(self.entries.values(), key=lambda e: e.key)]}

    def flush(self, delays=None, wait=None):
        """Write the ledger if it changed (store.Store._write_snapshot's rules). Returns True
        when this snapshot or a newer one is on disk, False when there was nothing to write.
        Raises OSError when the write failed (the ledger stays dirty). `delays`: atomic_write's
        replace backoff (None = ~3 s); `wait`: seconds to wait for another writer (None = as
        long as it takes; past it TimeoutError, an OSError, and the ledger stays dirty)."""
        if not self.path:
            return False
        with self.lock:
            if not self.dirty:
                return False
            self._gen += 1
            gen, covers = self._gen, self._changes
            snap = {'version': LEDGER_VERSION,
                    'bosses': [e.to_json() for e in sorted(self.entries.values(), key=lambda e: e.key)]}
        data = json.dumps(snap, indent=1).encode('utf-8')
        wrote = False
        if wait is None:
            self._save_mutex.acquire()
        elif not self._save_mutex.acquire(timeout=max(0.0, float(wait))):
            raise TimeoutError(f'another save is writing {self.path}')
        try:
            if gen > self._disk_gen:
                storemod.atomic_write(self.path, data, delays)
                self._disk_gen = gen
                wrote = True
        finally:
            self._save_mutex.release()
        with self.lock:
            if wrote:
                self.saves += 1
            if self._changes == covers:
                self.dirty = False
        return True

    # ------------------------------------------------------------- entries ---
    def get(self, key):
        with self.lock:
            return self.entries.get(key)

    def rows(self):
        with self.lock:
            return sorted(self.entries.values(), key=lambda e: e.key)

    def ensure(self, key, npc, name, value):
        """The entry of a boss tile, created alive on its first sight. A tile whose template
        changed (another client install) starts over. Returns (entry, created)."""
        with self.lock:
            entry = self.entries.get(key)
            if entry is not None and entry.npc == int(npc):
                if entry.value != int(value) or entry.name != name:
                    entry.value, entry.name = int(value), str(name)
                    self._mark()
                return entry, False
            channel, map_code, tile = key
            entry = BossEntry(channel=channel, map_code=map_code, tile=tile, npc=int(npc), name=str(name),
                              value=int(value))
            self.entries[key] = entry
            self._mark()
            return entry, True

    def wait(self, key, now=None):
        """Seconds until the boss of `key` may be up: 0 when it is alive, due or unknown."""
        now = self.clock() if now is None else now
        with self.lock:
            entry = self.entries.get(key)
            if entry is None or entry.alive:
                return 0.0
            return max(0.0, float(entry.next_spawn_at) - now)

    def mark_dead(self, key, respawn_secs, killer='', now=None):
        now = self.clock() if now is None else now
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                return None
            entry.alive = False
            entry.last_kill_at = now
            entry.next_spawn_at = now + max(0.0, float(respawn_secs))
            entry.last_killer = str(killer or '')
            entry.kills += 1
            self._mark()
            return entry

    def mark_alive(self, key):
        """True when the entry changed (it was down)."""
        with self.lock:
            entry = self.entries.get(key)
            if entry is None or entry.alive:
                return False
            entry.alive = True
            self._mark()
            return True

    def force_due(self, key, now=None):
        """A down boss is due now (`!boss respawn`). Returns the entry, or None when unknown."""
        now = self.clock() if now is None else now
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                return None
            if not entry.alive and entry.next_spawn_at > now:
                entry.next_spawn_at = now
                self._mark()
            return entry


def ledger_path(cfg, accounts_path):
    """BOSS_LEDGER_FILE next to the accounts file ('' = no file). Tests run on a temp
    accounts.json, so their ledger lands in the same temp directory."""
    name = str(cfg.get('BOSS_LEDGER_FILE', cfgmod.DEFAULTS['BOSS_LEDGER_FILE']) or '')
    if not name:
        return ''
    if os.path.isabs(name):
        return name
    return os.path.join(os.path.dirname(os.path.abspath(accounts_path)), name)


def fmt_secs(secs):
    secs = max(0, int(round(secs)))
    return f'{secs // 60}m{secs % 60:02d}s' if secs >= 60 else f'{secs}s'


# ==================================================================== runtime ===
class Bosses:
    """The field bosses of one GameServer (server.bosses): the ledger and the hooks the
    monster lifecycle calls. Every hook runs under the map's monster lock (the caller's) and
    only touches the ledger (a leaf); the file write goes to the tick thread."""

    def __init__(self, server, clock=None):
        self.server = server
        self.ledger = BossLedger(ledger_path(server.config, server.store.path), clock or time.time)
        self.ledger.load()
        self._plock = threading.Lock()
        self._pending = None
        # A run of failed ledger writes (store.Store._failures / _failure_logged): the
        # traceback of the first one only, then one line each until a write succeeds.
        self._failures = 0
        self._failure_logged = False

    @property
    def clock(self):
        return self.ledger.clock

    @clock.setter
    def clock(self, fn):
        self.ledger.clock = fn

    # ------------------------------------------------------------- config ---
    def tile_values(self):
        return frozenset(int(v) for v in self.server.config.get('BOSS_TILE_VALUES', DEFAULT_TILE_VALUES))

    def respawn_secs(self, value):
        """value_num * BOSS_RESPAWN_SCALE (evb B5): 300 s for every field boss at 1.0."""
        return max(0.0, float(value) * float(self.server.config.get('BOSS_RESPAWN_SCALE', 1.0)))

    def channel_of(self, mons):
        """arch09-channel-key: the channel of a map instance - its own once the channel model
        keys maps by channel (P18 ch-2), else the channel every channel's players share the
        map instances of today (channels.Channels.world_channel: CHANNELS[0]) - never the
        killer's listener, or one shared boss would be booked once per channel."""
        channel = getattr(mons, 'channel', None)
        if channel:
            return int(channel)
        chans = getattr(self.server, 'channels', None)
        if chans is not None:
            return int(chans.world_channel())
        channels = self.server.config.channels()
        return int(channels[0][0]) if channels else 1

    # -------------------------------------------------------------- hooks ---
    def on_populate(self, mons, placed):
        """A map's monsters were just built (GameServer._populate_map_monsters, mons.lock
        held): mark every boss tile's Monster (mob.boss = its ledger key) and build a boss
        the ledger has down as a corpse that is gone (alive False, despawned True: no client
        gets its 0x1A). Returns [(mob, seconds left)] for the caller's respawn timers."""
        values = self.tile_values()
        channel = self.channel_of(mons)
        down, changed = [], False
        for sp, mob in placed:
            if int(sp.value) not in values:
                continue
            key = make_key(channel, mons.map_code, sp.tile)
            entry, created = self.ledger.ensure(key, mob.npccode, mob.name, sp.value)
            changed = changed or created
            mob.boss = key
            wait = self.ledger.wait(key)
            if wait > 0:
                mob.alive, mob.hp, mob.despawned = False, 0, True
                down.append((mob, wait))
                log.info(f'[BOSS] {mob.name} uid={mob.uid:#x} on map {mons.map_code} tile {key[2]} is down: '
                         f'back in {fmt_secs(wait)} (ledger; killed by {entry.last_killer or "?"})')
            elif not entry.alive:
                changed = self.ledger.mark_alive(key) or changed
                log.info(f'[BOSS] {mob.name} uid={mob.uid:#x} on map {mons.map_code} tile {key[2]}: '
                         f'its respawn time passed while the map was not loaded - spawned')
            else:
                log.info(f'[BOSS] {mob.name} uid={mob.uid:#x} on map {mons.map_code} tile {key[2]} is up')
        if changed:
            self.request_save()
        return down

    def on_kill(self, mob, killer=None):
        """A boss died (GameServer._kill_monster, mons.lock held): the ledger holds it down
        for value * BOSS_RESPAWN_SCALE seconds. No-op for a regular monster."""
        key = getattr(mob, 'boss', None)
        if key is None:
            return None
        entry = self.ledger.get(key)
        if entry is None:                       # a hand-marked monster: start its row now
            entry, _ = self.ledger.ensure(key, mob.npccode, mob.name, 0)
        name = (killer.get('char_name') or killer.get('username') or '') if killer is not None else ''
        entry = self.ledger.mark_dead(key, self.respawn_secs(entry.value), name)
        self.request_save()
        log.info(f'[BOSS] {mob.name} uid={mob.uid:#x} (ch{key[0]} map {key[1]} tile {key[2]}) killed by '
                 f'{name or "nobody"}; back in {fmt_secs(self.ledger.wait(key))} (kill #{entry.kills})')
        return entry

    def respawn_delay(self, mob):
        """The respawn timer of a dead boss (seconds left in the ledger), None for a regular
        monster (MOB_RESPAWN_SECS)."""
        key = getattr(mob, 'boss', None)
        return None if key is None else self.ledger.wait(key)

    def on_respawn(self, mob):
        """A boss came back (GameServer._respawn_monster, mons.lock held)."""
        key = getattr(mob, 'boss', None)
        if key is not None and self.ledger.mark_alive(key):
            self.request_save()

    # --------------------------------------------------------------- save ---
    def request_save(self):
        """Write the ledger from the tick thread (never under the caller's locks). One pending
        save at a time; the write itself covers every change made before it runs."""
        if not self.ledger.path:
            return
        ticks = getattr(self.server, 'ticks', None)
        if ticks is None:
            self.flush()
            return
        with self._plock:
            if self._pending is not None:
                return
            self._pending = ticks.call_later(0, self._scheduled_flush, name='boss-ledger-save')

    def _scheduled_flush(self):
        with self._plock:
            self._pending = None
        self.tick_flush()

    def tick_flush(self):
        """flush() for the tick thread (the scheduled save, its retry, the maintenance save):
        only store.QUICK_REPLACE_DELAYS of replace backoff and no wait for another writer
        (store.Store.tick_flush) - a reader holding the file costs the tick ~0.1 s, not ~3 s,
        and the retry takes it from there."""
        return self.flush(delays=storemod.QUICK_REPLACE_DELAYS, wait=0)

    def flush(self, delays=None, wait=None):
        """Save now if anything changed (shutdown, tests; the tick thread: tick_flush). A
        failed write is logged (_log_failure), stays dirty and is retried RETRY_SECS later."""
        try:
            done = self.ledger.flush(delays=delays, wait=wait)
        except OSError as e:
            self._log_failure(e)
            ticks = getattr(self.server, 'ticks', None)
            if ticks is not None:
                with self._plock:
                    if self._pending is None:
                        self._pending = ticks.call_later(RETRY_SECS, self._scheduled_flush,
                                                         name='boss-ledger-retry')
            return False
        with self._plock:
            failed, self._failures, self._failure_logged = self._failures, 0, False
        if failed:
            log.info(f'[BOSS] boss ledger {self.ledger.path} saved after {failed} failed attempt(s)')
        return done

    def _log_failure(self, e):
        """A failed ledger write, as store.Store._log_failure logs the store's: the traceback
        once per run of failures, then one line each. The tick thread's saves never wait for
        another writer (tick_flush: wait 0), so a contended or briefly reader-held file fails
        every RETRY_SECS until it clears - one traceback, not one per retry. Called from the
        except block (log.exception takes its traceback)."""
        with self._plock:
            self._failures += 1
            n, first = self._failures, not self._failure_logged
            self._failure_logged = True
        if first:
            log.exception(f'[BOSS] saving the boss ledger {self.ledger.path} failed; kept dirty, '
                          f'retrying in {RETRY_SECS:g} s')
        else:
            log.warning(f'[BOSS] saving the boss ledger {self.ledger.path} failed again ({n} in a row): '
                        f'{e}; kept dirty, retrying in {RETRY_SECS:g} s')

    # ----------------------------------------------------------------- GM ---
    def _live(self, key):
        """(mons, mob) of a boss tile whose map is loaded (populated), else (None, None)."""
        inst = self.server.world.maps.get(int(key[1]))
        mons = getattr(inst, 'monsters', None)
        if mons is None or not mons.populated:
            return None, None
        with mons.lock:
            for mob in mons.values():
                if getattr(mob, 'boss', None) == key:
                    return mons, mob
        return mons, None

    def _named_keys(self):
        """[(key, name)] of every boss tile of this channel: the map data's (bosses.catalog),
        then ledger rows the data lacks (a tile of another install)."""
        channel = self.channel_of(None)
        out = [(make_key(channel, b.map_code, b.tile), b.name) for b in catalog(self.tile_values())]
        seen = {key for key, _ in out}
        out += [(e.key, e.name) for e in self.ledger.rows() if e.key not in seen]
        return out

    def describe_lines(self):
        """The `!boss` listing, one S2C 0x15 line (<= 80 bytes) per boss tile."""
        keys = self._named_keys()
        rows = []
        down = 0
        for key, name in keys:
            entry = self.ledger.get(key)
            _mons, mob = self._live(key)
            if entry is not None and not entry.alive:
                down += 1
                wait = self.ledger.wait(key)
                state = (f'down {fmt_secs(wait)}' if wait > 0 else 'due') + \
                        (f' ({entry.last_killer})' if entry.last_killer else '')
            elif mob is not None:
                state = f'up uid {mob.uid:#x} {mob.hp}/{mob.max_hp}'
            else:
                state = 'up (map not loaded)'
            rows.append(f'ch{key[0]} {key[1]} ({key[2][0]},{key[2][1]}) {name}: {state}'[:80])
        scale = float(self.server.config.get('BOSS_RESPAWN_SCALE', 1.0))
        head = (f'{len(keys)} field boss tile(s), {down} down; respawn value x {scale:g} s, '
                f'drops {self.server.config.get("DROP_MODE", "rates")}')
        return [head[:80]] + rows

    def quest_lines(self, session):
        """The `!boss quests` listing (boss-b4): one S2C 0x15 line (<= 80 bytes) per boss quest,
        with where it stands in the GM's own log."""
        rows = boss_quests(self.tile_values())
        char = self.server._session_char(session)
        state = questmod.QuestState(char) if char is not None else None
        kill = sum(1 for b in rows if b.kind == 'kill')
        lines = [f'{len(rows)} boss quests: {kill} kill, {len(rows) - kill} trophy']
        for b in rows:
            what = (f'kill {b.boss} x{b.need}' if b.kind == 'kill'
                    else f'{"+".join(str(i) for i in b.items)} from {b.boss}')
            mine = ''
            if state is not None:
                slot = state.slot_of(b.quest)
                if slot is not None:
                    mine = f' [slot {slot + 1}: {state.progress[slot]}]'
                elif state.times(b.quest):
                    mine = f' [done {state.times(b.quest)}x]'
            lines.append(f'{b.quest}: {what}{mine}'[:80])
        return lines

    def push_quest(self, session, quest_id):
        """`!boss quest <id>` (boss-b4 live-test shortcut): put boss quest `quest_id` into the GM's
        own quest log - the ev-e5 push (events.Events.push_quest: S2C 0x26 {quest}, the client
        writes its first empty slot, + 0x59 {slot, 0}, mirrored into the quest store and the bag
        model) WITHOUT the level / job / PrevQuest / Repeat gates, so a Lv 30 Rogue can hold 182
        without doing 181 first. Only a slot and room for the Send items are required. Returns
        the slot index (-1 for a talk-only quest)."""
        server = self.server
        quest_id = int(quest_id)
        if quest_id not in {b.quest for b in boss_quests(self.tile_values())}:
            raise gm.DevCommandError(f'quest {quest_id} is no boss quest (!boss quests)')
        q = EC.quests().get(quest_id)
        char = server._session_char(session)
        if char is None or q is None:
            raise gm.DevCommandError('no character in this session')
        with server._combat_lock(session):
            state = questmod.QuestState(char)
            held = state.slot_of(quest_id)
            if held is not None:
                raise gm.DevCommandError(f'quest {quest_id} is already in your log (slot {held + 1})')
            if q.needs_slot and state.first_free_slot() is None:
                raise gm.DevCommandError(f'all {questmod.MAX_ACTIVE} quest slots are used (abandon one)')
            full = server._quest_bag_space(session, q.send)
            if full is not None:
                raise gm.DevCommandError(f'no bag space for the Send items: {full}')
            done = state.times(quest_id)
            with server.store.lock:
                slot = state.accept(quest_id, needs_slot=q.needs_slot)
            if slot is None:
                raise gm.DevCommandError('the quest log filled up meanwhile')
            # the Send mirror under the lock too: the bag changes only under it (the trade
            # commit relies on that; the P2 accept and the ev-e5 push do the same)
            server._quest_mirror_items(session, q.send, f'boss quest {quest_id} send')
            server.store.mark_dirty(f'boss quest {quest_id} pushed by !boss quest')
            server._push(session, '0x26', {'quest_id': quest_id}, 'BOSS')
            if slot >= 0:
                server._push(session, '0x59', {'slot': slot + 1, 'progress': 0}, 'BOSS')
        where = f'slot {slot + 1}' if slot >= 0 else 'completed (talk-only)'
        server._gm_reply(session, f'Quest {quest_id} is in your log ({where})'
                                  + (f'; done {done}x before.' if done else '.'))
        log.info(f'[BOSS] !boss quest {quest_id} by {session.get("char_name")!r}: 0x26 -> {where} '
                 f'(no level/job/PrevQuest gates; completed {done}x before)')
        return slot

    def dev_boss(self, session, args):
        """`!boss [list] | respawn [all|<map>|<name>] | quests | quest <id>`."""
        server = self.server
        words = str(args or '').split()
        sub = words[0].lower() if words else 'list'
        rest = words[1:]
        if sub in ('list', 'ls'):
            for line in self.describe_lines():
                server._gm_reply(session, line)
            return
        if sub == 'quests' or (sub == 'quest' and not rest):
            for line in self.quest_lines(session):
                server._gm_reply(session, line)
            return
        if sub == 'quest':
            self.push_quest(session, gm.parse_int(rest[0], 'quest id', 1, 0xFFFF))
            return
        if sub not in ('respawn', 'spawn', 'up'):
            raise gm.DevCommandError(f'unknown sub-command {sub!r}')
        what = rest[0].lower() if rest else str(session.get('current_map') or '')
        if not what:
            raise gm.DevCommandError('needs all, a map code or a boss name')
        keys = [key for key, name in self._named_keys()
                if what == 'all' or (what.isdigit() and int(what) == key[1])
                or (not what.isdigit() and what in name.lower())]
        if not keys:
            raise gm.DevCommandError(f'no field boss matches {what!r} (!boss list)')
        done = []
        for key in keys:
            entry = self.ledger.get(key)
            if entry is None or entry.alive:
                continue
            self.ledger.force_due(key)
            mons, mob = self._live(key)
            if mons is not None and mob is not None and not mob.alive:
                with mons.lock:
                    server._cancel_monster_timers(mob)
                    server._respawn_monster(mons, mob)          # the 0x1A to every client there
                done.append(f'{entry.name} {key[1]} (uid {mob.uid:#x})')
            else:
                done.append(f'{entry.name} {key[1]} (on the next arrival)')
        self.request_save()
        if not done:
            server._gm_reply(session, f'Every boss matching {what!r} is up already.')
            return
        for line in gm.wrap_list(done, f'Respawned {len(done)}: ', limit=80):
            server._gm_reply(session, line)
        log.info(f'[BOSS] !boss respawn {what} by {session.get("char_name")!r}: {done}')


# The '!' command (gm.register: no edit of GameServer.DEV_COMMANDS); its handler is the thin
# GameServer._dev_boss, which hands over to server.bosses.
if 'boss' not in gm.COMMANDS:
    gm.register('boss', gm.DevCommand(
        '_dev_boss', '!boss [list] | respawn [all|<map>|<name>] | quests | quest <id>',
        'the field boss ledger (state, time left, killer); respawn makes a down boss due now; '
        'quests lists the boss quests, quest <id> puts one in your log',
        owner='boss-b1..b4 (P13 stages 3-4)', aliases=('bosses',)))
