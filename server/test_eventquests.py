#!/usr/bin/env python3
"""
test_eventquests.py - P13 stage 2 (event-quests, ev-e5), offline, both client builds
===================================================================================
Design re_tools/docs/systems_2009/events_bosses.md A3, A8 ("S2C 0x26 / C2S 0x17 / S2C 0x27"),
E5; P13 exit criteria 2 ("*Monster Card Challenge" in the quest log, once) and 4 (20 Blue
Mushrooms at Nicolas: 0x27 + card + EXP + gold, then 159; without them: nothing).

- the data gates (events.quest_refusal / quest_chain) against each build's own hqi: the 2009
  chain 158 -> 159 -> 160 is the only fully English one; the Korean-text quests (232-291, and
  the 2008 hqi's own 158-161) are refused unless EVENT_QUEST_KOREAN_TEXT; ENPC 0 (quest 63)
  is never pushed;
- the server through fakeclient: the first C2S 0x63 of an event pushes S2C 0x26 + 0x59 after
  the gift and before the popup, once (portal / relog: held, nothing); the C2S 0x17 turn-in
  sends 0x27 + 0x21 (the arch09-exp-pipeline, x2 with EVENT_EXP_QUESTS) + 0x3F and pushes the
  NextQuest while the event runs; a refused event-quest turn-in sends nothing at all; the
  chain resumes at a later login (event over, full log, abandon); C2S 0x16 for a chain quest
  (evb Q4) is a push, never a [Warning]; two clients get the quest once each; the 2008 build
  pushes nothing of the shipped event and runs a configured Korean quest end to end.

No port is bound, no client is started and the live accounts.json is never opened (temp
copies; the module checks its hash at the end).
"""
import copy
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
import en_content as EC  # noqa: E402
import events as E  # noqa: E402
import fakeclient as F  # noqa: E402
import inventory as INV  # noqa: E402
import packets as P  # noqa: E402
import quests as Q  # noqa: E402

W = F.import_server()
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = '2008', '2009'
DIR8 = os.path.join(HERE, cfgmod.DEFAULTS['CLIENT_DIR'])
DIR9 = os.path.join(HERE, cfgmod.DEFAULTS['CLIENT_DIR_2009'])
HAVE_2008 = os.path.exists(os.path.join(DIR8, 'hs', 'windslayer.hqi'))
HAVE_2009 = os.path.exists(os.path.join(DIR9, 'hs', 'windslayer.hqi'))
needs_2008 = unittest.skipUnless(HAVE_2008, 'needs the EN 2008 client data (hs/)')
needs_2009 = unittest.skipUnless(HAVE_2009, 'needs the EN 2009 client data (CLIENT_DIR_2009)')

LOVE_POTION = 1282
BLUE_MUSHROOM, PORK, SCRAP_IRON = 3, 140, 181
CARD_KORING, CARD_DUMPLING_PIG, CARD_IRON_BALL = 2031, 2038, 2043
GOLDEN_PIG, ELIXIR, ENERGIZER = 123, 1270, 1340
KOREAN_240 = 240                   # "*황금돼지를 모으자!" ENPC 76, Lv 1-20, Repeat 99, 123 x20
TOWN, PUPU_MAP = 101, 102
KEYS = {
    B8: {'enter': '0x42F904/0x2B', 'portal': '0x42F76B/0x7E', 'cards': '0x44EF67/0x63',
         'accept': '0x47734D/0x16', 'turn_in': '0x477F3E/0x17', 'abandon': '0x4774E7/0x1E'},
    B9: {'enter': '0x4315D7/0x2B', 'portal': '0x431284/0x7E', 'cards': '0x453557/0x63',
         'accept': '0x489A44/0x16', 'turn_in': '0x48A95A/0x17', 'abandon': '0x489BC2/0x1E'},
}
ACCOUNTS = {
    'test': {'password': 'test', 'characters': [
        {'name': 'TestHero', 'level': 10, 'class': 0, 'map': TOWN, 'x': 1411, 'y': 714,
         'hp': 100, 'mp': 50, 'gm': 1}]},
    'admin': {'password': 'admin', 'characters': [
        {'name': 'Bob', 'level': 5, 'class': 0, 'map': TOWN, 'x': 1411, 'y': 714,
         'hp': 100, 'mp': 50}]},
}
# The P13 exit-criteria event (ROADMAP_2009_ADDENDUM P13 criterion 2), enabled.
EXIT_EVENT = {'id': 'p13-exit', 'enabled': True, 'start': None, 'end': None, 'exp_mult': 2.0,
              'announce': {'text': 'EXP x2 event! Visit Nicolas.', 'every_min': 30, 'on_enter': True},
              'popup_event_news': True, 'login_gift': [[LOVE_POTION, 5]], 'push_quests': [158]}
