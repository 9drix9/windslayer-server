#!/usr/bin/env python3
"""
test_appearance.py - the composed look, both client builds
==========================================================
User report (2026-09-25, EN 2009): "the right clothing/equipment/weapon is not being shown on
my character" - TestHero wore the Blue Novice Hat, the Light Leather Vest (F) / Leggings (F) /
Gloves / Sandal and the Wooden Sword, and every enter-world, portal and relog drew her in
the creation outfit with only a weapon in the hand.

What the client does (2009 FUN_004282c0 = 2008 FUN_00426d50, read from both exes):
- S2C 0x02 / 0x07 / 0x04 / 0x05 copy the appearance words VERBATIM into the entity; the
  client never builds a look from the equipped item ids.
- S2C 0x1D (equip) and 0x1E / 0x24 (unequip) recompose ONE layer, after the grid change:
  the item's Spr_Num (0 on an unequip), a costume beating the regular item, the Clear Hat
  showing the hair, a class-mismatched weapon skin showing the real weapon, the underwear
  (1 / 101 by gender) under an emptied shirt / pant layer, the hair under a bare head.
- 2008 composes Kinds 0..13 only, 2009 0..16 (the pet layers 14..16 = store `look_ext`).

So the server must hold the composed words (inventory.compose applied at the same events,
GameServer._compose_looks migrating a look stored before that at login) and send them as they
are. Pinned here: the routine's branches, TestHero's exact words on the wire (0x02, 0x07,
0x04 / 0x05) for 2008 and 2009, the unequip defaults for both genders, replay idempotence
(the migration), and the whole flow over fake clients - an old record migrated at login, and
equip -> observer 0x1D -> unequip -> relog -> the select screen, the own 0x07 and the
observer's respawn record all carrying the composed look.

No port is bound, no game client runs and the live accounts.json is never opened (temp
copies; the module checks its hash at the end).
"""
import copy
import hashlib
import json
import logging
import os
import random
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import fakeclient as F  # noqa: E402
import packets as P  # noqa: E402
import progression  # noqa: E402

W = F.import_server()
cfgmod = W.cfgmod
EC = W.EC
R = W.R
INV = W.invmod
storemod = W.storemod
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = cfgmod.BUILD_2008, cfgmod.BUILD_2009
DIRS = {B8: cfgmod.DEFAULTS['CLIENT_DIR'], B9: cfgmod.DEFAULTS['CLIENT_DIR_2009']}
HAVE = {b: os.path.exists(os.path.join(HERE, d, 'hs', 'windslayer.hii')) for b, d in DIRS.items()}

# TestHero's worn set (hii, identical in both builds): id -> (Kind, Spr_Num, grid slot)
HAT, VEST, SWORD, LEGGINGS, GLOVES, SANDAL = 2068, 64, 9, 65, 106, 68
WORN = {HAT: (10, 360, 0), VEST: (6, 7, 4), SWORD: (11, 5, 5), LEGGINGS: (5, 7, 8),
        GLOVES: (8, 7, 9), SANDAL: (9, 7, 10)}
STICK = 179              # Wooden Stick, Kind 11, Spr 1
CASH_SHIRT = 3126        # cash Kind 6, Spr 20 -> costume slot 17
CLEAR_HAT = 3420         # cash Kind 10: the helm layer shows the hair
CASH_BLADE = 3178        # cash Kind 11, Spr 901, Job [0, 0, 1, ...] (a class-2 skin)
CLASS1_BLADE = 70        # Kind 11, Spr 2, Job [0, 1, ...] (a class-1 weapon)
BELT, EARRING, NECKLACE, RING = 1443, 1491, 1395, 1343      # Kinds 14 / 15 / 16 / 17
PET_HAT = 4292           # 2009 only: cash Kind 15, Spr 101 -> slot 23, layer 15 (look_ext[1])

