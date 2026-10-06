#!/usr/bin/env python3
"""
test_bosscombat.py - P13 stage 4 (boss-combat-quests: boss-b3, boss-b4), offline, both builds
=============================================================================================
Design re_tools/docs/systems_2009/events_bosses.md B3, B4, B7, B8, B10, E-B3/E-B4; P13 exit
criteria 6 (the Monkey King at Ascetic Quarter 409 uses attack A, attack B and the dash, seen
on both clients; Monkey Lord attack A only) and 7 (quest 64: kill the King, pick up 1291, turn
in for the Yellow Stripe Hat and +100 exp; quest 182 gets its 0x59 from the kill).

- the rules (bosses.command_flags / swing_age / boss_quests): the decision table of template 81
  (dash out of reach, attack A or B in reach, jump), Monkey Lord (hni 140) attack A only in
  every mode, the MOB_ATTACK_B / MOB_DASH scopes 'all' / 'bosses' / 'off', the swing kind of
  every victim event, and the boss quests of both hqi (kill 181/182/184/165/205/215, trophy
  6/64/104/137/144/210) with the client's own first-slot kill credit;
- the server through two fake clients per build on map 409: the King's 0x2A words (exact
  16-byte self-forms, per receiver) reach both clients - dash 19/1A, attack A 05/06, attack B
  15/16 - and Monkey Lord's never carry B or the dash; the config switches; a victim's swing
  event hurts with Weak_Atk (7/8) or Strong_Atk (4/5/9/10) only after the server commanded
  that kind (both DAMAGE_FORMULAs); the King's kill sends 0x59 for quest 182 and drops 1291
  owned by the killer, whose pickup re-checks quest 64 and whose C2S 0x17 turns it in (0x27 +
  the 0x21 of award_exp); `!boss quests` / `!boss quest <id>`.

No port is bound, no client is started and the live accounts.json is never opened (temp
copies; the module checks its hash at the end).
"""
import hashlib
import json
import logging
import os
import shutil
import struct
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import bosses as B  # noqa: E402
import config as cfgmod  # noqa: E402
import damage as dmgmod  # noqa: E402
import en_content as EC  # noqa: E402
import fakeclient as F  # noqa: E402
import inventory as INV  # noqa: E402
import mobai as AI  # noqa: E402
import packets as P  # noqa: E402
import quests as Q  # noqa: E402

W = F.import_server()
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = '2008', '2009'
DIR8 = os.path.join(HERE, cfgmod.DEFAULTS['CLIENT_DIR'])
DIR9 = os.path.join(HERE, cfgmod.DEFAULTS['CLIENT_DIR_2009'])
HAVE = {B8: os.path.exists(os.path.join(DIR8, 'hs', 'windslayer.hni')),
        B9: os.path.exists(os.path.join(DIR9, 'hs', 'windslayer.hni'))}

RYNX, MONKEY_KING, WASABLANCA, MONKEY_LORD = 12, 81, 95, 140
DIADEM, YELLOW_HAT = 1291, 1290
MAP = 409                                           # Ascetic Quarter: the King + 3 Monkey Lords
# The King's value-300 tile and spawn point, and the Monkey Lord at tile (2800, 1287), per build
# (evb B2; the 2008 map puts the King 300 px further right).
KING_AT = {B9: (2550.0, 1509.0), B8: (2750.0, 1509.0)}
LORD_AT = (2850.0, 1307.0)
KEYS = {
    B8: {'move': '0x42CE94/0x0D', 'pickup': '0x43D80F/0x1F', 'chat': '0x445CA7/0x03',
         'turn_in': '0x477F3E/0x17'},
    B9: {'move': '0x42E704/0x0D', 'pickup': '0x43DA4E/0x1F', 'chat': '0x44790E/0x03',
         'turn_in': '0x48A95A/0x17'},
}
WALK_L, WALK_R, JUMP_L = 0x01, 0x02, 0x0D
ATK_A_L, ATK_A_R, ATK_B_L, ATK_B_R, DASH_L, DASH_R = 0x05, 0x06, 0x15, 0x16, 0x19, 0x1A
KILL_QUESTS = {181: (RYNX, 1), 182: (MONKEY_KING, 1), 184: (WASABLANCA, 1), 165: (WASABLANCA, 2),
               205: (WASABLANCA, 2), 215: (WASABLANCA, 2)}
TROPHY_QUESTS = {6: (124,), 64: (DIADEM,), 104: (1559, 1582), 137: (167,), 144: (168,), 210: (1582,)}
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
            'accounts.json changed during test_bosscombat.py (tests must only use temp copies)'


def use_build(build):
    EC.configure(cfgmod.DEFAULTS['CLIENT_DIR_2009'] if build == B9 else cfgmod.DEFAULTS['CLIENT_DIR'], build)


def builds():
    return [b for b in (B8, B9) if HAVE[b]]


