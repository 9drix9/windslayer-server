#!/usr/bin/env python3
"""
test_inventory.py - the persistent bag, equipment grid and wallet
(item_inventory-model-persist, trade-economy-persist, shop_storage-wallet,
item_inventory-kind-slot-table, item_inventory-buff-and-gold-builders)

The model must mirror the client's own rules, so the assertions come from the decompiled
client and the live tests:

    item_inventory.md 1.2   stacks of 999 (type 0) / 99 (type 2), capacities 1..45
    item_inventory.md 1.3   id + exact 12-byte block, option words packed from w0, n <= 5
    FUN_00426680            Kind -> equip slot, cash Kinds -> slots 16..22, rings 13/14
    live T-S1D / T-S24      equipping removes exactly one bag copy; removal compacts
    live T-S3F              0x3F changes the gold label alone

No packet leaves this file except through packets.build, and no character record is read
from disk.
"""
import copy
import json
import logging
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import en_content as EC  # noqa: E402
import inventory as INV  # noqa: E402
import packets as P  # noqa: E402
import records as R  # noqa: E402
from wsproto import hexbytes  # noqa: E402

logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

STICK = 179       # Wooden Stick   Type 1, Kind 11 (weapon)
HERB = 5          # Herb           Type 0 (consume tab, stacks to 999)
ELEMENTIRIUM = 2975   # Type 2 (etc tab, stacks to 99)
SKILL_BOOK = 94   # Double Jump    Type 3: learned on receipt, never in a tab
KR_ONLY = 4356    # Random Cube Fragment: a KR id the EN client ignores
FEDORA = 1848     # Type 1 with Cash != 0
RING = 1360       # a Kind 17 ring (checked against the catalog in setUp)


def new_char(**extra):
    char = {'name': 'TestHero', 'gold': 1000, 'victy': 7}
    char.update(extra)
    INV.ensure(char)
    return char


class Normalizing(unittest.TestCase):
    """ensure() is the one normalizer: the store migration and every Inventory() run it,
    so a hand-edited or JSON-round-tripped record comes back in the canonical shape."""

    def test_creates_the_f4_shape(self):
        char = new_char()
        self.assertEqual(char['inventory'], {'equip': [], 'consume': {}, 'etc': {},
                                             'tab_slots': [35, 35, 35]})
        self.assertEqual(char['equipped'], {})

    def test_json_round_trip_restores_int_keys(self):
        char = new_char()
        bag = INV.Inventory(char)
        bag.add(HERB, 12)
        bag.add(STICK, 1, [7, 0, 0, 0, 0, 3])
        bag.equip(STICK, [7, 0, 0, 0, 0, 3])
        reloaded = json.loads(json.dumps(char))            # string keys, as accounts.json has
        INV.ensure(reloaded)
        self.assertEqual(reloaded['inventory'], char['inventory'])
        self.assertEqual(reloaded['equipped'], char['equipped'])
        self.assertEqual(INV.Inventory(reloaded).totals(), INV.Inventory(char).totals())

    def test_repairs_a_hand_edited_record(self):
        char = new_char(inventory={'equip': [STICK, {'id': HERB}], 'consume': [{'id': 5, 'qty': 3}],
                                   'etc': {'0': 9, '281': 4}, 'tab_slots': [99, 0, 35]},
                        equipped={'5': STICK, '99': 7, 'x': 1},
                        gold=-5, victy=2 ** 40)
        self.assertEqual([e['id'] for e in char['inventory']['equip']], [STICK, HERB])
        self.assertEqual(char['inventory']['consume'], {5: 3})
        self.assertEqual(char['inventory']['etc'], {281: 4})         # id 0 dropped
        self.assertEqual(char['inventory']['tab_slots'], [45, 1, 35])  # clamped to 1..45
        self.assertEqual(char['equipped'], {5: {'id': STICK, 'w': [0] * 6}})
        self.assertEqual((char['gold'], char['victy']), (0, INV.VICTY_MAX))

    def test_bad_record_type(self):
        with self.assertRaises(TypeError):
            INV.ensure(['not a character'])

    def test_a_second_ensure_does_not_orphan_a_live_model(self):
        """ensure() runs again on every Inventory()/Wallet(); if it replaced the dicts, an
        Inventory built earlier would keep writing into a record nobody saves."""
        char = new_char()
        bag = INV.Inventory(char)
        INV.Wallet(char)                                   # runs ensure() again
        INV.ensure(char)
        bag.add(HERB, 4)
        bag.equip(STICK)
        self.assertEqual(char['inventory']['consume'], {HERB: 4})
        self.assertEqual(char['equipped'][5]['id'], STICK)
        self.assertEqual(INV.Inventory(char).totals(), {HERB: 4})