CREATION_F = [0, 1, 0, 0, 1, 2, 2, 0, 0, 2, 1, 0, 0, 1]     # store.default_look(0)
# spec section 5.1: the words every record must carry for TestHero (female, gender bool 0)
TESTHERO_LOOK = [0, 1, 0, 0, 1, 7, 7, 0, 7, 7, 360, 5, 0, 1]
TESTHERO_EQUIP = [2068, 0, 0, 0, 64, 9, 0, 0, 65, 106, 68, 0, 0, 0, 0]      # grid slots 0..14

_LIVE_HASH = None


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def setUpModule():
    global _LIVE_HASH
    _LIVE_HASH = _sha(LIVE_ACCOUNTS) if os.path.exists(LIVE_ACCOUNTS) else None


def tearDownModule():
    EC.configure(DIRS[B8], B8)              # the 2009 classes switch en_content to 2009
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_appearance.py (tests must only use temp copies)'


def words_of(build):
    return 17 if build == B9 else 14


def testhero_record(build, **over):
    """TestHero as the live store held her on 2026-09-25: the creation look under the six
    worn items (the server never composed it) and `weapon` 5 from the old slot-11 merge."""
    char = {'name': 'TestHero', 'class': 1, 'job2': 0, 'exp': progression.exp_for_level(7),
            'look': list(CREATION_F), 'weapon': 5, 'str': 3, 'dex': 2, 'int': 1, 'spr': 3,
            'map': 101, 'x': 1411.0, 'y': 714.0, 'hp': 100, 'mp': 50,
            'equipped': {slot: {'id': item, 'w': [0] * 6} for item, (_k, _s, slot) in WORN.items()}}
    if build == B9:
        char.update(gender=0, look_ext=[0, 0, 0])
    char.update(over)
    INV.ensure(char)
    return char


def look_of(row):
    """The appearance words of a decoded 0x02 / 0x07 / 0x04 / 0x05 row."""
    for key, value in row.items():
        if key.startswith('repeat[') and value and isinstance(value[0], dict):
            first = value[0]
            if 'appearance_part' in first or 'appearance' in first:
                return [e.get('appearance_part', e.get('appearance')) for e in value]
    raise AssertionError('no appearance array in the row')


def grid_of(row):
    """The regular equip ids of a decoded 0x07 / 0x04 / 0x05 row."""
    for key, value in row.items():
        if key.startswith('repeat[') and value and isinstance(value[0], dict) and 'equip_item_id' in value[0]:
            return [e['equip_item_id'] for e in value]
    raise AssertionError('no equip grid in the row')


def cash_of(row):
    """The cash-half ids of a decoded 0x07 / 0x04 / 0x05 row (2008 9, 2009 10 rows)."""
    for key, value in row.items():
        if key.startswith('repeat[') and value and isinstance(value[0], dict):
            first = value[0]
            for name in ('cash_equip_item_id', 'extra_equip_item_id'):
                if name in first:
                    return [e[name] for e in value]
    raise AssertionError('no cash equip array in the row')


