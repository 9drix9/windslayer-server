#!/usr/bin/env python3
"""
test_shopbank.py - NPC shops and the bank, both client builds (P4 stage 1: shop_storage-
en-content, -npc-buy, -bank-model, -password-gate (+premium_cash-password-gate), -bank-gold,
-bank-items, -bank-fallback, -cleanup; shop_storage.md 1.2-1.5, F1-F7, 3.6)

Three layers:
- content: the EN merchant lists of each client build (hs/windslayer.hni `UI: 13`, 56 in
  both), the bank NPC and the 2009 guild NPC;
- the pure rules: shop.py (FUN_00465a80 listing, FUN_00467680 / 2009 FUN_00471450 price and
  discount, the 0/25/50 bank fee) and bank_tabs.py (FUN_00427740 / 00427940 / 00427610 /
  00427B70 and the 2009 FUN_00428eb0 etc-tab differences) with their quirks;
- the server through fakeclient for BOTH builds (no port, no game client, temp accounts.json):
  buy at an EN merchant with the discount, a skill book that lands in the skill list and not
  the bag, selling 300 of a stack, the password gate, deposit/withdraw of items and gold,
  every refusal's resync, relog, `!bank` / `/bank`, and the 2009 enter-world 0x65.
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

import bank_tabs as BT  # noqa: E402
import config as cfgmod  # noqa: E402
import en_content as EC  # noqa: E402
import fakeclient as F  # noqa: E402
import inventory as INV  # noqa: E402
import packets as P  # noqa: E402
import progression  # noqa: E402
import shop as SH  # noqa: E402
import skills as SK  # noqa: E402

W = F.import_server()
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = '2008', '2009'
CLIENT_DIR_2009 = cfgmod.DEFAULTS['CLIENT_DIR_2009']
HAVE_2008 = os.path.exists(EC.hs_path('windslayer.hii'))
HAVE_2009 = os.path.exists(os.path.join(HERE, CLIENT_DIR_2009, 'hs', 'windslayer.hii'))
needs_2008 = unittest.skipUnless(HAVE_2008, 'needs the EN 2008 client data (hs/)')
needs_2009 = unittest.skipUnless(HAVE_2009, 'needs the EN 2009 client data (CLIENT_DIR_2009)')

# EN content both builds share (hni idx; shop_storage.md 3.4)
MISTY, CHRISTINA, SALLY, DUINUKE, BANKER, PUPU = 8, 25, 43, 148, 97, 1
BLUE_MUSHROOM, HERB, ELIXIR, STICK = 3, 5, 1270, 179      # 1270: PMoney 10 (a Victy price)
IAP1, IAP2 = 292, 293                                       # Increase Attack Power Lv1/Lv2
DOUBLE_JUMP, OPEN_STALL = 94, 194                           # single-level families, 100 Victy
GOLD = W.storemod.DEFAULT_GOLD

# C2S send-site keys per build
KEYS = {
    B8: {'login': '0x44D8BF/0x01', 'enter': '0x42F904/0x2B', 'buy': '0x469D9C/0x0B',
         'sell': '0x46A679/0x0C', 'dep_item': '0x469D9C/0x3C', 'wd_equip': '0x469BA8/0x3D',
         'wd_item': '0x469D9C/0x3D', 'dep_gold': '0x469D9C/0x3E', 'wd_gold': '0x469D9C/0x3F',
         'password': '0x460831/0x51', 'chat': '0x445CA7/0x03'},
    B9: {'login': '0x451CE5/0x01', 'enter': '0x4315D7/0x2B', 'buy': '0x4745A4/0x0B',
         'sell': '0x4747C9/0x0C', 'dep_item': '0x473E14/0x3C', 'wd_equip': '0x473E14/0x3D',
         'wd_item': '0x473E14/0x3D', 'dep_gold': '0x4751F0/0x3E', 'wd_gold': '0x473E14/0x3F',
         'password': '0x468F05/0x51', 'chat': '0x44790E/0x03'},
}
ACCOUNTS = {
    'test': {'password': 'test',
             'characters': [{'name': 'TestHero', 'level': 1, 'class': 0, 'map': 101,
                             'x': 1411, 'y': 714, 'hp': 100, 'mp': 50, 'gm': 1}]},
    'admin': {'password': 'admin',
              'characters': [{'name': 'Plain', 'level': 1, 'class': 0, 'map': 101,
                              'x': 1411, 'y': 714, 'hp': 100, 'mp': 50}]},
}
_LIVE_HASH = None


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def setUpModule():
    global _LIVE_HASH
    _LIVE_HASH = _sha(LIVE_ACCOUNTS) if os.path.exists(LIVE_ACCOUNTS) else None


def tearDownModule():
    EC.configure(cfgmod.DEFAULTS['CLIENT_DIR'], B8)
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_shopbank.py (tests must only use temp copies)'


def use_build(build):
    """Point en_content at a build's client files (the server does the same on start)."""
    EC.configure(cfgmod.DEFAULTS['CLIENT_DIR_2009'] if build == B9 else cfgmod.DEFAULTS['CLIENT_DIR'], build)