class OptionBlocks(unittest.TestCase):
    """w0..w4 stay packed (the client stops counting at the first zero, FUN_004241d0) and
    w5 is the separate trailing word."""

    def test_packing(self):
        self.assertEqual(INV.pack_words([5, 0, 7, 0, 0, 9]), [5, 7, 0, 0, 0, 9])
        self.assertEqual(INV.pack_words([]), [0] * 6)
        self.assertEqual(INV.pack_words([1, 2, 3, 4, 5, 6, 7]), [1, 2, 3, 4, 5, 6])
        self.assertEqual(INV.wire_words([5, 0, 7, 0, 0, 9]), [5, 7])
        # An unpacked block is packed first, so no word is lost on the way out: the client
        # itself could never reach a word behind a zero gap.
        self.assertEqual(INV.wire_words([0, 1]), [1])
        self.assertEqual(INV.block_from_wire([2945, 2950], 7), [2945, 2950, 0, 0, 0, 7])
        self.assertEqual(INV.block_from_wire([], 0), [0] * 6)
        self.assertTrue(INV.is_zero_block([0, 0, 0, 0, 0, 0]))
        self.assertFalse(INV.is_zero_block([0, 0, 0, 0, 0, 1]))

    def test_matching_is_exact(self):
        self.assertTrue(INV.same_block([1, 0, 0, 0, 0, 0], [1]))
        self.assertFalse(INV.same_block([1, 0, 0, 0, 0, 0], [1, 0, 0, 0, 0, 2]))
        self.assertTrue(INV.same_block([0, 5], [5]))       # both pack to the same block


class BagRules(unittest.TestCase):
    def setUp(self):
        self.char = new_char()
        self.bag = INV.Inventory(self.char)

    def test_tabs_follow_the_client_type(self):
        self.assertEqual(self.bag.tab_of(STICK), 'equip')
        self.assertEqual(self.bag.tab_of(HERB), 'consume')
        self.assertEqual(self.bag.tab_of(ELEMENTIRIUM), 'etc')
        self.assertIsNone(self.bag.tab_of(SKILL_BOOK))     # Type 3: learned, never bagged
        self.assertIsNone(self.bag.tab_of(KR_ONLY))

    def test_add_and_remove(self):
        self.assertEqual(self.bag.add(HERB, 3), 3)
        self.assertEqual(self.bag.add(HERB, 2), 5)
        self.assertEqual(self.bag.count(HERB), 5)
        self.assertTrue(self.bag.has(HERB, 5))
        self.assertFalse(self.bag.has(HERB, 6))
        self.assertEqual(self.bag.remove(HERB, 2), 2)
        self.assertEqual(self.bag.count(HERB), 3)
        self.assertEqual(self.bag.remove(HERB, 99), 3)     # removes what is there
        self.assertEqual(self.bag.count(HERB), 0)
        self.assertNotIn(HERB, self.char['inventory']['consume'])

    def test_take_is_all_or_nothing(self):
        self.bag.add(HERB, 2)
        self.assertFalse(self.bag.take(HERB, 3))
        self.assertEqual(self.bag.count(HERB), 2)
        self.assertTrue(self.bag.take(HERB, 2))

    def test_ids_the_client_ignores_are_refused(self):
        for item in (KR_ONLY, 0, 99999, None, 'x'):
            with self.subTest(item=item):
                self.assertIsNone(self.bag.add(item, 1))
                self.assertIsNotNone(self.bag.fits(item, 1))
        # A skill book is a real EN item but belongs in no tab: the client learns it.
        self.assertIsNone(self.bag.add(SKILL_BOOK, 1))
        self.assertIn('consumes it on receipt', self.bag.fits(SKILL_BOOK, 1))
        self.assertEqual(self.bag.totals(), {})

    def test_stacks_split_at_the_client_limits(self):
        self.bag.add(HERB, 1200)                            # type 0: 999 per stack
        self.assertEqual([s['qty'] for s in self.bag.slots('consume')], [999, 201])
        self.assertEqual(self.bag.used_slots('consume'), 2)
        self.bag.add(ELEMENTIRIUM, 250)                      # type 2: 99 per stack
        self.assertEqual([s['qty'] for s in self.bag.slots('etc')], [99, 99, 52])

    def test_capacity_is_counted_in_slots(self):
        self.bag.set_capacity('etc', 2)
        self.assertEqual(self.bag.add(ELEMENTIRIUM, 198), 198)     # exactly 2 slots
        self.assertIsNone(self.bag.add(ELEMENTIRIUM, 1))           # would need a third
        self.assertIn('etc tab is full', self.bag.fits(ELEMENTIRIUM, 1))
        self.assertEqual(self.bag.count(ELEMENTIRIUM), 198)
        self.assertEqual(self.bag.free_slots('etc'), 0)
        self.bag.set_capacity('etc', 99)                           # clamped to 45
        self.assertEqual(self.bag.capacity('etc'), INV.MAX_CAPACITY)

    def test_equipment_does_not_stack_and_matches_by_block(self):
        plain, socketed = [0] * 6, [2945, 0, 0, 0, 0, 0]
        self.bag.add(STICK, 2)
        self.bag.add(STICK, 1, socketed)
        self.assertEqual(self.bag.count(STICK), 3)
        self.assertEqual(self.bag.count(STICK, plain), 2)
        self.assertEqual(self.bag.count(STICK, socketed), 1)
        self.assertEqual(self.bag.used_slots('equip'), 3)
        # Removing the socketed copy leaves the two plain ones, and the list compacts
        # (live T-S24: the later entries move down).
        self.assertEqual(self.bag.remove(STICK, 1, socketed), 1)
        self.assertEqual([e['w'] for e in self.bag.slots('equip')], [plain, plain])
        self.assertEqual(self.bag.instance(STICK)['w'], plain)
        self.assertIsNone(self.bag.instance(STICK, socketed))

    def test_totals_spans_every_tab(self):
        self.bag.add(HERB, 4)
        self.bag.add(ELEMENTIRIUM, 2)
        self.bag.add(STICK, 2)
        self.assertEqual(self.bag.totals(), {HERB: 4, ELEMENTIRIUM: 2, STICK: 2})


