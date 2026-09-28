#!/usr/bin/env python3
"""
test_world_content.py - P3 stage 1 world content (world-portal-table-en, world-1a-defaults,
world-template-map)

The EN client's own files are read from the install next to the server (hs/*.hmi, *.hsi,
windslayer.hni, NPCLngKo.lng); every number asserted here is either a live capture
(world_movement_npc#03/#04/#06/#20/#21, spec correction C17, the P2 live note for the 201
flea-market gate) or a count the files give independently of the code under test.

    PortalTable       the generated table: live anchors, counts, arrival rule vs KR rows,
                      the file on disk is what the generator produces, the anchor assert
    PortalLoader      EN table first, in-memory build without the JSON, KR only as a last
                      resort
    NpcTemplates      hni positions = 0x1A template_index, EN names (Pupu ... Kamikaze Rat)
    MapSpawns         event-2 monster tiles -> floor spawn points; towns get none
    SpawnRecordBytes  S2C 0x1A bytes packed field by field from the spec for several
                      templates, the effect/server_controlled switches, batching
    PortalHandler     the fake client walks 101 -> 102 -> 101 -> 102 -> 103 -> 102 (a
                      genuine index-0 portal) and 201 -> 9701 on the wire

Nothing here opens the real accounts.json or binds a port.
"""
import hashlib
import json
import logging
import os
import shutil
import struct
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import config as cfgmod  # noqa: E402
import en_content as EC  # noqa: E402
import en_maps as M  # noqa: E402
import fakeclient as F  # noqa: E402
import packets as P  # noqa: E402

W = F.import_server()
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
HAS_CLIENT = os.path.exists(EC.hs_path('windslayer.hni')) and os.path.exists(M.map_path(101))
needs_client = unittest.skipUnless(HAS_CLIENT, 'EN client hs/ files not installed next to the server')
_LIVE_HASH = None


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def setUpModule():
    global _LIVE_HASH
    _LIVE_HASH = _sha(LIVE_ACCOUNTS) if os.path.exists(LIVE_ACCOUNTS) else None


def tearDownModule():
    EC.reload()
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_world_content.py (tests must only use temp copies)'


# ------------------------------------------------------------------------------------------
class HsiAndLines(unittest.TestCase):
    """FUN_00406170's list: layers, then tiles, in XML order; every sprite line of each tile."""

    def test_parse_hsi_is_positional(self):
        text = ('Number_of_Sprites: 2\n'
                '7 image: 1 line_count: 1\nline: 0 0 12 100 12\n'
                '9 image: 1 line_count: 2\nline: 3 10 0 10 100\nline: 1 70 12 90 22\n')
        # The DLL indexes its sprite vector by load position (0x10004230), not by the
        # number a block starts with.
        self.assertEqual(M.parse_hsi(text), [[(0, 0, 12, 100, 12)],
                                             [(3, 10, 0, 10, 100), (1, 70, 12, 90, 22)]])

    @needs_client
    def test_map_101_line_23_is_the_live_portal(self):
        m = M.load_map(101)
        line = m.lines[23]
        self.assertEqual((line.index, line.type, line.x1, line.y1, line.x2, line.y2, line.event, line.value),
                         (23, 0, 1355, 814, 1455, 814, 1, 102))
        self.assertEqual((line.tile.x, line.tile.y), (1355, 802))     # <Tile ... event="1" value_num="102"/>
        self.assertEqual([ln.index for ln in m.portal_lines()], [23])
        self.assertEqual([ln.index for ln in m.lines], list(range(len(m.lines))))

    @needs_client
    def test_floor_below(self):
        m = M.load_map(101)
        self.assertEqual(m.floor_below(1405, 714), 814)                # the arrival settles here
        self.assertIsNone(m.floor_below(1405, 5000))