# ================================================================== content ===
class ShopContent(unittest.TestCase):
    """shop_storage-en-content: the merchant lists the client itself lists (FUN_00465a80)."""

    def tearDown(self):
        use_build(B8)

    def check_build(self, build):
        use_build(build)
        npcs = EC.npcs()
        merchants = npcs.merchants()
        self.assertEqual(len(merchants), 56)
        self.assertEqual(EC.shop_list(MISTY)[:9], [3, 5, 6, 7, 51, 17, 614, 1270, 1340])
        self.assertEqual(EC.shop_list(DUINUKE), [94, 80, 88, 90, 194, 86, 82, 2188])
        self.assertIn(IAP1, EC.shop_list(CHRISTINA))
        self.assertEqual(EC.shop_list(7), [])                        # Smith: a merchant, no stock
        self.assertIsNone(EC.shop_list(PUPU))                        # a monster is no merchant
        self.assertIsNone(EC.shop_list(BANKER))
        self.assertEqual(npcs.get(BANKER).ui, EC.UI_BANK)
        items = EC.items()
        for d in merchants:
            with self.subTest(build=build, npc=d.idx):
                for i in d.shop_items:
                    self.assertTrue(items.exists(i), i)
                    self.assertFalse(items.get(i).is_cash, i)                # never sold by an NPC
                    self.assertNotEqual(items.get(i).type, EC.TYPE_CLASS_CHANGE, i)
        return npcs

    @needs_2008
    def test_2008_merchants(self):
        self.check_build(B8)

    @needs_2009
    def test_2009_merchants_and_the_guild_npc(self):
        npcs = self.check_build(B9)
        self.assertEqual(npcs.get(181).ui, EC.UI_GUILD_NPC_2009)          # Moiba
        self.assertIsNone(EC.shop_list(181))

    def test_constants(self):
        self.assertEqual((EC.UI_NPC_SHOP, EC.UI_BANK, EC.STALL_MAP, EC.BANK_DEFAULT_SLOTS),
                         (13, 0x1A7, 9701, 35))


# ============================================================== pure rules ===
@needs_2008
class ShopRules(unittest.TestCase):
    """shop.py against FUN_00465a80 / FUN_00467680 / FUN_00471450 / FUN_00469090."""

    def test_skill_rows_show_the_next_level_only(self):
        self.assertEqual(SH.shown_id(IAP1, []), IAP1)
        self.assertEqual(SH.shown_id(IAP1, [IAP1]), IAP2)
        self.assertEqual(SH.shown_id(IAP1, [IAP1 + 4]), IAP1 + 5)
        self.assertEqual(SH.shown_id(IAP1, [IAP1 + 9]), IAP1 + 9)               # the top level stays
        self.assertEqual(SH.shown_id(OPEN_STALL, [OPEN_STALL]), OPEN_STALL)     # single-level family
        self.assertEqual(SH.shown_id(HERB, [HERB]), HERB)                       # not a skill
        stock = EC.shop_list(CHRISTINA)
        self.assertEqual(SH.offered(stock, IAP1, []), IAP1)
        self.assertIsNone(SH.offered(stock, IAP2, []))                         # not shown yet
        self.assertEqual(SH.offered(stock, IAP2, [IAP1]), IAP1)
        self.assertIsNone(SH.offered(stock, IAP1, [IAP1]))
        self.assertEqual(SH.family_row(stock, IAP1 + 9), IAP1)
        self.assertIsNone(SH.family_row(stock, IAP1 + 10))
        self.assertIsNone(SH.offered(EC.shop_list(MISTY), IAP1, []))

    def test_price_discount_and_rounding(self):
        self.assertEqual(SH.discount(B8, 0, 199), 1.0)
        self.assertEqual(SH.discount(B8, 0, 200), 0.9)
        self.assertEqual(SH.discount(B8, EC.TYPE_SKILL, 500), 1.0)            # skills never
        self.assertEqual(SH.discount(B8, 0, 500, npc=False), 1.0)             # stalls never
        self.assertEqual(SH.cost(50, 3, 0.9), 135)
        self.assertEqual(SH.cost(5, 1, 0.9), 4)                               # 4.5 -> half to even
        self.assertEqual(SH.cost(7, 1, 0.9), 6)                               # 6.3
        self.assertEqual(SH.cost(0, 999, 0.9), 0)                             # free stays free
        self.assertEqual(SH.cost(1, 1, 0.4), 1)                               # a discount never makes 0
        self.assertEqual(SH.cost(0xFFFFFFFF, 999, 1.0), 0xFFFFFFFF * 999)     # u64, no 32-bit wrap
        # 2009 FUN_00471450: x0.95 per equip slot 1 / 2 whose option word 0 is 2
        grid = {1: {'id': 500, 'w': [2, 0, 0, 0, 0, 0]}, 2: {'id': 501, 'w': [2, 0, 0, 0, 0, 0]}}
        self.assertEqual(SH.discount(B8, 0, 0, grid), 1.0)
        self.assertAlmostEqual(SH.discount(B9, 0, 0, grid), 0.95 * 0.95)
        self.assertAlmostEqual(SH.discount(B9, 0, 200, {1: grid[1]}), 0.9 * 0.95)
        self.assertEqual(SH.discount(B9, 0, 0, {1: {'id': 500, 'w': [3, 0, 0, 0, 0, 0]}}), 1.0)
        self.assertEqual(SH.discount(B9, EC.TYPE_SKILL, 500, grid), 1.0)

    def test_bank_fee(self):
        for manner, fee in ((500, 0), (100, 0), (99, 25), (30, 25), (29, 50), (0, 50), (-80, 50)):
            self.assertEqual(SH.bank_fee(manner), fee, manner)


CAPS = [35, 35, 35]


def arr(values, fill=0):
    return list(values) + [fill] * (BT.SLOTS - len(values))


