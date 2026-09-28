#!/usr/bin/env python3
"""
test_trade.py - P7 stage 1 (player-to-player trade), offline, both client builds
===============================================================================
trade-request-accept / -offer / -lock-commit / -cancel-lifecycle / -escrow-guards /
-audit-log (docs/systems/trade.md 1-3; roadmap P7 exit criteria 1-3):

- pure rules (trade.py): the S2C 0x45..0x4D bytes of both builds, both C2S 0x22 send sites,
  the 0x25 echo comparison, the swap capacity simulation, the routes and the reply policy;
- two / three fake clients on one server (MultiClient; TestHero test/test uid 1 = client 1,
  Watcher admin/admin uid 2 = client 2, Carol uid 3), for 2008 and 2009:
  exit 1 - Wooden Stick + 100 gold for 2 potions: request 0x20 -> 0x45, accept 0x21 -> 0x46
  both, offers 0x22 -> 0x4B both, locks 0x23 -> 0x48 both, the first 0x25 waits, the second
  commits -> 0x4A {new gold} both, exactly once, persisted, and a relog of both shows the
  same bags and gold in its 0x03;
  exit 2 - cancel (0x24), disconnect, portal and death -> 0x49 to both, nothing moved;
  exit 3 - the offered stick cannot be sold (0x18 + 0x15), used, equipped, dropped or banked;
  locked gold cannot be spent;
  plus the refusals (0x47 4 / 6, 0x15, 0x4D), take-back 0x26 -> 0x4C, the echo mismatch,
  the commit re-validation, simultaneous confirms, the audit log and `!trade`.

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
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import fakeclient as F  # noqa: E402
import inventory as INV  # noqa: E402
import packets as P  # noqa: E402
import registry  # noqa: E402
import trade as TR  # noqa: E402

W = F.import_server()
cfgmod = W.cfgmod
EC = W.EC
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = cfgmod.BUILD_2008, cfgmod.BUILD_2009
DIRS = {B8: cfgmod.DEFAULTS['CLIENT_DIR'], B9: cfgmod.DEFAULTS['CLIENT_DIR_2009']}
HAVE = {b: os.path.exists(os.path.join(HERE, d, 'hs', 'windslayer.hii')) for b, d in DIRS.items()}
PORTAL_101_TO_102 = bytes.fromhex('17000000')   # live 0x7E capture: 101 line 23
STICK = 179                                     # Wooden Stick, Type 1 (Kind 11), both builds
POTION = 7                                      # Minor Healing Potion, Type 0
CLERIC_WAND = 248
CASH_EQUIP = 1848                               # a Type 1 item with the hii Cash flag
MISTY = 8                                       # map 101 merchant (sells POTION)

# The trade send sites of each build (spec_2009: same grammars, new addresses).
KEYS = {
    B8: {'request': '0x4484CC/0x20', 'accept': '0x469C33/0x21', 'add': '0x46C6B5/0x22',
         'add_stack': '0x469D9C/0x22', 'lock': '0x469D05/0x23', 'cancel': '0x469D9C/0x24',
         'confirm': '0x469BA8/0x25', 'remove': '0x46C92C/0x26', 'sell': '0x46A679/0x0C',
         'buy': '0x469D9C/0x0B', 'equip': '0x44C481/0x0F', 'drop': '0x469AC9/0x13',
         'use': '0x44C2B3/0x15', 'bank_item': '0x469D9C/0x3C', 'bank_gold': '0x469D9C/0x3E'},
    B9: {'request': '0x44A9D6/0x20', 'accept': '0x47394E/0x21', 'add': '0x476BF5/0x22',
         'add_stack': '0x473E14/0x22', 'lock': '0x473ABA/0x23', 'cancel': '0x473B5B/0x24',
         'confirm': '0x473E14/0x25', 'remove': '0x476E62/0x26', 'sell': '0x4747C9/0x0C',
         'buy': '0x4745A4/0x0B', 'equip': '0x4500D1/0x0F', 'drop': '0x4737D9/0x13',
         'use': '0x44FE04/0x15', 'bank_item': '0x473E14/0x3C', 'bank_gold': '0x4751F0/0x3E'},
}

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
            'accounts.json changed during test_trade.py (tests must only use temp copies)'


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _ops(pkts):
    return [p.opcode for p in pkts]


def _char(name):
    return {'name': name, 'level': 1, 'class': 0, 'map': 101, 'x': 1411.0, 'y': 714.0, 'hp': 100, 'mp': 50}


def row(item, qty, opts=(), extra=0):
    """One 0x25 list row as the client's window 0x291 lists an entry."""
    return {'item_id': item, 'amount': qty, 'opt_count': len(opts),
            'repeat[opt_count]': [{'opt': w} for w in opts], 'attr_0c': extra}


