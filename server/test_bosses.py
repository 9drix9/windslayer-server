#!/usr/bin/env python3
"""
test_bosses.py - P13 stage 3 (boss-ledger-drops: boss-b1, boss-b2), offline, both client builds
===============================================================================================
Design re_tools/docs/systems_2009/events_bosses.md B1, B2, B5, B6, C, E-B1/E-B2; P13 exit
criterion 5 (Foothill 218: one Rynx near (1609,1835) with the same uid on both clients; A kills
it: both see the death, item 124 drops and only A may pick it up, it stays absent for 300 s
across map leave / return and a server restart, then reappears on both).

- the data (bosses.catalog): the field bosses are exactly the value_num 300 monster tiles -
  11 in the 2009 maps (Rynx on 218), 15 in the 2008 maps (Rynx on 208); the Crow at 901 is not;
- the drop roll (bosses.roll_drops) by DROP_MODE: 'rates' (per entry, rate / DROP_RATE_UNIT:
  a trophy 99.99 %, Wasablanca's pair independently), 'single' (one draw, never two items),
  'legacy' (the P0 60 % uniform pick), and the unit;
- the ledger (bosses.BossLedger): keyed (channel, map, tile), its timers on the UTC clock, the
  JSON round trip, a malformed file moved aside, memory-only, a failed write kept dirty and
  retried from the tick thread;
- the server through two fake clients per build: the same boss uid on both; a kill marks the
  ledger (300 s), the trophy lies on the ground owned by the killer (the other client's pickup
  is refused), no 15 s respawn, the boss stays away across a leave / return, a map discard and
  a server restart, and comes back at 300 s on both clients; GROUND_LOOT false puts every
  rolled item in the killer's bag with one 0x18 each; `!boss` lists and respawns.

No port is bound, no client is started and the live accounts.json is never opened (temp
copies; the module checks its hash at the end).
"""
import hashlib
import json
import logging
import os
import random
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import bosses as B  # noqa: E402
import config as cfgmod  # noqa: E402
import en_content as EC  # noqa: E402
import fakeclient as F  # noqa: E402
import inventory as INV  # noqa: E402
import packets as P  # noqa: E402

W = F.import_server()
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = '2008', '2009'
DIR8 = os.path.join(HERE, cfgmod.DEFAULTS['CLIENT_DIR'])
DIR9 = os.path.join(HERE, cfgmod.DEFAULTS['CLIENT_DIR_2009'])
HAVE = {B8: os.path.exists(os.path.join(DIR8, 'hs', 'windslayer.hni')),
        B9: os.path.exists(os.path.join(DIR9, 'hs', 'windslayer.hni'))}
needs_2008 = unittest.skipUnless(HAVE[B8], 'needs the EN 2008 client data (hs/)')
needs_2009 = unittest.skipUnless(HAVE[B9], 'needs the EN 2009 client data (CLIENT_DIR_2009)')

RYNX, MONKEY_KING, WASABLANCA, KAMIKAZE_RAT = 12, 81, 95, 6
RYNX_FUR, RYNX_CARD = 124, 2036
HORSERADISH, SUNFLOWER, WASA_CARD = 1559, 1582, 2053
# The boss map of each build: Rynx's value-300 tile, a portal out and the one back.
BOSS = {
    B9: {'map': 218, 'tile': (1609, 1835), 'spawn': (1709.0, 1847.0), 'out': (133, 215), 'back': (46, 218)},
    B8: {'map': 208, 'tile': (1100, 1400), 'spawn': (1150.0, 1412.0), 'out': (70, 210), 'back': (93, 208)},
}
# evb B2 table (2009 data): the eleven value-300 tiles (map, tile, npc)
CATALOG_2009 = {
    (218, (1609, 1835), 12), (409, (2400, 1489), 81), (511, (3700, 1025), 95), (511, (4300, 1025), 95),
    (619, (3100, 1674), 115), (819, (2000, 1473), 116), (920, (2600, 1108), 137),
    (1017, (2200, 900), 157), (1018, (800, 900), 158), (1021, (600, 1219), 159), (1111, (1000, 337), 170),
}
KEYS = {
    B8: {'move': '0x42CE94/0x0D', 'portal': '0x42F76B/0x7E', 'pickup': '0x43D80F/0x1F',
         'chat': '0x445CA7/0x03'},
    B9: {'move': '0x42E704/0x0D', 'portal': '0x431284/0x7E', 'pickup': '0x43DA4E/0x1F',
         'chat': '0x44790E/0x03'},
}
T0 = 1_790_000_000.0                  # the fake UTC clock of the ledger
_LIVE_HASH = None


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def setUpModule():
    global _LIVE_HASH
    _LIVE_HASH = _sha(LIVE_ACCOUNTS) if os.path.exists(LIVE_ACCOUNTS) else None
    P.STRICT_FIELDS.add(B9)          # every 2009 S2C must use the 2009 field names


def tearDownModule():
    P.STRICT_FIELDS.discard(B9)
    EC.configure(cfgmod.DEFAULTS['CLIENT_DIR'], B8)
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_bosses.py (tests must only use temp copies)'