def stand_in(npc, boss=True):
    """A mob-like object with the template's flags and a ledger key (or none)."""
    tpl = EC.npcs().get(npc)
    return types.SimpleNamespace(flags=AI.Flags.of(tpl.ai), boss=(1, MAP, (0, 0)) if boss else None,
                                 ai_attack_a_t=0.0, ai_attack_b_t=0.0)


def cfg(**over):
    return cfgmod.from_dict(over)


# ===================================================================== rules ===
class Config(unittest.TestCase):
    def test_defaults_validation_and_the_shipped_file(self):
        d = cfgmod.defaults()
        self.assertEqual((d.MOB_ATTACK_B, d.MOB_DASH), ('all', 'all'))
        self.assertEqual(cfgmod.MOB_COMMAND_SCOPES, ('all', 'bosses', 'off'))
        for scope in cfgmod.MOB_COMMAND_SCOPES:
            self.assertEqual(cfg(MOB_ATTACK_B=scope, MOB_DASH=scope).MOB_DASH, scope)
        for bad in ({'MOB_ATTACK_B': 'none'}, {'MOB_DASH': True}, {'MOB_DASH': 'Bosses'}, {'MOB_ATTACK_B': 0}):
            with self.subTest(bad), self.assertRaises(cfgmod.ConfigError):
                cfgmod.from_dict(bad)
        with open(os.path.join(HERE, 'config.json'), encoding='utf-8') as f:
            shipped = json.load(f)
        self.assertEqual((shipped['MOB_ATTACK_B'], shipped['MOB_DASH']), ('all', 'all'))
        # boss-b5 / boss-b6 are not implemented: no config key pretends otherwise
        self.assertFalse({'BOSS_ANNOUNCE', 'MOB_PACK_ASSIST'} & set(cfgmod.DEFAULTS))


