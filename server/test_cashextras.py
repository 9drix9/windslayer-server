#!/usr/bin/env python3
"""
test_cashextras.py - P8 stage 4: the cash extras, both client builds
===================================================================
premium_cash-presence, premium_cash-mileage-event, item_inventory-stone-extraction and the
retired DEV_FREE_NOTES (docs/systems/premium_cash.md F16 / F17, item_inventory.md F14):

  - presence: a player in the mall is hidden from his map (0x06 to the peers holding him, no
    record for a late arrival) and comes back only through his own EXIT (0x05 to everyone on
    the map then, 0x04 of them to him); GM /go and !warp, warp stones and any other map load
    are refused while he is inside, and a /go to him is refused too;
  - mileage events: `!mileage <n> [name|all]` -> the account's Mileage + S2C 0x70 {cash,
    mileage, 0} and the empty S2C 0x98 to an owner in the world or the mall (offline: credited
    silently); the purchase event (MILEAGE_EVENT_PCT / `!mileage event`) after a cash buy or
    gift; the cap; the config key;
  - stone extraction: C2S 0x72 with a selective / random Element Separator -> S2C 0x9C (21 B,
    the words the client sent, the removed stone, the tool's serial) + 0x18 (the stone),
    the bag model shifted and persisted into the next 0x03, one tool use consumed; every
    refusal is 0x9C {0} and changes nothing;
  - DEV_FREE_NOTES is retired (config.RETIRED_KEYS; test_cash has the note path).

No port is bound, no client is started and the live accounts.json is never opened (temp
copies; the module checks its hash at the end).
"""
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import fakeclient as F  # noqa: E402
import cash as CASH  # noqa: E402
import crafting as C  # noqa: E402
import gm  # noqa: E402
import inventory as INV  # noqa: E402
import mall as MALL  # noqa: E402
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

# C2S send-site keys per build (spec / spec_2009)
CHAT = {B8: '0x445CA7/0x03', B9: '0x44790E/0x03'}
CLOSE = {B8: '0x45F0D0/0x42', B9: '0x4655F0/0x42'}
BUY_ONE = {B8: '0x4601FF/0x43', B9: '0x468D84/0x43'}
GIFT = {B8: '0x46050C/0x47', B9: '0x467D8C/0x47'}
PASSWORD = {B8: '0x460831/0x51', B9: '0x468F05/0x51'}
EXTRACT = {B8: '0x46C0B8/0x72', B9: '0x4765EE/0x72'}
REGION_WARP = {B8: '0x4614D4/0x70', B9: '0x469C54/0x70'}

MEGAPHONE = 3381                                     # 100 Wind Cash
STICK, WOODEN_BLADE = 179, 70                        # Type 1 equipment (Lv 1 / Lv 13)
EARTH, WIND, FIRE = 2945, 2950, 2955                 # Chipped Elemental Stone - Earth / Wind / Fire
SEL1, SEL6, RND1, RND6 = 3952, 3437, 3951, 3436      # the Element Separators
REGION_STONE = 3434

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
            'accounts.json changed during test_cashextras.py (tests must only use temp copies)'


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


class Rolls:
    """A stand-in for random: randrange(n) returns the next queued value (mod n)."""

    def __init__(self, *values):
        self.values = list(values)

    def randrange(self, n):
        return (self.values.pop(0) if self.values else 0) % n


