#!/usr/bin/env python3
"""
test_questloop.py - the whole quest loop over the fake-client rig
(quest_cards_misc-accept-rework / -kill-progress / -turnin-0x17 / -abandon-0x1E /
-keepalive-reaper, and the rest of item_inventory-quest-grant-mirror)

Every test drives the real handler loop through fakeclient (no port, no game client, a
temp accounts.json) and asserts BOTH halves of each flow: the exact packets the client
gets, and the persisted model the client's own bookkeeping is mirrored into.

The client is the bookkeeper (quest_cards_misc.md 1.3, live-verified by injection in
live_verify/results_quest_cards_misc.json #09/#10/#11/#12), so the rules under test are:

    accept   C2S 0x16 -> S2C 0x26 [+ 0x59 slot,0]      no 0x18, no chat line
    kill     server-driven kill   -> S2C 0x59 slot,n   one slot per kill, capped at ReqPro
    turn in  C2S 0x17 -> S2C 0x27 + 0x21 [+ 0x3F]      no 0x18, no 0x23
    abandon  C2S 0x1E -> S2C 0x38                      Send items stay, no message

and the S1-11 / Q-B5 exploit it replaces: a second 0x16 for a held quest used to be
treated as a TURN-IN with an empty Demand list counting as met, so after a portal wiped
the client log (Q-B8) the player could re-accept quest 26 at the NPC and be paid Herb x5
plus the Double Jump skill with no Pupu killed (reproduced live in quest_cards_misc#02).

Quest 26 "Elder's Test" is the live-proven starter quest: Send Wooden Stick 179 x1,
ReqPro 1 on npccode 1 (Pupu), Reward Herb 5 x5 + Double Jump 94 x1 (type 3), Exp 10,
Money 0, Repeat 0, SNPC = ENPC = 75, Lv 1..20.
"""
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import en_content as EC  # noqa: E402
import fakeclient as F  # noqa: E402
import inventory as INV  # noqa: E402
import quests as Q  # noqa: E402
from wsproto import hexbytes  # noqa: E402

W = F.import_server()
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
CAP_7E_101_TO_102 = hexbytes('17 00 00 00')     # the live portal capture (map 101 line 23)
PORTAL_102_TO_101 = 31                    # EN line 31 (live world_movement_npc#04; KR had 26)
QUEST, STICK, HERB, DOUBLE_JUMP = 26, 179, 5, 94
PUPU_NPCCODE = 1
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
            'accounts.json changed during test_questloop.py (tests must only use temp copies)'


