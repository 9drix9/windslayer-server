#!/usr/bin/env python3
"""
test_quests.py - the persistent quest state (quest_cards_misc-quest-state-model)

Every rule asserted here is one the client applies to its own copy of this state
(quest_cards_misc.md 1.3 and 3.2):

    3 active slots, written first-empty                    scene+0x29E
    progress is the ReqPro KILL counter, one per slot       S2C 0x59, FUN_0041A230
    completed[[id, times <= 99]], <= 85, shift left on overflow   FUN_004264A0
    "already done" = completed and (Repeat == 0 or times >= Repeat)   FUN_00477FF0
    card deck <= 51 unique ids                              window 8 "count / 64"

No file, socket or packet is touched.
"""
import json
import logging
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import en_content as EC  # noqa: E402
import quests as Q  # noqa: E402

logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

STARTER = 26          # Elder's Test: ReqPro 1 on npccode 1 (Pupu), Repeat 0
TALK_ONLY = 1         # Money >= 0, no Demand, ReqPro 0 -> no slot


def new_char(**extra):
    char = {'name': 'TestHero'}
    char.update(extra)
    Q.ensure(char)
    return char


class Normalizing(unittest.TestCase):
    def test_creates_the_f4_shape(self):
        char = new_char()
        self.assertEqual(char['quests'], {'active': [0, 0, 0], 'progress': [0, 0, 0],
                                          'completed': []})
        self.assertEqual(char['card_deck'], [])

    def test_repairs_a_hand_edited_record(self):
        char = new_char(quests={'active': [26, 0, 0, 99], 'progress': [5, 7], 'completed':
                                [[26, 300], 27, {'id': 28, 'times': 2}, [26, 1], 0, 'x']},
                        card_deck=[2030, 2030, 0, 2031])
        # A 4th slot is dropped, progress is padded and an empty slot can carry none.
        self.assertEqual(char['quests']['active'], [26, 0, 0])
        self.assertEqual(char['quests']['progress'], [5, 0, 0])
        # Entries in any legacy shape become [id, times], times clamped, duplicates dropped.
        self.assertEqual(char['quests']['completed'], [[26, Q.MAX_COMPLETED_TIMES], [27, 1], [28, 2]])
        self.assertEqual(char['card_deck'], [2030, 2031])

    def test_json_round_trip(self):
        char = new_char()
        state = Q.QuestState(char)
        state.accept(STARTER)
        state.set_progress(0, 1)
        state.complete_append(99)
        state.add_card(2030)
        restored = json.loads(json.dumps(char))
        Q.ensure(restored)
        self.assertEqual(restored['quests'], char['quests'])
        self.assertEqual(restored['card_deck'], char['card_deck'])

    def test_a_second_ensure_does_not_orphan_a_live_state(self):
        char = new_char()
        state = Q.QuestState(char)
        Q.ensure(char)
        state.accept(STARTER)
        state.add_card(2030)
        self.assertEqual(char['quests']['active'], [STARTER, 0, 0])
        self.assertEqual(char['card_deck'], [2030])

    def test_bad_record_type(self):
        with self.assertRaises(TypeError):
            Q.ensure('nope')


class Slots(unittest.TestCase):
    def setUp(self):
        self.char = new_char()
        self.state = Q.QuestState(self.char)

    def test_accept_uses_the_first_empty_slot(self):
        self.assertEqual(self.state.accept(26), 0)
        self.assertEqual(self.state.accept(27), 1)
        self.assertEqual(self.state.slot_of(26), 0)
        self.assertEqual(self.state.slot_of(27), 1)
        self.assertIsNone(self.state.slot_of(99))
        self.assertTrue(self.state.is_active(26))
        self.assertEqual(self.state.active_ids(), [26, 27])
        # Abandoning frees that slot, and the next accept fills the hole (first-empty).
        self.assertEqual(self.state.abandon(26), 0)
        self.assertEqual(self.state.first_free_slot(), 0)
        self.assertEqual(self.state.accept(28), 0)

    def test_the_log_holds_three(self):
        for i, quest_id in enumerate((26, 27, 28)):
            self.assertEqual(self.state.accept(quest_id), i)
        self.assertIsNone(self.state.first_free_slot())
        self.assertIsNone(self.state.accept(29))
        self.assertIsNone(self.state.accept(26))               # already held
        self.assertEqual(self.state.active, [26, 27, 28])

    def test_a_talk_only_quest_takes_no_slot(self):
        """The client files those straight into the completed list (Q-B3): giving them a
        slot drifted the server's slot numbers from the client's."""
        self.assertEqual(self.state.accept(TALK_ONLY, needs_slot=False), -1)
        self.assertEqual(self.state.active, [0, 0, 0])
        self.assertEqual(self.state.times(TALK_ONLY), 1)

    def test_progress_is_per_slot_and_cleared_with_it(self):
        self.state.accept(26)
        self.assertEqual(self.state.set_progress(0, 3), 3)
        self.assertEqual(self.state.progress, [3, 0, 0])
        self.assertIsNone(self.state.set_progress(1, 5))       # empty slot
        self.assertIsNone(self.state.set_progress(9, 5))
        self.assertEqual(self.state.set_progress(0, 999), Q.MAX_PROGRESS)
        self.state.abandon(26)
        self.assertEqual(self.state.progress, [0, 0, 0])