# ============================================================ pure model ===
class _ExtractRules:
    """crafting.extract on the build's own catalog: the client's socket algorithm (FUN_00424260
    / 2009 FUN_00425690) and every refusal."""
    build = B8

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        EC.configure(DIRS[self.build], self.build)
        self.char = {'name': 'X', 'inventory': {}}
        INV.ensure(self.char)
        self.bag = INV.Inventory(self.char)
        self.assertIsNotNone(self.bag.add(STICK, 1, [EARTH, WIND, EARTH, FIRE, 0, 7]))

    def tearDown(self):
        EC.configure(DIRS[B8], B8)

    def test_the_tools_are_counted_cash_bag_items_in_this_build(self):
        for tool, uses in ((SEL1, 1), (SEL6, 6), (RND1, 1), (RND6, 6)):
            d = CASH.cash_def(tool)
            self.assertEqual((d.type, d.cash, d.kind, d.quantity()), (5, 1, CASH.KIND_COUNT, uses), tool)
        self.assertEqual([C.extract_tool_kind(t) for t in (SEL1, SEL6, RND1, RND6, 1894)],
                         ['selective', 'selective', 'random', 'random', None])

    def test_selective_takes_the_first_equal_socket_and_shifts_the_rest(self):
        words = [EARTH, WIND, EARTH, FIRE, 0, 7]
        out = C.extract(self.char, SEL1, STICK, EARTH, words)
        self.assertEqual((out.result, out.stone_id, out.old_words), (C.RESULT_OK, EARTH, words))
        self.assertEqual(self.bag.instance(STICK)['w'], [WIND, EARTH, FIRE, 0, 0, 7])     # w5 kept
        self.assertEqual(self.bag.count(EARTH), 1)
        out = C.extract(self.char, SEL6, STICK, FIRE, [WIND, EARTH, FIRE, 0, 0, 7])
        self.assertEqual((out.result, self.bag.instance(STICK)['w']), (C.RESULT_OK, [WIND, EARTH, 0, 0, 0, 7]))

    def test_random_rolls_a_filled_socket_and_never_answers_0(self):
        for roll, want, left in ((0, EARTH, [WIND, EARTH, FIRE]), (1, WIND, [EARTH, EARTH, FIRE]),
                                 (3, FIRE, [EARTH, WIND, EARTH])):
            with self.subTest(roll=roll):
                self.bag.instance(STICK)['w'] = [EARTH, WIND, EARTH, FIRE, 0, 7]
                out = C.extract(self.char, RND1, STICK, 0, [EARTH, WIND, EARTH, FIRE, 0, 7], rng=Rolls(roll))
                self.assertEqual((out.result, out.stone_id), (C.RESULT_OK, want))
                self.assertEqual(self.bag.instance(STICK)['w'], left + [0, 0, 7])

    def test_refusals_change_nothing(self):
        before = (list(self.bag.instance(STICK)['w']), self.bag.totals())
        full = [EARTH, WIND, EARTH, FIRE, 0, 7]
        cases = ((1894, STICK, EARTH, full, {}, 'no Element Separator'),
                 (SEL1, STICK, EARTH, full, {'tool_owned': False}, 'no usable'),
                 (SEL1, STICK, 0, full, {}, 'needs a picked stone'),
                 (SEL1, STICK, 2960, full, {}, 'not in the sockets'),
                 (SEL1, STICK, EARTH, [EARTH, 0, WIND, 0, 0, 7], {}, 'not packed'),
                 (SEL1, STICK, EARTH, [EARTH, WIND, 0, 0, 0, 7], {}, 'no bag instance'),
                 (SEL1, EARTH, EARTH, full, {}, 'not EN equipment'),
                 (RND1, WOODEN_BLADE, 0, [0] * 6, {}, 'no stone in the sockets'))
        for tool, equip, stone, words, kw, why in cases:
            with self.subTest(why=why):
                out = C.extract(self.char, tool, equip, stone, words, **kw)
                self.assertEqual(out.result, C.RESULT_FAILED)
                self.assertEqual(out.stone_id, 0)
                self.assertIn(why, out.why)
        self.assertEqual((self.bag.instance(STICK)['w'], self.bag.totals()), before)

    def test_a_socket_word_that_is_no_stone_is_never_extracted(self):
        """P8 review: a socket word outside the elemental option stones 2945..2974 (GM-made or
        corrupt data) is never handed out through the 0x18 as a "stone": a selective pick of
        it is refused, a random roll picks among the real stones only (none: refused)."""
        self.bag.instance(STICK)['w'] = [WOODEN_BLADE, EARTH, 0, 0, 0, 7]
        out = C.extract(self.char, SEL1, STICK, WOODEN_BLADE, [WOODEN_BLADE, EARTH, 0, 0, 0, 7])
        self.assertEqual((out.result, out.stone_id), (C.RESULT_FAILED, 0))
        self.assertIn('not an elemental option stone', out.why)
        out = C.extract(self.char, RND1, STICK, 0, [WOODEN_BLADE, EARTH, 0, 0, 0, 7], rng=Rolls(0))
        self.assertEqual((out.result, out.stone_id), (C.RESULT_OK, EARTH))
        self.assertEqual(self.bag.instance(STICK)['w'], [WOODEN_BLADE, 0, 0, 0, 0, 7])
        self.assertEqual(self.bag.count(WOODEN_BLADE), 0)
        out = C.extract(self.char, RND1, STICK, 0, [WOODEN_BLADE, 0, 0, 0, 0, 7], rng=Rolls(0))
        self.assertEqual(out.result, C.RESULT_FAILED)
        self.assertIn('no elemental option stone', out.why)
        self.assertEqual([C.is_option_stone(i) for i in (2944, 2945, 2974, 2975, 70)],
                         [False, True, True, False, False])

    def test_a_full_etc_tab_refuses(self):
        # 35 full etc stacks, the Earth stone's among them: the extracted one fits nowhere
        catalog = EC.items()
        etc = [EARTH] + [i for i in sorted(catalog.defs) if i != EARTH and catalog.type_of(i) == 2][:34]
        self.bag.set_capacity('etc', 35)
        for item in etc:
            self.assertIsNotNone(self.bag.add(item, INV.STACK_MAX['etc']))
        self.assertEqual(self.bag.free_slots('etc'), 0)
        out = C.extract(self.char, SEL1, STICK, EARTH, [EARTH, WIND, EARTH, FIRE, 0, 7])
        self.assertEqual(out.result, C.RESULT_FAILED)
        self.assertIn('does not fit', out.why)
        self.assertEqual(self.bag.instance(STICK)['w'], [EARTH, WIND, EARTH, FIRE, 0, 7])


