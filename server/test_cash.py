#!/usr/bin/env python3
"""
test_cash.py - P8 stage 1: the premium cash model (cash.py), both client builds
==============================================================================
premium_cash-wallet-model, premium_cash-catalog, premium_cash-cash-inventory-api,
premium_cash-owned-list-sync (docs/systems/premium_cash.md 1.2 / 1.3 / 1.7 / F1 / 3.1-3.3;
roadmap P8 order 1):

  - catalog: the client's own hii mall columns per build (2008 474 / 2009 530 mall items),
    the KR gamedef differences, the slot extensions, packets' 0x72 period gate from the hii;
  - records: the 28-byte record fields, SYSTEMTIME expiry, 0x6F in both framings (2009 pages);
  - store: the account wallet / box / inbox and the character's cash_items through an
    idempotent migration that writes the one-time accounts.json.bak-pre-p8 first;
  - the inventory API: serials (max + 1, >= 0x1000, store-wide), find (FUN_0045E760),
    consume (FUN_00464380), grant (count records merge);
  - the owned list after every map load (enter world, portal, warp) and nothing for a
    character without cash items; Notes are cash records now (limit_type 1, the 0x77 serial
    uses one up; DEV_FREE_NOTES off); `!cash` (0x70) / `!cash item` / `!note`; the 0x46
    backstop and the 0x02 first-purchase flag read the wallet.

No port is bound, no client is started and the live accounts.json is never opened (temp
copies; the module checks its hash at the end).
"""
import contextlib
import datetime
import hashlib
import json
import logging
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import fakeclient as F  # noqa: E402
import cash as CASH  # noqa: E402
import packets as P  # noqa: E402
import store as S  # noqa: E402

W = F.import_server()
cfgmod = W.cfgmod
EC = W.EC
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = cfgmod.BUILD_2008, cfgmod.BUILD_2009
DIRS = {B8: cfgmod.DEFAULTS['CLIENT_DIR'], B9: cfgmod.DEFAULTS['CLIENT_DIR_2009']}
HAVE = {b: os.path.exists(os.path.join(HERE, d, 'hs', 'windslayer.hii')) for b, d in DIRS.items()}
CHAT = {B8: '0x445CA7/0x03', B9: '0x44790E/0x03'}
NOTE_SEND = {B8: '0x461064/0x4B', B9: '0x467D8C/0x4B'}
BALANCE_REFRESH = {B8: '0x43E302/0x46', B9: '0x43E602/0x46'}
# The live-verified map-load prefix of a character without cash items (map 101: no monsters).
ENTRY = {B8: [0x03, 0x07, 0x15, 0x28, 0x44], B9: [0x03, 0x07, 0x15, 0x65, 0x28, 0x44]}
NOTE, MEGAPHONE, EXP_60D, STICK = 1894, 3381, 3321, 179

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
            'accounts.json changed during test_cash.py (tests must only use temp copies)'


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


def _char(name, gm=0):
    return {'name': name, 'level': 1, 'class': 0, 'map': 101, 'x': 700.0, 'y': 812.0, 'hp': 100, 'mp': 50,
            'gm': gm}


def _in_days(days):
    return (datetime.datetime.now() + datetime.timedelta(days=days)).replace(microsecond=0)


