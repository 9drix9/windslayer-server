#!/usr/bin/env python3
"""
test_stall.py - P7 stage 2 (personal stalls on the flea market 9701), offline, both builds
=========================================================================================
shop_storage-stall-registry / -stall-open-close / -stall-browse-buy / -stall-presence
(docs/systems/shop_storage.md 1.6, F9-F14; roadmap P7 exit criterion 4):

- pure rules (stall.py): the S2C 0x82..0x89 bytes of both builds, the 0x5E list checks, the
  ownership count, the partial return into a full bag, the persisted escrow normalizer and
  its store migration (one-time accounts.json.bak-pre-p7), the routes and the reply policy;
- two / three fake clients on one server (MultiClient; TestHero test/test uid 1 = client 1,
  the seller; Watcher admin/admin uid 2 = client 2, the buyer; Carol uid 3 arrives later),
  all on map 9701, for 2008 and 2009:
  exit 4 - A opens a stall with 2 items and a title (0x82 {1}, items out of the bag into the
  persisted escrow), B gets the 0x85 sign with the title, browses (0x61 -> 0x87), buys 1
  (0x88 {1} to B, 0x89 to A: gold moves both ways exactly, the item moves, A's list and the
  escrow shrink, one accounts.json write), A closes (0x84 {1}, unsold items back in the bag,
  0x86 to B); a player entering later gets the seller's record then its 0x85;
  plus stop-for-edit, the map-load close before the lead, disconnect, the crash-left escrow
  merged at login, self-buy / price / quantity / block / gold / room refusals (0x88 {0} and a
  fresh 0x87 to that buyer only), two buyers racing for the last unit, trade-locked gold;
  review round 1: rows a full bag kept survive the next Start (back in the bag or refused
  with 0x82 {2}), a Stop that crossed our 0x83 / 0x84 is ignored, no trade while selling.

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
import market as MK  # noqa: E402
import packets as P  # noqa: E402
import presence  # noqa: E402
import registry  # noqa: E402
import skills as SK  # noqa: E402
import stall as ST  # noqa: E402
import store as storemod  # noqa: E402
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
STICK = 179                     # Wooden Stick, Type 1 (Kind 11), both builds
POTION = 7                      # Minor Healing Potion, Type 0
HERB = 5                        # Type 0
CASH_EQUIP = 1848               # a Type 1 item with the hii Cash flag
FLEA, TOWN = 9701, 101
FLEA_ARRIVAL = (1500.0, 2168.0)
TITLE = b'Shopping...'

# The stall send sites of each build (spec_2009: same grammars, new addresses).
KEYS = {
    B8: {'open': '0x46B635/0x5E', 'stop': '0x469BA8/0x5F', 'close': '0x469BA8/0x60',
         'visit': '0x44CA2F/0x61', 'buy': '0x469D9C/0x62', 'portal': '0x42F76B/0x7E'},
    B9: {'open': '0x475A25/0x5E', 'stop': '0x473E14/0x5F', 'close': '0x473E14/0x60',
         'visit': '0x4507FC/0x61', 'buy': '0x473E14/0x62', 'portal': '0x431284/0x7E'},
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
            'accounts.json changed during test_stall.py (tests must only use temp copies)'


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _ops(pkts):
    return [p.opcode for p in pkts]


def _char(name, map_code=FLEA):
    return {'name': name, 'level': 10, 'class': 0, 'map': map_code, 'x': FLEA_ARRIVAL[0],
            'y': FLEA_ARRIVAL[1], 'hp': 100, 'mp': 50}


def item(item_id, qty, price, opts=(), extra=0):
    """One C2S 0x5E list node as the client sends it."""
    return {'item_id': item_id, 'qty': qty, 'unit_price': price, 'socket_count': len(opts),
            'repeat[socket_count]': [{'socket_stone_id': w} for w in opts], 'item_extra': extra}


# ================================================================ pure rules ===
@unittest.skipUnless(HAVE[B8], 'needs the EN 2008 client data')
class Rules(unittest.TestCase):
    def setUp(self):
        EC.configure(DIRS[B8], B8)

    def test_packet_bytes_of_both_builds(self):
        """S2C 0x82..0x89 (spec_2009 "identical"): 0x85 is 31 B with the assumed block, 0x87
        clears and rebuilds, 0x88 / 0x89 echo the descriptor."""
        stick = ST.Entry(STICK, 1, 100, True, [0] * 6)
        socketed = ST.Entry(STICK, 1, 250, True, INV.block_from_wire([5, 7], 3))
        potion = ST.Entry(POTION, 3, 20, False, [0] * 6)
        for build in (B8, B9):
            with self.subTest(build=build):
                for key in ('0x82', '0x83', '0x84'):
                    self.assertEqual(P.build(key, {'result': 1}, client_build=build), b'\x01')
                sign = P.build('0x85', ST.sign_fields(1, TITLE), client_build=build)
                self.assertEqual(sign, bytes.fromhex('01000000' + '0100') + TITLE.ljust(25, b'\x00'))
                self.assertEqual(P.build('0x86', {'owner_uid': 1}, client_build=build).hex(), '01000000')
                self.assertEqual(P.build('0x87', {'result': 0}, client_build=build), b'\x00')
                self.assertEqual(P.build('0x87', {'result': 6}, client_build=build), b'\x06')
                self.assertEqual(P.build('0x87', ST.list_fields(1, TITLE, []), client_build=build), b'\x01\x00')
                body = P.build('0x87', ST.list_fields(1, TITLE, [stick, potion]), client_build=build)
                self.assertEqual(body[:6], bytes.fromhex('0102' + '01000000'))
                self.assertEqual(body[6:31], TITLE.ljust(25, b'\x00'))
                self.assertEqual(body[31:], bytes.fromhex('b300 0100 64000000 00 0000'
                                                          '0700 0300 14000000 00 0000'))
                got = P.parse(0x87, P.build('0x87', ST.list_fields(2, TITLE, [socketed]), client_build=build),
                              direction='S2C', client_build=build)
                row = got['repeat[item_count]'][0]
                self.assertEqual(([o['opt'] for o in row['repeat[opt_count]']], row['item_ext']), ([5, 7], 3))
                self.assertEqual(P.build('0x88', {'result': 0}, client_build=build), b'\x00')
                self.assertEqual(P.build('0x88', ST.bought_fields(460, potion, 2), client_build=build).hex(),
                                 '01' + 'cc01000000000000' + '0700' + '0200' + '00' + '0000')
                self.assertEqual(P.build('0x89', ST.sold_fields(1040, potion, 2), client_build=build).hex(),
                                 '1004000000000000' + '0700' + '0200' + '00' + '0000')

    def test_the_open_list_checks(self):
        """F9 step 3.4: 9 for a tampered list, 2 for a KR NotTrade item; the title is cut."""
        def parse(rows, title=b'T'):
            return ST.parse_open({'item_count': len(rows), 'shop_name': title, 'repeat[item_count]': rows})
        title, entries, why = parse([item(STICK, 1, 100), item(POTION, 3, 20)], b'x' * 30)
        self.assertIsNone(why)
        self.assertEqual(title, b'x' * 24)
        self.assertEqual([(e.item_id, e.qty, e.price, e.equip) for e in entries],
                         [(STICK, 1, 100, True), (POTION, 3, 20, False)])
        for rows, reason in (([], 'item(s) listed'),
                             ([item(POTION, 1, 1)] * (ST.MAX_ITEMS + 1), 'item(s) listed'),
                             ([item(POTION, 1, 0)], 'price'),
                             ([item(POTION, 1000, 5)], 'qty'),
                             ([item(POTION, 0, 5)], 'qty'),
                             ([item(STICK, 2, 5)], 'qty'),
                             ([item(STICK, 1, 5, opts=[1, 2, 3, 4, 5, 6])], 'option words'),
                             ([item(CASH_EQUIP, 1, 5)], 'cash'),
                             ([item(194, 1, 5)], 'Type 3'),
                             ([item(60000, 1, 5)], 'catalog')):
            with self.subTest(reason=reason):
                _t, got, why = parse(rows)
                self.assertEqual((got, why[0]), ([], ST.OPEN_TAMPERED))
                self.assertIn(reason, why[1])
        # an item_count that does not match the rows (the client counts NULL nodes)
        _t, _e, why = ST.parse_open({'item_count': 2, 'shop_name': b'T', 'repeat[item_count]': [item(POTION, 1, 1)]})
        self.assertEqual(why[0], ST.OPEN_TAMPERED)

    def test_ownership_sums_stacks_and_matches_blocks(self):
        char = {'inventory': {}, 'equipped': {}}
        bag = INV.Inventory(char)
        bag.add(POTION, 5)
        bag.add(STICK, 1, [5, 0, 0, 0, 0, 0])
        _t, entries, _w = ST.parse_open({'item_count': 2, 'shop_name': b'T', 'repeat[item_count]': [
            item(POTION, 3, 1), item(POTION, 2, 1)]})
        self.assertIsNone(ST.ownership_refusal(bag, entries))
        _t, entries, _w = ST.parse_open({'item_count': 2, 'shop_name': b'T', 'repeat[item_count]': [
            item(POTION, 3, 1), item(POTION, 3, 1)]})
        self.assertIn('holds 5', ST.ownership_refusal(bag, entries))
        _t, entries, _w = ST.parse_open({'item_count': 1, 'shop_name': b'T', 'repeat[item_count]': [
            item(STICK, 1, 1)]})                                   # a plain stick: the bag's has a socket
        self.assertIn('block', ST.ownership_refusal(bag, entries))
        _t, entries, _w = ST.parse_open({'item_count': 1, 'shop_name': b'T', 'repeat[item_count]': [
            item(STICK, 1, 1, opts=[5])]})
        self.assertIsNone(ST.ownership_refusal(bag, entries))

    def test_a_full_bag_takes_back_what_fits(self):
        char = {'inventory': {'tab_slots': [1, 1, 1]}, 'equipped': {}}
        bag = INV.Inventory(char)
        bag.add(STICK, 1)
        bag.add(POTION, 990)
        left = ST.return_to_bag(bag, [ST.Entry(STICK, 1, 5, True, [0] * 6), ST.Entry(POTION, 20, 1, False, [0] * 6)])
        self.assertEqual(bag.count(POTION), 999)
        self.assertEqual([(e.item_id, e.qty) for e in left], [(STICK, 1), (POTION, 11)])

    def test_escrow_normalizer_and_migration_backup(self):
        char = {'stall_escrow': [{'id': STICK, 'qty': 1, 'price': 100, 'w': [0, 5]}, {'id': 0}, 'x',
                                 {'id': '7', 'qty': '3', 'price': '20'}]}
        ST.ensure(char)
        self.assertEqual(char['stall_escrow'], [{'id': STICK, 'qty': 1, 'price': 100, 'w': [5, 0, 0, 0, 0, 0]},
                                                {'id': POTION, 'qty': 3, 'price': 20}])
        self.assertEqual(ST.ensure({}), [])
        tmp = tempfile.mkdtemp(prefix='ws_stall_store_')
        try:
            path = os.path.join(tmp, 'accounts.json')
            accounts = F.two_player_accounts()
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(accounts, f)
            st = storemod.Store(path, hash_passwords=False).load()
            self.assertTrue(any('stall_escrow created' in c for c in st.migration_changes))
            self.assertTrue(os.path.exists(path + '.bak-pre-p7'))
            with open(path + '.bak-pre-p7', encoding='utf-8') as f:
                self.assertNotIn('stall_escrow', f.read())
            self.assertEqual(st.characters('test')[0]['stall_escrow'], [])
            again = storemod.Store(path, hash_passwords=False).load()
            self.assertEqual(again.migration_changes, [])                  # idempotent
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_routes_and_policy(self):
        want = {0x5E: '_handle_stall_open', 0x5F: '_handle_stall_stop_edit', 0x60: '_handle_stall_close',
                0x61: '_handle_stall_visit', 0x62: '_handle_stall_buy'}
        for routes in (W.GameServer.ROUTES, W.GameServer.ROUTES_2009):
            self.assertEqual({op: routes[op].handler for op in want}, want)
            self.assertEqual(registry.check_routes(routes, W.GameServer), [])
        for build in (B8, B9):
            must = registry.must_reply_table(build)
            self.assertEqual(set(want) & set(must), set(want))
            self.assertFalse(set(want) & set(registry.never_reply_table(build)))
            # the 0x5F / 0x60 backstops answer only while the client can be selling (C8)
            no_market = type('S', (), {'market': None})()
            for op, key in ((0x5F, '0x83'), (0x60, '0x84')):
                self.assertEqual(must[op].refusal(no_market, {}, None), [])
                self.assertEqual(must[op].refusal(no_market, {'stall_client_selling': True}, None),
                                 [(key, {'result': 1}, None)])
        self.assertEqual(W.GameServer.DEV_COMMANDS['stall'].handler, '_dev_stall')


# ============================================================== two players ===
class _Base:
    """TestHero (uid 1: Open Stall learned, a Wooden Stick, 5 potions, 1000 gold) and
    Watcher (uid 2: 500 gold) in world on the flea market 9701 of one server of `build`;
    Carol (uid 3, 300 gold) exists on 9701 and is offline until a test logs her in."""
    build = B8

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        self.tmp = tempfile.mkdtemp(prefix=f'ws_stall_{self.build}_')
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False})
        accounts = F.two_player_accounts()
        accounts['test']['characters'][0].update(_char('TestHero'))
        accounts['admin']['characters'][0].update(_char('Watcher'))
        accounts['carol'] = {'password': 'carol', 'characters': [_char('Carol')]}
        self.server = F.make_server(self.tmp, accounts=accounts, config=cfg)
        self.k = KEYS[self.build]
        self.stock('TestHero', gold=1000, items=[(STICK, 1), (POTION, 5)])
        self.stock('Watcher', gold=500, items=[])
        self.stock('Carol', gold=300, items=[])
        with self.server.store.lock:
            self.assertTrue(SK.learn(self.rec('TestHero'), ST.OPEN_STALL_SKILL, check=False).ok)
        self.prepare()
        self.extra = []
        self.mc = F.MultiClient(self.server)
        self.a, self.b = self.mc
        self.mc.drain()

    def prepare(self):
        """Store edits before anyone logs in (subclass hook)."""

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
        for item_id, qty in items:
            self.assertIsNotNone(bag.add(item_id, qty))
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

    def open_stall(self, c=None, rows=None, title=TITLE, result=1):
        c = c or self.a
        rows = rows if rows is not None else [item(STICK, 1, 100), item(POTION, 3, 20)]
        self.send(c, 'open', {'item_count': len(rows), 'shop_name': title, 'repeat[item_count]': rows})
        self.assertEqual(c.s2c(c.expect(0x82)), {'result': result})

    @staticmethod
    def text(rec, key):
        """The decoded record with its text field as raw bytes (parse returns latin-1 str)."""
        out = dict(rec)
        out[key] = P.to_bytes(out[key])
        return out

    def sign(self, c, owner=1, title=TITLE):
        self.assertEqual(self.text(c.s2c(c.expect(0x85)), 'stall_title'),
                         {'owner_uid': owner, 'sign_sprite_id': ST.SIGN_SPRITE, 'stall_title': title})

    def visit(self, c, owner=1):
        self.send(c, 'visit', {'stall_owner_uid': owner})
        return c.s2c(c.expect(0x87))

    def buy(self, c, item_id, qty, owner=1, opts=(), extra=0):
        self.send(c, 'buy', {'seller_uid': owner, 'item_id': item_id, 'qty': qty, 'socket_count': len(opts),
                             'repeat[socket_count]': [{'socket_stone_id': w} for w in opts],
                             'item_extra': extra})

    @staticmethod
    def rows(listing):
        return [(r['item_id'], r['qty'], r['price']) for r in listing.get('repeat[item_count]', [])]

    def refused(self, c, rows_after=None):
        """0x88 {0} and - while the stall is open - a fresh 0x87 of it to this buyer only."""
        if rows_after is None:
            self.assertEqual(c.s2c(c.expect(0x88)), {'result': 0})
            return None
        fail, listing = c.expect(0x88, 0x87)
        self.assertEqual(c.s2c(fail), {'result': 0})
        got = c.s2c(listing)
        self.assertEqual(self.rows(got), rows_after)
        return got

    def relog(self, account, password, name, number):
        c = F.FakeClient(self.server)
        self.extra.append(c)
        self.assertEqual(c.login(account, password)['result'], 1)
        c.char_name = name
        pkts = c.enter_world(name, port=F.P2P_PORT_BASE + number - 1)
        return c, pkts

    def audit_lines(self):
        path = os.path.join(self.tmp, MK.LOG_NAME)
        if not os.path.exists(path):
            return []
        with open(path, encoding='utf-8') as f:
            return [json.loads(line) for line in f if line.strip()]


class _Stalls(_Base):
    # ------------------------------------------------------------- exit 4 ---
    def test_exit4_open_browse_buy_and_close(self):
        a, b = self.a, self.b
        self.open_stall()
        self.sign(b)                                            # B sees the sign with the title
        a.expect_silence(0.05)
        # escrowed: out of the bag model, into the persisted record
        self.assertEqual(self.bag('TestHero'), {POTION: 2})
        escrow = [{'id': STICK, 'qty': 1, 'price': 100, 'w': [0] * 6}, {'id': POTION, 'qty': 3, 'price': 20}]
        self.assertEqual(self.rec('TestHero')['stall_escrow'], escrow)
        self.assertEqual(self.disk('TestHero')['stall_escrow'], escrow)
        # B browses
        listing = self.visit(b)
        self.assertEqual((listing['result'], listing['owner_uid'], P.to_bytes(listing['stall_name'])), (1, 1, TITLE))
        self.assertEqual(self.rows(listing), [(STICK, 1, 100), (POTION, 3, 20)])
        # B buys 2 potions: 40 gold from B to A, the potions into B's bag, A's list -2
        self.buy(b, POTION, 2)
        got = b.s2c(b.expect(0x88))
        self.assertEqual((got['result'], got['gold'], got['item_id'], got['qty']), (1, 460, POTION, 2))
        sold = a.s2c(a.expect(0x89))
        self.assertEqual((sold['gold'], sold['item_id'], sold['qty']), (1040, POTION, 2))
        self.assertEqual((self.gold('Watcher'), self.gold('TestHero')), (460, 1040))
        self.assertEqual(self.bag('Watcher'), {POTION: 2})
        self.assertEqual(self.bag('TestHero'), {POTION: 2})     # the seller's bag is not touched
        self.assertEqual(self.rec('TestHero')['stall_escrow'][1], {'id': POTION, 'qty': 1, 'price': 20})
        on_disk = (self.disk('TestHero'), self.disk('Watcher'))   # one write, before the packets
        self.assertEqual((on_disk[0]['gold'], on_disk[1]['gold']), (1040, 460))
        self.assertEqual(on_disk[1]['inventory']['consume'], {str(POTION): 2})
        # B buys the stick: its node goes, and a re-browse lists only the last potion
        self.buy(b, STICK, 1)
        self.assertEqual(b.s2c(b.expect(0x88))['gold'], 360)
        self.assertEqual(a.s2c(a.expect(0x89))['gold'], 1140)
        self.assertEqual(self.rows(self.visit(b)), [(POTION, 1, 20)])
        self.assertEqual(self.bag('Watcher'), {POTION: 2, STICK: 1})
        # A closes: 0x84 {1}; the unsold potion is back in A's bag; B's sign goes (0x86)
        self.send(a, 'close')
        self.assertEqual(a.s2c(a.expect(0x84)), {'result': 1})
        self.assertEqual(b.s2c(b.expect(0x86)), {'owner_uid': 1})
        self.assertEqual((self.bag('TestHero'), self.rec('TestHero')['stall_escrow']), ({POTION: 3}, []))
        self.assertEqual(self.disk('TestHero')['stall_escrow'], [])
        self.assertEqual(self.visit(b), {'result': 0})           # "The shop is closed or adjusting."
        self.send(a, 'close')                                    # the client's automatic re-send
        a.expect_silence(0.2)
        # gold is conserved and the audit has open / 2 sales / close
        self.assertEqual(self.gold('TestHero') + self.gold('Watcher'), 1500)
        self.assertEqual([e['event'] for e in self.audit_lines()], ['open', 'sale', 'sale', 'close'])

    def test_exit4_a_player_entering_later_sees_the_open_stall(self):
        """F14.2 / B13: the late arrival's 0x04 carries the seller's ordinary record and the
        0x85 with the title follows it at once; a closed stall sends nothing."""
        self.open_stall()
        self.sign(self.b)
        c, pkts = self.relog('carol', 'carol', 'Carol', 3)
        ops = _ops(pkts)
        at = ops.index(0x04)
        self.assertEqual(ops[at + 1], 0x85)
        rows = c.s2c(pkts[at])['repeat[player_count]']
        self.assertEqual(sorted(r['uid'] for r in rows), [1, 2])
        self.assertTrue(all(r['shop_open'] == 0 for r in rows))   # the sign rides on the 0x85
        self.assertEqual(self.text(c.s2c(pkts[at + 1]), 'stall_title'),
                         {'owner_uid': 1, 'sign_sprite_id': ST.SIGN_SPRITE, 'stall_title': TITLE})
        self.assertEqual(ops.count(0x85), 1)
        self.mc.drain()
        # Carol can browse and buy too
        self.assertEqual(self.rows(self.visit(c)), [(STICK, 1, 100), (POTION, 3, 20)])
        # an ordinary 0x05 re-spawn of the seller (presence.spawn) is followed by its sign too
        self.assertTrue(presence.despawn(self.server, self.a.session, c.session))
        c.expect(0x06)
        self.assertTrue(presence.spawn(self.server, self.a.session, c.session))
        appear, sign = c.expect(0x05, 0x85)
        self.assertEqual(c.s2c(sign)['owner_uid'], 1)
        # after a close a newcomer gets no sign
        self.send(self.a, 'close')
        self.a.expect(0x84)
        self.b.expect(0x86)
        c.expect(0x86)
        c.close()
        c.thread.join(timeout=5.0)
        self.mc.drain()
        c2, pkts = self.relog('carol', 'carol', 'Carol', 3)
        self.assertNotIn(0x85, _ops(pkts))

    def test_stop_for_edit_gives_the_escrow_back_and_start_reopens(self):
        a, b = self.a, self.b
        self.open_stall()
        self.sign(b)
        self.send(a, 'stop')
        self.assertEqual(a.s2c(a.expect(0x83)), {'result': 1})
        self.assertEqual(b.s2c(b.expect(0x86)), {'owner_uid': 1})
        self.assertEqual((self.bag('TestHero'), self.rec('TestHero')['stall_escrow']), ({STICK: 1, POTION: 5}, []))
        self.assertFalse(a.session['stall_client_selling'])
        self.assertEqual(self.visit(b), {'result': 0})
        # setup mode closes locally (no 0x60); Start again with an edited list and title
        self.open_stall(rows=[item(POTION, 5, 30)], title=b'Potions')
        self.sign(b, title=b'Potions')
        self.assertEqual(self.bag('TestHero'), {STICK: 1})
        self.assertEqual(self.rows(self.visit(b)), [(POTION, 5, 30)])
        self.send(a, 'stop')
        a.expect(0x83)
        b.expect(0x86)
        # a Stop with no stall and the selling mirror clear crossed our 0x83 / 0x84: ignored,
        # since 0x83 {1} sets ctx+0x1C in any mode (C8) and would dupe a later buyer window
        self.send(a, 'stop')
        a.expect_silence(0.2)
        b.expect_silence(0.1)
        # a failed Start leaves the client selling (+0x20): its Stop still gets 0x83 {1}
        self.open_stall(rows=[item(POTION, 6, 30)], result=9)
        self.send(a, 'stop')
        self.assertEqual(a.s2c(a.expect(0x83)), {'result': 1})
        self.assertFalse(a.session['stall_client_selling'])
        b.expect_silence(0.1)

    def test_a_repeated_start_replaces_the_stall_without_duplicating(self):
        """A second 0x5E while one is open (a lost 0x83): the old escrow comes back first
        (F9 3.7), so the same items can be listed again - never twice."""
        a, b = self.a, self.b
        self.open_stall(rows=[item(POTION, 5, 20)])
        self.sign(b)
        self.open_stall(rows=[item(POTION, 5, 25)])
        self.assertEqual(_ops(b.expect(0x86, 0x85)), [0x86, 0x85])
        self.assertEqual(self.rows(self.visit(b)), [(POTION, 5, 25)])
        self.assertEqual(self.bag('TestHero'), {STICK: 1})
        self.assertEqual(self.rec('TestHero')['stall_escrow'], [{'id': POTION, 'qty': 5, 'price': 25}])

    def test_a_portal_closes_the_stall_before_the_lead(self):
        """F14.1: 0x84 {1} before the 0x08, the escrow back in the bag and in the 0x03 of
        that load; B gets the 0x86 before the seller's 0x06."""
        a, b = self.a, self.b
        self.open_stall()
        self.sign(b)
        line = next(int(k.split('_')[1]) for k, v in sorted(EC.portals().items())
                    if k.startswith(f'{FLEA}_') and v[0] == TOWN)
        self.send(a, 'portal', {'portal_line_index': line})
        self.assertTrue(_wait(lambda: self.server.world.map_of(a.session) == TOWN))
        pkts = a.recv_until_quiet(0.3)
        self.assertEqual(_ops(pkts)[:3], [0x84, 0x08, 0x03])
        bag03 = a.s2c(pkts[2], allow_trailing=True)
        self.assertEqual([r['item_id'] for r in bag03['repeat[equip_item_count]']], [STICK])
        self.assertEqual([(r['item_id'], r['quantity']) for r in bag03['repeat[consume_item_count]']], [(POTION, 5)])
        self.assertEqual(_ops(b.recv_until_quiet(0.3)), [0x86, 0x06])
        self.assertEqual((self.bag('TestHero'), self.rec('TestHero')['stall_escrow']), ({STICK: 1, POTION: 5}, []))
        self.assertIsNone(self.server.market.stall_of(a.session))
        self.send(a, 'close')                                   # the 0x08 path's 0x60, if any
        a.expect_silence(0.2)

    def test_disconnect_returns_the_unsold_items(self):
        a, b = self.a, self.b
        self.open_stall()
        self.sign(b)
        self.buy(b, POTION, 1)
        b.expect(0x88)
        a.expect(0x89)
        a.close()
        a.thread.join(timeout=5.0)
        self.assertEqual(_ops(b.recv_until_quiet(0.3)), [0x86, 0x06])
        self.assertEqual((self.bag('TestHero'), self.rec('TestHero')['stall_escrow']), ({STICK: 1, POTION: 4}, []))
        self.assertEqual(self.server.market.stalls, {})
        self.assertEqual(self.disk('TestHero')['stall_escrow'], [])
        # the relog's 0x03 lists them
        c, pkts = self.relog('test', 'test', 'TestHero', 1)
        bag03 = c.s2c(next(p for p in pkts if p.opcode == 0x03), allow_trailing=True)
        self.assertEqual([(r['item_id'], r['quantity']) for r in bag03['repeat[consume_item_count]']], [(POTION, 4)])
        self.assertEqual(self.gold('TestHero'), 1020)

    def notice(self, pkt, c):
        return P.to_bytes(c.s2c(pkt)['text']).decode('cp949')

    def send_open(self, c, rows, title=TITLE):
        self.send(c, 'open', {'item_count': len(rows), 'shop_name': title, 'repeat[item_count]': rows})

    def test_items_a_full_bag_kept_are_never_lost_by_the_next_start(self):
        """Review fix 1: a close into a full bag keeps the rest in char['stall_escrow'];
        the next Start puts those rows back into the bag BEFORE its new escrow is written,
        and while some still do not fit it is refused (0x82 {2} + the bag-full line) with
        the rows kept - partly back is partly back. Nothing ever disappears."""
        a, b = self.a, self.b
        full = '[Warning] ' + MK.BAG_FULL_TEXT
        self.open_stall(rows=[item(STICK, 1, 100), item(POTION, 5, 20)])
        self.sign(b)
        with self.server.store.lock:                            # both tabs fill while selling
            ch = self.rec('TestHero')
            ch['inventory']['tab_slots'] = [1, 1, 35]
            self.assertIsNotNone(INV.Inventory(ch).add(STICK, 1))
            self.assertIsNotNone(INV.Inventory(ch).add(HERB, 10))
        self.send(a, 'close')
        self.assertEqual(self.notice(a.expect(0x84, 0x15)[1], a), full)
        b.expect(0x86)
        kept = [{'id': STICK, 'qty': 1, 'price': 100, 'w': [0] * 6}, {'id': POTION, 'qty': 5, 'price': 20}]
        self.assertEqual((self.bag('TestHero'), self.rec('TestHero')['stall_escrow']), ({STICK: 1, HERB: 10}, kept))
        # no room yet: refused, the rows stay (the old code overwrote them with the new list)
        self.send_open(a, [item(HERB, 10, 5)])
        fail, line = a.expect(0x82, 0x15)
        self.assertEqual((a.s2c(fail), self.notice(line, a)), ({'result': 2}, full))
        self.assertEqual((self.bag('TestHero'), self.rec('TestHero')['stall_escrow']), ({STICK: 1, HERB: 10}, kept))
        self.assertEqual(self.disk('TestHero')['stall_escrow'], kept)
        self.assertIsNone(self.server.market.stall_of(a.session))
        b.expect_silence(0.1)
        self.send(a, 'close')                                   # the refused Start kept +0x20
        a.expect(0x84)
        # equip room only: the stick comes back, the potions still refuse the Start
        with self.server.store.lock:
            self.rec('TestHero')['inventory']['tab_slots'] = [2, 1, 35]
        self.send_open(a, [item(HERB, 10, 5)])
        back, fail, line = a.expect(0x15, 0x82, 0x15)
        self.assertEqual((self.notice(back, a), a.s2c(fail), self.notice(line, a)),
                         ('[Warning] ' + MK.RESTORED_TEXT, {'result': 2}, full))
        self.assertEqual((self.bag('TestHero'), self.rec('TestHero')['stall_escrow']),
                         ({STICK: 2, HERB: 10}, kept[1:]))
        self.assertEqual(self.disk('TestHero')['stall_escrow'], kept[1:])
        self.send(a, 'close')
        a.expect(0x84)
        # room for all: the potions come back, then the herbs are escrowed and listed
        with self.server.store.lock:
            self.rec('TestHero')['inventory']['tab_slots'] = [2, 35, 35]
        self.send_open(a, [item(HERB, 10, 5)])
        ok, back = a.expect(0x82, 0x15)
        self.assertEqual((a.s2c(ok), self.notice(back, a)), ({'result': 1}, '[Warning] ' + MK.RESTORED_TEXT))
        self.sign(b)
        self.assertEqual((self.bag('TestHero'), self.rec('TestHero')['stall_escrow']),
                         ({STICK: 2, POTION: 5}, [{'id': HERB, 'qty': 10, 'price': 5}]))
        self.assertEqual(self.rows(self.visit(b)), [(HERB, 10, 5)])
        self.send(a, 'close')
        a.expect(0x84)
        b.expect(0x86)
        self.assertEqual((self.bag('TestHero'), self.rec('TestHero')['stall_escrow']),
                         ({STICK: 2, POTION: 5, HERB: 10}, []))
        self.assertEqual(self.disk('TestHero')['stall_escrow'], [])
        # the next 0x03 (a portal) shows every restored item
        line_no = next(int(k.split('_')[1]) for k, v in sorted(EC.portals().items())
                       if k.startswith(f'{FLEA}_') and v[0] == TOWN)
        self.send(a, 'portal', {'portal_line_index': line_no})
        self.assertTrue(_wait(lambda: self.server.world.map_of(a.session) == TOWN))
        pkts = a.recv_until_quiet(0.3)
        bag03 = a.s2c(next(p for p in pkts if p.opcode == 0x03), allow_trailing=True)
        self.assertEqual([r['item_id'] for r in bag03['repeat[equip_item_count]']], [STICK, STICK])
        self.assertEqual(sorted((r['item_id'], r['quantity']) for r in bag03['repeat[consume_item_count]']),
                         sorted([(POTION, 5), (HERB, 10)]))

    def test_no_trade_while_selling(self):
        """Review fix 3: a seller can neither request nor accept a trade (a trade filling
        its bag is what makes a close keep items back); a Start while trading was already
        refused (_open_gate)."""
        a, b = self.a, self.b
        trades = self.server.trade
        self.assertEqual(trades.request(a.session, 2), 'requested')     # prompts made BEFORE the Start
        b.expect(0x45)
        self.open_stall(rows=[item(POTION, 5, 20)])
        self.sign(b)
        self.assertEqual(trades.accept(b.session, 'TestHero'), 'busy')  # the host now sells
        self.assertEqual(b.s2c(b.expect(0x47)), {'result': TR.RESULT_BUSY})
        self.assertEqual(trades.request(b.session, 1), 'busy')          # the target sells
        self.assertEqual(b.s2c(b.expect(0x47)), {'result': TR.RESULT_BUSY})
        a.expect_silence(0.1)
        self.assertEqual(trades.request(a.session, 2), 'selling')       # the requester sells
        self.assertEqual(self.notice(a.expect(0x15), a), '[Warning] ' + TR.CANT_NOW_TEXT)
        b.expect_silence(0.1)
        self.assertEqual(trades.trades, {})
        self.send(a, 'close')
        a.expect(0x84)
        b.expect(0x86)
        self.assertEqual(trades.request(b.session, 1), 'requested')     # closed: trading again
        a.expect(0x45)
        self.assertEqual(trades.accept(a.session, 'Watcher'), 'open')

    # ------------------------------------------------------------ guards ---
    def test_no_buying_from_yourself(self):
        a = self.a
        self.open_stall()
        self.b.expect(0x85)
        self.buy(a, POTION, 1, owner=1)
        self.refused(a)
        self.assertEqual(self.visit(a), {'result': 6})           # "Open stall is opened..."
        self.assertEqual((self.gold('TestHero'), len(self.rec('TestHero')['stall_escrow'])), (1000, 2))

    def test_quantity_block_and_item_tampering_is_refused_with_a_fresh_list(self):
        b = self.b
        self.open_stall()
        b.expect(0x85)
        listed = [(STICK, 1, 100), (POTION, 3, 20)]
        for kwargs in ({'item_id': POTION, 'qty': 0}, {'item_id': POTION, 'qty': 4},
                       {'item_id': STICK, 'qty': 2}, {'item_id': STICK, 'qty': 1, 'opts': [5]},
                       {'item_id': HERB, 'qty': 1}):
            with self.subTest(**kwargs):
                self.buy(b, **kwargs)
                self.refused(b, listed)
        self.buy(b, POTION, 1, owner=3)                         # no stall of that uid
        self.refused(b)
        self.assertEqual((self.gold('Watcher'), self.gold('TestHero'), self.bag('Watcher')), (500, 1000, {}))
        self.a.expect_silence(0.1)                               # never a 0x89 for a refusal

    def test_short_of_gold_no_room_and_trade_locked_gold(self):
        b = self.b
        self.open_stall(rows=[item(STICK, 1, 600), item(POTION, 3, 20)])
        b.expect(0x85)
        listed = [(STICK, 1, 600), (POTION, 3, 20)]
        self.buy(b, STICK, 1)                                   # 600 > 500
        self.refused(b, listed)
        with self.server.store.lock:
            self.rec('Watcher')['inventory']['tab_slots'] = [35, 1, 35]
            INV.Inventory(self.rec('Watcher')).add(HERB, 999)   # the one consume slot is full
        self.buy(b, POTION, 1)
        self.refused(b, listed)
        with self.server.store.lock:
            INV.Inventory(self.rec('Watcher')).remove(HERB, 999)
        # gold locked in a trade (C2S 0x23) cannot pay for a stall item
        carol, _ = self.relog('carol', 'carol', 'Carol', 3)
        self.mc.drain()
        carol.recv_until_quiet(0.2)
        trades = self.server.trade
        self.assertEqual(trades.request(b.session, 3), 'requested')
        self.assertEqual(trades.accept(carol.session, 'Watcher'), 'open')
        self.assertEqual(trades.lock_offer(b.session, 450), 'locked')
        self.mc.drain()
        carol.recv_until_quiet(0.2)
        self.buy(b, POTION, 3)                                  # 60 > 500 - 450
        self.refused(b, listed)
        self.buy(b, POTION, 2)                                  # 40 <= 50
        self.assertEqual(b.s2c(b.expect(0x88))['gold'], 460)
        self.assertEqual(self.gold('TestHero'), 1040)

    def test_open_gates_and_tampered_lists(self):
        a, b = self.a, self.b
        before = (self.bag('TestHero'), self.gold('TestHero'))
        self.open_stall(rows=[item(POTION, 6, 20)], result=9)             # owns 5
        self.open_stall(rows=[item(POTION, 1, 0)], result=9)              # price 0
        self.open_stall(rows=[item(STICK, 1, 5, opts=[9])], result=9)     # no such block
        self.open_stall(rows=[item(POTION, 1, 1)] * 7, result=9)          # 7 > MAX_ITEMS
        a.send(0x5E, b'\x02' + P.build(self.k['open'], {'item_count': 1, 'shop_name': 'T', 'repeat[item_count]': [
            item(POTION, 1, 1)]}, direction='C2S', client_build=self.build)[1:])
        self.assertEqual(a.s2c(a.expect(0x82)), {'result': 2})            # does not decode (F9 3.3)
        with self.server.store.lock:
            SK.unlearn(self.rec('TestHero'), ST.OPEN_STALL_SKILL)
        self.open_stall(result=2)
        with self.server.store.lock:
            SK.learn(self.rec('TestHero'), ST.OPEN_STALL_SKILL, check=False)
            self.server.store.account('test')['manner'] = -80
        self.open_stall(result=2)
        self.server.store.account('test')['manner'] = 0
        self.assertEqual((self.bag('TestHero'), self.gold('TestHero')), before)
        self.assertEqual((self.rec('TestHero')['stall_escrow'], self.server.market.stalls), ([], {}))
        b.expect_silence(0.1)                                    # no sign for a refused Start
        # a failed Start still gets its 0x84 {1} on Close (the client kept +0x20)
        self.send(a, 'close')
        self.assertEqual(a.s2c(a.expect(0x84)), {'result': 1})

    def test_off_the_flea_market_is_refused(self):
        with self.server.store.lock:
            self.rec('Carol')['map'] = TOWN
            SK.learn(self.rec('Carol'), ST.OPEN_STALL_SKILL, check=False)
            INV.Inventory(self.rec('Carol')).add(POTION, 1)
        carol, _ = self.relog('carol', 'carol', 'Carol', 3)
        self.open_stall(c=carol, rows=[item(POTION, 1, 1)], result=2)
        self.assertEqual(self.bag('Carol'), {POTION: 1})

    def test_two_buyers_race_for_the_last_unit(self):
        """Both 0x62 arrive together for the one stick: the handler threads serialize on
        world_lock and the combat locks - exactly one 0x88 {1}, the other 0x88 {0} + 0x87."""
        b = self.b
        carol, _ = self.relog('carol', 'carol', 'Carol', 3)
        self.mc.drain()
        self.open_stall(rows=[item(STICK, 1, 100)])
        for c in (b, carol):
            c.expect(0x85)
        t = threading.Thread(target=self.buy, args=(carol, STICK, 1))
        t.start()
        self.buy(b, STICK, 1)
        t.join()
        results = {}
        for name, c in (('Watcher', b), ('Carol', carol)):
            pkts = c.recv_until_quiet(0.3)
            results[name] = c.s2c(pkts[0])['result']
            if results[name] == 0:
                self.assertEqual(c.s2c(pkts[1]), {'result': 1, 'item_count': 0})
        self.assertEqual(sorted(results.values()), [0, 1])
        self.assertEqual(_ops(self.a.recv_until_quiet(0.3)), [0x89])
        winner = next(n for n, r in results.items() if r == 1)
        self.assertEqual(self.bag(winner), {STICK: 1})
        self.assertEqual(self.gold('TestHero'), 1100)
        self.assertEqual(self.gold('Watcher') + self.gold('Carol'), 700)

    def test_a_crash_left_escrow_is_merged_back_at_login(self):
        """F14.5: a record that still holds an escrow and has no open stall (the server
        stopped mid-sale) gets it back at the next login - as much as the bag takes."""
        self.a.close()
        self.a.thread.join(timeout=5.0)
        self.b.recv_until_quiet(0.2)
        with self.server.store.lock:
            ch = self.rec('TestHero')
            ch['stall_escrow'] = [{'id': STICK, 'qty': 1, 'price': 100, 'w': [0] * 6},
                                  {'id': HERB, 'qty': 4, 'price': 5}]
            ch['inventory']['tab_slots'] = [1, 35, 35]           # the equip tab holds the stick already
        c, pkts = self.relog('test', 'test', 'TestHero', 1)
        self.assertEqual(self.bag('TestHero'), {STICK: 1, POTION: 5, HERB: 4})
        self.assertEqual(self.rec('TestHero')['stall_escrow'], [{'id': STICK, 'qty': 1, 'price': 100,
                                                                 'w': [0] * 6}])
        bag03 = c.s2c(next(p for p in pkts if p.opcode == 0x03), allow_trailing=True)
        self.assertIn((HERB, 4), [(r['item_id'], r['quantity']) for r in bag03['repeat[consume_item_count]']])

    def test_dev_stall_view(self):
        self.open_stall()
        self.b.expect(0x85)
        self.assertEqual(self.server.market.describe(self.a.session)[1:],
                         [f' {EC.item_name(STICK) or STICK} x1 @ 100', f' {EC.item_name(POTION) or POTION} x3 @ 20'])
        self.assertEqual(self.server.market.describe_all()[1], f'TestHero (uid 1) map {FLEA}: {STICK}x1@100, {POTION}x3@20')
        self.assertEqual(self.server.market.describe(self.b.session)[0], 'no stall (selling mirror False)')


class Stalls2008(_Stalls, unittest.TestCase):
    build = B8


class Stalls2009(_Stalls, unittest.TestCase):
    build = B9


if __name__ == '__main__':
    unittest.main()
