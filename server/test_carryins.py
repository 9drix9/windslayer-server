#!/usr/bin/env python3
"""
test_carryins.py - the P8 carry-ins C1-C8 (ROADMAP_2009_ADDENDUM section 2), both client builds
===============================================================================================
  C1  pet records: hii Type 6 / Kind 14 -> limit_type 3 (CashDef.kind), the pet fields in the
      28-byte record (0x6A / 0x6F pet branch; 0x6C / 0x79 raw), the normalizer, bind / grant,
      the equipment-tab slot a bagged pet takes, the mall paths behind MALL_PETS, 2008 has none;
  C2  the worn pet record is in every 0x6F, first, and is the own 0x07's grid slot 15;
  C3  the KR -> EN id shift (arch09-id-shift in en_content) and the audit of every gamedef read;
  C4  the rename hook: the stable character id (cid), the store's rename_rewriters, the world
      hook ON_RENAME (friends, party, the mentor's 0x7B) and a new subscriber;
  C5  the one S2C 0x73 builder, the 2009 C2S 0x4D PetRename refusal (a waiting box);
  C6  C2S 0x48 routes EN pet food / the pet name ticket through the pet hook stub;
  C7  mall.grant_box: an origin-3 box record, S2C 0x6C with the balances unchanged;
  C8  the 2009 cash opcodes: C2S 0x80 -> 0x71 {1, 0x17}, 0x81 (1 -> 0x71 {1, 0x16}; 0 / 2 no
      reply), 0x4E -> 0xC4 {0}; S2C 0xA9 / 0xAA never sent.

Fake clients (fakeclient.MultiClient); no port is bound and the live accounts.json is never
opened (temp copies; the module checks its hash).
"""
import hashlib
import json
import logging
import os
import re
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
import cashuse as CU  # noqa: E402
import inventory as INV  # noqa: E402
import mall as MALL  # noqa: E402
import packets as P  # noqa: E402
import pets as PETS  # noqa: E402
import records as R  # noqa: E402
import registry  # noqa: E402
import store as S  # noqa: E402
import world as worldmod  # noqa: E402

W = F.import_server()
cfgmod = W.cfgmod
EC = W.EC
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = cfgmod.BUILD_2008, cfgmod.BUILD_2009
DIRS = {B8: cfgmod.DEFAULTS['CLIENT_DIR'], B9: cfgmod.DEFAULTS['CLIENT_DIR_2009']}
HAVE = {b: os.path.exists(os.path.join(HERE, d, 'hs', 'windslayer.hii')) for b, d in DIRS.items()}
HAVE_GAMEDEF = os.path.exists(EC.GAMEDEF_PATH)

PICKY, ULIE, CHIKAPUKA, GURIGURI = 4294, 4299, 4304, 4309
PETS_2009 = (PICKY, ULIE, CHIKAPUKA, GURIGURI)
FOOD20, TICKET, BELL = 4289, 4322, 4285
MEGAPHONE, NICK, TAB_EXT, STICK = 3381, 1895, 1884, 179
FEDORA, GIFT_CERT = 1848, 3327           # a Type 1 cash costume; a Type 5 Cash 0 gift certificate
CHAT = {B8: '0x445CA7/0x03', B9: '0x44790E/0x03'}
CLOSE = {B8: '0x45F0D0/0x42', B9: '0x4655F0/0x42'}
BUY_ONE = {B8: '0x4601FF/0x43', B9: '0x468D84/0x43'}
PORTAL_101_TO_102 = bytes.fromhex('17000000')

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
            'accounts.json changed during test_carryins.py (tests must only use temp copies)'


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


def _char(name, gm=0, gender=None, map_code=101, x=1411.0, y=714.0):
    c = {'name': name, 'level': 1, 'class': 0, 'map': map_code, 'x': x, 'y': y, 'hp': 100, 'mp': 50, 'gm': gm}
    if gender is not None:
        c['gender'] = gender
    return c


def _pet_rec(serial=0x2000, item=PICKY, equipped=None, **pet):
    base = CASH.new_pet(item, bound=True)
    base.update(pet)
    return CASH.make_record(serial, item, CASH.KIND_PET, 1, origin=CASH.ORIGIN_CASH, equipped=equipped, pet=base)


class _Content(unittest.TestCase):
    """Runs with the 2009 client's item table loaded (skips without it)."""
    build = B9

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        EC.configure(DIRS[self.build], self.build)

    def tearDown(self):
        EC.configure(DIRS[B8], B8)


# ===================================================================== C1 ===
class C1PetDef(_Content):
    def test_cashdef_kind_special_cases_type_6(self):
        """hii Cash_T is 0 for all four pets, but their records are limit_type 3 (pet 2.1, X6)."""
        for item in PETS_2009:
            d = CASH.cash_def(item)
            with self.subTest(item=item):
                self.assertEqual((d.type, EC.items().kind_of(item), d.cash, d.duration_type, d.price),
                                 (CASH.TYPE_PET, 14, 1, 0, 4900))
                self.assertTrue(d.is_pet)
                self.assertEqual((d.kind, d.quantity(), d.quantity(7)), (CASH.KIND_PET, 1, 1))
        self.assertEqual((CASH.cash_def(FOOD20).kind, CASH.cash_def(FOOD20).quantity()), (CASH.KIND_COUNT, 20))
        self.assertEqual(CASH.cash_def(TICKET).kind, CASH.KIND_COUNT)
        self.assertFalse(CASH.cash_def(MEGAPHONE).is_pet)
        self.assertEqual(sorted(d.id for d in CASH.mall_catalog() if d.is_pet), list(PETS_2009))

    def test_2008_has_no_pet_items(self):
        if not HAVE[B8]:
            self.skipTest('needs the EN 2008 client data')
        EC.configure(DIRS[B8], B8)
        self.assertEqual([i for i, d in EC.items().defs.items() if d.type == CASH.TYPE_PET], [])
        self.assertIsNone(CASH.cash_def(PICKY))


