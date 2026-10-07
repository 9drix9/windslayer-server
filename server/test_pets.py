#!/usr/bin/env python3
"""
test_pets.py - P15 stage 1 (p15-pet-core): cp-3, arch09-id-shift, pet-s1, pet-s2, both client builds
=====================================================================================================
  Config15        CLIENT_ITEM_IDS ('en' default / 'kr') and SHOP_EXTRA_ITEMS (cp-3) validation;
  IdShiftAudit    arch09-id-shift: the exe-constant map (en_content.exe_item_id) per exe, every
                  server constant above 4248 is the EN 2009 hii row it must be, the guild board
                  builders per CLIENT_ITEM_IDS (EN 4284 / 4283, KR 4280 / 4279);
  IdSpaces        H6: every server-allocated uid stays below the client's 33,000,000 pet base;
  Builders        the 0xAB / 0xAC / 0xAD / 0xAF forms per receiver (H3 local 6 / 5 B, remote 21 /
                  7 / 19 B), names cut inside str[13] (H4), a missing receiver gate raises;
  PetWorld2009    two 2009 fake clients (A = TestHero GM uid 1, B = Watcher uid 2) on map 101:
                  T1 give + wear (0x82 -> 0xAB local / remote, B's pet_info mirror), T2 / T17 /
                  T18 (arrival, portal, logout), unequip and the H1 gates (0xAC / 0xAD(0) only to
                  a holder of the pet_info), the gear gate (T12), a swap, sleep / wake, H2 (no
                  0xB0 / 0xC0), H5 (every 0x6F carries the worn pet), the level packet, the look
                  word 14, refusals;
  ShopExtras      cp-3: the Pet Bell sells at a potion grocer through SHOP_EXTRA_ITEMS (hii price),
                  never elsewhere, pet food never at an NPC; with the cp-3d hni (the grocer row
                  lists 4285 itself) the extra is skipped, so the bell is listed once on either
                  hni; `!pet bell` / `!pet food` GM grants;
  Feed0x48        the C6 food path under CLIENT_ITEM_IDS 'en' (the stock-exe hint) and 'kr', the
                  wake it causes (0xAD to the owner and the viewer);
  PetWorld2008    the 2008 build: no pet block, no pet route, `!pet` refused, no pet packet.

Fake clients only (fakeclient.MultiClient); no port is bound, no game client started, and the
live accounts.json is never opened (temp copies; the module checks its hash).
"""
import hashlib
import logging
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import fakeclient as F  # noqa: E402
import cash as CASH  # noqa: E402
import clientview as cview  # noqa: E402
import guild as G  # noqa: E402
import ids  # noqa: E402
import inventory as INV  # noqa: E402
import packets as P  # noqa: E402
import pets as PETS  # noqa: E402
import records as R  # noqa: E402

W = F.import_server()
cfgmod = W.cfgmod
EC = W.EC
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = cfgmod.BUILD_2008, cfgmod.BUILD_2009
DIRS = {B8: cfgmod.DEFAULTS['CLIENT_DIR'], B9: cfgmod.DEFAULTS['CLIENT_DIR_2009']}
HAVE = {b: os.path.exists(os.path.join(HERE, d, 'hs', 'windslayer.hii')) for b, d in DIRS.items()}

PICKY, ULIE = 4294, 4299
RED_HOOD = 4290                                  # [Picky] Red Hood: Type 1, Kind 16 -> grid 24
FOOD20, BELL, TICKET = 4289, 4285, 4322
MISTY, MURDOCK = 8, 11                           # a potion grocer / a merchant without the bell
CHAT = {B8: '0x445CA7/0x03', B9: '0x44790E/0x03'}
EQUIP_82, UNEQUIP_83 = '0x450019/0x82', '0x47739D/0x83'
BUY_0B = '0x4745A4/0x0B'
PORTAL_101_TO_102, PORTAL_102_TO_101 = bytes.fromhex('17000000'), bytes.fromhex('1f000000')

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
            'accounts.json changed during test_pets.py (tests must only use temp copies)'


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _ops(pkts):
    return [p.opcode for p in pkts]


def _char(name, gm=0, gender=None, map_code=101, x=1411.0, y=714.0):
    c = {'name': name, 'level': 1, 'class': 0, 'map': map_code, 'x': x, 'y': y, 'hp': 100, 'mp': 50, 'gm': gm}
    if gender is not None:
        c['gender'] = gender
    return c


def _pet_rec(serial=0x2000, item=PICKY, equipped=None, **pet):
    base = CASH.new_pet(item, bound=True)
    base.update(pet)
    return CASH.make_record(serial, item, CASH.KIND_PET, 1, origin=CASH.ORIGIN_CASH, equipped=equipped, pet=base)


def _content(build):
    EC.configure(DIRS[build], build)