class ExtractRules2008(_ExtractRules, unittest.TestCase):
    build = B8


class ExtractRules2009(_ExtractRules, unittest.TestCase):
    build = B9


# ================================================================ in world ===
class _World:
    """TestHero (GM, uid 1) and Watcher (a player, uid 2) on map 101 of one server of `build`;
    Late (another account) logs in on demand."""
    build = B8
    config = {}

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        self.tmp = tempfile.mkdtemp(prefix=f'ws_cashx_{self.build}_')
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False,
                                **self.config})
        accounts = {'test': {'password': 'test', 'gender': 1,
                             'characters': [_char('TestHero', gm=1, gender=1)]},
                    'admin': {'password': 'admin', 'gender': 0,
                              'characters': [_char('Watcher', x=1400.0, gender=0)]},
                    'late': {'password': 'late', 'gender': 1, 'characters': [_char('Late', x=1000.0, gender=1)]}}
        self.server = F.make_server(self.tmp, accounts=accounts, config=cfg)
        self.hero = self.server.store.find_character('test', 'TestHero')
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            try:
                c.close()
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)
        EC.configure(DIRS[B8], B8)

    # ----------------------------------------------------------------- helpers ---
    def login(self, account='test', password='test', name='TestHero', port=F.P2P_PORT_BASE):
        c = F.FakeClient(self.server)
        self.clients.append(c)
        self.assertEqual(c.login(account, password)['result'], 1)
        c.entry = c.enter_world(name, port=port)
        return c

    def two(self):
        a = self.login()
        b = self.login('admin', 'admin', 'Watcher', port=F.P2P_PORT_BASE + 1)
        _wait(lambda: len(self.server.world.peers(a.session)) == 1)
        a.recv_until_quiet(0.2)
        return a, b

    def acc(self, username='test'):
        return self.server.store.account(username)

    def line(self, c, text):
        c.send_c2s(CHAT[self.build], {'msg_len': len(text), 'message': text})

    def lines(self, c, quiet=0.3):
        return [_text(c.s2c(p)['text']) for p in c.recv_until_quiet(quiet) if p.opcode == 0x15]

    def enter_mall(self, c):
        self.line(c, b'!mall')
        self.assertTrue(_wait(lambda: c.session.get('in_cash_shop')))
        pkts = c.recv_until_quiet(0.3)
        self.assertEqual(pkts[-1].opcode, 0x6A, _ops(pkts))
        return pkts

    def close(self, c):
        c.send_c2s(CLOSE[self.build], {'storage_deleted_flag': 0, 'to_storage_count': 0, 'to_character_count': 0,
                                        'repeat[to_storage_count]': [], 'repeat[to_character_count]': []})
        self.assertTrue(_wait(lambda: c.session.get('in_world') and not c.session.get('in_cash_shop')))
        return c.recv_until_quiet(0.3)

    def buy(self, c, item, mileage=0):
        if self.build == B9:
            c.send_c2s(BUY_ONE[B9], {'pay_with_mileage': mileage, 'item_count': 1,
                                     'repeat[item_count]': [{'item_code': item, 'option': 0}]})
        else:
            c.send_c2s(BUY_ONE[B8], {'pay_with_mileage': mileage, 'item_count': 1, 'item_code': item})
        return c.recv_until_quiet(0.3)

    def balance(self, c, pkt):
        self.assertEqual((pkt.opcode, len(pkt.payload)), (0x70, 9))
        rec = c.s2c(pkt)
        self.assertEqual(rec['first_purchase_bonus'], 0)            # never the first-purchase popup
        return rec['cash_balance'], rec['mileage_balance']

    def notice98(self, pkt):
        self.assertEqual((pkt.opcode, pkt.payload), (0x98, b''))

    def uids(self, c, pkts, op):
        out = []
        for p in pkts:
            if p.opcode != op:
                continue
            rec = c.s2c(p)
            out += [r['uid'] for r in rec['repeat[player_count]']] if op == 0x04 else [rec['uid']]
        return out