class C1PetRecord(unittest.TestCase):
    """The persisted pet record (structural: no catalog needed)."""

    def test_normalizer_keeps_kind_3_and_clamps_the_pet_fields(self):
        char = {'cash_items': [
            {'serial': 0x2000, 'item_id': PICKY, 'kind': 3, 'qty': 9, 'expire': '2030-01-01T00:00:00', 'origin': 3,
             'equipped': True, 'pet': {'exp': 99999, 'awake': 1, 'level': 12, 'gauge': -5, 'name': 'Tweetytweetybird'}},
            {'serial': 0x2001, 'item_id': ULIE, 'kind': 3, 'equipped': True},          # a second worn pet
            {'serial': 0x2002, 'item_id': MEGAPHONE, 'kind': 1, 'qty': 3, 'origin': 0, 'equipped': False}]}
        rows = CASH.ensure(char)
        self.assertEqual(rows[0], {'serial': 0x2000, 'item_id': PICKY, 'kind': 3, 'qty': 1, 'expire': None,
                                   'origin': 3, 'equipped': True,
                                   'pet': {'exp': 30600, 'awake': True, 'level': 9, 'gauge': 0, 'name': 'Tweetytweety'}})
        self.assertEqual((rows[1]['kind'], rows[1]['equipped'], rows[1]['pet']),
                         (3, False, {'exp': 0, 'awake': False, 'level': 0, 'gauge': 100, 'name': ''}))
        self.assertEqual(rows[2]['kind'], 1)
        self.assertIs(CASH.equipped_pet(char), rows[0])
        self.assertEqual(CASH.bagged_pets(char), 1)
        before = json.dumps(char, sort_keys=True)
        CASH.ensure(char)                                                  # idempotent
        self.assertEqual(json.dumps(char, sort_keys=True), before)
        box = CASH.ensure_account({'cash_box': [{'serial': 0x2003, 'item_id': PICKY, 'kind': 3}]})['cash_box']
        self.assertNotIn('equipped', box[0])
        self.assertEqual(box[0]['kind'], CASH.KIND_PET)

    def test_levels_and_binding(self):
        self.assertEqual([CASH.pet_level_for(e) for e in (0, 119, 120, 359, 360, 7559, 15240, 30599, 30600)],
                         [1, 1, 2, 2, 3, 6, 8, 8, 9])
        rec = CASH.make_record(0x2000, PICKY, CASH.KIND_PET, 1, pet={'name': '', 'level': 0, 'gauge': 40})
        self.assertTrue(CASH.bind_pet(rec))
        self.assertEqual({k: rec['pet'][k] for k in ('level', 'exp', 'gauge', 'awake')},
                         {'level': 1, 'exp': 0, 'gauge': 100, 'awake': True})
        self.assertFalse(CASH.bind_pet(rec))                               # bound already: untouched
        self.assertFalse(CASH.bind_pet(CASH.make_record(0x2001, MEGAPHONE, 1, 1)))
        self.assertEqual(CASH.clip_pet_name(b'Picky\x00junk'), 'Picky')
        self.assertEqual(len(CASH.clip_pet_name('x' * 20)), CASH.PET_NAME_MAX)

    def test_record_bytes_and_raw_fields(self):
        """0x6C / 0x79 read the 28 bytes raw: a pet in the 0x6F pet-branch layout with the
        origin at +0x1A (name[13]); any other record's raw_fields equal wire_record."""
        pet = _pet_rec(0x12345678, gauge=77, exp=400, level=3, name='Picky')
        pet['origin'] = CASH.ORIGIN_EVENT
        raw = CASH.record_bytes(pet)
        self.assertEqual(len(raw), CASH.RECORD_BYTES)
        self.assertEqual(raw[:8], bytes.fromhex('78563412') + PICKY.to_bytes(2, 'little') + b'\x03\x00')
        self.assertEqual(raw[8:13], (400).to_bytes(2, 'little') + bytes((1, 3, 77)))
        self.assertEqual(raw[13:18], b'Picky')
        self.assertEqual(raw[18:26], b'\x00' * 8)                          # the NUL + padding
        self.assertEqual((raw[CASH.ORIGIN_OFFSET], raw[27]), (3, 0))
        self.assertEqual(CASH.raw_fields(pet)['origin'], 3)
        for rec in (CASH.make_record(0x1000, MEGAPHONE, 1, 5, origin=0),
                    CASH.make_record(0x1001, 3321, 2, 1, expire='2030-02-03T04:05:06', origin=3)):
            with self.subTest(item=rec['item_id']):
                self.assertEqual(CASH.raw_fields(rec), CASH.wire_record(rec))
                for build in (B8, B9):
                    self.assertEqual(P.build('0x79', CASH.raw_fields(rec), client_build=build), CASH.record_bytes(rec))