# An event that only pushes quests (no line, gift or popup in the 0x63 tail).
QUESTS_ONLY = {'id': 'quests', 'enabled': True, 'push_quests': [158]}
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
            'accounts.json changed during test_eventquests.py (tests must only use temp copies)'


def use_build(build):
    EC.configure(cfgmod.DEFAULTS['CLIENT_DIR_2009'] if build == B9 else cfgmod.DEFAULTS['CLIENT_DIR'], build)


def event(base=EXIT_EVENT, **over):
    return {**json.loads(json.dumps(base)), **over}


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = float(t)

    def __call__(self):
        return self.t


class _Quest:
    """A stand-in hqi record for the chain walk (idx / next_q only)."""

    def __init__(self, idx, next_q):
        self.idx, self.next_q = idx, next_q


# ================================================================== data ===
class DataGates(unittest.TestCase):
    """events.quest_refusal / quest_text_refusal / quest_chain against each build's hqi."""

    def tearDown(self):
        use_build(B8)

    @needs_2009
    def test_2009_the_monster_card_chain_is_the_english_one(self):
        use_build(B9)
        self.assertEqual(E.quest_chain(158), [158, 159, 160])
        self.assertEqual(E.quest_chain(159), [159, 160])
        for quest_id, (demand, reward, exp, money) in zip(E.ENGLISH_EVENT_CHAIN, (
                ((BLUE_MUSHROOM, 20), (CARD_KORING, 1), 50, 500),
                ((PORK, 30), (CARD_DUMPLING_PIG, 1), 100, 1000),
                ((SCRAP_IRON, 50), (CARD_IRON_BALL, 1), 200, 2000))):
            with self.subTest(quest=quest_id):
                q = EC.quests().get(quest_id)
                self.assertEqual((q.snpc, q.enpc, q.start_lev, q.end_lev, q.repeat), (0, E.NICOLAS_NPC, 1, 99, 0))
                self.assertEqual((q.demand, q.reward, q.exp, q.money), ([demand], [reward], exp, money))
                self.assertIsNone(E.quest_refusal(quest_id))
        self.assertEqual(EC.lng(E.QUEST_TEXT_LNG)[EC.quests().get(158).title_text], '*Monster Card Challenge')
        self.assertIn('ENPC 0', E.quest_refusal(63))                       # Love Potion quest: no turn-in NPC
        self.assertIn('ENPC 0', E.quest_refusal(63, allow_korean=True))
        self.assertIn('hqi', E.quest_refusal(4999))

    @needs_2009
    def test_2009_korean_text_quests_need_the_switch(self):
        use_build(B9)
        korean = [q for q in range(232, 292) if EC.quests().get(q) is not None
                  and E.quest_text_refusal(EC.quests().get(q)) is not None]
        # 38 of 232-291; 243-261 and 282-284 are regular English quests (283 "Nunaga" has SNPC 0)
        self.assertEqual(len(korean), 38)
        self.assertEqual([q for q in range(232, 292) if EC.quests().get(q).snpc == 0 and q not in korean], [283])
        for quest_id in korean:
            with self.subTest(quest=quest_id):
                self.assertIn('Korean', E.quest_refusal(quest_id))
                if EC.quests().get(quest_id).enpc:
                    self.assertIsNone(E.quest_refusal(quest_id, allow_korean=True))

    @needs_2008
    def test_2008_has_no_english_event_quest(self):
        use_build(B8)
        for quest_id in (158, 159, 160):                                    # Korean Thanksgiving quests, ENPC 0
            with self.subTest(quest=quest_id):
                self.assertEqual(E.quest_chain(quest_id), [quest_id])
                self.assertIn('ENPC 0', E.quest_refusal(quest_id, allow_korean=True))
                self.assertIsNotNone(E.quest_text_refusal(EC.quests().get(quest_id)))
        nicolas = [q.idx for q in EC.quests().defs.values() if q.enpc == E.NICOLAS_NPC]
        self.assertIn(KOREAN_240, nicolas)
        for quest_id in nicolas:
            with self.subTest(quest=quest_id):
                self.assertIn('Korean', E.quest_refusal(quest_id))
        self.assertIsNone(E.quest_refusal(KOREAN_240, allow_korean=True))

    def test_the_chain_walk_is_cycle_safe(self):
        catalog = {1: _Quest(1, 2), 2: _Quest(2, 3), 3: _Quest(3, 1), 7: _Quest(7, 99)}
        self.assertEqual(E.quest_chain(1, catalog), [1, 2, 3])
        self.assertEqual(E.quest_chain(7, catalog), [7])                   # NextQuest missing: stop
        self.assertEqual(E.quest_chain(5, catalog), [])
        long = {i: _Quest(i, i + 1) for i in range(1, 100)}
        self.assertEqual(len(E.quest_chain(1, long)), E.QUEST_CHAIN_MAX)

    def test_config_key(self):
        self.assertIs(cfgmod.defaults().EVENT_QUEST_KOREAN_TEXT, False)
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.from_dict({'EVENT_QUEST_KOREAN_TEXT': 1})
        with open(os.path.join(HERE, 'config.json'), encoding='utf-8') as f:
            self.assertIs(json.load(f)['EVENT_QUEST_KOREAN_TEXT'], False)
        shipped = E.load_file(os.path.join(HERE, 'events.json'))[0]
        self.assertEqual(shipped.push_quests, (158,))                      # the English chain's head