# ---------------------------------------------------------------- presence ---
class _Presence(_World):
    def test_a_player_in_the_mall_is_hidden_and_comes_back_on_exit(self):
        """premium_cash-presence: the peers lose him at `!mall`, a late arrival never sees him,
        and his EXIT shows him to every client on the map (and them to him)."""
        a, b = self.two()
        uid_a = a.session['uid']
        self.enter_mall(a)
        self.assertEqual(self.uids(b, [b.expect(0x06)], 0x06), [uid_a])
        c = self.login('late', 'late', 'Late', port=F.P2P_PORT_BASE + 2)       # arrives on 101 now
        self.assertEqual(self.uids(c, c.entry, 0x04), [b.session['uid']])      # Watcher only
        self.assertEqual(self.uids(b, b.recv_until_quiet(0.3), 0x05), [c.session['uid']])
        self.assertFalse(W.presence._can_show(self.server, a.session, c.session))
        self.assertNotIn(uid_a, W.presence.spawned(c.session))
        pkts = self.close(a)
        self.assertEqual(sorted(self.uids(a, pkts, 0x04)), sorted([b.session['uid'], c.session['uid']]))
        for peer in (b, c):
            self.assertEqual(self.uids(peer, peer.recv_until_quiet(0.3), 0x05), [uid_a])
            self.assertIs(W.presence.spawned(peer.session)[uid_a], a.session)

    def test_only_the_exit_brings_him_back(self):
        """No other map load reaches a player in the mall: _map_transfer refuses it, `!warp`
        says so, a GM /go to him (or from inside) is refused, a warp stone never gets there
        (cashuse refuses in the mall), and he is still hidden afterwards."""
        a, b = self.two()
        b.session['gm'] = 1
        self.enter_mall(a)
        b.expect(0x06)
        with self.assertLogs('WS', logging.WARNING) as logs:
            self.assertFalse(self.server._map_transfer(a.session['sock'], a.session, 102, 300.0, 700.0,
                                                       reason='test'))
        self.assertTrue(any('only its EXIT brings him back' in line for line in logs.output), logs.output)
        with self.assertRaises(gm.DevCommandError) as err:
            self.server._dev_warp(a.session, '102')
        self.assertIn(f'in the {MALL.mall_name(self.build)}', str(err.exception))
        self.assertFalse(self.server._gm_go(b.session, 'TestHero'))
        self.assertIn(f'[Warning] TestHero is in the {MALL.mall_name(self.build)}.', self.lines(b))
        self.assertFalse(self.server._gm_go(a.session, 'Watcher'))
        self.assertIn('Not while you are in the', self.lines(a)[0])
        self.server.cash.grant(self.hero, REGION_STONE)
        a.send_c2s(REGION_WARP[self.build], {'item_id': REGION_STONE, 'dest_map_id': 102})
        self.assertEqual(a.s2c(a.expect(0x9A)), {'result': 0})
        self.assertTrue(a.session['in_cash_shop'])
        self.assertFalse(a.session['in_world'])
        self.assertIsNone(self.server.world.map_of(a.session))
        b.expect_silence(0.2)                                            # never shown meanwhile
        self.close(a)
        self.assertEqual(self.uids(b, b.recv_until_quiet(0.3), 0x05), [a.session['uid']])

    def test_a_stale_mall_flag_does_not_block_a_fresh_world_entry(self):
        """An enter-world of a session the server still had in the mall (its client went to
        character select without EXIT) drops the mall state and loads the world."""
        a = self.login()
        self.enter_mall(a)
        with self.assertLogs('WS', logging.WARNING) as logs:
            a.session['in_world'] = False
            pkts = a.enter_world('TestHero')
        self.assertTrue(any('mall state dropped' in line for line in logs.output), logs.output)
        self.assertIn(0x07, _ops(pkts))
        self.assertFalse(a.session.get('in_cash_shop'))
        self.assertNotIn('mall', a.session)
        self.assertEqual(self.server.world.map_of(a.session), 101)