def use_build(build):
    EC.configure(cfgmod.DEFAULTS['CLIENT_DIR_2009'] if build == B9 else cfgmod.DEFAULTS['CLIENT_DIR'], build)


class Wall:
    """The fake UTC clock of the ledger (Bosses.clock)."""

    def __init__(self, t=T0):
        self.t = float(t)

    def __call__(self):
        return self.t


def accounts(build):
    x, y = BOSS[build]['spawn']
    m = BOSS[build]['map']
    return {
        'test': {'password': 'test', 'characters': [
            {'name': 'TestHero', 'level': 10, 'class': 0, 'map': m, 'x': x - 60, 'y': y,
             'hp': 300, 'mp': 50, 'gm': 1}]},
        'admin': {'password': 'admin', 'characters': [
            {'name': 'Watcher', 'level': 10, 'class': 0, 'map': m, 'x': x + 60, 'y': y,
             'hp': 300, 'mp': 50}]},
    }


def fake_mob(table=(), items=()):
    return types.SimpleNamespace(drop_table=list(table), drop_items=list(items))


# ====================================================================== data ===
class Catalog(unittest.TestCase):
    """evb B1/B2: the field bosses are the map tiles with value_num 300."""

    def tearDown(self):
        use_build(B8)

    @needs_2009
    def test_2009_the_eleven_value_300_tiles(self):
        use_build(B9)
        rows = B.catalog()
        self.assertEqual(len(rows), 11)
        self.assertEqual({(b.map_code, b.tile, b.npc) for b in rows}, CATALOG_2009)
        rynx = [b for b in rows if b.map_code == 218]
        self.assertEqual([(b.name, b.value, (b.x, b.y)) for b in rynx], [('Rynx', 300, BOSS[B9]['spawn'])])
        self.assertNotIn(502, {b.map_code for b in rows})                   # the Crow tile at 901
        self.assertEqual(len(B.catalog((300, 901))), 12)
        # every boss drops its trophy at 99990 (evb B2 / B6)
        for npc in {b.npc for b in rows}:
            rates = dict(EC.npcs().get(npc).drops)
            self.assertIn(99990 if npc != WASABLANCA else 50000, rates.values())
            self.assertEqual(sum(r for _, r in EC.npcs().get(npc).drops), 100000)

    @needs_2008
    def test_2008_the_value_300_tiles(self):
        use_build(B8)
        rows = B.catalog()
        self.assertEqual(len(rows), 15)
        self.assertIn((208, (1100, 1400), RYNX), {(b.map_code, b.tile, b.npc) for b in rows})
        self.assertIn((709, (1650, 2100), 94), {(b.map_code, b.tile, b.npc) for b in rows})   # Atomic Ball
        self.assertEqual({b.value for b in rows}, {300})