@needs_client
class PortalTable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows, cls.report = M.build_portal_table()

    def test_live_anchors(self):
        # 101_23 world_movement_npc#03, 102_31 #04 (not KR 26), 102_17 #06 / C17 (not KR 14),
        # 201_192 the flea-market gate from the P2 live verification.
        self.assertEqual(M.check_live_portals(self.rows), [])
        for key, dest in (('101_23', 102), ('102_31', 101), ('102_17', 103), ('201_192', 9701)):
            self.assertEqual(self.rows[key]['dest'], dest, key)
        self.assertNotIn('102_26', self.rows)
        self.assertNotIn('102_14', self.rows)

    def test_counts(self):
        # 251 EN maps; 595 portal tiles (594 + map 1285's), one without a floor line
        # (1009 at (3800,947)) and one whose destination 1219 has no map file.
        self.assertEqual(self.report['maps'], 251)
        self.assertEqual(self.report['portal_tiles'], 595)
        self.assertEqual(self.report['tiles_without_line'], [[1009, 3800, 947, 1019]])
        self.assertEqual(self.report['unknown_destination'], [[1285, 36, 1219]])
        self.assertEqual(len(self.rows), 593)
        # 27 maps whose FIRST collision line is a portal floor: index 0 is genuine there.
        self.assertEqual(len(self.report['index_0']), 27)
        self.assertIn(103, self.report['index_0'])
        self.assertNotIn(101, self.report['index_0'])

    def test_arrival_is_next_to_the_reverse_portal(self):
        # 101_23 -> 102 lands on 102's portal back to 101 (tile (0,800), floor 0..100 @ 812):
        # its middle, 100 px up; KR had (48,713). 102_31 -> 101 (KR (1411,714)).
        self.assertEqual((self.rows['101_23']['x'], self.rows['101_23']['y']), (50.0, 712.0))
        self.assertEqual(self.rows['101_23']['arrival'], 'reverse portal 102_31')
        self.assertEqual((self.rows['102_31']['x'], self.rows['102_31']['y']), (1405.0, 714.0))
        self.assertEqual((self.rows['102_17']['x'], self.rows['102_17']['y']), (50.0, 512.0))
        # 9701 has one portal (-> 101): the towns arrive at it.
        self.assertTrue(self.rows['201_192']['arrival'].startswith('no reverse portal'))

    def test_arrival_rule_reproduces_the_kr_rows_that_match_the_en_layout(self):
        with open(EC.PORTALS_PATH, encoding='utf-8') as f:
            kr = json.load(f)
        agree = total = 0
        for key, row in kr.items():
            if not isinstance(row, list) or None in row[:3] or '_' not in key:
                continue
            src = key.split('_')[0]
            if not src.isdigit():
                continue
            dest = M.load_map(row[0])
            rev = [ln for ln in (dest.portal_lines() if dest else []) if ln.value == int(src)]
            if not rev:
                continue
            total += 1
            ln = min(rev, key=lambda l: abs((l.x1 + l.x2) / 2 - row[1]) + abs(l.y1 - 100 - row[2]))
            if abs(row[2] - (ln.y1 - M.ARRIVAL_ABOVE_LINE)) <= 3 and ln.x1 - 5 <= row[1] <= ln.x2 + 5:
                agree += 1
        # 293 of 354; the other 61 are KR-only layouts (world doc B14: 202_6 -> 201 ...).
        self.assertEqual((agree, total), (293, 354))

    def test_the_file_on_disk_is_the_generated_table(self):
        with open(EC.PORTALS_EN_PATH, encoding='utf-8') as f:
            stored = json.load(f)
        self.assertEqual(stored['portals'], json.loads(json.dumps(self.rows)),
                         'portals_en.json is stale: re-run `python en_maps.py`')
        self.assertEqual(stored['live_asserted']['101_23'], 102)

    def test_the_generator_refuses_a_table_that_contradicts_a_live_capture(self):
        tmp = tempfile.mkdtemp(prefix='ws_portals_')
        self.addCleanup(shutil.rmtree, tmp, True)
        out = os.path.join(tmp, 'portals_en.json')
        with mock.patch.dict(M.LIVE_PORTALS, {(101, 23): 103}):
            with self.assertRaises(M.MapError):
                M.write_portal_table(out)
        self.assertFalse(os.path.exists(out))
        M.write_portal_table(out)
        with open(out, encoding='utf-8') as f:
            self.assertEqual(len(json.load(f)['portals']), 593)


class PortalLoader(unittest.TestCase):
    def setUp(self):
        self.addCleanup(EC.reload)
        self.addCleanup(EC.configure, EC.client_dir())
        self.tmp = tempfile.mkdtemp(prefix='ws_portals_')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    @needs_client
    def test_the_generated_en_table_is_live(self):
        EC.reload()
        self.assertEqual(EC.portals_source(), EC.PORTALS_EN_PATH)
        self.assertEqual(len(EC.portals()), 593)
        self.assertEqual(EC.portal(101, 23), (102, 50.0, 712.0))
        self.assertIsNone(EC.portal(102, 26))                 # KR-only row

    @needs_client
    def test_without_the_json_the_table_is_built_from_the_client(self):
        with mock.patch.object(EC, 'PORTALS_EN_PATH', os.path.join(self.tmp, 'missing.json')):
            EC.reload()
            self.assertIn('in memory', EC.portals_source())
            self.assertEqual(len(EC.portals()), 593)
            self.assertEqual(EC.portal(102, 17)[0], 103)

    def test_without_json_and_client_the_kr_table_is_the_last_resort(self):
        with mock.patch.object(EC, 'PORTALS_EN_PATH', os.path.join(self.tmp, 'missing.json')):
            EC.configure(self.tmp)                            # no hs/ here
            self.assertIn('KR fallback', EC.portals_source())
            self.assertEqual(len(EC.portals()), 569)          # 589 rows, 20 malformed dropped