class C1PetWire(_Content):
    def test_0x6f_pet_branch_worn_pet_first_and_2008_leaves_it_out(self):
        worn = _pet_rec(0x2000, equipped=True, gauge=50, exp=150, level=2, name='Picky')
        mega = CASH.make_record(0x1000, MEGAPHONE, 1, 5, equipped=False)
        bag = _pet_rec(0x2001, ULIE, equipped=False, name='Ulie')
        bag['origin'] = CASH.ORIGIN_EVENT                                  # an event pet: no refund
        char = {'name': 'TestHero', 'cash_items': [mega, bag, worn]}
        pages = CASH.owned_list_packets(char, B9)
        self.assertEqual([r['serial'] for r in pages[0]['repeat[count]']], [0x2000, 0x1000, 0x2001])
        body = P.build('0x6F', pages[0], client_build=B9)
        self.assertEqual(len(body), 2 + 3 * CASH.ROW_BYTES)
        # one record, one set of bytes: the pet branch IS record_bytes (0x6C / 0x79), origin included
        self.assertEqual(body[2:2 + 29], b'\x01' + CASH.record_bytes(worn))
        last = body[2 + 2 * CASH.ROW_BYTES:]
        self.assertEqual(last, b'\x00' + CASH.record_bytes(bag))
        self.assertEqual((last[1 + 13:1 + 18], last[1 + CASH.ORIGIN_OFFSET]), (b'Ulie\x00', CASH.ORIGIN_EVENT))
        back = P.parse('0x6F', body, direction='S2C', client_build=B9,
                       assume={'char_bag_slots_0x373 > 45 || char_bag_slots_0x374 > 45 || char_bag_slots_0x375 > 45':
                               False})
        row = back['repeat[count]'][0]
        self.assertEqual((row['is_equipped'], row['limit_type'], row['pet_exp'], row['pet_summoned'], row['pet_level'],
                          row['pet_gauge_pct'], _text(row['pet_name'])), (1, 3, 150, 1, 2, 50, 'Picky'))
        self.assertEqual(back['repeat[count]'][1]['quantity'], 5)
        self.assertEqual(_text(back['repeat[count]'][2]['pet_name']), 'Ulie')       # the name stops at its NUL
        with self.assertLogs('WS', level='WARNING') as cm:
            eight = CASH.owned_list_packets(char, B8)
        self.assertIn('left out of the 2008 0x6F', cm.output[0])
        self.assertTrue(cm.output[0].startswith('WARNING'))                # every map load: not an ERROR
        self.assertEqual([r['serial'] for r in eight[0]['repeat[count]']], [0x1000])

    def test_0x6a_box_pet_and_0x6c_0x79_raw_record(self):
        box_pet = CASH.new_record(0x3000, CASH.cash_def(PICKY), CASH.ORIGIN_CASH)
        self.assertEqual((box_pet['kind'], box_pet['pet']['level'], box_pet['pet']['awake'], box_pet['pet']['name']),
                         (3, 0, False, 'Picky'))
        acc = CASH.ensure_account({'cash': 9000, 'mileage': 5, 'cash_box': [box_pet]})
        body = P.build('0x6A', MALL.enter_fields(acc, B9), client_build=B9)
        self.assertEqual(len(body), 10 + 28)
        row = P.parse('0x6A', body, direction='S2C', client_build=B9)['repeat[box_count]'][0]
        self.assertEqual((row['limit_type'], row['pet_level'], row['pet_gauge_pct'], _text(row['pet_name'])),
                         (3, 0, 100, 'Picky'))
        self.assertEqual(MALL.enter_fields(acc, B8)['box_count'], 0)            # 2008: no pets
        self.assertEqual(body[10:], CASH.record_bytes(box_pet))
        for origin in (CASH.ORIGIN_EVENT, 2):                                   # event / gifted: no refund
            with self.subTest(origin=origin):
                acc['cash_box'][0]['origin'] = origin
                again = P.build('0x6A', MALL.enter_fields(acc, B9), client_build=B9)
                self.assertEqual((again[10:], again[10 + CASH.ORIGIN_OFFSET]),
                                 (CASH.record_bytes(acc['cash_box'][0]), origin))
        acc['cash_box'][0]['origin'] = CASH.ORIGIN_CASH
        six_c = P.build('0x6C', MALL.buy_fields(9000, 5, box_pet), client_build=B9)
        self.assertEqual((len(six_c), six_c[9:]), (37, CASH.record_bytes(box_pet)))
        self.assertEqual(P.build('0x79', CASH.raw_fields(box_pet), client_build=B9), CASH.record_bytes(box_pet))


class C1Inventory(_Content):
    def test_a_bagged_pet_takes_an_equipment_tab_slot_and_grant_needs_one(self):
        tmp = tempfile.mkdtemp(prefix='ws_c1_inv_')
        try:
            st = S.Store(os.path.join(tmp, 'accounts.json'), client_build=B9).load()
            inv = CASH.CashInventory(st)
            hero = st.find_character('test', 'TestHero')
            bag = INV.Inventory(hero)
            bag.set_capacity('equip', 3)
            self.assertIsNotNone(bag.add(STICK))
            pet = inv.grant(hero, PICKY)
            self.assertEqual((pet['kind'], pet['equipped'], pet['origin']), (3, False, CASH.ORIGIN_EVENT))
            self.assertEqual((bag.used_slots('equip'), bag.free_slots('equip')), (2, 1))
            self.assertEqual(len(bag.slots('equip')), 1)                   # 0x03 lists the stick only
            inv.grant(hero, ULIE)
            self.assertIn('full', bag.fits(STICK))                          # the client refuses it too
            with self.assertRaisesRegex(CASH.CashError, 'equipment tab'):
                inv.grant(hero, CHIKAPUKA)
            with st.lock:
                pet['equipped'] = True                                      # worn: grid slot 15, not the bag
            self.assertEqual(bag.used_slots('equip'), 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_scratch_bags_count_the_pet_too(self):
        """The review case: tab capacity 2, a stick and a bagged Picky. The crafting, trade and
        quest room checks run on a copy of `inventory` alone; each must refuse what the real
        bag refuses (a pet-filled tab), and a 2008 table counts no pet at all."""
        import crafting as CR
        import trade as TR
        stick = TR.Offer(STICK, 1, True, [0] * 6, [], 0)
        hero = {'name': 'Hero', 'cash_items': [_pet_rec(0x2000, equipped=False)]}
        bag = INV.Inventory(hero)
        bag.set_capacity('equip', 2)
        self.assertIsNotNone(bag.add(STICK))
        self.assertIn('full', bag.fits(STICK))
        self.assertEqual(CR._scratch(bag).fits(STICK), bag.fits(STICK))
        self.assertEqual(TR.simulate(hero, [], [stick]), bag.fits(STICK))
        copy = INV.Inventory({'inventory': json.loads(json.dumps(hero['inventory']))}, pets=bag.pet_slots())
        self.assertEqual((copy.pet_slots(), copy.fits(STICK)), (1, bag.fits(STICK)))
        self.assertIsNone(TR.simulate(hero, [stick], [stick]))             # the give frees the slot first
        if not HAVE[B8]:
            return
        EC.configure(DIRS[B8], B8)                                          # the same record, a 2008 server
        self.assertEqual((INV.pet_slots(hero), INV.Inventory(hero).free_slots('equip')), (0, 1))
        self.assertIsNone(INV.Inventory(hero).fits(STICK))


# =============================================================== in world ===
class _World:
    """TestHero (GM, uid 1) and Watcher (uid 2) in world on map 101 of one server of `build`;
    Carol exists offline."""
    build = B9
    config = {}

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        self.tmp = tempfile.mkdtemp(prefix=f'ws_carryin_{self.build}_')
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False,
                                **self.config})
        accounts = {'test': {'password': 'test', 'gender': 1,
                             'characters': [_char('TestHero', gm=1, gender=1)]},
                    'admin': {'password': 'admin', 'gender': 0, 'characters': [_char('Watcher', gender=0)]},
                    'carol': {'password': 'carol', 'characters': [_char('Carol')]}}
        self.server = F.make_server(self.tmp, accounts=accounts, config=cfg)
        self.hero = self.server.store.find_character('test', 'TestHero')
        self.watcher = self.server.store.find_character('admin', 'Watcher')
        self.prepare()
        self.mc = F.MultiClient(self.server)
        self.a, self.b = self.mc
        _wait(lambda: len(self.server.world.peers(self.a.session)) == 1)
        self.mc.drain(0.3)

    def prepare(self):
        """Store edits before anyone logs in."""

    def tearDown(self):
        self.mc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def key(self, op, name=''):
        for v in P.variants(op, 'C2S', client_build=self.build):
            if name in v['name']:
                return v['key']
        raise KeyError(op)

    def send(self, c, op, fields=None, name=''):
        c.send_c2s(self.key(op, name), fields or {})

    def only(self, c, op, quiet=0.3):
        pkts = c.recv_until_quiet(quiet)
        mine = [p for p in pkts if p.opcode == op]
        self.assertEqual(len(mine), 1, f'expected one 0x{op:02X}, got {[hex(o) for o in _ops(pkts)]}')
        return mine[0]

    def line(self, c, text):
        c.send_c2s(CHAT[self.build], {'msg_len': len(text), 'message': text})

    def portal(self, c):
        c.send(0x7E, PORTAL_101_TO_102)
        self.assertTrue(_wait(lambda: self.server.world.map_of(c.session) == 102))
        return c.recv_until_quiet(0.3)

    def owned(self, char=None):
        return {r['serial']: r for r in (char or self.hero)['cash_items']}