# ================================================================== catalog ===
class Catalog(unittest.TestCase):
    """premium_cash-catalog: the client's own item table is the price list, per build."""

    def tearDown(self):
        EC.configure(DIRS[B8], B8)

    def test_tcat_dump_diff(self):
        """dump_item_catalog.py --cash (T-CAT, a live-client tool): the def bytes +0x1F0..+0x1FF
        parse into Cash / Cash_Cls / Cash_T / Cash_P, and cash_diff names every column that
        differs from the server's hii row (offline half of the check)."""
        if not HAVE[B8]:
            self.skipTest('needs the EN 2008 client data')
        EC.configure(DIRS[B8], B8)
        import dump_item_catalog as D
        note = CASH.cash_def(NOTE)
        block = bytearray(16)
        block[0] = note.cash
        struct.pack_into('<HH', block, 4, note.cls, note.duration_type)
        struct.pack_into('<I', block, 0xC, note.price)
        cols = D.read_cash_columns(block)
        self.assertEqual(cols, {'cash': 1, 'cls': 15, 'duration_type': 1, 'price': 30})
        self.assertEqual(D.cash_diff({NOTE: cols}, CASH.cash_def), [])
        self.assertEqual(D.cash_diff({NOTE: {**cols, 'duration_type': 0}, STICK: {'price': 5}}, CASH.cash_def),
                         [(STICK, 'price', 5, 0), (NOTE, 'duration_type', 0, 1)])

    def test_mall_rows_of_each_build(self):
        for build, mall, total in ((B8, 474, 4248), (B9, 530, 4322)):
            with self.subTest(build=build):
                if not HAVE[build]:
                    self.skipTest(f'needs the EN {build} client data')
                EC.configure(DIRS[build], build)
                rows = CASH.mall_catalog()
                self.assertEqual(len(EC.items()), total)
                self.assertEqual(sum(1 for d in rows if d.cash == 1), mall)       # every one priced
                self.assertTrue(all(d.price > 0 for d in rows))
                # the slot extensions: Cash 0, 4600 Wind Cash, the picker's rows (not the mall list)
                ext = [d for d in rows if d.id in CASH.SLOT_EXT]
                self.assertEqual([(d.id, d.cash, d.price) for d in ext], [(i, 0, 4600) for i in range(1884, 1890)])
                note = CASH.cash_def(NOTE)
                self.assertEqual((note.cash, note.cls, note.duration_type, note.price, note.value, note.kind),
                                 (1, 15, 1, 30, 1, CASH.KIND_COUNT))
                exp = CASH.cash_def(EXP_60D)
                self.assertEqual((exp.duration_type, exp.value, exp.price, exp.kind),
                                 (2, 60, 13800, CASH.KIND_PERIOD))
                self.assertFalse(CASH.cash_def(STICK).is_cash)
                self.assertIsNone(CASH.cash_def(total + 1))
                self.assertIn('windslayer.hii', CASH.catalog_source())

    def test_the_2008_rows_are_unchanged_in_2009_and_the_kr_gamedef_is_no_price_source(self):
        if not (HAVE[B8] and HAVE[B9]):
            self.skipTest('needs both EN clients')
        EC.configure(DIRS[B8], B8)
        old = {d.id: d for d in CASH.mall_catalog()}
        EC.configure(DIRS[B9], B9)
        new = {d.id: d for d in CASH.mall_catalog()}
        self.assertEqual({i: new[i] for i in old}, old)                       # identical mall columns
        self.assertEqual(sorted(set(new) - set(old))[:2], [4253, 4254])
        self.assertEqual(len(set(new) - set(old)), 56)
        # 2009 id 4286: counted x250 in the EN hii, a permanent Type 1 in the KR gamedef - the
        # 0x72 period gate (packets.cash_duration_type) follows the client's own table.
        self.assertEqual((new[4286].duration_type, new[4286].value), (1, 250))
        self.assertEqual(P.cash_duration_type(4286), 1)
        EC.configure(DIRS[B8], B8)
        self.assertEqual(P.cash_duration_type(EXP_60D), 2)
        self.assertEqual(CASH.cash_def(3436).price, 2500)                     # KR gamedef: 1500

    def test_quantity_of_a_new_record(self):
        if not HAVE[B8]:
            self.skipTest('needs the EN 2008 client data')
        EC.configure(DIRS[B8], B8)
        self.assertEqual(CASH.cash_def(3320).quantity(), 11)                  # "11 Message Pads"
        self.assertEqual(CASH.cash_def(3214).quantity(), 5)                   # Reset 5: qty = allowance (C13)
        self.assertEqual(CASH.cash_def(NOTE).quantity(3), 3)
        self.assertEqual(CASH.cash_def(EXP_60D).quantity(9), 1)               # a period item is one record

    def test_the_gamedef_fallback_shifts_2009_ids(self):
        """ROADMAP_2009_ADDENDUM C3: above 4248 an EN 2009 id is KR + 4 (4249..4252 are EN-only
        event items). The 0x72 period gate reads the hii of the build being ENCODED; when that
        table is not the loaded one it falls back to the KR gamedef row of the shifted id."""
        self.assertEqual([P.kr_item_idx(i, B9) for i in (EXP_60D, 4248, 4249, 4252, 4253, 4286)],
                         [EXP_60D, 4248, None, None, 4249, 4282])
        self.assertEqual(P.kr_item_idx(4286, B8), 4286)
        if not os.path.exists(P.GAMEDEF_PATH):
            self.skipTest('gamedef.sqlite3 not present')
        hii = None
        if HAVE[B9]:
            EC.configure(DIRS[B9], B9)
            hii = {d.id: d.duration_type for d in CASH.mall_catalog() if d.id > 4252}
            self.assertEqual(len(hii), 56)
        EC.configure(DIRS[B8], B8)                  # the loaded hii is 2008's: a 2009 encode falls back
        if hii is not None:
            self.assertEqual({i: P.cash_duration_type(i, B9) for i in hii}, hii)
        self.assertIsNone(P.cash_duration_type(4250, B9))
        # 4280 Rider's Bow: EN Cash_T 0, but KR idx 4280 is a period row - unshifted, the 0x72
        # would carry 16 expiry bytes the 2009 client never reads.
        own = P.build(0x72, {'player_uid': 1, 'item_id': 4280, 'item_serial': 5}, receiver_uid=1, client_build=B9)
        self.assertEqual(len(own), 10)