@needs_client
class NpcTemplates(unittest.TestCase):
    def test_catalog_positions_are_the_0x1A_template_index(self):
        npcs = EC.npcs()
        self.assertEqual(len(npcs), 180)
        self.assertEqual([npcs.template_index(i) for i in range(1, 181)], list(range(1, 181)))
        self.assertIsNone(npcs.template_index(181))
        self.assertIsNone(npcs.template_index(0))

    def test_en_names_match_the_live_nameplates(self):
        # world_movement_npc#21: templates 2, 3, 4, 6 on 101 and 102; LIVE_TEST_LOG bug 7.
        names = {i: EC.npc_name(i) for i in (1, 2, 3, 4, 6, 75)}
        self.assertEqual(names, {1: 'Pupu', 2: 'Blue Pupu', 3: 'Well-done Pupu', 4: 'Blood Pupu',
                                 6: 'Kamikaze Rat', 75: 'Murubisiri'})

    def test_pupu_stats_are_the_en_template(self):
        pupu = EC.npcs().get(1)
        self.assertEqual((pupu.type, pupu.lv, pupu.hp, pupu.body_atk, pupu.defense, pupu.exp),
                         (3, 1, 5, 3, 2, 5))
        self.assertEqual(pupu.hsi, './hs/mon001.hsi')
        self.assertEqual(pupu.drop_ids, [3, 4, 5, 18, 19, 20, 21, 47, 48, 2030])
        self.assertTrue(pupu.is_monster)
        self.assertFalse(EC.npcs().get(75).is_monster)        # town NPC: the client spawns it

    def test_monster_types(self):
        types = {d.type for d in EC.npcs().monsters()}
        self.assertEqual(types, {3, 4, 5, 6, 7})


@needs_client
class MapSpawns(unittest.TestCase):
    def test_map_102_is_eight_pupu_on_their_floors(self):
        spawns = EC.map_spawns(102)
        self.assertEqual([s.npc for s in spawns], [1] * 8)
        # The eight NpcId=1 tiles are the old MAP_SPAWNS points; each monster stands on the
        # middle of its tile's floor line (server_controlled mobs run no physics, C29).
        self.assertEqual([s.tile for s in spawns],
                         [(1239, 411), (1194, 702), (1494, 702), (700, 900), (1300, 900),
                          (2100, 800), (100, 485), (1100, 530)])
        self.assertEqual([(s.x, s.y) for s in spawns],
                         [(1339.0, 423.0), (1244.0, 714.0), (1594.0, 714.0), (800.0, 912.0),
                          (1400.0, 912.0), (2150.0, 812.0), (200.0, 497.0), (1200.0, 542.0)])

    def test_towns_get_no_server_spawn(self):
        for town in (101, 201, 9701):
            self.assertEqual(EC.map_spawns(town), [], town)

    def test_every_spawn_binds_a_template_and_stands_on_a_floor(self):
        total = 0
        for code in M.map_codes():
            data = M.load_map(code)
            for s in EC.map_spawns(code):
                total += 1
                self.assertIsNotNone(EC.npcs().template_index(s.npc))
                self.assertTrue(EC.npcs().get(s.npc).is_monster)
                self.assertEqual(data.floor_below(s.x, s.y), s.y, (code, s))
        self.assertEqual(total, 2233)

    def test_mixed_map_names(self):
        names = sorted(EC.npc_name(s.npc) for s in EC.map_spawns(105))
        self.assertEqual(names, ['Blue Pupu'] + ['Pupu'] * 9)


