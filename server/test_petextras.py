#!/usr/bin/env python3
"""
test_petextras.py - P15 stage 3 (p15-pet-extras): pet-s6 rename, pet-s7 mall, the C4 owner-rename hook
=====================================================================================================
  Builders        S2C 0xC0 / 0xB0 bytes (17 B; names cut inside str[13], H4; the receiver gates
                  stated), pet_name_refusal (FUN_0043e1b0 rules inside 12 bytes), pet_drawn (cp-1
                  CardNpc: no sprite template -> no 0xB0 / 0xC0, H2);
  Rename2009      T15: C2S 0x4D -> 0xC0 {ticket serial, name} to A + 0xB0 {uid, name} to B (holding
                  the pet_info); ticket consumed, name stored, B's next record carries it; T16 and
                  every refusal -> exactly one 0x73 {0}, nothing consumed, no 0xC0 / 0xB0 (asleep,
                  wrong serial, not bound, unpatched hii, no ticket, invalid / 13-byte / same name,
                  in the mall); a viewer without the pet_info gets no 0xB0; `!pet set name` on an
                  awake pet -> 0xB0; `!pet ticket`;
  RenameKr2009    the stock exe ('kr'): the ticket through C2S 0x48 stays refused and kept; a 0x4D
                  is answered the same way;
  OwnerRename2009 the C4 hook ON_RENAME: C2S 0x49 renames A with Picky out - no pet packet to anyone,
                  the record / pet_bound / B's pet_info unchanged, the hook's report; a pet rename and
                  B's next record after it;
  MallPets2009    pet-s7: MALL_PETS on by default; every Cash_Cls 17-19 row sold by its EN id but the
                  Cash 0 bell; buy a pet, gear, food and a ticket -> box records (the pet at level 0:
                  0x6C raw / 0x6A pet branch) -> the 0x42 move: the pet bound (F13), the gear in the
                  equipment tab, food / ticket in the cash bag -> wear, dress and rename them; a bound
                  pet never goes back (gear does, unless worn); a full equipment tab keeps them in the
                  box; gifts and grant_box of a pet / gear;
  MallPetsOff2009 MALL_PETS false: pet / gear buys refused (0x6C {0}), food / tickets still sold;
  Extras2008      the 2008 build: no 0x4D route, no pets ON_RENAME hook, no pet rows in the mall, a
                  costume refused as before, a character rename with pet store data sends nothing pet;
                  2009 pet gear in the box / cash_items / grid 23 never reaches the 2008 0x6A, 0x6F
                  or record (cash.foreign_to_client).

Fake clients only (fakeclient.MultiClient); no port is bound, no game client started, and the
live accounts.json is never opened (temp copies; the module checks its hash).
"""
import hashlib
import logging
import os
import struct
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fakeclient as F  # noqa: E402
import cash as CASH  # noqa: E402
import cashuse as CU  # noqa: E402
import clientview as cview  # noqa: E402
import inventory as INV  # noqa: E402
import mall as MALL  # noqa: E402
import packets as P  # noqa: E402
import pets as PETS  # noqa: E402
import records as R  # noqa: E402
import world as worldmod  # noqa: E402
from test_pets import _PetWorld2009, _World, _wait, PORTAL_101_TO_102, PORTAL_102_TO_101  # noqa: E402

W = F.import_server()
cfgmod = W.cfgmod
EC = W.EC
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = cfgmod.BUILD_2008, cfgmod.BUILD_2009
DIRS = {B8: cfgmod.DEFAULTS['CLIENT_DIR'], B9: cfgmod.DEFAULTS['CLIENT_DIR_2009']}

PICKY, ULIE = 4294, 4299
RED_HOOD, SKULL_HOOD = 4290, 4297                # Picky gear (Kind 16) / Ulie gear (Kind 15)
FOOD20, TICKET, BELL, NICK, FEDORA = 4289, 4322, 4285, 1895, 1848
RENAME_4D, RENAME_49 = '0x469072/0x4D', '0x469072/0x49'
BUY_43, CLOSE_42, GIFT_47 = '0x468D84/0x43', '0x4655F0/0x42', '0x467D8C/0x47'
EQUIP_82, EQUIP_0F = '0x450019/0x82', '0x4500D1/0x0F'
USE_48 = '0x48'
PET_OPS = {0xAB, 0xAC, 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xC0}

_LIVE_HASH = None


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def setUpModule():
    global _LIVE_HASH
    _LIVE_HASH = _sha(LIVE_ACCOUNTS) if os.path.exists(LIVE_ACCOUNTS) else None
    P.STRICT_FIELDS.add(B9)