class QuestTest(unittest.TestCase):
    """A fresh GameServer on a temp accounts.json, one client in world on map 101."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_quest_')
        self.server = F.make_server(self.tmp)
        self.clients = []
        self.c = self.enter()

    def tearDown(self):
        for c in self.clients:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- flows ---
    def client(self):
        c = F.FakeClient(self.server)
        self.clients.append(c)
        return c

    def enter(self, monsters=0, name='TestHero', user='test', password='test'):
        c = self.client()
        c.send_c2s('0x44D8BF/0x01', {'account_id': user, 'password': password})
        self.assertEqual(self.dec(c.expect(0x02))['result'], 1)
        c.send_c2s('0x42F904/0x2B', {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907,
                                     'char_name': name})
        c.expect(0x03, 0x07, 0x15, 0x28, 0x44, *F.mob_packets(monsters))
        self.assertTrue(c.wait_session(lambda s: s.get('in_world')))
        return c

    def relog(self, c, **kw):
        c.close()
        c.thread.join(timeout=5.0)
        return self.enter(**kw)

    @staticmethod
    def dec(pkt, length=None):
        return F.FakeClient.decode(pkt, allow_trailing=length is None and pkt.opcode == 0x03)

    def char(self, name='TestHero', user='test'):
        return self.server.store.find_character(user, name)

    def state(self):
        return Q.QuestState(self.char())

    def bag(self):
        return INV.Inventory(self.char()).totals()

    def gold(self):
        return INV.Wallet(self.char()).gold

    def accept(self, quest_id=QUEST, progress=True):
        """C2S 0x16 and the two packets a slot quest answers with."""
        self.c.send_c2s('0x47734D/0x16', {'quest_id': quest_id})
        return self.c.expect(0x26, 0x59) if progress else self.c.expect(0x26)

    def refusal(self, text=None):
        """The S2C 0x15 a refused quest request sends, and nothing else."""
        rec = self.dec(self.c.expect(0x15))
        if text is not None:
            self.assertEqual(rec['text'], f'[Warning] {text}')
        return rec

    def to_102(self, c=None):
        c = c or self.c
        c.send(0x7E, CAP_7E_101_TO_102)
        c.expect(0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(8))
        return c.session['monsters']

    def to_101(self, c=None):
        c = c or self.c
        c.send_c2s('0x42F76B/0x7E', {'portal_line_index': PORTAL_102_TO_101})
        c.expect(0x08, 0x03, 0x07, 0x28, 0x44)

    def kill(self, mob):
        """Swing until the monster dies, the way the memory combat driver does, and return
        the whole death cluster. The quiet window is generous on purpose: a short one turns
        a slow machine into a truncated packet list and a confusing assertion failure."""
        for _ in range(30):
            if not mob.alive:
                break
            self.server._memory_melee(mob.x, mob.y)
        self.assertFalse(mob.alive)
        return self.c.recv_until_quiet(quiet=0.6, timeout=8.0)


# ===========================================================================
class Quest26EndToEnd(QuestTest):
    """P2 exit criterion 3, end to end on the wire."""

    def test_accept_kill_turn_in_and_relog(self):
        q = EC.quests().get(QUEST)
        self.assertEqual((q.send, q.reward, q.exp, q.reqpro, q.npc), ([(STICK, 1)],
                         [(HERB, 5), (DOUBLE_JUMP, 1)], 10, 1, PUPU_NPCCODE))

        # 1. Accept: 0x26 + 0x59{slot 1, 0} and nothing else. The Wooden Stick is mirrored
        #    into the bag WITHOUT a 0x18 - the client grants it itself on 0x26 (live bug 4).
        grant, progress = self.accept()
        self.assertEqual(self.dec(grant, 2)['quest_id'], QUEST)
        self.assertEqual(self.dec(progress, 2), {'slot': 1, 'progress': 0})
        self.assertEqual(self.bag(), {STICK: 1})
        self.assertEqual(self.state().active, [QUEST, 0, 0])

        # 2. Kill one Pupu on 102: the death cluster carries the 0x59 that makes the
        #    counter read 1/1 and arms the client's green "ready" line.
        mobs = self.to_102()
        pkts = self.kill(mobs[W.MOB_UID_BASE])
        self.assertEqual([p.opcode for p in pkts], [0x29, 0x21, 0x59, 0x18])
        self.assertEqual(self.dec(pkts[2], 2), {'slot': 1, 'progress': 1})
        self.assertEqual(self.state().progress, [1, 0, 0])

        # A second kill does not run the counter past ReqPro.
        pkts = self.kill(mobs[W.MOB_UID_BASE + 1])
        self.assertEqual([p.opcode for p in pkts], [0x29, 0x21, 0x18])
        self.assertEqual(self.state().progress, [1, 0, 0])

        # 3. Turn in at the ENPC: 0x27 + 0x21 once. Money is 0, so no 0x3F.
        self.to_101()
        exp_before = self.char()['exp']
        # The Pupu drop table holds Herbs too, so the reward is asserted as a DELTA over
        # whatever the two kills happened to drop.
        herbs_before = self.bag().get(HERB, 0)
        self.c.send_c2s('0x477F3E/0x17', {'quest_id': QUEST})
        done, exp = self.c.expect(0x27, 0x21)
        self.assertEqual(self.dec(done, 2)['quest_id'], QUEST)
        self.assertEqual(self.dec(exp, 4)['exp_delta'], 10)
        self.assertEqual(self.char()['exp'], exp_before + 10)
        # Herb x5 is mirrored; the type-3 Double Jump is learned client-side and is in no
        # bag tab, so it must never appear in the model.
        bag = self.bag()
        self.assertEqual((bag[STICK], bag[HERB]), (1, herbs_before + 5))
        self.assertNotIn(DOUBLE_JUMP, bag)
        self.assertEqual(self.state().active, [0, 0, 0])
        self.assertEqual(self.state().completed, [[QUEST, 1]])

        # 4. The NPC stops offering it (Repeat 0), and a duplicate 0x17 pays nothing twice.
        self.c.send_c2s('0x47734D/0x16', {'quest_id': QUEST})
        self.refusal('You have already completed this quest.')
        self.c.send_c2s('0x477F3E/0x17', {'quest_id': QUEST})
        self.c.expect_silence()
        self.assertEqual(self.bag(), bag)                      # no second Herb x5

        # 5. After a relog it is in the completed list, and the 0x03 carries it.
        again = self.relog(self.c)
        self.assertEqual(Q.QuestState(self.char()).completed, [[QUEST, 1]])
        again.send_c2s('0x47734D/0x16', {'quest_id': QUEST})
        self.assertEqual(self.dec(again.expect(0x15))['text'],
                         '[Warning] You have already completed this quest.')

    def test_a_repeatable_quest_comes_back_until_its_repeat_count(self):
        """Q-B4: 152 EN quests have Repeat > 0 and could never be re-accepted."""
        state = self.state()
        q = EC.quests().get(QUEST)
        self.assertEqual(q.repeat, 0)
        for times, exhausted in ((1, True), (2, True)):
            state.complete_append(QUEST)
            self.assertEqual((state.times(QUEST), state.exhausted(QUEST, q)), (times, exhausted))
        repeatable = next(d for d in EC.quests().defs.values() if d.repeat == 3)
        st = Q.QuestState({})
        for times in (1, 2):
            st.complete_append(repeatable.idx)
            self.assertFalse(st.exhausted(repeatable.idx, repeatable))
        st.complete_append(repeatable.idx)
        self.assertTrue(st.exhausted(repeatable.idx, repeatable))


class ReAcceptExploit(QuestTest):
    """S1-11 / Q-B5: a repeated 0x16 for a held quest must never be a turn-in."""

    def test_re_accepting_a_held_quest_after_a_portal_pays_nothing(self):
        self.accept()
        self.to_102()
        self.to_101()
        bag, gold, exp = self.bag(), self.gold(), self.char()['exp']

        self.c.send_c2s('0x47734D/0x16', {'quest_id': QUEST})
        self.refusal('You are already on this quest.')          # never 0x27, never 0x21
        self.assertEqual((self.bag(), self.gold(), self.char()['exp']), (bag, gold, exp))
        self.assertEqual(self.state().active, [QUEST, 0, 0])
        self.assertEqual(self.state().completed, [])

    def test_an_empty_demand_list_alone_does_not_finish_a_quest(self):
        """The other half of Q-B5: `all(...)` over an empty Demand list is True, so the old
        handler completed every kill quest on the second 0x16. The turn-in gate is the
        ReqPro counter as well."""
        self.assertEqual(EC.quests().get(QUEST).demand, [])
        self.accept()
        self.c.send_c2s('0x477F3E/0x17', {'quest_id': QUEST})   # progress 0 of ReqPro 1
        self.refusal('This quest is not finished yet.')
        self.assertEqual(self.state().active, [QUEST, 0, 0])
        self.assertEqual(self.bag(), {STICK: 1})

    def test_a_turn_in_for_a_quest_that_is_not_held_is_ignored(self):
        self.c.send_c2s('0x477F3E/0x17', {'quest_id': QUEST})
        self.c.expect_silence()
        self.assertEqual(self.state().completed, [])
        self.assertEqual(self.bag(), {})


class TalkOnlyQuest(QuestTest):
    """F2 / Q-B3: the 9 talk-only EN quests use no slot - the client files them straight
    under Completed, so a server slot would make every later 0x59 write the wrong one."""

    TALK_ONLY = 1                                   # "Rona's Tip": Send Herb x3, Exp 5

    def test_a_talk_only_quest_is_completed_at_once_with_its_exp(self):
        q = EC.quests().get(self.TALK_ONLY)
        self.assertEqual((q.needs_slot, q.send, q.exp, q.money, q.reqpro),
                         (False, [(HERB, 3)], 5, 0, 0))
        exp_before = self.char()['exp']
        self.c.send_c2s('0x47734D/0x16', {'quest_id': self.TALK_ONLY})
        grant, exp = self.c.expect(0x26, 0x21)      # no 0x59: there is no slot to arm
        self.assertEqual(self.dec(grant, 2)['quest_id'], self.TALK_ONLY)
        self.assertEqual(self.dec(exp, 4)['exp_delta'], 5)
        self.assertEqual(self.char()['exp'], exp_before + 5)
        self.assertEqual(self.state().active, [0, 0, 0])
        self.assertEqual(self.state().completed, [[self.TALK_ONLY, 1]])
        self.assertEqual(self.bag(), {HERB: 3})

    def test_talk_only_quests_leave_all_three_slots_for_real_quests(self):
        self.c.send_c2s('0x47734D/0x16', {'quest_id': self.TALK_ONLY})
        self.c.expect(0x26, 0x21)
        grant, progress = self.accept()
        self.assertEqual(self.dec(progress, 2), {'slot': 1, 'progress': 0})
        self.assertEqual(self.state().active, [QUEST, 0, 0])


class AcceptValidation(QuestTest):
    """F1 step 3: the client's offer filter (E2) re-checked server side (Q-B6)."""

    def test_unknown_quest_id(self):
        self.c.send_c2s('0x47734D/0x16', {'quest_id': 65000})
        self.refusal('Unknown quest.')

    def test_a_quest_no_npc_offers_is_refused(self):
        snpc0 = next(d for d in EC.quests().defs.values() if not d.snpc)
        self.c.send_c2s('0x47734D/0x16', {'quest_id': snpc0.idx})
        self.c.expect_silence()                      # silent: the client cannot even offer it
        self.assertEqual(self.state().active, [0, 0, 0])

    def test_level_range(self):
        q = EC.quests().get(QUEST)
        self.c.session['level'] = q.end_lev + 1
        self.c.send_c2s('0x47734D/0x16', {'quest_id': QUEST})
        self.refusal('Your level does not match this quest.')
        self.c.session['level'] = q.start_lev
        self.accept()

    def test_job_flag(self):
        quests = EC.quests().defs
        quests[65002] = EC.QuestDef(65002, 0, {'Job': ['0' + '1' * 20], 'Start_Lev': ['1'],
                                               'End_Lev': ['99'], 'SNPC': ['75'], 'ReqPro': ['1']})
        self.addCleanup(quests.pop, 65002, None)
        self.assertEqual(self.char()['class'], 0)             # Novice: job[0 + 7*0]
        self.c.send_c2s('0x47734D/0x16', {'quest_id': 65002})
        self.refusal('Your class cannot take this quest.')

    def test_prev_quest_must_be_completed(self):
        chained = next(d for d in EC.quests().defs.values()
                       if d.prev_q and d.snpc and d.needs_slot)
        self.c.session['level'] = chained.start_lev
        self.c.send_c2s('0x47734D/0x16', {'quest_id': chained.idx})
        self.refusal('You must finish the previous quest first.')
        self.state().complete_append(chained.prev_q)
        self.c.send_c2s('0x47734D/0x16', {'quest_id': chained.idx})
        self.c.expect(0x26, 0x59)

    def test_a_full_log_is_refused_and_sends_no_0x26(self):
        """The client drops a 0x26 it has no free slot for in silence, so sending one would
        desync every later 0x59 (Q-B3)."""
        state = self.state()
        for quest_id in (2, 3, 4):
            state.accept(quest_id)
        self.c.send_c2s('0x47734D/0x16', {'quest_id': QUEST})
        self.refusal(f'Quest log full ({W.MAX_ACTIVE_QUESTS}).')
        self.assertEqual(self.state().active, [2, 3, 4])

    def test_no_bag_space_for_the_send_items(self):
        bag = INV.Inventory(self.char())
        for slot in range(bag.capacity('equip')):
            bag.add(STICK, 1)                                 # equipment does not stack
        self.c.send_c2s('0x47734D/0x16', {'quest_id': QUEST})
        self.refusal("There isn't empty space in the inventory.")
        self.assertEqual(self.state().active, [0, 0, 0])

    def test_a_short_payload_is_dropped(self):
        for opcode in (0x16, 0x17, 0x1E):
            with self.subTest(opcode=f'0x{opcode:02X}'):
                self.c.send(opcode, b'\x1a')
                self.c.expect_silence(0.2)