# ================================================================ the routine ===
class _Compose:
    """inventory.compose / Inventory.wear / take_off / compose_all on one build's hii."""
    build = B8

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        EC.configure(DIRS[self.build], self.build)
        self.catalog = EC.items()
        self.assertEqual(self.catalog.client_build, self.build)

    def fresh(self, gender=0, look=None):
        char = {'name': 'Dummy', 'look': list(look or storemod.default_look(gender))}
        if self.build == B9:
            char.update(gender=gender, look_ext=[0, 0, 0])
        INV.ensure(char)
        bag = INV.Inventory(char)
        bag.set_capacity('equip', INV.MAX_CAPACITY)
        return char, bag

    def wear(self, bag, item, gender=0):
        self.assertIsNotNone(bag.add(item), item)
        slot, _ = bag.wear(item, gender=gender)
        self.assertIsNotNone(slot, f'{item} has a {self.build} grid slot')
        return slot

    def words(self, char):
        return INV.Inventory(char).look_words()

    # ------------------------------------------------------------ TestHero ---
    def test_testhero_is_composed_from_the_worn_items_once(self):
        char = testhero_record(self.build)
        bag = INV.Inventory(char)
        self.assertEqual(bag.grid_ids()[:15], TESTHERO_EQUIP)
        self.assertTrue(bag.compose_all(0))
        self.assertEqual(char['look'], TESTHERO_LOOK)
        self.assertEqual(char.get('look_ext', [0, 0, 0]), [0, 0, 0])
        self.assertEqual(char['weapon'], 5)                     # the derived copy of word 11
        self.assertFalse(bag.compose_all(0))                    # idempotent: nothing to write
        self.assertEqual(char['look'], TESTHERO_LOOK)

    def test_testhero_words_equal_the_client_equipping_them_one_by_one(self):
        """The migration (replay) lands where the client's own 0x1D sequence lands."""
        char, bag = self.fresh()
        for item in (HAT, VEST, SWORD, LEGGINGS, GLOVES, SANDAL):
            self.wear(bag, item)
        self.assertEqual(char['look'], TESTHERO_LOOK)
        for item, (kind, spr, slot) in WORN.items():
            self.assertEqual(char['equipped'][slot]['id'], item)
            self.assertEqual(char['look'][kind], spr)

    # ----------------------------------------------------- unequip defaults ---
    def test_each_unequip_leaves_what_the_client_shows(self):
        """spec 5.3: vest / leggings -> the underwear (1 female, 101 male), the hat -> the
        hair, sword / gloves / sandal -> nothing; wearing it again puts its Spr_Num back.
        The creation clothes (2 / 102) never come back: they are not items."""
        for gender, underwear in ((0, 1), (1, 101)):
            char, bag = self.fresh(gender)
            for item in WORN:
                self.wear(bag, item, gender)
            hair = char['look'][INV.LAYER_HAIR]
            expect = {VEST: (6, underwear), LEGGINGS: (5, underwear), HAT: (10, hair),
                      SWORD: (11, 0), GLOVES: (8, 0), SANDAL: (9, 0)}
            for item, (layer, value) in expect.items():
                with self.subTest(gender=gender, item=item):
                    self.assertEqual(bag.take_off(item, gender=gender)[1]['id'], item)
                    self.assertEqual(char['look'][layer], value)
                    self.wear(bag, item, gender)
                    self.assertEqual(char['look'][layer], WORN[item][1])
            self.assertNotIn(2, (char['look'][5], char['look'][6]))

    # --------------------------------------------------------------- costumes ---
    def test_a_costume_beats_the_regular_item_and_its_removal_restores_it(self):
        char, bag = self.fresh()
        self.wear(bag, VEST)
        self.assertEqual(self.wear(bag, CASH_SHIRT), INV.kind_tables(self.catalog)[1][6])   # slot 17
        self.assertEqual(char['look'][6], self.catalog.get(CASH_SHIRT).spr_num)
        bag.take_off(VEST)                                      # the costume still shows
        self.assertEqual(char['look'][6], self.catalog.get(CASH_SHIRT).spr_num)
        self.wear(bag, VEST)                                    # still the costume on top
        self.assertEqual(char['look'][6], self.catalog.get(CASH_SHIRT).spr_num)
        bag.take_off(CASH_SHIRT)                                # the regular vest again
        self.assertEqual(char['look'][6], 7)
        bag.take_off(VEST)
        self.assertEqual(char['look'][6], 1)

    def test_the_clear_hat_shows_the_hair(self):
        char, bag = self.fresh()
        self.wear(bag, HAT)
        self.assertEqual(char['look'][10], 360)
        self.wear(bag, CLEAR_HAT)
        self.assertEqual(char['look'][10], char['look'][INV.LAYER_HAIR])
        bag.take_off(CLEAR_HAT)
        self.assertEqual(char['look'][10], 360)

    def test_weapon_skins_follow_the_job_rule(self):
        """A skin shows unless its Job[0] differs from the real weapon's and one of the two
        has Job[1] == 0; a skin over no real weapon changes nothing."""
        char, bag = self.fresh()
        self.wear(bag, CASH_BLADE)                              # nothing real under it
        self.assertEqual(char['look'][11], 0)
        self.wear(bag, SWORD)                                   # Job[0] 1 vs 0: the real sword
        self.assertEqual(char['look'][11], 5)
        bag.take_off(SWORD)
        self.wear(bag, CLASS1_BLADE)                            # Job[0] 0 == 0: the skin
        self.assertEqual(char['look'][11], self.catalog.get(CASH_BLADE).spr_num)
        bag.take_off(CLASS1_BLADE)                              # bare hand again
        self.assertEqual(char['look'][11], 0)

    def test_kinds_past_the_build_limit_change_no_word(self):
        """2008 composes Kinds 0..13 (belt / earring / necklace / ring have 2008 grid slots
        but no layer); 2009 has no regular slot for 14..16 and composes no ring (Kind 17)."""
        char, bag = self.fresh()
        before = self.words(char)
        for item in (BELT, EARRING, NECKLACE, RING):
            if bag.slot_for(item) is None:
                self.assertEqual(self.build, B9)
                continue
            self.wear(bag, item)
            self.assertEqual(self.words(char), before, item)
        self.assertEqual(len(before), words_of(self.build))

    def test_compose_ignores_a_kind_the_look_has_no_word_for(self):
        look = list(CREATION_F)
        INV.compose(look, [0] * 25, 15, 1, 101, 0, 1, self.catalog.get, INV.COMPOSE_KIND_LIMIT_2009)
        self.assertEqual(look, CREATION_F)                      # 14 words: no index 15 to write
        INV.compose(look, [0] * 25, -1, 0, 7, 0, 1, self.catalog.get)
        self.assertEqual(look, CREATION_F)                      # a def without a Kind

    # ------------------------------------------------------------- migration ---
    def test_replaying_the_worn_items_gives_the_look_back(self):
        """The migration is compose_all over the stored look; for any look the client could
        have composed it is a no-op, whatever the order the items were worn in."""
        tables = INV.kind_tables(self.catalog)
        items = [i for i, d in self.catalog.defs.items()
                 if d.type == 1 and d.kind >= 0 and (tables[1] if d.is_cash else tables[0]).get(d.kind) is not None]
        rng = random.Random(20260925)
        for trial in range(60):
            gender = trial % 2
            char, bag = self.fresh(gender)
            for step in range(25):
                worn = list(char['equipped'].values())
                if worn and rng.random() < 0.4:
                    entry = rng.choice(worn)
                    self.assertIsNotNone(bag.take_off(entry['id'], entry['w'], gender=gender)[0])
                else:
                    item = rng.choice(items)
                    self.assertIsNotNone(bag.add(item))
                    bag.wear(item, gender=gender)
                    if bag.free_slots('equip') < 5:
                        bag.data['equip'].clear()
                replay = copy.deepcopy(char)
                self.assertFalse(INV.Inventory(replay).compose_all(gender),
                                 f'trial {trial} step {step}: {char["look"]} -> {replay["look"]}')

    def test_unworn_layers_keep_the_stored_words(self):
        char = testhero_record(self.build, equipped={5: {'id': SWORD, 'w': [0] * 6}})
        INV.Inventory(char).compose_all(0)
        expect = list(CREATION_F)
        expect[11] = 5
        self.assertEqual(char['look'], expect)

    # ------------------------------------------------------------ the wire ---
    def test_testhero_records_carry_the_composed_words(self):
        """spec 5.2: 0x02, the own 0x07 and the 0x04 / 0x05 for others."""
        char = testhero_record(self.build)
        account = {'uid': 1, 'gender': 0, 'manner': 0, 'characters': [char]}
        INV.Inventory(char).compose_all(R.record_gender(char, account, self.build))
        look = TESTHERO_LOOK + ([0, 0, 0] if self.build == B9 else [])
        build = self.build if self.build == B9 else None
        # 0x02 (select screen)
        body = P.build('0x02', R.character_list(1, account, client_build=build), client_build=build)
        row = P.parse('0x02', body, direction='S2C', client_build=build)['repeat[char_count]'][0]
        self.assertEqual(look_of(row), look)
        if self.build == B9:
            self.assertEqual(row['gender'], 0)
        # the own 0x07 (enter world / map change / relog)
        session = {'uid': 1, 'hp': 100, 'mp': 50}
        rec = R.player_record(session, char, account, client_build=build)
        body = P.build('0x07', R.player_list(rec), client_build=build, receiver_uid=1)
        row = P.parse('0x07', body, direction='S2C', client_build=build)['repeat[player_count]'][0]
        self.assertEqual((row['gender'], look_of(row)), (0, look))
        self.assertEqual(grid_of(row), TESTHERO_EQUIP + ([] if self.build == B9 else [0]))
        self.assertEqual(cash_of(row), [0] * (10 if self.build == B9 else 9))
        # 0x04 to another client, and 0x05
        remote = R.player_record(session, char, account, remote=True, client_build=build)
        body = P.build('0x04', R.player_list(remote), client_build=build, receiver_uid=2)
        row = P.parse('0x04', body, direction='S2C', client_build=build)['repeat[player_count]'][0]
        self.assertEqual((row['gender'], look_of(row), grid_of(row)[:15]), (0, look, TESTHERO_EQUIP))
        body = P.build('0x05', R.to_0x05(remote, build), client_build=build, receiver_uid=2)
        row = P.parse('0x05', body, direction='S2C', client_build=build)
        self.assertEqual((row['gender'], look_of(row), grid_of(row)[:15]), (0, look, TESTHERO_EQUIP))