class CombatRules(unittest.TestCase):
    """boss-b3 over the real templates of both hni (evb B3 / B8, E-B3 'the decision table for
    template 81')."""

    def tearDown(self):
        use_build(B8)

    def decide(self, flags, mob_xy, player_xy, ai_dir=AI.DIR_LEFT, rng_bit=1, prev=None):
        d = AI.decide(mob_xy, player_xy, ai_dir, flags, rng_bit=rng_bit, prev_motion=prev)
        return AI.lo(d.direction, d.motion)

    def test_the_templates(self):
        for build in builds():
            with self.subTest(build=build):
                use_build(build)
                npcs = EC.npcs()
                king, lord = npcs.get(MONKEY_KING), npcs.get(MONKEY_LORD)
                self.assertEqual(AI.Flags.of(king.ai), AI.Flags(True, True, False, True, False, True, False))
                self.assertEqual((king.lv, king.hp, king.body_atk, king.weak_atk, king.strong_atk),
                                 (15, 288, 29, 24, 35))
                self.assertEqual(AI.Flags.of(lord.ai), AI.Flags(True, False, False, True, False, False, False))
                self.assertEqual((lord.lv, lord.weak_atk, lord.strong_atk), (12, 22, 0))

    def test_the_decision_table_of_template_81(self):
        for build in builds():
            use_build(build)
            full = B.command_flags(stand_in(MONKEY_KING), cfg())
            with self.subTest(build=build):
                self.assertEqual(self.decide(full, (1244, 714), (1000, 714)), DASH_L)       # out of reach
                self.assertEqual(self.decide(full, (756, 714), (1000, 714)), DASH_R)
                self.assertEqual(self.decide(full, (1040, 714), (1000, 714), rng_bit=1), ATK_A_L)
                self.assertEqual(self.decide(full, (1040, 714), (1000, 714), rng_bit=0), ATK_B_L)
                self.assertEqual(self.decide(full, (960, 714), (1000, 714), AI.DIR_RIGHT, rng_bit=0), ATK_B_R)
                self.assertEqual(self.decide(full, (1040, 714), (1000, 714), rng_bit=0,
                                             prev=AI.MOTION_ATTACK_A), ATK_A_L)                # keeps its kind
                self.assertEqual(self.decide(full, (1244, 714), (1000, 600)), JUMP_L)       # a jump wins
                # attack B off: A whatever the rng; dash off: a walk
                no_b = B.command_flags(stand_in(MONKEY_KING), cfg(MOB_ATTACK_B='off'))
                self.assertEqual(self.decide(no_b, (1040, 714), (1000, 714), rng_bit=0), ATK_A_L)
                self.assertEqual(self.decide(no_b, (1040, 714), (1000, 714), rng_bit=0,
                                             prev=AI.MOTION_ATTACK_B), ATK_A_L)
                no_dash = B.command_flags(stand_in(MONKEY_KING), cfg(MOB_DASH='off'))
                self.assertEqual(self.decide(no_dash, (1244, 714), (1000, 714)), WALK_L)
                self.assertEqual(self.decide(no_dash, (1040, 714), (1000, 714), rng_bit=0), ATK_B_L)

    def test_monkey_lord_swings_attack_a_only_in_every_mode(self):
        for build in builds():
            use_build(build)
            for scope in cfgmod.MOB_COMMAND_SCOPES:
                for boss in (True, False):
                    flags = B.command_flags(stand_in(MONKEY_LORD, boss), cfg(MOB_ATTACK_B=scope, MOB_DASH=scope))
                    with self.subTest(build=build, scope=scope, boss=boss):
                        self.assertEqual((flags.attack_a, flags.attack_b, flags.skill), (True, False, False))
                        words = {self.decide(flags, mob, (1000, 714), rng_bit=r, prev=p)
                                 for mob in ((1040, 714), (1244, 714), (1100, 714))
                                 for r in (0, 1) for p in (None, AI.MOTION_ATTACK_B)}
                        self.assertEqual(words, {ATK_A_L, WALK_L})

    def test_the_scopes(self):
        use_build(builds()[-1])
        king, regular = stand_in(MONKEY_KING), stand_in(MONKEY_KING, boss=False)
        table = {('all', True): True, ('all', False): True, ('bosses', True): True,
                 ('bosses', False): False, ('off', True): False, ('off', False): False}
        for (scope, is_boss), want in table.items():
            mob = king if is_boss else regular
            with self.subTest(scope=scope, boss=is_boss):
                self.assertEqual(B.scope_allows(scope, mob), want)
                b = B.command_flags(mob, cfg(MOB_ATTACK_B=scope))
                d = B.command_flags(mob, cfg(MOB_DASH=scope))
                self.assertEqual((b.attack_b, b.skill, b.attack_a), (want, True, True))
                self.assertEqual((d.skill, d.attack_b, d.attack_a), (want, True, True))
        self.assertIs(B.command_flags(king, cfg()), king.flags)            # nothing masked: as is

    def test_swing_kinds_and_ages(self):
        self.assertEqual({a: B.swing_kind(a) for a in range(1, 12)},
                         {1: None, 2: None, 3: None, 4: 'B', 5: 'B', 6: None, 7: 'A', 8: 'A', 9: 'B',
                          10: 'B', 11: None})
        self.assertEqual(B.SWING_A_EVENTS | B.SWING_B_EVENTS, W.GameServer.MOB_SWING_EVENTS)
        for a in B.SWING_A_EVENTS:
            self.assertEqual(W.GameServer.MOB_HIT_STATS[a], 'weak_atk')
        for a in B.SWING_B_EVENTS:
            self.assertEqual(W.GameServer.MOB_HIT_STATS[a], 'strong_atk')
        mob = types.SimpleNamespace(ai_attack_a_t=0.0, ai_attack_b_t=0.0)
        self.assertEqual(B.swing_age(mob, 7, 100.0), float('inf'))
        for lo in (WALK_L, DASH_R, JUMP_L, AI.STOP):
            B.note_command(mob, lo, 50.0)
        self.assertEqual((mob.ai_attack_a_t, mob.ai_attack_b_t), (0.0, 0.0))
        B.note_command(mob, ATK_A_R, 90.0)
        self.assertEqual((B.swing_age(mob, 7, 100.0), B.swing_age(mob, 9, 100.0)), (10.0, float('inf')))
        B.note_command(mob, ATK_B_L, 99.0)
        self.assertEqual((B.swing_age(mob, 8, 100.0), B.swing_age(mob, 4, 100.0)), (10.0, 1.0))
        self.assertEqual(B.swing_age(mob, 6, 100.0), float('inf'))       # contact is no swing
        AI.clear(mob)
        self.assertEqual((mob.ai_attack_a_t, mob.ai_attack_b_t), (0.0, 0.0))