# ================================================================ config ===
class Config15(unittest.TestCase):
    def test_client_item_ids_and_shop_extras(self):
        d = cfgmod.defaults()
        self.assertEqual(d['CLIENT_ITEM_IDS'], 'en')                       # cp-2 installed: the live default
        self.assertEqual(cfgmod.from_dict({'CLIENT_ITEM_IDS': 'kr'})['CLIENT_ITEM_IDS'], 'kr')
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.from_dict({'CLIENT_ITEM_IDS': 'EN'})
        self.assertEqual(d['SHOP_EXTRA_ITEMS'][str(MISTY)], [BELL])
        self.assertEqual(sorted(int(k) for k in d['SHOP_EXTRA_ITEMS']), [8, 48, 50, 86, 107, 138, 152, 172])
        for bad in ({'x': [1]}, {'8': 4285}, {'8': [0]}, {'8': ['4285']}, {'70000': [1]}, {'08': [4285]}):
            with self.subTest(bad=bad), self.assertRaises(cfgmod.ConfigError):
                cfgmod.from_dict({'SHOP_EXTRA_ITEMS': bad})
        self.assertEqual(cfgmod.from_dict({'SHOP_EXTRA_ITEMS': {}})['SHOP_EXTRA_ITEMS'], {})
        self.assertEqual(cfgmod.CLIENT_ITEM_IDS, EC.CLIENT_ITEM_IDS)


# ========================================================= arch09-id-shift ===
class IdShiftAudit(unittest.TestCase):
    def test_exe_constant_map_per_exe(self):
        for en, kr in ((4283, 4279), (4284, 4280), (4285, 4281), (4286, 4282), (4289, 4285), (4322, 4318)):
            self.assertEqual(EC.exe_item_id(en, 'en'), en)
            self.assertEqual(EC.exe_item_id(en, 'kr'), kr)
            self.assertEqual(EC.en_item_from_exe(kr, 'kr'), en)
            self.assertEqual(EC.en_item_from_exe(en, 'en'), en)
        # a row that is no exe constant keeps its EN id under both (the hii is authoritative)
        self.assertEqual((EC.exe_item_id(PICKY, 'kr'), EC.exe_item_id(5, 'kr')), (PICKY, 5))
        self.assertIsNone(EC.en_item_from_exe(4322, 'kr'))                # the stock exe never sends EN 4322
        self.assertEqual(EC.exe_item_id(4286, 'kr'), EC.kr_item_idx(4286, B9))   # the same +4 rule as C3

    def test_guild_board_items_follow_client_item_ids(self):
        self.assertEqual(G.board_items('en'), (4284, 4283))
        self.assertEqual(G.board_items('kr'), (4280, 4279))
        board = {'master_char_id': 1, 'master_name': 'TestHero', 'guild_id': 7, 'guild_name': 'Testers',
                 'emblem_symbol': 18, 'emblem_bg': 18, 'ad_text': 'Join us', 'pos_x': 1.0, 'pos_y': 2.0}
        for ids_, (plain, premium) in (('en', (4284, 4283)), ('kr', (4280, 4279))):
            with self.subTest(ids=ids_):
                for item in (plain, premium):
                    body = P.build(*G.board_add(dict(board, board_item_id=item), ids_), client_build=B9)
                    self.assertEqual(struct.unpack_from('<H', body, 4 + 17)[0], item)
                    listed = P.build(*G.board_list([dict(board, board_item_id=item)], ids_), client_build=B9)
                    self.assertEqual(P.parse('0xBB', listed, direction='S2C', client_build=B9)
                                     ['repeat[board_count]'][0]['board_item_id'], item)
                key, fields = G.sub185(1, plain, ids_)
                self.assertEqual(fields['s185_item_id'], plain)
                other = 4280 if ids_ == 'en' else 4284                   # the other exe's id
                with self.assertRaises(ValueError):
                    G.board_add(dict(board, board_item_id=other), ids_)
                with self.assertRaises(ValueError):
                    G.sub185(1, other, ids_)

    @unittest.skipUnless(HAVE[B9], 'needs the EN 2009 client data')
    def test_server_constants_above_4248_are_the_en_hii_rows(self):
        _content(B9)
        try:
            items = EC.items()
            rows = {4283: (5, 'Premium Guild Billboard'), 4284: (0, 'Guild Billboard'), BELL: (0, 'Pet Bell'),
                    TICKET: (5, 'Pet name making')}
            for item, (type_, name) in rows.items():
                with self.subTest(item=item):
                    self.assertEqual((items.get(item).type, EC.item_name(item)), (type_, name))
            for food in CASH.PET_FOOD:
                d = items.get(food)
                self.assertEqual((d.type, d.cash, EC.item_name(food).startswith('Pet Food')), (5, 1, True))
            self.assertEqual(set(CASH.PET_FOOD) | {CASH.PET_BELL, CASH.PET_NAME_TICKET} | set(G.board_items('en')),
                             set(EC.EXE_ID_ITEMS))
            for name, item in PETS.SPECIES.items():
                d = items.get(item)
                self.assertEqual((d.type, d.kind, d.cash, EC.item_name(item).lower()), (6, 14, 1, name))
                self.assertTrue(CASH.cash_def(item).is_pet)
            gear = items.get(RED_HOOD)
            self.assertEqual((gear.type, gear.kind, gear.cash), (1, 16, 1))
            self.assertEqual(INV.Inventory({'inventory': {}}).slot_for(RED_HOOD), 24)
            # cp-3's default stock: non-cash, sold for its hii price, at real merchants
            d = cfgmod.defaults()
            for npc, extra in d['SHOP_EXTRA_ITEMS'].items():
                self.assertIsNotNone(EC.shop_list(int(npc)), npc)
                self.assertIn(3, EC.shop_list(int(npc)))                    # a potion grocer
                self.assertEqual([(items.get(i).cash, items.get(i).buy) for i in extra], [(0, 500)])
        finally:
            _content(B8)


    def test_no_stray_item_id_above_4248_in_the_server_code(self):
        """The loader rule as a sweep: every item-id literal in 4249..4322 the server CODE holds
        (comments and docstrings dropped) is an EN exe-site id, a pet, the PET_FOOD range end or
        one of the stock exe's KR board ids in guild.BOARD_ITEMS_KR - never a KR-derived row."""
        import glob
        import re
        allowed = set(EC.EXE_ID_ITEMS) | set(PETS.SPECIES.values()) | {max(CASH.PET_FOOD) + 1}
        pat = re.compile(r'(?<![\w.])(0x10[A-Ea-e][0-9A-Fa-f]|4[23]\d\d)(?![\w.])')
        stray = []
        for path in sorted(glob.glob(os.path.join(HERE, '*.py'))):
            name = os.path.basename(path)
            if name.startswith(('test_', 'tool_')) or 'Name clash' in name or 'snapshot' in name:
                continue
            with open(path, encoding='utf-8', errors='replace') as f:
                text = re.sub(r'"""[\s\S]*?"""', '', f.read())
            for line in text.splitlines():
                code = line.split('#', 1)[0]
                for m in pat.finditer(code):
                    value = int(m.group(1), 0)
                    if 4249 <= value <= 4322 and value not in allowed and not (
                            name == 'guild.py' and 'BOARD_ITEMS_KR' in code and value in G.BOARD_ITEMS_KR):
                        stray.append((name, value, code.strip()))
        self.assertEqual(stray, [])