# ================================================================== records ===
class Records(unittest.TestCase):
    def test_ensure_builds_and_repairs(self):
        acc = {'gift_inbox': [{'sender': 'GM', 'message': 'x', 'item_id': 3327, 'serial': 0, 'delivered': False},
                              {'item_id': 0}, 'junk'],
               'cash': '1500', 'mileage': -5}
        CASH.ensure_account(acc)
        self.assertEqual(acc, {'gift_inbox': [{'sender': 'GM', 'message': 'x', 'item_id': 3327, 'serial': 0,
                                               'delivered': False}],
                               'cash': 1500, 'mileage': 0, 'first_purchase_done': False,
                               'first_purchase_notice': False, 'cash_box': []})
        again = json.loads(json.dumps(acc))
        CASH.ensure_account(again)
        self.assertEqual(again, acc)                                           # idempotent
        char = {'cash_items': [{'serial': 0x1001, 'item_id': NOTE, 'kind': 1, 'qty': 2},
                               {'serial': 0x1001, 'item_id': MEGAPHONE, 'kind': 1, 'qty': 1},   # duplicate serial
                               {'serial': 5, 'item_id': NOTE},                                   # below 0x1000
                               {'serial': 0x1002, 'item_id': NOTE, 'kind': 1, 'qty': 0},         # used up
                               {'serial': 0x1003, 'item_id': EXP_60D, 'kind': 2, 'qty': 1,
                                'expire': '2026-09-24T23:59:00.5', 'equipped': 1}]}
        CASH.ensure(char)
        self.assertEqual(char['cash_items'], [
            {'serial': 0x1001, 'item_id': NOTE, 'kind': 1, 'qty': 2, 'expire': None, 'origin': 0,
             'equipped': False},
            {'serial': 0x1003, 'item_id': EXP_60D, 'kind': 2, 'qty': 1, 'expire': '2026-09-24T23:59:00',
             'origin': 0, 'equipped': True}])
        items = char['cash_items']
        CASH.ensure(char)
        self.assertIs(char['cash_items'], items)                              # conforming: untouched
        self.assertEqual(CASH.clamp_cash(2 ** 40), CASH.CASH_MAX)

    def test_wire_record_and_systemtime(self):
        rec = CASH.make_record(0x1234, EXP_60D, CASH.KIND_PERIOD, 1, expire='2026-09-20T13:05:07', origin=3)
        wire = CASH.wire_record(rec)
        self.assertEqual(wire, {'serial': 0x1234, 'item_id': EXP_60D, 'limit_type': 2, 'unk_07': 0, 'quantity': 1,
                                'expire_year': 2026, 'expire_month': 9, 'expire_day_of_week': 0,   # a Sunday
                                'expire_day': 20, 'expire_hour': 13, 'expire_minute': 5, 'expire_second': 7,
                                'expire_milliseconds': 0, 'origin': 3, 'unk_1b': 0})
        self.assertEqual(CASH.expire_bytes(rec)[:4], bytes.fromhex('ea070900'))
        self.assertEqual(len(CASH.expire_bytes(rec)), 16)
        self.assertEqual(CASH.expire_bytes(CASH.make_record(0x1235, NOTE, 1, 1)), bytes(16))
        self.assertTrue(CASH.is_activated(rec))
        self.assertFalse(CASH.usable(rec))                                     # FUN_0045E760 skips it
        self.assertTrue(CASH.is_expired(rec, '2026-09-20T13:05:07'))
        self.assertFalse(CASH.is_expired(rec, '2026-09-20T13:05:06'))
        self.assertFalse(CASH.is_expired(CASH.make_record(0x1236, EXP_60D, 2, 1), '2030-01-01T00:00:00'))

    def test_0x6f_per_build(self):
        char = {'cash_items': [CASH.make_record(0x1001, NOTE, 1, 2, equipped=False),
                               CASH.make_record(0x1002, EXP_60D, 2, 1, expire=_in_days(-1), equipped=False),
                               CASH.make_record(0x1003, EXP_60D, 2, 1, expire=_in_days(7), equipped=False)]}
        [p8] = CASH.owned_list_packets(char, B8)
        raw8 = P.build('0x6F', p8, client_build=B8)
        self.assertEqual(len(raw8), 4 + 29 * 2)                               # the expired one is left out
        back = P.parse('0x6F', raw8, direction='S2C', client_build=B8,
                       assume=P.assume_defaults('0x6F', 'S2C', client_build=B8))
        self.assertEqual([r['serial'] for r in back['repeat[count]']], [0x1001, 0x1003])
        self.assertEqual(back['repeat[count]'][1]['expire_day'], _in_days(7).day)
        [p9] = CASH.owned_list_packets(char, B9)
        raw9 = P.build('0x6F', p9, client_build=B9)
        self.assertEqual((raw9[:2], len(raw9)), (bytes([0, 2]), 2 + 29 * 2))  # mode 0, u8 count
        self.assertEqual(raw9[2:], raw8[4:])                                   # same 29-byte rows
        empty = P.build('0x6F', CASH.owned_list_packets({'cash_items': []}, B8)[0], client_build=B8)
        self.assertEqual(empty, bytes(4))                                      # count 0 = "none"

    def test_0x6f_never_outgrows_one_frame(self):
        """The Fireway size field is 11 bits (0x7FF with the 8-byte header and the opcode), so
        a 0x6F holds at most 70 of its 29-byte rows in both builds: 2008 has one packet (the
        owned list is capped at 70; more is cut with an error), 2009 pages of 70."""
        self.assertEqual(CASH.FRAME_PAYLOAD_MAX, W.MAX_PKT - W.HEADER_SIZE - 1)
        self.assertEqual((CASH.PAGE_2009, CASH.OWNED_MAX), (70, 70))

        def many(n):
            return {'name': 'Many', 'cash_items': [CASH.make_record(0x2000 + i, NOTE, 1, 1) for i in range(n)]}

        [p8] = CASH.owned_list_packets(many(70), B8)
        self.assertEqual(len(P.build('0x6F', p8, client_build=B8)), 4 + 29 * 70)   # 2034 B
        [p9] = CASH.owned_list_packets(many(70), B9)
        self.assertEqual((p9['mode'], p9['count']), (CASH.MODE_SINGLE, 70))
        self.assertEqual(len(P.build('0x6F', p9, client_build=B9)), 2 + 29 * 70)   # 2032 B
        with self.assertLogs('WS', 'ERROR') as logs:
            [p8] = CASH.owned_list_packets(many(71), B8)
        self.assertEqual((p8['count'], p8['repeat[count]'][-1]['serial']), (70, 0x2045))
        self.assertIn('0x2046', logs.output[0])                               # the 71st is named
        for n, want in ((71, [(1, 70), (3, 1)]), (141, [(1, 70), (2, 70), (3, 1)])):
            with self.subTest(n=n):
                pages = CASH.owned_list_packets(many(n), B9)
                self.assertEqual([(p['mode'], p['count']) for p in pages], want)
                for page in pages:
                    self.assertLessEqual(len(P.build('0x6F', page, client_build=B9)), CASH.FRAME_PAYLOAD_MAX)

    def test_a_frame_past_0x7ff_is_refused_not_masked(self):
        """The send path refuses what make_raw_packet would have wrapped (P8 review), before
        the sequence number moves, so the connection stays in step."""
        self.assertEqual(len(W.make_raw_packet(b'\x6f' + bytes(2038))), 0x7FF)
        with self.assertRaises(ValueError):
            W.make_raw_packet(b'\x6f' + bytes(2039))
        server, session = W.GameServer.__new__(W.GameServer), {'send_seq': 7}
        with self.assertLogs('WS', 'ERROR'), self.assertRaises(P.PacketError):
            server._send_encrypted(None, session, 0x6F, bytes(2039))
        self.assertEqual(session['send_seq'], 7)

    def test_balance_fields_are_the_0x70_body(self):
        body = P.build('0x70', CASH.balance_fields({'cash': 1000, 'mileage': 7}), client_build=B9)
        self.assertEqual(body, bytes.fromhex('e8030000' '07000000' '00'))