# ------------------------------------------------------------------------------------------
def pack_record(template, uid, x, y, hp, home=None, effect=0, controlled=True):
    """One S2C 0x1A record packed field by field from the spec layout (not the grammar)."""
    hx, hy = home or (x, y)
    out = struct.pack('<II', template, uid)
    out += struct.pack('<B', 1 if effect else 0)
    if effect:
        out += struct.pack('<Hi', effect, 0)
    out += struct.pack('<BBII', 0, 0, 0, 0)                   # wander motion/cursor/mode/timer
    out += struct.pack('<III', int(hx), int(hy), 0)           # home, respawn_tick
    out += struct.pack('<IBBiIB', 0, 0, 8, 8, 0, 2)           # 954, 8d9, 8cf, 904 idle, e00, 8bd
    out += struct.pack('<ddi', float(x), float(y), 0)         # pos, e50
    out += struct.pack('<BBBB', 8, 0, 0, 0)                   # facing 8, motion 0,0,0
    out += bytes(5)                                           # 8da 8df 8dc 8dd 8de
    out += struct.pack('<HI', hp, 0)                          # cur_hp, d94
    out += struct.pack('<B', 1 if controlled else 0)
    if controlled:
        out += struct.pack('<I', 0)                           # cmd_hold_ms
    return out


class SpawnRecordBytes(unittest.TestCase):
    def test_shipped_default_is_the_client_wander_ai(self):
        # retail roaming mobs: server_controlled 0, no cmd_hold_ms -> an 83 B record
        self.assertFalse(cfgmod.defaults()['MOB_SERVER_CONTROLLED'])
        mob = W.Monster(uid=0xF0000, npccode=1, name='Pupu', level=1, hp=5, max_hp=5, body_atk=3,
                        defense=2, exp=5, x=1339.0, y=423.0, spawn_x=1339.0, spawn_y=423.0, template=1)
        rec = W.GameServer._monster_spawn_record(mock.Mock(config=cfgmod.defaults()), mob)
        self.assertEqual(rec['server_controlled'], 0)
        self.assertNotIn('cmd_hold_ms', rec)
        self.assertEqual(len(P.build('0x1A', {'count': 1, 'repeat[count]': [rec]})), 1 + 82)

    def builder(self, **cfg):
        config = cfgmod.defaults()
        config['MOB_SERVER_CONTROLLED'] = True       # the encoding these vectors pin
        config.update(cfg)
        return mock.Mock(config=config)

    def mob(self, npc, uid, x, y, hp, template=None):
        return W.Monster(uid=uid, npccode=npc, name='m', level=1, hp=hp, max_hp=hp, body_atk=1,
                         defense=1, exp=1, x=x, y=y, spawn_x=x, spawn_y=y,
                         template=npc if template is None else template)

    def build(self, mobs, **cfg):
        stub = self.builder(**cfg)
        recs = [W.GameServer._monster_spawn_record(stub, m) for m in mobs]
        return P.build('0x1A', {'count': len(recs), 'repeat[count]': recs})

    def test_several_templates_byte_for_byte(self):
        # Pupu, Blue Pupu, Kamikaze Rat, Rynx (type 4) with their EN HP.
        cases = [(1, 0xF0000, 1339.0, 423.0, 5), (2, 0xF0001, 1244.0, 714.0, 10),
                 (6, 0xF0002, 800.0, 912.0, 30), (12, 0xF0003, 2150.5, 812.0, 174)]
        body = self.build([self.mob(*c) for c in cases])
        want = struct.pack('<B', 4) + b''.join(pack_record(n, u, x, y, hp) for n, u, x, y, hp in cases)
        self.assertEqual(body, want)
        self.assertEqual(len(body), 1 + 4 * 86)
        rec = P.parse(0x1A, body, direction='S2C')['repeat[count]'][3]
        self.assertEqual((rec['template_index'], rec['action_state'], rec['facing'], rec['unk_8cf'],
                          rec['unk_8bd'], rec['effect_count'], rec['server_controlled'], rec['cur_hp']),
                         (12, 8, 8, 8, 2, 0, 1, 174))

    def test_the_template_index_is_the_hni_position_not_a_guess(self):
        body = self.build([self.mob(6, 0xF0000, 10.0, 20.0, 30, template=6)])
        self.assertEqual(struct.unpack_from('<I', body, 1)[0], 6)

    def test_effect_and_server_controlled_switches(self):
        mob = self.mob(1, 0xF0000, 1339.0, 423.0, 5)
        body = self.build([mob], MOB_SPAWN_EFFECT_ID=0x0B3B)
        self.assertEqual(body, b'\x01' + pack_record(1, 0xF0000, 1339.0, 423.0, 5, effect=0x0B3B))
        self.assertEqual(len(body), 93)
        body = self.build([mob], MOB_SERVER_CONTROLLED=False)
        self.assertEqual(body, b'\x01' + pack_record(1, 0xF0000, 1339.0, 423.0, 5, controlled=False))
        self.assertEqual(len(body), 83)                       # the T-1A-1 83 B form

    def test_batching_fills_but_never_overflows_a_packet(self):
        mob = self.mob(1, 0xF0000, 1.0, 2.0, 5)
        stub = self.builder()
        rec = W.GameServer._monster_spawn_record(stub, mob)
        batches = W.GameServer.monster_spawn_batches([rec] * 37)   # map 206, the densest
        self.assertEqual([len(b) for b in batches], [23, 14])
        for b in batches:
            payload = P.build('0x1A', {'count': len(b), 'repeat[count]': b})
            self.assertLessEqual(len(payload), W.GameServer.MAX_S2C_PAYLOAD)
        free = W.GameServer._monster_spawn_record(self.builder(MOB_SERVER_CONTROLLED=False), mob)
        self.assertEqual([len(b) for b in W.GameServer.monster_spawn_batches([free] * 25)], [24, 1])
        self.assertEqual(W.GameServer.monster_spawn_batches([]), [])
        self.assertEqual(F.mob_packets(8), [0x1A])
        self.assertEqual(F.mob_packets(0), [])


