#!/usr/bin/env python3
"""
test_cashuse.py - P8 stage 3: using cash items (cashuse.py), both client builds
==============================================================================
premium_cash-use-generic, -expiry, -rename (+ lc-rename), -stat-reset, -megaphone,
-region-warp and -friend-warp (docs/systems/premium_cash.md F8-F11, F13-F15):

  - the client's look rules (FUN_00427430 / FUN_004270f0 / FUN_00427020 port): a fixed hair
    colour, a fixed hairstyle, the random dye / style with the server-chosen hair code, the
    eyes; the stored look recomposed (inventory.compose) and persisted, 0x72 to the owner (10
    / 12 / 26 B) and the observer form to the other client;
  - period items: activation (0x72 with the SYSTEMTIME), the EXP multiplier on kill exp, the
    waived death penalty, the 60 s expiry ticker (S2C 0x93) and the map-load purge (0x15);
  - rename: 0x73 + 0x74 on both clients, the friend's 0x0B, the party frames, the store (name
    index, references, save), a relog under the new name; the refusals;
  - stat reset: the record quantity is the allowance (C13), 0x76 18 B / 12 B, refusals;
  - megaphone: the orange 0x90 on every client, the owner's consume 0x72, the rate limit;
  - region / friend warp stones: 0x9A / 0x9B {1, serial} + the map load, the refusals.

Two fake clients (fakeclient.MultiClient) per server; no port is bound, no client is started
and the live accounts.json is never opened (temp copies; the module checks its hash).
"""
import dataclasses
import datetime
import hashlib
import json
import logging
import os
import random
import shutil
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import fakeclient as F  # noqa: E402
import cash as CASH  # noqa: E402
import cashuse as CU  # noqa: E402
import inventory as INV  # noqa: E402
import packets as P  # noqa: E402
import registry  # noqa: E402

W = F.import_server()
cfgmod = W.cfgmod
EC = W.EC
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = cfgmod.BUILD_2008, cfgmod.BUILD_2009
DIRS = {B8: cfgmod.DEFAULTS['CLIENT_DIR'], B9: cfgmod.DEFAULTS['CLIENT_DIR_2009']}
HAVE = {b: os.path.exists(os.path.join(HERE, d, 'hs', 'windslayer.hii')) for b, d in DIRS.items()}

YELLOW, WHITE, RANDOM_DYE = 3409, 3415, 3408
STYLE_F, STYLE_M, PONYTAIL, PRINCE = 3385, 3386, 3395, 3396
EYES_M = 3117                                   # "Basic Lavender Eyes" (Gender 1, Spr 101)
EXP_7D, EXP2_7D, WAIVE = 3323, 3326, 1891
NICK, RESET5, MEGA, SUPER = 1895, 3214, 3381, 3379
REGION10, FRIEND10 = 3434, 3433
PORTAL_101_TO_102 = bytes.fromhex('17000000')   # live 0x7E capture: 101 line 23

_LIVE_HASH = None


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def setUpModule():
    global _LIVE_HASH
    _LIVE_HASH = _sha(LIVE_ACCOUNTS) if os.path.exists(LIVE_ACCOUNTS) else None
    P.STRICT_FIELDS.add(B9)                 # every 2009 S2C must use the 2009 field names


def tearDownModule():
    P.STRICT_FIELDS.discard(B9)
    EC.configure(DIRS[B8], B8)
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_cashuse.py (tests must only use temp copies)'


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _ops(pkts):
    return [p.opcode for p in pkts]


def _text(value):
    return P.to_bytes(value).split(b'\x00', 1)[0].decode('cp949', 'replace')


def _char(name, gm=0, gender=None, map_code=101, x=700.0, y=812.0):
    c = {'name': name, 'level': 1, 'class': 0, 'map': map_code, 'x': x, 'y': y, 'hp': 100, 'mp': 50, 'gm': gm}
    if gender is not None:
        c['gender'] = gender
    return c


class _Rng:
    """random.Random stand-in that plays back a fixed sequence of randrange results."""

    def __init__(self, *values):
        self.values = list(values)

    def randrange(self, n):
        v = self.values.pop(0)
        assert 0 <= v < n, (v, n)
        return v