class EquipSlots(unittest.TestCase):
    """item_inventory-kind-slot-table: FUN_00426680 read out of the decompilation."""

    def setUp(self):
        self.char = new_char()
        self.bag = INV.Inventory(self.char)
        self.catalog = EC.items()

    def test_table_covers_every_equippable_en_kind(self):
        kinds = {d.kind for d in self.catalog.defs.values()
                 if d.type == EC.TYPE_EQUIPMENT and d.kind >= 0}
        self.assertTrue(kinds.issubset(set(INV.KIND_TO_SLOT)), sorted(kinds - set(INV.KIND_TO_SLOT)))
        # No two Kinds share a slot, and every slot is inside the 16 regular ones.
        slots = sorted(INV.KIND_TO_SLOT.values())
        self.assertEqual(len(slots), len(set(slots)))
        self.assertTrue(all(0 <= s < INV.EQUIP_SLOTS for s in slots))
        self.assertTrue(all(INV.EQUIP_SLOTS <= s <= INV.MAX_EQUIP_SLOT
                            for s in INV.KIND_TO_CASH_SLOT.values()))

    def test_known_slots(self):
        self.assertEqual(INV.KIND_TO_SLOT[11], 5)          # weapon, the one slot PySlayer had right
        self.assertEqual(INV.KIND_TO_SLOT[6], 4)           # shirt
        self.assertEqual(INV.KIND_TO_SLOT[5], 8)           # pants
        self.assertEqual(INV.KIND_TO_SLOT[9], 10)          # shoes
        self.assertEqual(INV.KIND_TO_SLOT[10], 0)          # hat
        # The two the old _KIND_TO_GRID put in slot 0 with the hat.
        self.assertEqual((INV.KIND_TO_SLOT[15], INV.KIND_TO_SLOT[16]), (11, 12))
        self.assertEqual(self.bag.slot_for(STICK), 5)
        self.assertIsNone(self.bag.slot_for(HERB))         # not equipment
        self.assertIsNone(self.bag.slot_for(KR_ONLY))

    def test_cash_items_use_the_costume_slots(self):
        fedora = self.catalog.get(FEDORA)
        self.assertTrue(fedora.is_cash)
        self.assertEqual(self.bag.slot_for(FEDORA), INV.KIND_TO_CASH_SLOT[fedora.kind])
        self.assertGreaterEqual(self.bag.slot_for(FEDORA), INV.EQUIP_SLOTS)

    def test_equip_swaps_and_returns_the_old_item(self):
        slot, displaced = self.bag.equip(STICK, [0] * 6)
        self.assertEqual((slot, displaced), (5, None))
        self.assertEqual(self.char['equipped'][5], {'id': STICK, 'w': [0] * 6})
        slot, displaced = self.bag.equip(STICK, [2945, 0, 0, 0, 0, 0])
        self.assertEqual(slot, 5)
        self.assertEqual(displaced, {'id': STICK, 'w': [0] * 6})
        self.assertEqual(self.bag.grid_ids()[5], STICK)

    def test_unequip(self):
        self.bag.equip(STICK, [0] * 6)
        self.assertEqual(self.bag.unequip_item(STICK)[0], 5)
        self.assertEqual(self.char['equipped'], {})
        self.assertEqual(self.bag.unequip_item(STICK), (None, None))
        self.assertIsNone(self.bag.unequip(5))
        self.assertEqual(self.bag.grid_ids(), [0] * 25)

    def test_rings_use_two_slots(self):
        """FUN_00426680 case 0x11: the ring in slot 13 moves to 14, and only a ring that was
        already in 14 while 13 was occupied comes back to the bag."""
        rings = [i for i, d in self.catalog.defs.items()
                 if d.kind == INV.RING_KIND and not d.is_cash]
        self.assertTrue(rings)
        a, b, c = rings[0], rings[1], rings[2]
        self.assertEqual(self.bag.equip(a), (13, None))
        self.assertEqual(self.bag.equip(b), (13, None))             # a shifts into 14
        self.assertEqual(self.char['equipped'][14]['id'], a)
        self.assertEqual(self.char['equipped'][13]['id'], b)
        slot, displaced = self.bag.equip(c)                          # now one must come back
        self.assertEqual((slot, displaced['id']), (13, a))
        self.assertEqual(self.char['equipped'][14]['id'], b)

    def test_wear_follows_the_client_order_and_needs_the_bag_instance(self):
        """Live item_inventory#05: the requested entry leaves the bag and the tab compacts
        FIRST, then the displaced one is appended - so a full tab still has the one slot the
        removal just freed, and the 0x03 list order matches what the client shows."""
        self.assertEqual(self.bag.wear(STICK), (None, None))         # nothing in the bag
        self.bag.set_capacity('equip', 2)
        self.bag.add(STICK, 1, [0] * 6)
        self.bag.add(STICK, 1, [2945, 0, 0, 0, 0, 0])
        self.assertEqual(self.bag.free_slots('equip'), 0)            # full
        slot, displaced = self.bag.wear(STICK, [0] * 6)
        self.assertEqual((slot, displaced), (5, None))
        slot, displaced = self.bag.wear(STICK, [2945, 0, 0, 0, 0, 0])
        self.assertEqual((slot, displaced['w']), (5, [0] * 6))
        self.assertEqual(self.bag.data['equip'], [{'id': STICK, 'w': [0] * 6}])
        # the stored look is composed as the 0x1D handler composes it: the stick in the hand
        self.assertEqual(self.char['look'][INV.LAYER_WEAPON], self.catalog.get(STICK).spr_num)

    def test_take_off_returns_the_instance_and_refuses_a_full_bag(self):
        self.bag.add(STICK, 1, [0] * 6)
        self.bag.wear(STICK, [0] * 6)
        self.bag.set_capacity('equip', 1)
        self.bag.add(STICK, 1, [2945, 0, 0, 0, 0, 0])                # the one free slot
        self.assertEqual(self.bag.take_off(STICK, [0] * 6), (None, None))
        self.assertEqual(self.bag.worn(STICK, [0] * 6)[0], 5)        # still worn
        self.bag.set_capacity('equip', 2)
        slot, entry = self.bag.take_off(STICK, [0] * 6)
        self.assertEqual((slot, entry['w']), (5, [0] * 6))
        self.assertEqual(self.bag.count(STICK), 2)
        self.assertEqual(self.char['look'][INV.LAYER_WEAPON], 0)     # empty hand (0x1E, Spr 0)
        self.assertEqual(self.bag.take_off(STICK, [7, 0, 0, 0, 0, 0]), (None, None))

    def test_the_wearer_gates_mirror_the_client(self):
        """inventory.equip_refusal = the three gates of FUN_0044C2C0 (spec 0x44C481/0x0F):
        hii Gender against the account flag, hii Lv against the level, hii Job against the
        class with Job[0] as the any-class flag."""
        get = self.catalog.get
        self.assertIsNone(INV.equip_refusal(get(STICK), level=1, class_id=0, gender_flag=0))
        self.assertIn('hii Lv 7', INV.equip_refusal(get(9), level=6))       # Lv 7 weapon
        self.assertIsNone(INV.equip_refusal(get(9), level=7))
        male_only, female_only = get(14), get(19)                           # hii Gender 1 / 2
        self.assertEqual((male_only.gender, female_only.gender), (1, 2))
        self.assertIn('gender', INV.equip_refusal(male_only, gender_flag=0))
        self.assertIsNone(INV.equip_refusal(male_only, gender_flag=1))
        self.assertIn('gender', INV.equip_refusal(female_only, gender_flag=1))
        self.assertIsNone(INV.equip_refusal(female_only, gender_flag=0))
        class3 = get(11)                                                    # Job[3] only, Lv 10
        self.assertEqual(class3.job, [0, 0, 0, 1, 0, 0, 0])
        self.assertIn('excludes class 0', INV.equip_refusal(class3, level=10, class_id=0))
        self.assertIsNone(INV.equip_refusal(class3, level=10, class_id=3))
        # A class the 7-flag row does not have is allowed: the client reads past the row,
        # and a wrong refusal would strand an item the player can really wear.
        self.assertIsNone(INV.equip_refusal(class3, level=10, class_id=99))
        self.assertIsNotNone(INV.equip_refusal(None))

    def test_equipped_grid_reaches_the_0x07_record(self):
        """records.equip_grid reads the persisted shape, so the worn item is in the next
        spawn packet (which is what makes it show in the Equipment window after a portal)."""
        self.bag.equip(STICK, [2945, 0, 0, 0, 0, 7])
        rows = R.equip_grid({}, self.char)
        self.assertEqual(rows[5]['equip_item_id'], STICK)
        words = [w['equip_item_attr'] for w in rows[5]['repeat[6]']]
        self.assertEqual(words, [2945, 0, 0, 0, 0, 7])
        self.assertEqual(rows[0]['equip_item_id'], 0)