class IdSpaces(unittest.TestCase):
    def test_server_uids_stay_below_the_client_pet_uid_base(self):
        """H6: pet sprites take uids from gs+0x3FC = 33,000,000 up (FUN_00445970)."""
        self.assertEqual(ids.MAP_NPC.lo, 33_000_000)
        for space in ids.SPACES:
            if space.server_allocates and space in (ids.PLAYER, ids.MONSTER):
                self.assertLess(space.hi, ids.MAP_NPC.lo, space.name)
        self.assertLess(W.MOB_UID_BASE + 0xFFFF, ids.MAP_NPC.lo)


# ================================================================= builders ===
def b9(key, fields_assume, receiver_uid=None):
    fields, assume = fields_assume
    return P.build(key, fields, assume, client_build=B9, receiver_uid=receiver_uid)


class Builders(unittest.TestCase):
    def test_forms_per_receiver(self):
        awake = _pet_rec(name='Picky', level=3)
        asleep = _pet_rec(name='Picky', awake=False)
        local = b9('0xAB', PETS.ab_fields(1, PICKY, True, awake))
        self.assertEqual(local, bytes.fromhex('01000000c610'))                 # pet.md 3 example, 6 B (H3)
        remote = b9('0xAB', PETS.ab_fields(1, PICKY, False, awake))
        self.assertEqual(remote, bytes.fromhex('01000000c6100103') + b'Picky' + bytes(8))   # 21 B
        self.assertEqual(len(b9('0xAB', PETS.ab_fields(1, PICKY, False, asleep))), 7)
        self.assertEqual(b9('0xAD', PETS.ad_fields(1, False, True)), bytes.fromhex('0100000000'))     # 5 B
        self.assertEqual(b9('0xAD', PETS.ad_fields(1, True, True)), bytes.fromhex('0100000001'))      # no tail
        self.assertEqual(len(b9('0xAD', PETS.ad_fields(1, True, False, awake))), 19)
        self.assertEqual(len(b9('0xAD', PETS.ad_fields(1, False, False, awake))), 5)
        self.assertEqual(b9('0xAF', PETS.af_fields(1, 5)), bytes.fromhex('0100000005'))
        self.assertEqual(P.build('0xAC', {'uid': 1, 'pet_item_id': PICKY}, client_build=B9), bytes.fromhex('01000000c610'))
        # H4: a long name is cut inside str[13] with its NUL
        long = _pet_rec(name='ABCDEFGHIJKLMNOP')
        self.assertEqual(long['pet']['name'], 'ABCDEFGHIJKL')                 # stored: 12 bytes
        body = b9('0xAB', PETS.ab_fields(1, PICKY, False, long))
        self.assertEqual((len(body), body[8:21]), (21, b'ABCDEFGHIJKL\x00'))
        # no guessing: the receiver gate must be stated
        with self.assertRaises(P.MissingAssume):
            P.build('0xAB', {'uid': 1, 'pet_item_id': PICKY, 'has_pet_info': 0}, client_build=B9)
        with self.assertRaises(P.MissingAssume):
            P.build('0xAF', {'uid': 1, 'pet_level': 2}, client_build=B9)

    def test_record_pet_block(self):
        char = _char('TestHero')
        self.assertEqual(R.pet_block(char), {'has_pet': 0})
        char['cash_items'] = [_pet_rec(equipped=False)]
        self.assertEqual(R.pet_block(char), {'has_pet': 0})                    # bagged: no block
        char['cash_items'] = [_pet_rec(equipped=True, awake=False)]
        self.assertEqual(R.pet_block(char), {'has_pet': 0})                    # asleep: no pet_info (F6)
        char['cash_items'] = [_pet_rec(equipped=True, level=4, name='Tweety')]
        self.assertEqual(R.pet_block(char), {'has_pet': 1, 'pet_level': 4, 'pet_name': 'Tweety'})
        rec = R.player_record({'uid': 1, 'hp': 50, 'mp': 20}, char, {'uid': 1}, client_build=B9, remote=True)
        row = R.to_0x05(rec, B9)
        body = P.build('0x05', row, client_build=B9, receiver_uid=2)
        back = P.parse('0x05', body, direction='S2C', client_build=B9)
        self.assertEqual((back['has_pet'], back['pet_level'], back['pet_name']), (1, 4, 'Tweety'))
        self.assertEqual(body[-15:], bytes((1, 4)) + b'Tweety' + bytes(7))
        # H3: the owner's own 0x07 has the fields but not the bytes
        own = P.build('0x07', R.player_list(rec), client_build=B9, receiver_uid=1)
        bare = dict(rec, has_pet=0)
        bare.pop('pet_level'), bare.pop('pet_name')
        self.assertEqual(own, P.build('0x07', R.player_list(bare), client_build=B9, receiver_uid=1))
        remote = P.build('0x04', R.player_list(rec), client_build=B9, receiver_uid=2)
        self.assertEqual(len(remote), len(own) + 15)