# ================================================================ pure rules ===
class Rules(unittest.TestCase):
    def test_packet_bytes_of_both_builds(self):
        """trade.md 1.3: the S2C grammars (spec_2009 "identical")."""
        for build in (B8, B9):
            with self.subTest(build=build):
                self.assertEqual(P.build('0x45', {'requester_name': b'TestHero'}, client_build=build),
                                 P.name17('TestHero'))
                self.assertEqual(P.build('0x46', {'partner_name': b'Watcher'}, client_build=build),
                                 P.name17('Watcher'))
                self.assertEqual(P.build('0x47', {'result': 6}, client_build=build), b'\x06')
                self.assertEqual(P.build('0x48', {'confirmer_uid': 1, 'gold': 100}, client_build=build).hex(),
                                 '01000000' + '6400000000000000')
                self.assertEqual(P.build('0x49', {}, client_build=build), b'')
                self.assertEqual(P.build('0x4A', {'gold': 900}, client_build=build).hex(), '8403000000000000')
                stick = TR.Offer(STICK, 1, True, [0] * 6, [], 0)
                self.assertEqual(P.build('0x4B', stick.fields(1), client_build=build).hex(),
                                 'b300' + '0100' + '01000000' + '00' + '0000')   # 11 B
                socketed = TR.Offer(STICK, 1, True, [5, 7, 0, 0, 0, 3], [5, 7], 3)
                self.assertEqual(len(P.build('0x4B', socketed.fields(2), client_build=build)), 15)
                self.assertEqual(P.build('0x4C', {'slot_index': 1}, client_build=build), b'\x01')
                self.assertEqual(P.build('0x4D', {}, client_build=build), b'')

    def test_both_0x22_send_sites_give_one_descriptor(self):
        for build in (B8, B9):
            k = KEYS[build]
            with self.subTest(build=build):
                # the 7-byte form decodes as both sites (the equipment drag with no option)
                rec = P.parse(0x22, P.build(k['add'], {'item_id': STICK, 'quantity': 1, 'enchant_count': 0},
                                            direction='C2S', client_build=build), client_build=build)
                self.assertEqual(TR.descriptor(rec), (STICK, 1, 0, [], 0))
                rec = P.parse(0x22, P.build(k['add'], {'item_id': STICK, 'quantity': 1, 'enchant_count': 2,
                                                       'repeat[enchant_count]': [{'enchant': 5}, {'enchant': 7}],
                                                       'enchant_last': 3}, direction='C2S', client_build=build),
                              client_build=build)
                self.assertEqual(TR.descriptor(rec), (STICK, 1, 2, [5, 7], 3))
                rec = P.parse(0x22, P.build(k['add_stack'], {'item_id': POTION, 'qty': 2}, direction='C2S',
                                            client_build=build), client_build=build)
                self.assertEqual(TR.descriptor(rec), (POTION, 2, 0, [], 0))
                rec = P.parse(0x26, P.build(k['remove'], {'item_id': POTION, 'quantity': 2, 'trade_slot_index': 1},
                                            direction='C2S', client_build=build), client_build=build)
                self.assertEqual(TR.descriptor(rec), (POTION, 2, 0, [], 0))
                self.assertEqual(rec['trade_slot_index'], 1)

    def test_echo_comparison(self):
        offer = [TR.Offer(STICK, 1, True, INV.block_from_wire([5], 3), [5], 3),
                 TR.Offer(POTION, 2, False, [0] * 6, [], 0)]
        self.assertIsNone(TR.echo_mismatch(offer, [row(STICK, 1, [5], 3), row(POTION, 2)]))
        # a stack's block is not compared (the quantity dialog always sends 0 / 0)
        self.assertIsNone(TR.echo_mismatch(offer, [row(STICK, 1, [5], 3), row(POTION, 2, [9], 1)]))
        self.assertIn('block', TR.echo_mismatch(offer, [row(STICK, 1, [6], 3), row(POTION, 2)]))
        self.assertIn('entry 1', TR.echo_mismatch(offer, [row(STICK, 1, [5], 3), row(POTION, 3)]))
        self.assertIn('entries', TR.echo_mismatch(offer, [row(STICK, 1, [5], 3)]))
        self.assertIn('entry 0', TR.echo_mismatch(offer, [row(POTION, 2), row(STICK, 1, [5], 3)]))

    def test_the_swap_simulation_frees_before_it_fills(self):
        """trade.md 3.6 can_receive: the gives leave the bag first, so two full tabs swap."""
        char = {'inventory': {'equip': [{'id': CLERIC_WAND, 'w': [0] * 6}], 'consume': {}, 'etc': {},
                              'tab_slots': [1, 35, 35]}, 'equipped': {}}
        stick = TR.Offer(STICK, 1, True, [0] * 6, [], 0)
        wand = TR.Offer(CLERIC_WAND, 1, True, [0] * 6, [], 0)
        self.assertIn('full', TR.simulate(char, [], [stick]))
        self.assertIsNone(TR.simulate(char, [wand], [stick]))
        self.assertIn('no longer in the bag', TR.simulate(char, [stick], []))
        self.assertEqual(char['inventory']['equip'], [{'id': CLERIC_WAND, 'w': [0] * 6}])   # a snapshot

    def test_routes_and_policy(self):
        want = {0x20: '_handle_trade_request', 0x21: '_handle_trade_accept', 0x22: '_handle_trade_add',
                0x23: '_handle_trade_lock', 0x24: '_handle_trade_cancel', 0x25: '_handle_trade_confirm',
                0x26: '_handle_trade_remove'}
        for routes in (W.GameServer.ROUTES, W.GameServer.ROUTES_2009):
            self.assertEqual({op: routes[op].handler for op in want}, want)
            self.assertEqual(registry.check_routes(routes, W.GameServer), [])
        for build in (B8, B9):
            must = registry.must_reply_table(build)
            self.assertEqual({op for op in want if op in must}, {0x24, 0x25})
            self.assertFalse(set(want) & set(registry.never_reply_table(build)))
            # the backstop: 0x49 to the sender (a bare session: nothing to cancel)
            self.assertEqual(must[0x25].refusal(None, {'username': 'test'}, None), [('0x49', {}, None)])
        self.assertEqual(W.GameServer.DEV_COMMANDS['trade'].handler, '_dev_trade')