class BankTabs(unittest.TestCase):
    """bank_tabs ports, including the client quirks shop_storage.md 3.6 lists."""

    def test_consume_add_merges_into_the_first_open_stack_and_spills(self):
        ok, ids, q = BT.consume_change(CAPS, arr([3, 3]), arr([500, 10]), 3, 400)
        self.assertTrue(ok)
        self.assertEqual((ids[:2], q[:2]), ([3, 3], [900, 10]))
        ok, ids, q = BT.consume_change(CAPS, arr([3, 5]), arr([900, 1]), 3, 150)
        self.assertEqual((ok, ids[:3], q[:3]), (True, [3, 5, 3], [999, 1, 51]))

    def test_a_failed_spill_still_writes_999_and_the_precheck_is_looser(self):
        caps = [35, 2, 35]
        ok, ids, q = BT.consume_change(caps, arr([3, 3]), arr([998, 10]), 3, 5)
        self.assertFalse(ok)
        self.assertEqual(q[:2], [999, 10])                 # the client leaves that write behind
        tabs = {'equip': (arr([]), arr([], BT.ZERO)), 'consume': (arr([3, 3]), arr([998, 10])),
                'etc': (arr([]), arr([]))}
        self.assertTrue(BT.space_ok(caps, tabs, EC.TYPE_CONSUMABLE, 3, 5))   # slot 1: 10 + 5
        self.assertFalse(BT.space_ok(caps, tabs, EC.TYPE_CONSUMABLE, 3, 1000))

    def test_remove_walks_backwards_and_closes_only_the_first_hole(self):
        ok, ids, q = BT.consume_change(CAPS, arr([3, 3, 5, 3, 6]), arr([5, 5, 1, 5, 1]), 3, -10)
        self.assertTrue(ok)
        # slot 3 then slot 1 emptied; the shift closes hole 1 only - slot 4 keeps its place
        self.assertEqual((ids[:5], q[:5]), ([3, 5, 0, 0, 6], [5, 1, 0, 0, 1]))
        ok, _, _ = BT.consume_change(CAPS, arr([3]), arr([5]), 3, -6)
        self.assertFalse(ok)
        self.assertFalse(BT.consume_change(CAPS, arr([]), arr([]), 3, 1000)[0])     # range check

    def test_etc_tab_2008_quirks_and_the_2009_fix(self):
        ids, q = arr([9, 9]), arr([99, 50])
        ok8, ids8, q8 = BT.etc_change(CAPS, ids, q, 9, 10, B8)
        ok9, ids9, q9 = BT.etc_change(CAPS, ids, q, 9, 10, B9)
        self.assertEqual((ok8, ids8[:3], q8[:3]), (True, [9, 9, 9], [99, 50, 10]))    # 99 "absorbs"
        self.assertEqual((ok9, ids9[:3], q9[:3]), (True, [9, 9, 0], [99, 60, 0]))
        # compaction: 2008 stops on the CONSUME cap
        caps = [35, 2, 35]
        _, a8, _ = BT.etc_change(caps, arr([4, 5, 6, 7]), arr([1, 1, 1, 1]), 4, -1, B8)
        _, a9, _ = BT.etc_change(caps, arr([4, 5, 6, 7]), arr([1, 1, 1, 1]), 4, -1, B9)
        self.assertEqual(a8[:4], [5, 0, 6, 7])
        self.assertEqual(a9[:4], [5, 6, 7, 0])

    def test_equipment_by_exact_block(self):
        z, s = [0] * 6, [11, 12, 0, 0, 0, 5]
        ok, ids, recs = BT.equip_change(CAPS, arr([179, 179, 70]), arr([tuple(s), BT.ZERO, BT.ZERO], BT.ZERO),
                                        179, z, -1)
        self.assertTrue(ok)
        self.assertEqual((ids[:3], recs[0]), ([179, 70, 0], tuple(s)))     # the zero-block one left
        self.assertFalse(BT.equip_change(CAPS, arr([179]), arr([tuple(s)], BT.ZERO), 179, z, -1)[0])
        self.assertFalse(BT.equip_change([61, 35, 35], arr([]), arr([], BT.ZERO), 179, z, 1)[0])
        ok, ids, recs = BT.equip_change([2, 35, 35], arr([179, 0]), arr([BT.ZERO], BT.ZERO), 70, s, 1)
        self.assertEqual((ok, ids[:2], recs[1]), (True, [179, 70], tuple(s)))
        self.assertFalse(BT.equip_change([1, 35, 35], arr([179]), arr([], BT.ZERO), 70, s, 1)[0])

    @needs_2008
    def test_model_ensure_and_the_0x65_fields(self):
        char = {}
        BT.ensure(char)
        self.assertEqual((char['bank_slots'], char['bank']),
                         ([35, 35, 35], {'equip': [], 'consume': [], 'etc': [], 'gold': 0}))
        before = json.dumps(char)
        BT.ensure(char)
        self.assertEqual(json.dumps(char), before)                          # idempotent
        bank = BT.Bank(char)
        self.assertTrue(bank.add(STICK, 1, [7, 8, 9, 10, 11, 3]))
        self.assertFalse(bank.add(BLUE_MUSHROOM, 1200))                # one move is <= 999
        self.assertTrue(bank.add(BLUE_MUSHROOM, 999))
        self.assertTrue(bank.add(BLUE_MUSHROOM, 201))
        bank.gold = 12345
        self.assertEqual(bank.total(BLUE_MUSHROOM), 1200)
        fields = BT.fields_for_65(char)
        for build in (B8, B9):
            body = P.build('0x65', fields, client_build=build)
            back = P.parse('0x65', body, direction='S2C', client_build=build)
            self.assertEqual((back['bank_equip_slots'], back['equip_count'], back['consume_count'],
                              back['misc_count'], back['bank_gold']), (35, 1, 2, 0, 12345))
            row = back['repeat[equip_count]'][0]
            self.assertEqual((row['item_id'], [o['option'] for o in row['repeat[option_count]']],
                              row['equip_attr_last']), (STICK, [7, 8, 9, 10, 11], 3))
            self.assertEqual([(r['item_id'], r['quantity']) for r in back['repeat[consume_count]']],
                             [(3, 999), (3, 201)])
        # a hand-edited record is clamped: caps 0..60, qty to the stack size, <= 60 slots
        bad = {'bank_slots': [99, -1, 'x'], 'bank': {'consume': [{'id': 3, 'qty': 5000}] * 70,
                                                    'gold': -5, 'junk': 1}}
        BT.ensure(bad)
        self.assertEqual(bad['bank_slots'], [60, 0, 35])
        self.assertEqual((len(bad['bank']['consume']), bad['bank']['consume'][0]['qty'], bad['bank']['gold']),
                         (60, 999, 0))
        self.assertNotIn('junk', bad['bank'])