@unittest.skipUnless(HAVE[B8], 'needs the EN 2008 client data')
class Compose2008(_Compose, unittest.TestCase):
    build = B8

    def test_an_earring_in_its_2008_grid_slot_is_no_layer(self):
        """Kind 15 sits in 2008 grid slot 11 - the same index as the weapon LAYER. The grid
        slot is not a layer: the earring must not touch word 11."""
        char, bag = self.fresh()
        self.wear(bag, SWORD)
        self.assertEqual(self.wear(bag, EARRING), 11)
        self.assertEqual(char['look'][11], 5)


@unittest.skipUnless(HAVE[B9], 'needs the EN 2009 client data')
class Compose2009(_Compose, unittest.TestCase):
    build = B9

    def test_the_pet_layers_live_in_look_ext(self):
        char, bag = self.fresh()
        self.assertEqual(self.wear(bag, PET_HAT), 23)
        self.assertEqual((char['look'], char['look_ext']), (CREATION_F, [0, 101, 0]))
        rec = R.player_record({'uid': 1}, char, {'uid': 1}, client_build=B9)
        self.assertEqual(look_of(rec)[15], 101)                 # word 15 of the 17
        bag.take_off(PET_HAT)
        self.assertEqual(char['look_ext'], [0, 0, 0])