class DropRoll(unittest.TestCase):
    """bosses.roll_drops by DROP_MODE (evb B6, E-B2)."""
    N = 20000
    RYNX_TABLE = [(RYNX_CARD, 10), (RYNX_FUR, 99990)]
    WASA_TABLE = [(HORSERADISH, 50000), (SUNFLOWER, 49990), (WASA_CARD, 10)]

    def runs(self, table, mode, unit=100000, seed=7):
        rng = random.Random(seed)
        mob = fake_mob(table)
        return [B.roll_drops(mob, mode, unit, rng) for _ in range(self.N)]

    def test_rates_rolls_every_entry_on_its_own(self):
        out = self.runs(self.RYNX_TABLE, 'rates')
        fur = sum(RYNX_FUR in d for d in out)
        card = sum(RYNX_CARD in d for d in out)
        self.assertGreaterEqual(fur, self.N - 20)                            # 99.99 %
        self.assertLessEqual(card, 20)                                       # 0.01 %
        out = self.runs(self.WASA_TABLE, 'rates')
        a = sum(HORSERADISH in d for d in out)
        b = sum(SUNFLOWER in d for d in out)
        both = sum(HORSERADISH in d and SUNFLOWER in d for d in out)
        for n in (a, b):
            self.assertTrue(0.47 * self.N < n < 0.53 * self.N, n)
        self.assertTrue(0.22 * self.N < both < 0.28 * self.N, both)         # independent: ~25 %

    def test_single_is_one_draw_over_the_column(self):
        out = self.runs(self.WASA_TABLE, 'single')
        self.assertTrue(all(len(d) <= 1 for d in out))
        a = sum(HORSERADISH in d for d in out)
        self.assertTrue(0.47 * self.N < a < 0.53 * self.N, a)
        out = self.runs(self.RYNX_TABLE, 'single')
        self.assertGreaterEqual(sum(RYNX_FUR in d for d in out), self.N - 20)
        # the draw walks the column in order: 0.0 lands on the first entry, 0.99999 on the last
        mob = fake_mob(self.RYNX_TABLE)
        self.assertEqual(B.roll_drops(mob, 'single', 100000, mock.Mock(random=lambda: 0.0)), [RYNX_CARD])
        self.assertEqual(B.roll_drops(mob, 'single', 100000, mock.Mock(random=lambda: 0.99999)), [RYNX_FUR])
        self.assertEqual(B.roll_drops(fake_mob([(3, 30000)]), 'single', 100000,
                                      mock.Mock(random=lambda: 0.5)), [])

    def test_the_unit_scales_every_rate(self):
        out = self.runs(self.RYNX_TABLE, 'rates', unit=1_000_000)
        fur = sum(RYNX_FUR in d for d in out)
        self.assertTrue(0.08 * self.N < fur < 0.12 * self.N, fur)           # 99990 / 1e6 = 10 %

    def test_legacy_is_the_old_sixty_percent_uniform_pick(self):
        mob = fake_mob(self.RYNX_TABLE, [RYNX_CARD, RYNX_FUR])
        rng = mock.Mock(random=lambda: 0.59, choice=lambda seq: seq[-1])
        self.assertEqual(B.roll_drops(mob, 'legacy', 100000, rng), [RYNX_FUR])
        rng = mock.Mock(random=lambda: 0.6, choice=lambda seq: seq[-1])
        self.assertEqual(B.roll_drops(mob, 'legacy', 100000, rng), [])
        self.assertEqual(B.roll_drops(fake_mob(self.RYNX_TABLE, []), 'legacy', 100000, rng), [])
        # rates ignore drop_items: an empty table drops nothing whatever the rng says
        self.assertEqual(B.roll_drops(fake_mob([], [RYNX_FUR]), 'rates', 100000, mock.Mock(random=lambda: 0.0)), [])

    def test_config(self):
        d = cfgmod.defaults()
        self.assertEqual((d.DROP_MODE, d.DROP_RATE_UNIT, d.BOSS_TILE_VALUES, d.BOSS_RESPAWN_SCALE,
                          d.BOSS_LEDGER_FILE), ('rates', 100000, [300], 1.0, 'boss_ledger.json'))
        self.assertEqual(cfgmod.from_dict({'DROP_MODE': 'single'}).DROP_MODE, 'single')
        for bad in ({'DROP_MODE': 'all'}, {'DROP_RATE_UNIT': 0}, {'BOSS_RESPAWN_SCALE': 0},
                    {'BOSS_TILE_VALUES': ['300']}, {'BOSS_TILE_VALUES': [0]}):
            with self.assertRaises(cfgmod.ConfigError, msg=bad):
                cfgmod.from_dict(bad)
        with open(os.path.join(HERE, 'config.json'), encoding='utf-8') as f:
            shipped = json.load(f)
        self.assertEqual((shipped['DROP_MODE'], shipped['BOSS_TILE_VALUES'], shipped['BOSS_RESPAWN_SCALE']),
                         ('rates', [300], 1.0))