def tearDownModule():
    P.STRICT_FIELDS.discard(B9)
    EC.configure(DIRS[B8], B8)
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_petextras.py (tests must only use temp copies)'


def _ops(pkts):
    return [p.opcode for p in pkts]


def _name13(text):
    raw = text.encode() if isinstance(text, str) else text
    return raw + bytes(13 - len(raw))


class _Def:
    def __init__(self, id_, card_npc, type_=6, kind=14, cash=1):
        self.id, self.card_npc, self.type, self.kind, self.cash = id_, card_npc, type_, kind, cash


class _Catalog(dict):
    client_build = B9


# ================================================================ builders ===
class Builders(unittest.TestCase):
    def test_c0_b0_bytes_and_gates(self):
        body = P.build('0xC0', *PETS.c0_fields(0x2001, 'Tweety'), client_build=B9)
        self.assertEqual(body, struct.pack('<I', 0x2001) + _name13('Tweety'))        # 17 B
        body = P.build('0xB0', *PETS.b0_fields(1, 'Tweety'), client_build=B9)
        self.assertEqual(body, struct.pack('<I', 1) + _name13('Tweety'))
        # H4: a long name is cut inside str[13] with its NUL
        body = P.build('0xB0', *PETS.b0_fields(1, 'ABCDEFGHIJKLMNOP'), client_build=B9)
        self.assertEqual((len(body), body[4:]), (17, b'ABCDEFGHIJKL\x00'))
        with self.assertRaises(P.MissingAssume):                            # the receiver gate is stated
            P.build('0xB0', {'uid': 1, 'pet_name': 'x'}, client_build=B9)
        with self.assertRaises(P.MissingAssume):
            P.build('0xC0', {'cash_item_serial': 1, 'pet_name': 'x'}, client_build=B9)
        with self.assertRaises(KeyError):                                   # no 2008 handler at all
            P.build('0xC0', *PETS.c0_fields(1, 'x'), client_build=B8)

    def test_pet_name_rules(self):
        self.assertIsNone(PETS.pet_name_refusal('Tweety'))
        self.assertIsNone(PETS.pet_name_refusal(b'Tweety\x00junk'))          # cut at the NUL
        self.assertIsNone(PETS.pet_name_refusal('ABCDEFGHIJKL'))             # 12 bytes
        for bad in ('', 'two words', 'Windy', 'GameMaster1', "Pi'ky", 'Tw$ty', 'ABCDEFGHIJKLM'):
            with self.subTest(bad=bad):
                self.assertIsNotNone(PETS.pet_name_refusal(bad))

    def test_pet_drawn_is_the_cp1_card_npc(self):
        cat = _Catalog({PICKY: _Def(PICKY, 182), ULIE: _Def(ULIE, 0)})
        self.assertTrue(PETS.pet_drawn(PICKY, cat))
        self.assertFalse(PETS.pet_drawn(ULIE, cat))                          # stock hii: no sprite (B1)
        self.assertFalse(PETS.pet_drawn(1, cat))
        self.assertFalse(PETS.pet_drawn(0, cat))


