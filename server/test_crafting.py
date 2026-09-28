#!/usr/bin/env python3
"""
test_crafting.py - Concoction / Mineral Refining, Reinforcement, Gathering and the item
dead-code cleanup, both client builds (P4 stage 3: item_inventory-crafting,
-reinforcement, -gathering, -dead-code-cleanup; item_inventory.md F11-F13, crafting.py)

Four layers:
- the client content both builds share: the Union recipes (361), the success table read
  straight out of each client exe (2008 DAT_006f09ec / 2009 DAT_005250c4), the gather nodes
  per map, the Elementirium -> option stone mapping;
- crafting.py on a bare character record (every result code and its exact bag change);
- the server through fakeclient for BOTH builds (no port, no game client, temp
  accounts.json): C2S 0x67 / 0x68 / 0x69 -> exactly one S2C 0x8D / 0x8E / 0x8F with the
  bytes the spec reads, the bag model changed the way the client changes its own, the
  change persisted, the reinforced option in the 0x03 of a portal and a relog, the rate
  limit, and the registry backstop when a handler raises;
- the dead code item_inventory-dead-code-cleanup lists is gone.
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

import config as cfgmod  # noqa: E402
import crafting as C  # noqa: E402
import en_content as EC  # noqa: E402
import fakeclient as F  # noqa: E402
import inventory as INV  # noqa: E402
import packets as P  # noqa: E402
import progression  # noqa: E402
import registry  # noqa: E402
import skills as SK  # noqa: E402

W = F.import_server()
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = '2008', '2009'
DIR8 = os.path.join(HERE, cfgmod.DEFAULTS['CLIENT_DIR'])
DIR9 = os.path.join(HERE, cfgmod.DEFAULTS['CLIENT_DIR_2009'])
HAVE_2008 = os.path.exists(os.path.join(DIR8, 'hs', 'windslayer.hii'))
HAVE_2009 = os.path.exists(os.path.join(DIR9, 'hs', 'windslayer.hii'))
needs_2008 = unittest.skipUnless(HAVE_2008, 'needs the EN 2008 client data (hs/)')
needs_2009 = unittest.skipUnless(HAVE_2009, 'needs the EN 2009 client data (CLIENT_DIR_2009)')

# EN content both builds share (the recipe, stone and node records are identical)
HERB, BLUE_MUSHROOM, WEED, WOOD_PIECE, ELEDUST = 5, 3, 286, 197, 281
STICK, WOODEN_BLADE = 179, 70                  # Kind 11, Lv 1 / Lv 13
CHIPPED, FLAWED, PERFECT, BOOSTER = 2975, 2976, 2979, 2980
MINOR_HEAL = 3057                              # Concoction: Herb x1 + Weed x2, Lv 5, Type 0
CRUDE_CLUB = 1828                              # Mineral Refining: Wood Piece x3, Lv 3, Type 1
CONCOCTION_1, CONCOCTION_3, REFINING_1, REINFORCE = 2177, 2179, 2166, 2188
HERB_GATHERING, MINING = 86, 82
CRUDE_GARDEN_SHOVEL, GARDEN_SHOVEL, CRUDE_SHOVEL = 2215, 2216, 2219
COMMON_HERB, IMPERIAL_HERB, COMMON_VEIN, IMPERIAL_VEIN = 118, 123, 128, 127
TOWN, PUPU_MAP, HERB_FARM, MINING_AREA = 101, 102, 241, 243
FIRE_GROUP = 2                                 # 2955..2959 Elemental Stone - Fire

KEYS = {
    B8: {'login': '0x44D8BF/0x01', 'enter': '0x42F904/0x2B', 'portal': '0x42F76B/0x7E',
         'craft': '0x46873D/0x67', 'reinforce': '0x4686E8/0x68', 'gather': '0x4685B2/0x69',
         'chat': '0x445CA7/0x03'},
    B9: {'login': '0x451CE5/0x01', 'enter': '0x4315D7/0x2B', 'portal': '0x431284/0x7E',
         'craft': '0x4724C7/0x67', 'reinforce': '0x472478/0x68', 'gather': '0x472602/0x69',
         'chat': '0x44790E/0x03'},
}
# The success table's address in each exe (FUN_00468280 / 2009 FUN_004720a0).
TABLE_VA = {B8: (os.path.join(DIR8, 'WindSlayer.exe'), 0x6F09EC),
            B9: (os.path.join(DIR9, 'WindSlayer.exe'), 0x5250C4)}
LV30 = progression.exp_for_level(30)


def _char(name, map_code, x=1411, y=714):
    return {'name': name, 'level': 30, 'exp': LV30, 'class': 0, 'map': map_code, 'x': x, 'y': y,
            'hp': 100, 'mp': 50}


ACCOUNTS = {
    'test': {'password': 'test', 'characters': [dict(_char('TestHero', TOWN), gm=1)]},
    'herb': {'password': 'herb', 'characters': [_char('Herbalist', HERB_FARM, 400, 600)]},
    'miner': {'password': 'miner', 'characters': [_char('Miner', MINING_AREA, 400, 600)]},
}
_LIVE_HASH = None


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def setUpModule():
    global _LIVE_HASH
    _LIVE_HASH = _sha(LIVE_ACCOUNTS) if os.path.exists(LIVE_ACCOUNTS) else None
    P.STRICT_FIELDS.add(B9)          # every 2009 S2C must use the 2009 field names


def tearDownModule():
    P.STRICT_FIELDS.discard(B9)
    EC.configure(cfgmod.DEFAULTS['CLIENT_DIR'], B8)
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_crafting.py (tests must only use temp copies)'


def use_build(build):
    EC.configure(cfgmod.DEFAULTS['CLIENT_DIR_2009'] if build == B9 else cfgmod.DEFAULTS['CLIENT_DIR'], build)


class Rolls:
    """A pinned random source: randrange(n) returns the queued values in order (clamped to
    n - 1), then 0."""

    def __init__(self, *values):
        self.values = list(values)
        self.calls = []

    def randrange(self, n):
        self.calls.append(n)
        value = self.values.pop(0) if self.values else 0
        return max(0, min(int(value), n - 1))


def bare_char(level=30, map_code=TOWN):
    char = {'name': 'Bare', 'exp': progression.exp_for_level(level), 'map': map_code}
    INV.ensure(char)
    SK.ensure(char)
    return char


def read_table(path, va):
    """CRAFT_SUCCESS_PCT rows 1..10 as the exe holds them (PE section walk)."""
    with open(path, 'rb') as f:
        data = f.read()
    pe = struct.unpack_from('<I', data, 0x3C)[0]
    nsec = struct.unpack_from('<H', data, pe + 6)[0]
    optsz = struct.unpack_from('<H', data, pe + 20)[0]
    base = struct.unpack_from('<I', data, pe + 24 + 28)[0]
    rva = va - base
    for i in range(nsec):
        vsz, sva, rsz, rptr = struct.unpack_from('<IIII', data, pe + 24 + optsz + i * 40 + 8)
        if sva <= rva < sva + max(vsz, rsz):
            off = rptr + rva - sva
            vals = struct.unpack_from('<121i', data, off)
            return [tuple(vals[r * 11:(r + 1) * 11]) for r in range(1, 11)]
    raise AssertionError(f'{va:#x} is in no section of {path}')


# ================================================================== content ===
class ContentChecks:
    """What crafting.py reads from the client files, per build."""
    build = B8

    def setUp(self):
        use_build(self.build)

    def tearDown(self):
        use_build(B8)

    def test_the_recipes_are_the_union_columns(self):
        cat = EC.items()
        recipes = [d for d in cat.defs.values() if C.craft_kind(d)]
        kinds = [C.craft_kind(d) for d in recipes]
        self.assertEqual((len(recipes), kinds.count(C.UNION_KIND_REFINING), kinds.count(C.UNION_KIND_CONCOCTION)),
                         (361, 342, 19))
        self.assertEqual(C.materials(cat.get(CHIPPED)), [C.Material(ELEDUST, 10, 'etc')])
        self.assertEqual(C.materials(cat.get(MINOR_HEAL)), [C.Material(HERB, 1, 'consume'), C.Material(WEED, 2, 'etc')])
        self.assertEqual(C.materials(cat.get(PERFECT)), [C.Material(284, 2, 'etc'), C.Material(285, 1, 'etc')])
        for d in recipes:
            with self.subTest(product=d.id):
                self.assertIn(cat.type_of(d.id), (0, 1, 2))
                self.assertLessEqual(d.lv, 99)          # the table has no column past Lv 99
                for m in C.materials(d):
                    self.assertIn(m.tab, ('consume', 'etc'))   # EN recipes use Type 0 / 2 only
        self.assertIsNone(C.craft_kind(cat.get(WOODEN_BLADE)))  # Union set, Union_Kind 0: no recipe

    def test_the_success_table_is_the_client_exe_table(self):
        path, va = TABLE_VA[self.build]
        if not os.path.exists(path):
            self.skipTest(f'{path} not installed')
        self.assertEqual(read_table(path, va), list(C.CRAFT_SUCCESS_PCT[1:]))

    def test_success_pct_reads_the_client_columns(self):
        self.assertEqual(C.success_pct(1, 7), 70)            # Chipped Elementirium at Lv 1
        self.assertEqual(C.success_pct(1, 14), 1)
        self.assertEqual(C.success_pct(3, 25), 15)
        self.assertEqual(C.success_pct(10, 99), 1)          # Lv 99 -> column 10
        self.assertEqual(C.success_pct(10, 95), 15)
        self.assertEqual(C.success_pct(0, 1), 0)             # not learned
        self.assertEqual(C.success_pct(11, 1), 0)

    def test_gather_nodes_per_map(self):
        self.assertEqual(EC.gather_nodes(HERB_FARM), {COMMON_HERB, IMPERIAL_HERB})
        self.assertEqual(EC.gather_nodes(MINING_AREA), {COMMON_VEIN, IMPERIAL_VEIN})
        self.assertEqual(EC.gather_nodes(TOWN), frozenset())
        npcs = EC.npcs()
        for idx in (118, 122, 123, 124, 125, 126, 127, 128):
            node = npcs.at_index(idx)                     # the C2S 0x69 index = hni position
            self.assertEqual(node.idx, idx)
            self.assertIn(node.type, EC.GATHER_NODE_TYPES)
            rewards = C.gather_rewards(node)
            self.assertTrue(rewards)
            self.assertTrue(all(EC.items().tab_of(i) in INV.TABS for i, _ in rewards))

    def test_every_elementirium_maps_to_an_option_stone_of_its_grade(self):
        cat = EC.items()
        for grade, stone in enumerate(range(CHIPPED, PERFECT + 1)):
            self.assertTrue(C.is_elementirium(stone))
            for group in range(C.OPTION_STONE_GROUPS):
                opt = C.option_stone(stone, group)
                with self.subTest(stone=stone, group=group):
                    self.assertTrue(cat.exists(opt))
                    self.assertEqual((cat.type_of(opt), cat.get(opt).lv), (2, grade + 1))
        self.assertEqual(C.option_stone(CHIPPED, FIRE_GROUP), 2955)
        self.assertFalse(C.is_elementirium(BOOSTER))       # 2980 is Type 0, not a stone
        self.assertEqual(cat.type_of(BOOSTER), 0)


@needs_2008
class Content2008(ContentChecks, unittest.TestCase):
    build = B8


@needs_2009
class Content2009(ContentChecks, unittest.TestCase):
    build = B9

    def test_the_2009_only_farms_have_nodes_too(self):
        self.assertTrue({COMMON_HERB, IMPERIAL_HERB} <= EC.gather_nodes(1601))
        self.assertTrue({COMMON_VEIN, IMPERIAL_VEIN} <= EC.gather_nodes(1501))


# ============================================================== model rules ===
@needs_2008
class ModelRules(unittest.TestCase):
    """crafting.py on a bare character record: the result and the bag change."""

    def setUp(self):
        use_build(B8)
        self.char = bare_char()
        self.bag = INV.Inventory(self.char)

    def test_craft_results(self):
        c, bag = self.char, self.bag
        self.assertEqual(C.craft(c, CHIPPED).result, C.RESULT_NO_SKILL)
        SK.learn(c, CONCOCTION_1, check=False)
        out = C.craft(c, CHIPPED, rng=Rolls(0))
        self.assertEqual((out.result, out.consumed), (C.RESULT_SHORT, []))
        bag.add(ELEDUST, 20)
        out = C.craft(c, CHIPPED, rng=Rolls(69))               # 70% at Lv 1: 69 succeeds
        self.assertEqual((out.result, out.pct, out.granted), (C.RESULT_OK, 70, True))
        self.assertEqual((bag.count(ELEDUST), bag.count(CHIPPED)), (10, 1))
        out = C.craft(c, CHIPPED, rng=Rolls(70))               # ... and 70 fails
        self.assertEqual((out.result, out.granted), (C.RESULT_FAILED, False))
        self.assertEqual((bag.count(ELEDUST), bag.count(CHIPPED)), (0, 1))
        self.assertEqual(C.craft(c, STICK).result, C.RESULT_GENERIC)      # no recipe
        self.assertEqual(C.craft(c, 60000).result, C.RESULT_SHORT)        # no client record

    def test_the_skill_level_sets_the_chance(self):
        SK.learn(self.char, CONCOCTION_3, check=False)
        self.bag.add(ELEDUST, 10)
        self.bag.add(282, 5)                                   # Flawed: Elestone (Regular) x5, Lv 14
        out = C.craft(self.char, FLAWED, rng=Rolls(29))
        self.assertEqual((out.skill_level, out.pct, out.result), (3, 30, C.RESULT_OK))

    def test_a_type_1_material_goes_one_instance_at_a_time(self):
        d = mock.Mock(recipe=[(STICK, 3), (ELEDUST, 2), (STICK, 5)])
        self.assertEqual(C.materials(d), [C.Material(STICK, 2, 'equip'), C.Material(ELEDUST, 2, 'etc')])

    def test_no_room_consumes_the_materials(self):
        SK.learn(self.char, CONCOCTION_1, check=False)
        self.bag.add(ELEDUST, 15)
        self.bag.set_capacity('etc', 1)                        # 5 Eledust stay in the one slot
        out = C.craft(self.char, CHIPPED, rng=Rolls(0))
        self.assertEqual(out.result, C.RESULT_FULL)
        self.assertEqual((self.bag.count(ELEDUST), self.bag.count(CHIPPED)), (5, 0))
        # exactly 10: the slot empties first, so the product fits (the client's own order)
        self.bag.add(ELEDUST, 5)
        self.assertEqual(C.craft(self.char, CHIPPED, rng=Rolls(0)).result, C.RESULT_OK)

    def test_reinforce_results(self):
        c, bag = self.char, self.bag
        bag.add(WOODEN_BLADE, 1)
        bag.add(CHIPPED, 1)
        zero = [0] * 6
        self.assertEqual(C.reinforce(c, WOODEN_BLADE, CHIPPED, zero).result, C.RESULT_FAILED)   # no skill
        SK.learn(c, REINFORCE, check=False)
        out = C.reinforce(c, WOODEN_BLADE, CHIPPED, zero, rng=Rolls(99), success_rate=70)
        self.assertEqual(out.result, C.RESULT_FAILED)
        self.assertEqual((bag.instance(WOODEN_BLADE)['w'], bag.count(CHIPPED)), (zero, 1))
        out = C.reinforce(c, WOODEN_BLADE, CHIPPED, zero, rng=Rolls(0, FIRE_GROUP), success_rate=70)
        self.assertEqual((out.result, out.new_option, out.old_words), (C.RESULT_OK, 2955, zero))
        self.assertEqual((bag.instance(WOODEN_BLADE)['w'], bag.count(CHIPPED)), ([2955, 0, 0, 0, 0, 0], 0))
        for equip, stone, words, why in (
                (STICK, CHIPPED, zero, 'stone Lv 7 > equipment Lv 1'),
                (WOODEN_BLADE, BOOSTER, [2955, 0, 0, 0, 0, 0], 'not an Elementirium'),
                (WOODEN_BLADE, CHIPPED, zero, 'no bag instance'),       # the block is [2955, ...] now
                (WOODEN_BLADE, CHIPPED, [2955, 0, 0, 0, 0, 0], 'no 2975 in the bag'),
                (WOODEN_BLADE, CHIPPED, [0, 2955, 0, 0, 0, 0], 'not packed'),
                (WOODEN_BLADE, CHIPPED, [1, 2, 3, 4, 5, 0], 'no free option word'),
                (HERB, CHIPPED, zero, 'not EN equipment')):
            with self.subTest(why):
                bag.add(STICK, 1)
                out = C.reinforce(c, equip, stone, words, rng=Rolls(0, 0), success_rate=100)
                self.assertEqual(out.result, C.RESULT_CANNOT_REINFORCE)
                self.assertIn(why, out.why)
        self.assertEqual(bag.instance(WOODEN_BLADE)['w'], [2955, 0, 0, 0, 0, 0])

    def test_gather_results(self):
        c, bag = bare_char(level=30, map_code=HERB_FARM), None
        bag = INV.Inventory(c)
        bag.add(CRUDE_GARDEN_SHOVEL, 5)
        out = C.gather(c, HERB_FARM, COMMON_HERB, CRUDE_GARDEN_SHOVEL, rng=Rolls(0))
        self.assertEqual((out.result, out.tool_removed), (C.RESULT_FAILED, True))    # no skill
        self.assertIn('not learned', out.why)
        SK.learn(c, HERB_GATHERING, check=False)
        out = C.gather(c, HERB_FARM, COMMON_HERB, CRUDE_GARDEN_SHOVEL, rng=Rolls(0))
        self.assertEqual((out.result, out.reward), (C.RESULT_OK, HERB))              # Herb: rate 20000
        self.assertEqual((bag.count(CRUDE_GARDEN_SHOVEL), bag.count(HERB)), (3, 1))
        out = C.gather(c, HERB_FARM, COMMON_HERB, CRUDE_GARDEN_SHOVEL, rng=Rolls(65550))
        self.assertEqual((out.result, out.reward), (C.RESULT_FAILED, 0))            # past the 65550 total
        rolls = Rolls(30000)
        out = C.gather(c, HERB_FARM, COMMON_HERB, CRUDE_GARDEN_SHOVEL, rng=rolls, divisor=0)
        self.assertEqual((out.result, out.reward), (C.RESULT_OK, WEED))             # weights: always something
        self.assertEqual(rolls.calls, [65550])                                      # the column total
        self.assertEqual((bag.count(CRUDE_GARDEN_SHOVEL), bag.count(WEED)), (1, 1))

    def test_roll_reward_walks_the_drop_column(self):
        node = EC.npcs().get(COMMON_HERB)
        rewards = C.gather_rewards(node)
        self.assertEqual(rewards[:3], [(HERB, 20000), (WEED, 15000), (WOOD_PIECE, 15000)])
        self.assertEqual(C.roll_reward(node, rng=Rolls(19999)), HERB)
        self.assertEqual(C.roll_reward(node, rng=Rolls(20000)), WEED)
        self.assertEqual(C.roll_reward(node, rng=Rolls(sum(r for _, r in rewards))), 0)


# =============================================================== server rig ===
class Rig(unittest.TestCase):
    build = B8

    def setUp(self):
        use_build(self.build)
        self.tmp = tempfile.mkdtemp(prefix=f'ws_craft{self.build}_')
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
    def char(self, user='test'):
        return self.server.store.characters(user)[0]

    def bag(self, user='test'):
        return INV.Inventory(self.char(user))

    def enter(self, user='test', mobs=0):
        c = F.FakeClient(self.server)
        self.clients.append(c)
        if self.build == B9:
            c.send_c2s(self.keys['login'], F.sso_login(user, user))
        else:
            c.send_c2s(self.keys['login'], {'account_id': user, 'password': user})
        self.assertEqual(c.s2c(c.expect(0x02))['result'], 1)
        c.send_c2s(self.keys['enter'], {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907,
                                        'char_name': self.char(user)['name']})
        ops = [0x03, 0x07, 0x15] + ([0x65] if self.build == B9 else []) + [0x28, 0x44]
        pkts = c.expect(*ops, *F.mob_packets(mobs))
        self.assertTrue(c.wait_session(lambda s: s.get('in_world')))
        return c, c.s2c(pkts[0], allow_trailing=True)

    def relog(self, c, user='test', mobs=0):
        c.close()
        c.thread.join(timeout=5.0)
        return self.enter(user, mobs)

    def give(self, c, item, qty=1, words=None):
        self.assertIsNotNone(self.server._inv_add(c.session, item, qty, 'test', words))

    def learn(self, user, *skills):
        for sid in skills:
            self.assertTrue(SK.learn(self.char(user), sid, check=False).ok)

    def rolls(self, *values):
        self.server.CRAFT_RNG = Rolls(*values)
        return self.server.CRAFT_RNG

    @staticmethod
    def rearm(c):
        """The next completion comes a full progress bar later."""
        c.session.pop('craft_last_ms', None)
        c.session.pop('gather_last_ms', None)

    def disk_char(self, user='test'):
        self.server.store.flush()
        with open(self.server.db_file, encoding='utf-8') as f:
            return json.load(f)[user]['characters'][0]

    def craft(self, c, product):
        c.send_c2s(self.keys['craft'], {'product_item_id': product})
        pkt = c.expect(0x8D)
        self.assertEqual(len(pkt.payload), 3)
        return c.s2c(pkt)

    def reinforce(self, c, equip, stone, words=(0, 0, 0, 0, 0, 0)):
        c.send_c2s(self.keys['reinforce'], {'equip_item_id': equip, 'stone_item_id': stone,
                                            'repeat[6]': [{'equip_option_word': w} for w in words]})
        pkt = c.expect(0x8E)
        return pkt, c.s2c(pkt)

    def gather(self, c, node, tool):
        c.send_c2s(self.keys['gather'], {'gather_node_record_index': node, 'tool_item_id': tool})
        pkt = c.expect(0x8F)
        return pkt, c.s2c(pkt)

    @staticmethod
    def equip_rows(rec_03):
        return [(r['item_id'], [o['option_value'] for o in r['repeat[option_count]']], r['item_extra'])
                for r in rec_03['repeat[equip_item_count]']]


class CraftFlow:
    """P4 exit criterion 5, per build."""

    # --------------------------------------------------------- concoction ---
    def test_concoction_consumes_the_materials_once_and_grants_the_product(self):
        c, _ = self.enter()
        self.learn('test', CONCOCTION_1)
        self.give(c, ELEDUST, 25)
        self.rolls(0)
        self.assertEqual(self.craft(c, CHIPPED), {'result': 1, 'product_item_id': CHIPPED})
        bag = self.bag()
        self.assertEqual((bag.count(ELEDUST), bag.count(CHIPPED)), (15, 1))
        self.assertEqual(self.disk_char()['inventory']['etc'], {str(ELEDUST): 15, str(CHIPPED): 1})
        # a second completion inside the bar is not the client's: plain failure, no change
        self.assertEqual(self.craft(c, CHIPPED)['result'], C.RESULT_GENERIC)
        self.assertEqual((bag.count(ELEDUST), bag.count(CHIPPED)), (15, 1))
        self.rearm(c)
        self.rolls(0)
        self.assertEqual(self.craft(c, CHIPPED)['result'], 1)
        self.assertEqual((bag.count(ELEDUST), bag.count(CHIPPED)), (5, 2))
        self.rearm(c)
        self.assertEqual(self.craft(c, CHIPPED)['result'], C.RESULT_SHORT)      # 5 < 10: nothing used
        self.assertEqual((bag.count(ELEDUST), bag.count(CHIPPED)), (5, 2))

    def test_a_failed_roll_uses_the_materials_and_grants_nothing(self):
        c, _ = self.enter()
        self.learn('test', CONCOCTION_1)
        self.give(c, HERB, 3)
        self.give(c, WEED, 2)
        self.rolls(70)                                             # 70% at Lv 1 for a Lv-5 potion
        self.assertEqual(self.craft(c, MINOR_HEAL), {'result': 0, 'product_item_id': MINOR_HEAL})
        bag = self.bag()
        self.assertEqual((bag.count(HERB), bag.count(WEED), bag.count(MINOR_HEAL)), (2, 0, 0))

    def test_mineral_refining_needs_its_own_skill(self):
        c, _ = self.enter()
        self.learn('test', CONCOCTION_1)
        self.give(c, WOOD_PIECE, 3)
        self.assertEqual(self.craft(c, CRUDE_CLUB)['result'], C.RESULT_NO_SKILL)
        self.assertEqual(self.bag().count(WOOD_PIECE), 3)
        self.learn('test', REFINING_1)
        self.rearm(c)
        self.rolls(0)
        self.assertEqual(self.craft(c, CRUDE_CLUB)['result'], 1)
        bag = self.bag()
        self.assertEqual((bag.count(WOOD_PIECE), bag.instance(CRUDE_CLUB)['w']), (0, [0] * 6))

    def test_non_recipes_and_unknown_ids_change_nothing(self):
        c, _ = self.enter()
        self.learn('test', CONCOCTION_1)
        self.give(c, STICK)
        self.assertEqual(self.craft(c, STICK), {'result': C.RESULT_GENERIC, 'product_item_id': STICK})
        self.rearm(c)
        self.assertEqual(self.craft(c, 60000), {'result': C.RESULT_SHORT, 'product_item_id': 60000})
        self.assertEqual(self.bag().totals(), {STICK: 1})

    # ------------------------------------------------------ reinforcement ---
    def test_reinforce_adds_the_option_and_it_survives_portal_and_relog(self):
        c, _ = self.enter()
        self.learn('test', REINFORCE)
        self.give(c, WOODEN_BLADE)
        self.give(c, CHIPPED, 2)
        self.rolls(0, FIRE_GROUP)
        pkt, rec = self.reinforce(c, WOODEN_BLADE, CHIPPED)
        self.assertEqual(len(pkt.payload), 19)
        self.assertEqual(rec, {'result': 1, 'equip_item_id': WOODEN_BLADE, 'stone_item_id': CHIPPED,
                               'repeat[6]': [{'equip_option': 0}] * 6, 'new_option_item_id': 2955})
        bag = self.bag()
        self.assertEqual((bag.instance(WOODEN_BLADE)['w'], bag.count(CHIPPED)), ([2955, 0, 0, 0, 0, 0], 1))
        # a second one goes into the next word; the 0x8E echoes the words the client SENT
        self.rearm(c)
        self.rolls(0, 0)
        _, rec = self.reinforce(c, WOODEN_BLADE, CHIPPED, (2955, 0, 0, 0, 0, 0))
        self.assertEqual(([w['equip_option'] for w in rec['repeat[6]']], rec['new_option_item_id']),
                         ([2955, 0, 0, 0, 0, 0], 2945))
        self.assertEqual((bag.instance(WOODEN_BLADE)['w'], bag.count(CHIPPED)), ([2955, 2945, 0, 0, 0, 0], 0))
        # the portal's 0x03 rebuilds the bag from the model: the options are in it
        c.send_c2s(self.keys['portal'], {'portal_line_index': 23})
        pkts = c.expect(0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(8))
        self.assertEqual(self.equip_rows(c.s2c(pkts[1], allow_trailing=True)), [(WOODEN_BLADE, [2955, 2945], 0)])
        self.assertEqual(self.disk_char()['inventory']['equip'], [{'id': WOODEN_BLADE, 'w': [2955, 2945, 0, 0, 0, 0]}])
        c2, rec03 = self.relog(c, mobs=8)                             # back on map 102
        self.assertEqual(self.equip_rows(rec03), [(WOODEN_BLADE, [2955, 2945], 0)])

    def test_a_failed_reinforce_consumes_nothing(self):
        c, _ = self.enter()
        self.learn('test', REINFORCE)
        self.give(c, WOODEN_BLADE)
        self.give(c, CHIPPED)
        self.rolls(99)
        pkt, rec = self.reinforce(c, WOODEN_BLADE, CHIPPED)
        self.assertEqual((len(pkt.payload), rec), (5, {'result': 0, 'equip_item_id': WOODEN_BLADE,
                                                       'stone_item_id': CHIPPED}))
        bag = self.bag()
        self.assertEqual((bag.instance(WOODEN_BLADE)['w'], bag.count(CHIPPED)), ([0] * 6, 1))

    def test_reinforce_refusals_are_0x11_and_change_nothing(self):
        c, _ = self.enter()
        self.learn('test', REINFORCE)
        self.give(c, STICK)
        self.give(c, WOODEN_BLADE)
        self.give(c, CHIPPED)
        self.give(c, BOOSTER)
        before = self.bag().totals()
        for equip, stone, words in ((STICK, CHIPPED, (0,) * 6),           # stone Lv 7 > stick Lv 1
                                    (WOODEN_BLADE, BOOSTER, (0,) * 6),     # 2980 is no Elementirium
                                    (WOODEN_BLADE, CHIPPED, (7, 0, 0, 0, 0, 0)),   # no such block
                                    (WOODEN_BLADE, PERFECT, (0,) * 6)):    # stone not owned (Lv 59 too)
            with self.subTest(equip=equip, stone=stone, words=words):
                self.rearm(c)
                self.rolls(0, 0)
                pkt, rec = self.reinforce(c, equip, stone, words)
                self.assertEqual((len(pkt.payload), rec['result']), (5, C.RESULT_CANNOT_REINFORCE))
        self.assertEqual(self.bag().totals(), before)
        self.assertEqual(self.bag().instance(WOODEN_BLADE)['w'], [0] * 6)

    # ---------------------------------------------------------- gathering ---
    def test_herb_gathering_yields_a_reward_and_uses_the_tool(self):
        c, _ = self.enter('herb')
        self.learn('herb', HERB_GATHERING)
        self.give(c, CRUDE_GARDEN_SHOVEL, 3)
        rolls = self.rolls(0)
        pkt, rec = self.gather(c, COMMON_HERB, CRUDE_GARDEN_SHOVEL)
        self.assertEqual((len(pkt.payload), rec), (5, {'result': 1, 'tool_item_id': CRUDE_GARDEN_SHOVEL,
                                                       'reward_item_id': HERB}))
        self.assertEqual(rolls.calls, [100000])                    # GATHER_RATE_DIVISOR default
        bag = self.bag('herb')
        self.assertEqual((bag.count(CRUDE_GARDEN_SHOVEL), bag.count(HERB)), (2, 1))
        self.assertEqual(self.disk_char('herb')['inventory']['consume'], {str(HERB): 1})
        # nothing found: the tool is still used up (the client removed it)
        self.rearm(c)
        self.rolls(99999)
        pkt, rec = self.gather(c, COMMON_HERB, CRUDE_GARDEN_SHOVEL)
        self.assertEqual((len(pkt.payload), rec), (3, {'result': 0, 'tool_item_id': CRUDE_GARDEN_SHOVEL}))
        self.assertEqual((bag.count(CRUDE_GARDEN_SHOVEL), bag.count(HERB)), (1, 1))
        # inside the bar: refused, and the tool still goes
        self.rolls(0)
        self.assertEqual(self.gather(c, COMMON_HERB, CRUDE_GARDEN_SHOVEL)[1]['result'], 0)
        self.assertEqual((bag.count(CRUDE_GARDEN_SHOVEL), bag.count(HERB)), (0, 1))

    def test_gather_gates(self):
        c, _ = self.enter('herb')
        self.give(c, CRUDE_GARDEN_SHOVEL, 10)
        self.give(c, CRUDE_SHOVEL, 10)
        bag = self.bag('herb')
        cases = [(COMMON_HERB, CRUDE_GARDEN_SHOVEL, 'no Herb Gathering yet'),
                 (IMPERIAL_HERB, CRUDE_GARDEN_SHOVEL, 'Lv-3 shovel on a grade-26 herb'),
                 (COMMON_HERB, CRUDE_SHOVEL, 'a mining shovel on a herb'),
                 (COMMON_VEIN, CRUDE_SHOVEL, 'no vein on the herbal farm'),
                 (9999, CRUDE_GARDEN_SHOVEL, 'no such record')]
        for n, (node, tool, why) in enumerate(cases):
            if n == 1:
                self.learn('herb', HERB_GATHERING)
            with self.subTest(why):
                self.rearm(c)
                self.rolls(0)
                have = bag.count(tool)
                pkt, rec = self.gather(c, node, tool)
                self.assertEqual((len(pkt.payload), rec['result']), (3, 0))
                self.assertEqual(bag.count(tool), have - 1)
                self.assertEqual(bag.count(HERB), 0)
        # a tool the server bag does not hold: answered, nothing to remove
        self.rearm(c)
        self.assertEqual(self.gather(c, COMMON_HERB, GARDEN_SHOVEL)[1]['result'], 0)
        # an id the client has no record of: it only clears its busy flag (3 B either way)
        self.rearm(c)
        pkt, rec = self.gather(c, COMMON_HERB, 60000)
        self.assertEqual((len(pkt.payload), rec['result']), (3, 0))

    def test_a_full_bag_is_0x11_and_the_tool_still_goes(self):
        c, _ = self.enter('herb')
        self.learn('herb', HERB_GATHERING)
        self.give(c, CRUDE_GARDEN_SHOVEL, 2)
        self.give(c, BLUE_MUSHROOM, 1)
        self.bag('herb').set_capacity('consume', 1)                # the Herb reward has no slot
        self.rolls(0)
        pkt, rec = self.gather(c, COMMON_HERB, CRUDE_GARDEN_SHOVEL)
        self.assertEqual((len(pkt.payload), rec['result']), (3, C.RESULT_FULL))
        bag = self.bag('herb')
        self.assertEqual((bag.count(CRUDE_GARDEN_SHOVEL), bag.count(HERB)), (1, 0))

    def test_mining_on_the_mining_area(self):
        c, _ = self.enter('miner')
        self.learn('miner', MINING)
        self.give(c, CRUDE_SHOVEL, 1)
        self.rolls(0)
        _, rec = self.gather(c, COMMON_VEIN, CRUDE_SHOVEL)
        self.assertEqual((rec['result'], rec['reward_item_id']), (1, 287))    # Stone Debris, rate 30000
        bag = self.bag('miner')
        self.assertEqual((bag.count(CRUDE_SHOVEL), bag.count(287)), (0, 1))

    def test_a_gm_can_warp_to_the_herbal_farm_and_gather_there(self):
        # 241 is in no KR map table, only in the EN client's own files: !warp takes it now
        c, _ = self.enter()
        self.learn('test', HERB_GATHERING)
        self.give(c, CRUDE_GARDEN_SHOVEL)
        line = b'!warp 241 882 723'
        c.send_c2s(self.keys['chat'], {'msg_len': len(line), 'message': line})
        pkts = c.expect(0x15, 0x08, 0x03, 0x07, 0x28, 0x44)
        self.assertIn(b'Popola Herbal Farm-1', P.to_bytes(c.s2c(pkts[0])['text']))
        self.assertTrue(c.wait_session(lambda s: s.get('current_map') == HERB_FARM and s.get('in_world')))
        self.rolls(0)
        self.assertEqual(self.gather(c, COMMON_HERB, CRUDE_GARDEN_SHOVEL)[1]['reward_item_id'], HERB)
        c.send_c2s(self.keys['chat'], {'msg_len': 11, 'message': b'!warp 65000'})
        self.assertIn(b'not in the map table', P.to_bytes(c.s2c(c.expect(0x15))['text']))

    # ------------------------------------------------------------ backstop ---
    def test_a_raising_handler_still_releases_the_busy_flag(self):
        c, _ = self.enter()
        with mock.patch.object(C, 'craft', side_effect=RuntimeError('craft bug')), \
                self.assertLogs('WS', logging.ERROR):
            self.assertEqual(self.craft(c, CHIPPED), {'result': 0x0F, 'product_item_id': CHIPPED})
        with mock.patch.object(C, 'reinforce', side_effect=RuntimeError('bug')), \
                self.assertLogs('WS', logging.ERROR):
            self.rearm(c)
            self.assertEqual(self.reinforce(c, WOODEN_BLADE, CHIPPED)[1]['result'], 0x11)
        self.assertTrue(c.session['in_world'])

    def test_routes(self):
        routes = self.server.routes
        for op, handler in ((0x67, '_handle_craft_complete'), (0x68, '_handle_reinforce_complete'),
                            (0x69, '_handle_gather_complete')):
            self.assertEqual(routes[op].handler, handler)
            self.assertEqual(routes[op].style, registry.STYLE_REC)
            self.assertIn(op, registry.must_reply_table(self.build))     # the backstop stays


@needs_2008
class Craft2008(CraftFlow, Rig):
    build = B8


@needs_2009
class Craft2009(CraftFlow, Rig):
    build = B9


# =============================================================== dead code ===
class DeadCode(unittest.TestCase):
    """item_inventory-dead-code-cleanup (item_inventory.md B11/B13, I-15): the hand-packed item
    builders whose field order or count was wrong, and the u32 ground uid base."""

    DEAD = ('_build_en_opcode_1D', '_build_en_opcode_1E', '_build_opcode_12_drop', '_build_opcode_13',
            '_build_opcode_23', '_build_en_opcode_1D_equip', '_build_opcode_18', '_build_opcode_19')

    def test_the_dead_item_builders_are_gone(self):
        for name in self.DEAD:
            with self.subTest(name):
                self.assertFalse(hasattr(W.GameServer, name))
                self.assertFalse(hasattr(W, name))

    def test_no_module_defines_a_ground_uid_base(self):
        import ground
        import ids
        for mod in (W, ground, ids, INV):
            self.assertFalse(hasattr(mod, 'GROUND_ITEM_UID_BASE'), mod.__name__)

    def test_the_interim_craft_stub_is_replaced(self):
        # the gather stub answered {2, tool} whatever happened; the craft/reinforce ones were
        # log-only routes the policy refused
        for op in (0x67, 0x68, 0x69):
            self.assertIsNotNone(W.GameServer.ROUTES[op].handler)
            self.assertIsNotNone(W.GameServer.ROUTES_2009[op].handler)


class Config(unittest.TestCase):
    def test_defaults_and_validation(self):
        d = cfgmod.defaults()
        self.assertEqual((d.REINFORCE_SUCCESS_PCT, d.GATHER_RATE_DIVISOR), (70, 100000))
        self.assertEqual(cfgmod.from_dict({'GATHER_RATE_DIVISOR': 0}).GATHER_RATE_DIVISOR, 0)
        for bad in ({'REINFORCE_SUCCESS_PCT': 101}, {'REINFORCE_SUCCESS_PCT': -1},
                    {'GATHER_RATE_DIVISOR': -5}, {'REINFORCE_SUCCESS_PCT': 50.5}):
            with self.subTest(bad=bad), self.assertRaises(cfgmod.ConfigError):
                cfgmod.from_dict(bad)

    def test_the_shipped_config_file_matches_the_defaults(self):
        with open(os.path.join(HERE, 'config.json'), encoding='utf-8') as f:
            shipped = json.load(f)
        self.assertEqual((shipped['REINFORCE_SUCCESS_PCT'], shipped['GATHER_RATE_DIVISOR']), (70, 100000))


if __name__ == '__main__':
    unittest.main()