# =================================================================== worlds ===
class _World:
    """TestHero (GM, uid 1) and Watcher (uid 2) in world on map 101 of one server of `build`."""
    build = B9
    config = {}

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        self.tmp = tempfile.mkdtemp(prefix=f'ws_pets_{self.build}_')
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False,
                                **self.config})
        accounts = {'test': {'password': 'test', 'gender': 1,
                             'characters': [_char('TestHero', gm=1, gender=1)]},
                    'admin': {'password': 'admin', 'gender': 0, 'characters': [_char('Watcher', gender=0)]}}
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

    def line(self, c, text):
        c.send_c2s(CHAT[self.build], {'msg_len': len(text), 'message': text})

    def gm(self, text, quiet=0.3):
        """A GM line of A; (A's packets, B's packets)."""
        self.line(self.a, text.encode())
        return self.a.recv_until_quiet(quiet), self.b.recv_until_quiet(0.15)

    def owned(self, char=None):
        return {r['serial']: r for r in (char or self.hero)['cash_items']}

    def pet(self, char=None):
        return CASH.equipped_pet(char or self.hero)


class _PetWorld2009(_World):
    def give(self, item=PICKY, wear=False):
        a_pkts, b_pkts = self.gm(f'!pet give {item}{" wear" if wear else ""}')
        return a_pkts, b_pkts

    def wear(self, item=PICKY):
        self.a.send_c2s(EQUIP_82, {'pet_item_id': item})
        return self.a.recv_until_quiet(0.3), self.b.recv_until_quiet(0.15)

    def take_off(self, item=PICKY):
        self.a.send_c2s(UNEQUIP_83, {'pet_item_id': item})
        return self.a.recv_until_quiet(0.3), self.b.recv_until_quiet(0.15)

    def seen(self, c=None, uid=1):
        return cview.has_pet_info((c or self.b).session, uid)

    def portal(self, c, payload, dest):
        c.send(0x7E, payload)
        self.assertTrue(_wait(lambda: self.server.world.map_of(c.session) == dest))
        return c.recv_until_quiet(0.3)

    def rows_of(self, c, pkts, uid=1):
        out = []
        for p in pkts:
            if p.opcode == 0x04:
                out += [r for r in c.s2c(p)['repeat[player_count]'] if r['uid'] == uid]
            elif p.opcode == 0x05:
                r = c.s2c(p)
                if r['uid'] == uid:
                    out.append(r)
        return out