# ================================================================ rename ===
class _Rename2009(_PetWorld2009):
    """TestHero (uid 1) wearing an awake Picky, one pet name ticket; Watcher holds the pet_info."""

    def setUp(self):
        super().setUp()
        self.give(wear=True)
        self.ticket = self.server.cash.grant(self.hero, TICKET)
        self.a.recv_until_quiet(0.1)
        self.b.recv_until_quiet(0.1)
        self.assertTrue(self.seen())
        # The tests describe the cp-1 install (the live default, G-CP). The real check runs
        # whenever the local hii is patched; an unpatched one is stood in for here, and its
        # own refusal is tested explicitly (test_refusals_one_0x73_each).
        if not PETS.pet_drawn(PICKY):
            patcher = mock.patch.object(PETS, 'pet_drawn', return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def rename(self, name, serial=None, raw=None):
        serial = self.pet()['serial'] if serial is None else serial
        if raw is not None:
            self.a.send(0x4D, struct.pack('<I', serial) + raw)
        else:
            self.a.send_c2s(RENAME_4D, {'pet_serial': serial, 'new_name': name.encode()})
        return self.a.recv_until_quiet(0.3), self.b.recv_until_quiet(0.15)

    def ticket_left(self):
        return sum(r['qty'] for r in self.hero['cash_items'] if r['item_id'] == TICKET)


class Rename2009(_Rename2009, unittest.TestCase):
    def test_t15_rename_owner_and_viewer(self):
        a, b = self.a, self.b
        with self.assertLogs('WS', level='INFO') as cm:
            a_pkts, b_pkts = self.rename('Tweety')
        self.assertEqual(_ops(a_pkts), [0xC0])                               # one reply, no 0x73
        self.assertEqual(a_pkts[0].payload, struct.pack('<I', self.ticket['serial']) + _name13('Tweety'))
        got = a.s2c(a_pkts[0], assume={PETS.C0_GATE: False})
        self.assertEqual((got['cash_item_serial'], got['pet_name']), (self.ticket['serial'], 'Tweety'))
        self.assertEqual(_ops(b_pkts), [0xB0])
        self.assertEqual(b_pkts[0].payload, struct.pack('<I', 1) + _name13('Tweety'))
        self.assertTrue(any("'Picky' -> 'Tweety'" in line and '0xB0 to 1 viewer' in line for line in cm.output),
                        cm.output)
        self.assertEqual(self.pet()['pet']['name'], 'Tweety')
        self.assertEqual(self.ticket_left(), 0)                              # the ticket record is gone
        self.assertTrue(self.server.store.dirty)
        self.assertEqual(R.pet_block(self.hero), {'has_pet': 1, 'pet_level': 1, 'pet_name': 'Tweety'})
        # B's next record of A carries the new name
        self.portal(b, PORTAL_101_TO_102, 102)
        a.recv_until_quiet(0.2)
        rows = self.rows_of(b, self.portal(b, PORTAL_102_TO_101, 101))
        self.assertEqual([(r['has_pet'], r['pet_name']) for r in rows], [(1, 'Tweety')])
        # no ticket left: the next request is refused
        a.recv_until_quiet(0.2)
        a_pkts, b_pkts = self.rename('Robin')
        self.assertEqual(([(p.opcode, p.payload) for p in a_pkts], _ops(b_pkts)), ([(0x73, b'\x00')], []))
        self.assertEqual(self.pet()['pet']['name'], 'Tweety')

    def test_t16_asleep_is_refused_without_a_crash_packet(self):
        self.gm('!pet set awake 0')
        a_pkts, b_pkts = self.rename('Tweety')
        self.assertEqual([(p.opcode, p.payload) for p in a_pkts], [(0x73, b'\x00')])
        self.assertEqual(_ops(b_pkts), [])                                   # H2: no 0xB0 / 0xC0
        self.assertEqual((self.pet()['pet']['name'], self.ticket_left()), ('Picky', 1))

    def test_refusals_one_0x73_each(self):
        a = self.a
        serial = self.pet()['serial']
        cases = [('wrong serial', dict(name='Tweety', serial=serial + 1)),
                 ('space', dict(name='two words')),
                 ('reserved word', dict(name='Windy')),
                 ('not [A-Za-z0-9]', dict(name='Tw$ty')),
                 ('empty', dict(name='')),
                 ('13 bytes, no NUL', dict(name=None, raw=b'ABCDEFGHIJKLM')),
                 ('the same name', dict(name='Picky'))]
        for why, kw in cases:
            with self.subTest(why=why), self.assertLogs('WS', level='INFO') as cm:
                a_pkts, b_pkts = self.rename(**kw)
                self.assertEqual([(p.opcode, p.payload) for p in a_pkts], [(0x73, b'\x00')])
                self.assertEqual(_ops(b_pkts), [])
                self.assertTrue(any('pet rename refused' in line for line in cm.output), cm.output)
        # the client has not bound the record (+0x1628): its 0xC0 would read nothing
        a.session['pet_bound'] = None
        try:
            a_pkts, _ = self.rename('Tweety')
        finally:
            a.session['pet_bound'] = serial
        self.assertEqual(_ops(a_pkts), [0x73])
        # an unpatched hii (CardNpc 0): no client built a sprite, so 0xC0 / 0xB0 would crash (B1 / H2)
        with mock.patch.object(PETS, 'pet_drawn', return_value=False):
            a_pkts, b_pkts = self.rename('Tweety')
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([0x73], []))
        # in the mall (the preview avatar is the local player there)
        a.session['in_cash_shop'] = True
        try:
            a_pkts, _ = self.rename('Tweety')
        finally:
            a.session['in_cash_shop'] = False
        self.assertEqual(_ops(a_pkts), [0x73])
        self.assertEqual((self.pet()['pet']['name'], self.ticket_left()), ('Picky', 1))     # nothing consumed
        # and then it works
        a_pkts, b_pkts = self.rename('Tweety')
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([0xC0], [0xB0]))

    def test_no_ticket_and_a_viewer_without_the_pet_info(self):
        a, b = self.a, self.b
        self.server.cash.consume(self.hero, self.ticket['serial'], 1)
        a_pkts, b_pkts = self.rename('Tweety')
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([0x73], []))
        self.ticket = self.server.cash.grant(self.hero, TICKET)
        with cview.lock(b.session):
            cview.forget_pet_info(b.session, 1)
        a_pkts, b_pkts = self.rename('Tweety')
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([0xC0], []))         # B reads no 0xB0 without it

    def test_two_tickets_spend_one_a_time(self):
        self.server.cash.grant(self.hero, TICKET)                           # merges: qty 2
        self.assertEqual(self.ticket_left(), 2)
        a_pkts, _ = self.rename('Tweety')
        self.assertEqual(struct.unpack('<I', a_pkts[0].payload[:4])[0], self.ticket['serial'])
        self.assertEqual(self.ticket_left(), 1)

    def test_gm_set_name_and_ticket(self):
        a_pkts, b_pkts = self.gm('!pet set name Robin')
        self.assertIn(0x6F, _ops(a_pkts))                                   # the owner re-binds by 0x6F
        self.assertEqual([(p.opcode, p.payload) for p in b_pkts], [(0xB0, struct.pack('<I', 1) + _name13('Robin'))])
        self.assertNotIn(0xC0, _ops(a_pkts))                                # no ticket: no 0xC0
        a_pkts, _ = self.gm('!pet ticket 2')
        self.assertIn(0x6F, _ops(a_pkts))
        self.assertEqual(self.ticket_left(), 3)
        self.assertEqual(self.server.routes[0x4D].handler, '_handle_pet_rename')