class Ledger(unittest.TestCase):
    """bosses.BossLedger (boss-b1, evb C)."""
    KEY = B.make_key(1, 218, (1609, 1835))

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_bossledger_')
        self.path = os.path.join(self.tmp, 'boss_ledger.json')
        self.wall = Wall()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_timers_on_the_utc_clock_and_the_round_trip(self):
        led = B.BossLedger(self.path, self.wall)
        entry, created = led.ensure(self.KEY, RYNX, 'Rynx', 300)
        self.assertTrue(created and entry.alive)
        self.assertEqual(led.ensure(self.KEY, RYNX, 'Rynx', 300), (entry, False))
        self.assertEqual(led.wait(self.KEY), 0.0)
        led.mark_dead(self.KEY, 300, 'TestHero')
        self.wall.t += 120
        self.assertAlmostEqual(led.wait(self.KEY), 180.0)
        self.assertTrue(led.flush())
        self.assertFalse(led.flush())                                        # nothing changed since
        with open(self.path, encoding='utf-8') as f:
            data = json.load(f)
        self.assertEqual(data['version'], 1)
        self.assertEqual(data['bosses'], [{'channel': 1, 'map': 218, 'tile': [1609, 1835], 'npc': RYNX,
                                           'name': 'Rynx', 'value': 300, 'alive': False,
                                           'next_spawn_at': T0 + 300, 'last_kill_at': T0,
                                           'last_killer': 'TestHero', 'kills': 1}])
        again = B.BossLedger(self.path, self.wall)
        self.assertEqual(again.load(), 1)
        self.assertEqual(again.get(self.KEY), led.get(self.KEY))
        self.assertAlmostEqual(again.wait(self.KEY), 180.0)
        again.force_due(self.KEY)
        self.assertEqual(again.wait(self.KEY), 0.0)
        self.assertTrue(again.mark_alive(self.KEY))
        self.assertFalse(again.mark_alive(self.KEY))
        # a tile whose template changed starts over
        entry, created = again.ensure(self.KEY, MONKEY_KING, 'Monkey King', 300)
        self.assertTrue(created)
        self.assertEqual((entry.npc, entry.kills), (MONKEY_KING, 0))

    def test_a_malformed_file_is_moved_aside(self):
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write('{"bosses": [')
        led = B.BossLedger(self.path, self.wall)
        with self.assertLogs('WS', logging.ERROR):
            self.assertEqual(led.load(), 0)
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(len([n for n in os.listdir(self.tmp) if n.startswith('boss_ledger.json.bad-')]), 1)
        # a bad row is skipped, the rest kept
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump({'version': 1, 'bosses': [{'channel': 1, 'map': 218, 'tile': [1, 2], 'npc': 12},
                                                {'map': 1}]}, f)
        with self.assertLogs('WS', logging.WARNING):
            self.assertEqual(B.BossLedger(self.path, self.wall).load(), 1)

    def test_an_unreadable_file_stops_the_start_and_is_never_replaced(self):
        """P13 review: a ledger that could not be READ started empty, and the first save then
        overwrote every boss timer and kill count in it. Now LedgerError (a StoreError: the
        server start stops, nothing bound) and the file is untouched - also for a malformed
        file that cannot be moved aside."""
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump({'version': 1, 'bosses': [{'channel': 1, 'map': 218, 'tile': [1609, 1835], 'npc': RYNX,
                                                 'alive': False, 'next_spawn_at': T0 + 300, 'kills': 7}]}, f)
        before = _sha(self.path)
        real_open = open

        def denied(path, *args, **kw):
            if os.path.abspath(str(path)) == os.path.abspath(self.path):
                raise PermissionError(13, 'Permission denied', str(path))
            return real_open(path, *args, **kw)
        led = B.BossLedger(self.path, self.wall)
        with mock.patch('builtins.open', side_effect=denied), self.assertLogs('WS', logging.ERROR):
            with self.assertRaises(B.LedgerError) as caught:
                led.load()
        self.assertIsInstance(caught.exception, B.storemod.StoreError)
        self.assertIn('cannot be read', str(caught.exception))
        self.assertEqual((led.entries, led.dirty), ({}, False))
        self.assertEqual(_sha(self.path), before)
        # readable again: every row is there
        self.assertEqual(B.BossLedger(self.path, self.wall).load(), 1)
        # malformed, and the move aside fails: refused, the file stays as it is
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write('{"bosses": [')
        before = _sha(self.path)
        with mock.patch.object(B.os, 'replace', side_effect=PermissionError(5, 'held')), \
                self.assertLogs('WS', logging.ERROR):
            with self.assertRaises(B.LedgerError):
                B.BossLedger(self.path, self.wall).load()
        self.assertEqual(_sha(self.path), before)
        self.assertEqual([n for n in os.listdir(self.tmp) if '.bad-' in n], [])

    def test_memory_only(self):
        led = B.BossLedger('', self.wall)
        led.ensure(self.KEY, RYNX, 'Rynx', 300)
        self.assertFalse(led.flush())
        self.assertEqual(os.listdir(self.tmp), [])

    def test_a_failed_write_stays_dirty(self):
        led = B.BossLedger(self.path, self.wall)
        led.ensure(self.KEY, RYNX, 'Rynx', 300)
        with mock.patch.object(B.storemod, 'atomic_write', side_effect=PermissionError(5, 'held')):
            with self.assertRaises(OSError):
                led.flush()
        self.assertTrue(led.dirty)
        self.assertTrue(led.flush())
        self.assertFalse(led.dirty)
        self.assertEqual(led.saves, 1)