class PetWorld2009(_PetWorld2009, unittest.TestCase):
    def test_t1_give_and_wear_then_the_viewer_sees_it(self):
        a, b = self.a, self.b
        pkts, _ = self.give()
        six_f = [p for p in pkts if p.opcode == 0x6F]
        self.assertEqual(len(six_f), 1)
        rows = a.s2c(six_f[0])['repeat[count]']
        bagged = next(r for r in rows if r['item_id'] == PICKY)
        self.assertEqual((bagged['is_equipped'], bagged['limit_type'], bagged['pet_level'], bagged['pet_summoned']),
                         (0, 3, 1, 1))
        serial = bagged['serial']
        self.assertIsNone(self.pet())
        # C2S 0x82 -> 0xAB: the 6-byte local form to A, the 21-byte remote form to B
        a_pkts, b_pkts = self.wear()
        self.assertEqual(_ops(a_pkts), [0xAB])
        self.assertEqual(a_pkts[0].payload, struct.pack('<IH', 1, PICKY))
        self.assertEqual(_ops(b_pkts), [0xAB])
        got = b.s2c(b_pkts[0], assume={PETS.AB_GATE: False, PETS.LOCAL: False})
        self.assertEqual((got['uid'], got['pet_item_id'], got['has_pet_info'], got['pet_level'], got['pet_name']),
                         (1, PICKY, 1, 1, 'Picky'))
        self.assertEqual(len(b_pkts[0].payload), 21)
        self.assertTrue(self.seen())
        worn = self.pet()
        self.assertEqual((worn['serial'], a.session['pet_bound']), (serial, serial))
        self.assertEqual(self.hero['look_ext'][0], PETS.pet_spr(PICKY))      # appearance word 14
        self.assertEqual(R.pet_slot_2009(self.hero), PICKY)
        # a second 0x82 for the same item: nothing bagged any more - no reply at all
        a_pkts, b_pkts = self.wear()
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([], []))

    def test_give_wear_in_one_gm_line(self):
        pkts, b_pkts = self.give(wear=True)
        ops = _ops(pkts)
        self.assertLess(ops.index(0x6F), ops.index(0xAB))                    # H7: the record first
        self.assertIn(0xAB, _ops(b_pkts))
        self.assertTrue(self.seen())

    def test_t2_t17_t18_arrival_portal_relog_logout(self):
        a, b = self.a, self.b
        self.give(wear=True)
        # T2: B leaves and comes back: A's record carries the pet block (its pet_info)
        self.portal(b, PORTAL_101_TO_102, 102)
        self.assertFalse(self.seen())                                       # B's 0x03 cleared it
        a.recv_until_quiet(0.2)
        pkts = self.portal(b, PORTAL_102_TO_101, 101)
        rows = self.rows_of(b, pkts)
        self.assertEqual([(r['has_pet'], r['pet_level'], r['pet_name'], r['repeat[10]'][0]['cash_equip_item_id'])
                          for r in rows], [(1, 1, 'Picky', PICKY)])
        self.assertTrue(self.seen())
        self.assertEqual([r['appearance_part'] for r in rows[0]['repeat[17]']][14], PETS.pet_spr(PICKY))
        a.recv_until_quiet(0.2)
        # T17: A portals: B gets the 0x06 (its pet_info freed); A's own load brings the pet back
        pkts = self.portal(a, PORTAL_101_TO_102, 102)
        self.assertEqual(_ops(b.recv_until_quiet(0.2)), [0x06])
        self.assertFalse(self.seen())
        own = next(p for p in pkts if p.opcode == 0x07)
        mine = a.s2c(own)['repeat[player_count]'][0]
        self.assertEqual(mine['repeat[10]'][0]['cash_equip_item_id'], PICKY)
        self.assertNotIn('has_pet', a.s2c(own)['repeat[player_count]'][0])  # H3: no bytes on the own row
        six_f = [a.s2c(p) for p in pkts if p.opcode == 0x6F]
        self.assertEqual((six_f[0]['repeat[count]'][0]['item_id'], six_f[0]['repeat[count]'][0]['is_equipped']),
                         (PICKY, 1))                                         # C2 / H5: first, equipped
        self.assertLess(_ops(pkts).index(0x07), _ops(pkts).index(0x6F))
        pkts = self.portal(a, PORTAL_102_TO_101, 101)
        rows = self.rows_of(b, b.recv_until_quiet(0.3))
        self.assertEqual([r['has_pet'] for r in rows], [1])
        self.assertTrue(self.seen())
        # T18: A logs out: B loses A (0x06) and its pet_info
        self.mc.clients.remove(a)
        a.close()
        self.assertTrue(_wait(lambda: not self.seen()))
        self.assertIn(0x06, _ops(b.recv_until_quiet(0.3)))

    def test_unequip_reaches_only_holders_of_the_pet_info(self):
        a, b = self.a, self.b
        self.give(wear=True)
        self.assertTrue(self.seen())
        a_pkts, b_pkts = self.take_off()
        self.assertEqual(_ops(a_pkts), [0xAC])
        self.assertEqual(a_pkts[0].payload, struct.pack('<IH', 1, PICKY))
        self.assertEqual(_ops(b_pkts), [0xAC])
        self.assertFalse(self.seen())                                       # F2 step 4
        self.assertIsNone(self.pet())
        self.assertIsNone(a.session['pet_bound'])
        self.assertEqual(self.hero['look_ext'][0], 0)
        # H5: the next 0x6F lists it bagged, and no "lacks the worn pet" error
        with self.assertLogs('WS', level='INFO') as cm:
            self.server._send_owned_cash(a.session['sock'], a.session, reason='test', force=True)
        self.assertFalse(any(r.levelno >= logging.ERROR for r in cm.records), cm.output)
        rows = a.s2c(next(p for p in a.recv_until_quiet(0.2) if p.opcode == 0x6F))['repeat[count]']
        self.assertEqual([(r['item_id'], r['is_equipped']) for r in rows], [(PICKY, 0)])
        # H1: wear it asleep - B never gets its pet_info, so B gets no 0xAC for it
        with self.server.store.lock:
            next(r for r in self.hero['cash_items'] if r['item_id'] == PICKY)['pet']['awake'] = False
        a_pkts, b_pkts = self.wear()
        self.assertEqual(_ops(b_pkts), [0xAB])
        self.assertEqual(len(b_pkts[0].payload), 7)                          # has_pet_info 0
        self.assertFalse(self.seen())
        a_pkts, b_pkts = self.take_off()
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([0xAC], []))

    def test_t12_the_gear_gate_and_refusals(self):
        a, b = self.a, self.b
        self.give(wear=True)
        with self.server.store.lock:
            self.hero['equipped'][24] = {'id': RED_HOOD, 'w': [0] * 6}
        a_pkts, b_pkts = self.take_off()
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([0x15], []))
        self.assertIn(PETS.GEAR_ON_TEXT.encode(), a_pkts[0].payload)
        self.assertIsNotNone(self.pet())
        self.give(ULIE)
        a_pkts, b_pkts = self.wear(ULIE)                                    # equip over: the same gate
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([0x15], []))
        with self.server.store.lock:
            del self.hero['equipped'][24]
        # forged / stale requests: no reply at all (the client takes no lock)
        for key, item in ((UNEQUIP_83, ULIE), (EQUIP_82, 4285), (EQUIP_82, 1234)):
            with self.subTest(key=key, item=item):
                a.send_c2s(key, {'pet_item_id': item})
                self.assertEqual(_ops(a.recv_until_quiet(0.2)), [])
        self.assertEqual(self.pet()['item_id'], PICKY)

    def test_swap_takes_the_old_pet_off_first(self):
        a, b = self.a, self.b
        self.give(wear=True)
        old = self.pet()['serial']
        self.give(ULIE)
        b.recv_until_quiet(0.1)
        a_pkts, b_pkts = self.wear(ULIE)
        self.assertEqual(_ops(a_pkts), [0xAC, 0xAB])
        self.assertEqual(a_pkts[0].payload, struct.pack('<IH', 1, PICKY))
        self.assertEqual(a_pkts[1].payload, struct.pack('<IH', 1, ULIE))
        self.assertEqual(_ops(b_pkts), [0xAC, 0xAB])
        self.assertEqual(len(b_pkts[1].payload), 21)
        self.assertTrue(self.seen())
        self.assertEqual((self.pet()['item_id'], self.owned()[old]['equipped']), (ULIE, False))
        self.assertEqual(self.hero['look_ext'][0], PETS.pet_spr(ULIE))
        # a full equipment tab refuses the swap (the 0xAC needs a slot for the old pet)
        with self.server.store.lock:
            INV.Inventory(self.hero).set_capacity('equip', INV.Inventory(self.hero).used_slots('equip'))
        a_pkts, b_pkts = self.wear(PICKY)
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([0x15], []))
        self.assertEqual(self.pet()['item_id'], ULIE)

    def test_sleep_and_wake_follow_h1(self):
        a, b = self.a, self.b
        self.give(wear=True)
        a_pkts, b_pkts = self.gm('!pet set awake 0')
        self.assertEqual(a_pkts[0].opcode, 0xAD)
        self.assertEqual(a_pkts[0].payload, bytes.fromhex('0100000000'))     # local: 5 B
        self.assertIn(0x6F, _ops(a_pkts))                                    # the owner's record re-bound
        self.assertEqual(_ops(b_pkts), [0xAD])
        self.assertEqual(b_pkts[0].payload, bytes.fromhex('0100000000'))
        self.assertTrue(self.seen())                                         # the block survives asleep
        self.assertEqual((self.pet()['pet']['awake'], self.pet()['pet']['gauge']), (False, 0))
        # B reloads: A's record has has_pet 0 now -> no pet_info -> a sleep is never sent to B
        self.portal(b, PORTAL_101_TO_102, 102)
        a.recv_until_quiet(0.2)
        pkts = self.portal(b, PORTAL_102_TO_101, 101)
        self.assertEqual([r['has_pet'] for r in self.rows_of(b, pkts)], [0])
        self.assertFalse(self.seen())
        a.recv_until_quiet(0.2)
        a_pkts, b_pkts = self.gm('!pet set awake 0')
        self.assertEqual(_ops(b_pkts), [])                                   # H1
        # wake: the 19-byte remote form allocates B's pet_info
        a_pkts, b_pkts = self.gm('!pet set awake 1')
        self.assertEqual(_ops(b_pkts), [0xAD])
        got = b.s2c(b_pkts[0], assume={PETS.AD_GATE: False, PETS.LOCAL: False})
        self.assertEqual((got['awake'], got['pet_level'], got['pet_name'], len(b_pkts[0].payload)), (1, 1, 'Picky', 19))
        self.assertTrue(self.seen())
        a_pkts, b_pkts = self.gm('!pet set awake 0')
        self.assertEqual(_ops(b_pkts), [0xAD])

    def test_h2_no_rename_packets_and_the_level(self):
        a, b = self.a, self.b
        self.give(wear=True)
        self.gm('!pet set awake 0')
        a_pkts, b_pkts = self.gm('!pet set name Tweety')
        self.assertFalse({0xB0, 0xC0} & set(_ops(a_pkts) + _ops(b_pkts)))  # H2
        rows = a.s2c(next(p for p in a_pkts if p.opcode == 0x6F))['repeat[count]']
        self.assertEqual(P.to_bytes(rows[0]['pet_name']).split(b'\x00')[0], b'Tweety')
        self.gm('!pet set awake 1')
        a_pkts, b_pkts = self.gm('!pet set level 5')
        self.assertEqual(a_pkts[0].payload, bytes.fromhex('0100000005'))     # 0xAF to the owner
        self.assertEqual([(p.opcode, p.payload) for p in b_pkts], [(0xAF, bytes.fromhex('0100000005'))])
        self.assertEqual((self.pet()['pet']['level'], self.pet()['pet']['exp']), (5, CASH.PET_EXP_TABLE[4]))
        # a viewer without the pet_info gets no 0xAF
        with cview.lock(b.session):
            cview.forget_pet_info(b.session, 1)
        a_pkts, b_pkts = self.gm('!pet set level 6')
        self.assertEqual(_ops(b_pkts), [])
        # the record a new viewer gets carries the new name / level
        rec = R.pet_block(self.hero)
        self.assertEqual(rec, {'has_pet': 1, 'pet_level': 6, 'pet_name': 'Tweety'})

    def test_in_the_mall_or_a_2008_route_nothing(self):
        self.give()
        self.a.session['in_cash_shop'] = True
        try:
            a_pkts, b_pkts = self.wear()
        finally:
            self.a.session['in_cash_shop'] = False
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([], []))
        self.assertIsNone(self.pet())
        self.assertEqual(self.server.routes[0x82].handler, '_handle_pet_equip')
        self.assertEqual(self.server.routes[0x83].handler, '_handle_pet_unequip')

    def test_gm_lines(self):
        a_pkts, _ = self.gm('!pet')
        self.assertTrue(any(b'no pets' in p.payload for p in a_pkts if p.opcode == 0x15))
        self.give(wear=True)
        a_pkts, _ = self.gm('!pet seen')
        self.assertTrue(any(b'Watcher: holds pet_info' in p.payload for p in a_pkts if p.opcode == 0x15))
        a_pkts, b_pkts = self.gm('!pet off')
        self.assertIn(0xAC, _ops(a_pkts))
        self.assertIn(0xAC, _ops(b_pkts))
        a_pkts, b_pkts = self.gm('!pet wear')
        self.assertIn(0xAB, _ops(a_pkts))
        a_pkts, _ = self.gm('!pet set exp 400')
        self.assertEqual((self.pet()['pet']['exp'], self.pet()['pet']['level']), (400, 3))
        a_pkts, _ = self.gm('!pet give 1234')
        self.assertTrue(any(b'no pet item' in p.payload for p in a_pkts if p.opcode == 0x15))