class KillProgress(QuestTest):
    """F3 / Q-B9: without this the ReqPro counter of 88 EN quests never moves, so their
    turn-in can never pass the client's own `progress >= ReqPro` gate."""

    def test_only_the_quest_that_wants_this_monster_is_credited(self):
        state = self.state()
        other = next(d for d in EC.quests().defs.values()
                     if d.reqpro and d.npc != PUPU_NPCCODE)
        state.accept(other.idx)                               # slot 1: wants another monster
        state.accept(QUEST)                                   # slot 2: wants the Pupu
        self.server.store.mark_dirty('test')
        mobs = self.to_102()
        pkts = self.kill(mobs[W.MOB_UID_BASE])
        self.assertEqual([p.opcode for p in pkts], [0x29, 0x21, 0x59, 0x18])
        self.assertEqual(self.dec(pkts[2], 2), {'slot': 2, 'progress': 1})
        self.assertEqual(self.state().progress, [0, 1, 0])

    def test_one_kill_credits_exactly_one_slot(self):
        """FUN_0041A230 breaks after the first matching slot, and so does the server."""
        state = self.state()
        state.accept(QUEST)
        state.active[1] = QUEST                               # two slots wanting one Pupu
        self.server.store.mark_dirty('test')
        mobs = self.to_102()
        pkts = self.kill(mobs[W.MOB_UID_BASE])
        self.assertEqual([p.opcode for p in pkts].count(0x59), 1)
        self.assertEqual(self.state().progress, [1, 0, 0])

    def test_a_kill_with_no_quest_sends_no_0x59(self):
        mobs = self.to_102()
        self.assertEqual([p.opcode for p in self.kill(mobs[W.MOB_UID_BASE])],
                         [0x29, 0x21, 0x18])

    def test_a_demand_item_re_sends_the_unchanged_progress(self):
        """F4 / Q-B10: a collected Demand item re-arms the client's readiness check, and the
        progress byte stays the ReqPro kill counter - it is not an item count."""
        quests = EC.quests().defs
        quests[65003] = EC.QuestDef(65003, 0, {
            'Demand': ['0005' + '0' * 36], 'Demand_Num': ['002' + '0' * 27],
            'ReqPro': ['3'], 'NPC': [str(PUPU_NPCCODE)], 'SNPC': ['75'],
            'Start_Lev': ['1'], 'End_Lev': ['99']})
        self.addCleanup(quests.pop, 65003, None)
        state = self.state()
        state.accept(65003)
        state.set_progress(0, 2)
        self.server.store.mark_dirty('test')
        self.server._quest_credit_item(self.c.session['sock'], self.c.session, HERB, 4, False)
        self.assertEqual(self.dec(self.c.expect(0x59), 2), {'slot': 1, 'progress': 2})
        self.assertEqual(self.state().progress, [2, 0, 0])