class WalletRules(unittest.TestCase):
    def setUp(self):
        self.char = new_char(gold=500, victy=10)
        self.wallet = INV.Wallet(self.char)

    def test_reads_and_writes_the_record(self):
        self.assertEqual(self.wallet.as_tuple(), (500, 10))
        self.wallet.gold += 250
        self.assertEqual(self.char['gold'], 750)
        self.wallet.victy = 3
        self.assertEqual(self.char['victy'], 3)

    def test_clamped_to_the_wire_types(self):
        self.wallet.gold = -1
        self.assertEqual(self.wallet.gold, 0)
        self.wallet.gold = 2 ** 70
        self.assertEqual(self.wallet.gold, INV.GOLD_MAX)
        self.wallet.victy = 2 ** 40
        self.assertEqual(self.wallet.victy, INV.VICTY_MAX)

    def test_pay_is_all_or_nothing(self):
        self.assertFalse(self.wallet.pay(501))
        self.assertEqual(self.wallet.as_tuple(), (500, 10))
        self.assertFalse(self.wallet.pay(100, 11))
        self.assertEqual(self.wallet.as_tuple(), (500, 10))
        self.assertTrue(self.wallet.pay(100, 10))
        self.assertEqual(self.wallet.as_tuple(), (400, 0))

    def test_earn(self):
        self.wallet.earn(100, 5)
        self.assertEqual(self.wallet.as_tuple(), (600, 15))