class QuestRules(unittest.TestCase):
    """boss-b4: the boss quests of both hqi and the client's own first-slot kill credit."""

    def tearDown(self):
        use_build(B8)

    def test_the_boss_quests_of_both_builds(self):
        for build in builds():
            with self.subTest(build=build):
                use_build(build)
                rows = B.boss_quests()
                kill = {b.quest: (b.npc, b.need) for b in rows if b.kind == 'kill'}
                trophy = {b.quest: b.items for b in rows if b.kind == 'trophy'}
                if build == B9:
                    self.assertEqual(kill, KILL_QUESTS)
                    self.assertEqual(trophy, TROPHY_QUESTS)
                else:                                    # 2008 adds the Atomic Ball of map 709
                    self.assertEqual(kill, {**KILL_QUESTS, 183: (94, 1)})
                    self.assertEqual(trophy, {**TROPHY_QUESTS, 102: (1558,)})
                self.assertEqual([b.quest for b in rows], sorted(b.quest for b in rows))
                q64 = EC.quests().get(64)
                self.assertEqual((q64.snpc, q64.enpc, q64.demand, q64.reward, q64.exp),
                                 (21, 21, [(DIADEM, 1)], [(YELLOW_HAT, 1)], 100))
                self.assertIn((DIADEM, 99990), EC.npcs().get(MONKEY_KING).drops)

    def test_the_first_slot_that_wants_the_boss_gets_the_kill(self):
        for build in builds():
            use_build(build)
            catalog = EC.quests()
            for quest, (npc, need) in KILL_QUESTS.items():
                with self.subTest(build=build, quest=quest):
                    st = Q.QuestState({})
                    st.accept(quest)
                    self.assertIsNone(st.credit_kill(MONKEY_LORD, catalog))    # a regular monkey
                    for k in range(1, need + 1):
                        self.assertEqual(st.credit_kill(npc, catalog), (0, k))
                    self.assertIsNone(st.credit_kill(npc, catalog))            # full: ReqPro reached
            # two quests on Wasablanca: 184 (x1) in slot 1 fills first, then 215 (x2) in slot 2
            st = Q.QuestState({})
            st.accept(184)
            st.accept(215)
            got = [st.credit_kill(WASABLANCA, catalog) for _ in range(4)]
            self.assertEqual(got, [(0, 1), (1, 1), (1, 2), None])


# ============================================================== server flows ===
def accounts(build):
    x, y = KING_AT[build]
    return {
        'test': {'password': 'test', 'characters': [
            {'name': 'TestHero', 'level': 40, 'class': 4, 'map': MAP, 'x': x - 60, 'y': y,
             'hp': 9999, 'mp': 50, 'gm': 1,
             'quests': {'active': [64, 182, 0], 'progress': [0, 0, 0], 'completed': [[181, 1]]}}]},
        'admin': {'password': 'admin', 'characters': [
            {'name': 'Watcher', 'level': 40, 'class': 0, 'map': MAP, 'x': x + 60, 'y': y,
             'hp': 9999, 'mp': 50}]},
    }