class RenameKr2009(_Rename2009, unittest.TestCase):
    config = {'CLIENT_ITEM_IDS': 'kr'}

    def test_stock_exe_ticket_via_0x48_kept_and_0x4d_alike(self):
        a = self.a
        a.send_c2s(P.variants(0x48, 'C2S', client_build=B9)[0]['key'], {'item_id': TICKET})
        pkts = a.recv_until_quiet(0.3)
        self.assertEqual([p.opcode for p in pkts], [0x72])
        self.assertEqual((a.s2c(pkts[0])['item_id'], self.ticket_left()), (0, 1))     # refused, kept
        a_pkts, b_pkts = self.rename('Tweety')                              # a forged / future 0x4D
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([0xC0], [0xB0]))


# ====================================================== C4: owner rename ===
class OwnerRename2009(_Rename2009, unittest.TestCase):
    def test_owner_rename_keeps_the_pet_and_sends_no_pet_packet(self):
        a, b = self.a, self.b
        pets_hooks = [fn for fn in self.server.world.hooks.registered(worldmod.ON_RENAME)
                      if getattr(fn, '__module__', '') == 'pets']
        self.assertEqual(len(pets_hooks), 1)
        serial = self.pet()['serial']
        self.server.cash.grant(self.hero, NICK)
        reports = []
        real = self.server.pets.owner_renamed
        with mock.patch.object(self.server.pets, 'owner_renamed',
                               side_effect=lambda *args: reports.append(real(*args)) or reports[-1]):
            with self.assertLogs('WS', level='INFO') as cm:
                a.send_c2s(RENAME_49, {'new_name': b'NewHero'})
                a_pkts = a.recv_until_quiet(0.3)
                b_pkts = b.recv_until_quiet(0.2)
        self.assertEqual(_ops(a_pkts)[:2], [0x73, 0x74])
        self.assertIn(0x74, _ops(b_pkts))
        self.assertFalse(PET_OPS & set(_ops(a_pkts) + _ops(b_pkts)))           # no pet packet (H2)
        self.assertEqual(reports, [{'worn': serial, 'bound': True, 'holders': ['Watcher']}])
        self.assertTrue(any("owner 'TestHero' is now 'NewHero'" in line for line in cm.output), cm.output)
        self.assertEqual(self.hero['name'], 'NewHero')
        worn = self.pet()
        self.assertEqual((worn['serial'], worn['pet']['name'], worn['pet']['awake'], a.session['pet_bound']),
                         (serial, 'Picky', True, serial))
        self.assertTrue(self.seen())                                        # B keeps A's pet_info (uid 1)
        # the pet still renames, and B's next record shows both names
        a_pkts, b_pkts = self.rename('Tweety')
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([0xC0], [0xB0]))
        self.portal(b, PORTAL_101_TO_102, 102)
        a.recv_until_quiet(0.2)
        pkts = self.portal(b, PORTAL_102_TO_101, 101)
        rows = self.rows_of(b, pkts)
        self.assertEqual([(P.to_bytes(r['name']).split(b'\x00')[0], r['has_pet'], r['pet_name']) for r in rows],
                         [(b'NewHero', 1, 'Tweety')])

    def test_report_without_a_pet(self):
        self.take_off()
        out = self.server.pets.owner_renamed(self.a.session, 'TestHero', 'TestHero')
        self.assertEqual(out, {'worn': None, 'bound': False, 'holders': []})