# ================================================================ server ===
class Rig(unittest.TestCase):
    build = B8
    events = (EXIT_EVENT,)
    extra_config = {}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix=f'ws_evquests{self.build}_')
        self.clients = []
        self.make(self.events, self.extra_config)

    def make(self, events, extra_config=None):
        """A fresh server on a fresh temp accounts.json with `events` as its schedule."""
        for c in self.clients:
            c.close()
        self.clients = []
        use_build(self.build)
        work = tempfile.mkdtemp(dir=self.tmp)
        path = os.path.join(work, 'events.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'events': list(events)}, f)
        cfg = {'MOB_SERVER_CONTROLLED': True, 'CLIENT_BUILD': self.build, 'EVENTS_FILE': path,
               **(extra_config or {})}
        self.server = F.make_server(work, accounts=json.loads(json.dumps(ACCOUNTS)),
                                    config=cfgmod.from_dict(cfg))
        self.mono = self.server.events.mono = FakeClock()
        self.keys = KEYS[self.build]
        return self.server

    def tearDown(self):
        for c in self.clients:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        use_build(B8)

    # ------------------------------------------------------------ helpers ---
    def char(self, user='test'):
        return self.server.store.characters(user)[0]

    def state(self, user='test'):
        return Q.QuestState(self.char(user))

    def bag(self, user='test'):
        return INV.Inventory(self.char(user))

    def gold(self, user='test'):
        return INV.Wallet(self.char(user)).gold

    def give(self, item, count, user='test'):
        with self.server.store.lock:
            self.assertIsNotNone(self.bag(user).add(item, count))

    def disk_quests(self, user='test'):
        self.server.store.flush()
        with open(self.server.db_file, encoding='utf-8') as f:
            return json.load(f)[user]['characters'][0]['quests']

    def enter(self, user='test'):
        others = list(self.clients)
        c = F.FakeClient(self.server)
        self.clients.append(c)
        self.assertEqual(c.login(user, user)['result'], 1)
        c.send_c2s(self.keys['enter'], {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907 + len(others),
                                        'char_name': self.char(user)['name']})
        self.assertTrue(c.wait_session(lambda s: s.get('in_world')))
        ops = [0x03, 0x07, 0x15] + ([0x65] if self.build == B9 else []) + [0x28, 0x44]
        F.expect_entry(c, ops, others=others, map_code=self.char(user)['map'])
        return c

    def relog(self, c, user='test'):
        self.clients.remove(c)
        F.close_seen(c, self.clients)
        return self.enter(user)

    def resync(self, c, *extra, first=False, user='test'):
        """The client's own C2S 0x63 after a map load: 0x8A, the 0x59 re-arm of every held
        slot, [0x99 sub 8 on the first of a connection] and then `extra` (the event tail).
        Returns the tail packets."""
        held = len(self.state(user).active_ids())
        c.send_c2s(self.keys['cards'])
        head = [0x8A] + [0x59] * held + ([0x99] if first else [])
        pkts = c.expect(*head, *extra)
        pkts = pkts if isinstance(pkts, list) else [pkts]
        return pkts[len(head):]

    def pushed(self, c, pkts, quest_id, slot=1):
        """A 0x26 {quest_id} + 0x59 {slot, 0} pair."""
        self.assertEqual([p.opcode for p in pkts], [0x26, 0x59])
        self.assertEqual(c.s2c(pkts[0]), {'quest_id': quest_id})
        self.assertEqual(c.s2c(pkts[1]), {'slot': slot, 'progress': 0})

    def turn_in(self, c, quest_id, *ops):
        c.send_c2s(self.keys['turn_in'], {'quest_id': quest_id})
        if not ops:
            c.expect_silence()
            return []
        pkts = c.expect(*ops)
        return pkts if isinstance(pkts, list) else [pkts]

    def portal_index(self, src, dst):
        return next(int(k.split('_')[1]) for k, v in sorted(EC.portals().items())
                    if k.startswith(f'{src}_') and v[0] == dst)

    def portal(self, c, src, dst, mobs=0):
        c.send_c2s(self.keys['portal'], {'portal_line_index': self.portal_index(src, dst)})
        c.expect(0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(mobs))
        self.assertTrue(c.wait_session(lambda s: s.get('in_world') and s.get('current_map') == dst))

    def replies(self, c, n=1):
        pkts = c.expect(*([0x15] * n))
        pkts = pkts if isinstance(pkts, list) else [pkts]
        return [c.s2c(p)['text'] for p in pkts]


@needs_2009
class EventQuests2009(Rig):
    """The primary target: the English Monster Card Challenge chain."""
    build = B9

    def test_the_first_entry_pushes_158_once_between_gift_and_popup(self):
        c = self.enter()
        tail = self.resync(c, 0x15, 0x99, 0x26, 0x59, 0x80, first=True)
        self.assertEqual(c.s2c(tail[1]), {'sub_type': 9, 'item_id': LOVE_POTION, 'count': 5})
        self.pushed(c, tail[2:4], 158)
        self.assertEqual(bytes([0x80]) + tail[4].payload, bytes.fromhex('80 01 FB 03 00 00'))
        self.assertEqual(self.state().active, [158, 0, 0])
        self.assertEqual(self.disk_quests()['active'], [158, 0, 0])        # the quest store persists it
        # a portal: the 0x63 re-arms slot 1 and pushes nothing
        self.portal(c, TOWN, PUPU_MAP, mobs=8)
        self.assertEqual(self.resync(c), [])
        self.portal(c, PUPU_MAP, TOWN)
        self.assertEqual(self.resync(c), [])
        # a relog (within and after the quiet window): held, so no 0x26 again
        self.mono.t += 60
        c = self.relog(c)
        self.assertEqual(self.resync(c, first=True), [])
        self.mono.t += 1800
        c = self.relog(c)
        self.assertEqual([p.opcode for p in self.resync(c, 0x15, 0x80, first=True)], [0x15, 0x80])
        self.assertEqual(self.state().active, [158, 0, 0])

    def test_nicolas_turn_ins_run_the_whole_chain(self):
        """P13 exit criterion 4: 0x17 158 -> 0x27 + 0x21 + 0x3F, card and gold, then 159."""
        c = self.enter()
        self.resync(c, 0x15, 0x99, 0x26, 0x59, 0x80, first=True)
        steps = ((158, BLUE_MUSHROOM, 20, CARD_KORING, 50, 500, 159),
                 (159, PORK, 30, CARD_DUMPLING_PIG, 100, 1000, 160),
                 (160, SCRAP_IRON, 50, CARD_IRON_BALL, 200, 2000, None))
        for quest_id, item, need, card, exp, money, nxt in steps:
            with self.subTest(quest=quest_id):
                self.give(item, need)
                gold, total = self.gold(), self.char()['exp']
                ops = [0x27, 0x21, 0x3F] + ([0x26, 0x59] if nxt else [])
                pkts = self.turn_in(c, quest_id, *ops)
                self.assertEqual(c.s2c(pkts[0]), {'quest_id': quest_id})
                self.assertEqual(struct.unpack('<i', pkts[1].payload)[0], exp)      # no event on quest exp
                self.assertEqual(c.s2c(pkts[2])['gold'], gold + money)
                if nxt:
                    self.pushed(c, pkts[3:], nxt)
                self.assertEqual((self.char()['exp'], self.gold()), (total + exp, gold + money))
                self.assertEqual((self.bag().count(item), self.bag().count(card)), (0, 1))
                self.assertEqual(self.state().active, [nxt or 0, 0, 0])
                self.assertEqual(self.state().times(quest_id), 1)
        self.assertEqual(self.disk_quests()['completed'], [[158, 1], [159, 1], [160, 1]])
        # all done: a later login pushes nothing, a 0x17 or 0x16 again is silent
        self.mono.t += 1800
        c = self.relog(c)
        self.assertEqual([p.opcode for p in self.resync(c, 0x15, 0x80, first=True)], [0x15, 0x80])
        self.turn_in(c, 160)
        c.send_c2s(self.keys['accept'], {'quest_id': 158})
        c.expect_silence()

    def test_without_the_items_nothing_happens(self):
        """P13 exit criterion 4, B: no 0x27, no 0x21, no [Warning] line - nothing."""
        c = self.enter()
        self.resync(c, 0x15, 0x99, 0x26, 0x59, 0x80, first=True)
        gold = self.gold()
        self.turn_in(c, 158)                                               # no mushroom at all
        self.give(BLUE_MUSHROOM, 19)
        self.turn_in(c, 158)                                               # one short
        self.assertEqual((self.state().active, self.state().completed), ([158, 0, 0], []))
        self.assertEqual((self.bag().count(BLUE_MUSHROOM), self.bag().count(CARD_KORING), self.gold()),
                         (19, 0, gold))
        # a P2 quest keeps its [Warning] (the silence is for event quests only)
        with self.server.store.lock:
            self.state().accept(26)
        self.turn_in(c, 26, 0x15)

    def test_quest_exp_follows_the_event_with_EVENT_EXP_QUESTS(self):
        self.make(self.events, {'EVENT_EXP_QUESTS': True})
        c = self.enter()
        self.resync(c, 0x15, 0x99, 0x26, 0x59, 0x80, first=True)
        self.give(BLUE_MUSHROOM, 20)
        pkts = self.turn_in(c, 158, 0x27, 0x21, 0x3F, 0x26, 0x59)
        self.assertEqual(struct.unpack('<i', pkts[1].payload)[0], 100)    # 50 x2 (arch09-exp-pipeline)

    def test_the_next_quest_waits_for_a_running_event_and_resumes_later(self):
        c = self.enter()
        self.resync(c, 0x15, 0x99, 0x26, 0x59, 0x80, first=True)
        ev = self.server.events
        ev.dev_event(c.session, 'stop p13-exit')
        self.assertIn('off (GM)', self.replies(c)[0])
        self.give(BLUE_MUSHROOM, 20)
        self.turn_in(c, 158, 0x27, 0x21, 0x3F)                            # completed, no 159: the event is over
        self.assertEqual(self.state().active, [0, 0, 0])
        ev.dev_event(c.session, 'auto p13-exit')
        c.expect(0x15)
        self.mono.t += 60                                                  # quiet window: no line, no popup
        c = self.relog(c)
        self.pushed(c, self.resync(c, 0x26, 0x59, first=True), 159)        # the chain resumes at 159

    def test_a_full_log_pushes_nothing_until_a_slot_is_free(self):
        with self.server.store.lock:
            state = self.state()
            for quest_id in (26, 28, 30):
                state.accept(quest_id)
        c = self.enter()
        with self.assertLogs('WS', logging.WARNING) as logs:
            self.resync(c, 0x15, 0x99, 0x80, first=True)
        self.assertTrue(any('quest 158 not pushed' in line and 'slots are used' in line for line in logs.output))
        c.send_c2s(self.keys['abandon'], {'quest_id': 28})
        c.expect(0x38)
        self.mono.t += 60
        c = self.relog(c)
        self.pushed(c, self.resync(c, 0x26, 0x59, first=True), 158, slot=2)
        self.assertEqual(self.state().active, [26, 158, 30])

    def test_no_push_while_a_map_load_is_under_way(self):
        """P13 review: the readiness is re-checked under the combat lock the push runs under
        (and the map-load hook clears it under the same lock), so no 0x26 lands between a map
        load's lead and the client's resync; the chain is pushed at the next C2S 0x63."""
        c = self.enter()
        self.resync(c, 0x15, 0x99, 0x26, 0x59, 0x80, first=True)
        c.send_c2s(self.keys['abandon'], {'quest_id': 158})
        c.expect(0x38)
        before = E.grants(c.session)
        with self.server._combat_lock(c.session):
            c.session['events_ready'] = False                          # a map load began
        with self.assertLogs('WS', logging.INFO) as logs:
            self.assertFalse(self.server.events.push_quest(c.session, 158, 'test'))
        self.assertTrue(any('map load is under way' in line for line in logs.output))
        c.expect_silence(0.2)
        self.assertEqual((self.state().active, E.grants(c.session)), ([0, 0, 0], before))
        with self.server._combat_lock(c.session):
            c.session['events_ready'] = True                           # its C2S 0x63
        self.assertTrue(self.server.events.push_quest(c.session, 158, 'test'))
        self.pushed(c, c.expect(0x26, 0x59), 158)
        self.assertEqual(E.grants(c.session), before + 1)

    def test_a_talk_only_event_quests_exp_is_awarded_under_the_combat_lock(self):
        """P13 post-merge review: a talk-only event quest (no slot: the client files it as
        completed at once) gets its exp right after its 0x26, while push_quest still holds the
        combat lock its readiness check ran under - so a map load that begins meanwhile can
        neither rebuild its 0x03 before the exp is applied nor get the 0x21 between its lead
        and its resync. The shipped chain has no such quest: 158 is reshaped for the test."""
        self.make([event(QUESTS_ONLY, push_quests=[])])
        c = self.enter()
        self.assertEqual(self.resync(c, first=True), [])
        catalog = EC.quests()
        talk = copy.copy(catalog.get(158))
        talk.demand, talk.send, talk.reqpro, talk.money, talk.exp = (), (), 0, 0, 50
        self.assertFalse(talk.needs_slot)
        real_get, real_award = catalog.get, self.server.award_exp
        held = []

        def award(session, amount, source, **kw):
            held.append(self.server._combat_lock(session)._is_owned())
            return real_award(session, amount, source, **kw)

        with mock.patch.object(catalog, 'get', side_effect=lambda i, *a: talk if int(i) == 158 else real_get(i, *a)), \
                mock.patch.object(self.server, 'award_exp', side_effect=award):
            before = E.grants(c.session)
            self.assertTrue(self.server.events.push_quest(c.session, 158, 'test'))
        pkts = c.expect(0x26, 0x21)
        self.assertEqual(c.s2c(pkts[0]), {'quest_id': 158})
        self.assertEqual(struct.unpack_from('<i', pkts[1].payload)[0], 50)
        self.assertEqual((held, E.grants(c.session)), ([True], before + 1))
        self.assertEqual(self.state().active, [0, 0, 0])                   # no slot used

    def test_an_abandoned_event_quest_comes_back_at_the_next_login(self):
        c = self.enter()
        self.resync(c, 0x15, 0x99, 0x26, 0x59, 0x80, first=True)
        c.send_c2s(self.keys['abandon'], {'quest_id': 158})
        c.expect(0x38)
        self.mono.t += 60
        c = self.relog(c)
        self.pushed(c, self.resync(c, 0x26, 0x59, first=True), 158)

    def test_a_0x16_for_the_chain_is_a_push_never_a_warning(self):
        """evb Q4: the Accept of quest 158's Finish window sends some 0x16 - whichever id it
        carries, the answer is the push or nothing, never the P2 'SNPC 0' refusal."""
        c = self.enter()
        self.resync(c, 0x15, 0x99, 0x26, 0x59, 0x80, first=True)
        c.send_c2s(self.keys['accept'], {'quest_id': 158})                 # held
        c.expect_silence()
        self.give(BLUE_MUSHROOM, 20)
        self.turn_in(c, 158, 0x27, 0x21, 0x3F, 0x26, 0x59)
        for quest_id in (158, 159, 160):                                    # done / held / PrevQuest 159 open
            c.send_c2s(self.keys['accept'], {'quest_id': quest_id})
            c.expect_silence()
        self.assertEqual(self.state().active, [159, 0, 0])
        c.send_c2s(self.keys['abandon'], {'quest_id': 159})
        c.expect(0x38)
        c.send_c2s(self.keys['accept'], {'quest_id': 159})
        self.pushed(c, c.expect(0x26, 0x59), 159)
        # outside a running event a 0x16 for an SNPC-0 quest is the P2 refusal (silent too)
        self.server.events.dev_event(c.session, 'stop p13-exit')
        c.expect(0x15)
        c.send_c2s(self.keys['abandon'], {'quest_id': 159})
        c.expect(0x38)
        c.send_c2s(self.keys['accept'], {'quest_id': 159})
        c.expect_silence()
        self.assertEqual(self.state().active, [0, 0, 0])

    def test_korean_text_quests_are_pushed_only_when_configured(self):
        korean = event(QUESTS_ONLY, id='kr', push_quests=[KOREAN_240])
        self.make([korean])
        c = self.enter()
        with self.assertLogs('WS', logging.INFO) as logs:
            self.assertEqual(self.resync(c, first=True), [])
        self.assertTrue(any('quest 240 not pushed' in line and 'Korean title' in line for line in logs.output))
        self.make([korean], {'EVENT_QUEST_KOREAN_TEXT': True})
        c = self.enter()
        self.pushed(c, self.resync(c, 0x26, 0x59, first=True), KOREAN_240)

    def test_two_clients_each_get_the_quest_once(self):
        """P13 exit criterion 2: A (TestHero) and B (Bob) each see the quest once."""
        a, b = self.enter(), self.enter('admin')
        for c, user in ((a, 'test'), (b, 'admin')):
            tail = self.resync(c, 0x15, 0x99, 0x26, 0x59, 0x80, first=True, user=user)
            self.pushed(c, tail[2:4], 158)
            self.assertEqual(self.state(user).active, [158, 0, 0])
        self.assertEqual(self.server.events.tick(), 0)
        b = self.relog(b, 'admin')
        self.assertEqual(self.resync(b, first=True, user='admin'), [])
        a.expect_silence(0.2)

    def test_an_event_started_mid_session_pushes_on_the_tick(self):
        self.make([event(QUESTS_ONLY, enabled=False)])
        c = self.enter()
        self.assertEqual(self.resync(c, first=True), [])
        self.server.events.dev_event(c.session, 'start quests')
        c.expect(0x15)
        self.assertEqual(self.server.events.tick(), 1)
        self.pushed(c, c.expect(0x26, 0x59), 158)
        self.assertEqual(self.server.events.tick(), 0)


@needs_2008
class EventQuests2008(Rig):
    """The 2008 hqi has no English event quest: the shipped event pushes nothing, and a
    configured Korean one runs the same loop."""
    build = B8

    def test_the_exit_event_pushes_nothing_and_says_why_at_start(self):
        with self.assertLogs('WS', logging.WARNING) as logs:
            self.make(self.events)
        self.assertTrue(any('quest 158 is never pushed on the 2008 client' in line and 'ENPC 0' in line
                            for line in logs.output))
        c = self.enter()
        self.assertEqual([p.opcode for p in self.resync(c, 0x15, 0x99, 0x80, first=True)], [0x15, 0x99, 0x80])
        self.assertEqual(self.state().active, [0, 0, 0])
        c.send_c2s(self.keys['accept'], {'quest_id': 158})                # SNPC 0, not pushable: silent
        c.expect_silence()

    def test_a_configured_korean_quest_runs_end_to_end(self):
        korean = event(QUESTS_ONLY, id='kr', push_quests=[KOREAN_240])
        self.make([korean])
        c = self.enter()
        self.assertEqual(self.resync(c, first=True), [])                   # off by default
        self.make([korean], {'EVENT_QUEST_KOREAN_TEXT': True})
        c = self.enter()
        self.pushed(c, self.resync(c, 0x26, 0x59, first=True), KOREAN_240)
        self.turn_in(c, KOREAN_240)                                         # no Golden Pig: nothing
        self.give(GOLDEN_PIG, 20)
        total = self.char()['exp']
        pkts = self.turn_in(c, KOREAN_240, 0x27, 0x21)                     # Money 0: no 0x3F; no NextQuest
        self.assertEqual(c.s2c(pkts[0]), {'quest_id': KOREAN_240})
        self.assertEqual(struct.unpack('<i', pkts[1].payload)[0], 500)
        self.assertEqual(self.char()['exp'], total + 500)
        self.assertEqual((self.bag().count(GOLDEN_PIG), self.bag().count(ELIXIR), self.bag().count(ENERGIZER)),
                         (0, 10, 10))
        self.assertEqual(self.state().times(KOREAN_240), 1)
        # Repeat 99: the next login offers it again (the event is still on)
        self.mono.t += 60
        c = self.relog(c)
        self.pushed(c, self.resync(c, 0x26, 0x59, first=True), KOREAN_240)


if __name__ == '__main__':
    unittest.main()