class Rig(unittest.TestCase):
    build = B9
    overrides = {}

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        use_build(self.build)
        self.tmp = tempfile.mkdtemp(prefix=f'ws_bosscombat{self.build}_')
        self.keys = KEYS[self.build]
        self.clients = []
        self.server = F.make_server(self.tmp, accounts=accounts(self.build), config=self.config(),
                                    ground_loot=None, grade_roll=None, drop_mode=None)

    def config(self, **over):
        return cfgmod.from_dict({'CLIENT_BUILD': self.build, 'MOB_AGGRO': True, 'GROUND_LOOT': True,
                                 'DAMAGE_GRADE_ROLL': False, 'MOB_CONTACT_MIN_SECS': 0,
                                 **self.overrides, **over})

    def reconfigure(self, **over):
        self.server.config = self.config(**over)

    def tearDown(self):
        for c in self.clients:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        use_build(B8)

    # ------------------------------------------------------------ helpers ---
    def enter(self, user='test', password='test', name='TestHero'):
        c = F.FakeClient(self.server)
        self.clients.append(c)
        self.assertEqual(c.login(user, password)['result'], 1)
        c.enter_world(name, port=F.P2P_PORT_BASE + len(self.clients) - 1)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world') and s.get('current_map') == MAP))
        for o in self.clients:
            if o is not c:
                o.recv_until_quiet(0.1)
        return c

    def two(self):
        a = self.enter()
        b = self.enter('admin', 'admin', 'Watcher')
        a.recv_until_quiet(0.1)
        with self.server._combat_lock(a.session):
            a.session['hp'] = 5000                  # room for every swing of the damage test
        return a, b

    def mons(self):
        return self.server.world.map(MAP).monsters

    def mob(self, npc):
        found = [m for m in self.mons().values() if m.npccode == npc and m.alive]
        self.assertTrue(found, npc)
        return found[0]

    def uid(self, c):
        return P.session_uid(c.session)

    def move(self, c, state_lo, **extra):
        c.send_c2s(self.keys['move'], {'realtime_delta_ms': 0, 'map_code': MAP, 'logic_elapsed_ms': 30,
                                       'state_lo': state_lo, 'state_hi': 0, **extra})

    def report_hit(self, c, mob):
        """The attacker's 61 B C2S 0x0D after his own client caught a swing on `mob`."""
        px, py = c.session['pos']
        self.move(c, W.HIT_REPORT_EVENT << 16, target_uid=mob.uid, pos_x=float(px), pos_y=float(py),
                  target_dx=float(mob.x - px), target_dy=float(mob.y - py), flag_8db=0, flag_8e7=0,
                  timer_dac=0, target_action_event=7)

    def provoke(self, a, b, mob):
        """A's surviving reported hit: the 0x2A release to A, the hit relay to B; the mob now
        chases A."""
        with self.server._combat(a.session):
            mob.hp = mob.max_hp = 5000
        self.report_hit(a, mob)
        self.assertEqual(a.expect(0x2A).payload, self.cmd(mob, AI.STOP, self.uid(a)))
        b.recv_until_quiet(0.1)
        self.assertEqual(mob.aggro_uid, self.uid(a))

    @staticmethod
    def recovered(mob):
        """The first moment a chase word may follow the provoking hit (desync fix M1: no word
        before MOB_HIT_RECOVER_SECS after a client-caught basic swing, Monster.ai_recover_until;
        boss words too); now when no gate runs."""
        return max(time.monotonic(), mob.ai_recover_until)

    @staticmethod
    def cmd(mob, lo, receiver):
        """The 16-byte 0x2A self-form {mob, lo, hi 0, the receiver's own uid}."""
        return struct.pack('<IIII', mob.uid, lo, 0, receiver)

    def step(self, a, b, mob, now, mob_x, rng_bit=1):
        """Pin the mob at (A.x + mob_x offset) on A's floor, tick the AI at `now` and return the
        lo word both clients got (each its own 16-byte self-form)."""
        for c in (a, b):
            c.recv_until_quiet(0.05)            # A's own moves reach B as the 0x1B relay
        px, py = a.session['pos']
        with self.server._combat(a.session):
            mob.x, mob.y, mob.fix_t = float(px + mob_x), float(py), now
        with mock.patch.object(self.server, '_mob_rng_bit', return_value=rng_bit):
            n = self.server._tick_monster_ai(now)
        got = {c: [(p.opcode, p.payload) for p in c.recv_until_quiet(0.08)] for c in (a, b)}
        self.assertEqual(n, 2, got)
        words = set()
        for c in (a, b):
            self.assertEqual(len(got[c]), 1, got[c])
            op, payload = got[c][0]
            self.assertEqual((op, len(payload)), (0x2A, 16))
            lo = struct.unpack_from('<I', payload, 4)[0]
            self.assertEqual(payload, self.cmd(mob, lo, self.uid(c)))
            self.assertEqual(c.s2c(F.Packet(op, payload, True, 0)),
                             {'mover_uid': mob.uid, 'move_bits': struct.pack('<II', lo, 0),
                              'target_uid': self.uid(c)})
            words.add(lo)
        self.assertEqual(len(words), 1)
        return words.pop()

    def struck(self, a, mob, action, taken):
        """A's own client reports `mob` hitting him with victim event `action`; `taken` = the HP
        lost (None: nothing happens)."""
        with self.server._combat_lock(a.session):
            a.session.pop('contact_t', None)
            a.session.pop('swing_t', None)
            hp = a.session['hp']
        self.move(a, action << 12, event_source_uid=mob.uid)
        if taken is None:
            a.expect_silence(0.15)
            self.assertEqual(a.session['hp'], hp)
            return
        self.assertEqual(a.s2c(a.expect(0x28, quiet=0.1)), {'hp': hp - taken})

    def gm(self, c, line):
        c.send_c2s(self.keys['chat'], {'msg_len': len(line), 'message': line})
        first = c.recv(10.0)                        # the first boss_quests scans every map
        pkts = ([first] if first is not None else []) + c.recv_until_quiet(0.3)
        texts = [c.s2c(p)['text'] for p in pkts if p.opcode == 0x15]
        return [t.decode('latin-1') if isinstance(t, bytes) else t for t in texts], pkts

    def state(self, user='test'):
        return Q.QuestState(self.server.store.characters(user)[0])

    def bag(self, user='test'):
        return INV.Inventory(self.server.store.characters(user)[0])