# =================================================================== mall ===
class _Mall2009(_PetWorld2009):
    def setUp(self):
        super().setUp()
        self.server.cash.set_balance(self.server.store.account('test'), cash=30000)

    def enter_mall(self, c=None):
        c = c or self.a
        self.line(c, b'!mall')
        self.assertTrue(_wait(lambda: c.session.get('in_cash_shop')))
        pkts = c.recv_until_quiet(0.3)
        self.assertEqual(pkts[-1].opcode, 0x6A, _ops(pkts))
        return pkts

    def close(self, c=None, to_box=(), to_char=()):
        c = c or self.a
        c.send_c2s(CLOSE_42, {'storage_deleted_flag': 0, 'to_storage_count': len(to_box),
                              'to_character_count': len(to_char),
                              'repeat[to_storage_count]': [{'item_serial': s} for s in to_box],
                              'repeat[to_character_count]': [{'item_serial': s} for s in to_char]})
        self.assertTrue(_wait(lambda: c.session.get('in_world') and not c.session.get('in_cash_shop')))
        return c.recv_until_quiet(0.3)

    def buy(self, *items):
        self.a.send_c2s(BUY_43, {'pay_with_mileage': 0, 'item_count': len(items),
                                 'repeat[item_count]': [{'item_code': i, 'option': 0} for i in items]})
        return [p for p in self.a.recv_until_quiet(0.3) if p.opcode == 0x6C]

    def box(self, username='test'):
        return self.server.store.account(username)['cash_box']

    def boxed(self, item, username='test'):
        return [r for r in self.box(username) if r['item_id'] == item]

    def mine(self, item):
        return [r for r in self.hero['cash_items'] if r['item_id'] == item]


