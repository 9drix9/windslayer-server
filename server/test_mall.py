#!/usr/bin/env python3
"""
test_mall.py - P8 stage 2: the Item Mall / Spark Shop (mall.py), both client builds
==================================================================================
premium_cash-mall-enter, -mall-exit, -balance-refresh, -buy, -box-delete, -gift and
chat_mail_gm-gift-inbox (docs/systems/premium_cash.md F2-F7, chat_mail_gm.md F8):

  - `!mall` (any player; the client has no entry request) -> 0x6F, [0x6D], 0x6A in each
    build's layout (2009 mode 0), the session off its map (peers get 0x06), refusals;
  - C2S 0x42 close: the box <-> character moves (idempotent, cash-bag items only), 0x6B, the
    map-load replay to the remembered point (peers see the player again);
  - C2S 0x46 -> exactly one 0x70 per request (the charge flag clears: no resend loop);
  - C2S 0x43 buy: server prices, cart running balances, mileage payment (2009 fallback),
    refusals 0 / 0x0E, costumes / pets / 2009 options refused, the slot extension +5 capped
    at 45 (60 in the bank) and persisted into the next 0x03;
  - C2S 0x45 delete: 30 % mileage refund for cash-bought unused records only;
  - C2S 0x47 gift: 2008 password unlock (C2S 0x51 -> 0x80 {1, 0x1F9}), 2009 none; the result
    codes 0 / 2 / 0x0C / 0x14 / 0x0E; the recipient's box + inbox, 0x79 / 0x6D live;
  - the gift inbox: once per connection, count-0 relight after a map load (C20), delivered at
    the 0x6A, P5 `!gift` entries get their box record, undisplayable ones become memos;
  - the 2009 must-reply rows of the mall's unmodelled windows (0x4E, 0x80).

No port is bound, no client is started and the live accounts.json is never opened (temp
copies; the module checks its hash at the end).
"""
import hashlib
import logging
import os
import shutil
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
CART = {B8: '0x45FFC8/0x43', B9: '0x466BDE/0x43'}
DELETE = {B8: '0x460676/0x45', B9: '0x464928/0x45'}
REFRESH = {B8: '0x43E302/0x46', B9: '0x43E602/0x46'}
GIFT = {B8: '0x46050C/0x47', B9: '0x467D8C/0x47'}
PASSWORD = {B8: '0x460831/0x51', B9: '0x468F05/0x51'}

MEGAPHONE, SUPER_MEGA, NOTE, COSTUME, STICK = 3381, 3379, 1894, 4235, 179
HAIR_M, HAIR_F = 3386, 3385              # hii Gender 1 (male) / 2 (female)
TAB_EXT, BANK_EXT = 1884, 1887           # equipment tab / bank equipment tab (+5)
PRICE = {MEGAPHONE: 100, SUPER_MEGA: 500, NOTE: 30, TAB_EXT: 4600}

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
            'accounts.json changed during test_mall.py (tests must only use temp copies)'


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


# ============================================================== pure rules ===
class Rules(unittest.TestCase):
    """mall.py's pure rules on the configured client's catalog (2008 by default)."""

    def setUp(self):
        if not HAVE[B8]:
            self.skipTest('needs the EN 2008 client data')
        EC.configure(DIRS[B8], B8)

    def test_frame_limits(self):
        # 2008 0x6A {u32, u32, i32} + 72 x 28 = 2028 B; 2009 {u8, u8, u32, u32} + 72 x 28 = 2026 B
        self.assertEqual(MALL.BOX_MAX, 72)
        rows = [CASH.make_record(0x1000 + i, MEGAPHONE, 1, 1) for i in range(MALL.BOX_MAX)]
        acc = CASH.ensure_account({'cash_box': rows})
        self.assertEqual(len(P.build('0x6A', MALL.enter_fields(acc, B8), client_build=B8)), 12 + 28 * 72)
        self.assertEqual(len(P.build('0x6A', MALL.enter_fields(acc, B9), client_build=B9)), 10 + 28 * 72)
        gifts = [{'sender': 'Bob', 'message': 'x' * 90, 'item_id': MEGAPHONE}] * 40
        for build, per in ((B8, 16), (B9, 15)):
            pages = MALL.gift_pages(gifts, build)
            self.assertEqual([p['gift_count'] for p in pages], [per, per, 40 - 2 * per])
            for fields in pages:
                self.assertLessEqual(len(P.build('0x6D', fields, client_build=build)), CASH.FRAME_PAYLOAD_MAX)
        self.assertEqual(MALL.gift_pages([], B9), [{'gift_count': 0, 'repeat[gift_count]': []}])

    def test_sale_refusal(self):
        self.assertIsNone(MALL.sale_refusal(CASH.cash_def(MEGAPHONE), MEGAPHONE))
        self.assertIsNone(MALL.sale_refusal(CASH.cash_def(TAB_EXT), TAB_EXT))          # the picker's row
        self.assertIn('costume', MALL.sale_refusal(CASH.cash_def(COSTUME), COSTUME))
        self.assertIn('not sold', MALL.sale_refusal(CASH.cash_def(3327), 3327))        # gift card: Cash 0
        self.assertIn('not sold', MALL.sale_refusal(CASH.cash_def(STICK), STICK))
        self.assertIn('not in the client', MALL.sale_refusal(None, 60000))
        self.assertIn('option', MALL.sale_refusal(CASH.cash_def(MEGAPHONE), MEGAPHONE, 2))

    def test_refund_and_gender(self):
        d = CASH.cash_def(SUPER_MEGA)
        rec = CASH.make_record(0x1000, SUPER_MEGA, CASH.KIND_COUNT, 1, origin=CASH.ORIGIN_CASH)
        self.assertEqual(MALL.delete_refund(rec, d), 150)                # live: 150 for a 500 item
        self.assertEqual(MALL.delete_refund({**rec, 'origin': CASH.ORIGIN_MILEAGE}, d), 0)
        self.assertEqual(MALL.delete_refund({**rec, 'origin': CASH.ORIGIN_GIFT}, d), 0)
        eleven = CASH.cash_def(3380)                                     # "11 Megaphones": Cash_V 11
        used = CASH.make_record(0x1001, 3380, CASH.KIND_COUNT, eleven.value - 1, origin=CASH.ORIGIN_CASH)
        self.assertEqual(MALL.delete_refund(used, eleven), 0)            # partly used
        self.assertGreater(MALL.delete_refund({**used, 'qty': eleven.value}, eleven), 0)
        self.assertIsNone(MALL.gender_refusal(CASH.cash_def(HAIR_M), 1))
        self.assertIsNotNone(MALL.gender_refusal(CASH.cash_def(HAIR_M), 0))
        self.assertIsNone(MALL.gender_refusal(CASH.cash_def(HAIR_F), 0))
        self.assertIsNotNone(MALL.gender_refusal(CASH.cash_def(HAIR_F), 1))
        self.assertIsNone(MALL.gender_refusal(CASH.cash_def(MEGAPHONE), 0))

    def test_gift_display_gate(self):
        ok = {'sender': 'Bob', 'message': 'hi', 'item_id': MEGAPHONE}
        self.assertIsNone(MALL.gift_display_refusal(ok))
        self.assertIsNone(MALL.gift_display_refusal({**ok, 'item_id': 3327}))    # gift card: Cash 0, allowed
        self.assertIn('def+0x1F0', MALL.gift_display_refusal({**ok, 'item_id': STICK}))
        self.assertIn('sender', MALL.gift_display_refusal({**ok, 'sender': 'Two Words'}))
        self.assertIn('sender', MALL.gift_display_refusal({**ok, 'sender': ''}))
        self.assertIn('sender', MALL.gift_display_refusal({**ok, 'sender': 'x' * 17}))


