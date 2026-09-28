#!/usr/bin/env python3
"""
test_items.py - equip, unequip and consumable use over the real handler loop
(item_inventory-equip, item_inventory-unequip, item_inventory-use-consumable; P2 stage 3)

Every test drives GameServer._handle_fireway through fakeclient (no port, no game client,
a temp accounts.json). What it pins, and why each case exists:

    F3 equip      exactly ONE S2C 0x1D, the request's 12-byte block echoed byte for byte,
                  the bag entry gone, the displaced item back in the bag in the client's own
                  order, and the stored look composed as the client's 0x1D composes it
                  (the weapon's Spr_Num in word 11) so the hand still holds it after the
                  next 0x07 (P2 exit criteria 1 and 6)
    F4 unequip    S2C 0x1E, the instance back in the bag with the same block, the grid slot
                  and look word 11 cleared, and the item equippable again (the drag-back
                  of exit criterion 6). Idempotent against the double-click that repeats it.
    F5 use        S2C 0x25 alone (the client removes the unit and applies HP/MP itself),
                  the stack -1, the cooldown that makes spamming do nothing (exit criterion
                  5), and S2C 0x42 for an item with a hii `Con` duration.

Failure branches: not owned (S2C 0x23 takes the client's phantom copy back), a block that
does not match the stored one, level / job / gender gates, a full bag, an unknown id, a
block the wire cannot carry, and a use inside the cooldown.

The live captures these replay: item_inventory#16 (C2S 0x0F `B3 00 00 00 00`),
item_inventory#17 (C2S 0x11, the identical 5 bytes), item_inventory#20 (C2S 0x15 `05 00`
twice inside the 4000 ms CT), item_inventory#03/#04/#05 (what 0x1D / 0x1E / 0x24 do to the
client bag and grid).
"""
import hashlib
import logging
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import en_content as EC  # noqa: E402
import fakeclient as F  # noqa: E402
import inventory as INV  # noqa: E402
import progression  # noqa: E402
from wsproto import hexbytes  # noqa: E402

W = F.import_server()
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
CAP_0F_EQUIP_STICK = hexbytes('B3 00 00 00 00')   # live item_inventory#16: item 179, n 0, tail 0
CAP_11_UNEQUIP_STICK = hexbytes('B3 00 00 00 00')  # live item_inventory#17: the same 5 bytes
CAP_15_USE_HERB = hexbytes('05 00')               # live item_inventory#20

STICK = 179          # Wooden Stick: Type 1, Kind 11 -> grid slot 5, Lv 1, any class, Spr_Num 1
SWORD_LV7 = 9        # Kind 11, Lv 7, any class      -> the level gate
SWORD_CLASS3 = 11    # Kind 11, Lv 10, Job[3] only   -> the job gate
SHIRT_MALE = 14      # Kind 6, Lv 0, hii Gender 1    -> the gender gate (test's flag is 0)
HERB = 5             # Type 0, HP +20, CT 4000
MUSHROOM = 3         # Type 0, MP +20, CT 3000
RELAX_HERB = 148     # Type 0, HP/MP 0, Con 10000 -> the 0x42 buff
ETC_ITEM = 2975      # Type 2 (Chipped Elementirium)
WEAPON_SLOT = INV.WEAPON_SLOT
_LIVE_HASH = None


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def setUpModule():
    global _LIVE_HASH
    _LIVE_HASH = _sha(LIVE_ACCOUNTS) if os.path.exists(LIVE_ACCOUNTS) else None


def tearDownModule():
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_items.py (tests must only use temp copies)'