class C2WornPet2009(_World, unittest.TestCase):
    """ROADMAP_2009_ADDENDUM C2 / pet H5: while grid slot 15 holds the pet, every 0x6F carries
    its record (is_equipped 1) FIRST; both come from the one worn pet record."""

    def prepare(self):
        with self.server.store.lock:
            CASH.ensure(self.hero).extend([CASH.make_record(0x1000, MEGAPHONE, 1, 3, equipped=False),
                                           _pet_rec(0x2000, equipped=True, name='Picky')])

    def test_own_0x07_slot_15_and_every_0x6f_carry_the_worn_pet(self):
        a, b = self.a, self.b
        entry = a.entry
        own = next(p for p in entry if p.opcode == 0x07)
        rec = a.s2c(own)['repeat[player_count]'][0]
        self.assertEqual(rec['repeat[10]'][0], {'cash_equip_item_id': PICKY, 'cash_equip_item_attr0': 0})
        six_f = [p for p in entry if p.opcode == 0x6F]
        self.assertEqual(len(six_f), 1)
        self.assertGreater(_ops(entry).index(0x6F), _ops(entry).index(0x07))
        rows = a.s2c(six_f[0])['repeat[count]']
        self.assertEqual([(r['serial'], r['is_equipped'], r['limit_type']) for r in rows],
                         [(0x2000, 1, 3), (0x1000, 0, 1)])
        self.assertEqual(a.session['pet_bound'], 0x2000)
        # the observer sees grid slot 15 and - P15 pet-s1 - the awake pet's block (its pet_info)
        seen = [r for p in b.entry if p.opcode in (0x04,) for r in b.s2c(p)['repeat[player_count]']]
        hero = next(r for r in seen if r['uid'] == 1)
        self.assertEqual((hero['repeat[10]'][0]['cash_equip_item_id'], hero['has_pet'], hero['pet_name']),
                         (PICKY, 1, 'Picky'))
        self.assertTrue(W.cview.has_pet_info(b.session, 1))
        self.assertEqual(R.info_equipment(a.session, self.hero, B9)[15][0], PICKY)
        # a map load: the 0x07 and the 0x6F again, the pet first
        pkts = self.portal(a)
        rows = a.s2c(next(p for p in pkts if p.opcode == 0x6F))['repeat[count]']
        self.assertEqual(rows[0]['serial'], 0x2000)
        # a forced re-send (a grant) keeps it too
        self.server.cash.grant(self.hero, MEGAPHONE, 2)
        self.server._send_owned_cash(a.session['sock'], a.session, reason='test', force=True)
        rows = a.s2c(self.only(a, 0x6F))['repeat[count]']
        self.assertEqual((rows[0]['serial'], rows[0]['is_equipped']), (0x2000, 1))

    def test_a_list_without_the_bound_pet_is_an_error(self):
        a = self.a
        with self.server.store.lock:
            self.hero['cash_items'] = [r for r in self.hero['cash_items'] if r['serial'] != 0x2000]
        with self.assertLogs('WS', level='ERROR') as cm:
            self.server._send_owned_cash(a.session['sock'], a.session, reason='test', force=True)
        self.assertTrue(any('C2' in line and '0x2000' in line for line in cm.output), cm.output)
        a.recv_until_quiet(0.2)
        self.assertIsNone(a.session['pet_bound'])


class C2NoPets2008(_World, unittest.TestCase):
    build = B8

    def prepare(self):
        with self.server.store.lock:
            CASH.ensure(self.hero).append(_pet_rec(0x2000, equipped=True))      # store data from a 2009 server

    def test_2008_never_sends_a_pet(self):
        six_f = [p for p in self.a.entry if p.opcode == 0x6F]
        self.assertEqual([self.a.s2c(p)['count'] for p in six_f], [])     # the pet is all it owns: none
        self.assertIsNone(self.a.session.get('pet_bound'))
        with self.assertLogs('WS', level='WARNING') as cm:                # every map load: a WARNING
            self.server._send_owned_cash(self.a.session['sock'], self.a.session, reason='test', force=True)
        self.assertTrue(any(line.startswith('WARNING') and 'left out of the 2008 0x6F' in line for line in cm.output))
        self.assertEqual(self.a.s2c(self.only(self.a, 0x6F))['count'], 0)
        rec = R.player_record(self.a.session, self.hero, {'uid': 1})
        self.assertEqual(len(rec['repeat[9]']), 9)                          # the 2008 grid has no pet slot