# ============================================================== server flows ===
class Rig(unittest.TestCase):
    build = B9
    overrides = {}

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        use_build(self.build)
        self.tmp = tempfile.mkdtemp(prefix=f'ws_bosses{self.build}_')
        self.boss = BOSS[self.build]
        self.keys = KEYS[self.build]
        self.clients = []
        self.wall = Wall()
        self.server = self.make()

    def make(self, **over):
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False,
                                'GROUND_LOOT': True, 'DAMAGE_GRADE_ROLL': False, **self.overrides, **over})
        server = F.make_server(self.tmp, accounts=accounts(self.build), config=cfg, ground_loot=None,
                               grade_roll=None, drop_mode=None)
        server.bosses.clock = self.wall
        return server

    def tearDown(self):
        for c in self.clients:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        use_build(B8)

    # ------------------------------------------------------------ helpers ---
    def n_mobs(self, map_code=None):
        return len(EC.map_spawns(map_code or self.boss['map']))

    @staticmethod
    def uids(c, pkts):
        return [r['uid'] for p in pkts if p.opcode == 0x1A for r in c.s2c(p)['repeat[count]']]

    def enter(self, user='test', password='test', name='TestHero'):
        c = F.FakeClient(self.server)
        self.clients.append(c)
        self.assertEqual(c.login(user, password)['result'], 1)
        pkts = c.enter_world(name, port=F.P2P_PORT_BASE + len(self.clients) - 1)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world') and s.get('current_map') == self.boss['map']))
        for o in self.clients:
            if o is not c:
                o.recv_until_quiet(0.1)
        return c, self.uids(c, pkts)

    def two(self):
        a, ua = self.enter()
        b, ub = self.enter('admin', 'admin', 'Watcher')
        a.recv_until_quiet(0.1)
        return a, b, ua, ub

    def portal(self, c, index, dest):
        c.send_c2s(self.keys['portal'], {'portal_line_index': index})
        self.assertTrue(c.wait_session(lambda s: s.get('current_map') == dest and s.get('in_world')))
        pkts = c.recv_until_quiet(0.25)
        for o in self.clients:
            if o is not c:
                o.recv_until_quiet(0.1)
        return self.uids(c, pkts)

    def leave_and_return(self, c):
        index, dest = self.boss['out']
        self.portal(c, index, dest)
        index, dest = self.boss['back']
        return self.portal(c, index, dest)

    def mons(self):
        return self.server.world.map(self.boss['map']).monsters

    def rynx(self):
        found = [m for m in self.mons().values() if m.boss is not None]
        self.assertEqual(len(found), 1)
        return found[0]

    def key(self):
        return B.make_key(1, self.boss['map'], self.boss['tile'])

    def report_hit(self, c, mob):
        px, py = c.session['pos']
        c.send_c2s(self.keys['move'], {
            'realtime_delta_ms': 0, 'map_code': self.boss['map'], 'logic_elapsed_ms': 30,
            'state_lo': W.HIT_REPORT_EVENT << 16, 'state_hi': 0, 'target_uid': mob.uid,
            'pos_x': float(px), 'pos_y': float(py), 'target_dx': float(mob.x - px),
            'target_dy': float(mob.y - py), 'flag_8db': 0, 'flag_8e7': 0, 'timer_dac': 0,
            'target_action_event': 7})

    def kill(self, c, mob, roll=0.5):
        """A reported one-hit kill; `roll` is what every random.random() of the drop returns
        (0.5: the trophy at 99990 drops, the card at 10 does not)."""
        with self.server._combat(c.session):
            mob.hp = 1
        with mock.patch.object(W.random, 'random', return_value=roll):
            self.report_hit(c, mob)
            pkts = c.recv_until_quiet(0.3)
        self.assertFalse(mob.alive)
        return pkts

    def gm(self, c, line):
        c.send_c2s(self.keys['chat'], {'msg_len': len(line), 'message': line})
        first = c.recv(10.0)                        # the first `!boss` scans every map (~0.5 s)
        pkts = ([first] if first is not None else []) + c.recv_until_quiet(0.3)
        texts = []
        for p in pkts:
            if p.opcode == 0x15:
                t = c.s2c(p)['text']
                texts.append(t.decode('latin-1') if isinstance(t, bytes) else t)
        return texts, pkts

    def saved(self):
        with open(os.path.join(self.tmp, 'boss_ledger.json'), encoding='utf-8') as f:
            rows = json.load(f)['bosses']
        return {B.make_key(r['channel'], r['map'], r['tile']): r for r in rows}