class CombatFlows:
    """boss-b3, run for each build by the concrete classes below."""

    def test_the_king_dashes_and_swings_a_and_b_on_both_clients(self):
        a, b = self.two()
        king = self.mob(MONKEY_KING)
        self.assertIsNotNone(king.boss)
        self.provoke(a, b, king)
        T = self.recovered(king)
        seq = [(244, 1, DASH_L),            # out of reach: the dash (AI[3]) instead of the walk
               (40, 1, ATK_A_L),            # in reach, rng bit 1: attack A (AI[0])
               (100, 0, DASH_L),            # out of reach again
               (40, 0, ATK_B_L),            # rng bit 0: attack B (AI[8])
               (-244, 0, DASH_R),           # passed him: turns and dashes back
               (-40, 1, ATK_A_R),
               (-100, 1, DASH_R),
               (-40, 0, ATK_B_R)]
        for k, (dx, rng, want) in enumerate(seq):
            with self.subTest(dx=dx, rng=rng):
                self.assertEqual(self.step(a, b, king, T + 0.7 * k, dx, rng), want)
        self.assertEqual(king.ai_attack_b_t, T + 0.7 * 7)
        self.assertEqual(king.ai_attack_a_t, T + 0.7 * 5)

    def test_monkey_lord_swings_attack_a_only_on_both_clients(self):
        a, b = self.two()
        lord = self.mob(MONKEY_LORD)
        self.assertIsNone(lord.boss)
        self.provoke(a, b, lord)
        T = self.recovered(lord)
        seq = [(244, 0, WALK_L), (40, 0, ATK_A_L), (100, 0, WALK_L), (40, 1, ATK_A_L), (-244, 0, WALK_R),
               (-40, 0, ATK_A_R)]
        for k, (dx, rng, want) in enumerate(seq):
            with self.subTest(dx=dx, rng=rng):
                self.assertEqual(self.step(a, b, lord, T + 0.7 * k, dx, rng), want)
        self.assertEqual(lord.ai_attack_b_t, 0.0)

    def test_the_config_switches(self):
        self.reconfigure(MOB_ATTACK_B='off', MOB_DASH='off')
        a, b = self.two()
        king = self.mob(MONKEY_KING)
        self.provoke(a, b, king)
        T = self.recovered(king)
        self.assertEqual(self.step(a, b, king, T, 244, 0), WALK_L)
        self.assertEqual(self.step(a, b, king, T + 0.7, 40, 0), ATK_A_L)
        # 'bosses': the King (a field boss) gets both words back ...
        self.reconfigure(MOB_ATTACK_B='bosses', MOB_DASH='bosses')
        self.assertEqual(self.step(a, b, king, T + 1.4, 244, 0), DASH_L)
        self.assertEqual(self.step(a, b, king, T + 2.1, 40, 0), ATK_B_L)
        # ... a regular monster with the same flags does not
        with self.server._combat(a.session):
            king.boss = None
        self.assertEqual(self.step(a, b, king, T + 2.8, 244, 0), WALK_L)
        self.assertEqual(self.step(a, b, king, T + 3.5, 40, 0), ATK_A_L)

    def test_weak_and_strong_damage_follow_the_event_and_the_commanded_kind(self):
        self.reconfigure(DAMAGE_FORMULA='placeholder')      # Weak_Atk / Strong_Atk - Def 0
        a, b = self.two()
        king = self.mob(MONKEY_KING)
        self.assertEqual((king.body_atk, king.weak_atk, king.strong_atk), (29, 24, 35))
        self.struck(a, king, 7, None)                               # not after him yet
        self.provoke(a, b, king)
        self.struck(a, king, 6, 29)                                 # body contact
        for action in (7, 9):
            self.struck(a, king, action, None)                      # no swing commanded yet
        self.assertEqual(self.step(a, b, king, self.recovered(king), 40, 1), ATK_A_L)
        self.struck(a, king, 7, 24)                                 # attack A: Weak_Atk
        self.struck(a, king, 8, 24)                                 # ... on an airborne victim
        for action in (9, 10, 4, 5):                                # attack B never commanded
            self.struck(a, king, action, None)
        for action in (2, 3):
            self.struck(a, king, action, None)                      # attack A into a guard
        self.assertEqual(self.step(a, b, king, time.monotonic(), 100, 0), DASH_L)
        self.assertEqual(self.step(a, b, king, time.monotonic(), 40, 0), ATK_B_L)
        for action in (9, 10, 4, 5):
            self.struck(a, king, action, 35)                        # attack B: Strong_Atk
        # each kind has its own window (MOB_ATTACK_EVENT_SECS): an attack A 1 s ago still hurts
        # after the attack B command, one past the window no longer does, whatever B's age
        window = W.GameServer.MOB_ATTACK_EVENT_SECS
        with self.server._combat(a.session):
            king.ai_attack_a_t = king.ai_attack_b_t = time.monotonic() - 1.0
        self.struck(a, king, 7, 24)
        self.struck(a, king, 9, 35)
        with self.server._combat(a.session):
            king.ai_attack_a_t = time.monotonic() - window - 0.1
            king.ai_attack_b_t = time.monotonic() - 1.0
        self.struck(a, king, 7, None)
        self.struck(a, king, 9, 35)
        with self.server._combat(a.session):
            king.ai_attack_b_t = time.monotonic() - window - 0.1
        self.struck(a, king, 9, None)
        # the client's own formula: the same events pick the same stats
        self.reconfigure(DAMAGE_FORMULA='client')
        with self.server._combat(a.session):
            now = time.monotonic()
            king.ai_attack_a_t = king.ai_attack_b_t = king.ai_attack_t = now
            hp = a.session['hp']
        att, vic = dmgmod.monster_stats(king), self.server._player_stats(a.session)
        weak = dmgmod.damage(att, vic, 7, cap=hp).total
        strong = dmgmod.damage(att, vic, 9, cap=hp - weak).total
        self.assertLess(weak, strong)
        self.struck(a, king, 7, weak)
        self.struck(a, king, 9, strong)

    def test_monkey_lord_strong_events_never_hurt(self):
        self.reconfigure(DAMAGE_FORMULA='placeholder')
        a, b = self.two()
        lord = self.mob(MONKEY_LORD)
        self.provoke(a, b, lord)
        self.assertEqual(self.step(a, b, lord, self.recovered(lord), 40, 0), ATK_A_L)
        for action in (9, 10, 4, 5):
            self.struck(a, lord, action, None)
        self.struck(a, lord, 7, 22)

    def test_the_livefix_gates_cover_the_boss_words(self):
        """The livefix merge: desync fix M1 holds EVERY chase word of the King - attack B and
        the dash too - until MOB_HIT_RECOVER_SECS after the provoking basic swing; the timed
        reach-edge hold (MOB_ATTACK_HOLD_X for MOB_ATTACK_HOLD_SECS) keeps attack B as it keeps
        A, then the plain box decides; body contact while attack B runs hurts through the
        contact slot (P7 live L2) next to the per-kind swing gate; and the King's dead
        reckoning uses its template walk speed (desync fix P3: hni speed 80000 -> 125 px/s)."""
        self.reconfigure(DAMAGE_FORMULA='placeholder')      # Body 29 / Strong 35 - Def 0
        a, b = self.two()
        king = self.mob(MONKEY_KING)
        self.assertEqual(king.walk_px_s, 125.0)
        self.assertEqual(self.server._mob_walk_px_s(king), 125.0)
        self.provoke(a, b, king)
        T = king.ai_recover_until
        self.assertGreater(T, 0.0)                          # the provoking hit set the gate
        px, py = a.session['pos']
        for dx in (40, 244):                                # attack B / the dash would be decided
            with self.subTest(gated=dx):
                with self.server._combat(a.session):
                    king.x, king.y, king.fix_t = float(px + dx), float(py), T - 0.1
                with mock.patch.object(self.server, '_mob_rng_bit', return_value=0):
                    self.assertEqual(self.server._tick_monster_ai(T - 0.1), 0)
                for c in (a, b):
                    self.assertEqual([p.payload for p in c.recv_until_quiet(0.08)
                                      if p.opcode == 0x2A], [])
        self.assertEqual(self.step(a, b, king, T, 40, 0), ATK_B_L)
        self.assertEqual(king.ai_attack_start_t, T)
        # 75 px: past the 60 px reach, inside reach + hold 30 - attack B holds (rng 1 would
        # pick A for a new swing) and its keep-alive is no new swing ...
        self.assertEqual(self.step(a, b, king, T + 0.7, 75, 1), ATK_B_L)
        self.assertEqual(king.ai_attack_start_t, T)
        # ... contact while it runs hurts (the contact slot), and so does its swing (the B kind)
        self.struck(a, king, 6, 29)
        self.struck(a, king, 9, 35)
        # ... until MOB_ATTACK_HOLD_SECS after the swing began: out of reach, the dash
        self.assertEqual(self.step(a, b, king, T + 1.4, 75, 1), DASH_L)