class TurnInRewards(QuestTest):
    """F5: the mirror of what S2C 0x27 does client-side, and the gates before it."""

    def make_quest(self, quest_id=65004, **fields):
        spec = {'Start_Lev': ['1'], 'End_Lev': ['99'], 'SNPC': ['75'], 'ENPC': ['75']}
        spec.update(fields)
        quests = EC.quests().defs
        quests[quest_id] = EC.QuestDef(quest_id, 0, spec)
        self.addCleanup(quests.pop, quest_id, None)
        return quests[quest_id]

    def test_a_negative_money_quest_needs_the_fee_and_pays_it_once(self):
        q = self.make_quest(Money=['-300'], Reward=['0005' + '0' * 76],
                            Reward_Num=['001' + '0' * 57])
        self.assertTrue(q.needs_slot)                          # Money < 0 takes a slot
        self.accept(q.idx)
        INV.Wallet(self.char()).gold = 299
        self.c.send_c2s('0x477F3E/0x17', {'quest_id': q.idx})
        self.refusal('Not enough gold.')
        self.assertEqual(self.state().active, [q.idx, 0, 0])

        INV.Wallet(self.char()).gold = 1000
        self.c.send_c2s('0x477F3E/0x17', {'quest_id': q.idx})
        done, gold = self.c.expect(0x27, 0x3F)                 # Exp 0: no 0x21
        self.assertEqual(self.dec(done, 2)['quest_id'], q.idx)
        self.assertEqual(self.dec(gold, 8)['gold'], 700)
        self.assertEqual(self.gold(), 700)
        self.assertEqual(self.bag(), {HERB: 1})

    def test_demand_items_must_be_held_and_are_removed_once(self):
        q = self.make_quest(Demand=['0005' + '0' * 36], Demand_Num=['003' + '0' * 27],
                            Reward=['0179' + '0' * 76], Reward_Num=['001' + '0' * 57])
        self.accept(q.idx)
        self.c.send_c2s('0x477F3E/0x17', {'quest_id': q.idx})
        self.refusal('This quest is not finished yet.')

        self.server._inv_add(self.c.session, HERB, 3, 'test')
        self.c.send_c2s('0x477F3E/0x17', {'quest_id': q.idx})
        self.c.expect(0x27)
        # The client removes the Demand items and adds the Reward itself on 0x27, so the
        # model mirrors both and sends no 0x19/0x23/0x18 (item_inventory F2/F4).
        self.assertEqual(self.bag(), {STICK: 1})
        self.assertEqual(self.state().completed, [[q.idx, 1]])

    def test_only_ids_the_en_client_has_reach_the_bag(self):
        """Reward id validation (S2-11): a KR-only id is logged and dropped, and it never
        blocks the turn-in - the client grants what it can and silently drops the rest."""
        q = self.make_quest(Reward=['0005' + '4356' + '0' * 72],
                            Reward_Num=['002' + '001' + '0' * 54], ReqPro=['1'],
                            NPC=[str(PUPU_NPCCODE)])
        self.accept(q.idx)
        self.state().set_progress(0, 1)
        with self.assertLogs('WS', logging.WARNING) as cm:
            self.c.send_c2s('0x477F3E/0x17', {'quest_id': q.idx})
            self.c.expect(0x27)
        self.assertEqual(self.bag(), {HERB: 2})
        self.assertTrue(any('item 4356 refused' in line for line in cm.output))

    def test_a_type_3_reward_is_not_bagged_and_is_persisted_as_a_skill(self):
        """Changed on purpose in P3 stage 3 (cs-skill-learn): the client learns a skill book
        on 0x27 (live #11) and the item id IS the skill id (combat_skill.md finding 1), so
        the server mirrors it into char['skills'] with no packet of its own. This test used
        to pin the live bug (logged, not persisted: Double Jump gone after a relog)."""
        self.assertEqual(EC.items().type_of(DOUBLE_JUMP), 3)
        self.accept()
        self.state().set_progress(0, 1)
        with self.assertLogs('WS', logging.INFO) as cm:
            self.c.send_c2s('0x477F3E/0x17', {'quest_id': QUEST})
            self.c.expect(0x27, 0x21)                       # no 0x18: the client learned it
        self.assertTrue(any(f'learned {DOUBLE_JUMP} Double Jump' in line for line in cm.output))
        self.assertNotIn(DOUBLE_JUMP, self.bag())
        self.assertEqual(self.char()['skills'], [DOUBLE_JUMP])

    def test_a_type_4_reward_is_not_bagged_and_is_persisted_as_the_class(self):
        """Changed on purpose in P3 stage 5 (lc-class-change): the client applies a job item
        on 0x27 through FUN_004249E0, so the server mirrors it into class/job2 with no packet
        of its own. This test used to pin the "NOT persisted yet" warning (a relog turned the
        new class back into a Novice); test_classvillage.QuestRewards covers the rest."""
        class_change = next(i for i in EC.items().ids_of_type(4))
        self.assertEqual(class_change, 180)                         # Class Change: Warrior
        q = self.make_quest(Reward=[f'{class_change:04d}' + '0' * 76],
                            Reward_Num=['001' + '0' * 57], ReqPro=['1'],
                            NPC=[str(PUPU_NPCCODE)])
        self.accept(q.idx)
        self.state().set_progress(0, 1)
        with self.assertLogs('WS', logging.INFO) as cm:
            self.c.send_c2s('0x477F3E/0x17', {'quest_id': q.idx})
            self.c.expect(0x27)                                     # no 0x18: applied client-side
        self.assertFalse(any('NOT persisted' in line for line in cm.output))
        self.assertEqual((self.char()['class'], self.char()['job2']), (1, 0))
        self.assertEqual(self.bag(), {})

    def test_no_reward_space_refuses_before_anything_changes(self):
        q = self.make_quest(Reward=['0179' + '0' * 76], Reward_Num=['001' + '0' * 57],
                            ReqPro=['1'], NPC=[str(PUPU_NPCCODE)])
        self.accept(q.idx)
        self.state().set_progress(0, 1)
        bag = INV.Inventory(self.char())
        for _ in range(bag.capacity('equip')):
            bag.add(STICK, 1)
        self.c.send_c2s('0x477F3E/0x17', {'quest_id': q.idx})
        self.refusal("There isn't empty space in the inventory.")
        self.assertEqual(self.state().active, [q.idx, 0, 0])   # still held, nothing paid
        self.assertEqual(self.state().completed, [])

    def test_the_demand_items_free_the_space_the_rewards_need(self):
        """The client removes the Demand items before it adds the Rewards, so the space
        check must run in that order too."""
        q = self.make_quest(Demand=['0179' + '0' * 36], Demand_Num=['001' + '0' * 27],
                            Reward=['0179' + '0' * 76], Reward_Num=['001' + '0' * 57])
        self.accept(q.idx)
        bag = INV.Inventory(self.char())
        for _ in range(bag.capacity('equip')):
            bag.add(STICK, 1)                                  # full, with the Demand inside
        self.c.send_c2s('0x477F3E/0x17', {'quest_id': q.idx})
        self.c.expect(0x27)
        self.assertEqual(self.bag()[STICK], bag.capacity('equip'))