class Presence2008(_Presence, unittest.TestCase):
    build = B8


class Presence2009(_Presence, unittest.TestCase):
    build = B9


# ------------------------------------------------------------ mileage event ---
class _MileageEvent(_World):
    def test_gm_credit_sends_0x70_then_the_0x98_line(self):
        """premium_cash-mileage-event (F16): `!mileage <n>` credits the account and tells an
        online owner 0x70 {cash, mileage, 0} + 0x98 (empty); offline owners are credited
        silently; `all` reaches every logged-in account once."""
        a, b = self.two()
        self.server.cash.set_balance(self.acc(), cash=40)
        self.line(a, b'!mileage 500')
        bal, note, reply = a.expect(0x70, 0x98, 0x15)
        self.assertEqual(self.balance(a, bal), (40, 500))
        self.notice98(note)
        self.assertEqual(_text(a.s2c(reply)['text']), 'test: +500 event Mileage -> 500.')
        self.assertEqual(CASH.balance(self.acc()), (40, 500))
        self.line(a, b'!mileage +25 Watcher')
        bal, note = b.expect(0x70, 0x98)
        self.assertEqual(self.balance(b, bal), (0, 25))
        self.assertEqual(self.lines(a), ['admin: +25 event Mileage -> 25.'])
        self.line(a, b'!mileage 7 Late')                                  # offline: credited only
        self.assertEqual(self.lines(a), ['late: +7 event Mileage -> 7 (not in the world or the mall: not told).'])
        self.assertEqual(CASH.balance(self.acc('late'))[1], 7)
        self.line(a, b'!mileage 3 all')
        self.assertEqual(self.balance(b, b.expect(0x70, 0x98)[0]), (0, 28))
        pkts = a.recv_until_quiet(0.3)
        self.assertEqual(_ops(pkts), [0x70, 0x98, 0x15])
        self.assertEqual(_text(a.s2c(pkts[2])['text']), '+3 event Mileage to 2 account(s), 2 told.')
        self.assertEqual(CASH.balance(self.acc('late'))[1], 7)            # not logged in: not in "all"
        self.server.store.flush()
        with open(self.server.store.path, encoding='utf-8') as f:
            self.assertEqual({u: json.load(f)[u]['mileage'] for u in ('test',)}, {'test': 503})

    def test_an_owner_inside_the_mall_is_told_too(self):
        a, b = self.two()
        self.enter_mall(b)
        a.expect(0x06)
        self.line(a, b'!mileage 10 Watcher')
        self.assertEqual(self.balance(b, b.expect(0x70, 0x98)[0]), (0, 10))
        self.assertEqual(self.lines(a), ['admin: +10 event Mileage -> 10.'])

    def test_the_cap_and_the_refusals(self):
        a = self.login()
        self.server.cash.set_balance(self.acc(), mileage=CASH.CASH_MAX - 3)
        self.line(a, b'!mileage 10')
        self.assertEqual(self.balance(a, a.expect(0x70, 0x98, 0x15)[0])[1], CASH.CASH_MAX)
        self.line(a, b'!mileage 10')                                        # at the cap: nothing told
        self.assertEqual(self.lines(a), [f'test: +0 event Mileage -> {CASH.CASH_MAX} (at the cap).'])
        for bad in (b'!mileage 0', b'!mileage x', b'!mileage 5 Nobody', b'!mileage event', b'!mileage event -1'):
            with self.subTest(bad=bad):
                self.line(a, bad)
                [text] = self.lines(a)
                self.assertIn('!mileage [<n> [name|all]', text)
        self.assertEqual(CASH.balance(self.acc())[1], CASH.CASH_MAX)

    def test_the_purchase_event(self):
        """While a purchase event runs, a Wind Cash buy / gift earns pct % of its price after its
        own reply (0x6C / 0x71 carry the running balance without it); mileage payments earn
        nothing; `off` and `config` switch it."""
        a, b = self.two()
        self.server.cash.set_balance(self.acc(), cash=1000, mileage=0)
        self.line(a, b'!mileage')
        self.assertEqual(self.lines(a), ['Mileage event: purchases earn 0% (config 0%); your Mileage 0.'])
        self.line(a, b'!mileage event 10')
        self.assertEqual(self.lines(a), ['Mileage event: purchases earn 10%.'])
        self.enter_mall(a)
        b.expect(0x06)
        pkts = self.buy(a, MEGAPHONE)
        self.assertEqual(_ops(pkts), [0x6C, 0x70, 0x98])
        got = a.s2c(pkts[0])
        self.assertEqual((got['result'], got['cash_balance'], got['mileage_balance']), (1, 900, 0))
        self.assertEqual(self.balance(a, pkts[1]), (900, 10))
        pkts = self.buy(a, MEGAPHONE, mileage=1)                             # paid with mileage? short: cash
        if self.build == B9:                                                 # 2009 falls back to cash
            self.assertEqual(_ops(pkts), [0x6C, 0x70, 0x98])
            self.assertEqual(self.balance(a, pkts[1]), (800, 20))
        else:
            self.assertEqual(_ops(pkts), [0x6C])
            self.assertEqual(a.s2c(pkts[0])['result'], MALL.BUY_SHORT)
            self.server.cash.set_balance(self.acc(), cash=800, mileage=20)
        self.server.cash.set_balance(self.acc(), mileage=500)
        pkts = self.buy(a, MEGAPHONE, mileage=1)                             # a mileage payment: no event
        self.assertEqual(_ops(pkts), [0x6C])
        self.assertEqual(a.s2c(pkts[0])['mileage_balance'], 400)
        if self.build == B8:
            a.send_c2s(PASSWORD[B8], {'password': 'test', 'target_window_id': 0x1F9})
            a.expect(0x80)
        fields = {'pay_with_mileage': 0, 'item_code': MEGAPHONE, 'recipient_name': 'Watcher',
                  'message_len': 2, 'message': b'hi'}
        if self.build == B9:
            fields['option'] = 0
        a.send_c2s(GIFT[self.build], fields)
        pkts = a.recv_until_quiet(0.3)
        self.assertEqual(_ops(pkts), [0x71, 0x70, 0x98])
        self.assertEqual(self.balance(a, pkts[1]), (700, 410))
        # (chat is dropped inside the mall - "not in world" - so the GM switch runs directly)
        self.server._dev_mileage(a.session, 'event off')
        self.assertEqual(self.lines(a), ['Mileage event: purchases earn 0%.'])
        self.assertEqual(_ops(self.buy(a, MEGAPHONE)), [0x6C])
        self.server.config['MILEAGE_EVENT_PCT'] = 50
        self.server._dev_mileage(a.session, 'event config')
        self.assertEqual(self.lines(a), ['Mileage event: purchases earn 50% (config).'])
        pkts = self.buy(a, MEGAPHONE)
        self.assertEqual(self.balance(a, pkts[1]), (500, 460))

    def test_config(self):
        self.assertEqual(cfgmod.defaults()['MILEAGE_EVENT_PCT'], 0)
        self.assertEqual(cfgmod.from_dict({'MILEAGE_EVENT_PCT': 25})['MILEAGE_EVENT_PCT'], 25)
        for bad in ({'MILEAGE_EVENT_PCT': -1}, {'MILEAGE_EVENT_PCT': 1001}, {'MILEAGE_EVENT_PCT': 'x'},
                    {'MILEAGE_BONUS_PCT': -5}):
            with self.subTest(bad=bad), self.assertRaises(cfgmod.ConfigError):
                cfgmod.from_dict(bad)
        self.assertNotIn('DEV_FREE_NOTES', cfgmod.DEFAULTS)
        self.assertIn('DEV_FREE_NOTES', cfgmod.RETIRED_KEYS)
        with open(os.path.join(HERE, 'config.json'), encoding='utf-8') as f:
            self.assertNotIn('DEV_FREE_NOTES', json.load(f))

    def test_wire(self):
        self.assertEqual(P.build('0x98', {}, client_build=self.build), b'')
        self.assertEqual(len(P.build('0x70', CASH.balance_fields({'cash': 5, 'mileage': 6}),
                                     client_build=self.build)), 9)