class LookAtLogin2009(_PetWorld2009, unittest.TestCase):
    """A worn pet whose stored word 14 is stale: the login's look migration fixes it, so the
    0x02 / 0x07 / remote rows draw the pet body layer (pets.sync_look)."""

    def prepare(self):
        with self.server.store.lock:
            CASH.ensure(self.hero).append(_pet_rec(0x2000, equipped=True))
            self.hero['look_ext'] = [0, 0, 0]
            self.watcher['look_ext'] = [7, 0, 0]                            # no pet worn: 0

    def test_word_14_is_the_worn_pets_spr(self):
        self.assertEqual(self.hero['look_ext'][0], PETS.pet_spr(PICKY))
        self.assertEqual(self.watcher['look_ext'][0], 0)
        own = next(p for p in self.a.entry if p.opcode == 0x07)
        row = self.a.s2c(own)['repeat[player_count]'][0]
        self.assertEqual(row['repeat[17]'][14]['appearance_part'], PETS.pet_spr(PICKY))
        self.assertEqual(self.a.session['pet_bound'], 0x2000)
        self.assertTrue(self.seen())                                        # B's 0x04 had the block


# ================================================================== cp-3 ===
class ShopExtras2009(_PetWorld2009, unittest.TestCase):
    def buy(self, item, npc):
        self.a.send_c2s(BUY_0B, {'item_id': item, 'qty': 1, 'npc_id': npc})
        return self.a.recv_until_quiet(0.3)

    def test_the_pet_bell_sells_at_a_potion_grocer(self):
        """Either hni: the stock one has no bell (SHOP_EXTRA_ITEMS adds it), the cp-3d one ends
        Misty's row with it (the extra is then skipped). The stock the buy checks lists it once."""
        self.gm('!gold 1000')
        in_hni = BELL in EC.shop_list(MISTY)
        self.assertEqual(self.server._shop_extras(MISTY), [] if in_hni else [BELL])
        self.assertEqual((EC.shop_list(MISTY) + self.server._shop_extras(MISTY)).count(BELL), 1)
        pkts = self.buy(BELL, MISTY)
        got = self.a.s2c(pkts[0])
        self.assertEqual((pkts[0].opcode, got['item_id'], got['count'], got['gold']), (0x18, BELL, 1, 500))
        self.assertEqual(INV.Inventory(self.hero).count(BELL), 1)
        for item, npc in ((BELL, MURDOCK), (FOOD20, MISTY)):              # not stocked there / a cash item
            with self.subTest(item=item, npc=npc):
                pkts = self.buy(item, npc)
                self.assertEqual(_ops(pkts), [0x18, 0x15])
                self.assertEqual(self.a.s2c(pkts[0])['item_id'], 0)
        self.assertEqual(INV.Inventory(self.hero).count(BELL), 1)

    def test_an_hni_row_that_lists_the_bell_gets_no_extra(self):
        """cp-3d (CLIENT_PATCH_SET_RE_2026-10-06.md 9): the patched hni appends 4285 to the
        grocer rows, and the server reads that same hni - _shop_extras skips an id the row
        already lists, so the bell is never on the list twice. Both row states, forced here, so
        the test does not depend on which hni is installed."""
        misty = EC.npcs().get(MISTY)
        stock = [i for i in misty.shop_items if i != BELL]               # the Build 14 row
        self.addCleanup(setattr, misty, 'shop_items', list(misty.shop_items))
        for row, extras in ((stock, [BELL]), (stock + [BELL], [])):
            with self.subTest(row=row[-1]):
                misty.shop_items = list(row)
                self.assertEqual(self.server._shop_extras(MISTY), extras)
                self.assertEqual((EC.shop_list(MISTY) + self.server._shop_extras(MISTY)).count(BELL), 1)
        self.gm('!gold 1000')
        pkts = self.buy(BELL, MISTY)                                        # the cp-3d row: sold once
        got = self.a.s2c(pkts[0])
        self.assertEqual((_ops(pkts), got['item_id'], got['count'], got['gold']), ([0x18], BELL, 1, 500))

    def test_gm_bell_and_food(self):
        a_pkts, _ = self.gm('!pet bell 3')
        self.assertIn(0x18, _ops(a_pkts))
        self.assertEqual(INV.Inventory(self.hero).count(BELL), 3)
        a_pkts, _ = self.gm('!pet food 2')
        self.assertIn(0x6F, _ops(a_pkts))
        food = [r for r in self.hero['cash_items'] if r['item_id'] == FOOD20]
        self.assertEqual([(r['kind'], r['qty']) for r in food], [(CASH.KIND_COUNT, 2)])