class C1MallPets2009(_World, unittest.TestCase):
    """MALL_PETS on: buy -> a level-0 box pet (0x6C raw pet record), 0x6A pet branch, 0x42
    box -> character binds it into the equipment tab; a bound pet never goes back."""
    config = {'MALL_PETS': True}

    def enter_mall(self, c):
        self.line(c, b'!mall')
        self.assertTrue(_wait(lambda: c.session.get('in_cash_shop')))
        pkts = c.recv_until_quiet(0.3)
        self.assertEqual(pkts[-1].opcode, 0x6A, _ops(pkts))
        return pkts

    def close(self, c, to_box=(), to_char=()):
        c.send_c2s(CLOSE[self.build], {'storage_deleted_flag': 0, 'to_storage_count': len(to_box),
                                        'to_character_count': len(to_char),
                                        'repeat[to_storage_count]': [{'item_serial': s} for s in to_box],
                                        'repeat[to_character_count]': [{'item_serial': s} for s in to_char]})
        self.assertTrue(_wait(lambda: c.session.get('in_world') and not c.session.get('in_cash_shop')))
        return c.recv_until_quiet(0.3)

    def test_buy_box_bind_and_keep(self):
        a = self.a
        self.server.cash.set_balance(self.server.store.account('test'), cash=10000)
        self.enter_mall(a)
        a.send_c2s(BUY_ONE[B9], {'pay_with_mileage': 0, 'item_count': 1,
                                 'repeat[item_count]': [{'item_code': PICKY, 'option': 0}]})
        six_c = self.only(a, 0x6C)
        got = a.s2c(six_c)
        self.assertEqual((got['result'], got['cash_balance'], got['item_id'], got['item_kind']), (1, 5100, PICKY, 3))
        box = self.server.store.account('test')['cash_box']
        pet = box[-1]
        self.assertEqual(six_c.payload[9:], CASH.record_bytes(pet))
        self.assertEqual((pet['kind'], pet['pet']['level'], pet['origin']), (3, 0, CASH.ORIGIN_CASH))
        pkts = self.close(a, to_char=[pet['serial']])
        self.assertNotIn(pet['serial'], [r['serial'] for r in box])
        mine = self.owned()[pet['serial']]
        self.assertEqual({k: mine['pet'][k] for k in ('level', 'awake', 'gauge')}, {'level': 1, 'awake': True, 'gauge': 100})
        self.assertEqual(INV.Inventory(self.hero).used_slots('equip'), 1)
        rows = a.s2c(next(p for p in pkts if p.opcode == 0x6F))['repeat[count]']
        self.assertEqual([(r['serial'], r['is_equipped'], r['pet_level']) for r in rows], [(pet['serial'], 0, 1)])
        # the 0x6A of the next visit has no pet; moving the bound pet back is refused
        pkts = self.enter_mall(a)
        self.assertEqual(a.s2c(pkts[-1])['box_count'], 0)
        self.close(a, to_box=[pet['serial']])
        self.assertIn(pet['serial'], self.owned())


class C1MallPetsOff2009(_World, unittest.TestCase):
    def test_pet_sales_are_on_since_pet_s7(self):
        # P15 pet-s7 turned MALL_PETS on by default (pets and pet gear sold; test_petextras)
        self.assertTrue(cfgmod.defaults()['MALL_PETS'])
        self.assertIn('MALL_PETS', MALL.sale_refusal(CASH.cash_def(PICKY), PICKY))
        self.assertIsNone(MALL.sale_refusal(CASH.cash_def(PICKY), PICKY, pets=True))


# ===================================================================== C3 ===
class C3IdShift(unittest.TestCase):
    def test_the_rule_per_build(self):
        self.assertEqual([EC.kr_item_idx(i, B9) for i in (1, 4248, 4249, 4252, 4253, 4286, 4322)],
                         [1, 4248, None, None, 4249, 4282, 4318])
        self.assertEqual([EC.kr_item_idx(i, B8) for i in (4248, 4250, 4286)], [4248, 4250, 4286])
        self.assertEqual([EC.en_item_id(i, B9) for i in (4248, 4249, 4282, 4318)], [4248, 4253, 4286, 4322])
        self.assertEqual(EC.en_item_id(4282, B8), 4282)
        for en in range(4253, 4400):
            self.assertEqual(EC.en_item_id(EC.kr_item_idx(en, B9), B9), en)
        self.assertEqual(P.kr_item_idx(4286, B9), 4282)                  # packets delegates to the one rule

    def test_gamedef_item_rows_are_the_shifted_ones(self):
        if not (HAVE_GAMEDEF and HAVE[B9]):
            self.skipTest('needs gamedef.sqlite3 and the EN 2009 client data')
        EC.configure(DIRS[B9], B9)
        try:
            cat = EC.items()
            self.assertIsNone(EC.gamedef_item(4250))                      # EN-only event item
            self.assertEqual(EC.gamedef_item(4286)['idx'], 4282)
            wrong = 0
            for en in range(4253, cat.max_id + 1):
                d, row = cat.get(en), EC.gamedef_item(en, columns='Type, Cash, Cash_T')
                self.assertEqual((row['Type'], row['Cash'], row['Cash_T']), (d.type, d.cash, d.cash_t), en)
                raw = EC.gamedef_row('items', en, 'Type, Cash_T')
                wrong += raw is None or (raw['Type'], raw['Cash_T']) != (d.type, d.cash_t)
            self.assertGreater(wrong, 20)                                 # unshifted, 25 of 70 rows differ
            self.assertEqual(EC.gamedef_item(4286, B8)['idx'], 4286)       # explicit build: no shift
        finally:
            EC.configure(DIRS[B8], B8)

    def test_en_ids_used_by_the_server_are_the_en_hii_rows(self):
        if not HAVE[B9]:
            self.skipTest('needs the EN 2009 client data')
        EC.configure(DIRS[B9], B9)
        try:
            cat = EC.items()
            for item in CASH.PET_FOOD:
                d = CASH.cash_def(item)
                self.assertEqual((d.type, d.kind, d.cash), (5, CASH.KIND_COUNT, 1), item)
                self.assertTrue(EC.item_name(item).startswith('Pet Food'))
            self.assertEqual((CASH.cash_def(TICKET).kind, EC.item_name(TICKET)), (CASH.KIND_COUNT, 'Pet name making'))
            self.assertEqual((cat.type_of(BELL), EC.item_name(BELL)), (0, 'Pet Bell'))
            self.assertEqual([EC.item_name(i) for i in PETS_2009], ['Picky', 'Ulie', 'ChikaPuka', 'GuriGuri'])
        finally:
            EC.configure(DIRS[B8], B8)

    def test_every_gamedef_reader_goes_through_en_content(self):
        """The audit as a guard: only en_content opens the KR DB, no module reads an `items` row
        but gamedef_item (the shift), and the retired quest_defs.py is imported by nothing."""
        skip = re.compile(r'(^test_|Name clash|snapshot|^quest_defs\.py$)')
        offenders, importers = [], []
        for name in sorted(os.listdir(HERE)):
            if not name.endswith('.py') or skip.search(name):
                continue
            with open(os.path.join(HERE, name), encoding='utf-8', errors='replace') as f:
                text = f.read()
            if name != 'en_content.py' and re.search(r'sqlite3\.connect|gamedef_row\(\s*[\'"]items', text):
                offenders.append(name)
            if re.search(r'^\s*(import quest_defs|from quest_defs)', text, re.M):
                importers.append(name)
        self.assertEqual((offenders, importers), ([], []))