class ItemTest(unittest.TestCase):
    """A fresh GameServer on a temp accounts.json, one client, in world on map 101."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_items_')
        self.server = F.make_server(self.tmp)
        self.clients = []
        self.c = self.enter()

    def tearDown(self):
        for c in self.clients:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- flows ---
    def enter(self, name='TestHero', user='test', password='test'):
        c = F.FakeClient(self.server)
        self.clients.append(c)
        c.send_c2s('0x44D8BF/0x01', {'account_id': user, 'password': password})
        self.assertEqual(F.FakeClient.decode(c.expect(0x02))['result'], 1)
        c.send_c2s('0x42F904/0x2B', {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907,
                                     'char_name': name})
        c.expect(0x03, 0x07, 0x15, 0x28, 0x44)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world')))
        return c

    def char(self, name='TestHero', user='test'):
        return self.server.store.find_character(user, name)

    def bag(self):
        return INV.Inventory(self.char())

    def give(self, item, count=1, words=None):
        self.assertIsNotNone(self.server._inv_add(self.c.session, item, count, 'test', words=words),
                             f'fixture: item {item} was refused by the bag model')

    def equip(self, item=STICK, stones=(), extra=0):
        self.c.send_c2s('0x44C481/0x0F', {'item_id': item, 'stone_count': len(stones),
                                          'extra_option': extra,
                                          'repeat[stone_count]': [{'stone_id': s} for s in stones]})

    def unequip(self, item=STICK, stones=(), extra=0):
        self.c.send_c2s('0x46CE18/0x11', {'item_id': item, 'enchant_count': len(stones),
                                          'enchant_last': extra,
                                          'repeat[enchant_count]': [{'enchant': s} for s in stones]})

    def use(self, item=HERB):
        self.c.send_c2s('0x44C2B3/0x15', {'item_id': item})

    # --------------------------------------------------------------- decode ---
    @staticmethod
    def rec(pkt):
        return F.FakeClient.decode(pkt)

    def equip_ids(self):
        return [e['id'] for e in self.bag().data['equip']]


# ============================================================= F3: equip ===
class Equip(ItemTest):
    """C2S 0x0F -> S2C 0x1D (item_inventory-equip, F3)."""

    def test_one_reply_moves_the_item_and_dresses_the_character(self):
        self.give(STICK)
        self.equip()
        pkt = self.c.expect(0x1D)                       # and nothing else: never a 0x23 (B1)
        self.assertEqual(pkt.payload, hexbytes('01 00 00 00 B3 00 00 00 00'))   # the live 9 B
        char = self.char()
        bag = INV.Inventory(char)
        self.assertEqual(bag.equipped(WEAPON_SLOT), {'id': STICK, 'w': [0] * 6})
        self.assertEqual(bag.count(STICK), 0)
        # Look word 11 is what renders the weapon in hand after the next 0x07.
        self.assertEqual(char['look'][INV.LAYER_WEAPON], EC.items().get(STICK).spr_num)

    def test_the_worn_weapon_is_still_in_hand_after_a_portal(self):
        """P2 exit criteria 1 and 6: the 0x07 of the next map carries the sprite and the
        grid slot, because both are read back from the character record."""
        self.give(STICK)
        self.equip()
        self.c.expect(0x1D)
        self.c.send_c2s('0x42F76B/0x7E', {'portal_line_index': 23})    # 101 -> 102
        spawn = self.c.expect(0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(8))[2]
        me = self.rec(spawn)['repeat[player_count]'][0]
        self.assertEqual(me['repeat[14]'][11]['appearance_part'], EC.items().get(STICK).spr_num)
        self.assertEqual(me['repeat[16]'][WEAPON_SLOT]['equip_item_id'], STICK)

    def test_the_block_is_echoed_verbatim_and_matched_exactly(self):
        """The client finds the bag entry by a memcmp of the 12 bytes it sent, so the reply
        repeats them unchanged (a packed rebuild matches nothing: S1-16)."""
        self.give(STICK, 1, words=[2945, 2950, 0, 0, 0, 7])
        self.equip(stones=(2945, 2950), extra=7)
        rec = self.rec(self.c.expect(0x1D))
        self.assertEqual([e['stone_id'] for e in rec['repeat[stone_count]']], [2945, 2950])
        self.assertEqual(rec['block_tail'], 7)
        self.assertEqual(self.bag().equipped(WEAPON_SLOT)['w'], [2945, 2950, 0, 0, 0, 7])

    def test_swapping_returns_the_old_item_in_the_client_order(self):
        """Live item_inventory#05: 0x1D removes the requested bag entry, the tab compacts,
        and only then does the previously worn item land in the first empty slot - so it is
        the LAST entry, which is the order the next 0x03 must list."""
        self.give(STICK)                                        # zero block
        self.give(STICK, 1, words=[2945, 0, 0, 0, 0, 0])        # socketed
        self.give(ETC_ITEM, 1)                                  # another tab: never touched
        self.equip()                                            # wear the zero-block one
        self.c.expect(0x1D)
        self.equip(stones=(2945,))                              # wear the socketed one
        self.c.expect(0x1D)
        bag = self.bag()
        self.assertEqual(bag.equipped(WEAPON_SLOT), {'id': STICK, 'w': [2945, 0, 0, 0, 0, 0]})
        self.assertEqual([e['w'] for e in bag.data['equip']], [[0] * 6])   # the old one is back
        self.assertEqual(bag.count(ETC_ITEM), 1)

    def test_an_item_the_bag_does_not_have_is_taken_off_the_client(self):
        """F3 step 3: 0x0F only comes from a bag double-click, so a request the model cannot
        match means the client is showing a phantom (a GM-injected item). S2C 0x23 removes
        it with the same 12 bytes; nothing is equipped."""
        with self.assertLogs('WS', logging.WARNING) as cm:
            self.equip()
            pkt = self.c.expect(0x23)
        self.assertEqual(self.rec(pkt), {'item_id': STICK, 'count': 1, 'opt_count': 0,
                                         'repeat[opt_count]': [], 'opt_extra': 0})
        self.assertEqual(self.bag().equipped_items(), {})
        self.assertIn('S2C 0x23 removes the client copy', '\n'.join(cm.output))

    def test_a_block_that_does_not_match_the_stored_one_is_a_phantom_too(self):
        """The bag holds the item but not that instance: equipping it would wear an item the
        model never had and leave the real one behind."""
        self.give(STICK)
        with self.assertLogs('WS', logging.WARNING):
            self.equip(stones=(2945,))
            pkt = self.c.expect(0x23)
        rec = self.rec(pkt)
        self.assertEqual([e['opt'] for e in rec['repeat[opt_count]']], [2945])
        self.assertEqual(self.bag().count(STICK), 1)             # the real stick is untouched
        self.assertEqual(self.bag().equipped_items(), {})

    def test_the_level_job_and_gender_gates_refuse_in_silence(self):
        """All three are client-side gates (spec 0x44C481/0x0F), so a request that fails one
        is forged: nothing is pending client-side, so nothing is sent back."""
        char = self.char()
        for item, expect in ((SWORD_LV7, 'hii Lv 7'),               # Lv 7 vs level 1
                             (SHIRT_MALE, 'gender')):              # hii Gender 1, account flag 0
            with self.subTest(item=item):
                self.give(item)
                with self.assertLogs('WS', logging.WARNING) as cm:
                    self.equip(item)
                    self.c.expect_silence()
                self.assertIn(expect, '\n'.join(cm.output))
                self.assertEqual(self.bag().count(item), 1)        # still in the bag
        # The job gate needs a character past the item's level, or the level gate fires first.
        char['exp'] = progression.exp_for_level(10)
        self.server.store.mark_dirty('test')
        self.give(SWORD_CLASS3)
        with self.assertLogs('WS', logging.WARNING) as cm:
            self.equip(SWORD_CLASS3)
            self.c.expect_silence()
        self.assertIn('excludes class 0', '\n'.join(cm.output))
        self.assertEqual(self.bag().equipped_items(), {})

    def test_the_same_item_on_a_character_that_passes_the_gate(self):
        """The positive control for the gates above: level 7 and the Job[3] weapon on a
        class-3 character both go through."""
        char = self.char()
        char.update({'exp': progression.exp_for_level(10), 'class': 3})
        self.server.store.mark_dirty('test')
        self.give(SWORD_CLASS3)
        self.equip(SWORD_CLASS3)
        self.c.expect(0x1D)
        self.assertEqual(self.bag().equipped(WEAPON_SLOT)['id'], SWORD_CLASS3)

    def test_unknown_ids_wrong_types_and_overlong_blocks_are_dropped(self):
        with self.assertLogs('WS', logging.WARNING) as cm:
            self.equip(4356)                                      # KR-only id
            self.equip(HERB)                                      # Type 0
            self.c.send_c2s('0x44C481/0x0F', {'item_id': STICK, 'stone_count': 6,
                                              'repeat[stone_count]': [{'stone_id': 1}] * 6})
            self.c.expect_silence()
        text = '\n'.join(cm.output)
        self.assertIn('item=4356 is not an EN client item', text)
        self.assertIn('6 stones refused', text)
        self.assertEqual(self.bag().equipped_items(), {})


# =========================================================== F4: unequip ===
class Unequip(ItemTest):
    """C2S 0x11 -> S2C 0x1E (item_inventory-unequip, F4; B9: there was no route at all)."""

    def wear_stick(self, stones=(), extra=0):
        self.give(STICK, 1, words=INV.block_from_wire(stones, extra))
        self.equip(stones=stones, extra=extra)
        self.c.expect(0x1D)

    def test_the_item_comes_back_to_the_bag_and_the_hand_is_empty(self):
        self.wear_stick()
        self.c.send(0x11, CAP_11_UNEQUIP_STICK)                  # the live 5 B capture
        pkt = self.c.expect(0x1E)
        self.assertEqual(pkt.payload, hexbytes('01 00 00 00 B3 00 00 00 00'))
        char = self.char()
        bag = INV.Inventory(char)
        self.assertEqual(bag.equipped_items(), {})
        self.assertEqual([e['id'] for e in bag.data['equip']], [STICK])
        self.assertEqual(char['look'][INV.LAYER_WEAPON], 0)      # empty hand on the next 0x07

    def test_dragging_it_back_works(self):
        """P2 exit criterion 6: the returned instance is the same instance, so the equip that
        follows matches it again."""
        self.wear_stick(stones=(2945,), extra=7)
        self.unequip(stones=(2945,), extra=7)
        self.c.expect(0x1E)
        self.assertEqual(self.bag().data['equip'], [{'id': STICK, 'w': [2945, 0, 0, 0, 0, 7]}])
        self.equip(stones=(2945,), extra=7)
        self.c.expect(0x1D)
        self.assertEqual(self.bag().equipped(WEAPON_SLOT)['w'], [2945, 0, 0, 0, 0, 7])

    def test_the_echo_is_the_block_the_grid_holds(self):
        self.wear_stick(stones=(2945, 2950), extra=7)
        self.unequip(stones=(2945, 2950), extra=7)
        rec = self.rec(self.c.expect(0x1E))
        self.assertEqual([e['option_value'] for e in rec['repeat[option_count]']], [2945, 2950])
        self.assertEqual(rec['option_extra'], 7)

    def test_a_second_double_click_does_nothing(self):
        """The client re-sends 0x11 on every double-click and waits for nothing, so the
        repeat must not add a second copy to the bag."""
        self.wear_stick()
        self.c.send(0x11, CAP_11_UNEQUIP_STICK)
        self.c.expect(0x1E)
        with self.assertLogs('WS', logging.INFO) as cm:
            self.c.send(0x11, CAP_11_UNEQUIP_STICK)
            self.c.expect_silence()
        self.assertIn('is not equipped - ignoring', '\n'.join(cm.output))
        self.assertEqual(self.equip_ids(), [STICK])

    def test_a_block_the_grid_never_held_is_refused(self):
        """Spec 0x1E duplication hazard, proven live (item_inventory#04b): the client ignores
        the grid-removal result, so a mismatched block leaves the item worn AND adds a bag
        copy. The server must never send that packet."""
        self.wear_stick()
        with self.assertLogs('WS', logging.INFO) as cm:
            self.unequip(stones=(2945,))
            self.c.expect_silence()
        self.assertIn('is not equipped', '\n'.join(cm.output))
        self.assertEqual(self.bag().equipped(WEAPON_SLOT)['id'], STICK)
        self.assertEqual(self.equip_ids(), [])

    def test_a_full_equipment_tab_keeps_the_item_on(self):
        """The client refuses to send ("You have no more slots for the item.") and 0x1E into
        a full bag drops the item silently, so the request is dropped instead."""
        self.wear_stick()
        bag = self.bag()
        bag.set_capacity('equip', 1)
        self.give(STICK)                                         # fills the one slot
        self.server.store.mark_dirty('test')
        with self.assertLogs('WS', logging.INFO) as cm:
            self.c.send(0x11, CAP_11_UNEQUIP_STICK)
            self.c.expect_silence()
        self.assertIn('equip tab is full', '\n'.join(cm.output))
        self.assertEqual(self.bag().equipped(WEAPON_SLOT)['id'], STICK)

    def test_unknown_ids_wrong_types_and_overlong_blocks_are_dropped(self):
        self.wear_stick()
        with self.assertLogs('WS', logging.WARNING) as cm:
            self.unequip(4356)
            self.unequip(HERB)
            self.c.send_c2s('0x46CE18/0x11', {'item_id': STICK, 'enchant_count': 6,
                                              'repeat[enchant_count]': [{'enchant': 1}] * 6})
            self.c.expect_silence()
        text = '\n'.join(cm.output)
        self.assertIn('item=4356 is not an EN client item', text)
        self.assertIn('6 enchant words refused', text)
        self.assertEqual(self.bag().equipped(WEAPON_SLOT)['id'], STICK)


# ======================================================= F5: use consumable ===
class UseConsumable(ItemTest):
    """C2S 0x15 (Type-0 branch) -> S2C 0x25 (item_inventory-use-consumable, F5; B10)."""

    def hurt(self, hp=60, mp=20):
        with self.server._combat_lock(self.c.session):
            self.c.session['hp'], self.c.session['mp'] = hp, mp
        return hp, mp

    def test_one_0x25_decrements_the_stack_and_heals(self):
        """S2C 0x25 IS the use: the client removes the unit and adds the hii HP itself, so
        nothing else may follow it (an 0x28 before it double-applies, spec 0x25 hazard 5)."""
        self.give(HERB, 3)
        hp, _ = self.hurt()
        self.c.send(0x15, CAP_15_USE_HERB)                       # the live 2 B capture
        pkt = self.c.expect(0x25)                                # no 0x28, no 0x44, no 0x23
        self.assertEqual(pkt.payload, hexbytes('05 00'))
        self.assertEqual(self.c.session['hp'], hp + EC.items().get(HERB).hp)
        self.assertEqual(self.bag().count(HERB), 2)
        # The stack is in the character record, so a relog or portal shows the same two.
        self.assertEqual(self.char()['inventory']['consume'][HERB], 2)

    def test_spamming_inside_the_cooldown_does_nothing(self):
        """P2 exit criterion 5. The client itself has no lock while the cooldown is only
        client-side (live item_inventory#20: two 0x15 3.8 s apart inside the 4000 ms CT), and
        a 0x25 inside the client's own cooldown is dropped by the client with no removal, so
        a second decrement here would desync the bag until the next portal."""
        self.give(HERB, 3)
        self.hurt()
        self.use()
        self.c.expect(0x25)
        with self.assertLogs('WS', logging.INFO) as cm:
            self.use()
            self.use()
            self.c.expect_silence()
        self.assertEqual(sum('of the 4000 ms cooldown left' in line for line in cm.output), 2)
        self.assertEqual(self.bag().count(HERB), 2)

    def test_the_cooldown_expires(self):
        self.give(HERB, 2)
        self.hurt()
        self.use()
        self.c.expect(0x25)
        # Rewind the stamp past the item's CT instead of sleeping 4 s.
        self.c.session[W.GameServer.ITEM_COOLDOWN_KEY][HERB] -= EC.items().get(HERB).ct / 1000.0 + 0.1
        self.use()
        self.c.expect(0x25)
        self.assertEqual(self.bag().count(HERB), 0)

    def test_the_cooldown_is_per_item(self):
        """Different ids have their own stamps (item_inventory.md 3.3), so a potion does not
        block a different one."""
        self.give(HERB, 1)
        self.give(MUSHROOM, 1)
        self.hurt()
        self.use(HERB)
        self.c.expect(0x25)
        self.use(MUSHROOM)
        self.c.expect(0x25)
        self.assertEqual(self.c.session['mp'], 20 + EC.items().get(MUSHROOM).mp)

    def test_hp_and_mp_are_clamped_to_the_maximum(self):
        """The clamp is the client's own maximum (cs-hp-mp-model: TestHero Lv1 SPR 3 = 140),
        not the stored current HP: the old `session.get('max_hp', char['hp'])` capped this
        herb at the 100 the record held, so the heal above it was lost (P2 finding)."""
        self.give(HERB, 1)
        self.hurt(hp=135)
        self.use()
        self.c.expect(0x25)
        self.assertEqual(self.c.session['max_hp'], 140)
        self.assertEqual(self.c.session['hp'], 140)

    def test_a_heal_above_the_stored_hp_is_kept(self):
        """The live P2 bug: the record said 100, so the model's maximum was 100 and a
        herb at 100 HP healed nothing server-side while the client showed 120/140."""
        self.give(HERB, 1)
        self.hurt(hp=100)
        self.use()
        self.c.expect(0x25)
        self.assertEqual(self.c.session['hp'], 100 + EC.items().get(HERB).hp)

    def test_an_item_the_bag_does_not_have_is_taken_off_the_client(self):
        """F5 step 2: the client is showing a stack the model does not have (an injected
        item). 0x23 removes one unit and a [Warning] line says why."""
        with self.assertLogs('WS', logging.WARNING):
            self.use()
            remove, notice = self.c.expect(0x23, 0x15)
        self.assertEqual(self.rec(remove), {'item_id': HERB, 'count': 1, 'opt_count': 0,
                                            'repeat[opt_count]': [], 'opt_extra': 0})
        self.assertEqual(self.rec(notice)['msg_type'], 2)        # [Warning]
        self.assertEqual(self.bag().count(HERB), 0)

    def test_an_item_with_a_duration_also_gets_its_buff(self):
        """F5 step 6, proven live (item_inventory#10): item 148 has hii Con 10000, and the
        client's buff record counts down from exactly that."""
        self.give(RELAX_HERB, 1)
        self.use(RELAX_HERB)
        use, buff = self.c.expect(0x25, 0x42)
        self.assertEqual(self.rec(use), {'item_or_skill_id': RELAX_HERB})
        self.assertEqual(self.rec(buff), {'target_uid': 1, 'source_uid': 0,
                                          'item_id': RELAX_HERB})

    def test_equipment_and_unknown_ids_are_not_consumable(self):
        self.give(STICK, 1)
        self.give(ETC_ITEM, 1)
        with self.assertLogs('WS', logging.INFO) as cm:
            self.use(STICK)                                      # Type 1: equip is 0x0F
            self.use(ETC_ITEM)                                   # Type 2
            self.use(4356)                                       # not in the EN catalog
            self.c.expect_silence()
        text = '\n'.join(cm.output)
        self.assertIn('not consumable', text)
        self.assertIn('is not an EN client item', text)
        self.assertEqual(self.bag().count(STICK), 1)

    def test_a_use_survives_the_portal_that_rebuilds_the_bag(self):
        """P2 exit criterion 1: the potion is gone from the 0x03 the next map load sends,
        because the decrement went into the record and not onto the session."""
        self.give(HERB, 2)
        self.hurt()
        self.use()
        self.c.expect(0x25)
        self.c.send_c2s('0x42F76B/0x7E', {'portal_line_index': 23})
        state = self.c.expect(0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(8))[1]
        rows = F.FakeClient.decode(state, allow_trailing=True)['repeat[consume_item_count]']
        self.assertEqual([(r['item_id'], r['quantity']) for r in rows], [(HERB, 1)])


if __name__ == '__main__':
    unittest.main(verbosity=2)