class Registry2009(unittest.TestCase):
    """The mall's unmodelled 2009 windows (ROADMAP_2009_ADDENDUM C8) wait for an answer."""

    def test_add_option_and_sale_offer_get_a_refusal(self):
        must9 = registry.must_reply_table(B9)
        self.assertEqual(must9[0x4E].refusal(None, {}, None), [('0xC4', {'result': 0}, None)])
        self.assertEqual(must9[0x80].refusal(None, {}, None), [('0x71', {'is_trade': 1, 'result': 0x17}, None)])
        self.assertEqual(P.build('0x71', {'is_trade': 1, 'result': 0x17}, client_build=B9), b'\x01\x17')
        self.assertNotIn(0x4E, registry.MUST_REPLY)                    # no such 2008 window
        self.assertIs(registry.must_reply_table(B8), registry.MUST_REPLY)


# ================================================================ in world ===
class _MallWorld:
    """TestHero (GM, uid 1) and Watcher (a player, uid 2) on map 101 of one server of `build`;
    Alt is TestHero's second character, Late an offline character of another account."""
    build = B8

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        self.tmp = tempfile.mkdtemp(prefix=f'ws_mall_{self.build}_')
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False})
        accounts = {'test': {'password': 'test', 'gender': 1,
                             'characters': [_char('TestHero', gm=1, gender=1), _char('Alt', gender=1)]},
                    'admin': {'password': 'admin', 'gender': 0,
                              'characters': [_char('Watcher', x=1400.0, gender=0)]},
                    'late': {'password': 'late', 'characters': [_char('Late')]}}
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

    def wallet(self, username='test', cash=None, mileage=None):
        self.server.cash.set_balance(self.acc(username), cash=cash, mileage=mileage)

    def line(self, c, text):
        c.send_c2s(CHAT[self.build], {'msg_len': len(text), 'message': text})

    def enter_mall(self, c):
        self.line(c, b'!mall')
        self.assertTrue(_wait(lambda: c.session.get('in_cash_shop')))
        pkts = c.recv_until_quiet(0.3)
        self.assertEqual(pkts[-1].opcode, 0x6A, _ops(pkts))
        return pkts

    def mall(self, c, pkt):
        rec = c.s2c(pkt)
        if self.build == B9:
            self.assertEqual(rec['mode'], CASH.MODE_SINGLE)
        return rec

    def buy(self, c, *items, mileage=0, options=None):
        if self.build == B9:
            rows = [{'item_code': i, 'option': (options or {}).get(i, 0)} for i in items]
            key = BUY_ONE[B9] if len(items) == 1 else CART[B9]
            c.send_c2s(key, {'pay_with_mileage': mileage, 'item_count': len(items), 'repeat[item_count]': rows})
        elif len(items) == 1:
            c.send_c2s(BUY_ONE[B8], {'pay_with_mileage': mileage, 'item_count': 1, 'item_code': items[0]})
        else:
            c.send_c2s(CART[B8], {'pay_with_mileage': mileage, 'item_count': len(items),
                                  'repeat[item_count]': [{'item_code': i} for i in items]})
        pkts = c.recv_until_quiet(0.3)
        self.assertTrue(pkts and set(_ops(pkts)) == {0x6C}, _ops(pkts))
        return [c.s2c(p) for p in pkts]

    def close(self, c, to_box=(), to_char=(), flag=0):
        c.send_c2s(CLOSE[self.build], {'storage_deleted_flag': flag, 'to_storage_count': len(to_box),
                                        'to_character_count': len(to_char),
                                        'repeat[to_storage_count]': [{'item_serial': s} for s in to_box],
                                        'repeat[to_character_count]': [{'item_serial': s} for s in to_char]})
        self.assertTrue(_wait(lambda: c.session.get('in_world') and not c.session.get('in_cash_shop')))
        return c.recv_until_quiet(0.3)

    def unlock(self, c):
        """2008: the gift row's password dialog (C2S 0x51 {password, 0x1F9} -> 0x80 {1, 0x1F9});
        2009 has none (the flag is forced to 0), and needs nothing."""
        if self.build == B8:
            c.send_c2s(PASSWORD[B8], {'password': 'test', 'target_window_id': 0x1F9})
            self.assertEqual(c.s2c(c.expect(0x80)), {'result': 1, 'target_window_id': 0x1F9})

    def gift(self, c, item, to, message=b'hi', option=0):
        fields = {'pay_with_mileage': 0, 'item_code': item, 'recipient_name': to,
                  'message_len': len(message), 'message': message}
        if self.build == B9:
            fields['option'] = option
        c.send_c2s(GIFT[self.build], fields)
        return c.s2c(c.expect(0x71, quiet=0.2))

    def gift_ok(self, cash, mileage=0):
        want = {'result': 1, 'cash_balance': cash, 'mileage_balance': mileage}
        return {'is_trade': 0, **want} if self.build == B9 else want

    def gift_fail(self, code):
        return {'is_trade': 0, 'result': code} if self.build == B9 else {'result': code}

    # ------------------------------------------------------------------- enter ---
    def test_mall_enter_sends_0x6a_and_takes_the_player_off_the_map(self):
        """premium_cash-mall-enter: `!mall` -> the owned list, then 0x6A with the account's
        wallet and box in this build's layout; the session leaves its map like a map load
        (in_world False, off the monsters, the peer sees 0x06)."""
        a, b = self.two()
        self.wallet(cash=1000, mileage=7)
        note = self.server.cash.grant(self.hero, NOTE, 2)
        with self.server.store.lock:
            self.acc()['cash_box'].append(CASH.make_record(0x2001, SUPER_MEGA, 1, 1, origin=CASH.ORIGIN_CASH))
        pkts = self.enter_mall(a)
        self.assertEqual(_ops(pkts), [0x6F, 0x6A])
        entry = self.mall(a, pkts[1])
        self.assertEqual((entry['cash_balance'], entry['mileage_balance'], entry['box_count']), (1000, 7, 1))
        self.assertEqual([(r['serial'], r['item_id'], r['limit_type'], r['quantity'], r['origin'])
                          for r in entry['repeat[box_count]']], [(0x2001, SUPER_MEGA, 1, 1, 0)])
        s = a.session
        self.assertTrue(s['in_cash_shop'])
        self.assertFalse(s['in_world'])
        self.assertIsNone(self.server.world.map_of(s))
        self.assertEqual(s['current_map'], 101)                       # the way back
        self.assertEqual(s['mall']['back']['map'], 101)
        self.assertEqual(s['mall']['box'], {0x2001})
        self.assertEqual(s['mall']['char'], {note['serial']})
        self.assertEqual(b.s2c(b.expect(0x06)), {'uid': s['uid']})     # the peer loses it
        self.assertEqual(self.server.world.peers(b.session), [])

    def test_mall_is_every_players_command_and_refusals_are_one_line(self):
        a, b = self.two()
        pkts = self.enter_mall(b)                                       # Watcher is no GM
        self.assertEqual(_ops(pkts), [0x6A])                            # nothing owned: no 0x6F
        a.expect(0x06)
        self.assertEqual(self.server.mall.enter(b.session), 'You are in the '
                         f'{MALL.mall_name(self.build)} already.')
        a.session['dead'] = True
        self.line(a, b'!mall')
        self.assertEqual(_text(a.s2c(a.expect(0x15))['text']), '[Warning] Not while you are dead.')
        a.session['dead'] = False
        a.session['stall_client_selling'] = True
        self.assertEqual(self.server.mall.enter(a.session), 'Close your shop first.')
        a.session['stall_client_selling'] = False
        self.line(a, b'!mall now')
        self.assertIn('unknown argument', _text(a.s2c(a.expect(0x15))['text']))
        self.line(a, b'!mall status')
        lines = [_text(a.s2c(p)['text']) for p in a.recv_until_quiet(0.3)]
        self.assertTrue(lines[0].startswith(f'{MALL.mall_name(self.build)}: outside; Wind Cash 0'), lines)
        self.assertFalse(a.session.get('in_cash_shop'))

    # ------------------------------------------------------------------- close ---
    def test_close_applies_the_moves_idempotently_and_replays_the_map(self):
        """premium_cash-mall-exit: C2S 0x42 moves the records, 0x6B, then the whole map load
        back to where `!mall` was typed; a second close is ignored."""
        a, b = self.two()
        note = self.server.cash.grant(self.hero, NOTE, 2)
        box = CASH.make_record(0x2001, MEGAPHONE, 1, 5, origin=CASH.ORIGIN_CASH)
        costume = CASH.make_record(0x2002, COSTUME, 0, 1, origin=CASH.ORIGIN_CASH)
        with self.server.store.lock:
            self.acc()['cash_box'].extend([box, costume])
        self.enter_mall(a)
        b.expect(0x06)
        with self.assertLogs('WS', logging.WARNING) as logs:        # 0x7777 unknown, the costume stays
            pkts = self.close(a, to_box=[note['serial'], 0x7777], to_char=[0x2001, 0x2002, 0x2001])
        self.assertTrue(any('0x2002' in line and '0x7777' in line for line in logs.output), logs.output)
        self.assertEqual([op for op in _ops(pkts) if op != 0x04], [0x6B, 0x08, 0x03, 0x07, 0x28, 0x44, 0x6F])
        self.assertIn(0x04, _ops(pkts))                               # Watcher's record, back on 101
        self.assertEqual(struct_map(pkts[1]), 101)
        spawn = a.s2c(pkts[3])['repeat[player_count]'][0]
        # where `!mall` was typed: the server's estimate on the floor (map 101 x 700: the floor
        # line at 912 under the 812 spawn point), and the exit lands him ON it: every map load
        # settles its arrival point onto the floor (livetest bug 5)
        floor = W.presence.settle(101, 700.0, 812.0, W.presence.SLOPE_SLACK_PX)[1]
        self.assertEqual((spawn['pos_x'], spawn['pos_y']), (700.0, floor))
        owned = a.s2c(pkts[-1])['repeat[count]']
        self.assertEqual([(r['serial'], r['item_id'], r['quantity']) for r in owned], [(0x2001, MEGAPHONE, 5)])
        self.assertEqual(sorted(r['serial'] for r in self.acc()['cash_box']), sorted([0x2002, note['serial']]))
        self.assertEqual([r['serial'] for r in self.hero['cash_items']], [0x2001])
        self.assertTrue(a.session['in_world'])
        self.assertEqual(self.server.world.map_of(a.session), 101)
        self.assertNotIn('mall', a.session)
        b.expect(0x05)                                                  # the peer sees him again
        self.close_again(a)

    def test_a_full_cash_tab_or_box_leaves_the_record_where_it_was(self):
        """The client checks its 45 cash-bag slots before a move; the server does too, and a
        record it cannot take stays in the box (the exit 0x6F puts the client back in step)."""
        a = self.login()
        for _ in range(MALL.CASH_TAB_SLOTS):
            self.server.cash.grant(self.hero, 3321)                   # period items, not activated
        with self.server.store.lock:
            self.acc()['cash_box'].append(CASH.make_record(0x2001, MEGAPHONE, 1, 1))
        self.assertEqual(MALL.cash_tab_used(self.hero), MALL.CASH_TAB_SLOTS)
        self.enter_mall(a)
        with self.assertLogs('WS', logging.WARNING):
            pkts = self.close(a, to_char=[0x2001])
        self.assertEqual([r['serial'] for r in self.acc()['cash_box']], [0x2001])
        [owned] = [a.s2c(p) for p in pkts if p.opcode == 0x6F]
        self.assertNotIn(0x2001, [r['serial'] for r in owned['repeat[count]']])
        out = self.server.mall.apply_moves(a.session, [], [])        # an empty close moves nothing
        self.assertFalse(any(out.values()))

    def test_a_cash_0_box_record_stays_in_the_box(self):
        """P8 review minor: a Cash 0 record (a `!gift` gift certificate) never reaches the
        character's cash list, as grant() refuses it too - the client's 0x6F purges only Cash
        items before re-inserting the list, so every in-world re-send would duplicate its
        icon. The move is left undone and the exit 0x6F puts the client back in step."""
        a = self.login()
        with self.server.store.lock:
            self.acc()['cash_box'].extend([CASH.make_record(0x2001, 3327, 1, 1, origin=CASH.ORIGIN_GIFT),
                                           CASH.make_record(0x2002, MEGAPHONE, 1, 1)])
        self.enter_mall(a)
        with self.assertLogs('WS', logging.WARNING) as logs:
            pkts = self.close(a, to_char=[0x2001, 0x2002])
        self.assertTrue(any('0x2001' in line and 'not moved' in line for line in logs.output), logs.output)
        self.assertEqual([r['serial'] for r in self.acc()['cash_box']], [0x2001])
        self.assertEqual([r['serial'] for r in self.hero['cash_items']], [0x2002])
        [owned] = [a.s2c(p) for p in pkts if p.opcode == 0x6F]
        self.assertEqual([r['serial'] for r in owned['repeat[count]']], [0x2002])

    def close_again(self, a):
        with self.assertLogs('WS', logging.WARNING) as logs:
            a.send_c2s(CLOSE[self.build], {'storage_deleted_flag': 0, 'to_storage_count': 0, 'to_character_count': 0,
                                            'repeat[to_storage_count]': [], 'repeat[to_character_count]': []})
            a.expect_silence(0.3)
        self.assertTrue(any('outside the mall' in line for line in logs.output))

    def test_a_close_that_does_not_decode_still_replays(self):
        a = self.login()
        self.enter_mall(a)
        with self.assertLogs('WS', logging.WARNING):
            a.send(0x42, b'\x01\x05\x00')
            self.assertTrue(_wait(lambda: a.session.get('in_world')))
        self.assertEqual(_ops(a.recv_until_quiet(0.3))[:3], [0x6B, 0x08, 0x03])

    def test_a_disconnect_inside_the_mall_relogs_on_the_original_map(self):
        """The disconnect saves the map and the point `!mall` was typed at (not the warp's
        arrival, which the store held already), and the relog's own 0x07 lands there."""
        a = self.login()
        self.line(a, b'!warp 102 300 700')
        self.assertTrue(_wait(lambda: self.server.world.map_of(a.session) == 102))
        a.recv_until_quiet(0.3)
        arrival = (self.hero['x'], self.hero['y'])
        spot = W.presence.settle(102, 520.0, 700.0, W.presence.SLOPE_SLACK_PX)    # on the floor
        self.assertNotEqual(spot, arrival)
        a.session['pos'] = spot                                   # as a walk's C2S 0x0D would
        self.enter_mall(a)
        a.close()
        self.assertTrue(_wait(lambda: a.session is None))
        stored = self.server.store.find_character('test', 'TestHero')
        self.assertEqual((stored['map'], stored['x'], stored['y']), (102, *spot))
        c = self.login()
        self.assertEqual(c.session['current_map'], 102)
        [spawn07] = [p for p in c.entry if p.opcode == 0x07]
        me = c.s2c(spawn07)['repeat[player_count]'][0]
        self.assertEqual((me['pos_x'], me['pos_y']), spot)

    def test_a_gift_committed_before_the_0x6a_is_not_pushed_again(self):
        """P8 review: gift() commits under store.lock only. When the recipient's enter()
        takes its 0x6A snapshot after that commit but before the sender's _push_gift reaches
        Mall.lock, the 0x6A lists the record already: the push sends no 0x79 (it appends
        without a duplicate check, so the row showed twice) - and its 0x6D went out with the
        0x6A's own gift queue."""
        a, b = self.two()
        pushes = []
        real_push = self.server.mall._push_gift
        with mock.patch.object(self.server.mall, '_push_gift', lambda *args: pushes.append(args)):
            username, rec = self.server.mall.dev_gift('TestHero', self.server.store.character_by_name('Watcher'),
                                                      MEGAPHONE, b'race')
        self.assertEqual(pushes, [('admin', rec)])
        pkts = self.enter_mall(b)                                    # the snapshot has the record
        self.assertEqual(_ops(pkts)[-2:], [0x6D, 0x6A])
        self.assertIn(rec['serial'], [r['serial'] for r in self.mall(b, pkts[-1])['repeat[box_count]']])
        self.assertTrue(real_push(username, rec))                     # the sender's push, late
        b.expect_silence(0.3)
        self.assertIn(rec['serial'], b.session['mall']['box'])
        # a gift committed after the 0x6A still gets its 0x79 (and joins the snapshot)
        _, later = self.server.mall.dev_gift('TestHero', self.server.store.character_by_name('Watcher'),
                                             SUPER_MEGA, b'later')
        add, queued = b.expect(0x79, 0x6D)
        self.assertEqual(b.s2c(add)['serial'], later['serial'])
        self.assertIn(later['serial'], b.session['mall']['box'])

    def test_mall_lock_is_never_held_across_a_socket_write(self):
        """P8 review: Mall.lock is server-wide, so every send made under it only queues
        (flush=False) and is written after the lock; and a map load of an account with no
        gift pending never takes the lock at all."""
        a, b = self.two()
        mall, calls = self.server.mall, []
        real_send = mall._send

        def spy(sock, session, key, fields, flush=None):
            calls.append((key, flush, mall.lock._is_owned()))
            return real_send(sock, session, key, fields, flush)

        with mock.patch.object(mall, '_push_gift', lambda *args: None):      # queued at the entry
            mall.dev_gift('Watcher', self.server.store.character_by_name('TestHero'), MEGAPHONE, b'x')
        with mock.patch.object(mall, '_send', spy):
            pkts = self.enter_mall(a)
        self.assertEqual(_ops(pkts)[-2:], [0x6D, 0x6A])
        locked = [(key, flush) for key, flush, held in calls if held]
        self.assertEqual(locked, [('0x6D', False), ('0x6A', False)])
        self.close(a)
        taken, real_lock = [], mall.lock

        class Spy:
            def __enter__(self):
                taken.append(1)
                return real_lock.__enter__()

            def __exit__(self, *exc):
                return real_lock.__exit__(*exc)

        self.assertFalse(any(not g['delivered'] for g in self.acc()['gift_inbox']))
        with mock.patch.object(mall, 'lock', Spy()):
            self.line(a, b'!warp 102')                        # a map load, nothing pending
            self.assertTrue(_wait(lambda: self.server.world.map_of(a.session) == 102))
            a.recv_until_quiet(0.3)
            self.assertEqual(mall.send_gift_queue(b.session.get('sock'), b.session), [])
        self.assertEqual(taken, [])

    def test_a_gift_to_a_closed_connection_is_kept_for_the_next_login(self):
        """P8 review: the recipient's outbox closed after the online check - the push is
        dropped quietly (no OSError out of the gift, which is committed) and the entry stays
        undelivered for his next login."""
        a, b = self.two()
        b.session['outbox'].close()
        username, rec = self.server.mall.dev_gift('TestHero', self.server.store.character_by_name('Watcher'),
                                                  MEGAPHONE, b'later')
        self.assertEqual(username, 'admin')
        self.assertEqual([(g['serial'], g['delivered']) for g in self.acc('admin')['gift_inbox']],
                         [(rec['serial'], False)])

    def test_the_first_purchase_popup_of_the_0x02_is_cleared_by_the_0x6a(self):
        """P8 review: premium_cash.md 1.1 - the 0x02's cash_first_purchase_flag shows its popup
        at the next 0x6A, which clears it; the account's flag goes then too, so the next login's
        0x02 no longer carries it."""
        with self.server.store.lock:
            self.acc()['first_purchase_notice'] = True
        c = F.FakeClient(self.server)
        self.clients.append(c)
        self.assertEqual(c.login('test', 'test')['cash_first_purchase_flag'], 1)
        c.entry = c.enter_world('TestHero', port=F.P2P_PORT_BASE)
        self.assertTrue(self.acc()['first_purchase_notice'])       # the map load does not clear it
        self.enter_mall(c)
        self.assertFalse(self.acc()['first_purchase_notice'])
        self.assertTrue(self.server.store.dirty)
        self.close(c)
        c.close()
        self.assertTrue(_wait(lambda: c.session is None))
        again = F.FakeClient(self.server)
        self.clients.append(again)
        self.assertEqual(again.login('test', 'test')['cash_first_purchase_flag'], 0)

    def test_mall_entry_waits_out_the_portal_cooldown_and_the_chat_rate(self):
        """P8 review: every exit is a full map load, so `!mall` waits PORTAL_COOLDOWN_SECS after
        a map change (the exit's included), and a player's command line costs a chat-rate slot
        (5 lines in 3 s): a patched client cannot loop `!mall` + 0x42 into back-to-back loads."""
        a, b = self.two()
        self.server.PORTAL_COOLDOWN_SECS = 2.0
        b.session['last_transfer_t'] = time.monotonic()
        self.line(b, b'!mall')
        self.assertIn('wait a moment', _text(b.s2c(b.expect(0x15))['text']))
        self.assertFalse(b.session.get('in_cash_shop'))
        b.session['last_transfer_t'] = 0.0
        with self.assertLogs('WS', logging.INFO) as logs:
            for _ in range(6):
                self.line(b, b'!mall status')
            lines = b.recv_until_quiet(0.4)
        self.assertEqual(len(lines), 4)                                # 1 slot went to the refusal
        self.assertEqual(sum('dropped command line' in line for line in logs.output), 2)

    # ----------------------------------------------------------------- refresh ---
    def test_each_0x46_gets_exactly_one_0x70(self):
        """premium_cash-balance-refresh: every minimise / restore after the charge button
        sends one 0x46 until a 0x70 clears the flag - one 0x70 per request, never more; the
        owed first-purchase popup rides on the first."""
        a = self.login()
        self.wallet(cash=321, mileage=12)
        with self.server.store.lock:
            self.acc()['first_purchase_notice'] = True
        self.enter_mall(a)
        for bonus in (1, 0):
            a.send_c2s(REFRESH[self.build], {})
            self.assertEqual(a.s2c(a.expect(0x70)),
                             {'cash_balance': 321, 'mileage_balance': 12, 'first_purchase_bonus': bonus})
        self.assertFalse(self.acc()['first_purchase_notice'])
        self.close(a)
        a.send_c2s(REFRESH[self.build], {})                             # outside the mall too
        self.assertEqual(a.s2c(a.expect(0x70))['cash_balance'], 321)
        self.assertEqual(a.session['mall_refreshes'], 3)

    # --------------------------------------------------------------------- buy ---
    def test_buy_uses_the_server_price_and_boxes_the_record(self):
        """premium_cash-buy: one 0x6C per item with the running balances; the record goes to
        the account box (origin 0 cash / 1 mileage); nothing moves to the character."""
        a = self.login()
        self.wallet(cash=1000, mileage=600)
        self.enter_mall(a)
        [one] = self.buy(a, MEGAPHONE)
        self.assertEqual((one['result'], one['cash_balance'], one['mileage_balance'], one['item_id'],
                          one['item_kind'], one['quantity'], one['origin'], one['expire_time']),
                         (1, 900, 600, MEGAPHONE, 1, 1, 0, bytes(16)))
        cart = self.buy(a, SUPER_MEGA, NOTE)
        self.assertEqual([(r['cash_balance'], r['item_id']) for r in cart], [(400, SUPER_MEGA), (370, NOTE)])
        [m] = self.buy(a, SUPER_MEGA, mileage=1)
        self.assertEqual((m['cash_balance'], m['mileage_balance'], m['origin']), (370, 100, CASH.ORIGIN_MILEAGE))
        box = self.acc()['cash_box']
        self.assertEqual([(r['serial'], r['item_id'], r['origin']) for r in box],
                         [(one['item_serial'], MEGAPHONE, 0), (cart[0]['item_serial'], SUPER_MEGA, 0),
                          (cart[1]['item_serial'], NOTE, 0), (m['item_serial'], SUPER_MEGA, 1)])
        self.assertEqual(len({r['serial'] for r in box}), 4)
        self.assertEqual((self.acc()['cash'], self.acc()['mileage']), (370, 100))
        self.assertEqual(self.hero['cash_items'], [])
        self.assertTrue(self.acc()['first_purchase_done'])
        self.server.store.flush()
        import json
        with open(self.server.store.path, encoding='utf-8') as f:
            self.assertEqual(len(json.load(f)['test']['cash_box']), 4)

    def test_buy_refusals(self):
        a = self.login()
        self.wallet(cash=150, mileage=50)
        self.enter_mall(a)
        self.assertEqual(self.buy(a, SUPER_MEGA), [{'result': MALL.BUY_SHORT}])          # 500 > 150
        self.assertEqual(self.buy(a, MEGAPHONE, SUPER_MEGA), [{'result': MALL.BUY_SHORT}])
        self.assertEqual(self.buy(a, COSTUME), [{'result': MALL.BUY_FAILED}])             # Type 1: not modelled
        self.assertEqual(self.buy(a, STICK), [{'result': MALL.BUY_FAILED}])               # not sold
        self.assertEqual(self.buy(a, 3327), [{'result': MALL.BUY_FAILED}])                # gift card: Cash 0
        if self.build == B9:
            self.assertEqual(self.buy(a, MEGAPHONE, options={MEGAPHONE: 1}), [{'result': MALL.BUY_FAILED}])
            # the 2009 client's own fallback: mileage short -> its cash check passed -> cash pays
            [r] = self.buy(a, MEGAPHONE, mileage=1)
            self.assertEqual((r['cash_balance'], r['mileage_balance'], r['origin']), (50, 50, CASH.ORIGIN_CASH))
        else:
            self.assertEqual(self.buy(a, MEGAPHONE, mileage=1), [{'result': MALL.BUY_SHORT}])
        self.assertEqual(len(self.acc()['cash_box']), 1 if self.build == B9 else 0)
        self.close(a)
        a.send_c2s(BUY_ONE[self.build], {'pay_with_mileage': 0, 'item_count': 1, 'item_code': MEGAPHONE,
                                          'repeat[item_count]': [{'item_code': MEGAPHONE, 'option': 0}]})
        self.assertEqual(a.s2c(a.expect(0x6C)), {'result': 0})                            # outside the mall

    def test_the_box_never_outgrows_one_0x6a_frame(self):
        a = self.login()
        self.wallet(cash=10000)
        with self.server.store.lock:
            self.acc()['cash_box'].extend(CASH.make_record(0x3000 + i, MEGAPHONE, 1, 1)
                                          for i in range(MALL.BOX_MAX - 1))
        self.enter_mall(a)
        self.assertEqual(len(self.buy(a, MEGAPHONE)), 1)                                   # the 72nd
        self.assertEqual(self.buy(a, MEGAPHONE), [{'result': MALL.BUY_FAILED}])

    def test_slot_extension_adds_5_capped_at_45_and_survives_the_map_load(self):
        """premium_cash-buy slot extensions (exit criterion 6): +5 per purchase, applied (not
        boxed), refused past the client cap 45 (bank 60), in the next 0x03 / the bank model."""
        a = self.login()
        self.wallet(cash=100000)
        self.enter_mall(a)
        caps = []
        for _ in range(2):
            [r] = self.buy(a, TAB_EXT)
            self.assertEqual((r['result'], r['item_id']), (1, TAB_EXT))
            caps.append(W.invmod.Inventory(self.hero).capacities()[0])
        self.assertEqual(caps, [40, 45])
        self.assertEqual(self.buy(a, TAB_EXT), [{'result': MALL.BUY_FAILED}])              # 45 + 5 > 45
        rows = self.buy(a, BANK_EXT, BANK_EXT)                                            # a cart of two
        self.assertEqual([(r['result'], r['item_id'], r['cash_balance']) for r in rows],
                         [(1, BANK_EXT, 100000 - 3 * PRICE[TAB_EXT]), (1, BANK_EXT, 100000 - 4 * PRICE[TAB_EXT])])
        self.assertEqual(self.hero['bank_slots'], [45, 35, 35])
        self.assertEqual(self.acc()['cash_box'], [])                                      # nothing stored
        self.assertEqual(self.acc()['cash'], 100000 - 4 * PRICE[TAB_EXT])
        pkts = self.close(a)
        self.assertEqual(a.s2c([p for p in pkts if p.opcode == 0x03][0])['equip_tab_slots'], 45)
        self.line(a, b'!warp 102')                                                       # "survives portal"
        self.assertTrue(_wait(lambda: self.server.world.map_of(a.session) == 102))
        pkts = a.recv_until_quiet(0.3)
        self.assertEqual(a.s2c([p for p in pkts if p.opcode == 0x03][0])['equip_tab_slots'], 45)

    # ------------------------------------------------------------------ delete ---
    def test_delete_refunds_30_percent_mileage(self):
        a = self.login()
        self.wallet(cash=1000)
        self.enter_mall(a)
        [cashed] = self.buy(a, SUPER_MEGA)
        self.wallet(mileage=500)
        [mileaged] = self.buy(a, SUPER_MEGA, mileage=1)
        a.send_c2s(DELETE[self.build], {'item_code': SUPER_MEGA, 'cash_item_serial': cashed['item_serial']})
        self.assertEqual(a.s2c(a.expect(0x6E)), {'result': 1, 'mileage_balance': 150,
                                                 'item_serial': cashed['item_serial']})
        a.send_c2s(DELETE[self.build], {'item_code': SUPER_MEGA, 'cash_item_serial': mileaged['item_serial']})
        self.assertEqual(a.s2c(a.expect(0x6E))['mileage_balance'], 150)                   # no refund
        for item, serial in ((SUPER_MEGA, cashed['item_serial']), (MEGAPHONE, 0x9999)):
            a.send_c2s(DELETE[self.build], {'item_code': item, 'cash_item_serial': serial})
            self.assertEqual(a.s2c(a.expect(0x6E)), {'result': 0})
        self.assertEqual(self.acc()['cash_box'], [])
        self.assertEqual(self.acc()['mileage'], 150)

    # -------------------------------------------------------------------- gift ---
    def test_gift_reaches_the_recipient_box_and_pops_at_his_next_mall_entry(self):
        """premium_cash-gift + chat_mail_gm-gift-inbox (exit criterion 5): A pays, B's box
        gets the record (origin 2), B in the world gets the 0x6D now, its popup opens at the
        end of B's next 0x6A, which marks it delivered."""
        a, b = self.two()
        self.wallet(cash=1000)
        self.enter_mall(a)
        b.expect(0x06)
        self.unlock(a)
        self.assertEqual(self.gift(a, MEGAPHONE, 'watcher', b'enjoy'), self.gift_ok(900))
        queued = b.s2c(b.expect(0x6D))
        self.assertEqual(queued['gift_count'], 1)
        row = queued['repeat[gift_count]'][0]
        self.assertEqual((_text(row['sender_name']), _text(row['message']), row['item_id']),
                         ('TestHero', 'enjoy', MEGAPHONE))
        [rec] = self.acc('admin')['cash_box']
        self.assertEqual((rec['item_id'], rec['origin']), (MEGAPHONE, CASH.ORIGIN_GIFT))
        self.assertEqual(self.acc('admin')['gift_inbox'],
                         [{'sender': 'TestHero', 'message': 'enjoy', 'item_id': MEGAPHONE,
                           'serial': rec['serial'], 'delivered': False}])
        self.assertEqual(self.acc()['cash'], 900)
        pkts = self.enter_mall(b)
        self.assertEqual(_ops(pkts), [0x6A])                    # queued already: no second 0x6D
        self.assertEqual([r['serial'] for r in self.mall(b, pkts[0])['repeat[box_count]']], [rec['serial']])
        self.assertTrue(self.acc('admin')['gift_inbox'][0]['delivered'])
        self.assertEqual(b.session['gifts_on_client'], [])

    def test_gift_to_a_recipient_inside_the_mall_adds_to_his_box(self):
        a, b = self.two()
        self.wallet(cash=1000)
        self.enter_mall(b)
        a.expect(0x06)
        self.enter_mall(a)
        self.unlock(a)
        self.assertEqual(self.gift(a, SUPER_MEGA, 'Watcher'), self.gift_ok(500))
        add, queued = b.expect(0x79, 0x6D)
        rec = self.acc('admin')['cash_box'][0]
        self.assertEqual((b.s2c(add)['serial'], b.s2c(add)['item_id']), (rec['serial'], SUPER_MEGA))
        self.assertEqual(b.s2c(queued)['gift_count'], 1)
        # B's close reports the gifted record as "now in storage": a no-op
        self.close(b, to_box=[rec['serial']])
        self.assertEqual([r['serial'] for r in self.acc('admin')['cash_box']], [rec['serial']])

    def test_gift_result_codes(self):
        a, b = self.two()
        self.wallet(cash=150)
        self.enter_mall(a)
        b.expect(0x06)
        self.unlock(a)
        self.assertEqual(self.gift(a, MEGAPHONE, 'Nobody'), self.gift_fail(MALL.GIFT_NO_CHAR))
        self.unlock(a)
        self.assertEqual(self.gift(a, MEGAPHONE, 'Alt'), self.gift_fail(MALL.GIFT_OWN_CHAR))
        self.unlock(a)
        self.assertEqual(self.gift(a, HAIR_M, 'Watcher'), self.gift_fail(MALL.GIFT_GENDER))   # male-only, flag 0
        self.unlock(a)
        self.assertEqual(self.gift(a, SUPER_MEGA, 'Watcher'), self.gift_fail(MALL.GIFT_SHORT))
        self.unlock(a)
        self.assertEqual(self.gift(a, COSTUME, 'Watcher'), self.gift_fail(MALL.GIFT_FAILED))
        self.unlock(a)
        self.assertEqual(self.gift(a, TAB_EXT, 'Watcher'), self.gift_fail(MALL.GIFT_FAILED))
        if self.build == B9:
            self.assertEqual(self.gift(a, MEGAPHONE, 'Watcher', option=1), self.gift_fail(MALL.GIFT_FAILED))
        else:
            # 2008: the password dialog comes first; without a fresh unlock the gift fails, and
            # one unlock pays for one gift
            self.assertEqual(self.gift(a, MEGAPHONE, 'Watcher'), self.gift_fail(MALL.GIFT_FAILED))
            self.unlock(a)
            a.session['pw_ok'][0x1F9] -= MALL.GIFT_PASSWORD_SECS + 1
            self.assertEqual(self.gift(a, MEGAPHONE, 'Watcher'), self.gift_fail(MALL.GIFT_FAILED))
            self.unlock(a)
        self.assertEqual(self.gift(a, MEGAPHONE, 'Watcher'), self.gift_ok(50))
        b.expect(0x6D)
        self.assertEqual(len(self.acc('admin')['cash_box']), 1)
        self.assertEqual(self.acc()['cash'], 50)

    def test_gift_inbox_once_per_connection_relit_after_a_map_load(self):
        """chat_mail_gm-gift-inbox: a gift for an offline account is queued on its enter world
        (after the 0x6F slot), relit with a count-0 0x6D on the next map load (C20), never
        re-sent on the same connection, delivered at the 0x6A and not sent to a later one."""
        a = self.login()
        self.line(a, b'!gift Late 3381 welcome')
        self.assertIn('Gift 3381 stored for late', _text(a.s2c(a.expect(0x15))['text']))
        late = self.login('late', 'late', 'Late', port=F.P2P_PORT_BASE + 2)
        ops = _ops(late.entry)
        self.assertIn(0x6D, ops)
        self.assertLess(ops.index(0x44), ops.index(0x6D))
        gift = late.s2c(late.entry[ops.index(0x6D)])
        self.assertEqual((gift['gift_count'], gift['repeat[gift_count]'][0]['item_id']), (1, MEGAPHONE))
        late.send(0x7E, bytes.fromhex('17000000'))                    # portal 101 -> 102
        self.assertTrue(_wait(lambda: self.server.world.map_of(late.session) == 102))
        pkts = late.recv_until_quiet(0.3)
        relit = [late.s2c(p) for p in pkts if p.opcode == 0x6D]
        self.assertEqual(relit, [{'gift_count': 0, 'repeat[gift_count]': []}])
        self.assertEqual(_ops(self.enter_mall(late)), [0x6A])
        self.assertTrue(self.acc('late')['gift_inbox'][0]['delivered'])
        pkts = self.close(late)
        self.assertNotIn(0x6D, _ops(pkts))
        late.close()
        self.assertTrue(_wait(lambda: late.session is None))
        again = self.login('late', 'late', 'Late', port=F.P2P_PORT_BASE + 2)
        self.assertNotIn(0x6D, _ops(again.entry))

    def test_legacy_and_undisplayable_inbox_entries(self):
        """A P5 `!gift` entry (serial 0) gets its box record when it is queued; an entry the
        client's popup could never show (not a cash item / a bad sender) becomes a memo."""
        with self.server.store.lock:
            self.acc('late')['gift_inbox'] = [
                {'sender': 'GM', 'message': 'old', 'item_id': MEGAPHONE, 'serial': 0, 'delivered': False},
                {'sender': 'GM', 'message': 'stick', 'item_id': STICK, 'serial': 0, 'delivered': False},
                {'sender': 'Two Words', 'message': 'x', 'item_id': MEGAPHONE, 'serial': 0, 'delivered': False}]
        with self.assertLogs('WS', logging.WARNING):
            late = self.login('late', 'late', 'Late')
        gifts = [late.s2c(p) for p in late.entry if p.opcode == 0x6D]
        self.assertEqual([r['item_id'] for g in gifts for r in g['repeat[gift_count]']], [MEGAPHONE])
        [entry] = self.acc('late')['gift_inbox']
        [rec] = self.acc('late')['cash_box']
        self.assertEqual((entry['serial'], rec['item_id'], rec['origin']), (rec['serial'], MEGAPHONE, CASH.ORIGIN_GIFT))
        memos = self.server.store.find_character('late', 'Late')['memos']
        self.assertEqual(len(memos), 2)
        self.assertIn('Wooden Stick', memos[0]['text'])

    def test_dev_gift_is_a_real_gift(self):
        a, b = self.two()
        self.line(a, b'!gift Watcher 3381 from the gm')
        self.assertIn('Gift 3381 stored for admin', _text(a.s2c(a.expect(0x15))['text']))
        self.assertEqual(b.s2c(b.expect(0x6D))['repeat[gift_count]'][0]['item_id'], MEGAPHONE)
        self.line(a, b'!gift Watcher 179 no')
        self.assertIn('not a gift', _text(a.s2c(a.expect(0x15))['text']))
        self.assertEqual(len(self.acc('admin')['cash_box']), 1)


def struct_map(pkt):
    """The map code of a map-load lead (2008 0x08 u16 first; 2009 u8 reason first)."""
    import struct
    return struct.unpack_from('<H', pkt.payload, 1 if len(pkt.payload) == 7 else 0)[0]


class MallWorld2008(_MallWorld, unittest.TestCase):
    build = B8


class MallWorld2009(_MallWorld, unittest.TestCase):
    build = B9


if __name__ == '__main__':
    unittest.main()