# ============================================================== pure rules ===
class HairRules(unittest.TestCase):
    """The client's own hair arithmetic (FUN_00427020 / FUN_00427080 / FUN_004270f0), read
    off the 2008 asm and identical in 2009 (FUN_00428590 / 4285F0 / 428660)."""

    def test_colour_digit_and_default_table(self):
        self.assertEqual(CU.hair_color(415, 0), 1)                       # tens digit
        self.assertEqual(CU.hair_color(532, 1), 3)
        # creation codes (tens 0): the table at 0x6F103C / 0x525B64, 5 * gender + style
        self.assertEqual([CU.hair_color(s, 0) for s in (1, 2, 3, 4)], [5, 2, 1, 5])
        self.assertEqual([CU.hair_color(100 + s, 1) for s in (1, 2, 3, 4)], [1, 4, 3, 6])
        self.assertEqual(CU.hair_color(405, 0), 0)                       # style 5: no default
        self.assertEqual(CU.hair_color(400, 1), 0)

    def test_fixed_colour_and_style(self):
        self.assertEqual(CU.set_color(415, 7), 475)
        self.assertEqual(CU.dye(101, YELLOW), 111)                       # (u8)(0xD51 - 0x50) = 1
        self.assertEqual(CU.dye(516, WHITE), 576)                        # 0xD57 -> 7
        self.assertEqual(CU.restyle(411, 101, 1), 411)                   # old colour 1 (table) kept
        self.assertEqual(CU.restyle(516, 131, 1), 536)                   # Prince, old colour 3
        self.assertEqual(CU.restyle(4, 25, 0), 24)                       # Basic Pigtails, colour 2

    def test_random_dye_skips_the_current_colour(self):
        # current colour of 131 is 3; rand % 7 + 1: 2 -> 3 (same, rolled again), 4 -> 5
        self.assertEqual(CU.random_dye(131, 1, _Rng(2, 4)), 151)
        for _ in range(200):
            hair = CU.random_dye(101, 1, random.Random())
            self.assertIn(CU.hair_color(hair, 1), range(2, 8))           # 101's colour is 1
            self.assertEqual(hair % 10, 1)

    def test_random_style_sets_the_new_hair_set(self):
        # male: 5xx, 1..6; female: 4xx, 1..7; the old colour kept, the old new-set style avoided
        self.assertEqual(CU.random_style(101, 1, _Rng(2)), 513)          # colour 1 (table), style 3
        self.assertEqual(CU.random_style(2, 0, _Rng(6)), 427)            # colour 2 (table), style 7
        self.assertEqual(CU.random_style(533, 1, _Rng(2, 0)), 531)       # style 3 is current: again
        self.assertEqual(CU.random_style(103, 1, _Rng(2)), 533)          # 1xx: nothing to avoid
        with self.assertRaises(AssertionError):
            CU.random_style(101, 1, _Rng(6))                              # male rolls % 6

    def test_boost_and_texts(self):
        self.assertEqual((CU.boosted(10, 50), CU.boosted(10, 100), CU.boosted(1, 50), CU.boosted(10, 0)),
                         (15, 20, 1, 10))
        now = CASH.local_now()
        rows = [CASH.make_record(0x1001, EXP_7D, 2, 1, expire=now + datetime.timedelta(days=1)),
                CASH.make_record(0x1002, EXP2_7D, 2, 1, expire=now - datetime.timedelta(days=1)),
                CASH.make_record(0x1003, EXP2_7D, 2, 1)]                  # not activated
        self.assertEqual(CU.boost_pct({'cash_items': rows}), 50)
        self.assertFalse(CU.waives_penalty({'cash_items': rows}))
        self.assertEqual(CU.megaphone_text('TestHero', b'hello\x00junk'), b'[Megaphone] TestHero : hello')
        self.assertEqual(CU.megaphone_text('A', b'\x01 \t '), b'')
        self.assertEqual(len(CU.megaphone_text('x' * 16, b'y' * 60)), 87)

    def test_rename_and_stat_rules(self):
        self.assertIsNone(CU.rename_refusal('NewHero', 'TestHero', None, 'test'))
        self.assertIn('invalid', CU.rename_refusal('New Hero', 'TestHero', None, 'test'))
        self.assertIn('invalid', CU.rename_refusal('WindKing', 'TestHero', None, 'test'))
        self.assertIn('already used', CU.rename_refusal('Watcher', 'TestHero', 'admin', 'test'))
        self.assertIsNone(CU.rename_refusal('testhero', 'TestHero', 'test', 'test'))    # a case change
        self.assertIn('same', CU.rename_refusal('TestHero', 'TestHero', 'test', 'test'))
        rec = {'qty': 5}
        self.assertIsNone(CU.stat_reset_refusal(RESET5, [2, 0, 1, 0], [3, 2, 1, 3], rec))
        self.assertIn('allows 5', CU.stat_reset_refusal(RESET5, [3, 2, 1, 0], [3, 2, 1, 3], rec))
        self.assertIn('< 0', CU.stat_reset_refusal(RESET5, [0, 3, 0, 0], [3, 2, 1, 3], rec))
        self.assertIn('nothing', CU.stat_reset_refusal(RESET5, [0, 0, 0, 0], [3, 2, 1, 3], rec))
        self.assertIn('no usable', CU.stat_reset_refusal(RESET5, [1, 0, 0, 0], [3, 2, 1, 3], None))
        self.assertIn('no stat reset', CU.stat_reset_refusal(MEGA, [1, 0, 0, 0], [3, 2, 1, 3], rec))


class LookChangePerBuild(unittest.TestCase):
    """look_change on each build's own item table: the switch on Cash_Cls, the random ids,
    and the composition the random branch runs equals the item's own Kind / Cash / Job."""

    def test_both_catalogs(self):
        for build in (B8, B9):
            if not HAVE[build]:
                continue
            with self.subTest(build=build):
                EC.configure(DIRS[build], build)
                cat = EC.items()

                def change(item, old, gender=1, rng=None):
                    return CU.look_change(CASH.cash_def(item), cat.get(item).spr_num, old, gender, rng or _Rng(0))
                self.assertEqual(change(YELLOW, 101), (111, None))
                self.assertEqual(change(PRINCE, 131), (536, None))
                self.assertEqual(change(EYES_M, 101), (101, None))
                self.assertEqual(change(RANDOM_DYE, 101, rng=_Rng(3)), (141, 141))
                self.assertEqual(change(STYLE_M, 101, rng=_Rng(0)), (511, 511))
                self.assertIsNone(change(EXP_7D, 101))                       # Cash_Cls 14: no look
                self.assertIsNone(change(MEGA, 101))                         # 15: none
                for item in CU.RANDOM_LOOK:                                  # 0x72 random branch args
                    d = cat.get(item)
                    self.assertEqual((d.kind, d.cash, INV._job(d, 0)), (INV.LAYER_HAIR, 1, 1))
        EC.configure(DIRS[B8], B8)