class MallPets2009(_Mall2009, unittest.TestCase):
    def test_the_pet_tabs_are_sold_by_en_id(self):
        self.assertTrue(cfgmod.defaults()['MALL_PETS'])
        cat = EC.items()
        rows = {i: cat.get(i) for i in cat.defs if cat.get(i).cash_cls in (17, 18, 19)}
        self.assertEqual(sorted(rows), list(range(4285, 4310)) + [TICKET])
        for item, d in rows.items():
            with self.subTest(item=item):
                cd = CASH.cash_def(item)
                if item == BELL:                                            # Cash 0: an NPC item (cp-3)
                    self.assertIn('not sold', MALL.sale_refusal(cd, item, pets=True))
                    continue
                self.assertIsNone(MALL.sale_refusal(cd, item, pets=True))
                pet_item = cd.is_pet or INV.is_pet_gear(item)
                self.assertEqual(MALL.is_pet_item(cd), pet_item)
                self.assertEqual(MALL.sale_refusal(cd, item, pets=False) is None, not pet_item)
        self.assertEqual([EC.item_name(i) for i in (PICKY, RED_HOOD, FOOD20, TICKET)],
                         ['Picky', 'Red Hood', 'Pet Food 20 ea.', 'Pet name making'])

    def test_buy_move_wear_dress_and_rename(self):
        a, b = self.a, self.b
        self.enter_mall()
        sixc = self.buy(PICKY, RED_HOOD, FOOD20, TICKET)
        got = [a.s2c(p) for p in sixc]
        self.assertEqual([(g['result'], g['item_id'], g['item_kind']) for g in got],
                         [(1, PICKY, 3), (1, RED_HOOD, 0), (1, FOOD20, 1), (1, TICKET, 1)])
        self.assertEqual(got[-1]['cash_balance'], 30000 - 4900 - 1800 - 500 - 1100)       # hii Cash_P
        pet, hood, food, ticket = (self.boxed(i)[0] for i in (PICKY, RED_HOOD, FOOD20, TICKET))
        self.assertEqual(sixc[0].payload[9:], CASH.record_bytes(pet))
        self.assertEqual((pet['pet']['level'], pet['pet']['awake'], pet['origin']), (0, False, CASH.ORIGIN_CASH))
        self.assertEqual(sixc[0].payload[9 + 0x0B], 0)                       # +0x0B level 0: unbound box pet
        self.assertEqual((hood['kind'], hood['qty'], food['qty'], ticket['qty']), (CASH.KIND_PERMANENT, 1, 20, 1))
        # the next 0x6A lists the pet in the grammar's pet branch at level 0
        self.close()
        pkts = self.enter_mall()
        rows = {r['item_id']: r for r in a.s2c(pkts[-1])['repeat[box_count]']}
        self.assertEqual((rows[PICKY]['limit_type'], rows[PICKY]['pet_level'], rows[PICKY]['pet_summoned']), (3, 0, 0))
        # the 0x42 move to the bag: the pet bound (F13), gear in the equipment tab, food / ticket in the cash bag
        used = INV.Inventory(self.hero).used_slots('equip')
        with self.assertLogs('WS', level='INFO') as cm:
            pkts = self.close(to_char=[pet['serial'], hood['serial'], food['serial'], ticket['serial']])
        self.assertTrue(any('pets bound to the character (F13)' in line for line in cm.output), cm.output)
        self.assertEqual(self.box(), [])
        moved = self.mine(PICKY)[0]
        self.assertEqual({k: moved['pet'][k] for k in ('level', 'exp', 'gauge', 'awake', 'name')},
                         {'level': 1, 'exp': 0, 'gauge': 100, 'awake': True, 'name': 'Picky'})
        self.assertEqual((moved['equipped'], self.mine(RED_HOOD)[0]['equipped']), (False, False))
        self.assertEqual(INV.Inventory(self.hero).used_slots('equip'), used + 2)
        self.assertEqual(MALL.cash_tab_used(self.hero), 2)
        six_f = {r['item_id']: r for p in pkts if p.opcode == 0x6F for r in a.s2c(p)['repeat[count]']}
        self.assertEqual((six_f[PICKY]['pet_level'], six_f[PICKY]['is_equipped'], six_f[RED_HOOD]['limit_type']),
                         (1, 0, 0))
        self.assertEqual(six_f[FOOD20]['quantity'], 20)
        b.recv_until_quiet(0.2)
        # wear the bought pet, dress it with the bought hood, rename it with the bought ticket
        a.send_c2s(EQUIP_82, {'pet_item_id': PICKY})
        self.assertEqual(_ops(a.recv_until_quiet(0.3)), [0xAB])
        b_pkts = b.recv_until_quiet(0.2)
        self.assertEqual((_ops(b_pkts), len(b_pkts[0].payload)), ([0xAB], 21))
        a.send_c2s(EQUIP_0F, {'item_id': RED_HOOD, 'stone_count': 0})
        self.assertEqual((_ops(a.recv_until_quiet(0.3)), _ops(b.recv_until_quiet(0.2))), ([0x1D], [0x1D]))
        self.assertEqual(R.grid_item(a.session, self.hero, 24), RED_HOOD)
        if not PETS.pet_drawn(PICKY):
            self.skipTest('the local hii has no cp-1 CardNpc: the rename is refused by design (H2)')
        a.send_c2s(RENAME_4D, {'pet_serial': moved['serial'], 'new_name': b'Tweety'})
        a_pkts = a.recv_until_quiet(0.3)
        self.assertEqual(_ops(a_pkts), [0xC0])
        self.assertEqual(struct.unpack('<I', a_pkts[0].payload[:4])[0], ticket['serial'])
        self.assertEqual(_ops(b.recv_until_quiet(0.2)), [0xB0])
        self.assertEqual((self.pet()['pet']['name'], self.mine(TICKET)), ('Tweety', []))

    def test_a_bound_pet_never_goes_back_gear_does_unless_worn(self):
        a = self.a
        self.enter_mall()
        self.buy(PICKY, RED_HOOD, SKULL_HOOD)
        pet, hood, skull = (self.boxed(i)[0] for i in (PICKY, RED_HOOD, SKULL_HOOD))
        self.close(to_char=[pet['serial'], hood['serial'], skull['serial']])
        # wear Picky + its hood, then try to put everything back in the box
        a.send_c2s(EQUIP_82, {'pet_item_id': PICKY})
        a.send_c2s(EQUIP_0F, {'item_id': RED_HOOD, 'stone_count': 0})
        a.recv_until_quiet(0.3)
        self.self_check_worn()
        self.enter_mall()
        with self.assertLogs('WS', level='INFO') as cm:
            self.close(to_box=[pet['serial'], hood['serial'], skull['serial']])
        self.assertEqual([r['serial'] for r in self.box()], [skull['serial']])            # loose gear goes back
        self.assertEqual({r['serial'] for r in self.hero['cash_items']} >= {pet['serial'], hood['serial']}, True)
        self.assertFalse(any('bound pet(s)' in line for line in cm.output))  # worn: not a "bound pet" line
        # take the hood off and the pet off: the bagged, bound pet still never goes back
        a.send_c2s('0x477368/0x11', {'item_id': RED_HOOD, 'enchant_count': 0})
        a.send_c2s('0x47739D/0x83', {'pet_item_id': PICKY})
        a.recv_until_quiet(0.3)
        self.assertIsNone(self.pet())
        self.enter_mall()
        with self.assertLogs('WS', level='INFO') as cm:
            self.close(to_box=[pet['serial'], hood['serial']])
        self.assertTrue(any('bound pet(s)' in line and hex(pet['serial']) in line for line in cm.output), cm.output)
        self.assertIn(pet['serial'], {r['serial'] for r in self.hero['cash_items']})
        self.assertEqual({r['serial'] for r in self.box()}, {skull['serial'], hood['serial']})

    def self_check_worn(self):
        self.assertIsNotNone(self.pet())
        self.assertEqual(R.grid_item(self.a.session, self.hero, 24), RED_HOOD)

    def test_a_full_equipment_tab_keeps_pets_and_gear_in_the_box(self):
        self.enter_mall()
        self.buy(PICKY, ULIE, RED_HOOD, FOOD20)
        picky, ulie, hood, food = (self.boxed(i)[0] for i in (PICKY, ULIE, RED_HOOD, FOOD20))
        with self.server.store.lock:
            bag = INV.Inventory(self.hero)
            bag.set_capacity('equip', bag.used_slots('equip') + 1)          # one free slot
        self.close(to_char=[picky['serial'], ulie['serial'], hood['serial'], food['serial']])
        mine = {r['item_id'] for r in self.hero['cash_items']}
        self.assertEqual(mine & {PICKY, ULIE, RED_HOOD, FOOD20}, {PICKY, FOOD20})        # one slot: the first
        self.assertEqual({r['item_id'] for r in self.box()}, {ULIE, RED_HOOD})
        self.assertEqual([r['pet']['level'] for r in self.boxed(ULIE)], [0])              # still unbound

    def test_gifts_and_grant_box(self):
        a, b = self.a, self.b
        self.enter_mall()
        for item in (PICKY, RED_HOOD):
            a.send_c2s(GIFT_47, {'pay_with_mileage': 0, 'item_code': item, 'option': 0, 'recipient_name': b'Watcher',
                                 'message_len': 3, 'message': b'hi!'})
            got = a.s2c(next(p for p in a.recv_until_quiet(0.3) if p.opcode == 0x71))
            self.assertEqual((got['is_trade'], got['result']), (0, MALL.GIFT_OK))
        self.assertIn(0x6D, _ops(b.recv_until_quiet(0.3)))
        gift = self.boxed(PICKY, 'admin')[0]
        self.assertEqual((gift['pet']['level'], gift['origin']), (0, CASH.ORIGIN_GIFT))
        self.assertEqual(self.boxed(RED_HOOD, 'admin')[0]['kind'], CASH.KIND_PERMANENT)
        self.close()
        # C7 grant_box: pet gear can now leave the box, so it can be granted; a costume still not
        rec, told = self.server.mall.grant_box('admin', SKULL_HOOD)
        self.assertEqual((rec['item_id'], rec['kind'], rec['origin'], told), (SKULL_HOOD, 0, CASH.ORIGIN_EVENT, True))
        six_c = next(p for p in b.recv_until_quiet(0.3) if p.opcode == 0x6C)
        self.assertEqual(b.s2c(six_c)['item_id'], SKULL_HOOD)
        rec, _ = self.server.mall.grant_box('admin', ULIE)
        self.assertEqual(rec['pet']['level'], 0)
        with self.assertRaises(CASH.CashError):
            self.server.mall.grant_box('admin', FEDORA)