# ===================================================================== C4 ===
class C4StableIds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_c4_store_')
        self.path = os.path.join(self.tmp, 'accounts.json')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_migration_new_characters_and_lookup(self):
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump({'test': {'password': 'test', 'characters': [{'name': 'A', 'cid': 7}, {'name': 'B', 'cid': 7}]},
                       'admin': {'password': 'admin', 'characters': [{'name': 'C'}, {'name': 'D', 'cid': 'x'}]}}, f)
        st = S.Store(self.path).load()
        self.assertEqual([c['cid'] for u in ('test', 'admin') for c in st.characters(u)], [7, 8, 9, 10])
        self.assertIn('test/B: cid 8 (was 7)', st.migration_changes)
        self.assertIn('admin/C: cid 9', st.migration_changes)
        self.assertTrue(os.path.exists(self.path + S.BACKUP_SUFFIX_CID))
        self.assertIn(S.BACKUP_SUFFIX_CID, S.BACKUP_SUFFIXES)
        again = S.Store(self.path).load()
        self.assertEqual((again.migration_changes, again.saves), ([], 0))
        new = again.new_character('Nova', s10=1, s1=1, s6=2, s5=2, s9=2, stats=(3, 2, 1, 3))
        spare = again.new_character('Spare', s10=1, s1=1, s6=2, s5=2, s9=2, stats=(3, 2, 1, 3))
        self.assertEqual((new['cid'], spare['cid']), (11, 12))            # never reused, even unadded
        again.add_character('admin', new)
        dup = dict(spare, name='Dup', cid=7)                               # a record with a taken cid
        again.add_character('admin', dup)
        self.assertEqual(dup['cid'], 13)
        self.assertEqual(again.character_by_cid(11)[2]['name'], 'Nova')
        self.assertIsNone(again.character_by_cid(999))
        self.assertIsNone(again.character_by_cid(True))
        self.assertEqual(S.Store(self.path).load().migration_changes, [])

    def test_a_deleted_top_cid_is_not_reused_after_a_restart(self):
        """The review case: the highest-cid character is deleted, the store reloads, and the
        next new character must still get a cid nobody ever had (the account's retired_cid)."""
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump({'test': {'password': 'test', 'characters': [{'name': 'A', 'cid': 1}, {'name': 'B', 'cid': 4}]},
                       'admin': {'password': 'admin', 'characters': [{'name': 'C', 'cid': 5}]}}, f)
        st = S.Store(self.path).load()
        self.assertTrue(st.remove_character('admin', 'C'))
        self.assertTrue(st.remove_character('test', 'A'))                  # a lower one: the mark stays 5
        with open(self.path, encoding='utf-8') as f:
            disk = json.load(f)
        self.assertEqual((disk['admin'][S.RETIRED_CID], disk['test'][S.RETIRED_CID]), (5, 1))
        again = S.Store(self.path).load()
        self.assertEqual(again.migration_changes, [])
        new = again.new_character('Newbie', s10=1, s1=1, s6=2, s5=2, s9=2, stats=(3, 2, 1, 3))
        self.assertEqual(new['cid'], 6)
        again.add_character('admin', new)
        self.assertIsNone(again.character_by_cid(5))
        # the migration's max + 1 counts it too (a record without a cid on a later load)
        with open(self.path, encoding='utf-8') as f:
            disk = json.load(f)
        del disk['admin']['characters'][0]['cid']
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(disk, f)
        self.assertEqual(S.Store(self.path).load().find_character('admin', 'Newbie')['cid'], 6)
        self.assertEqual(S.assign_cids({'x': {S.RETIRED_CID: 9, 'characters': [{'name': 'Z'}]}}), ['x/Z: cid 10'])

    def test_rename_keeps_the_cid_and_runs_every_rewriter(self):
        st = S.Store(self.path).load()
        hero = st.find_character('test', 'TestHero')
        cid = hero['cid']
        seen = []

        def blacklist(store, username, char, old, new):                   # a P12 / P14-style subscriber
            seen.append((username, char['cid'], old, new))
            return 2

        def broken(store, username, char, old, new):
            raise RuntimeError('boom')

        st.rename_rewriters.extend([broken, blacklist])
        with self.assertLogs('WS', level='ERROR'):
            touched = st.rename_character('test', 'TestHero', 'NewHero')
        self.assertEqual(touched, 2)
        self.assertEqual(seen, [('test', cid, 'TestHero', 'NewHero')])
        self.assertIs(st.character_by_cid(cid)[2], hero)
        self.assertEqual(hero['name'], 'NewHero')
        self.assertIs(st.rename_rewriters[0], S.rewrite_social_references)