class Registry(unittest.TestCase):
    def test_routes_and_must_reply(self):
        for routes in (W.GameServer.ROUTES, W.GameServer.ROUTES_2009):
            for op, handler in ((0x48, '_handle_cash_item_use'), (0x49, '_handle_cash_rename'),
                                (0x4A, '_handle_cash_stat_reset'), (0x4C, '_handle_cash_megaphone'),
                                (0x70, '_handle_cash_region_warp'), (0x71, '_handle_cash_friend_warp')):
                self.assertEqual((routes[op].handler, routes[op].style), (handler, registry.STYLE_REC))
        self.assertIsNotNone(registry.MUST_REPLY[0x4A].refusal)
        self.assertNotIn(0x4C, registry.MUST_REPLY)                          # no waiting box

    def test_config(self):
        self.assertEqual(cfgmod.defaults()['MEGAPHONE_CONSUME_PACKET'], '0x72')
        self.assertEqual(cfgmod.from_dict({'MEGAPHONE_CONSUME_PACKET': '0x6F'})['MEGAPHONE_CONSUME_PACKET'], '0x6F')
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.from_dict({'MEGAPHONE_CONSUME_PACKET': '0x15'})


# ================================================================ in world ===
class _UseWorld:
    """TestHero (GM, male, uid 1) and Watcher (female, uid 2) in world on map 101 of one server
    of `build` (fakeclient.MultiClient); Carol exists offline."""
    build = B8

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        self.tmp = tempfile.mkdtemp(prefix=f'ws_cashuse_{self.build}_')
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False})
        accounts = {'test': {'password': 'test', 'gender': 1,
                             'characters': [_char('TestHero', gm=1, gender=1, x=1411.0, y=714.0)]},
                    'admin': {'password': 'admin', 'gender': 0,
                              'characters': [_char('Watcher', gender=0, x=1411.0, y=714.0)]},
                    'carol': {'password': 'carol', 'characters': [_char('Carol')]}}
        self.server = F.make_server(self.tmp, accounts=accounts, config=cfg)
        self.server.cashuse.rng = random.Random(7)
        self.hero = self.server.store.find_character('test', 'TestHero')
        self.prepare()
        self.extra = []
        self.mc = F.MultiClient(self.server)
        self.a, self.b = self.mc
        _wait(lambda: len(self.server.world.peers(self.a.session)) == 1)
        self.mc.drain(0.3)

    def prepare(self):
        """Store edits before anyone logs in."""

    def tearDown(self):
        for c in self.extra:
            try:
                c.close()
            except OSError:
                pass
        self.mc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ----------------------------------------------------------------- helpers ---
    def key(self, op, name=''):
        for v in P.variants(op, 'C2S', client_build=self.build):
            if name in v['name'] and '9999' not in v['name']:
                return v['key']
        raise KeyError(op)

    def send(self, c, op, fields=None):
        c.send_c2s(self.key(op), fields or {})

    def grant(self, item, count=None, char=None):
        return self.server.cash.grant(char or self.hero, item, count)

    def put(self, item, qty=1, char=None):
        """A stored counted record grant() refuses to make (a Cash 0 gift certificate: P8
        review minor), as older data or a hand edit can hold one."""
        rec = CASH.make_record(self.server.cash.next_serial(), item, CASH.KIND_COUNT, qty, equipped=False)
        with self.server.store.lock:
            CASH.ensure(char or self.hero).append(rec)
        return rec

    def rec(self, name):
        found = self.server.store.character_by_name(name)
        return found[2] if found else None

    def owned(self, char=None):
        return {r['serial']: r for r in (char or self.hero)['cash_items']}

    def only(self, c, op, quiet=0.3):
        pkts = c.recv_until_quiet(quiet)
        mine = [p for p in pkts if p.opcode == op]
        self.assertEqual(len(mine), 1, f'expected one 0x{op:02X}, got {[hex(o) for o in _ops(pkts)]}')
        return mine[0]

    def line(self, c, text):
        self.send(c, 0x03, {'msg_len': len(text), 'message': text})

    def portal(self, c):
        c.send(0x7E, PORTAL_101_TO_102)
        self.assertTrue(_wait(lambda: self.server.world.map_of(c.session) == 102))
        return c.recv_until_quiet(0.3)

    def saved(self):
        """The accounts file as written (the store saves at once or on flush)."""
        self.server.store.flush()
        with open(os.path.join(self.tmp, 'accounts.json'), encoding='utf-8') as f:
            return json.load(f)

    def saved_char(self, account, name):
        return next(c for c in self.saved()[account]['characters'] if c['name'] == name)

    def use_refused(self, c, item):
        """C2S 0x48 {item} answered by CashUse.use's own refusal: 0x72 {uid, 0, 0} (10 B), its
        '[CASHUSE] ... refused' line, and nothing at ERROR - so not the registry's MUST_REPLY
        backstop, which catches a handler that raised and logs '[DISPATCH] ... raised'."""
        with self.assertLogs('WS', level='INFO') as cm:
            self.send(c, 0x48, {'item_id': item})
            pkt = self.only(c, 0x72)
        errors = [r.getMessage() for r in cm.records if r.levelno >= logging.ERROR]
        self.assertEqual(errors, [])
        self.assertTrue(any('[CASHUSE]' in r.getMessage() and 'refused' in r.getMessage() for r in cm.records),
                        cm.output)
        self.assertEqual(len(pkt.payload), 10)
        self.assertEqual(c.s2c(pkt), {'player_uid': 1, 'item_id': 0, 'item_serial': 0})

    # ------------------------------------------------------------ look (F8) ---
    def test_hair_colour_changes_the_look_on_both_clients_and_persists(self):
        a, b = self.a, self.b
        rec = self.grant(YELLOW)
        self.assertEqual(INV.Inventory(self.hero).look_words(1)[INV.LAYER_HAIR], 101)
        self.send(a, 0x48, {'item_id': YELLOW})
        own = self.only(a, 0x72)
        self.assertEqual(len(own.payload), 10)
        self.assertEqual(a.s2c(own), {'player_uid': 1, 'item_id': YELLOW, 'item_serial': rec['serial']})
        seen = self.only(b, 0x72)
        self.assertEqual(len(seen.payload), 6)                          # the observer form
        self.assertEqual(b.s2c(seen, assume={'player_uid != local_player_uid': True}),
                         {'player_uid': 1, 'item_id': YELLOW})
        look = self.hero['look']
        self.assertEqual((look[INV.LAYER_HAIR], look[INV.LAYER_HELM], look[INV.LAYER_BACK_HAIR]), (111, 111, 111))
        self.assertNotIn(rec['serial'], self.owned())                    # counted x1: used up
        self.assertEqual(self.saved_char('test', 'TestHero')['look'][INV.LAYER_HAIR], 111)
        # the spawn record a later observer gets carries the new hair
        c = F.FakeClient(self.server)
        self.extra.append(c)
        self.assertEqual(c.login('carol', 'carol')['result'], 1)
        entry = c.enter_world('Carol', port=F.P2P_PORT_BASE + 2)
        recs = [r for p in entry if p.opcode == 0x04 for r in c.s2c(p)['repeat[player_count]']]
        hero = next(r for r in recs if r['uid'] == 1)
        self.assertEqual(self._look_of(hero)[INV.LAYER_HAIR], 111)

    @staticmethod
    def _look_of(record):
        """The appearance words of a decoded 0x04 / 0x05 / 0x07 player record."""
        for v in record.values():
            if isinstance(v, list) and v and isinstance(v[0], dict) and set(v[0]) == {'appearance_part'}:
                return [x['appearance_part'] for x in v]
        raise AssertionError(f'no look in {sorted(record)}')

    def test_random_dye_and_style_carry_the_servers_hair_code(self):
        a, b = self.a, self.b
        self.server.cashuse.rng = _Rng(3, 1)                           # colour 4; style 2
        dye = self.grant(RANDOM_DYE)
        self.send(a, 0x48, {'item_id': RANDOM_DYE})
        own = self.only(a, 0x72)
        self.assertEqual(len(own.payload), 12)
        self.assertEqual(a.s2c(own), {'player_uid': 1, 'item_id': RANDOM_DYE, 'hair_code': 141,
                                      'item_serial': dye['serial']})
        seen = self.only(b, 0x72)
        self.assertEqual(len(seen.payload), 8)
        self.assertEqual(b.s2c(seen, assume={'player_uid != local_player_uid': True})['hair_code'], 141)
        self.assertEqual(self.hero['look'][INV.LAYER_HAIR], 141)
        style = self.grant(STYLE_M)
        self.send(a, 0x48, {'item_id': STYLE_M})
        self.assertEqual(a.s2c(self.only(a, 0x72))['hair_code'], 542)   # 5xx, colour 4 kept, style 2
        b.recv_until_quiet(0.2)
        self.assertEqual(self.hero['look'][INV.LAYER_HAIR], 542)
        self.assertNotIn(style['serial'], self.owned())
        # the other gender's random hairstyle is refused (hii Gender 2)
        female = self.grant(STYLE_F)
        self.use_refused(a, STYLE_F)
        self.assertIn(female['serial'], self.owned())
        self.assertEqual(self.hero['look'][INV.LAYER_HAIR], 542)

    def test_refusals_change_nothing(self):
        a = self.a
        self.use_refused(a, YELLOW)                                     # not owned
        mega = self.grant(MEGA, 3)
        self.use_refused(a, MEGA)                                       # has its own window / request
        self.assertEqual(self.owned()[mega['serial']]['qty'], 3)
        self.use_refused(a, 179)                                        # no cash item
        card = self.put(3327)                                            # no effect modelled: kept
        self.use_refused(a, 3327)
        self.assertEqual(self.owned()[card['serial']]['qty'], 1)
        self.assertEqual(self.b.recv_until_quiet(0.1), [])

    def test_use_refusal_needs_no_must_reply_backstop(self):
        """With the registry's 0x48 refusal switched off (the MUST_REPLY row with refusal None)
        the waiting box still gets its 10 B 0x72: CashUse.use builds it itself (item 0 has no
        Cash_T, so it passes REFUSAL_72_ASSUME - without it P.build raises MissingAssume)."""
        row = dataclasses.replace(registry.MUST_REPLY[0x48], refusal=None)
        with mock.patch.dict(registry.MUST_REPLY, {0x48: row}):
            self.use_refused(self.a, YELLOW)
            self.grant(MEGA)
            self.use_refused(self.a, MEGA)
        self.assertEqual(self.b.recv_until_quiet(0.1), [])

    def test_a_registered_effect_makes_an_item_usable(self):
        """CashUse.effects (ROADMAP_2009_ADDENDUM C6): an owner registers the effect; the use
        then consumes the record, sends both 0x72 forms and runs it with the used record."""
        a, b = self.a, self.b
        ran = []
        self.server.cashuse.effects[3327] = lambda server, s, d, rec: ran.append((s['char_name'], d.id, rec['qty']))
        card = self.put(3327, 2)
        self.send(a, 0x48, {'item_id': 3327})
        self.assertEqual(a.s2c(self.only(a, 0x72)), {'player_uid': 1, 'item_id': 3327, 'item_serial': card['serial']})
        self.assertEqual(len(self.only(b, 0x72).payload), 6)
        self.assertEqual(ran, [('TestHero', 3327, 1)])
        self.assertEqual(self.owned()[card['serial']]['qty'], 1)

    # ------------------------------------------------------ period items (F8) ---
    def test_period_exp_item_activates_boosts_and_shows_its_expiry(self):
        a, b = self.a, self.b
        rec = self.grant(EXP_7D)
        before = CASH.local_now()
        self.send(a, 0x48, {'item_id': EXP_7D})
        own = self.only(a, 0x72)
        self.assertEqual(len(own.payload), 26)                          # + 16-byte SYSTEMTIME
        got = a.s2c(own)
        year, month, _dow, day, hour, minute = struct.unpack('<6H', got['expire_time'][:12])
        until = datetime.datetime(year, month, day, hour, minute)
        self.assertLessEqual(abs((until - (before + datetime.timedelta(days=7))).total_seconds()), 120)
        self.assertEqual(len(self.only(b, 0x72).payload), 6)
        stored = self.owned()[rec['serial']]
        self.assertTrue(CASH.is_activated(stored))
        self.assertEqual(stored['qty'], 1)
        self.assertEqual(self.server.cashuse.boost_exp(a.session, 10), 15)
        # a second one of the same effect is refused while it runs
        self.grant(EXP_7D)
        self.use_refused(a, EXP_7D)
        # every later 0x6F carries it activated (its tail prints "will expire in 7 day(s).")
        pkts = self.portal(a)
        owned = a.s2c([p for p in pkts if p.opcode == 0x6F][0])
        row = next(r for r in owned['repeat[count]'] if r['serial'] == rec['serial'])
        self.assertEqual((row['limit_type'], row['expire_year'], row['expire_day']), (2, until.year, until.day))

    def test_exp_item_raises_kill_exp(self):
        a = self.a
        self.grant(EXP2_7D)
        self.send(a, 0x48, {'item_id': EXP2_7D})
        self.only(a, 0x72)
        self.portal(a)
        self.mc.drain(0.2)
        mob = next(m for m in a.session['monsters'].values() if m.alive)
        for _ in range(60):
            if not mob.alive:
                break
            self.server._memory_melee(mob.x, mob.y, uid=a.session['uid'])
        self.assertFalse(mob.alive)
        gains = [struct.unpack_from('<i', p.payload)[0] for p in a.recv_until_quiet(0.3) if p.opcode == 0x21]
        self.assertEqual(gains, [CU.boosted(self.server._kill_exp(a.session, mob), 100)])

    def test_waive_exp_penalty(self):
        self.assertFalse(self.server.cashuse.waives_penalty(self.a.session))
        self.grant(WAIVE)
        self.send(self.a, 0x48, {'item_id': WAIVE})
        self.assertEqual(len(self.only(self.a, 0x72).payload), 26)
        self.assertTrue(self.server.cashuse.waives_penalty(self.a.session))

    # ---------------------------------------------------------- expiry (F15) ---
    def activate(self, item=EXP_7D, when=None):
        rec = self.grant(item)
        with self.server.store.lock:
            rec['expire'] = CASH.format_expire(when or CASH.local_now() + datetime.timedelta(days=1))
        return rec

    def test_the_ticker_expires_with_0x93_and_ends_the_boost(self):
        a, b = self.a, self.b
        rec = self.activate()
        self.assertEqual(self.server.cashuse.tick_expiry(), 0)
        with self.server.store.lock:
            rec['expire'] = CASH.format_expire(CASH.local_now() - datetime.timedelta(seconds=1))
        self.assertEqual(self.server.cashuse.tick_expiry(), 1)
        self.assertEqual(a.s2c(self.only(a, 0x93)), {'item_serial': rec['serial']})
        self.assertEqual(b.recv_until_quiet(0.1), [])
        self.assertNotIn(rec['serial'], self.owned())
        self.assertEqual(self.server.cashuse.boost_exp(a.session, 10), 10)
        self.assertNotIn(rec['serial'], {r['serial'] for r in self.saved_char('test', 'TestHero')['cash_items']})

    def test_dev_expire_runs_the_same_path(self):
        a = self.a
        rec = self.activate()
        self.line(a, f'!cash expire {EXP_7D}'.encode())
        pkts = a.recv_until_quiet(0.3)
        self.assertEqual(_ops(pkts), [0x93, 0x15])
        self.assertEqual(a.s2c(pkts[0]), {'item_serial': rec['serial']})
        self.assertIn('expired now', _text(a.s2c(pkts[1])['text']))

    def test_expired_offline_is_removed_at_the_map_load_with_the_clients_words(self):
        a = self.a
        live, gone = self.activate(), self.activate(EXP2_7D, CASH.local_now() - datetime.timedelta(hours=1))
        pkts = self.portal(a)
        ops = _ops(pkts)
        i = ops.index(0x6F)
        rows = a.s2c(pkts[i])['repeat[count]']
        self.assertEqual([r['serial'] for r in rows], [live['serial']])  # left out of the 0x6F
        self.assertEqual(ops[i + 1], 0x15)                                # then the notice
        self.assertEqual(_text(a.s2c(pkts[i + 1])['text']), f'[{EC.item_name(EXP2_7D)}] is expired.')
        self.assertNotIn(gone['serial'], self.owned())
        self.assertNotIn(0x93, ops)

    # --------------------------------------------------------- rename (F9) ---
    def test_rename_shows_on_both_clients_friend_list_party_and_store(self):
        a, b = self.a, self.b
        with self.server.store.lock:
            self.rec('Watcher')['friends'] = ['TestHero']
        self.send(b, 0x2F)                                               # b's list is synced
        b.recv_until_quiet(0.2)
        self.server.party.invite(a.session, 2)
        b.expect(0x4E)
        self.server.party.accept(b.session, 'TestHero')
        a.recv_until_quiet(0.2)
        b.recv_until_quiet(0.2)
        nick = self.grant(NICK)
        self.send(a, 0x49, {'new_name': 'NewHero'})
        pkts = a.recv_until_quiet(0.3)
        self.assertEqual(_ops(pkts)[:2], [0x73, 0x74])
        self.assertEqual(a.s2c(pkts[0]), {'result': 1, 'item_serial': nick['serial']})
        self.assertEqual(len(pkts[1].payload), 21)
        self.assertEqual(_text(a.s2c(pkts[1])['new_name']), 'NewHero')
        got = {p.opcode: p for p in b.recv_until_quiet(0.3)}
        self.assertEqual(len(got[0x74].payload), 21)
        self.assertEqual(_text(b.s2c(got[0x74])['new_name']), 'NewHero')
        rows = b.s2c(got[0x0B])['repeat[friend_count]']
        self.assertEqual([_text(r['name']) for r in rows], ['NewHero'])
        self.assertEqual(_text(b.s2c(got[0x4F])['member_name']), 'NewHero')   # frame rebuilt
        self.assertEqual(b.session['party_hud'][0], 1)
        # the store: the record, the references, the index, saved at once
        store = self.server.store
        self.assertIsNone(store.find_character('test', 'TestHero'))
        self.assertIs(store.find_character('test', 'NewHero'), self.hero)
        self.assertEqual((store.name_owner('newhero'), store.name_owner('testhero')), ('test', None))
        self.assertEqual(self.rec('Watcher')['friends'], ['NewHero'])
        self.assertIs(self.server.world.by_char_name('NewHero'), a.session)
        self.assertEqual(a.session['char_name'], 'NewHero')
        self.assertNotIn(nick['serial'], self.owned())
        on_disk = self.saved()
        self.assertEqual([c['name'] for c in on_disk['test']['characters']], ['NewHero'])
        self.assertEqual(next(c for c in on_disk['admin']['characters'])['friends'], ['NewHero'])
        # chat carries the new name; a relog under it works
        self.line(b, b'hi')
        a.recv_until_quiet(0.2)
        gone = a.session
        a.close()
        self.assertTrue(_wait(lambda: self.server.world.by_char_name('NewHero') is not gone))
        c = F.FakeClient(self.server)
        self.extra.append(c)
        rec = c.login('test', 'test')
        self.assertEqual([_text(ch['name']) for ch in rec['repeat[char_count]']], ['NewHero'])
        c.enter_world('NewHero', port=F.P2P_PORT_BASE)
        self.assertTrue(c.session['in_world'])

    def test_rename_moves_the_session_name_before_the_store_lock_is_released(self):
        """GameServer._session_char finds the record by session['char_name'] and falls back to
        chars[0]: the name must change before the rename's store.lock hold ends, or a thread
        waking on that lock (tick save, regen) resolves this session to the wrong record."""
        a, store = self.a, self.server.store
        real, renamed, at_release = store.lock, [], []
        rename_character = store.rename_character

        def spy(*args, **kw):
            out = rename_character(*args, **kw)
            renamed.append(out)
            return out

        class Lock:
            """store.lock stand-in (same RLock): notes the session name at the first release
            after rename_character returned - the end of CashUse.rename's hold."""
            def acquire(self, *args, **kw):
                return real.acquire(*args, **kw)

            def release(self):
                if renamed and not at_release:
                    at_release.append(a.session.get('char_name'))
                real.release()

            __enter__ = acquire

            def __exit__(self, *exc):
                self.release()

        self.grant(NICK)
        with mock.patch.object(store, 'lock', Lock()), mock.patch.object(store, 'rename_character', spy):
            self.send(a, 0x49, {'new_name': 'NewHero'})
            self.assertEqual(_ops(a.recv_until_quiet(0.3))[:2], [0x73, 0x74])
        self.assertEqual(at_release, ['NewHero'])
        self.assertEqual(a.session['char_name'], 'NewHero')

    def test_a_failed_rename_save_leaves_a_complete_rename_for_the_retry(self):
        """P8 review: the rename's immediate save runs after store.lock (livetest bug 7). A write
        that fails after every replace retry no longer turns into a refusal over a record that
        is already renamed in memory (a free rename, and a session name left on the old
        record): the rename stands, the store stays dirty and its retry writes it."""
        a, store = self.a, self.server.store
        nick = self.grant(NICK)
        with mock.patch.object(store, '_write_snapshot', side_effect=PermissionError(5, 'held by a reader')), \
                self.assertLogs('WS', logging.ERROR) as logs:
            self.send(a, 0x49, {'new_name': 'NewHero'})
            pkts = a.recv_until_quiet(0.3)
        self.assertEqual(_ops(pkts)[:2], [0x73, 0x74])
        self.assertEqual(a.s2c(pkts[0]), {'result': 1, 'item_serial': nick['serial']})
        self.assertTrue(any('immediate accounts.json save failed' in line for line in logs.output), logs.output)
        self.assertEqual(a.session['char_name'], 'NewHero')
        self.assertIs(store.find_character('test', 'NewHero'), self.hero)
        self.assertNotIn(nick['serial'], self.owned())
        self.assertTrue(store.dirty)
        self.assertEqual([c['name'] for c in self.saved()['test']['characters']], ['NewHero'])   # the retry

    def test_rename_refusals(self):
        a = self.a
        for name, why in (('NewHero', 'no item'), ('Watcher', 'taken'), ('carol', 'taken (case)'),
                          ('Bad Name', 'space'), ('GameMasterX', 'reserved')):
            if why != 'no item' and not any(r['item_id'] == NICK for r in self.hero['cash_items']):
                self.grant(NICK)
            with self.subTest(name=name, why=why):
                self.send(a, 0x49, {'new_name': name})
                self.assertEqual(a.s2c(self.only(a, 0x73)), {'result': 0})
        self.assertEqual(a.session['char_name'], 'TestHero')
        self.assertEqual(sum(1 for r in self.hero['cash_items'] if r['item_id'] == NICK), 1)
        self.assertEqual(self.b.recv_until_quiet(0.1), [])

    # ----------------------------------------------------- stat reset (F10) ---
    def test_stat_reset_uses_the_record_quantity(self):
        a, b = self.a, self.b
        rec = self.grant(RESET5)
        self.assertEqual(rec['qty'], 5)                                   # C13: the allowance
        self.assertEqual(CU.CashUse.stats_of(self.hero), [3, 2, 1, 3])
        self.send(a, 0x4A, {'item_id': RESET5, 'str_removed': 2, 'dex_removed': 0, 'int_removed': 1,
                            'tol_removed': 0})
        own = self.only(a, 0x76)
        self.assertEqual(len(own.payload), 18)
        self.assertEqual(a.s2c(own), {'target_uid': 1, 'str': 1, 'dex': 2, 'int': 0, 'spi': 3,
                                      'cash_item_serial': rec['serial'], 'consume_count': 3})
        seen = self.only(b, 0x76)
        self.assertEqual(len(seen.payload), 12)
        self.assertEqual(CU.CashUse.stats_of(self.hero), [1, 2, 0, 3])
        self.assertEqual(self.owned()[rec['serial']]['qty'], 2)
        self.assertEqual(self.saved_char('test', 'TestHero')['str'], 1)
        # 3 more than the 2 left, and a stat below 0: refused, unchanged, nothing consumed
        for removed in ((3, 0, 0, 0), (0, 0, 1, 0)):
            with self.subTest(removed=removed):
                self.send(a, 0x4A, dict(zip(('item_id', 'str_removed', 'dex_removed', 'int_removed',
                                             'tol_removed'), (RESET5,) + removed)))
                self.assertEqual(a.s2c(self.only(a, 0x76)),
                                 {'target_uid': 1, 'str': 1, 'dex': 2, 'int': 0, 'spi': 3,
                                  'cash_item_serial': 0, 'consume_count': 0})
        self.assertEqual(self.owned()[rec['serial']]['qty'], 2)
        self.assertEqual(b.recv_until_quiet(0.1), [])
        self.assertEqual(registry.send_refusal(self.server, a.session['sock'], a.session, 0x4A), ['0x76'])
        self.assertEqual(a.s2c(self.only(a, 0x76))['cash_item_serial'], 0)

    # ------------------------------------------------------ megaphone (F11) ---
    def test_megaphone_is_an_orange_line_on_both_clients_and_the_count_drops(self):
        a, b = self.a, self.b
        rec = self.grant(MEGA, 5)
        self.send(a, 0x4C, {'item_id': MEGA, 'message': b'hello'})
        pkts = a.recv_until_quiet(0.3)
        self.assertEqual(sorted(_ops(pkts)), [0x72, 0x90])
        line = [a.s2c(p) for p in pkts if p.opcode == 0x90][0]
        self.assertEqual(P.to_bytes(line['text']), b'[Megaphone] TestHero : hello')
        self.assertEqual(a.s2c([p for p in pkts if p.opcode == 0x72][0]),
                         {'player_uid': 1, 'item_id': MEGA, 'item_serial': rec['serial']})
        self.assertEqual(P.to_bytes(b.s2c(self.only(b, 0x90))['text']), b'[Megaphone] TestHero : hello')
        self.assertEqual(self.owned()[rec['serial']]['qty'], 4)
        # rate limit: the second within 10 s reaches nobody and costs nothing
        self.send(a, 0x4C, {'item_id': MEGA, 'message': b'again'})
        self.assertEqual(_ops(a.recv_until_quiet(0.3)), [0x15])
        self.assertEqual(b.recv_until_quiet(0.1), [])
        self.assertEqual(self.owned()[rec['serial']]['qty'], 4)
        # a Super Megaphone reaches a player on another map; none owned -> nothing at all
        self.portal(b)
        self.mc.drain(0.2)
        sup = self.grant(SUPER)
        a.session['megaphone_t'] = None
        self.send(a, 0x4C, {'item_id': SUPER, 'message': b'world'})
        self.assertIn(b'world', P.to_bytes(b.s2c(self.only(b, 0x90))['text']))
        a.recv_until_quiet(0.2)
        self.assertNotIn(sup['serial'], self.owned())
        a.session['megaphone_t'] = None
        self.send(a, 0x4C, {'item_id': SUPER, 'message': b'none left'})
        self.assertEqual(b.recv_until_quiet(0.2), [])
        # MEGAPHONE_CONSUME_PACKET '0x6F': the owner gets the whole owned list instead of 0x72
        self.server.config = cfgmod.from_dict({**dict(self.server.config), 'MEGAPHONE_CONSUME_PACKET': '0x6F'})
        a.recv_until_quiet(0.1)
        a.session['megaphone_t'] = None
        self.send(a, 0x4C, {'item_id': MEGA, 'message': b'list'})
        pkts = a.recv_until_quiet(0.3)
        self.assertEqual(sorted(_ops(pkts)), [0x6F, 0x90])
        rows = a.s2c([p for p in pkts if p.opcode == 0x6F][0])['repeat[count]']
        self.assertEqual([(r['serial'], r['quantity']) for r in rows], [(rec['serial'], 3)])

    # ---------------------------------------------------- warp stones (F13/14) ---
    def test_region_warp_stone(self):
        a = self.a
        rec = self.grant(REGION10)
        # 60000 no map; 9801 a PvP room; 219 is in the static data.map_codes table but neither
        # client has stage02_19.hmi (a MapTransfer there leaves the client unable to load);
        # 9805 (2009 Seren plain Arena), 9901 (Battlefield Lobby) and 2008 1285 have stage
        # files but no portal leads in (P8 review: design F13, a portal destination only)
        for dest in (60000, 9801, 219, 9805, 9901) + ((1285,) if self.build == B8 else ()):
            with self.subTest(dest=dest):
                self.send(a, 0x70, {'item_id': REGION10, 'dest_map_id': dest})
                self.assertEqual(a.s2c(self.only(a, 0x9A)), {'result': 0})
        self.send(a, 0x70, {'item_id': REGION10, 'dest_map_id': 102})
        self.assertTrue(_wait(lambda: self.server.world.map_of(a.session) == 102))
        pkts = a.recv_until_quiet(0.3)
        self.assertEqual(_ops(pkts)[:6], [0x9A, 0x08, 0x03, 0x07, 0x28, 0x44])
        self.assertEqual(a.s2c(pkts[0]), {'result': 1, 'cash_item_serial': rec['serial']})
        rows = a.s2c([p for p in pkts if p.opcode == 0x6F][0])['repeat[count]']
        self.assertEqual([(r['serial'], r['quantity']) for r in rows], [(rec['serial'], 9)])
        self.assertEqual(self.owned()[rec['serial']]['qty'], 9)
        self.send(a, 0x70, {'item_id': 3432, 'dest_map_id': 101})        # no such stone owned
        self.assertEqual(a.s2c(self.only(a, 0x9A)), {'result': 0})

    def test_a_record_gone_between_the_check_and_the_use_is_refused(self):
        """P8 review: the megaphone and both warp stones take their record with find() and
        consume() in ONE store-lock hold; a record gone meanwhile (consume None) is a refusal -
        no line broadcast, no 0x9A {1} or map load."""
        a, b = self.a, self.b
        self.grant(MEGA, 2)
        self.grant(REGION10)
        with mock.patch.object(self.server.cash, 'consume', return_value=None):
            self.send(a, 0x4C, {'item_id': MEGA, 'message': b'ghost'})
            self.assertEqual(a.recv_until_quiet(0.3), [])
            self.assertEqual(b.recv_until_quiet(0.1), [])
            self.send(a, 0x70, {'item_id': REGION10, 'dest_map_id': 102})
            self.assertEqual(a.s2c(self.only(a, 0x9A)), {'result': 0})
        self.assertEqual(self.server.world.map_of(a.session), 101)

    def test_expiry_reads_the_clock_once(self):
        """P8 review: take_expired resolves the clock once. It read it again for each record
        and pass, so a record expiring between the two passes left the list without being
        reported (no 0x93 / 0x15, the client kept its row)."""
        base = datetime.datetime(2026, 9, 28, 12, 0, 0)
        old = CASH.make_record(0x7001, EXP_7D, CASH.KIND_PERIOD, 1, expire=base - datetime.timedelta(days=1))
        edge = CASH.make_record(0x7002, EXP2_7D, CASH.KIND_PERIOD, 1, expire=base)
        with self.server.store.lock:
            self.hero['cash_items'] = [old, edge]
        real, calls = CASH.local_now, []

        def clock(now=None):
            if now is not None:
                return real(now)
            calls.append(1)
            return base - datetime.timedelta(seconds=1) if len(calls) == 1 else base

        with mock.patch.object(CASH, 'local_now', clock):
            gone = self.server.cashuse.take_expired(self.hero)
        self.assertEqual(len(calls), 1)
        self.assertEqual([r['serial'] for r in gone], [0x7001])
        self.assertEqual([r['serial'] for r in self.hero['cash_items']], [0x7002])

    def test_friend_warp_stone(self):
        a, b = self.a, self.b
        rec = self.grant(FRIEND10)
        self.portal(b)
        self.mc.drain(0.2)
        for name in ('Nobody', 'TestHero', 'Carol'):                       # unknown, oneself, offline
            with self.subTest(name=name):
                self.send(a, 0x71, {'item_id': FRIEND10, 'friend_name': name})
                self.assertEqual(a.s2c(self.only(a, 0x9B)), {'result': 0})
        self.send(a, 0x71, {'item_id': FRIEND10, 'friend_name': 'watcher'})
        self.assertTrue(_wait(lambda: self.server.world.map_of(a.session) == 102))
        pkts = a.recv_until_quiet(0.3)
        self.assertEqual(_ops(pkts)[:2], [0x9B, 0x08])
        self.assertEqual(a.s2c(pkts[0]), {'result': 1, 'cash_item_serial': rec['serial']})
        self.assertEqual(self.owned()[rec['serial']]['qty'], 9)
        self.assertIn(a.session, self.server.world.peers(b.session))
        # a target in the mall is refused
        self.server.mall.enter(b.session)
        self.send(a, 0x71, {'item_id': FRIEND10, 'friend_name': 'Watcher'})
        self.assertEqual(a.s2c(self.only(a, 0x9B)), {'result': 0})


class UseWorld2008(_UseWorld, unittest.TestCase):
    build = B8


class UseWorld2009(_UseWorld, unittest.TestCase):
    build = B9


if __name__ == '__main__':
    unittest.main()