# ================================================================ server flows ===
class Rig(unittest.TestCase):
    build = B8

    def setUp(self):
        use_build(self.build)
        self.tmp = tempfile.mkdtemp(prefix=f'ws_shopbank{self.build}_')
        cfg = {'MOB_SERVER_CONTROLLED': True, 'CLIENT_BUILD': self.build}
        self.server = F.make_server(self.tmp, accounts=json.loads(json.dumps(ACCOUNTS)),
                                    config=cfgmod.from_dict(cfg))
        self.keys = KEYS[self.build]
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        use_build(B8)

    # ------------------------------------------------------------ helpers ---
    def char(self, user='test', name='TestHero'):
        return self.server.store.find_character(user, name)

    def login(self, c, user='test', password='test'):
        if self.build == B9:
            c.send_c2s(self.keys['login'], F.sso_login(user, password))
        else:
            c.send_c2s(self.keys['login'], {'account_id': user, 'password': password})
        self.assertEqual(c.s2c(c.expect(0x02))['result'], 1)

    def enter(self, user='test', password='test', name='TestHero'):
        """Login + enter world on map 101 (no monsters). 2009 gets its bank block (0x65)."""
        c = F.FakeClient(self.server)
        self.clients.append(c)
        self.login(c, user, password)
        c.send_c2s(self.keys['enter'], {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907, 'char_name': name})
        ops = [0x03, 0x07, 0x15] + ([0x65] if self.build == B9 else []) + [0x28, 0x44]
        # world-presence: + 0x04 / 0x05 when another client is on that map (F.expect_entry)
        map_code = int(self.char(user, name).get('map') or 101)
        pkts = F.expect_entry(c, ops, self.clients, map_code)
        for pkt in pkts:
            c.s2c(pkt, allow_trailing=pkt.opcode == 0x03)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world')))
        return c, pkts

    def relog(self, c):
        c.close()
        c.thread.join(timeout=5.0)
        return self.enter()

    @staticmethod
    def warning_text(c, pkt):
        text = c.s2c(pkt)['text']
        return text.decode('latin-1') if isinstance(text, bytes) else text

    def warning(self, c, pkt):
        self.assertEqual(c.s2c(pkt)['msg_type'], 2)
        return self.warning_text(c, pkt)

    def gold(self):
        return self.char()['gold']

    def give(self, item, qty=1, words=None):
        self.assertIsNotNone(self.server._inv_add(self.clients[-1].session, item, qty, 'test', words))

    def bag(self):
        return INV.Inventory(self.char()).totals()

    def bank(self):
        return BT.Bank(self.char(), build=self.build)

    def disk(self):
        self.server.store.flush()
        with open(self.server.db_file, encoding='utf-8') as f:
            return json.load(f)