class QuestFlows:
    """boss-b4, run for each build by the concrete classes below."""

    def test_the_kings_kill_feeds_182_and_its_trophy_turns_in_64(self):
        a, b = self.two()
        uid_a = self.uid(a)
        king = self.mob(MONKEY_KING)
        self.assertEqual(self.state().active, [64, 182, 0])
        with self.server._combat(a.session):
            king.hp = 1
        with mock.patch.object(W.random, 'random', return_value=0.5):       # the trophy, no card
            self.report_hit(a, king)
            pkts = a.recv_until_quiet(0.3)
        self.assertFalse(king.alive)
        ops = [p.opcode for p in pkts]
        self.assertEqual([op for op in ops if op in (0x29, 0x21, 0x59, 0x12, 0x18)], [0x29, 0x21, 0x59, 0x12, 0x18])
        got = {p.opcode: a.s2c(p) for p in pkts}
        self.assertEqual(got[0x29]['uid'], king.uid)
        self.assertEqual(got[0x21]['exp_delta'], king.exp)                   # the King's Exp
        self.assertEqual(king.exp, 282 if self.build == B9 else 141)
        self.assertEqual(got[0x59], {'slot': 2, 'progress': 1})              # 182: ReqPro 1 reached
        self.assertEqual(self.state().progress, [0, 1, 0])
        entry = got[0x12]['repeat[entry_count]']
        self.assertEqual([(e['item_id'], e['owner_uid'], e['source_uid']) for e in entry], [(DIADEM, uid_a, king.uid)])
        gid = entry[0]['ground_id']
        seen = b.recv_until_quiet(0.2)
        self.assertEqual([b.s2c(p)['uid'] for p in seen if p.opcode == 0x29], [king.uid])
        self.assertNotIn(0x59, [p.opcode for p in seen])                     # only the killer's log
        # B may not take A's trophy; A picks it up: 0x13, then the 0x59 re-check of quest 64
        b.send_c2s(self.keys['pickup'], {'ground_item_uid': gid})
        b.expect_silence(0.2)
        a.send_c2s(self.keys['pickup'], {'ground_item_uid': gid})
        took, recheck = a.expect(0x13, 0x59)
        self.assertEqual(a.s2c(took)['picker_uid'], uid_a)
        self.assertEqual(a.s2c(recheck), {'slot': 1, 'progress': 0})
        b.recv_until_quiet(0.1)
        self.assertEqual(self.bag().count(DIADEM), 1)
        # C2S 0x17 64 at Mei: 0x27, then +100 exp through the EXP pipeline (award_exp 'quest')
        with mock.patch.object(self.server, 'award_exp', wraps=self.server.award_exp) as award:
            a.send_c2s(self.keys['turn_in'], {'quest_id': 64})
            done, exp = a.expect(0x27, 0x21)
        self.assertEqual(a.s2c(done), {'quest_id': 64})
        self.assertEqual(a.s2c(exp)['exp_delta'], 100)
        self.assertEqual([c.args[1:] for c in award.call_args_list], [(100, 'quest')])
        bag = self.bag()
        self.assertEqual((bag.count(DIADEM), bag.count(YELLOW_HAT)), (0, 1))
        # C2S 0x17 182 at Pitsher: 0x27 only (no exp, no item: the job chain goes on)
        a.send_c2s(self.keys['turn_in'], {'quest_id': 182})
        self.assertEqual(a.s2c(a.expect(0x27)), {'quest_id': 182})
        st = self.state()
        self.assertEqual((st.active, st.times(64), st.times(182)), ([0, 0, 0], 1, 1))

    def test_a_kill_quest_needs_the_kill(self):
        a, b = self.two()
        a.send_c2s(self.keys['turn_in'], {'quest_id': 182})                  # progress 0 of 1
        texts = [a.s2c(p)['text'] for p in a.recv_until_quiet(0.2) if p.opcode == 0x15]
        self.assertEqual(len(texts), 1)
        self.assertEqual(self.state().active, [64, 182, 0])
        # a Monkey Lord kill is not the King's
        lord = self.mob(MONKEY_LORD)
        with self.server._combat(a.session):
            lord.hp = 1
        self.report_hit(a, lord)
        self.assertNotIn(0x59, [p.opcode for p in a.recv_until_quiet(0.3)])
        self.assertEqual(self.state().progress, [0, 0, 0])

    def test_gm_boss_quests_and_quest(self):
        a, b = self.two()
        texts, _ = self.gm(a, '!boss quests')
        n = len(B.boss_quests())
        self.assertEqual(texts[0], f'{n} boss quests: {6 + (self.build == B8)} kill, '
                                   f'{6 + (self.build == B8)} trophy')
        self.assertIn('64: 1291 from Monkey King [slot 1: 0]', texts)
        self.assertIn('182: kill Monkey King x1 [slot 2: 0]', texts)
        self.assertIn('181: kill Rynx x1 [done 1x]', texts)
        self.assertIn('215: kill Wasablanca x2', texts)
        self.assertEqual(len(texts), n + 1)
        self.assertTrue(all(len(t) <= 80 for t in texts))
        # !boss quest <id>: 0x26 + 0x59 into the first free slot, no level / job / PrevQuest gate
        texts, pkts = self.gm(a, '!boss quest 215')                          # a Bishop quest, 214 not done
        self.assertEqual([p.opcode for p in pkts], [0x26, 0x59, 0x15])
        self.assertEqual(a.s2c(pkts[0]), {'quest_id': 215})
        self.assertEqual(a.s2c(pkts[1]), {'slot': 3, 'progress': 0})
        self.assertEqual(texts, ['Quest 215 is in your log (slot 3).'])
        self.assertEqual(self.state().active, [64, 182, 215])
        for line, why in (('!boss quest 215', 'already in your log'), ('!boss quest 6', 'slots are used'),
                          ('!boss quest 158', 'no boss quest'), ('!boss quest x', 'not a number')):
            with self.subTest(line=line):
                texts, pkts = self.gm(a, line)
                self.assertEqual([p.opcode for p in pkts], [0x15])
                self.assertIn(why, texts[0])
        self.assertEqual(self.state().active, [64, 182, 215])
        # the kill of a Wasablanca would credit 215 now (slot 3): the ReqPro path is the same
        self.assertEqual(self.state().credit_kill(WASABLANCA, EC.quests()), (2, 1))


class BossCombat2009(CombatFlows, Rig):
    build = B9


class BossCombat2008(CombatFlows, Rig):
    build = B8


class BossQuests2009(QuestFlows, Rig):
    build = B9


class BossQuests2008(QuestFlows, Rig):
    build = B8


if __name__ == '__main__':
    unittest.main()