# ============================================================== two players ===
class _Base:
    """TestHero (uid 1: a Wooden Stick, 1000 gold) and Watcher (uid 2: 5 Minor Healing
    Potions, 500 gold) in world on map 101 of one server of `build`; Carol (uid 3) exists and
    is offline until a test logs her in."""
    build = B8

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        self.tmp = tempfile.mkdtemp(prefix=f'ws_trade_{self.build}_')
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False})
        accounts = F.two_player_accounts()
        accounts['carol'] = {'password': 'carol', 'characters': [_char('Carol')]}
        self.server = F.make_server(self.tmp, accounts=accounts, config=cfg)
        self.k = KEYS[self.build]
        self.stock('TestHero', gold=1000, items=[(STICK, 1)])
        self.stock('Watcher', gold=500, items=[(POTION, 5)])
        self.stock('Carol', gold=300, items=[])
        self.prepare()
        self.extra = []
        self.mc = F.MultiClient(self.server, refuse=self.refuse())
        self.a, self.b = self.mc
        self.mc.drain()

    def prepare(self):
        """Store edits before anyone logs in (subclass hook)."""

    def refuse(self):
        return None

    def tearDown(self):
        for c in self.extra:
            try:
                c.close()
            except OSError:
                pass
        self.mc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ----------------------------------------------------------------- helpers ---
    def rec(self, name):
        found = self.server.store.character_by_name(name)
        return found[2] if found else None

    def stock(self, name, gold, items, caps=None):
        char = self.rec(name)
        INV.ensure(char)
        char['inventory'].update({'equip': [], 'consume': {}, 'etc': {}})
        if caps:
            char['inventory']['tab_slots'] = list(caps)
        bag = INV.Inventory(char)
        for item, qty in items:
            self.assertIsNotNone(bag.add(item, qty))
        char['gold'] = gold

    def bag(self, name):
        return INV.Inventory(self.rec(name)).totals()

    def gold(self, name):
        return self.rec(name)['gold']

    def disk(self, name):
        with open(self.server.db_file, encoding='utf-8') as f:
            for acc in json.load(f).values():
                for ch in acc.get('characters', []):
                    if ch.get('name') == name:
                        return ch
        return None

    def send(self, c, what, fields=None):
        return c.send_c2s(self.k[what], fields or {})

    def uid(self, c):
        return c.session['uid']

    def notice(self, pkt, c):
        return P.to_bytes(c.s2c(pkt)['text']).decode('cp949')

    def open_trade(self, a=None, b=None):
        """a requests b (0x20 -> 0x45 on b), b accepts (0x21 -> 0x46 on both)."""
        a, b = a or self.a, b or self.b
        self.send(a, 'request', {'target_uid': self.uid(b)})
        self.assertEqual(b.expect(0x45).payload, P.name17(a.char_name))
        self.send(b, 'accept', {'requester_name': a.char_name})
        self.assertEqual(a.expect(0x46).payload, P.name17(b.char_name))
        self.assertEqual(b.expect(0x46).payload, P.name17(a.char_name))
        return self.server.trade.trade_of(a.session)

    def offer_stick(self, c=None, opts=(), extra=0):
        c = c or self.a
        self.send(c, 'add', {'item_id': STICK, 'quantity': 1, 'enchant_count': len(opts),
                             'repeat[enchant_count]': [{'enchant': w} for w in opts], 'enchant_last': extra})

    def offer_stack(self, c, item, qty):
        self.send(c, 'add_stack', {'item_id': item, 'qty': qty})

    def added(self, c, item, count, owner):
        got = c.s2c(c.expect(0x4B))
        self.assertEqual((got['item_id'], got['count'], got['owner_uid']), (item, count, owner))
        return got

    def both_added(self, item, count, owner):
        self.added(self.a, item, count, owner)
        self.added(self.b, item, count, owner)

    def lock(self, c, gold):
        self.send(c, 'lock', {'gold': gold})

    def locked(self, uid, gold):
        for c in (self.a, self.b):
            self.assertEqual(c.s2c(c.expect(0x48)), {'confirmer_uid': uid, 'gold': gold})

    def confirm(self, c, my_gold, mine, partner_gold, theirs):
        self.send(c, 'confirm', {'my_gold': my_gold, 'my_item_count': len(mine), 'repeat[my_item_count]': mine,
                                 'partner_gold': partner_gold, 'partner_item_count': len(theirs),
                                 'repeat[partner_item_count]': theirs})

    def stick_for_potions(self):
        """Exit criterion 1 up to both locks: stick + 100 gold (A) for 2 potions (B)."""
        self.open_trade()
        self.offer_stick()
        self.both_added(STICK, 1, 1)
        self.offer_stack(self.b, POTION, 2)
        self.both_added(POTION, 2, 2)
        self.lock(self.a, 100)
        self.locked(1, 100)
        self.lock(self.b, 0)
        self.locked(2, 0)
        return self.server.trade.trade_of(self.a.session)

    def audit_lines(self):
        path = os.path.join(self.tmp, TR.LOG_NAME)
        if not os.path.exists(path):
            return []
        with open(path, encoding='utf-8') as f:
            return [json.loads(line) for line in f if line.strip()]

    def relog(self, account, password, name, number):
        c = F.FakeClient(self.server)
        self.extra.append(c)
        self.assertEqual(c.login(account, password)['result'], 1)
        c.char_name = name
        pkts = c.enter_world(name, port=F.P2P_PORT_BASE + number - 1)
        return c, pkts

    def carol(self):
        c, _ = self.relog('carol', 'carol', 'Carol', 3)
        self.mc.drain()
        return c