class Abandon(QuestTest):
    """F6 / Q-B12: the Abandon -> OK click used to do nothing at all."""

    def test_abandon_removes_the_quest_and_keeps_the_send_items(self):
        self.accept()
        self.c.send_c2s('0x4774E7/0x1E', {'quest_id': QUEST})
        self.assertEqual(self.dec(self.c.expect(0x38), 2)['quest_id'], QUEST)
        self.assertEqual(self.state().active, [0, 0, 0])
        self.assertEqual(self.state().progress, [0, 0, 0])
        self.assertEqual(self.bag(), {STICK: 1})               # 0x38 returns nothing (live #12)
        self.assertEqual(self.state().completed, [])           # abandoning is not completing

    def test_a_duplicate_0x1E_is_tolerated(self):
        """One click produced one packet live (#04), but the client checks no event type
        there, so the second one must find no slot and be ignored."""
        self.accept()
        self.c.send_c2s('0x4774E7/0x1E', {'quest_id': QUEST})
        self.c.expect(0x38)
        self.c.send_c2s('0x4774E7/0x1E', {'quest_id': QUEST})
        self.c.expect_silence()

    def test_it_stays_gone_after_a_portal_and_a_relog(self):
        """P2 exit criterion 4."""
        self.accept()
        self.c.send_c2s('0x4774E7/0x1E', {'quest_id': QUEST})
        self.c.expect(0x38)
        self.to_102()
        self.to_101()
        self.assertEqual(self.state().active, [0, 0, 0])
        again = self.relog(self.c)
        self.assertEqual(Q.QuestState(self.char()).active, [0, 0, 0])
        # And the NPC offers it again, because abandoning does not complete it.
        again.send_c2s('0x47734D/0x16', {'quest_id': QUEST})
        again.expect(0x26, 0x59)

    def test_abandoning_a_quest_that_is_not_held_is_ignored(self):
        self.c.send_c2s('0x4774E7/0x1E', {'quest_id': QUEST})
        self.c.expect_silence()