class ShopFlow:
    """shop_storage-npc-buy / sell (F1/F2) and P4 exit criterion 1, per build."""

    def buy(self, c, item, qty=1, npc=MISTY):
        c.send_c2s(self.keys['buy'], {'item_id': item, 'qty': qty, 'npc_id': npc})

    def refusal(self, c):
        resync, line = c.expect(0x18, 0x15)
        rec = c.s2c(resync)
        self.assertEqual((rec['gold'], rec['victy'], rec['item_id'], rec['count']),
                         (self.char()['gold'], self.char()['victy'], 0, 0))
        return self.warning(c, line)

    def test_a_potion_costs_its_price_and_the_manner_discount(self):
        c, _ = self.enter()
        price = EC.items().get(HERB).buy
        self.buy(c, HERB, 3)
        rec = c.s2c(c.expect(0x18))
        self.assertEqual((rec['item_id'], rec['count'], rec['gold']), (HERB, 3, GOLD - 3 * price))
        self.assertEqual(self.bag(), {HERB: 3})
        # manner 200 (> 199): the client charges ROUND(qty x price x 0.9), so must the server
        self.server.store.adjust_manner('test', 200)
        self.buy(c, HERB, 7)
        rec = c.s2c(c.expect(0x18))
        self.assertEqual(rec['gold'], GOLD - 3 * price - SH.cost(price, 7, 0.9))
        self.assertEqual(self.gold(), rec['gold'])
        self.assertEqual(self.disk()['test']['characters'][0]['gold'], rec['gold'])

    def test_victy_priced_items_charge_victy(self):
        c, _ = self.enter()
        self.buy(c, ELIXIR, 1)                             # 100 gold + 10 Victy, Victy 0
        self.assertEqual(self.refusal(c), '[Warning] Not enough Victy.')
        with self.server.store.lock:
            self.char()['victy'] = 25
        self.buy(c, ELIXIR, 2)
        rec = c.s2c(c.expect(0x18))
        self.assertEqual((rec['victy'], rec['gold'], rec['count']), (5, GOLD - 200, 2))

    def test_refusals_resync_and_never_grant(self):
        c, _ = self.enter()
        cases = [
            ('a monster is no merchant', dict(item=HERB, npc=PUPU), 'not for sale'),
            ('the bank NPC is no merchant', dict(item=HERB, npc=BANKER), 'not for sale'),
            ('not on this list', dict(item=STICK, npc=MISTY), 'not for sale'),
            ('KR-only id', dict(item=4356, npc=MISTY), 'not for sale'),
            ('qty 0', dict(item=HERB, qty=0), 'amount'),
            ('qty 1000', dict(item=HERB, qty=1000), 'amount'),
            ('equipment qty 2', dict(item=STICK, qty=2, npc=11), 'amount'),    # Murdock sells 179
        ]
        for label, kw, text in cases:
            with self.subTest(label):
                self.buy(c, **kw)
                self.assertIn(text, self.refusal(c))
        self.assertEqual((self.gold(), self.bag()), (GOLD, {}))
        with self.server.store.lock:
            self.char()['gold'] = 10
        self.buy(c, HERB, 1)
        self.assertEqual(self.refusal(c), '[Warning] Not enough gold.')
        self.assertEqual(self.bag(), {})

    def test_a_skill_book_teaches_the_skill_and_files_no_bag_item(self):
        with self.server.store.lock:
            ch = self.char()
            ch['class'], ch['exp'] = 1, progression.exp_for_level(20)
        c, _ = self.enter()
        self.buy(c, IAP2, 1, CHRISTINA)                     # not shown yet: Lv1 first
        self.assertIn('not for sale', self.refusal(c))
        self.buy(c, IAP1, 1, CHRISTINA)
        rec = c.s2c(c.expect(0x18))
        self.assertEqual((rec['item_id'], rec['count'], rec['gold']), (IAP1, 1, GOLD - 560))
        self.assertEqual((self.char()['skills'], self.bag()), ([IAP1], {}))
        self.buy(c, IAP1, 1, CHRISTINA)                     # the row now shows Lv2
        self.assertEqual(self.refusal(c), '[Warning] You already have learned this skill.')
        self.buy(c, IAP2, 1, CHRISTINA)
        self.assertEqual(c.s2c(c.expect(0x18))['item_id'], IAP2)
        self.assertEqual(self.char()['skills'], [IAP2])     # the family level replaced in place
        # Double Jump at Duinuke: 3000 gold + 100 Victy, no discount for skills
        self.server.store.adjust_manner('test', 500)
        self.buy(c, DOUBLE_JUMP, 1, DUINUKE)
        self.assertEqual(self.refusal(c), '[Warning] Not enough Victy.')
        with self.server.store.lock:
            self.char()['victy'] = 100
        gold = self.gold()
        self.buy(c, DOUBLE_JUMP, 1, DUINUKE)
        rec = c.s2c(c.expect(0x18))
        self.assertEqual((rec['gold'], rec['victy']), (gold - 3000, 0))
        self.assertEqual((self.char()['skills'], self.bag()), ([IAP2, DOUBLE_JUMP], {}))

    def test_selling_300_of_a_stack_credits_300_times_the_price(self):
        c, _ = self.enter()
        self.give(BLUE_MUSHROOM, 300)
        c.send_c2s(self.keys['sell'], {'item_id': BLUE_MUSHROOM, 'qty': 300})
        rec = c.s2c(c.expect(0x19))
        price = EC.items().get(BLUE_MUSHROOM).sell
        self.assertEqual((rec['count'], rec['gold']), (300, GOLD + 300 * price))
        self.assertEqual(self.bag(), {})

    def test_a_socketed_item_sells_by_its_exact_block(self):
        c, _ = self.enter()
        self.give(STICK, 1, [11, 12, 0, 0, 0, 5])
        self.give(STICK, 1)
        c.send_c2s(self.keys['sell'], {'item_id': STICK, 'qty': 1, 'socket_count': 2, 'item_extra': 5,
                                       'repeat[socket_count]': [{'socket_stone_id': 11},
                                                                {'socket_stone_id': 12}]})
        rec = c.s2c(c.expect(0x19))
        self.assertEqual(([o['opt'] for o in rec['repeat[opt_count]']], rec['opt_extra']), ([11, 12], 5))
        left = INV.Inventory(self.char()).slots('equip')
        self.assertEqual([(e['id'], e['w']) for e in left], [(STICK, [0] * 6)])

    def test_an_undecodable_buy_gets_the_policy_refusal(self):
        c, _ = self.enter()
        c.send(0x0B, b'\x05\x00')
        self.assertIn('could not be bought', self.refusal(c))