class MallPetsOff2009(_Mall2009, unittest.TestCase):
    config = {'MALL_PETS': False}

    def test_pets_and_gear_refused_food_and_tickets_sold(self):
        a = self.a
        self.enter_mall()
        for item in (PICKY, RED_HOOD):
            with self.subTest(item=item):
                sixc = self.buy(item)
                self.assertEqual([p.payload for p in sixc], [b'\x00'])       # 0x6C {0}
        sixc = self.buy(FOOD20, TICKET)
        self.assertEqual([a.s2c(p)['result'] for p in sixc], [1, 1])
        self.assertEqual({r['item_id'] for r in self.box()}, {FOOD20, TICKET})
        a.send_c2s(GIFT_47, {'pay_with_mileage': 0, 'item_code': PICKY, 'option': 0, 'recipient_name': b'Watcher',
                             'message_len': 0, 'message': b''})
        got = a.s2c(next(p for p in a.recv_until_quiet(0.3) if p.opcode == 0x71))
        self.assertEqual(got['result'], MALL.GIFT_FAILED)
        # a pet record already in a box still moves (and binds)
        rec, _ = self.server.mall.grant_box('test', PICKY)
        self.close()
        self.enter_mall()
        self.close(to_char=[rec['serial']])
        self.assertEqual(self.mine(PICKY)[0]['pet']['level'], 1)