class Completion(unittest.TestCase):
    def setUp(self):
        self.char = new_char()
        self.state = Q.QuestState(self.char)

    def test_complete_frees_the_slot_and_records_the_times(self):
        self.state.accept(26)
        self.state.set_progress(0, 1)
        self.assertEqual(self.state.complete(26), (0, 1))
        self.assertEqual((self.state.active, self.state.progress), ([0, 0, 0], [0, 0, 0]))
        self.assertEqual(self.state.completed, [[26, 1]])
        self.assertEqual(self.state.times(26), 1)

    def test_append_rules_follow_fun_004264a0(self):
        for _ in range(3):
            self.state.complete_append(26)
        self.assertEqual(self.state.completed, [[26, 3]])       # existing entry increments
        for _ in range(200):
            self.state.complete_append(26)
        self.assertEqual(self.state.times(26), Q.MAX_COMPLETED_TIMES)   # capped at 99
        for quest_id in range(100, 100 + Q.MAX_COMPLETED - 1):
            self.state.complete_append(quest_id)
        self.assertEqual(len(self.state.completed), Q.MAX_COMPLETED)
        # Full list: it shifts left by one and appends, so the oldest entry is lost.
        self.state.complete_append(999)
        self.assertEqual(len(self.state.completed), Q.MAX_COMPLETED)
        self.assertEqual(self.state.times(26), 0)               # the shifted-out entry
        self.assertEqual(self.state.completed[-1], [999, 1])

    def test_exhausted_matches_fun_00477ff0(self):
        catalog = EC.quests()
        once = catalog.get(STARTER)                             # Repeat 0
        self.assertFalse(self.state.exhausted(STARTER, once))
        self.state.complete_append(STARTER)
        self.assertTrue(self.state.exhausted(STARTER, once))
        repeatable = next(q for q in catalog.defs.values() if q.repeat == 3)
        for i in range(3):
            self.assertEqual(self.state.exhausted(repeatable.idx, repeatable), i >= 3)
            self.state.complete_append(repeatable.idx)
        self.assertTrue(self.state.exhausted(repeatable.idx, repeatable))
        # With no definition the safe reading is "once ever" (repeat 0).
        self.assertTrue(self.state.exhausted(STARTER, None))


class KillCredit(unittest.TestCase):
    """F3 / FUN_0041A230: one kill credits ONE slot, the first whose quest wants that
    monster and is not yet full."""

    def setUp(self):
        self.char = new_char()
        self.state = Q.QuestState(self.char)
        self.catalog = EC.quests()

    def test_one_kill_credits_one_slot(self):
        starter = self.catalog.get(STARTER)                     # ReqPro 1 on npccode 1
        other = next(q for q in self.catalog.defs.values()
                     if q.reqpro > 1 and q.npc == starter.npc and q.idx != STARTER)
        self.state.accept(STARTER)
        self.state.accept(other.idx)
        self.assertEqual(self.state.credit_kill(starter.npc, self.catalog), (0, 1))
        # Slot 0 is full now (ReqPro 1), so the next kill goes to the other quest.
        self.assertEqual(self.state.credit_kill(starter.npc, self.catalog), (1, 1))
        self.assertEqual(self.state.progress[:2], [1, 1])

    def test_a_monster_no_quest_wants_credits_nothing(self):
        self.state.accept(STARTER)
        self.assertIsNone(self.state.credit_kill(9999, self.catalog))
        self.assertEqual(self.state.progress, [0, 0, 0])

    def test_progress_stops_at_reqpro(self):
        starter = self.catalog.get(STARTER)
        self.state.accept(STARTER)
        self.assertEqual(self.state.credit_kill(starter.npc, self.catalog), (0, 1))
        self.assertIsNone(self.state.credit_kill(starter.npc, self.catalog))
        self.assertEqual(self.state.progress[0], starter.reqpro)


class CardDeck(unittest.TestCase):
    def setUp(self):
        self.state = Q.QuestState(new_char())

    def test_unique_and_capped(self):
        self.assertEqual(self.state.add_card(2030), 1)
        self.assertIsNone(self.state.add_card(2030))            # S2C 0x8B result 6
        self.assertTrue(self.state.has_card(2030))
        self.assertFalse(self.state.has_card(2031))
        for card in range(3000, 3000 + Q.MAX_DECK):
            self.state.add_card(card)
        self.assertEqual(len(self.state.deck), Q.MAX_DECK)
        self.assertIsNone(self.state.add_card(4000))
        self.assertIsNone(self.state.add_card(0))


if __name__ == '__main__':
    unittest.main()