class WireAdapters(unittest.TestCase):
    """item_inventory-buff-and-gold-builders: one adapter per opcode, so the model's
    (id, w[6]) never has to know each grammar's field names."""

    def test_block_fields_per_opcode(self):
        words = [2945, 2950, 0, 0, 0, 7]
        self.assertEqual(INV.block_fields('0x1D', words),
                         {'stone_count': 2,
                          'repeat[stone_count]': [{'stone_id': 2945}, {'stone_id': 2950}],
                          'block_tail': 7})
        self.assertEqual(INV.block_fields('0x1E', words)['option_count'], 2)
        self.assertEqual(INV.block_fields('0x23', words)['opt_extra'], 7)
        # Ground-item records zero their buffer once per packet, so they always send 5.
        self.assertEqual(INV.block_fields('0x12', [1, 0, 0, 0, 0, 0])['opt_count'], 5)
        with self.assertRaises(KeyError):
            INV.block_fields('0x99', words)

    def test_an_echo_keeps_the_wire_order_and_count(self):
        """What the client sent comes back byte for byte: it removes an item only on an
        exact 12-byte match, and the STORED form packs the words toward w0, so the echo
        of n=2 [0, 7] used to be n=1 [7] - a descriptor the client owns nothing of."""
        echo = INV.echo_block_fields('0x19', [0, 7], 9)
        self.assertEqual(echo, {'opt_count': 2, 'repeat[opt_count]': [{'opt': 0}, {'opt': 7}],
                                'opt_extra': 9})
        self.assertEqual(INV.block_fields('0x19', INV.block_from_wire([0, 7], 9)),
                         {'opt_count': 1, 'repeat[opt_count]': [{'opt': 7}], 'opt_extra': 9})
        fields = INV.item_fields('0x19', 3, count=1, block=echo)
        self.assertEqual(P.build('0x19', dict(fields, gold=0))[8:],
                         hexbytes('03 00 01 00 02 00 00 07 00 09 00'))
        with self.assertRaises(KeyError):
            INV.echo_block_fields('0x99', [0, 7], 9)

    def test_item_fields_build_the_live_packets(self):
        """0x1D {uid 1, item 179, no stones} is the 9-byte packet the live equip produced."""
        fields = INV.item_fields('0x1D', STICK, [0] * 6, uid=1)
        self.assertEqual(P.build('0x1D', fields), hexbytes('01 00 00 00 B3 00 00 00 00'))
        sell = INV.item_fields('0x19', 3, [0] * 6, count=300)
        self.assertEqual(P.build('0x19', dict(sell, gold=1234))[8:], hexbytes('03 00 2C 01 00 00 00'))

    def test_a_block_over_five_words_can_never_be_built(self):
        with self.assertRaises(P.PacketError):
            P.build('0x1D', {'uid': 1, 'item_id': STICK, 'stone_count': 6,
                             'repeat[stone_count]': [{'stone_id': 1}] * 6, 'block_tail': 0})

    def test_currency_and_buff_builders(self):
        wallet = INV.Wallet(new_char(gold=123456, victy=777))
        self.assertEqual(INV.gold_fields(wallet), {'gold': 123456})
        self.assertEqual(len(P.build('0x3F', INV.gold_fields(wallet))), 8)
        self.assertEqual(INV.currency_fields(wallet, HERB, 3),
                         {'gold': 123456, 'victy': 777, 'item_id': HERB, 'count': 3})
        self.assertEqual(INV.currency_fields(wallet), {'gold': 123456, 'victy': 777,
                                                       'item_id': 0, 'count': 0})
        self.assertEqual(len(P.build('0x18', INV.currency_fields(wallet))), 16)
        buff = INV.item_buff_fields(1, 148)
        self.assertEqual(buff, {'target_uid': 1, 'source_uid': 0, 'item_id': 148})
        self.assertEqual(len(P.build('0x42', buff)), 10)
        self.assertEqual(P.build('0x41', buff), P.build('0x42', buff))