class ShopExtrasEmpty2009(_PetWorld2009, unittest.TestCase):
    config = {'SHOP_EXTRA_ITEMS': {}}

    def buy(self, item, npc):
        self.a.send_c2s(BUY_0B, {'item_id': item, 'qty': 1, 'npc_id': npc})
        return self.a.recv_until_quiet(0.3)

    def test_no_extras_no_bell(self):
        """SHOP_EXTRA_ITEMS {}: the bell sells only where the loaded hni row itself lists it -
        nowhere on the stock hni, at Misty (and the other seven grocers) once cp-3d is installed.
        A merchant whose row has no bell (Murdock) refuses it on either hni."""
        self.gm('!gold 1000')
        self.assertEqual(self.server._shop_extras(MISTY), [])
        pkts = self.buy(BELL, MISTY)
        if BELL in EC.shop_list(MISTY):                                     # cp-3d hni
            got = self.a.s2c(pkts[0])
            self.assertEqual((_ops(pkts), got['item_id'], got['count'], got['gold']), ([0x18], BELL, 1, 500))
        else:                                                               # stock hni
            self.assertEqual((_ops(pkts), self.a.s2c(pkts[0])['item_id']), ([0x18, 0x15], 0))
        self.assertNotIn(BELL, EC.shop_list(MURDOCK))
        pkts = self.buy(BELL, MURDOCK)
        self.assertEqual((_ops(pkts), self.a.s2c(pkts[0])['item_id']), ([0x18, 0x15], 0))
        self.assertEqual(INV.Inventory(self.hero).count(BELL), 1 if BELL in EC.shop_list(MISTY) else 0)