class IdleReaper(unittest.TestCase):
    """F13 / Q-B17: the recv timeout used to `continue` forever, so a dead connection kept
    its session, its monsters and its unsaved state until the process died."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_reap_')
        self.server = F.make_server(self.tmp)
        # Real values are 120 s / 300 s; the loop is the same at test speed. The idle
        # window is kept a full second so a GC pause on a busy machine cannot reap a
        # connection the keepalive test is still feeding.
        self.server.RECV_TIMEOUT_SECS = 0.05
        self.server.IDLE_TIMEOUT_SECS = 1.0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def enter(self):
        c = F.FakeClient(self.server)
        c.send_c2s('0x44D8BF/0x01', {'account_id': 'test', 'password': 'test'})
        c.expect(0x02)
        c.send_c2s('0x42F904/0x2B', {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907,
                                     'char_name': 'TestHero'})
        c.expect(0x03, 0x07, 0x15, 0x28, 0x44)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world')))
        return c

    def test_a_silent_connection_is_closed_and_saved(self):
        c = self.enter()
        addr = c.addr
        with self.server._combat_lock(c.session):
            c.session['hp'] = 42
        c.thread.join(timeout=5.0)
        self.assertFalse(c.thread.is_alive())
        self.assertIsNone(self.server.sessions.get(addr))
        self.server.store.flush()
        with open(self.server.db_file, encoding='utf-8') as f:
            stored = json.load(f)['test']['characters'][0]
        self.assertEqual(stored['hp'], 42)                    # reaped, not lost
        c.close()

    def test_the_keepalive_holds_the_session_open_and_is_never_answered(self):
        c = self.enter()
        end = time.monotonic() + self.server.IDLE_TIMEOUT_SECS * 2
        while time.monotonic() < end:
            c.send(0x05)                                       # C2S KeepAlive, 0 B
            time.sleep(self.server.IDLE_TIMEOUT_SECS / 5)
        self.assertTrue(c.thread.is_alive())
        self.assertIsNotNone(self.server.sessions.get(c.addr))
        c.expect_silence(0.2)                                  # NEVER_REPLY: a reply kicks
        c.close()


if __name__ == '__main__':
    unittest.main()