class _Trade(_Base):
    # ------------------------------------------------------------- exit 1 ---
    def test_exit1_stick_and_gold_for_potions_swap_exactly_once_and_persist(self):
        """Exit criterion 1: both lock and confirm; the first confirm waits in silence, the
        second commits: 0x4A {new absolute gold} to each and nothing else (the clients move
        the items themselves); the store holds the swap on disk before the 0x4A; a repeat
        confirm moves nothing; after a relog both 0x03s carry the new bags and gold."""
        a, b = self.a, self.b
        t = self.stick_for_potions()
        self.assertEqual(t.state, TR.CONFIRMING)
        self.confirm(a, 100, [row(STICK, 1)], 0, [row(POTION, 2)])
        a.expect_silence(0.2)                           # the first confirm waits (no 0x49!)
        b.expect_silence(0.05)
        self.assertEqual((self.bag('TestHero'), self.gold('TestHero')), ({STICK: 1}, 1000))
        self.confirm(b, 0, [row(POTION, 2)], 100, [row(STICK, 1)])
        self.assertEqual(a.s2c(a.expect(0x4A)), {'gold': 900})
        self.assertEqual(b.s2c(b.expect(0x4A)), {'gold': 600})
        self.assertEqual((self.bag('TestHero'), self.gold('TestHero')), ({POTION: 2}, 900))
        self.assertEqual((self.bag('Watcher'), self.gold('Watcher')), ({STICK: 1, POTION: 3}, 600))
        # persisted before the 0x4A (one atomic accounts.json write)
        self.assertEqual((self.disk('TestHero')['gold'], self.disk('Watcher')['gold']), (900, 600))
        self.assertEqual(self.disk('Watcher')['inventory']['equip'], [{'id': STICK, 'w': [0] * 6}])
        self.assertIsNone(self.server.trade.trade_of(a.session))
        self.assertIsNone(self.server.trade.trade_of(b.session))
        self.assertEqual(self.server.trade.trades, {})
        # exactly once: a repeated confirm finds no trade (a stale window gets its 0x49)
        self.confirm(b, 0, [row(POTION, 2)], 100, [row(STICK, 1)])
        b.expect(0x49)
        self.assertEqual((self.gold('TestHero'), self.gold('Watcher')), (900, 600))
        self.assertEqual(self.bag('Watcher'), {STICK: 1, POTION: 3})
        # trade-audit-log: open + commit with before / after
        events = self.audit_lines()
        self.assertEqual([e['event'] for e in events], ['open', 'commit'])
        commit = events[1]
        self.assertEqual((commit['a']['uid'], commit['b']['uid'], commit['a']['gold']), (1, 2, 100))
        self.assertEqual(commit['a']['offer'], [{'id': STICK, 'qty': 1, 'w': [0] * 6}])
        self.assertEqual(commit['before']['TestHero'], {'gold': 1000, 'items': {str(STICK): 1}})
        self.assertEqual(commit['after']['Watcher'], {'gold': 600, 'items': {str(POTION): 3, str(STICK): 1}})
        # relog both: the 0x03 lists and gold are the store's
        self.mc.close()
        self.assertTrue(_wait(lambda: not self.server.world.by_uid))
        for account, pw, name, n, gold, equip, consume in (
                ('test', 'test', 'TestHero', 1, 900, [], [(POTION, 2)]),
                ('admin', 'admin', 'Watcher', 2, 600, [STICK], [(POTION, 3)])):
            c, pkts = self.relog(account, pw, name, n)
            got = c.s2c(next(p for p in pkts if p.opcode == 0x03), allow_trailing=True)
            self.assertEqual(got['gold'], gold)
            self.assertEqual([r['item_id'] for r in got['repeat[equip_item_count]']], equip)
            self.assertEqual([(r['item_id'], r['quantity']) for r in got['repeat[consume_item_count]']], consume)

    def test_simultaneous_confirms_commit_once(self):
        """Both 0x25 arrive together: the two handler threads serialize on world_lock and
        the combat locks; one waits, the other commits - one 0x4A each, no 0x49."""
        a, b = self.a, self.b
        self.stick_for_potions()
        t1 = threading.Thread(target=self.confirm, args=(a, 100, [row(STICK, 1)], 0, [row(POTION, 2)]))
        t1.start()
        self.confirm(b, 0, [row(POTION, 2)], 100, [row(STICK, 1)])
        t1.join()
        self.assertEqual(a.s2c(a.expect(0x4A)), {'gold': 900})
        self.assertEqual(b.s2c(b.expect(0x4A)), {'gold': 600})
        self.assertEqual((self.bag('TestHero'), self.bag('Watcher')), ({POTION: 2}, {STICK: 1, POTION: 3}))
        self.assertEqual([e['event'] for e in self.audit_lines()].count('commit'), 1)

    def test_socketed_equipment_moves_with_its_block(self):
        """The 0x4B echoes the client's option words (its self path compares 12 bytes) and
        the commit moves the stored instance, block and all."""
        self.stock('TestHero', gold=1000, items=[])
        INV.Inventory(self.rec('TestHero')).add(STICK, 1, [5, 7, 0, 0, 0, 3])
        self.open_trade()
        self.offer_stick(opts=[5, 7], extra=3)
        got = self.added(self.a, STICK, 1, 1)
        self.added(self.b, STICK, 1, 1)
        self.assertEqual(([e['opt'] for e in got['repeat[opt_count]']], got['opt_extra']), ([5, 7], 3))
        self.lock(self.a, 0)
        self.locked(1, 0)
        self.lock(self.b, 0)
        self.locked(2, 0)
        self.confirm(self.a, 0, [row(STICK, 1, [5, 7], 3)], 0, [])
        self.confirm(self.b, 0, [], 0, [row(STICK, 1, [5, 7], 3)])
        self.a.expect(0x4A)
        self.b.expect(0x4A)
        self.assertEqual(self.rec('Watcher')['inventory']['equip'], [{'id': STICK, 'w': [5, 7, 0, 0, 0, 3]}])

    # ------------------------------------------------------------- exit 2 ---
    def test_exit2_cancel_returns_the_escrow_to_both(self):
        """0x24 from either side -> 0x49 to both; the records never changed (escrow)."""
        a, b = self.a, self.b
        self.open_trade()
        self.offer_stick()
        self.both_added(STICK, 1, 1)
        self.send(a, 'cancel')
        a.expect(0x49)
        b.expect(0x49)
        self.assertEqual((self.bag('TestHero'), self.gold('TestHero')), ({STICK: 1}, 1000))
        self.assertEqual(self.bag('Watcher'), {POTION: 5})
        self.assertIsNone(self.server.trade.trade_of(b.session))
        self.assertEqual(self.audit_lines()[-1]['reason'], 'canceled by TestHero')
        # a stale window still closes: 0x24 with no trade -> 0x49 to the sender only
        self.send(b, 'cancel')
        b.expect(0x49)
        a.expect_silence(0.1)
        # at CONFIRMING too, from the other side
        self.stick_for_potions()
        self.send(b, 'cancel')
        a.expect(0x49)
        b.expect(0x49)
        self.assertEqual(self.bag('TestHero'), {STICK: 1})

    def test_exit2_disconnect_cancels_for_the_partner(self):
        a, b = self.a, self.b
        self.open_trade()
        self.offer_stick()
        self.both_added(STICK, 1, 1)
        a.close()
        a.thread.join(timeout=5.0)
        self.assertEqual(sorted(_ops(b.recv_until_quiet(0.3))), [0x06, 0x49])
        self.assertIsNone(self.server.trade.trade_of(b.session))
        self.assertEqual(self.bag('TestHero'), {STICK: 1})
        self.server.store.flush()                       # the disconnect save (debounced)
        self.assertEqual(self.disk('TestHero')['inventory']['equip'], [{'id': STICK, 'w': [0] * 6}])

    def test_exit2_portal_cancels_before_the_lead(self):
        """The 0x49 reaches the mover BEFORE the 0x08 (the partner's entity and window 0x4E
        die with it) and the partner gets its 0x49 before the mover's 0x06."""
        a, b = self.a, self.b
        self.open_trade()
        self.offer_stick()
        self.both_added(STICK, 1, 1)
        a.send(0x7E, PORTAL_101_TO_102)
        self.assertTrue(_wait(lambda: self.server.world.map_of(a.session) == 102))
        ops = _ops(a.recv_until_quiet(0.3))
        self.assertEqual(ops[:2], [0x49, 0x08])
        self.assertEqual(_ops(b.recv_until_quiet(0.3)), [0x49, 0x06])
        self.assertIsNone(self.server.trade.trade_of(a.session))
        self.assertEqual(self.bag('TestHero'), {STICK: 1})

    def test_exit2_death_cancels(self):
        a, b = self.a, self.b
        self.open_trade()
        self.offer_stick()
        self.both_added(STICK, 1, 1)
        with self.server._combat_lock(a.session):
            self.server._player_death(a.session, 'test')
        self.assertIn(0x49, _ops(a.recv_until_quiet(0.3)))
        self.assertIn(0x49, _ops(b.recv_until_quiet(0.3)))
        self.assertIsNone(self.server.trade.trade_of(b.session))
        self.assertEqual(self.bag('TestHero'), {STICK: 1})

    # ------------------------------------------------------------- exit 3 ---
    def test_exit3_offered_goods_cannot_leave_the_bag(self):
        """trade-escrow-guards: with the stick on offer it cannot be sold (0x18 resync +
        0x15), equipped, dropped or banked; an offered potion cannot be used, the rest can;
        locked gold cannot be spent. The trade still commits afterwards."""
        a, b = self.a, self.b
        self.open_trade()
        self.offer_stick()
        self.both_added(STICK, 1, 1)
        self.send(a, 'sell', {'item_id': STICK, 'qty': 1})
        resync, line = a.expect(0x18, 0x15)
        self.assertEqual(a.s2c(resync)['item_id'], 0)
        self.assertIn(TR.IN_TRADE_TEXT, self.notice(line, a))
        self.send(a, 'equip', {'item_id': STICK})
        self.assertIn(TR.IN_TRADE_TEXT, self.notice(a.expect(0x15), a))
        self.send(a, 'drop', {'item_id': STICK, 'amount': 1})
        self.assertIn(TR.IN_TRADE_TEXT, self.notice(a.expect(0x15), a))
        self.send(a, 'bank_item', {'item_id': STICK, 'qty': 1})
        bank, line = a.expect(0x65, 0x15)
        self.assertIn(TR.IN_TRADE_TEXT, self.notice(line, a))
        self.assertEqual(self.bag('TestHero'), {STICK: 1})
        self.assertEqual(self.rec('TestHero')['equipped'], {})
        # B offers 2 of its 5 potions: 3 stay usable, not a 4th
        self.offer_stack(b, POTION, 2)
        self.both_added(POTION, 2, 2)
        for _ in range(3):
            self.send(b, 'use', {'item_id': POTION})
            b.expect(0x25)
            b.session.get('use_cd', {}).clear()           # the server cooldown is not the point
        self.send(b, 'use', {'item_id': POTION})
        self.assertIn(TR.IN_TRADE_TEXT, self.notice(b.expect(0x15), b))
        self.assertEqual(self.bag('Watcher'), {POTION: 2})
        self.assertEqual(_ops(a.recv_until_quiet(0.2)), [0x40] * 3)   # the heal visuals of B's uses
        # locked gold: A locks 950 of 1000 - a 100-gold potion and a 100-gold deposit are refused
        self.lock(a, 950)
        self.locked(1, 950)
        self.send(a, 'buy', {'item_id': POTION, 'qty': 1, 'npc_id': MISTY})
        resync, line = a.expect(0x18, 0x15)
        self.assertIn(TR.GOLD_IN_TRADE_TEXT, self.notice(line, a))
        self.send(a, 'bank_gold', {'gold': 100})
        moved, line = a.expect(0x68, 0x15)
        self.assertEqual(a.s2c(moved)['amount'], 0)
        self.assertIn(TR.GOLD_IN_TRADE_TEXT, self.notice(line, a))
        self.assertEqual(self.gold('TestHero'), 1000)
        # the trade is intact and commits
        self.lock(b, 0)
        self.locked(2, 0)
        self.confirm(a, 950, [row(STICK, 1)], 0, [row(POTION, 2)])
        self.confirm(b, 0, [row(POTION, 2)], 950, [row(STICK, 1)])
        self.assertEqual(a.s2c(a.expect(0x4A)), {'gold': 50})
        self.assertEqual(b.s2c(b.expect(0x4A)), {'gold': 1450})
        self.assertEqual((self.bag('TestHero'), self.bag('Watcher')), ({POTION: 2}, {STICK: 1}))
        # no trade: the same sell goes through (Sell 0)
        self.send(b, 'sell', {'item_id': STICK, 'qty': 1})
        self.assertEqual(b.s2c(b.expect(0x19))['item_id'], STICK)

    # ---------------------------------------------------------- refusals ---
    def test_request_refusals(self):
        a, b = self.a, self.b
        # not on this map / oneself / unknown uid -> the client's own "same field" line
        for uid in (99, self.uid(a)):
            self.send(a, 'request', {'target_uid': uid})
            self.assertEqual(self.notice(a.expect(0x15), a).split('] ')[-1], TR.NOT_HERE_TEXT)
        # a repeat inside SPAM_SECS is ignored; a trading target is busy for a third player
        self.send(a, 'request', {'target_uid': 2})
        b.expect(0x45)
        self.send(a, 'request', {'target_uid': 2})
        b.expect_silence(0.15)
        carol = self.carol()
        self.send(carol, 'request', {'target_uid': 2})       # B holds A's prompt
        self.assertEqual(carol.s2c(carol.expect(0x47)), {'result': TR.RESULT_BUSY})
        self.send(b, 'accept', {'requester_name': 'TestHero'})
        a.expect(0x46)
        b.expect(0x46)
        self.send(carol, 'request', {'target_uid': 1})       # A is trading
        self.assertEqual(carol.s2c(carol.expect(0x47)), {'result': TR.RESULT_BUSY})
        self.send(a, 'request', {'target_uid': 3})           # the requester is trading
        self.assertEqual(a.s2c(a.expect(0x47)), {'result': TR.RESULT_BUSY})
        carol.expect_silence(0.05)
        # an accept with no prompt, or the wrong name -> 0x15
        self.send(carol, 'accept', {'requester_name': 'Watcher'})
        self.assertIn(TR.EXPIRED_TEXT, self.notice(carol.expect(0x15), carol))

    def test_expired_prompt_opens_nothing(self):
        self.send(self.a, 'request', {'target_uid': 2})
        self.b.expect(0x45)
        inv = self.server.trade.invites[2]
        inv.t -= TR.INVITE_TTL + 1
        self.send(self.b, 'accept', {'requester_name': 'TestHero'})
        self.assertIn(TR.EXPIRED_TEXT, self.notice(self.b.expect(0x15), self.b))
        self.a.expect_silence(0.1)
        self.assertEqual(self.server.trade.trades, {})

    def test_offer_refusals_and_the_capacity_check(self):
        a, b = self.a, self.b
        self.stock('Watcher', gold=500, items=[(CLERIC_WAND, 1)], caps=[1, 35, 35])
        INV.Inventory(self.rec('TestHero')).add(CASH_EQUIP, 1)
        self.open_trade()
        # more than owned, a cash item, a bad quantity: nothing moves, a 0x15 at most
        self.offer_stack(a, POTION, 1)
        self.assertIn(TR.NOT_OWNED_TEXT, self.notice(a.expect(0x15), a))
        self.send(a, 'add', {'item_id': CASH_EQUIP, 'quantity': 1})
        self.assertIn(TR.NO_TRADE_ITEM_TEXT, self.notice(a.expect(0x15), a))
        self.send(a, 'add', {'item_id': STICK, 'quantity': 2})
        a.expect_silence(0.15)
        b.expect_silence(0.05)
        # B's one equip slot is full -> 0x4D to A only
        self.offer_stick()
        a.expect(0x4D)
        b.expect_silence(0.1)
        # B offers its wand: the slot it frees takes the stick (gives before takes)
        self.send(b, 'add', {'item_id': CLERIC_WAND, 'quantity': 1})
        self.both_added(CLERIC_WAND, 1, 2)
        self.offer_stick()
        self.both_added(STICK, 1, 1)
        # the same stick twice: only one is owned
        self.offer_stick()
        self.assertIn(TR.NOT_OWNED_TEXT, self.notice(a.expect(0x15), a))
        # after a lock nothing more is offered (Q4) and a take-back cancels
        self.lock(b, 0)
        self.locked(2, 0)
        self.offer_stack(b, POTION, 1)
        self.assertIn(TR.LOCKED_TEXT, self.notice(b.expect(0x15), b))
        self.send(a, 'remove', {'item_id': STICK, 'quantity': 1, 'trade_slot_index': 0})
        self.assertEqual(_ops(a.recv_until_quiet(0.3)), [0x49, 0x15])
        b.expect(0x49)

    def test_take_back_tells_only_the_partner(self):
        """0x26: the client already restored its own grid; the partner gets 0x4C {index}."""
        a, b = self.a, self.b
        self.open_trade()
        self.offer_stack(b, POTION, 1)
        self.both_added(POTION, 1, 2)
        self.offer_stack(b, POTION, 2)
        self.both_added(POTION, 2, 2)
        # a stale index falls back to the content match: entry 1 (x2) is found at index 1
        self.send(b, 'remove', {'item_id': POTION, 'quantity': 2, 'trade_slot_index': 5})
        self.assertEqual(a.s2c(a.expect(0x4C)), {'slot_index': 1})
        b.expect_silence(0.1)
        self.send(b, 'remove', {'item_id': POTION, 'quantity': 1, 'trade_slot_index': 0})
        self.assertEqual(a.s2c(a.expect(0x4C)), {'slot_index': 0})
        self.send(b, 'remove', {'item_id': POTION, 'quantity': 1, 'trade_slot_index': 0})
        a.expect_silence(0.15)                          # not on offer: logged, ignored
        self.assertEqual(self.server.trade.trade_of(b.session).b.offer, [])

    def test_lock_with_more_gold_than_held_cancels(self):
        a, b = self.a, self.b
        self.open_trade()
        self.lock(a, 1001)
        self.assertEqual(_ops(a.recv_until_quiet(0.3)), [0x49, 0x15])
        b.expect(0x49)
        self.assertEqual(self.server.trade.trades, {})

    def test_echo_mismatch_cancels(self):
        """The client's 0x25 must describe the server's deal; a different gold or list is a
        desync (or a forged packet) and cancels, nothing moved."""
        a, b = self.a, self.b
        self.stick_for_potions()
        self.confirm(a, 100, [row(STICK, 1)], 0, [row(POTION, 3)])
        self.assertEqual(_ops(a.recv_until_quiet(0.3)), [0x49, 0x15])
        b.expect(0x49)
        self.stick_for_potions()
        self.confirm(b, 0, [row(POTION, 2)], 99, [row(STICK, 1)])
        self.assertEqual(_ops(b.recv_until_quiet(0.3)), [0x49, 0x15])
        a.expect(0x49)
        self.assertEqual((self.bag('TestHero'), self.gold('Watcher')), ({STICK: 1}, 500))

    def test_commit_revalidates_ownership(self):
        """Goods that left the record between the lock and the confirm (here: removed behind
        the server's back) make the commit refuse - 0x49 both, nothing half-applied."""
        a, b = self.a, self.b
        self.stick_for_potions()
        INV.Inventory(self.rec('TestHero')).remove(STICK, 1)
        self.confirm(a, 100, [row(STICK, 1)], 0, [row(POTION, 2)])
        a.expect_silence(0.15)
        self.confirm(b, 0, [row(POTION, 2)], 100, [row(STICK, 1)])
        self.assertEqual(_ops(b.recv_until_quiet(0.3)), [0x49, 0x15])
        a.expect(0x49)
        self.assertEqual((self.bag('Watcher'), self.gold('Watcher'), self.gold('TestHero')), ({POTION: 5}, 500, 1000))
        self.assertEqual(self.audit_lines()[-1]['event'], 'cancel')

    def test_refusing_trades_gets_0x47_4(self):
        self.mc.close()
        self.assertTrue(_wait(lambda: not self.server.world.by_uid))
        self.mc = F.MultiClient(self.server, refuse={'Watcher': {'exchange': True}})
        self.a, self.b = self.mc
        self.mc.drain()
        self.send(self.a, 'request', {'target_uid': 2})
        self.assertEqual(self.a.s2c(self.a.expect(0x47)), {'result': TR.RESULT_REFUSING})
        self.b.expect_silence(0.1)

    def test_dev_trade_command(self):
        """`!trade` drives the same paths (the live helper `wsdev dev <char> '!trade ...'`)."""
        a, b = self.a, self.b
        srv = self.server
        srv._dev_trade(a.session, 'request Watcher')
        b.expect(0x45)
        a.expect(0x15)
        srv._dev_trade(b.session, 'accept TestHero')
        self.assertEqual(_ops(b.recv_until_quiet(0.3)), [0x46, 0x15])
        a.expect(0x46)
        srv._dev_trade(a.session, 'offer 179')
        self.assertEqual(_ops(a.recv_until_quiet(0.3)), [0x4B, 0x15])
        b.expect(0x4B)
        srv._dev_trade(a.session, 'lock 10')
        srv._dev_trade(b.session, 'lock 0')
        srv._dev_trade(a.session, 'all')
        lines = [self.notice(p, a) for p in a.recv_until_quiet(0.3) if p.opcode == 0x15]
        self.assertTrue(any('1 live trade(s)' in line for line in lines), lines)
        b.recv_until_quiet(0.2)
        srv._dev_trade(a.session, 'confirm')
        srv._dev_trade(b.session, 'confirm')
        self.assertIn(0x4A, _ops(a.recv_until_quiet(0.3)))
        self.assertIn(0x4A, _ops(b.recv_until_quiet(0.3)))
        self.assertEqual((self.bag('Watcher'), self.gold('Watcher')), ({STICK: 1, POTION: 5}, 510))


class Trade2008(_Trade, unittest.TestCase):
    build = B8


class Trade2009(_Trade, unittest.TestCase):
    build = B9


if __name__ == '__main__':
    unittest.main()