class MileageEvent2008(_MileageEvent, unittest.TestCase):
    build = B8


class MileageEvent2009(_MileageEvent, unittest.TestCase):
    build = B9


# --------------------------------------------------------- stone extraction ---
class _Extraction(_World):
    def socketed(self, c, words=(EARTH, WIND, FIRE, 0, 0, 0), item=STICK):
        self.assertIsNotNone(self.server._inv_add(c.session, item, 1, 'test', list(words)))

    def extract(self, c, tool, stone, words=(EARTH, WIND, FIRE, 0, 0, 0), equip=STICK):
        c.send_c2s(EXTRACT[self.build], {'mode_id': tool, 'equip_item_id': equip, 'stone_item_id': stone,
                                         'repeat[5]': [{'socket_stone_id': w} for w in words[:5]],
                                         'equip_extra': words[5]})
        return c.recv_until_quiet(0.3)

    def bag(self):
        return INV.Inventory(self.hero)

    def test_selective_extraction_answers_0x9c_and_grants_the_stone(self):
        """item_inventory-stone-extraction (F14): 0x9C {1, equip, the words BEFORE, the stone,
        the tool serial} (21 B), then 0x18 {gold, victy, stone, 1}; the model shifts the
        sockets, bags the stone, uses one of the 6 uses; the next 0x03 lists the new block."""
        a = self.login()
        tool = self.server.cash.grant(self.hero, SEL6)
        self.assertEqual(tool['qty'], 6)
        self.socketed(a)
        gold, victy = self.server._wallet(a.session)
        pkts = self.extract(a, SEL6, WIND)
        self.assertEqual(_ops(pkts), [0x9C, 0x18])
        self.assertEqual(len(pkts[0].payload), 21)
        self.assertEqual(a.s2c(pkts[0]), {'result': 1, 'equip_item_id': STICK,
                                          'repeat[5]': [{'socket_stone': w} for w in (EARTH, WIND, FIRE, 0, 0)],
                                          'socket_extra': 0, 'stone_id': WIND, 'cash_item_serial': tool['serial']})
        self.assertEqual(a.s2c(pkts[1]), {'gold': gold, 'victy': victy, 'item_id': WIND, 'count': 1})
        self.assertEqual(self.bag().instance(STICK)['w'], [EARTH, FIRE, 0, 0, 0, 0])
        self.assertEqual(self.bag().count(WIND), 1)
        self.assertEqual([r['qty'] for r in self.hero['cash_items']], [5])
        self.server.store.flush()
        with open(self.server.store.path, encoding='utf-8') as f:
            disk = json.load(f)['test']['characters'][0]
        self.assertEqual(disk['inventory']['equip'], [{'id': STICK, 'w': [EARTH, FIRE, 0, 0, 0, 0]}])
        self.assertEqual(disk['cash_items'][0]['qty'], 5)
        # the next map load's 0x03 carries the shifted block and the stone
        self.line(a, b'!warp 102 300 700')
        pkts = a.recv_until_quiet(0.4)
        rec03 = a.s2c([p for p in pkts if p.opcode == 0x03][0], allow_trailing=True)
        self.assertEqual([(r['item_id'], [o['option_value'] for o in r['repeat[option_count]']])
                          for r in rec03['repeat[equip_item_count]']], [(STICK, [EARTH, FIRE])])
        owned = [a.s2c(p) for p in pkts if p.opcode == 0x6F]
        self.assertEqual([r['quantity'] for p in owned for r in p['repeat[count]']], [5])

    def test_random_extraction_rolls_the_socket_and_a_single_use_tool_is_gone(self):
        a = self.login()
        tool = self.server.cash.grant(self.hero, RND1)
        self.socketed(a)
        self.server.CRAFT_RNG = Rolls(2)                                    # the third socket
        pkts = self.extract(a, RND1, 0)
        rec = a.s2c(pkts[0])
        self.assertEqual((rec['stone_id'], rec['cash_item_serial']), (FIRE, tool['serial']))
        self.assertEqual(a.s2c(pkts[1])['item_id'], FIRE)
        self.assertEqual(self.bag().instance(STICK)['w'], [EARTH, WIND, 0, 0, 0, 0])
        self.assertEqual(self.hero['cash_items'], [])                        # 1 use: record gone

    def test_refusals_are_0x9c_0_and_change_nothing(self):
        a = self.login()
        self.socketed(a)
        before = (list(self.bag().instance(STICK)['w']), self.bag().totals())
        cases = [(SEL1, EARTH, (EARTH, WIND, FIRE, 0, 0, 0), 'no tool owned'),
                 (1894, EARTH, (EARTH, WIND, FIRE, 0, 0, 0), 'not a separator')]
        for tool, stone, words, why in cases:
            with self.subTest(why=why):
                pkts = self.extract(a, tool, stone, words)
                self.assertEqual(_ops(pkts), [0x9C])
                self.assertEqual((len(pkts[0].payload), a.s2c(pkts[0])), (1, {'result': 0}))
        tool = self.server.cash.grant(self.hero, SEL1)
        for stone, words, why in ((2960, (EARTH, WIND, FIRE, 0, 0, 0), 'stone not in the sockets'),
                                  (EARTH, (EARTH, WIND, 0, 0, 0, 0), 'no such bag instance'),
                                  (0, (EARTH, WIND, FIRE, 0, 0, 0), 'no picked stone')):
            with self.subTest(why=why):
                pkts = self.extract(a, SEL1, stone, words)
                self.assertEqual((_ops(pkts), a.s2c(pkts[0])), ([0x9C], {'result': 0}))
        self.assertEqual((self.bag().instance(STICK)['w'], self.bag().totals()), before)
        self.assertEqual([r['qty'] for r in self.hero['cash_items']], [tool['qty']])

    def test_outside_the_world_the_backstop_answers(self):
        """In the mall (no world, no window 0x473) the MUST_REPLY row answers 0x9C {0}."""
        a = self.login()
        self.server.cash.grant(self.hero, SEL1)
        self.socketed(a)
        self.enter_mall(a)
        with self.assertLogs('WS', logging.INFO) as logs:
            pkts = self.extract(a, SEL1, EARTH)
        self.assertEqual((_ops(pkts), a.s2c(pkts[0])), ([0x9C], {'result': 0}))
        self.assertTrue(any('[MUST-REPLY] C2S 0x72' in line for line in logs.output), logs.output)
        self.assertEqual(self.bag().instance(STICK)['w'], [EARTH, WIND, FIRE, 0, 0, 0])

    def test_socket_dev_command_puts_a_socketed_item_on_the_client(self):
        """`!socket` (live-check aid): the instance goes into the model and the same-map reload's
        0x03 lists it with its words; an extraction then works on it."""
        a = self.login()
        self.server.cash.grant(self.hero, SEL1)
        self.line(a, b'!socket 179 2945 2950')
        pkts = a.recv_until_quiet(0.4)
        self.assertEqual([op for op in _ops(pkts) if op in (0x15, 0x08, 0x03, 0x07)], [0x15, 0x08, 0x03, 0x07])
        rec03 = a.s2c([p for p in pkts if p.opcode == 0x03][0], allow_trailing=True)
        self.assertEqual([(r['item_id'], [o['option_value'] for o in r['repeat[option_count]']])
                          for r in rec03['repeat[equip_item_count]']], [(STICK, [EARTH, WIND])])
        self.assertEqual(self.server.world.map_of(a.session), 101)
        pkts = self.extract(a, SEL1, EARTH, (EARTH, WIND, 0, 0, 0, 0))
        self.assertEqual((_ops(pkts), a.s2c(pkts[0])['stone_id']), ([0x9C, 0x18], EARTH))
        self.assertEqual(self.bag().instance(STICK)['w'], [WIND, 0, 0, 0, 0, 0])
        for bad in (b'!socket 179', b'!socket 2945 2945', b'!socket 179 1 2 3 4 5 6', b'!socket 179 70',
                    b'!socket 179 2945 2975'):
            with self.subTest(bad=bad):
                self.line(a, bad)
                [text] = self.lines(a)
                self.assertIn('!socket <equip_id>', text)

    def test_route(self):
        route = self.server.routes[0x72]
        self.assertEqual((route.handler, route.style), ('_handle_stone_extract', registry.STYLE_REC))
        self.assertIn(0x72, registry.must_reply_table(self.build))           # the backstop stays


class Extraction2008(_Extraction, unittest.TestCase):
    build = B8


class Extraction2009(_Extraction, unittest.TestCase):
    build = B9


if __name__ == '__main__':
    unittest.main()