# =================================================================== C6 ===
class Feed0x48En2009(_PetWorld2009, unittest.TestCase):
    """CLIENT_ITEM_IDS 'en' (cp-2): pet food reaching C2S 0x48 means a stock exe - logged once;
    the use still feeds, and a sleeping pet wakes on both clients (0xAD)."""

    def test_hint_and_wake(self):
        a, b = self.a, self.b
        self.give(wear=True)
        self.gm('!pet set awake 0')
        food = self.server.cash.grant(self.hero, FOOD20)
        with self.assertLogs('WS', level='WARNING') as cm:
            a.send_c2s(P.variants(0x48, 'C2S', client_build=B9)[0]['key'], {'item_id': FOOD20})
            a_pkts = a.recv_until_quiet(0.3)
        self.assertTrue(any("CLIENT_ITEM_IDS 'en'" in line for line in cm.output), cm.output)
        # P15 pet-s4: the 0x72 consumed it; 0xB1 {gauge 90, serial 0} redraws, then the wake
        self.assertEqual(_ops(a_pkts)[:3], [0x72, 0xB1, 0xAD])
        self.assertEqual(a_pkts[1].payload, struct.pack('<BI', PETS.FOOD_GAUGE, 0))
        self.assertEqual(a_pkts[2].payload, bytes.fromhex('0100000001'))
        b_pkts = b.recv_until_quiet(0.2)
        self.assertIn(0xAD, _ops(b_pkts))
        self.assertEqual((self.pet()['pet']['awake'], self.pet()['pet']['gauge']), (True, PETS.FOOD_GAUGE))
        self.assertEqual(self.owned()[food['serial']]['qty'], 19)


class Feed0x48Kr2009(_PetWorld2009, unittest.TestCase):
    config = {'CLIENT_ITEM_IDS': 'kr'}

    def test_no_hint_on_the_stock_exe(self):
        self.give(wear=True)
        self.server.cash.grant(self.hero, FOOD20)
        self.assertEqual(self.server.pets.client_item_ids, 'kr')
        with self.assertLogs('WS', level='INFO') as cm:
            self.a.send_c2s(P.variants(0x48, 'C2S', client_build=B9)[0]['key'], {'item_id': FOOD20})
            self.a.recv_until_quiet(0.3)
        self.assertFalse(any(r.levelno >= logging.WARNING for r in cm.records), cm.output)


# ================================================================= 2008 ===
class PetWorld2008(_World, unittest.TestCase):
    build = B8

    def prepare(self):
        with self.server.store.lock:
            CASH.ensure(self.hero).append(_pet_rec(0x2000, equipped=True))     # store data from a 2009 server

    def test_no_pets_on_the_2008_client(self):
        a, b = self.a, self.b
        a_pkts, b_pkts = self.gm('!pet')
        self.assertTrue(any(b'2008 client has no pets' in p.payload for p in a_pkts if p.opcode == 0x15))
        self.assertEqual(self.server._shop_extras(MISTY), [])               # 4285 is no 2008 item
        rows = [r for p in b.entry if p.opcode == 0x04 for r in b.s2c(p)['repeat[player_count]']]
        self.assertTrue(rows and all('has_pet' not in r for r in rows))
        route = self.server.routes.get(0x82)
        self.assertNotEqual(getattr(route, 'handler', None), '_handle_pet_equip')
        self.assertIsNone(self.server.pets.equip(a.session, PICKY))
        self.assertIsNone(self.server.pets.set_awake(a.session, False))
        self.assertFalse({0xAB, 0xAC, 0xAD, 0xAF} & set(_ops(a.recv_until_quiet(0.2) + b.recv_until_quiet(0.1))))


if __name__ == '__main__':
    unittest.main()