class StoreRoundTrip(unittest.TestCase):
    """What the P2 stage promises: the whole item state survives a save/load cycle of the
    character record (this is the offline half of "it is still there after a relog")."""

    def test_bag_wallet_and_grid_survive_json(self):
        char = new_char(gold=100000, victy=0)
        bag, wallet = INV.Inventory(char), INV.Wallet(char)
        bag.add(HERB, 1200)
        bag.add(ELEMENTIRIUM, 5)
        bag.add(STICK, 2)
        bag.equip(STICK, [2945, 0, 0, 0, 0, 7])
        bag.remove(STICK, 1)
        wallet.pay(250)
        before = copy.deepcopy(char)

        restored = json.loads(json.dumps(char))
        INV.ensure(restored)
        self.assertEqual(restored, before)
        bag2 = INV.Inventory(restored)
        self.assertEqual(bag2.totals(), {HERB: 1200, ELEMENTIRIUM: 5, STICK: 1})
        self.assertEqual([s['qty'] for s in bag2.slots('consume')], [999, 201])
        self.assertEqual(bag2.equipped(5), {'id': STICK, 'w': [2945, 0, 0, 0, 0, 7]})
        self.assertEqual(INV.Wallet(restored).as_tuple(), (100000 - 250, 0))


if __name__ == '__main__':
    unittest.main()