class BossFlows:
    """Run for each build by the concrete classes below."""

    def test_one_boss_tile_with_the_same_uid_on_both_clients(self):
        a, b, ua, ub = self.two()
        self.assertEqual(ua, ub)
        self.assertEqual(len(ua), self.n_mobs())
        mob = self.rynx()
        self.assertIn(mob.uid, ua)
        self.assertEqual((mob.npccode, mob.boss, (mob.spawn_x, mob.spawn_y)), (RYNX, self.key(), self.boss['spawn']))
        self.assertEqual([m.boss for m in self.mons().values()].count(None), self.n_mobs() - 1)
        entry = self.server.bosses.ledger.get(self.key())
        self.assertEqual((entry.alive, entry.value, entry.name), (True, 300, 'Rynx'))
        self.assertEqual(mob.drop_table, [(RYNX_CARD, 10), (RYNX_FUR, 99990)])

    def test_kill_trophy_ownership_and_the_300_s_respawn_across_leave_and_return(self):
        a, b, ua, _ = self.two()
        uid_a, uid_b = P.session_uid(a.session), P.session_uid(b.session)
        mob = self.rynx()
        pkts = self.kill(a, mob)
        now = time.monotonic()
        # A: the death, the exp and the trophy on the ground (the killer's), gold only in 0x18
        ops = [p.opcode for p in pkts]
        self.assertEqual([op for op in ops if op in (0x29, 0x21, 0x12, 0x18)], [0x29, 0x21, 0x12, 0x18])
        entry = [r for p in pkts if p.opcode == 0x12 for r in a.s2c(p)['repeat[entry_count]']]
        self.assertEqual([(e['item_id'], e['owner_uid'], e['source_uid']) for e in entry], [(RYNX_FUR, uid_a, mob.uid)])
        self.assertEqual(a.s2c(pkts[ops.index(0x18)])['item_id'], 0)
        gid = entry[0]['ground_id']
        # B: the same death and the same item, still A's
        seen = b.recv_until_quiet(0.2)
        self.assertEqual([b.s2c(p)['uid'] for p in seen if p.opcode == 0x29], [mob.uid])
        entry_b = [r for p in seen if p.opcode == 0x12 for r in b.s2c(p)['repeat[entry_count]']]
        self.assertEqual([(e['item_id'], e['owner_uid'], e['ground_id']) for e in entry_b], [(RYNX_FUR, uid_a, gid)])
        # the ledger: down for value_num * BOSS_RESPAWN_SCALE = 300 s, persisted by the tick thread
        led = self.server.bosses.ledger
        got = led.get(self.key())
        self.assertEqual((got.alive, got.next_spawn_at, got.last_killer, got.kills), (False, T0 + 300, 'TestHero', 1))
        self.server.ticks.run_due(now)
        self.assertFalse(self.saved()[self.key()]['alive'])
        # "You don't have ownership of this item.": B's pickup inside the 15 s is refused, A's taken
        b.send_c2s(self.keys['pickup'], {'ground_item_uid': gid})
        b.expect_silence(0.2)
        a.send_c2s(self.keys['pickup'], {'ground_item_uid': gid})
        self.assertEqual(a.s2c(a.expect(0x13)), {'ground_id': gid, 'quantity': 1, 'picker_uid': uid_a})
        self.assertEqual(b.s2c(b.expect(0x13))['picker_uid'], uid_a)
        self.assertEqual(INV.Inventory(self.server.store.characters('test')[0]).count(RYNX_FUR), 1)
        # the corpse goes at 3 s; nothing at the regular 15 s respawn
        self.server.ticks.run_due(now + W.MOB_CORPSE_SECS + 0.5)
        for c in (a, b):
            self.assertEqual(c.s2c(c.expect(0x06)), {'uid': mob.uid})
        self.server.ticks.run_due(now + W.MOB_RESPAWN_SECS + 1.0)
        a.expect_silence(0.1)
        b.expect_silence(0.1)
        # 200 s: B leaves and comes back - still no Rynx in B's 0x1A burst
        self.wall.t = T0 + 200
        self.server.ticks.run_due(now + 200)
        back = self.leave_and_return(b)
        self.assertEqual(len(back), self.n_mobs() - 1)
        self.assertNotIn(mob.uid, back)
        self.server.ticks.run_due(now + 299)
        a.expect_silence(0.1)
        # 300 s: the same Rynx comes back on both clients
        self.wall.t = T0 + 300
        self.server.ticks.run_due(now + 300.5)
        for c in (a, b):
            self.assertEqual(self.uids(c, [c.expect(0x1A)]), [mob.uid])
        self.assertTrue(mob.alive and mob.hp == mob.max_hp)
        self.assertTrue(led.get(self.key()).alive)
        self.server.ticks.run_due(now + 301)
        self.assertTrue(self.saved()[self.key()]['alive'])

    def test_a_discarded_map_rebuilds_the_boss_down_until_it_is_due(self):
        self.server.config = cfgmod.from_dict({**dict(self.server.config), 'MOB_MAP_KEEP_SECS': 0})
        a, ua = self.enter()
        mob = self.rynx()
        self.kill(a, mob)
        index, dest = self.boss['out']
        self.portal(a, index, dest)                                  # the last player: discarded at once
        self.assertFalse(self.mons().populated)
        self.wall.t = T0 + 100
        index, dest = self.boss['back']
        got = self.portal(a, index, dest)
        self.assertEqual(sorted(got), sorted(u for u in ua if u != mob.uid))
        again = self.rynx()
        self.assertIsNot(again, mob)
        self.assertEqual((again.uid, again.alive, again.despawned), (mob.uid, False, True))
        self.assertEqual([t.name for t in again.timers], [f'boss-respawn-{mob.uid:#x}'])
        timer = again.timers[0]
        self.assertAlmostEqual(timer.when - time.monotonic(), 200.0, delta=2.0)
        # the ledger comes due while nobody is there: the next build spawns it
        index, dest = self.boss['out']
        self.portal(a, index, dest)
        self.assertTrue(timer.cancelled)                             # discarded with its map
        self.wall.t = T0 + 300
        index, dest = self.boss['back']
        self.assertEqual(sorted(self.portal(a, index, dest)), sorted(ua))
        self.assertTrue(self.rynx().alive)
        self.assertTrue(self.server.bosses.ledger.get(self.key()).alive)

    def test_a_server_restart_keeps_the_boss_down_for_the_rest_of_its_time(self):
        a, ua = self.enter()
        mob = self.rynx()
        self.kill(a, mob)
        self.server.ticks.run_due(time.monotonic())                  # the ledger save
        self.assertFalse(self.saved()[self.key()]['alive'])
        F.close_seen(a)
        self.clients.remove(a)
        # restart at 250 s: a new server on the same directory
        self.wall.t = T0 + 250
        self.server = self.make()
        self.assertFalse(self.server.bosses.ledger.get(self.key()).alive)
        a, got = self.enter()
        self.assertEqual(len(got), self.n_mobs() - 1)
        self.assertNotIn(self.rynx().uid, got)
        now = time.monotonic()
        self.server.ticks.run_due(now + 48.0)
        a.expect_silence(0.1)
        self.wall.t = T0 + 300
        self.server.ticks.run_due(now + 51.0)
        self.assertEqual(self.uids(a, [a.expect(0x1A)]), [self.rynx().uid])
        self.assertTrue(self.rynx().alive)
        self.server.ticks.run_due(now + 52.0)
        self.assertTrue(self.saved()[self.key()]['alive'])

    def test_gm_boss_list_and_respawn(self):
        a, b, ua, _ = self.two()
        mob = self.rynx()
        texts, _ = self.gm(a, '!boss')
        n = len(B.catalog())
        self.assertTrue(texts[0].startswith(f'{n} field boss tile(s), 0 down'), texts[0])
        tx, ty = self.boss['tile']
        line = f'ch1 {self.boss["map"]} ({tx},{ty}) Rynx: up uid {mob.uid:#x} {mob.max_hp}/{mob.max_hp}'
        self.assertIn(line, texts)
        self.assertEqual(len(texts), n + 1)
        self.kill(a, mob)
        b.recv_until_quiet(0.1)
        texts, _ = self.gm(a, '!boss list')
        self.assertIn(f'ch1 {self.boss["map"]} ({tx},{ty}) Rynx: down 5m00s (TestHero)', texts)
        texts, _ = self.gm(a, '!boss respawn nosuchboss')
        self.assertTrue(any('no field boss matches' in t for t in texts), texts)
        texts, pkts = self.gm(a, '!boss respawn rynx')
        self.assertIn(f'Respawned 1: Rynx {self.boss["map"]} (uid {mob.uid:#x})', texts)
        for c, got in ((a, pkts), (b, b.recv_until_quiet(0.2))):
            self.assertEqual([p.opcode for p in got if p.opcode in (0x06, 0x1A)], [0x06, 0x1A])   # corpse, then fresh
            self.assertEqual(self.uids(c, got), [mob.uid])
        self.assertTrue(mob.alive)
        self.assertTrue(self.server.bosses.ledger.get(self.key()).alive)
        texts, _ = self.gm(a, '!boss respawn all')
        self.assertEqual(texts, ["Every boss matching 'all' is up already."])
        self.server.ticks.run_due(time.monotonic() + 400)                     # no second respawn
        a.recv_until_quiet(0.1)
        self.assertEqual([p.opcode for p in b.recv_until_quiet(0.1) if p.opcode == 0x1A], [])

    def test_bag_loot_gets_one_0x18_per_rolled_item(self):
        self.server.config = cfgmod.from_dict({**dict(self.server.config), 'GROUND_LOOT': False})
        a, _ = self.enter()
        mob = self.rynx()
        pkts = self.kill(a, mob, roll=0.0)                                # 'rates': both entries
        drops = [a.s2c(p) for p in pkts if p.opcode == 0x18]
        self.assertEqual([(d['item_id'], d['count']) for d in drops], [(RYNX_CARD, 1), (RYNX_FUR, 1)])
        self.assertEqual(drops[0]['gold'], drops[1]['gold'])              # the gold came once
        bag = INV.Inventory(self.server.store.characters('test')[0])
        self.assertEqual((bag.count(RYNX_CARD), bag.count(RYNX_FUR)), (1, 1))

    def test_a_regular_monster_kill_under_rates(self):
        """DROP_MODE 'rates' (the shipped default) on a monster that is no boss - a Kamikaze
        Rat on the boss map: every entry of its OWN hni table rolls on its own at rate /
        DROP_RATE_UNIT (no 60 % gate, no single pick), the loot is the killer's on the ground,
        and its respawn is MOB_RESPAWN_SECS with no ledger row."""
        self.assertEqual(self.server.config.DROP_MODE, 'rates')
        a, _ = self.enter()
        uid_a = P.session_uid(a.session)
        rats = [m for m in self.mons().values() if m.npccode == KAMIKAZE_RAT and m.alive]
        self.assertGreaterEqual(len(rats), 2)
        tpl = EC.npcs().get(KAMIKAZE_RAT)
        table = [(i, r) for i, r in tpl.drops if i in set(rats[0].drop_items)]
        self.assertEqual(rats[0].drop_table, table)
        self.assertTrue(any(r == 10000 for _, r in table) and any(r < 10000 for _, r in table))
        rows = len(self.server.bosses.ledger.entries)
        px, py = a.session['pos']
        # 0.0999: every 10000 entry (10 %) drops, the 4000 / 100 / 50 ones do not; 0.59: nothing
        # (the legacy roll would drop one entry at 0.59)
        for rat, roll in ((rats[0], 0.0999), (rats[1], 0.59)):
            with self.subTest(roll=roll):
                self.assertIsNone(rat.boss)
                with self.server._combat(a.session):
                    rat.x, rat.y = float(px + 60), float(py)
                pkts = self.kill(a, rat, roll=roll)
                loot = [r for p in pkts if p.opcode == 0x12 for r in a.s2c(p)['repeat[entry_count]']]
                want = sorted(i for i, r in table if roll < r / 100000)
                self.assertEqual(sorted(e['item_id'] for e in loot), want)
                self.assertEqual({(e['owner_uid'], e['source_uid']) for e in loot},
                                 {(uid_a, rat.uid)} if want else set())
                self.assertEqual(len(want) > 0, roll < 0.1)
                self.assertIsNone(self.server.bosses.respawn_delay(rat))    # MOB_RESPAWN_SECS
        self.assertEqual(len(self.server.bosses.ledger.entries), rows)       # no ledger row

    def test_single_and_legacy_modes_on_a_kill(self):
        self.server.config = cfgmod.from_dict({**dict(self.server.config), 'GROUND_LOOT': False,
                                               'DROP_MODE': 'single'})
        a, _ = self.enter()
        mob = self.rynx()
        pkts = self.kill(a, mob, roll=0.0)                                # one draw: the first entry
        self.assertEqual([a.s2c(p)['item_id'] for p in pkts if p.opcode == 0x18], [RYNX_CARD])
        self.server.bosses.ledger.force_due(self.key())
        with self.server._combat(a.session):
            self.server._respawn_monster(self.mons(), mob)
        a.recv_until_quiet(0.1)
        self.server.config = cfgmod.from_dict({**dict(self.server.config), 'DROP_MODE': 'legacy'})
        with mock.patch.object(W.random, 'choice', side_effect=lambda seq: seq[-1]):
            pkts = self.kill(a, mob, roll=0.59)                           # < 0.6: one uniform pick
        self.assertEqual([a.s2c(p)['item_id'] for p in pkts if p.opcode == 0x18], [RYNX_FUR])

    def test_a_failed_ledger_write_is_retried_from_the_tick_thread(self):
        a, _ = self.enter()
        self.kill(a, self.rynx())
        path = os.path.join(self.tmp, 'boss_ledger.json')
        now = time.monotonic()
        with mock.patch.object(B.storemod, 'atomic_write', side_effect=PermissionError(5, 'held')), \
                self.assertLogs('WS', logging.ERROR):
            self.server.ticks.run_due(now)
        self.assertTrue(self.server.bosses.ledger.dirty)
        self.assertEqual(self.server.bosses._pending.name, 'boss-ledger-retry')
        self.server.ticks.run_due(now + B.RETRY_SECS + 0.5)
        self.assertFalse(self.server.bosses.ledger.dirty)
        self.assertFalse(self.saved()[self.key()]['alive'])
        self.assertTrue(os.path.exists(path))

    def test_a_run_of_failed_ledger_writes_logs_one_traceback(self):
        """P13 post-merge review: the tick-thread saves never wait for another writer, so a
        contended ledger fails at every RETRY_SECS retry. Like store.Store._log_failure: the
        traceback once per run of failures, then one WARNING line each, and a line when it
        is saved again; the next run gets its traceback again."""
        a, _ = self.enter()
        self.kill(a, self.rynx())
        bosses = self.server.bosses
        with mock.patch.object(B.storemod, 'atomic_write', side_effect=PermissionError(5, 'held')):
            with self.assertLogs('WS', logging.WARNING) as logs:
                for _ in range(3):                                        # the save and two retries
                    self.assertFalse(bosses.tick_flush())
        boss = [r for r in logs.records if '[BOSS]' in r.getMessage()]
        self.assertEqual([(r.levelno, r.exc_info is not None) for r in boss],
                         [(logging.ERROR, True), (logging.WARNING, False), (logging.WARNING, False)])
        self.assertIn('failed again (3 in a row)', boss[-1].getMessage())
        self.assertIsNotNone(bosses._pending)                              # a save / retry is due
        with self.assertLogs('WS', logging.INFO) as logs:
            self.assertTrue(bosses.tick_flush())
        self.assertTrue(any('saved after 3 failed attempt(s)' in line for line in logs.output))
        self.assertFalse(self.server.bosses.ledger.dirty)
        with self.server.bosses.ledger.lock:
            self.server.bosses.ledger._mark()
        with mock.patch.object(B.storemod, 'atomic_write', side_effect=PermissionError(5, 'held')), \
                self.assertLogs('WS', logging.ERROR):                    # a new run: its traceback
            self.assertFalse(self.server.bosses.tick_flush())

    def test_tick_thread_saves_take_the_quick_backoff(self):
        """The livefix merge: the ledger's scheduled save, its retry and the maintenance save
        run on the tick thread, so - like store.Store.tick_flush - they get only
        QUICK_REPLACE_DELAYS of replace backoff and never wait for another writer (the retry
        takes over); only the shutdown flush keeps the full ~3 s."""
        a, _ = self.enter()
        self.kill(a, self.rynx())
        led = self.server.bosses.ledger

        def ledger_writes(write):
            return [c.args[2] for c in write.call_args_list if c.args[0] == led.path]
        with mock.patch.object(B.storemod, 'atomic_write') as write:
            self.server.ticks.run_due(time.monotonic())                   # the scheduled save
        self.assertEqual(ledger_writes(write), [B.storemod.QUICK_REPLACE_DELAYS])
        with led.lock:
            led._mark()
        with led._save_mutex:                                             # another writer on disk
            with mock.patch.object(B.storemod, 'atomic_write') as write, self.assertLogs('WS', logging.ERROR):
                self.server._maintenance_save()                           # the store's own write only
        self.assertEqual(ledger_writes(write), [])
        self.assertTrue(led.dirty)
        self.assertEqual(self.server.bosses._pending.name, 'boss-ledger-retry')
        with mock.patch.object(B.storemod, 'atomic_write') as write:
            self.assertTrue(self.server.bosses.flush())                   # shutdown: the default
        self.assertEqual(ledger_writes(write), [None])
        self.assertFalse(led.dirty)


class Bosses2009(BossFlows, Rig):
    build = B9


class Bosses2008(BossFlows, Rig):
    build = B8


if __name__ == '__main__':
    unittest.main()