# ================================================================ migration ===
class MigrationP8(unittest.TestCase):
    """The wallet and the owned list arrive through an idempotent migration that first
    writes the one-time accounts.json.bak-pre-p8 (hard rule for a persisted-schema change)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_p8_store_')
        self.path = os.path.join(self.tmp, 'accounts.json')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pre_p8_file(self, build):
        st = S.Store(self.path, client_build=build).load()                  # a current P7 file
        with st.lock:
            for acc in st.accounts.values():
                for k in CASH.ACCOUNT_FIELDS:
                    acc.pop(k, None)
                for ch in acc['characters']:
                    ch.pop('cash_items', None)
            # a P5 `!gift` entry is carried over as it is
            st.accounts['admin']['gift_inbox'] = [{'sender': 'GM', 'message': 'hi', 'item_id': 3327,
                                                   'serial': 0, 'delivered': False}]
        st.save_now()
        for suffix in S.BACKUP_SUFFIXES + (S.BACKUP_SUFFIX_2009,):
            path = self.path + suffix
            if suffix == '.bak-pre-p8':
                if os.path.exists(path):
                    os.remove(path)
            elif not os.path.exists(path):
                open(path, 'wb').close()                                   # earlier phases: done
        with open(self.path, 'rb') as f:
            return f.read()

    def test_the_cash_fields_are_added_once_with_their_backup(self):
        self.assertIn('.bak-pre-p8', S.BACKUP_SUFFIXES)
        for build in (B8, B9):
            with self.subTest(build=build):
                original = self._pre_p8_file(build)
                st = S.Store(self.path, client_build=build).load()
                want = [f'{u}: {k} created' for u in ('test', 'admin') for k in CASH.ACCOUNT_FIELDS
                        if not (u == 'admin' and k == 'gift_inbox')]
                self.assertEqual(sorted(st.migration_changes),
                                 sorted(want + ['test/TestHero: cash_items created']))
                with open(self.path + '.bak-pre-p8', 'rb') as f:
                    self.assertEqual(f.read(), original)
                with open(self.path, encoding='utf-8') as f:
                    disk = json.load(f)
                self.assertEqual({k: disk['test'][k] for k in CASH.ACCOUNT_FIELDS},
                                 {'cash': 0, 'mileage': 0, 'first_purchase_done': False,
                                  'first_purchase_notice': False, 'cash_box': [], 'gift_inbox': []})
                self.assertEqual(disk['admin']['gift_inbox'][0]['item_id'], 3327)
                self.assertEqual(disk['test']['characters'][0]['cash_items'], [])
                again = S.Store(self.path, client_build=build).load()
                self.assertEqual((again.migration_changes, again.saves), ([], 0))
                # a second pre-P8 state never overwrites the first backup
                self._pre_p8_file(build)
                with open(self.path + '.bak-pre-p8', 'wb') as f:
                    f.write(b'first')
                S.Store(self.path, client_build=build).load()
                with open(self.path + '.bak-pre-p8', 'rb') as f:
                    self.assertEqual(f.read(), b'first')
                os.remove(self.path + '.bak-pre-p8')

    def test_new_characters_and_accounts_carry_them(self):
        st = S.Store(self.path).load()
        ch = st.new_character('Nova', s10=1, s1=1, s6=2, s5=2, s9=2, stats=(3, 2, 1, 3))
        self.assertEqual(ch['cash_items'], [])
        acc = st.create_account('dora', 'pw')
        self.assertEqual((acc['cash'], acc['mileage'], acc['cash_box'], acc['gift_inbox']), (0, 0, [], []))
        st.add_character('dora', ch)
        self.assertEqual(S.Store(self.path).load().migration_changes, [])


# ============================================================ inventory API ===
class InventoryApi(unittest.TestCase):
    def setUp(self):
        if not HAVE[B8]:
            self.skipTest('needs the EN 2008 client data')
        self.tmp = tempfile.mkdtemp(prefix='ws_p8_api_')
        self.path = os.path.join(self.tmp, 'accounts.json')
        EC.configure(DIRS[B8], B8)
        self.st = S.Store(self.path).load()
        self.inv = CASH.CashInventory(self.st)
        self.hero = self.st.find_character('test', 'TestHero')

    def tearDown(self):
        EC.configure(DIRS[B8], B8)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_serials_are_store_wide_max_plus_one(self):
        self.assertEqual(self.inv.next_serial(), 0x1000)
        with self.st.lock:
            self.st.accounts['admin']['cash_box'] = [CASH.make_record(0x5000, MEGAPHONE, 1, 1)]
            self.st.accounts['admin']['gift_inbox'] = [{'sender': 'A', 'message': '', 'item_id': NOTE,
                                                        'serial': 0x6000, 'delivered': False}]
        fresh = CASH.CashInventory(self.st)
        self.assertEqual([fresh.next_serial(), fresh.next_serial()], [0x6001, 0x6002])

    def test_grant_find_consume(self):
        a = self.inv.grant(self.hero, NOTE, 2)
        b = self.inv.grant(self.hero, NOTE, 3)                             # merges: one record, one slot
        self.assertIs(a, b)
        self.assertEqual((a['kind'], a['qty'], a['origin']), (CASH.KIND_COUNT, 5, CASH.ORIGIN_EVENT))
        big = self.inv.grant(self.hero, NOTE, 251)                         # past STACK_MAX: a new record
        self.assertNotEqual(big['serial'], a['serial'])
        exp = self.inv.grant(self.hero, EXP_60D)
        self.assertEqual((exp['kind'], exp['qty'], exp['expire']), (CASH.KIND_PERIOD, 1, None))
        for bad in (STICK, 1884, 99999):
            with self.subTest(bad=bad), self.assertRaises(CASH.CashError):
                self.inv.grant(self.hero, bad)
        self.assertIs(self.inv.find(self.hero, NOTE), a)                   # the first usable one
        self.assertEqual(self.inv.consume(self.hero, a['serial'], 4), 1)
        self.assertEqual(self.inv.consume(self.hero, a['serial']), 0)      # freed at 0
        self.assertIsNone(self.inv.by_serial(self.hero, a['serial']))
        self.assertIs(self.inv.find(self.hero, NOTE), big)
        self.assertIsNone(self.inv.consume(self.hero, a['serial']))        # unknown serial
        self.assertEqual(self.inv.consume(self.hero, exp['serial']), 1)    # a period record is not used up
        with self.st.lock:
            exp['expire'] = CASH.format_expire(_in_days(3))                # activated
        self.assertIsNone(self.inv.find(self.hero, EXP_60D))
        self.assertTrue(self.st.dirty)

    def test_grant_refuses_cash_0_items(self):
        """P8 review minor: the gift certificates 3327..3332 and Add Quick Slot 3435 are Type 5
        with Cash 0. The client's 0x6F purges only Cash items (def+0x1F0 != 0) before it
        re-inserts the list, so a forced in-world 0x6F (`!note`, `!cash item`) added a second
        icon of such a record each time. grant() refuses them until the mall models them."""
        for item in (3327, 3332, 3435):
            with self.subTest(item=item), self.assertRaisesRegex(CASH.CashError, 'Cash 0 item'):
                self.inv.grant(self.hero, item)
        self.assertEqual(self.hero['cash_items'], [])
        self.assertEqual(self.inv.grant(self.hero, MEGAPHONE)['item_id'], MEGAPHONE)    # Cash 1: granted

    def test_grant_refuses_an_item_table_without_cash_columns(self):
        """P8 review minor: without the hii (en_content falls back to en_item_catalog.json, Type
        only) every CashDef says Cash_T 0, so a Note became a limit_type 0 record the client
        never uses up (the 2026-09-25 live bug). grant() refuses instead, as
        packets._client_cash_t ignores such a table for the 0x72 gate."""
        dump = EC.ItemCatalog.from_dump(EC.ITEM_CATALOG_JSON)
        self.assertFalse(CASH.has_cash_columns(dump))
        self.assertTrue(CASH.has_cash_columns())                       # the configured 2008 hii
        with mock.patch.object(EC, 'items', return_value=dump):
            with self.assertRaisesRegex(CASH.CashError, 'not a client .hii'):
                self.inv.grant(self.hero, NOTE, 2)
        self.assertEqual(self.hero['cash_items'], [])
        self.assertEqual(self.inv.grant(self.hero, NOTE, 2)['kind'], CASH.KIND_COUNT)

    def test_grant_caps_the_owned_list_at_one_frame(self):
        """OWNED_MAX (70) carried records per character in both builds: a merge still fits, a
        new record is refused whole, an expired one (left out of 0x6F) frees its place."""
        note = self.inv.grant(self.hero, NOTE, 1)
        exps = [self.inv.grant(self.hero, EXP_60D) for _ in range(CASH.OWNED_MAX - 1)]
        self.assertEqual(len({r['serial'] for r in exps + [note]}), 70)
        self.assertIs(self.inv.grant(self.hero, NOTE, 2), note)                # merges: no new record
        for item in (EXP_60D, MEGAPHONE):
            with self.subTest(item=item), self.assertRaisesRegex(CASH.CashError, 'at most 70'):
                self.inv.grant(self.hero, item)
        self.assertEqual(len(self.hero['cash_items']), 70)
        with self.st.lock:
            exps[0]['expire'] = CASH.format_expire(_in_days(-1))            # expired: not carried
        self.inv.grant(self.hero, MEGAPHONE)
        self.assertEqual((len(self.hero['cash_items']), len(CASH.owned_records(self.hero))), (71, 70))

    def test_a_count_above_stack_max_is_split(self):
        """The bag shows a quantity's low byte, so a counted grant of 600 is 255 + 255 + 90
        (consecutive serials); a split that would pass OWNED_MAX is refused whole."""
        first = self.inv.grant(self.hero, NOTE, 600)
        rows = self.hero['cash_items']
        self.assertEqual([(r['item_id'], r['qty']) for r in rows], [(NOTE, 255), (NOTE, 255), (NOTE, 90)])
        self.assertIs(first, rows[0])
        self.assertEqual([r['serial'] - first['serial'] for r in rows], [0, 1, 2])
        for _ in range(66):
            self.inv.grant(self.hero, EXP_60D)                                # 69 records
        with self.assertRaisesRegex(CASH.CashError, 'at most 70'):
            self.inv.grant(self.hero, NOTE, 300)                             # needs 2 new records
        self.assertEqual(len(rows), 69)
        self.inv.grant(self.hero, NOTE, 165)                                  # 90 + 165: merges
        self.assertEqual(rows[2]['qty'], 255)

    def test_only_cash_bag_items_and_pets_are_granted(self):
        """GRANT_TYPES: a Type 1 costume would land in the client's equipment tab (0x6F
        category 1) and is refused; a 2009 Type 6 pet is granted as a BOUND pet record, limit_type
        3 (ROADMAP_2009_ADDENDUM C1; test_carryins.py covers the record and its wire forms)."""
        costume = next(d for d in CASH.mall_catalog() if d.type == EC.TYPE_EQUIPMENT)
        with self.assertRaisesRegex(CASH.CashError, 'costume'):
            self.inv.grant(self.hero, costume.id)
        self.assertEqual(self.hero['cash_items'], [])
        if HAVE[B9]:
            EC.configure(DIRS[B9], B9)
            try:
                self.assertEqual(sorted(d.id for d in CASH.mall_catalog() if d.type == CASH.TYPE_PET),
                                 [4294, 4299, 4304, 4309])
                pet = self.inv.grant(self.hero, 4294)
                self.assertEqual((pet['kind'], pet['qty'], pet['equipped'], pet['pet']['level'], pet['pet']['name']),
                                 (CASH.KIND_PET, 1, False, 1, 'Picky'))
            finally:
                EC.configure(DIRS[B8], B8)

    def test_next_serial_takes_the_store_lock_first(self):
        """Lock order store.lock -> _counter_lock, as in grant(): a first next_serial() waiting
        for the store lock must not hold the counter lock meanwhile (no inversion)."""
        fresh = CASH.CashInventory(self.st)
        held, release, got = threading.Event(), threading.Event(), []

        def holder():
            with self.st.lock:
                held.set()
                release.wait(3)
        t1 = threading.Thread(target=holder)
        t1.start()
        self.assertTrue(held.wait(3))
        t2 = threading.Thread(target=lambda: got.append(fresh.next_serial()))
        t2.start()
        time.sleep(0.1)
        try:
            self.assertEqual(got, [])                                          # waits for the store lock
            self.assertTrue(fresh._counter_lock.acquire(blocking=False))       # without the counter lock
            fresh._counter_lock.release()
        finally:
            release.set()
            t1.join(3)
            t2.join(3)
        self.assertEqual(got, [0x1000])

    def test_set_balance_clamps(self):
        acc = self.st.account('test')
        self.assertEqual(self.inv.set_balance(acc, cash=1000), (1000, 0))
        self.assertEqual(self.inv.set_balance(acc, mileage=-3), (1000, 0))
        self.assertEqual(self.inv.set_balance(acc, cash=2 ** 33, mileage=5), (CASH.CASH_MAX, 5))


# ================================================================ in world ===
class _CashWorld:
    """TestHero (GM, uid 1) in world on map 101 of one server of `build`; Late exists offline
    (a Note recipient)."""
    build = B8

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        self.tmp = tempfile.mkdtemp(prefix=f'ws_cash_{self.build}_')
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False})
        accounts = {'test': {'password': 'test', 'characters': [_char('TestHero', gm=1)]},
                    'admin': {'password': 'admin', 'characters': []},
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
    def enter(self):
        c = F.FakeClient(self.server)
        self.clients.append(c)
        self.login = c.login('test', 'test')
        self.assertEqual(self.login['result'], 1)
        return c, c.enter_world('TestHero')

    def line(self, c, text):
        c.send_c2s(CHAT[self.build], {'msg_len': len(text), 'message': text})

    def owned(self, c, pkt):
        rec = c.s2c(pkt)
        if self.build == B9:
            self.assertEqual(rec['mode'], CASH.MODE_SINGLE)
        return [(r['serial'], r['item_id'], r['limit_type'], r['quantity']) for r in rec['repeat[count]']]

    def warp(self, c, map_code):
        """`!warp` (the GM's 0x15 reply first, then the map load); the map load's packets."""
        self.line(c, f'!warp {map_code}'.encode())
        self.assertTrue(_wait(lambda: self.server.world.map_of(c.session) == map_code))
        pkts = c.recv_until_quiet(0.3)
        self.assertEqual(pkts[0].opcode, 0x15)
        return pkts[1:]

    def seed(self):
        inv = self.server.cash
        return [inv.grant(self.hero, MEGAPHONE, 5), inv.grant(self.hero, EXP_60D),
                self._expired()]

    def _expired(self):
        rec = CASH.make_record(self.server.cash.next_serial(), EXP_60D, CASH.KIND_PERIOD, 1,
                               expire=_in_days(-1), equipped=False)
        with self.server.store.lock:
            self.hero['cash_items'].append(rec)
        return rec

    # ------------------------------------------------------------------- tests ---
    def test_no_cash_items_no_0x6f(self):
        """A character without cash items keeps the live-verified map-load sequence."""
        c, pkts = self.enter()
        self.assertEqual(_ops(pkts), ENTRY[self.build])
        self.assertEqual(_ops(self.warp(c, 102))[:5], [0x08, 0x03, 0x07, 0x28, 0x44])
        self.assertNotIn(0x6F, _ops(c.recv_until_quiet(0.1)))

    def test_owned_list_after_every_map_load(self):
        """premium_cash-owned-list-sync: enter world and every later map load end their
        0x03 / 0x07 / 0x28 / 0x44 with the full owned list (the Spark Items tab after a portal);
        an expired period record is left out. Since P8 stage 3 (premium_cash-expiry, cashuse.py)
        that map load also removes it and prints the client's own "[x] is expired." (0x15)."""
        mega, exp, gone = self.seed()
        want = [(mega['serial'], MEGAPHONE, 1, 5), (exp['serial'], EXP_60D, 2, 1)]
        c, pkts = self.enter()
        self.assertEqual(_ops(pkts), ENTRY[self.build] + [0x6F, 0x15])
        self.assertEqual(self.owned(c, pkts[-2]), want)
        self.assertEqual(_text(c.s2c(pkts[-1])['text']), f'[{EC.item_name(EXP_60D)}] is expired.')
        c.send(0x7E, bytes.fromhex('17000000'))                             # the 101 -> 102 portal (line 23)
        self.assertTrue(_wait(lambda: self.server.world.map_of(c.session) == 102))
        ops = c.recv_until_quiet(0.3)
        self.assertEqual(_ops(ops)[:6], [0x08, 0x03, 0x07, 0x28, 0x44, 0x6F])  # before the 0x1A
        self.assertEqual(self.owned(c, ops[5]), want)
        self.assertEqual(set(_ops(ops[6:])), {0x1A})
        ops = self.warp(c, 101)                                               # a `!warp` map load too
        self.assertEqual(self.owned(c, [p for p in ops if p.opcode == 0x6F][0]), want)
        self.assertNotIn(gone['serial'], [r['serial'] for r in                  # removed at enter world
                                          self.server.store.find_character('test', 'TestHero')['cash_items']])
        self.assertNotIn(0x15, _ops(ops))                                       # told once

    def test_the_owned_list_never_outgrows_one_frame(self):
        """70 records: one 0x6F of 2034 B (2008) / 2032 B (2009) on enter world; `!cash item` of
        a 71st is refused and the stream stays intact. 71 stored records (written past the API)
        still load: 2008 cuts the 0x6F to 70 with an error, 2009 sends pages 1 + 3."""
        for _ in range(CASH.OWNED_MAX):
            self.server.cash.grant(self.hero, EXP_60D)
        c, pkts = self.enter()
        self.assertEqual(_ops(pkts), ENTRY[self.build] + [0x6F])
        self.assertEqual(len(pkts[-1].payload), {B8: 4, B9: 2}[self.build] + 29 * 70)
        self.assertEqual(len(self.owned(c, pkts[-1])), 70)
        self.line(c, f'!cash item {EXP_60D}'.encode())
        self.assertIn('at most 70', _text(c.s2c(c.expect(0x15))['text']))
        self.assertEqual(len(self.hero['cash_items']), 70)
        with self.server.store.lock:
            self.hero['cash_items'].append(CASH.make_record(self.server.cash.next_serial(), EXP_60D,
                                                            CASH.KIND_PERIOD, 1, equipped=False))
        if self.build == B8:
            with self.assertLogs('WS', 'ERROR') as logs:
                ops = self.warp(c, 102)
            self.assertTrue(any('owns 71 cash records' in line for line in logs.output), logs.output)
            [page] = [c.s2c(p) for p in ops if p.opcode == 0x6F]
            self.assertEqual(page['count'], 70)
        else:
            ops = self.warp(c, 102)
            pages = [c.s2c(p) for p in ops if p.opcode == 0x6F]
            self.assertEqual([(p['mode'], p['count']) for p in pages],
                             [(CASH.MODE_FIRST, 70), (CASH.MODE_LAST, 1)])
        with self.assertLogs('WS', 'ERROR') if self.build == B8 else contextlib.nullcontext():
            self.assertIn(0x6F, _ops(self.warp(c, 101)))                      # the stream still decodes

    def test_notes_are_cash_records_and_a_sent_note_uses_one_up(self):
        """`!note 2` grants a limit_type 1 record (S2C 0x6F); each sent note names its serial in
        0x77 and the model takes one off (the client does the same); DEV_FREE_NOTES is retired,
        so with none left the send is refused; the next map load still clears the client's lists."""
        self.assertNotIn('DEV_FREE_NOTES', self.server.config)
        c, _ = self.enter()
        self.line(c, b'!note 2')
        cash, notice = c.expect(0x6F, 0x15)
        [(serial, item, kind, qty)] = self.owned(c, cash)
        self.assertEqual((item, kind, qty), (NOTE, CASH.KIND_COUNT, 2))
        self.assertGreaterEqual(serial, CASH.SERIAL_MIN)
        self.assertIn(f'{serial:#x}', _text(c.s2c(notice)['text']))
        self.line(c, b'!note 1')                                            # merges into the record
        self.assertEqual(self.owned(c, c.expect(0x6F, 0x15)[0]), [(serial, NOTE, 1, 3)])
        for left in (2, 1, 0):
            c.send_c2s(NOTE_SEND[self.build], {'item_id': NOTE, 'recipient_name': 'Late', 'contents': 'hi'})
            self.assertEqual(c.s2c(c.expect(0x77)), {'result': 1, 'cash_item_serial': serial})
            self.assertEqual([r['qty'] for r in self.hero['cash_items']], [left] if left else [])
        c.send_c2s(NOTE_SEND[self.build], {'item_id': NOTE, 'recipient_name': 'Late', 'contents': 'hi'})
        self.assertEqual(c.s2c(c.expect(0x77)), {'result': 0})
        self.assertEqual(len(self.server.store.find_character('late', 'Late')['memos']), 3)
        ops = self.warp(c, 102)                     # the client held records: one empty 0x6F clears it
        [empty] = [p for p in ops if p.opcode == 0x6F]
        self.assertEqual(self.owned(c, empty), [])
        self.assertNotIn(0x6F, _ops(self.warp(c, 101)))                    # then nothing any more

    def test_dev_free_notes_is_retired(self):
        """P8 stage 4 (roadmap P8 "Turn off DEV_FREE_NOTES"): the key is gone from the defaults,
        a config.json that still sets it gets a warning and is ignored, and a note without an
        owned Note is refused whatever the file says."""
        self.assertNotIn('DEV_FREE_NOTES', cfgmod.DEFAULTS)
        with self.assertLogs('WS', logging.WARNING) as logs:
            cfg = cfgmod.from_dict({'DEV_FREE_NOTES': True})
        self.assertNotIn('DEV_FREE_NOTES', cfg)
        self.assertTrue(any('DEV_FREE_NOTES' in line and 'retired' in line for line in logs.output), logs.output)
        self.server.config.update({'DEV_FREE_NOTES': True})        # even a hand-set stray key does nothing
        c, _ = self.enter()
        c.send_c2s(NOTE_SEND[self.build], {'item_id': NOTE, 'recipient_name': 'Late', 'contents': 'hi'})
        self.assertEqual(c.s2c(c.expect(0x77)), {'result': 0})
        self.assertEqual(self.server.store.find_character('late', 'Late').get('memos') or [], [])

    def test_cash_command_sets_the_account_wallet_and_sends_0x70(self):
        c, _ = self.enter()
        for text, want in ((b'!cash 1000', (1000, 0)), (b'!cash +500', (1500, 0)),
                           (b'!cash mileage 7', (1500, 7)), (b'!cash -99999', (0, 7)), (b'!cash 1000', (1000, 7))):
            with self.subTest(text=text):
                self.line(c, text)
                bal, notice = c.expect(0x70, 0x15)
                self.assertEqual(c.s2c(bal), {'cash_balance': want[0], 'mileage_balance': want[1],
                                              'first_purchase_bonus': 0})
                self.assertEqual(_text(c.s2c(notice)['text']), f'Wind Cash {want[0]}, Mileage {want[1]}.')
        acc = self.server.store.account('test')
        self.assertEqual((acc['cash'], acc['mileage']), (1000, 7))
        self.server.store.flush()
        with open(self.server.store.path, encoding='utf-8') as f:
            self.assertEqual(json.load(f)['test']['cash'], 1000)
        self.line(c, b'!cash item 3381 5')
        cash, _ = c.expect(0x6F, 0x15)
        [(serial, item, kind, qty)] = self.owned(c, cash)
        self.assertEqual((item, kind, qty), (MEGAPHONE, 1, 5))
        self.line(c, b'!cash')
        lines = [_text(c.s2c(p)['text']) for p in c.recv_until_quiet(0.3)]
        self.assertTrue(lines[0].startswith('Wind Cash 1000, Mileage 7; box 0, owned 1.'), lines)
        self.assertIn(f'{serial:#x}', lines[1])
        for bad, why in ((b'!cash item 179', 'not a cash item'), (b'!cash item 1884', 'slot extension'),
                         (b'!cash item 3327', 'Cash 0 item'), (b'!cash lots', 'not a number')):
            with self.subTest(bad=bad):
                self.line(c, bad)
                self.assertIn(why, _text(c.s2c(c.expect(0x15))['text']))

    def test_0x46_backstop_and_the_0x02_flag_read_the_wallet(self):
        with self.server.store.lock:
            acc = self.server.store.account('test')
            acc.update(cash=321, mileage=12, first_purchase_notice=True)
        c, _ = self.enter()
        self.assertEqual(self.login['cash_first_purchase_flag'], 1)
        # P8 stage 2 (premium_cash-balance-refresh, mall.refresh): the 0x46 handler shows the
        # owed first-purchase popup on its 0x70 once, then clears it (was the backstop's 0).
        for bonus in (1, 0):
            c.send_c2s(BALANCE_REFRESH[self.build], {})
            self.assertEqual(c.s2c(c.expect(0x70)),
                             {'cash_balance': 321, 'mileage_balance': 12, 'first_purchase_bonus': bonus})

    def test_serials_survive_a_restart(self):
        rec = self.server.cash.grant(self.hero, NOTE, 1)
        self.server.store.save_now()
        with open(self.server.store.path, encoding='utf-8') as f:
            saved = json.load(f)
        again = F.make_server(tempfile.mkdtemp(dir=self.tmp), accounts=saved, config=self.server.config)
        self.assertEqual(again.cash.next_serial(), rec['serial'] + 1)
        self.assertEqual(again.store.find_character('test', 'TestHero')['cash_items'][0]['serial'], rec['serial'])


class CashWorld2008(_CashWorld, unittest.TestCase):
    build = B8


class CashWorld2009(_CashWorld, unittest.TestCase):
    build = B9


if __name__ == '__main__':
    unittest.main()