# ------------------------------------------------------------------------------------------
@needs_client
class PortalHandler(unittest.TestCase):
    """Exit criterion 1 offline: every portal on the route lands on the map the .hmi names,
    and exit criterion 2's wire half: the 102 mobs are template 1 (Pupu) with idle defaults."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_world_')
        self.server = F.make_server(self.tmp)
        self.clients = []
        self.addCleanup(EC.reload)

    def tearDown(self):
        for c in self.clients:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def enter(self):
        c = F.FakeClient(self.server)
        self.clients.append(c)
        c.send_c2s('0x44D8BF/0x01', {'account_id': 'test', 'password': 'test'})
        c.expect(0x02)
        c.send_c2s('0x42F904/0x2B', {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907, 'char_name': 'TestHero'})
        c.expect(0x03, 0x07, 0x15, 0x28, 0x44)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world')))
        return c

    def portal(self, c, index, dest):
        c.send_c2s('0x42F76B/0x7E', {'portal_line_index': index})
        mobs = len(EC.map_spawns(dest))
        pkts = c.expect(0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(mobs))
        self.assertEqual(F.FakeClient.decode(pkts[0])['map_code'], dest)
        self.assertEqual(c.session['current_map'], dest)
        row = F.FakeClient.decode(pkts[2])['repeat[player_count]'][0]
        return pkts, (row['pos_x'], row['pos_y'])

    def test_walk_101_102_101_102_103_102(self):
        c = self.enter()
        with self.assertLogs('WS', logging.INFO) as cm:
            pkts, pos = self.portal(c, 23, 102)
            self.assertEqual(pos, (50.0, 712.0))
            blocks = F.FakeClient.decode(pkts[5])['repeat[count]']
            self.assertEqual([b['template_index'] for b in blocks], [1] * 8)
            self.assertEqual({(b['action_state'], b['facing'], b['effect_count'], b['server_controlled'])
                              for b in blocks}, {(8, 8, 0, 1)})
            self.assertEqual({m.name for m in c.session['monsters'].values()}, {'Pupu'})
            self.assertEqual(self.portal(c, 31, 101)[1], (1405.0, 714.0))
            self.portal(c, 23, 102)
            self.assertEqual(self.portal(c, 17, 103)[1], (50.0, 512.0))
            # 103_0 is a genuine index-0 portal (its first collision line is the floor of
            # the portal back to 102), so a 0 here is honoured.
            self.assertEqual(self.portal(c, 0, 102)[1], (2356.0, 576.0))
        self.assertFalse([l for l in cm.output if 'refused' in l or 'no portal table' in l])
        self.assertTrue(any('Novice Hunting Park' in l for l in cm.output))

    def test_town_201_flea_market_gate(self):
        c = self.enter()
        s = c.session
        self.assertTrue(self.server._map_transfer(s['sock'], s, 201, 1000.0, 1100.0, reason='test'))
        c.expect(0x08, 0x03, 0x07, 0x28, 0x44)                 # a town: no monsters
        pos = self.portal(c, 192, 9701)[1]
        self.assertEqual(pos, (1500.0, 2168.0))                 # 9701's only portal line
        # world-flea-return: the exit tile names 101, but the player goes back to the town
        # market portal he came through (201_192: line x 1600..1700 at y 1912, 100 px above)
        line = EC.portal_line(201, 192)
        self.assertEqual(self.portal(c, 76, 201)[1], ((line[0] + line[2]) / 2, line[1] - 100.0))


if __name__ == '__main__':
    unittest.main()