class BankFlow:
    """shop_storage-bank-* and the password gate, per build (P4 exit criterion 2)."""

    def open_bank(self, c):
        """The build's own way into window 0x1A7: 2008 the password gate, 2009 nothing at all
        (the enter-world 0x65 is the block the window shows)."""
        if self.build == B9:
            return None
        c.send_c2s(self.keys['password'], {'password': 'test', 'target_window_id': 0x1A7})
        a, opened, b = c.expect(0x65, 0x80, 0x65)
        self.assertEqual(c.s2c(opened), {'result': 1, 'target_window_id': 0x1A7})
        self.assertEqual(c.s2c(a), c.s2c(b))
        return c.s2c(a)

    def deposit(self, c, item, qty, opts=(), extra=0):
        """C2S 0x3C as the client builds it: the equipment branch (spec `if(item_type == 1)`,
        a client-side condition) carries the block, every other Type `u8 0, u16 0`."""
        key = self.keys['dep_item']
        fields = {'item_id': item, 'qty': qty}
        equipment = bool(opts or extra)
        if equipment:
            fields.update({'socket_count': len(opts), 'item_extra': extra,
                           'repeat[socket_count]': [{'socket_stone_id': o} for o in opts]})
        payload = P.build(key, fields, {'item_type == 1': equipment}, direction='C2S',
                          client_build=self.build)
        c.send(P.opcode(key, 'C2S', client_build=self.build), payload)

    def item_refusal(self, c):
        contents, line = c.expect(0x65, 0x15)
        self.assertEqual(c.s2c(contents), c.s2c(contents))
        return c.s2c(contents), self.warning(c, line)

    def test_password_gate(self):
        c, _ = self.enter()
        pw = self.keys['password']
        c.send_c2s(pw, {'password': 'nope', 'target_window_id': 0x1A7})
        self.assertEqual(c.s2c(c.expect(0x80)), {'result': 0})
        c.send_c2s(pw, {'password': 'test', 'target_window_id': 0x1234})         # not unlockable
        self.assertEqual(c.s2c(c.expect(0x80)), {'result': 0})
        c.send_c2s(pw, {'password': 'test', 'target_window_id': 0x1F9})         # cash gift
        self.assertEqual(c.s2c(c.expect(0x80)), {'result': 1, 'target_window_id': 0x1F9})
        c.send_c2s(pw, {'password': 'test', 'target_window_id': 0x235})         # delete confirm
        self.assertEqual(c.s2c(c.expect(0x80)), {'result': 1, 'target_window_id': 0x235})
        c.send_c2s(pw, {'password': 'test', 'target_window_id': 0x1A7})
        a, opened, _ = c.expect(0x65, 0x80, 0x65)
        self.assertEqual(c.s2c(opened), {'result': 1, 'target_window_id': 0x1A7})
        self.assertEqual((c.s2c(a)['bank_equip_slots'], c.s2c(a)['bank_gold']), (35, 0))
        self.assertTrue(c.session.get('bank_open'))
        # a second password, when set, replaces the login password for 0x51
        with self.server.store.lock:
            self.server.store.account('test')['second_password'] = '1234'
        c.send_c2s(pw, {'password': 'test', 'target_window_id': 0x1F9})
        self.assertEqual(c.s2c(c.expect(0x80)), {'result': 0})
        c.send_c2s(pw, {'password': '1234', 'target_window_id': 0x1F9})
        self.assertEqual(c.s2c(c.expect(0x80))['result'], 1)

    def test_deposit_withdraw_items_and_gold_then_relog(self):
        """P4 exit criterion 2: deposit an item and 1000 gold, withdraw some, relog."""
        c, _ = self.enter()
        self.give(HERB, 10)
        self.give(STICK, 1, [11, 12, 0, 0, 0, 5])
        self.open_bank(c)
        self.deposit(c, HERB, 4)
        rec = c.s2c(c.expect(0x66))
        fee = SH.bank_fee(0)                                    # manner 0 -> 50
        self.assertEqual((rec['gold'], rec['item_id'], rec['qty'], rec['opt_count']), (GOLD - fee, HERB, 4, 0))
        self.deposit(c, STICK, 1, (11, 12), 5)
        rec = c.s2c(c.expect(0x66))
        self.assertEqual(([o['opt'] for o in rec['repeat[opt_count]']], rec['item_ext']), ([11, 12], 5))
        self.assertEqual(rec['gold'], GOLD - 2 * fee)
        c.send_c2s(self.keys['dep_gold'], {'gold': 1000})
        rec = c.s2c(c.expect(0x68))
        self.assertEqual(rec, {'amount': 1000, 'storage_gold': 1000, 'gold': GOLD - 2 * fee - 1000})
        c.send_c2s(self.keys['wd_gold'], {'gold': 400})
        rec = c.s2c(c.expect(0x69))
        self.assertEqual(rec, {'amount': 400, 'storage_gold': 600, 'gold': GOLD - 2 * fee - 600})
        c.send_c2s(self.keys['wd_item'], {'item_id': HERB, 'amount' if self.build == B9 else 'qty': 1,
                                          **({'opt_count': 0, 'attr_0c': 0} if self.build == B9 else
                                             {'socket_count': 0, 'item_extra': 0})})
        rec = c.s2c(c.expect(0x67))
        self.assertEqual((rec['item_id'], rec['qty']), (HERB, 1))
        self.assertEqual(self.bag(), {HERB: 7})
        bank = self.bank()
        self.assertEqual((bank.total(HERB), bank.total(STICK), bank.gold), (3, 1, 600))

        c2, pkts = self.relog(c)
        self.assertEqual(self.bag(), {HERB: 7})
        self.assertEqual(self.gold(), GOLD - 2 * fee - 600)
        contents = c2.s2c(pkts[3]) if self.build == B9 else self.open_bank(c2)
        self.assertEqual(contents['bank_gold'], 600)
        self.assertEqual([(r['item_id'], r['quantity']) for r in contents['repeat[consume_count]']], [(HERB, 3)])
        stick = contents['repeat[equip_count]'][0]
        self.assertEqual((stick['item_id'], [o['option'] for o in stick['repeat[option_count]']],
                          stick['equip_attr_last']), (STICK, [11, 12], 5))
        disk = self.disk()['test']['characters'][0]
        self.assertEqual((disk['bank']['gold'], disk['bank_slots']), (600, [35, 35, 35]))

    def test_a_failed_bag_add_leaves_the_item_in_the_bank(self):
        """The space check, the bank remove and the bag add are one step under the combat
        lock. If the add still fails (e.g. a kill's loot took the last slot), the item goes
        back into the bank and the client gets the refusal resync, not a 0x67."""
        c, _ = self.enter()
        self.give(HERB, 5)
        self.open_bank(c)
        self.deposit(c, HERB, 3)
        c.expect(0x66)
        with mock.patch.object(self.server, '_inv_add', return_value=None):
            c.send_c2s(self.keys['wd_item'], {'item_id': HERB, 'amount' if self.build == B9 else 'qty': 2,
                                              **({'opt_count': 0, 'attr_0c': 0} if self.build == B9 else
                                                 {'socket_count': 0, 'item_extra': 0})})
            self.assertIn('space', self.item_refusal(c)[1])
        self.assertEqual((self.bank().total(HERB), self.bag()), (3, {HERB: 2}))

    def test_equipment_withdraw_sends_the_stored_block(self):
        c, _ = self.enter()
        self.give(STICK, 1, [11, 0, 0, 0, 0, 5])
        self.open_bank(c)
        self.deposit(c, STICK, 1, (11,), 5)
        c.expect(0x66)
        # path (b) style request: an all-zero block still finds the first slot with that id,
        # and the 0x67 carries the slot's OWN block (the client memcmps it)
        c.send_c2s(self.keys['wd_equip'], {'item_id': STICK, 'amount': 1, 'opt_count': 0, 'attr_0c': 0})
        rec = c.s2c(c.expect(0x67))
        self.assertEqual(([o['opt'] for o in rec['repeat[opt_count]']], rec['item_ext'], rec['qty']),
                         ([11], 5, 1))
        self.assertEqual([e['w'] for e in INV.Inventory(self.char()).slots('equip')], [[11, 0, 0, 0, 0, 5]])
        self.assertEqual(self.bank().total(STICK), 0)
        # a block the bank does not hold is refused with the full 0x65 resync
        c.send_c2s(self.keys['wd_equip'], {'item_id': STICK, 'amount': 1, 'opt_count': 1,
                                           'repeat[opt_count]': [{'opt': 99}], 'attr_0c': 0})
        _, text = self.item_refusal(c)
        self.assertIn("doesn't hold", text)

    def test_item_refusals_resync_the_bank(self):
        c, _ = self.enter()
        self.give(HERB, 5)
        self.give(1848, 1)                                    # Brown Fedora: Cash 1
        self.open_bank(c)
        for label, args, text in (
                ('more than owned', (HERB, 6), "don't have"),
                ('qty 0', (HERB, 0), "can't be stored"),
                ('cash item', (1848, 1), "can't be stored"),
                ('a skill is no bank item', (IAP1, 1), "can't be stored")):
            with self.subTest(label):
                self.deposit(c, *args)
                self.assertIn(text, self.item_refusal(c)[1])
        with self.server.store.lock:
            self.char()['gold'] = 49
        self.deposit(c, HERB, 1)
        self.assertIn('short of gold', self.item_refusal(c)[1])
        self.assertEqual((self.bag(), self.gold()), ({HERB: 5, 1848: 1}, 49))
        c.send_c2s(self.keys['wd_item'], {'item_id': HERB, 'amount' if self.build == B9 else 'qty': 1,
                                          **({'opt_count': 0, 'attr_0c': 0} if self.build == B9 else
                                             {'socket_count': 0, 'item_extra': 0})})
        self.assertIn("doesn't hold", self.item_refusal(c)[1])

    def test_the_looser_precheck_does_not_fool_the_server(self):
        """shop_storage.md 6.3: 998 + 10 of one id in the only two slots, no empty slot - the
        client's FUN_00427B70 lets a deposit of 5 through, its add then fails. The server
        refuses with the 0x65 resync and changes nothing."""
        with self.server.store.lock:
            ch = self.char()
            BT.ensure(ch)
            ch['bank_slots'] = [35, 2, 35]
            ch['bank']['consume'] = [{'id': HERB, 'qty': 998}, {'id': HERB, 'qty': 10}]
        c, _ = self.enter()
        self.give(HERB, 5)
        self.open_bank(c)
        self.assertTrue(self.bank().space_ok(HERB, 5))
        self.deposit(c, HERB, 5)
        contents, text = self.item_refusal(c)
        self.assertIn('no empty space in Bank', text)
        self.assertEqual([(r['item_id'], r['quantity']) for r in contents['repeat[consume_count]']],
                         [(HERB, 998), (HERB, 10)])
        self.assertEqual((self.bag(), self.gold()), ({HERB: 5}, GOLD))

    def test_gold_refusals_are_zero_amount_resyncs(self):
        c, _ = self.enter()
        self.open_bank(c)
        for key, gold, op in (('dep_gold', 0, 0x68), ('dep_gold', GOLD + 1, 0x68),
                              ('dep_gold', (1 << 64) - 5, 0x68),       # a negative _atol entry
                              ('wd_gold', 1, 0x69)):
            with self.subTest(key=key, gold=gold):
                c.send_c2s(self.keys[key], {'gold': gold})
                reply, line = c.expect(op, 0x15)
                self.assertEqual(c.s2c(reply), {'amount': 0, 'storage_gold': 0, 'gold': GOLD})
                self.warning(c, line)
        self.assertEqual((self.gold(), self.bank().gold), (GOLD, 0))

    def test_bank_fallback_dev_command(self):
        c, _ = self.enter()
        for line in (b'!bank', b'/bank'):
            with self.subTest(line=line):
                c.send_c2s(self.keys['chat'], {'msg_len': len(line), 'message': line})
                a, opened, b, reply = c.expect(0x65, 0x80, 0x65, 0x15)
                self.assertEqual(c.s2c(opened), {'result': 1, 'target_window_id': 0x1A7})
                self.assertIn('Bank opened', self.warning_text(c, reply))
        # a non-GM's /bank is ordinary chat
        plain, _ = self.enter('admin', 'admin', 'Plain')
        plain.send_c2s(self.keys['chat'], {'msg_len': 5, 'message': b'/bank'})
        self.assertEqual(plain.s2c(plain.expect(0x16))['text'], '/bank')

    def test_bank_moves_need_a_character_in_world(self):
        c = F.FakeClient(self.server)
        self.clients.append(c)
        self.login(c)
        with self.assertLogs('WS', logging.WARNING):
            c.send_c2s(self.keys['dep_gold'], {'gold': 5})
            c.expect_silence(0.2)
        c.send_c2s(self.keys['password'], {'password': 'test', 'target_window_id': 0x1A7})
        self.assertEqual(c.s2c(c.expect(0x80)), {'result': 0})