class C4RenameHook2009(_World, unittest.TestCase):
    """The world hook ON_RENAME: the messenger (friends' 0x0B, the mentor's 0x7B), the party,
    and any group that registers (P12 / P14) get (old, new, cid) after the 0x73 / 0x74."""

    def prepare(self):
        with self.server.store.lock:
            self.hero['mentor'] = 'Watcher'
            self.watcher['mentees'] = ['TestHero']
            self.watcher['friends'] = ['TestHero']

    def test_hook_subscribers_and_the_mentor_row(self):
        a, b = self.a, self.b
        hooks = self.server.world.hooks
        self.assertIn(worldmod.ON_RENAME, worldmod.HOOK_NAMES)
        # messenger + blacklist + party + guild (P14 g4 sub 22) + pets (P15 pet-s6: owner renames)
        self.assertEqual(len(hooks.registered(worldmod.ON_RENAME)), 5)
        got = []
        hooks.register(worldmod.ON_RENAME, lambda server, session, **kw: got.append((session['char_name'], kw)))
        self.send(b, 0x2F)                                                 # b's messenger is synced
        pkts = b.recv_until_quiet(0.3)
        self.assertIn(0x7E, _ops(pkts))                                    # TestHero as an online mentee
        nick = self.server.cash.grant(self.hero, NICK)
        self.send(a, 0x49, {'new_name': 'NewHero'})
        mine = a.recv_until_quiet(0.3)
        self.assertEqual(_ops(mine)[:2], [0x73, 0x74])
        self.assertEqual(a.s2c(mine[0]), CU.name_change_fields(True, nick['serial']))
        theirs = {p.opcode: p for p in b.recv_until_quiet(0.3)}
        self.assertEqual(b.s2c(theirs[0x7B])['mentee_uid'], 1)
        self.assertEqual(_text(b.s2c(theirs[0x7B])['mentee_name']), 'NewHero')
        self.assertEqual([_text(r['name']) for r in b.s2c(theirs[0x0B])['repeat[friend_count]']], ['NewHero'])
        self.assertEqual(got, [('NewHero', {'old': 'TestHero', 'new': 'NewHero', 'cid': self.hero['cid']})])
        self.assertEqual(self.watcher['mentees'], ['NewHero'])


# ===================================================================== C5 ===
class C5NameChangeBuilder(unittest.TestCase):
    def test_one_builder_both_forms_both_builds(self):
        self.assertEqual(CU.name_change_fields(True, 0x1234), {'result': 1, 'item_serial': 0x1234})
        self.assertEqual(CU.name_change_fields(False), {'result': 0})
        self.assertEqual(PETS.name_change_refusal(), {'result': 0})
        for build in (B8, B9):
            self.assertEqual(P.build('0x73', CU.name_change_fields(False), client_build=build), b'\x00')
            self.assertEqual(P.build('0x73', CU.name_change_fields(True, 5), client_build=build), b'\x01\x05\x00\x00\x00')
        # the registry backstops are the same bytes: 0x49 (both builds) and the 2009 0x4D
        for build in (B8, B9):
            self.assertEqual(registry.must_reply_table(build)[0x49].refusal(None, {}, None),
                             [('0x73', CU.name_change_fields(False), None)])
        must9 = registry.must_reply_table(B9)
        self.assertEqual(must9[0x4D].refusal(None, {}, None), [('0x73', CU.name_change_fields(False), None)])
        self.assertNotIn(0x4D, registry.MUST_REPLY)
        self.assertEqual(W.GameServer.ROUTES_2009[0x4D].handler, '_handle_pet_rename')


class C5PetRename2009(_World, unittest.TestCase):
    def test_pet_rename_gets_one_0x73_refusal(self):
        a = self.a
        with self.assertLogs('WS', level='INFO') as cm:
            self.send(a, 0x4D, {'pet_serial': 0x2000, 'new_name': b'Tweety'})
            pkt = self.only(a, 0x73)
        self.assertEqual(pkt.payload, b'\x00')
        self.assertTrue(any('pet rename refused' in line for line in cm.output), cm.output)
        self.assertFalse(any(r.levelno >= logging.ERROR for r in cm.records))


# ===================================================================== C6 ===
class C6PetItemsVia0x48_2009(_World, unittest.TestCase):
    def use(self, c, item):
        self.send(c, 0x48, {'item_id': item})
        return c.s2c(self.only(c, 0x72))

    def test_food_and_ticket_route_to_the_pet_hook(self):
        a, uid = self.a, 1
        food = self.server.cash.grant(self.hero, FOOD20)
        ticket = self.server.cash.grant(self.hero, TICKET)
        self.assertEqual(food['qty'], 20)
        refused = {'player_uid': uid, 'item_id': 0, 'item_serial': 0}
        with self.assertLogs('WS', level='INFO') as cm:
            self.assertEqual(self.use(a, FOOD20), refused)                  # no pet worn: kept
        self.assertTrue(any('no pet worn' in line for line in cm.output), cm.output)
        self.assertEqual(self.owned()[food['serial']]['qty'], 20)
        self.assertEqual(self.use(a, TICKET), refused)                      # the rename window is cp-2's
        self.assertEqual(self.owned()[ticket['serial']]['qty'], 1)
        with self.server.store.lock:
            CASH.ensure(self.hero).append(_pet_rec(0x2000, equipped=True, gauge=5, awake=False))
        self.assertEqual(self.use(a, FOOD20), {'player_uid': uid, 'item_id': FOOD20, 'item_serial': food['serial']})
        self.assertEqual(self.owned()[food['serial']]['qty'], 19)            # the client's own consume
        pet = self.owned()[0x2000]['pet']
        self.assertEqual((pet['gauge'], pet['awake']), (PETS.FOOD_GAUGE, True))


class C6PetItems2008(_World, unittest.TestCase):
    build = B8

    def test_the_2008_client_has_no_such_items(self):
        self.send(self.a, 0x48, {'item_id': FOOD20})
        self.assertEqual(self.a.s2c(self.only(self.a, 0x72)), {'player_uid': 1, 'item_id': 0, 'item_serial': 0})
        self.assertIn(FOOD20, self.server.cashuse.gates)                   # registered, never reached