# ================================================================== the flows ===
class _Flow:
    """TestHero (test/test, uid 1) and Watcher (admin/admin, uid 2) on map 101 of a server
    of `build` over fake clients."""
    build = B8

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        self.tmp = tempfile.mkdtemp(prefix=f'ws_appearance_{self.build}_')
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            try:
                c.close()
            except OSError:
                pass
        EC.configure(DIRS[B8], B8)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_server(self, hero=None):
        accounts = F.two_player_accounts()
        accounts['test']['characters'][0].update(hero or {
            'class': 1, 'exp': progression.exp_for_level(7)})
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False})
        self.server = F.make_server(self.tmp, accounts=accounts, config=cfg)
        return self.server

    def key(self, op, index=-1):
        return P.variants(op, 'C2S', client_build=self.build)[index]['key']

    def client(self):
        c = F.FakeClient(self.server)
        self.clients.append(c)
        return c

    def login_enter(self, user, password, name, number):
        """(client, its S2C 0x02, the packets of its map load)."""
        c = self.client()
        c.number, c.char_name = number, name
        charlist = c.login(user, password)
        self.assertEqual(charlist['result'], 1)
        entry = c.enter_world(name, port=F.P2P_PORT_BASE + number - 1)
        return c, charlist, entry

    def hero(self):
        return self.server.store.find_character('test', 'TestHero')

    def rows_for(self, c, pkts, uid):
        """Decoded player rows of `uid` in 0x07 / 0x04 / 0x05 packets."""
        out = []
        for p in pkts:
            if p.opcode in (0x07, 0x04):
                out += [r for r in c.s2c(p)['repeat[player_count]'] if r['uid'] == uid]
            elif p.opcode == 0x05:
                rec = c.s2c(p)
                if rec['uid'] == uid:
                    out.append(rec)
        return out

    def expected_look(self):
        return TESTHERO_LOOK + ([0, 0, 0] if self.build == B9 else [])

    def assert_testhero_row(self, row):
        self.assertEqual(look_of(row), self.expected_look())
        self.assertEqual(row['gender'], 0)
        self.assertEqual(grid_of(row)[:15], TESTHERO_EQUIP)
        self.assertEqual(cash_of(row), [0] * (10 if self.build == B9 else 9))

    def test_a_record_stored_before_the_fix_is_composed_at_login(self):
        """The live record: creation look + six worn items + weapon 5. The login composes it
        (GameServer._compose_looks), so the select screen, the own 0x07 and the record the
        observer gets all draw the worn set - and the file keeps the composed words."""
        hero = testhero_record(self.build)
        for key in ('gender', 'look_ext'):                      # the 2009 store adds them itself
            hero.pop(key, None)
        self.make_server(hero)
        self.assertEqual(self.hero()['look'], CREATION_F)       # untouched until the login
        a, charlist, entry = self.login_enter('test', 'test', 'TestHero', 1)
        row = charlist['repeat[char_count]'][0]
        self.assertEqual(look_of(row), self.expected_look())
        if self.build == B9:
            self.assertEqual(row['gender'], 0)
        (own,) = self.rows_for(a, entry, 1)
        self.assert_testhero_row(own)
        b, _, entry_b = self.login_enter('admin', 'admin', 'Watcher', 2)
        (seen,) = self.rows_for(b, entry_b, 1)                  # A's row in B's arrival
        self.assert_testhero_row(seen)
        self.server.store.flush()
        with open(self.server.db_file, encoding='utf-8') as f:
            disk = json.load(f)['test']['characters'][0]
        self.assertEqual((disk['look'], disk['weapon']), (TESTHERO_LOOK, 5))
        # no schema change, so no new one-time backup for it
        self.assertFalse(os.path.exists(self.server.db_file + '.bak-pre-appearance'))

    def test_equip_reaches_the_observer_and_survives_a_relog(self):
        self.make_server()
        a, _, _ = self.login_enter('test', 'test', 'TestHero', 1)
        b, _, _ = self.login_enter('admin', 'admin', 'Watcher', 2)
        a.recv_until_quiet(0.2), b.recv_until_quiet(0.2)
        for item in (HAT, VEST, SWORD, LEGGINGS, GLOVES, SANDAL):
            self.assertIsNotNone(self.server._inv_add(a.session, item, 1, 'test'))
            a.send_c2s(self.key(0x0F), {'item_id': item, 'stone_count': 0, 'extra_option': 0})
            mine = [p for p in a.recv_until_quiet(0.2) if p.opcode == 0x1D]
            theirs = [p for p in b.recv_until_quiet(0.2) if p.opcode == 0x1D]
            self.assertEqual((len(mine), len(theirs)), (1, 1), item)
            self.assertEqual((b.s2c(theirs[0])['uid'], b.s2c(theirs[0])['item_id']), (1, item))
        # the stored look is what both clients composed from those 0x1D
        self.assertEqual(self.hero()['look'], TESTHERO_LOOK)
        # unequip the vest: the underwear on both clients and in the store, then wear it again
        a.send_c2s(self.key(0x11), {'item_id': VEST, 'enchant_count': 0, 'enchant_last': 0})
        self.assertEqual([p.opcode for p in a.recv_until_quiet(0.2) if p.opcode == 0x1E], [0x1E])
        self.assertEqual([p.opcode for p in b.recv_until_quiet(0.2) if p.opcode == 0x1E], [0x1E])
        self.assertEqual(self.hero()['look'][6], 1)
        a.send_c2s(self.key(0x0F), {'item_id': VEST, 'stone_count': 0, 'extra_option': 0})
        a.recv_until_quiet(0.2), b.recv_until_quiet(0.2)
        self.assertEqual(self.hero()['look'], TESTHERO_LOOK)
        # relog A: the select screen, the own 0x07 and B's new record of A all carry the look
        a.close()
        self.assertTrue(_wait(lambda: self.server.world.session(1) is None))
        b.recv_until_quiet(0.2)                                 # A's despawn
        a2, charlist, entry = self.login_enter('test', 'test', 'TestHero', 1)
        self.assertEqual(look_of(charlist['repeat[char_count]'][0]), self.expected_look())
        (own,) = self.rows_for(a2, entry, 1)
        self.assert_testhero_row(own)
        seen = self.rows_for(b, b.recv_until_quiet(0.3), 1)    # A's new spawn on B
        self.assertTrue(seen)
        for row in seen:
            self.assert_testhero_row(row)

    def test_dropping_a_worn_vest_leaves_the_underwear_on_every_record(self):
        """C2S 0x14 -> S2C 0x24 (0x1E without the bag add), the third grid writer: both clients
        recompose the shirt layer with Spr 0 (the underwear, not the creation shirt), and so
        must the store, or the relog 0x02 / 0x07 and the observer's respawn would put the
        creation shirt back."""
        self.make_server()
        a, _, _ = self.login_enter('test', 'test', 'TestHero', 1)
        b, _, _ = self.login_enter('admin', 'admin', 'Watcher', 2)
        a.recv_until_quiet(0.2), b.recv_until_quiet(0.2)
        self.assertEqual(self.hero()['look'][6], CREATION_F[6])
        self.assertIsNotNone(self.server._inv_add(a.session, VEST, 1, 'test'))
        a.send_c2s(self.key(0x0F), {'item_id': VEST, 'stone_count': 0, 'extra_option': 0})
        a.recv_until_quiet(0.2), b.recv_until_quiet(0.2)
        self.assertEqual(self.hero()['look'][6], 7)
        a.send_c2s(self.key(0x14), {'item_id': VEST, 'opt_count': 0, 'opt6': 0})
        self.assertIn(0x24, [p.opcode for p in a.recv_until_quiet(0.2)])
        self.assertIn(0x24, [p.opcode for p in b.recv_until_quiet(0.2)])
        self.assertEqual(self.hero()['equipped'], {})
        self.assertEqual(self.hero()['look'][6], 1)             # female underwear
        a.close()
        self.assertTrue(_wait(lambda: self.server.world.session(1) is None))
        b.recv_until_quiet(0.2)                                 # A's despawn
        a2, charlist, entry = self.login_enter('test', 'test', 'TestHero', 1)
        self.assertEqual(look_of(charlist['repeat[char_count]'][0])[6], 1)
        (own,) = self.rows_for(a2, entry, 1)
        self.assertEqual(look_of(own)[6], 1)
        seen = self.rows_for(b, b.recv_until_quiet(0.3), 1)
        self.assertTrue(seen)
        self.assertEqual({look_of(row)[6] for row in seen}, {1})


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


@unittest.skipUnless(HAVE[B8], 'needs the EN 2008 client data')
class Flow2008(_Flow, unittest.TestCase):
    build = B8


@unittest.skipUnless(HAVE[B9], 'needs the EN 2009 client data')
class Flow2009(_Flow, unittest.TestCase):
    build = B9


if __name__ == '__main__':
    unittest.main(verbosity=1)