@needs_2008
class Shop2008(ShopFlow, Rig):
    build = B8

    def test_npc_id_zero_is_no_merchant(self):
        """The 2009 guild sale's npc_id 0 (send site 0x474327) means nothing on 2008."""
        c, _ = self.enter()
        with self.assertLogs('WS', logging.INFO) as logs:
            self.buy(c, 4283, 1, npc=0)
            self.assertEqual(self.refusal(c), '[Warning] That item is not for sale.')
        self.assertTrue(any('npc 0' in m and 'not a merchant' in m for m in logs.output), logs.output)
        self.assertEqual(self.gold(), GOLD)


@needs_2009
class Shop2009(ShopFlow, Rig):
    build = B9

    def test_the_guild_npc_sells_the_guild_billboard(self):
        """P15 guild-g6: Moiba's send site 0x474327 asks for its hni row 4283 (the premium cash
        board) with npc_id 0 - always 0 on the wire (p15 live triage 1); on the Guild Plaza the
        cp-2 exe ('en', the default) gets the Guild Billboard EN 4284 at its hii Buy (boards.py /
        GameServer._buy_guild_board) and a line with the real price; the stock exe ('kr') stays
        refused."""
        with self.server.store.lock:
            self.char().update(map=9702, x=2200, y=1300)                      # Moiba's tile
        c, _ = self.enter()
        price = EC.items().get(4284).buy
        c.send_c2s('0x474327/0x0B', {'item_id': 4283, 'qty': 1, 'npc_id': 0})
        grant, line = c.expect(0x18, 0x15)
        rec = c.s2c(grant)
        self.assertEqual((rec['gold'], rec['item_id'], rec['count']), (GOLD - price, 4284, 1))
        self.assertEqual(self.warning_text(c, line), f'Guild Billboard x1 bought for {price:,} gold.')
        self.assertEqual(self.server._inventory(self.server.world.by_char_name('TestHero')).get(4284), 1)
        self.server.config['CLIENT_ITEM_IDS'] = 'kr'
        c.send_c2s('0x474327/0x0B', {'item_id': 4283, 'qty': 1, 'npc_id': 0})
        self.assertEqual(self.refusal(c), '[Warning] Guild billboards are not available.')

    def test_the_2009_slot_discount(self):
        with self.server.store.lock:
            self.char()['equipped'] = {1: {'id': 500, 'w': [2, 0, 0, 0, 0, 0]}}
        c, _ = self.enter()
        price = EC.items().get(HERB).buy
        self.buy(c, HERB, 10)
        self.assertEqual(c.s2c(c.expect(0x18))['gold'], GOLD - SH.cost(price, 10, 0.95))