# ================================================================== 2008 ===
class Extras2008(_World, unittest.TestCase):
    build = B8

    def prepare(self):
        with self.server.store.lock:
            pet = CASH.make_record(0x2000, PICKY, CASH.KIND_PET, 1, equipped=True, pet=CASH.new_pet(PICKY, bound=True))
            CASH.ensure(self.hero).append(pet)                              # store data from a 2009 server
            self.stored = dict(pet['pet'])

    def test_nothing_pet_on_the_2008_client(self):
        a, b = self.a, self.b
        self.assertIsNone(self.server.routes.get(0x4D))                     # no 2008 C2S 0x4D at all
        self.assertFalse([fn for fn in self.server.world.hooks.registered(worldmod.ON_RENAME)
                          if getattr(fn, '__module__', '') == 'pets'])
        self.assertTrue(cfgmod.defaults()['MALL_PETS'])
        self.assertIn('not in the client item table', MALL.sale_refusal(CASH.cash_def(PICKY), PICKY, pets=True))
        fedora = CASH.cash_def(FEDORA)
        self.assertFalse(MALL.is_pet_item(fedora))
        self.assertIn('cash costume', MALL.sale_refusal(fedora, FEDORA, pets=True))
        with self.assertRaises(CASH.CashError):
            self.server.mall.grant_box('test', FEDORA)
        # a character rename on the 2008 client: no pet hook, no pet packet
        self.server.cash.grant(self.hero, NICK)
        key = next(v['key'] for v in P.variants(0x49, 'C2S', client_build=B8))
        a.send_c2s(key, {'new_name': b'NewHero'})
        a_pkts, b_pkts = a.recv_until_quiet(0.3), b.recv_until_quiet(0.2)
        self.assertEqual(_ops(a_pkts)[:2], [0x73, 0x74])
        self.assertFalse(PET_OPS & set(_ops(a_pkts) + _ops(b_pkts)))
        self.assertIsNone(self.server.pets.rename(a.session.get('sock'), a.session,
                                                  {'pet_serial': 0x2000, 'new_name': b'Tweety'}))
        self.assertEqual(_ops(a.recv_until_quiet(0.2)), [0x73])             # the 2008 0x73 {0}, no 0xC0
        self.assertEqual(CU.name_change_fields(False), {'result': 0})
        pet = next(r for r in self.hero['cash_items'] if CASH.is_pet_record(r))
        self.assertEqual((pet['serial'], pet['equipped'], pet['pet']), (0x2000, True, self.stored))   # untouched

    def test_2009_gear_never_reaches_the_2008_client(self):
        """A store shared with a 2009 server: pet gear bought in its Spark Shop (MALL_PETS) sits
        in the box, in cash_items and worn in grid 23 / 24. The 2008 hii has no such row, so the
        0x6A box, the 0x6F owned list and the record's cash-equip words leave it out."""
        with self.server.store.lock:
            acc = self.server.store.account('test')
            CASH.ensure_account(acc)['cash_box'].append(CASH.make_record(0x2101, RED_HOOD, CASH.KIND_PERMANENT, 1))
            CASH.ensure_account(acc)['cash_box'].append(CASH.make_record(0x2102, FEDORA, CASH.KIND_PERMANENT, 1))
            CASH.ensure(self.hero).append(CASH.make_record(0x2103, SKULL_HOOD, CASH.KIND_PERMANENT, 1))
            CASH.ensure(self.hero).append(CASH.make_record(0x2104, FEDORA, CASH.KIND_PERMANENT, 1))
            self.hero['equipped'] = {23: {'id': RED_HOOD, 'w': [0] * 6}, 17: {'id': FEDORA, 'w': [0] * 6}}
            with self.assertLogs('WS', logging.WARNING) as logs:
                box = MALL.enter_fields(acc, B8)['repeat[box_count]']
                owned = CASH.owned_list_packets(self.hero, B8)[0]['repeat[count]']
            words = [r['cash_equip_item_id'] for r in R.cash_equip(None, self.hero)]
        self.assertEqual([r['item_id'] for r in box], [FEDORA])
        self.assertEqual([r['item_id'] for r in owned], [FEDORA])                 # the pet (2009 data) too
        self.assertEqual((words[17 - 16], words[23 - 16]), (FEDORA, 0))
        self.assertTrue(any('left out of the 2008 0x6A' in m for m in logs.output), logs.output)
        self.assertTrue(any('the 2008 client lacks' in m and '0x6F' in m for m in logs.output), logs.output)
        self.assertTrue(CASH.foreign_to_client(RED_HOOD, B8))
        self.assertFalse(CASH.foreign_to_client(FEDORA, B8))
        self.assertFalse(CASH.foreign_to_client(RED_HOOD, B9))


if __name__ == '__main__':
    unittest.main()