# ===================================================================== C7 ===
class _C7:
    def test_grant_box_record_origin_3_and_0x6c(self):
        a = self.a
        acc = self.server.store.account('test')
        self.server.cash.set_balance(acc, cash=700, mileage=40)
        rec, told = self.server.mall.grant_box('test', MEGAPHONE, reason='test')
        self.assertTrue(told)
        pkt = self.only(a, 0x6C)
        self.assertEqual(len(pkt.payload), 37)
        got = a.s2c(pkt)
        self.assertEqual((got['result'], got['cash_balance'], got['mileage_balance'], got['item_serial'],
                          got['item_id'], got['item_kind'], got['quantity'], got['origin']),
                         (1, 700, 40, rec['serial'], MEGAPHONE, 1, rec['qty'], 3))
        self.assertEqual(CASH.balance(acc), (700, 40))                     # mileage unchanged
        self.assertIn(rec['serial'], [r['serial'] for r in acc['cash_box']])
        # an offline owner: stored, not told
        rec2, told2 = self.server.mall.grant_box('carol', MEGAPHONE)
        self.assertFalse(told2)
        self.assertIn(rec2['serial'], [r['serial'] for r in self.server.store.account('carol')['cash_box']])
        for bad, why in ((TAB_EXT, 'slot extension'), (STICK, 'not a cash item'), (99999, 'not in the client'),
                         (FEDORA, 'cash costume'), (GIFT_CERT, 'Cash 0')):
            with self.subTest(bad=bad), self.assertRaisesRegex(CASH.CashError, why):
                self.server.mall.grant_box('test', bad)
        with self.assertRaisesRegex(CASH.CashError, 'no account'):
            self.server.mall.grant_box('nobody', MEGAPHONE)
        with self.server.store.lock:
            acc['cash_box'].extend(CASH.make_record(0x9000 + i, MEGAPHONE, 1, 1) for i in range(MALL.BOX_MAX))
        with self.assertRaisesRegex(CASH.CashError, 'full'):
            self.server.mall.grant_box('test', MEGAPHONE)

    def test_a_count_above_stack_max_is_split_like_grant(self):
        acc = self.server.store.account('carol')
        rec, told = self.server.mall.grant_box('carol', MEGAPHONE, CASH.STACK_MAX * 2 + 10)
        box = [r for r in acc['cash_box'] if r['item_id'] == MEGAPHONE]
        self.assertEqual([r['qty'] for r in box], [CASH.STACK_MAX, CASH.STACK_MAX, 10])
        self.assertEqual((rec, told), (box[0], False))
        self.assertEqual(len({r['serial'] for r in box}), 3)
        self.assertTrue(all(r['origin'] == CASH.ORIGIN_EVENT for r in box))

    def test_the_gm_command(self):
        a = self.a
        self.line(a, b'!cash box 3381 2 Watcher')
        pkt = self.only(self.b, 0x6C)
        self.assertEqual(self.b.s2c(pkt)['origin'], 3)
        a.recv_until_quiet(0.2)


class C7Grant2008(_C7, _World, unittest.TestCase):
    build = B8


class C7Grant2009(_C7, _World, unittest.TestCase):
    def test_inside_the_mall_the_0x6c_joins_the_open_box(self):
        """An owner in the mall whose 0x6A is out gets the 0x6C (the record appended to his
        box list) and the mall snapshot learns the serial, so the 0x42 close knows it."""
        a = self.a
        self.line(a, b'!mall')
        self.assertTrue(_wait(lambda: a.session.get('in_cash_shop') and (a.session.get('mall') or {}).get('open')))
        a.recv_until_quiet(0.3)
        rec, told = self.server.mall.grant_box('test', MEGAPHONE)
        self.assertTrue(told)
        self.assertEqual(a.s2c(self.only(a, 0x6C))['item_serial'], rec['serial'])
        self.assertIn(rec['serial'], a.session['mall']['box'])

    def test_a_pet_goes_into_the_box_unbound(self):
        rec, told = self.server.mall.grant_box('test', PICKY)
        pkt = self.only(self.a, 0x6C)
        self.assertEqual(pkt.payload[9:], CASH.record_bytes(rec))
        self.assertEqual((pkt.payload[9 + 6], pkt.payload[9 + CASH.ORIGIN_OFFSET]), (3, 3))   # kind 3, origin 3
        self.assertEqual((rec['kind'], rec['pet']['level'], rec['origin']), (3, 0, 3))


# ===================================================================== C8 ===
class C8CashOpcodes2009(_World, unittest.TestCase):
    def test_sale_offer_reply_and_add_option(self):
        a = self.a
        self.send(a, 0x80, {'item_code': MEGAPHONE, 'option': 0, 'buyer_name': b'Watcher', 'price': 1000})
        self.assertEqual(self.only(a, 0x71).payload, b'\x01\x17')
        self.send(a, 0x81, {'reply': 1})                                   # the seller's Cancel
        self.assertEqual(self.only(a, 0x71).payload, b'\x01\x16')
        for reply in (0, 2):                                               # the buyer's: no offer pending
            with self.subTest(reply=reply):
                self.send(a, 0x81, {'reply': reply})
                self.assertEqual(a.recv_until_quiet(0.3), [])
        self.send(a, 0x4E, {'pay_with_mileage': 0, 'item_id': MEGAPHONE, 'option': 1, 'serial': 0x1000})
        self.assertEqual(self.only(a, 0xC4).payload, b'\x00')
        self.assertEqual(self.b.recv_until_quiet(0.1), [])

    def test_ownership_and_must_reply_rows(self):
        routes = W.GameServer.ROUTES_2009
        self.assertEqual({op: routes[op].handler for op in (0x4D, 0x4E, 0x80, 0x81)},
                         {0x4D: '_handle_pet_rename', 0x4E: '_handle_cash_add_option',
                          0x80: '_handle_cash_sale_offer', 0x81: '_handle_cash_sale_reply'})
        must9 = registry.must_reply_table(B9)
        self.assertEqual(must9[0x80].refusal(None, {}, None), [('0x71', {'is_trade': 1, 'result': 0x17}, None)])
        self.assertEqual(must9[0x4E].refusal(None, {}, None), [('0xC4', {'result': 0}, None)])
        self.assertNotIn(0x81, must9)                                      # no waiting box
        # the server never builds the sale's S2C 0xA9 / 0xAA
        for name in sorted(os.listdir(HERE)):
            if name.endswith('.py') and not re.search(r'(^test_|Name clash|snapshot)', name):
                with open(os.path.join(HERE, name), encoding='utf-8', errors='replace') as f:
                    text = f.read()
                self.assertIsNone(re.search(r"['\"]0x(A9|AA)['\"]", text), name)


if __name__ == '__main__':
    unittest.main()