@needs_2008
class Bank2008(BankFlow, Rig):
    build = B8

    def test_enter_world_is_unchanged(self):
        c, pkts = self.enter()
        self.assertEqual([p.opcode for p in pkts], [0x03, 0x07, 0x15, 0x28, 0x44])


@needs_2009
class Bank2009(BankFlow, Rig):
    build = B9

    def test_enter_world_carries_the_bank_block(self):
        with self.server.store.lock:
            ch = self.char()
            BT.ensure(ch)
            ch['bank']['gold'] = 777
            ch['bank']['etc'] = [None, {'id': 2030, 'qty': 4}]
        c, pkts = self.enter()
        contents = c.s2c(pkts[3])
        self.assertEqual((contents['bank_gold'], contents['misc_count']), (777, 2))
        self.assertEqual([(r['item_id'], r['quantity']) for r in contents['repeat[misc_count]']],
                         [(0, 0), (2030, 4)])
        # a portal keeps the client's block: no second 0x65 on a map change
        self.server._map_transfer(c.session['sock'], c.session, 102, 50, 712, reason='test')
        self.assertNotIn(0x65, [p.opcode for p in c.recv_until_quiet(0.3)])


# =============================================================== persistence ===
class Migration(unittest.TestCase):
    def test_bank_fields_are_added_once_with_a_p4_backup(self):
        tmp = tempfile.mkdtemp(prefix='ws_shopbank_mig_')
        self.addCleanup(shutil.rmtree, tmp, True)
        server = F.make_server(tmp, accounts=json.loads(json.dumps(ACCOUNTS)))
        path = server.db_file
        self.assertTrue(os.path.exists(path + '.bak-pre-p4'))
        with open(path + '.bak-pre-p4', encoding='utf-8') as f:
            self.assertNotIn('bank_slots', f.read())                  # the pre-migration bytes
        with open(path, encoding='utf-8') as f:
            hero = json.load(f)['test']['characters'][0]
        self.assertEqual((hero['bank_slots'], hero['bank']),
                         ([35, 35, 35], {'equip': [], 'consume': [], 'etc': [], 'gold': 0}))
        again = W.storemod.Store(path).load()
        self.assertEqual(again.migration_changes, [])                  # idempotent


if __name__ == '__main__':
    unittest.main()
