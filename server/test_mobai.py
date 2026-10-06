#!/usr/bin/env python3
"""
test_mobai.py - retail monster aggro run by the server (MONSTER_AGGRO_RE_2026-09-23)
===================================================================================
The field client never gives a monster a target (both writers of +0xE44 sit behind the host
flag scene+0xF40 == 0), so the server keeps the hate list and drives a mob that turned on a
player with command nodes, handing it back with S2C 0x9E (mobai.py + GameServer's MONSTER
AGGRO section). Two layers:

- the pure rules (mobai.py): the 0x2A/0x1B lo word table, the hni AI flags of both builds,
  the hate list rule 0x41A737..0x41A815, the chase branch 0x418EA0..0x41919A with the exe's
  own constants (65/140/200/20/40, leash 600) and the 2009 proximity box;
- the server through fakeclient, for BOTH client builds (the wire bytes are the same): a
  surviving reported hit and server-side hits (skill, DoT, !trap, a driver swing) turn the
  mob, the 'monster-ai' tick's decisions (face / walk-through / jump / drop / attack A and B
  / dash / stop) as exact 0x2A self-form bytes with their keep-alive cadence, the 0x1B
  fallback, every release (target dead or gone, leash, timeout, the player's death) as the
  exact 0x9E, no command after a kill, contact damage per event type (S2C 0x28, 0x3E at 0),
  the 2009 AI[5] proximity aggro, and MOB_AGGRO false = the old behaviour;
- P7 live L2: a touch from a monster parked in its commanded attack hurts through the
  contact slot, and the attack word holds at the reach edge (MOB_ATTACK_HOLD_X hysteresis).
- the desync fixes of 2026-09-28: M1's recovery gate (no chase word before
  MOB_HIT_RECOVER_SECS after a client-caught basic swing, interact event 7; the skill /
  guarded / trap catches that set none are in test_sharedmobs) on both the '2A' and the '1B'
  path, and P3's per-template speed (1e7 / hni speed, x 2.7 in a dash; MOB_SPEED_FROM_TEMPLATE).

No port is bound, no client or server is started; ticks run on an explicit clock and the live
accounts.json is never opened (temp copies; checked at the end of the module).
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
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import combat as C  # noqa: E402
import config as cfgmod  # noqa: E402
import debuffs as D  # noqa: E402
import en_content as EC  # noqa: E402
import fakeclient as F  # noqa: E402
import hpmp  # noqa: E402
import mobai as AI  # noqa: E402
import packets as P  # noqa: E402
import progression  # noqa: E402
import skills as SK  # noqa: E402

W = F.import_server()
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = cfgmod.BUILD_2008, cfgmod.BUILD_2009
DIRS = {B8: cfgmod.DEFAULTS['CLIENT_DIR'], B9: cfgmod.DEFAULTS['CLIENT_DIR_2009']}
HAVE = {b: os.path.exists(os.path.join(HERE, d, 'hs', 'windslayer.hii')) for b, d in DIRS.items()}
EXE_2009 = os.path.join(HERE, DIRS[B9], 'WindSlayer.exe')

# C2S send-site keys and the world-entry sequence of each build (the 2009 client gets its
# bank block 0x65 at every entry).
KEYS = {
    B8: {'login': '0x44D8BF/0x01', 'enter': '0x42F904/0x2B', 'move': '0x42CE94/0x0D',
         'skill': '0x44C239/0x15', 'chat': '0x445CA7/0x03', 'portal': '0x42F76B/0x7E'},
    B9: {'login': '0x451CE5/0x01', 'enter': '0x4315D7/0x2B', 'move': '0x42E704/0x0D',
         'skill': '0x44FC61/0x15', 'chat': '0x44790E/0x03', 'portal': '0x431284/0x7E'},
}
ENTRY = {B8: (0x03, 0x07, 0x15, 0x28, 0x44), B9: (0x03, 0x07, 0x15, 0x65, 0x28, 0x44)}

ICE_SPEAR, POISON, STUN, BOOBY_TRAP = 260, 391, 435, 2609
WOODEN_BLADE, DAGGER = 70, 256
PUPU, BLUE_PUPU, IRON_BALL, RYNX, TOAD_CANNON, SLOW_PEACH = 1, 2, 26, 12, 143, 193
MONKEY_SOLDIER = 15
PORTAL_102_TO_101 = 31
WALK_L, WALK_R, JUMP_L, JUMP_R, DROP_L, DROP_R = 0x01, 0x02, 0x0D, 0x0E, 0x11, 0x12
ATK_A_L, ATK_A_R, ATK_B_L, ATK_B_R, DASH_L, DASH_R = 0x05, 0x06, 0x15, 0x16, 0x19, 0x1A

ACCOUNTS = {
    'test': {'password': 'test',
             'characters': [{'name': 'TestHero', 'level': 1, 'class': 0, 'map': 102,
                             'x': 1000, 'y': 714, 'hp': 100, 'mp': 50, 'gm': 1}]},
    'admin': {'password': 'admin',
              'characters': [{'name': 'Watcher', 'level': 1, 'class': 0, 'map': 102,
                              'x': 1500, 'y': 714, 'hp': 100, 'mp': 50}]},
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
    EC.configure(DIRS[B8], B8)       # a 2009 server may have left en_content on the 2009 data
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_mobai.py (tests must only use temp copies)'


def blob(lo):
    return struct.pack('<II', lo, 0)


class Mob:
    """A stand-in with the fields the pure hate rule reads."""

    def __init__(self):
        self.hate, self.aggro_uid = {}, 0


# =========================================================================== pure rules
class Rules(unittest.TestCase):
    def test_the_lo_word_table(self):
        L, R = AI.DIR_LEFT, AI.DIR_RIGHT
        table = {(L, AI.MOTION_WALK): 0x01, (R, AI.MOTION_WALK): 0x02,
                 (L, AI.MOTION_JUMP): 0x0D, (R, AI.MOTION_JUMP): 0x0E,
                 (L, AI.MOTION_DROP): 0x11, (R, AI.MOTION_DROP): 0x12,
                 (L, AI.MOTION_ATTACK_A): 0x05, (R, AI.MOTION_ATTACK_A): 0x06,
                 (L, AI.MOTION_ATTACK_B): 0x15, (R, AI.MOTION_ATTACK_B): 0x16,
                 (L, AI.MOTION_SKILL): 0x19, (R, AI.MOTION_SKILL): 0x1A}
        for (d, m), word in table.items():
            with self.subTest(direction=d, motion=m):
                self.assertEqual(AI.lo(d, m), word)
                self.assertEqual((AI.direction_of(word), AI.motion_of(word)), (d, m))
                self.assertEqual(word & 0xFF000, 0)                 # never action / reaction bits
        self.assertEqual(AI.STOP, 0)
        self.assertTrue(AI.is_attack(0x05) and AI.is_attack(0x16))
        self.assertFalse(AI.is_attack(0x01) or AI.is_attack(0x19) or AI.is_attack(AI.STOP))

    def test_flags_and_padding(self):
        self.assertEqual(AI.pad_ai([1, 2]), (1, 2) + (0,) * 11)
        self.assertEqual(len(AI.pad_ai(range(20))), 13)
        pupu = AI.Flags.of([0, 0, 0, 0, 0, 0, 0, 1, 0, 0])
        self.assertEqual(pupu, AI.Flags(False, False, False, False, True, False, False))
        rynx = AI.Flags.of([1, 1, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0])
        self.assertTrue(rynx.attack_a and rynx.attack_b and rynx.counter_jump and rynx.attacks)
        self.assertFalse(rynx.no_jump or rynx.stationary or rynx.skill)

    def test_the_templates_of_both_hni(self):
        for build in (B8, B9):
            if not HAVE[build]:
                continue
            with self.subTest(build=build):
                EC.configure(DIRS[build], build)
                npcs = EC.npcs()
                one = npcs.get(PUPU)
                self.assertEqual(len(one.ai), 13 if build == B9 else 10)
                # Ssiyo (2009) is Pupu (2008): it chases and drops but never jumps or swings
                self.assertEqual(AI.Flags.of(one.ai), AI.Flags(False, False, False, False, True, False, False))
                self.assertEqual((one.body_atk, one.weak_atk, one.strong_atk), (3, 4, 7))
                rynx = npcs.get(RYNX)
                self.assertEqual(AI.Flags.of(rynx.ai), AI.Flags(True, False, False, True, False, True, False))
                self.assertEqual((rynx.body_atk, rynx.weak_atk, rynx.strong_atk), (18, 17, 25))
                for idx in (2, 3, 4, 6):                          # all flags 0: chase, jump, contact
                    self.assertEqual(AI.Flags.of(npcs.get(idx).ai), AI.Flags(*[False] * 7))
                self.assertTrue(AI.Flags.of(npcs.get(TOAD_CANNON).ai).stationary)
                self.assertTrue(AI.Flags.of(npcs.get(IRON_BALL).ai).skill)
                peach = npcs.get(SLOW_PEACH)
                self.assertEqual(peach is not None and AI.Flags.of(peach.ai).proximity, build == B9)
        EC.configure(DIRS[B8], B8)

    def test_the_hate_list_rule(self):
        """0x41A737..0x41A815: aggro = attacker when he is the top damager (the first entry
        with the greatest total) or the top damager is gone or dead."""
        mob, dead = Mob(), set()
        alive = lambda uid: uid not in dead                                   # noqa: E731
        self.assertTrue(AI.add_hate(mob, 1, 5, alive))
        self.assertEqual((mob.aggro_uid, mob.hate), (1, {1: 5}))
        self.assertFalse(AI.add_hate(mob, 2, 5, alive))                       # a tie is not the top
        self.assertEqual(mob.aggro_uid, 1)
        self.assertTrue(AI.add_hate(mob, 2, 1, alive))                        # 6 > 5: top -> target
        self.assertEqual(mob.aggro_uid, 2)
        self.assertFalse(AI.add_hate(mob, 1, 0, alive))                       # 5 < 6: stays on 2
        self.assertEqual(mob.aggro_uid, 2)
        dead.add(2)
        self.assertTrue(AI.add_hate(mob, 1, 0, alive))                        # the top is dead
        self.assertEqual((mob.aggro_uid, mob.hate), (1, {1: 5, 2: 6}))
        AI.clear(mob)
        self.assertEqual((mob.aggro_uid, mob.hate, mob.ai_owned, mob.ai_lo), (0, {}, False, -1))

    def decide(self, mob_xy, player_xy, ai_dir=AI.DIR_LEFT, flags=AI.Flags(*[False] * 7), **kw):
        d = AI.decide(mob_xy, player_xy, ai_dir, flags, **kw)
        return AI.lo(d.direction, d.motion)

    def test_face_walk_through_and_turn_back(self):
        self.assertEqual(self.decide((1244, 714), (1000, 714)), WALK_L)          # |dx| 244 > 65
        self.assertEqual(self.decide((1000, 714), (1244, 714), AI.DIR_LEFT), WALK_R)
        self.assertEqual(self.decide((1030, 714), (1000, 714), AI.DIR_LEFT), WALK_L)
        self.assertEqual(self.decide((970, 714), (1000, 714), AI.DIR_LEFT), WALK_L)  # through: kept
        self.assertEqual(self.decide((935, 714), (1000, 714), AI.DIR_LEFT), WALK_L)  # 65: still kept
        self.assertEqual(self.decide((934, 714), (1000, 714), AI.DIR_LEFT), WALK_R)  # 66: turns back
        self.assertEqual(self.decide((1000, 714), (1000, 714), 0), WALK_R)           # no direction yet

    def test_jump_and_drop_mirror_the_two_branches(self):
        pupu = AI.Flags.of([0, 0, 0, 0, 0, 0, 0, 1, 0, 0])
        anyone = AI.Flags(*[False] * 7)
        self.assertEqual(self.decide((1200, 714), (1000, 600), flags=anyone), JUMP_L)
        self.assertEqual(self.decide((1030, 714), (1000, 600), flags=anyone), JUMP_L)   # near branch
        self.assertEqual(self.decide((1200, 714), (1000, 600), flags=pupu), WALK_L)     # AI[7]: never
        self.assertEqual(self.decide((1200, 714), (1000, 708), flags=anyone), WALK_L)   # 6 px: noise
        # far branch (|dx| > 65): > 140 px lower and |dx| < 200
        self.assertEqual(self.decide((1100, 714), (1000, 855), flags=pupu), DROP_L)
        self.assertEqual(self.decide((1100, 714), (1000, 854), flags=pupu), WALK_L)
        self.assertEqual(self.decide((1200, 714), (1000, 900), flags=pupu), WALK_L)     # |dx| 200
        # near branch (|dx| <= 65): > 20 px lower and |dx| < 40; nothing for 40..65
        self.assertEqual(self.decide((1030, 714), (1000, 735), flags=pupu), DROP_L)
        self.assertEqual(self.decide((1030, 714), (1000, 734), flags=pupu), WALK_L)
        self.assertEqual(self.decide((1050, 714), (1000, 900), flags=pupu), WALK_L)

    def test_attack_a_or_b_in_reach_in_front(self):
        rynx = AI.Flags.of([1, 1, 0, 0, 0, 0, 1, 0, 1, 0])
        self.assertEqual(self.decide((1040, 714), (1000, 714), flags=rynx, rng_bit=1), ATK_A_L)
        self.assertEqual(self.decide((1040, 714), (1000, 714), flags=rynx, rng_bit=0), ATK_B_L)
        self.assertEqual(self.decide((960, 714), (1000, 714), AI.DIR_RIGHT, rynx, rng_bit=1), ATK_A_R)
        # a running attack keeps its kind whatever the rng says
        self.assertEqual(self.decide((1040, 714), (1000, 714), flags=rynx, rng_bit=0,
                                     prev_motion=AI.MOTION_ATTACK_A), ATK_A_L)
        self.assertEqual(self.decide((1040, 714), (1000, 714), flags=rynx, rng_bit=1,
                                     prev_motion=AI.MOTION_ATTACK_B), ATK_B_L)
        # out of reach, behind, or too far below: no swing
        self.assertEqual(self.decide((1061, 714), (1000, 714), flags=rynx, rng_bit=1), WALK_L)
        self.assertEqual(self.decide((960, 714), (1000, 714), AI.DIR_LEFT, rynx, rng_bit=1), WALK_L)
        self.assertEqual(self.decide((1035, 714), (1000, 755), flags=rynx, rng_bit=1), DROP_L)
        # the attack overrides a jump (0x41916E writes +0x940 last)
        self.assertEqual(self.decide((1040, 714), (1000, 690), flags=rynx, rng_bit=1), ATK_A_L)
        only_b = AI.Flags.of([0, 0, 0, 0, 0, 0, 0, 0, 1])
        self.assertEqual(self.decide((1040, 714), (1000, 714), flags=only_b, rng_bit=1), ATK_B_L)
        contact_only = AI.Flags.of([0, 0, 0, 0, 0, 0, 0, 1])
        self.assertEqual(self.decide((1000, 714), (1000, 714), flags=contact_only, rng_bit=1), WALK_L)

    def test_an_attack_holds_until_the_target_is_clearly_out_of_reach(self):
        """P7 live L2 "Related": at the reach edge the word flipped walk / attack on every
        0.3 s tick. An attack starts at reach_x; one under way (same facing) holds up to
        reach_x + hold_x - for ATTACK_HOLD_SECS after it began (P7 live L2 review: without
        the time limit a target parked 1..30 px beyond reach kept the mob swinging at air for
        good); the client's facing / jump / drop rules are untouched."""
        rynx = AI.Flags.of([1, 1, 0, 0, 0, 0, 1, 0, 1, 0])
        A = AI.MOTION_ATTACK_A
        hold = dict(flags=rynx, rng_bit=1, hold_x=AI.ATTACK_HOLD_X, swing_secs=0.3)
        self.assertEqual((AI.ATTACK_HOLD_X, AI.ATTACK_HOLD_SECS), (30.0, 1.0))
        # starting: the plain box, whatever the hold
        self.assertEqual(self.decide((1060, 714), (1000, 714), **hold), ATK_A_L)
        self.assertEqual(self.decide((1061, 714), (1000, 714), **hold), WALK_L)
        self.assertEqual(self.decide((1061, 714), (1000, 714), prev_motion=AI.MOTION_WALK, **hold), WALK_L)
        # under way: holds to 60 + 30 px, then walks after the target
        for mx, want in ((1061, ATK_A_L), (1085, ATK_A_L), (1090, ATK_A_L), (1091, WALK_L)):
            with self.subTest(mob_x=mx):
                self.assertEqual(self.decide((mx, 714), (1000, 714), prev_motion=A, **hold), want)
        # attack B holds the same way and keeps its kind
        self.assertEqual(self.decide((1080, 714), (1000, 714), prev_motion=AI.MOTION_ATTACK_B, **hold),
                         ATK_B_L)
        # hold_x 0 (the default of decide): the plain box every tick
        self.assertEqual(self.decide((1061, 714), (1000, 714), flags=rynx, rng_bit=1, prev_motion=A), WALK_L)
        # the hold never outlasts a turn: 70 px past the target the mob faces it again
        # (|dx| > 65), and that new swing starts at the plain reach
        self.assertEqual(self.decide((930, 714), (1000, 714), AI.DIR_LEFT, prev_motion=A, **hold), WALK_R)
        self.assertEqual(self.decide((930, 714), (1000, 714), AI.DIR_RIGHT, prev_motion=A, **hold), ATK_A_R)
        # behind the mob (kept facing within 65 px) or outside reach_y: no hold
        self.assertEqual(self.decide((960, 714), (1000, 714), AI.DIR_LEFT, prev_motion=A, **hold), WALK_L)
        self.assertEqual(self.decide((1040, 714), (1000, 673), prev_motion=A, **hold), JUMP_L)
        self.assertEqual(self.decide((1035, 714), (1000, 755), prev_motion=A, **hold), DROP_L)
        # the hold is timed: 61..90 px ahead it holds while the swing is younger than
        # ATTACK_HOLD_SECS, and walks after the target from then on (or with no known age)
        for dx in (61, 70, 80, 90):
            for age, want in ((0.0, ATK_A_L), (0.99, ATK_A_L), (1.0, WALK_L), (6.0, WALK_L),
                              (None, WALK_L)):
                with self.subTest(dx=dx, swing_secs=age):
                    self.assertEqual(self.decide((1000 + dx, 714), (1000, 714), prev_motion=A,
                                                 **{**hold, 'swing_secs': age}), want)
        # ... and inside the plain box the swing goes on whatever its age
        self.assertEqual(self.decide((1060, 714), (1000, 714), prev_motion=A, **{**hold, 'swing_secs': 6.0}),
                         ATK_A_L)
        # hold_secs 0: no hold at all
        self.assertEqual(self.decide((1061, 714), (1000, 714), prev_motion=A, hold_secs=0, **hold), WALK_L)

    def test_dash_when_nothing_else(self):
        dash = AI.Flags.of([0, 0, 0, 1])
        self.assertEqual(self.decide((1200, 714), (1000, 714), flags=dash), DASH_L)
        self.assertEqual(self.decide((1000, 714), (1200, 714), flags=dash), DASH_R)
        self.assertEqual(self.decide((1200, 714), (1000, 600), flags=dash), JUMP_L)

    def test_the_proximity_box_is_strict(self):
        self.assertTrue(AI.in_proximity((1000, 700), (1199, 501)))
        self.assertFalse(AI.in_proximity((1000, 700), (1200, 700)))
        self.assertFalse(AI.in_proximity((1000, 700), (1000, 900)))

    def test_the_constants_are_the_exe_values(self):
        try:
            import pefile
        except ImportError:
            self.skipTest('pefile not available')
        if not os.path.exists(EXE_2009):
            self.skipTest(f'{EXE_2009} not available')
        pe = pefile.PE(EXE_2009, fast_load=True)
        if pe.FILE_HEADER.TimeDateStamp != 0x49797E29:
            self.skipTest('not the EN 2009 build 14 exe')
        rd = lambda va: struct.unpack('<d', pe.get_data(va - pe.OPTIONAL_HEADER.ImageBase, 8))[0]  # noqa: E731
        self.assertEqual([rd(va) for va in (0x52EFD8, 0x52EFD0, 0x52EE08, 0x52ED20, 0x52EFC8, 0x52EFC0)],
                         [AI.FACE_DX, AI.DROP_FAR_DY, AI.DROP_FAR_DX, AI.DROP_NEAR_DY, AI.DROP_NEAR_DX,
                          AI.LEASH_PX])
        self.assertEqual(AI.PROXIMITY_PX, rd(0x52EE08))


class Config(unittest.TestCase):
    def test_defaults_and_validation(self):
        d = cfgmod.defaults()
        self.assertEqual((d.MOB_AGGRO, d.MOB_CONTACT_DAMAGE, d.MOB_CONTACT_AGGRO_ONLY,
                          d.MOB_SERVER_CONTROLLED, d.MOB_AI_COMMAND,
                          d.MOB_AI_TICK_SECS, d.MOB_CMD_KEEPALIVE_SECS, d.MOB_LEASH_PX,
                          d.MOB_AGGRO_TIMEOUT_SECS, d.MOB_ATTACK_REACH_X, d.MOB_ATTACK_REACH_Y),
                         (True, True, True, False, '2A', 0.3, 0.6, 900.0, 15.0, 60.0, 40.0))
        self.assertEqual(cfgmod.from_dict({'MOB_AI_COMMAND': '1B'}).MOB_AI_COMMAND, '1B')
        self.assertEqual(d.MOB_CONTACT_MIN_SECS, 1.2)                     # livetest bug 2
        self.assertEqual(cfgmod.from_dict({'MOB_CONTACT_MIN_SECS': 0}).MOB_CONTACT_MIN_SECS, 0.0)
        self.assertEqual(d.MOB_ATTACK_HOLD_X, AI.ATTACK_HOLD_X)            # P7 live L2 hysteresis
        self.assertEqual(cfgmod.from_dict({'MOB_ATTACK_HOLD_X': 0}).MOB_ATTACK_HOLD_X, 0.0)
        for bad in (-1, float('nan'), float('inf')):
            with self.subTest(MOB_ATTACK_HOLD_X=bad), self.assertRaises(cfgmod.ConfigError):
                cfgmod.from_dict({'MOB_ATTACK_HOLD_X': bad})
        self.assertEqual(d.MOB_ATTACK_HOLD_SECS, AI.ATTACK_HOLD_SECS)      # ... for about one swing
        self.assertEqual(cfgmod.from_dict({'MOB_ATTACK_HOLD_SECS': 0}).MOB_ATTACK_HOLD_SECS, 0.0)
        for bad in (-1, float('nan'), float('inf')):
            with self.subTest(MOB_ATTACK_HOLD_SECS=bad), self.assertRaises(cfgmod.ConfigError):
                cfgmod.from_dict({'MOB_ATTACK_HOLD_SECS': bad})
        for bad in ({'MOB_AI_COMMAND': '9F'}, {'MOB_AI_COMMAND': 0x2A}, {'MOB_AGGRO': 1},
                    {'MOB_CMD_KEEPALIVE_SECS': 0.96}, {'MOB_CMD_KEEPALIVE_SECS': 0},
                    {'MOB_AI_TICK_SECS': 0}, {'MOB_LEASH_PX': -1}, {'MOB_ATTACK_REACH_X': 0},
                    {'MOB_CONTACT_MIN_SECS': -0.1},
                    # json.load takes NaN / Infinity: NaN would switch the limit off, inf never
                    # frees (or prunes) a monster's slot
                    {'MOB_CONTACT_MIN_SECS': float('nan')}, {'MOB_CONTACT_MIN_SECS': float('inf')}):
            with self.subTest(bad), self.assertRaises(cfgmod.ConfigError):
                cfgmod.from_dict(bad)
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.from_dict(json.loads('{"MOB_CONTACT_MIN_SECS": NaN}'))

    def test_the_desync_fix_keys(self):
        """M1 / M3 / P3 (desync fix plan 2026-09-28, HIT_STUN_PER_SWING_RE): each switch alone,
        validated at load. The gate is the hit + max(MOB_HIT_RECOVER_SECS, stun +
        MOB_HIT_RECOVER_MARGIN_SECS), so it always outlasts the stun: the floor no longer has
        to cover MOB_HIT_RELAY_HURT_MS, the margin has to cover the tick rounding (>= 0.09)."""
        d = cfgmod.defaults()
        self.assertEqual((d.MOB_HIT_RELAY_KNOCKBACK, d.MOB_HIT_RELAY_HURT_MS, d.MOB_HIT_HURT_PER_SWING,
                          d.MOB_HIT_RELAY_SKILL_VARIANT, d.MOB_HIT_RECOVER_SECS, d.MOB_HIT_RECOVER_MARGIN_SECS,
                          d.MOB_HIT_CAST_GATE, d.MOB_SPEED_FROM_TEMPLATE, d.MOB_HIT_ICE_CHASE_SECS),
                         (True, 490, True, True, 0.45, 0.12, True, True, 0.6))
        for good in ({'MOB_HIT_RELAY_KNOCKBACK': False}, {'MOB_HIT_RECOVER_SECS': 0},
                     {'MOB_HIT_RECOVER_SECS': 0.03}, {'MOB_HIT_RELAY_HURT_MS': 0},
                     {'MOB_HIT_RELAY_HURT_MS': 4095, 'MOB_HIT_RECOVER_SECS': 0.1},
                     {'MOB_HIT_HURT_PER_SWING': False}, {'MOB_HIT_RELAY_SKILL_VARIANT': False},
                     {'MOB_HIT_CAST_GATE': False}, {'MOB_HIT_RECOVER_MARGIN_SECS': 0.09},
                     {'MOB_HIT_RECOVER_MARGIN_SECS': 1}, {'MOB_SPEED_FROM_TEMPLATE': False},
                     {'MOB_HIT_ICE_CHASE_SECS': 0}, {'MOB_HIT_ICE_CHASE_SECS': 0.01},
                     {'MOB_HIT_ICE_CHASE_SECS': 0.949}):
            with self.subTest(good):
                cfgmod.from_dict(good)
        for bad in ({'MOB_HIT_RELAY_HURT_MS': -1}, {'MOB_HIT_RELAY_HURT_MS': 4096},
                    {'MOB_HIT_RECOVER_SECS': -0.1}, {'MOB_HIT_RECOVER_SECS': float('nan')},
                    {'MOB_HIT_RECOVER_SECS': float('inf')}, {'MOB_HIT_RELAY_KNOCKBACK': 1},
                    {'MOB_HIT_RECOVER_MARGIN_SECS': 0.089}, {'MOB_HIT_RECOVER_MARGIN_SECS': 1.01},
                    {'MOB_HIT_RECOVER_MARGIN_SECS': float('nan')}, {'MOB_HIT_HURT_PER_SWING': 1},
                    {'MOB_HIT_RELAY_SKILL_VARIANT': 'yes'}, {'MOB_HIT_CAST_GATE': None},
                    {'MOB_HIT_RELAY_HURT_MS': 360.0}, {'MOB_SPEED_FROM_TEMPLATE': None},
                    # the ice chase word must beat the copies' stall at the 0.96 s node hold
                    {'MOB_HIT_ICE_CHASE_SECS': 0.95}, {'MOB_HIT_ICE_CHASE_SECS': 1.0},
                    {'MOB_HIT_ICE_CHASE_SECS': -0.1}, {'MOB_HIT_ICE_CHASE_SECS': float('nan')}):
            with self.subTest(bad), self.assertRaises(cfgmod.ConfigError):
                cfgmod.from_dict(bad)


# =========================================================================== server
class AggroServer(unittest.TestCase):
    """A fresh GameServer of `build` on a temp accounts.json (the default config plus
    `overrides`); TestHero (uid 1, a GM) and Watcher (uid 2) stand on map 102."""
    build = B8
    overrides = {}

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        self.tmp = tempfile.mkdtemp(prefix='ws_mobai_')
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, **self.overrides})
        self.server = F.make_server(self.tmp, accounts=copy.deepcopy(ACCOUNTS), config=cfg)
        self.keys = KEYS[self.build]
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ setup ---
    def char(self, user='test', name='TestHero'):
        return self.server.store.find_character(user, name)

    def hero(self, cls=0, level=1, weapon=0, hp=None, skills=()):
        with self.server.store.lock:
            ch = self.char()
            ch['class'] = cls
            ch['exp'] = progression.exp_for_level(level)
            ch['equipped'] = {5: {'id': weapon, 'w': [0] * 6}} if weapon else {}
            ch['skills'] = list(skills)
            d = hpmp.derive({}, ch)
            ch['hp'] = d.max_hp if hp is None else hp
            ch['mp'] = d.max_mp
        self.server.store.mark_dirty('test setup')

    def enter(self, user='test', password='test', name='TestHero'):
        c = F.FakeClient(self.server)
        self.clients.append(c)
        if self.build == B9:
            c.send_c2s(self.keys['login'], F.sso_login(user, password))
        else:
            c.send_c2s(self.keys['login'], {'account_id': user, 'password': password})
        self.assertEqual(c.s2c(c.expect(0x02))['result'], 1)
        c.send_c2s(self.keys['enter'], {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907, 'char_name': name})
        # world-presence: + 0x04 / 0x05 when another client is on map 102 (F.expect_entry)
        F.expect_entry(c, (*ENTRY[self.build], *F.mob_packets(8)), self.clients, 102)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world') and s.get('current_map') == 102))
        return c

    def lock(self, c):
        return self.server._combat_lock(c.session)

    @staticmethod
    def recovered(mob):
        """The first moment a chase word may follow the last client-caught hit on `mob`
        (desync fix M1: MOB_HIT_RECOVER_SECS after it, Monster.ai_recover_until); now when no
        gate runs."""
        return max(time.monotonic(), mob.ai_recover_until)

    def mob(self, c, i=1):
        return c.session['monsters'][W.MOB_UID_BASE + i]

    def chase(self, template=None, mob_xy=(1244.0, 714.0), player_xy=(1000.0, 714.0), hp=500, c=None):
        """Enter (unless `c`), make monster #1 the given hni template at mob_xy (its spawn too)
        with `hp`, park the other seven far away and put the player at player_xy."""
        c = c or self.enter()
        with self.lock(c):
            mons = c.session['monsters']
            for i, m in enumerate(mons.values()):
                m.x, m.y = 5000.0 + 300 * i, 100.0
            uid = W.MOB_UID_BASE + 1
            if template is not None:
                npcs = EC.npcs()
                mons[uid] = self.server._make_monster(npcs.get(template), npcs.template_index(template),
                                                      uid, *mob_xy)
            mob = mons[uid]
            mob.x, mob.y = mob.spawn_x, mob.spawn_y = (float(mob_xy[0]), float(mob_xy[1]))
            mob.hp = mob.max_hp = hp
            c.session['pos'] = tuple(map(float, player_xy))
        return c, mob

    def put(self, c, x, y):
        with self.lock(c):
            c.session['pos'] = (float(x), float(y))

    def pin(self, c, mob, x, y, now):
        """A position fix (what the driver's 0.25 s sample gives) at `now`."""
        with self.lock(c):
            mob.x, mob.y, mob.fix_t = float(x), float(y), now

    # -------------------------------------------------------------- traffic ---
    def move(self, c, state_lo, **extra):
        c.send_c2s(self.keys['move'], {'realtime_delta_ms': 0, 'map_code': 102, 'logic_elapsed_ms': 30,
                                       'state_lo': state_lo, 'state_hi': 0, **extra})

    def report_hit(self, c, mob):
        """The 61 B C2S 0x0D the client sends the tick after its own hit detection caught a
        swing: interact event 7 with the victim in the tail (at its current position)."""
        px, py = c.session['pos']
        self.move(c, W.HIT_REPORT_EVENT << 16, target_uid=mob.uid, pos_x=float(px), pos_y=float(py),
                  target_dx=float(mob.x - px), target_dy=float(mob.y - py), flag_8db=0, flag_8e7=0,
                  timer_dac=0, target_action_event=7)

    def hit(self, c, mob):
        """A surviving reported hit: exactly the 16-byte 0x2A release (lo 0) comes back."""
        self.report_hit(c, mob)
        pkt = c.expect(0x2A)
        self.assertEqual(pkt.payload, self.cmd(mob, AI.STOP))
        return pkt

    @staticmethod
    def cmd(mob, lo, receiver=1):
        """The 0x2A self-form: mob uid, lo, hi 0, target = the receiver's own uid."""
        return struct.pack('<I', mob.uid) + blob(lo) + struct.pack('<I', receiver)

    @staticmethod
    def hand_back(mob):
        return struct.pack('<I', mob.uid) + bytes(8)

    def drain(self, c, quiet=0.08):
        return [(p.opcode, p.payload) for p in c.recv_until_quiet(quiet)]

    def tick(self, c, now):
        """One 'monster-ai' pass at `now`; the (opcode, payload) the client received. The
        monsters are shared (world-shared-monsters), so every other client of the test on the
        map gets the same words: self.others = {client: its packets}; the count the tick
        returns is all of them."""
        n = self.server._tick_monster_ai(now)
        got = self.drain(c, 0.05) if n else []
        self.others = {o: (self.drain(o, 0.05) if n else []) for o in self.clients if o is not c}
        self.assertEqual(len(got) + sum(len(v) for v in self.others.values()), n)
        return got

    def step(self, c, mob, now, mob_xy, player_xy=None):
        """Pin the mob (and the player), tick, and return the lo of the 0x2A it got."""
        if player_xy is not None:
            self.put(c, *player_xy)
        self.pin(c, mob, *mob_xy, now)
        got = self.tick(c, now)
        self.assertEqual(len(got), 1, got)
        op, payload = got[0]
        self.assertEqual((op, len(payload)), (0x2A, 16))
        me = P.session_uid(c.session)
        self.assertEqual(c.s2c(P_Packet(op, payload)), {'mover_uid': mob.uid, 'move_bits': payload[4:12],
                                                        'target_uid': me})
        self.assertEqual(payload[8:], bytes(4) + struct.pack('<I', me))  # hi 0, target = me
        return struct.unpack_from('<I', payload, 4)[0]


def P_Packet(opcode, payload):
    return F.Packet(opcode, payload, True, 0)


class AggroFlows:
    """The server flows, run for each build by the concrete classes below."""

    # ------------------------------------------------------------ the chase ---
    def test_a_surviving_reported_hit_turns_the_mob_and_the_tick_walks_it(self):
        c, mob = self.chase()
        with self.assertNoLogs('WS', logging.ERROR):             # no NEVER_REPLY violation
            self.hit(c, mob)
        self.assertEqual((mob.aggro_uid, mob.hate, mob.ai_owned, mob.ai_dir), (1, {1: 500 - mob.hp}, True,
                                                                                 AI.DIR_LEFT))
        T = self.recovered(mob)
        got = self.tick(c, T)
        self.assertEqual(got, [(0x2A, bytes.fromhex('01 00 0f 00 01 00 00 00 00 00 00 00 01 00 00 00'))])
        self.assertEqual(c.s2c(P_Packet(*got[0])), {'mover_uid': mob.uid, 'move_bits': blob(WALK_L),
                                                    'target_uid': 1})

    def test_the_keepalive_cadence_stays_under_the_960_ms_hold(self):
        c, mob = self.chase()
        self.hit(c, mob)
        T = self.recovered(mob)
        walk = (0x2A, self.cmd(mob, WALK_L))
        self.assertEqual(self.tick(c, T), [walk])
        # MOB_CMD_KEEPALIVE_SECS 0.6 less the 50 ms timer-jitter allowance
        self.assertAlmostEqual(self.server.config.MOB_CMD_KEEPALIVE_SECS - W.GameServer.MOB_AI_JITTER_SECS, 0.55)
        for dt, sent in ((0.3, False), (0.54, False), (0.56, True), (0.86, False), (1.1, False),
                         (1.12, True), (1.4, False)):
            with self.subTest(dt=dt):
                self.pin(c, mob, 1244, 714, T + dt)                 # the driver holds it in place
                self.assertEqual(self.tick(c, T + dt), [walk] if sent else [])
        # a change goes out at once, whatever the cadence
        self.put(c, 1400, 714)
        self.assertEqual(self.step(c, mob, T + 1.55, (1244, 714)), WALK_R)

    def test_a_contact_only_mob_walks_through_turns_back_and_drops_but_never_jumps(self):
        c, mob = self.chase()                                        # Pupu / Ssiyo, AI[7] no-jump
        self.assertTrue(mob.flags.no_jump and not mob.flags.attacks)
        self.hit(c, mob)
        T = self.recovered(mob)
        seq = [((1244, 714), (1000, 714), WALK_L),                   # faces the player
               ((1030, 714), (1000, 714), WALK_L),
               ((970, 714), (1000, 714), WALK_L),                    # walks through him
               ((930, 714), (1000, 714), WALK_R),                    # 70 px past: turns back
               ((1200, 714), (1000, 600), WALK_L),                   # higher: never jumps
               ((1100, 714), (1000, 864), DROP_L),                   # 150 lower, |dx| 100
               ((1030, 714), (1000, 744), DROP_L),                   # 30 lower, |dx| 30
               ((1050, 714), (1000, 864), WALK_L),                   # |dx| 50: no drop (40..65)
               ((1250, 714), (1000, 864), WALK_L)]                   # |dx| 250: no drop
        for k, (mob_xy, player_xy, want) in enumerate(seq):
            with self.subTest(mob=mob_xy, player=player_xy):
                # 0.7 s apart: every step sends (a change, or the keep-alive)
                self.assertEqual(self.step(c, mob, T + 0.7 * k, mob_xy, player_xy), want)

    def test_a_jumping_template_jumps_toward_a_higher_player(self):
        c, mob = self.chase(template=BLUE_PUPU)                      # Blue Pupu / Koring: flags 0
        self.hit(c, mob)
        T = self.recovered(mob)
        self.assertEqual(self.step(c, mob, T, (1200, 714), (1000, 600)), JUMP_L)
        self.assertEqual(self.step(c, mob, T + 0.7, (1030, 714), (1000, 600)), JUMP_L)
        self.assertEqual(self.step(c, mob, T + 1.4, (900, 714), (1000, 600)), JUMP_R)
        self.assertEqual(self.step(c, mob, T + 2.1, (900, 714), (1000, 708)), WALK_R)  # 6 px: noise

    def test_rynx_swings_attack_a_or_b_in_reach_and_walks_out_of_it(self):
        c, mob = self.chase(template=RYNX)
        self.assertTrue(mob.flags.attack_a and mob.flags.attack_b)
        self.assertEqual((mob.body_atk, mob.weak_atk, mob.strong_atk), (18, 17, 25))
        self.hit(c, mob)
        T = self.recovered(mob)
        with mock.patch.object(self.server, '_mob_rng_bit', return_value=1):
            self.assertEqual(self.step(c, mob, T, (1244, 714)), WALK_L)
            self.assertEqual(self.step(c, mob, T + 0.3, (1040, 714)), ATK_A_L)
            self.assertEqual(mob.ai_attack_t, T + 0.3)
        with mock.patch.object(self.server, '_mob_rng_bit', return_value=0):
            self.assertEqual(self.step(c, mob, T + 1.0, (1040, 714)), ATK_A_L)    # keeps its kind
            self.assertEqual(self.step(c, mob, T + 1.3, (1100, 714)), WALK_L)     # out of reach
            self.assertEqual(self.step(c, mob, T + 1.6, (1040, 714)), ATK_B_L)
            self.assertEqual(self.step(c, mob, T + 1.9, (930, 714)), WALK_R)      # passed: turns
            self.assertEqual(self.step(c, mob, T + 2.2, (960, 714)), ATK_B_R)
        # the keep-alive re-sends the swing while the player stays in reach
        self.pin(c, mob, 960, 714, T + 2.9)
        self.assertEqual(self.tick(c, T + 2.9), [(0x2A, self.cmd(mob, ATK_B_R))])
        # ... and every keep-alive renews the attack clock (P7 live L2 fix step 2)
        self.assertEqual(mob.ai_attack_t, T + 2.9)
        for dt in (3.5, 4.1):
            self.pin(c, mob, 960, 714, T + dt)
            self.assertEqual(self.tick(c, T + dt), [(0x2A, self.cmd(mob, ATK_B_R))])
        self.assertEqual(mob.ai_attack_t, T + 4.1)
        # A swing report (event 9) 0.2 s after that keep-alive still hurts, although the word
        # last CHANGED at T + 2.2, 2.1 s (> MOB_ATTACK_EVENT_SECS) earlier: the swing filter
        # counts from the last send of the attack, not from the last change of word.
        self.assertGreater(T + 4.3 - (T + 2.2), W.GameServer.MOB_ATTACK_EVENT_SECS)
        with self.lock(c):
            c.session['hp'] = hp = 100
            c.session.pop('swing_t', None)
        with self.server._combat(c.session):
            self.server._monster_contact(c.session, {'event_source_uid': mob.uid}, 9, T + 4.3)
        self.assertLess(c.s2c(c.expect(0x28, quiet=0.1))['hp'], hp)

    def test_the_attack_word_holds_at_the_reach_edge(self):
        """P7 live L2 "Related": near the edge of the reach box the chase word flipped walk /
        attack on every 0.3 s tick (36 changes in 21 s, each a 0x2A that flushes the client's
        queue) while the server's x of the mob swung ~25 px per tick. With the hysteresis
        (MOB_ATTACK_HOLD_X 30) a swing that started at reach holds while the target stays
        within reach + 30 px - for MOB_ATTACK_HOLD_SECS (1 s) after it began: only its
        keep-alive goes out. Then the plain box decides again, so the edge costs at most one
        walk / attack pair per ~1 s instead of a flip per tick."""
        c, mob = self.chase(template=RYNX)
        self.assertEqual((self.server.config.MOB_ATTACK_HOLD_X, self.server.config.MOB_ATTACK_HOLD_SECS),
                         (30.0, 1.0))
        self.hit(c, mob)
        T = self.recovered(mob)
        with mock.patch.object(self.server, '_mob_rng_bit', return_value=1):
            self.assertEqual(self.step(c, mob, T, (1244, 714)), WALK_L)
            self.assertEqual(self.step(c, mob, T + 0.3, (1055, 714)), ATK_A_L)     # starts at reach
            self.assertEqual(mob.ai_attack_start_t, T + 0.3)
            atk = [(0x2A, self.cmd(mob, ATK_A_L))]
            for dt, x, sent in ((0.6, 1080, []), (0.9, 1062, atk), (1.2, 1087, [])):
                with self.subTest(dt=dt, mob_x=x):
                    self.pin(c, mob, x, 714, T + dt)
                    self.assertEqual(self.tick(c, T + dt), sent)            # no flip, keep-alives
            self.assertEqual(mob.ai_attack_start_t, T + 0.3)                 # a keep-alive is no new swing
            # 1.2 s into the swing the hold is over: 66 px is out of the plain box
            self.assertEqual(self.step(c, mob, T + 1.5, (1066, 714)), WALK_L)
            self.assertEqual(self.step(c, mob, T + 1.8, (1060, 714)), ATK_A_L)     # at reach: a new swing
            self.assertEqual(mob.ai_attack_start_t, T + 1.8)
            for dt, x, sent in ((2.1, 1085, []), (2.4, 1070, atk)):
                with self.subTest(dt=dt, mob_x=x):
                    self.pin(c, mob, x, 714, T + dt)
                    self.assertEqual(self.tick(c, T + dt), sent)            # held again
            self.assertEqual(self.step(c, mob, T + 2.7, (1091, 714)), WALK_L)     # clearly out of reach
            self.pin(c, mob, 1070, 714, T + 3.0)
            self.assertEqual(self.tick(c, T + 3.0), [])                             # walking: not in reach
            self.assertEqual(self.step(c, mob, T + 3.3, (1060, 714)), ATK_A_L)     # at reach again
            # MOB_ATTACK_HOLD_X 0: the plain box on every tick - the old flip at the edge
            self.server.config = cfgmod.from_dict({'CLIENT_BUILD': self.build, **self.overrides,
                                                    'MOB_ATTACK_HOLD_X': 0})
            for k, (x, want) in enumerate(((1080, WALK_L), (1058, ATK_A_L), (1083, WALK_L))):
                with self.subTest(hold=0, mob_x=x):
                    self.assertEqual(self.step(c, mob, T + 3.6 + 0.3 * k, (x, 714)), want)

    def test_a_target_parked_just_beyond_reach_is_walked_after(self):
        """P7 live L2 review: the hold was spatial only. A commanded attack never moves the mob
        (only walk / jump / drop / dash are dead-reckoned) and nothing fixes its x while
        nothing hits, so a target that stood 1..30 px beyond the 60 px reach kept the mob in
        its attack for good - swinging at air, a keep-alive every 0.55 s, never walking in
        (20 of 20 ticks at 61..90 px). The hold now lasts MOB_ATTACK_HOLD_SECS after the swing
        began; then the mob walks in and swings again at reach."""
        c, mob = self.chase(template=RYNX)
        self.hit(c, mob)
        T = self.recovered(mob)
        walk, atk = [(0x2A, self.cmd(mob, WALK_L))], [(0x2A, self.cmd(mob, ATK_A_L))]
        with mock.patch.object(self.server, '_mob_rng_bit', return_value=1):
            self.assertEqual(self.step(c, mob, T, (1244, 714)), WALK_L)
            self.assertEqual(self.step(c, mob, T + 0.3, (1055, 714)), ATK_A_L)     # starts at reach
            self.pin(c, mob, 1075, 714, T + 0.6)                   # the target now stands 75 px away
            for dt, sent in ((0.6, []), (0.9, atk), (1.2, [])):     # held: 0.3 / 0.6 / 0.9 s in
                with self.subTest(dt=dt):
                    self.assertEqual(self.tick(c, T + dt), sent)
            self.assertEqual(mob.x, 1075.0)                          # swinging never moved it
            self.assertEqual(self.tick(c, T + 1.5), walk)            # 1.2 s: the hold is over
            # no fix: the walk is dead-reckoned at the Rynx's own speed (desync fix P3: 1e7 /
            # hni speed 80000 = 125 px/s) x 0.3 s = 37.5 px, back into reach
            self.assertEqual(mob.walk_px_s, 125.0)
            self.assertEqual(self.tick(c, T + 1.8), atk)
            self.assertAlmostEqual(mob.x, 1075.0 - mob.walk_px_s * 0.3, places=3)
            self.assertEqual(mob.ai_attack_start_t, T + 1.8)
            # the target keeps stepping back to 75 px whenever a swing starts (knock-back):
            # 6 s of it, and the mob walks in again after every held swing - never more than
            # 4 attack ticks in a row (the one at reach + 0.3 / 0.6 / 0.9 s of hold)
            words = [ATK_A_L]                                        # the swing of T + 1.8
            for k in range(20):
                now = T + 2.1 + 0.3 * k
                if AI.is_attack(mob.ai_lo):
                    self.pin(c, mob, 1075, 714, now)
                self.tick(c, now)
                words.append(mob.ai_lo)
        self.assertEqual(set(words), {ATK_A_L, WALK_L})
        runs = ''.join('A' if w == ATK_A_L else 'W' for w in words)
        self.assertEqual(max(map(len, runs.split('W'))), 4, runs)
        self.assertGreaterEqual(runs.count('W'), 4, runs)

    def test_a_dash_template_dashes(self):
        c, mob = self.chase(template=IRON_BALL)
        self.assertTrue(mob.flags.skill)
        self.hit(c, mob)
        T = self.recovered(mob)
        self.assertEqual(self.step(c, mob, T, (1244, 714)), DASH_L)        # out of any reach
        self.assertEqual(self.step(c, mob, T + 0.3, (800, 714)), DASH_R)

    def test_dead_reckoning_without_the_driver(self):
        c, mob = self.chase()
        self.hit(c, mob)
        T = self.recovered(mob)
        self.assertEqual(self.step(c, mob, T, (1244, 714)), WALK_L)
        self.assertEqual(self.tick(c, T + 0.3), [])                          # no fix: reckoned
        # at the template's speed (desync fix P3): Pupu / Ssiyo 1e7 / 120000 = 83.3 px/s
        self.assertAlmostEqual(mob.walk_px_s, 1e7 / 120000)
        self.assertAlmostEqual(mob.x, 1244 - 0.3 * mob.walk_px_s)
        self.pin(c, mob, 1200, 714, T + 0.4)                                  # a fix wins
        self.tick(c, T + 0.5)
        self.assertAlmostEqual(mob.x, 1200 - 0.1 * mob.walk_px_s)
        # a hit report's interact tail is a fix as well
        self.report_hit(c, mob)
        c.expect(0x2A)
        self.assertGreater(mob.fix_t, 0)

    def test_the_chase_waits_for_the_hurt_of_a_client_caught_hit(self):
        """M1: after the release no word goes out until MOB_HIT_RECOVER_SECS after the hit
        (the watchers' copies run the hurt the relay gave them); MOB_HIT_RECOVER_SECS 0: the
        next tick, as before."""
        c, mob = self.chase()
        self.hit(c, mob)
        T = mob.ai_recover_until
        self.pin(c, mob, 1244, 714, T - 0.1)
        self.assertEqual(self.tick(c, T - 0.1), [])
        self.assertEqual(self.step(c, mob, T, (1244, 714)), WALK_L)
        self.server.config['MOB_HIT_RECOVER_SECS'] = 0.0
        self.hit(c, mob)
        self.assertEqual(mob.ai_recover_until, T)                           # not moved on
        # T + 0.01 is inside the second hit's 0.55 s (it came > 0.25 s after the first):
        # with no gate the chase word goes out anyway
        self.pin(c, mob, 1244, 714, T + 0.01)
        self.assertEqual(self.tick(c, T + 0.01), [(0x2A, self.cmd(mob, WALK_L))])

    def test_monsters_are_reckoned_at_their_template_speed(self):
        """P3: +0x1280 = hni speed x 0.0001 and a walk step is tick / +0x1280 px, so a Monkey
        Soldier (80000) walks 3.75 px per 30 ms tick = 125 px/s (the measured knockback 7.5 px
        is two of those ticks) and dashes 2.7 x that; MOB_SPEED_FROM_TEMPLATE false = 82.5."""
        c, mob = self.chase(template=MONKEY_SOLDIER)
        self.assertEqual(mob.walk_px_s, 125.0)
        self.assertAlmostEqual(W.GameServer.template_walk_px_s(EC.npcs().get(PUPU)), 1e7 / 120000)

        class NoSpeed:
            speed = 0
        self.assertEqual(W.GameServer.template_walk_px_s(NoSpeed()), W.GameServer.MOB_WALK_PX_PER_SEC)

        def reckon(lo):
            with self.lock(c):
                mob.x, mob.ai_lo, mob.ai_owned = 1000.0, lo, True
                mob.ai_sent_t = mob.ai_step_t = mob.fix_t = 100.0
                mob.ai_hold_end = 101.0
                return self.server._mob_dead_reckon(mob, 101.0)
        self.assertAlmostEqual(reckon(WALK_R), 125.0)
        self.assertAlmostEqual(mob.x, 1125.0)
        self.assertAlmostEqual(reckon(DASH_L), 125.0 * 2.7)
        self.assertAlmostEqual(mob.x, 1000.0 - 337.5)
        self.server.config['MOB_SPEED_FROM_TEMPLATE'] = False
        self.assertAlmostEqual(reckon(WALK_R), 82.5)
        self.assertAlmostEqual(reckon(DASH_L), 82.5)
        # the hurt slide of a client-caught hit: 2 ticks at its speed = 7.5 px (M1); a skill
        # hit 4 ticks = 15 px (M3; live s02: Ice Spear slid A's copy 15 px), wind 19 x 1.0 / 2.5
        self.server.config['MOB_SPEED_FROM_TEMPLATE'] = True
        with self.lock(c):
            mob.x = 1100.0
            mob.ai_lo = AI.STOP
        self.assertAlmostEqual(self.server._mob_knock_estimate(mob, C.FACING_LEFT, 200.0), -7.5)
        self.assertAlmostEqual(mob.x, 1092.5)
        self.assertEqual(mob.fix_t, 200.0)
        self.assertAlmostEqual(self.server._mob_knock_estimate(mob, C.FACING_RIGHT, 201.0, 4), 15.0)
        self.assertAlmostEqual(self.server._mob_knock_estimate(mob, C.FACING_RIGHT, 202.0, 19), 71.25)
        self.assertAlmostEqual(self.server._mob_knock_estimate(mob, C.FACING_LEFT, 203.0, 47.5), -178.125)
        self.assertEqual(self.server._mob_knock_estimate(mob, C.FACING_LEFT, 204.0, 0), 0.0)
        self.assertAlmostEqual(mob.x, 1092.5 + 15.0 + 71.25 - 178.125)

    def test_a_stunned_mob_stands_until_the_stun_ends(self):
        c, mob = self.chase()
        self.hit(c, mob)
        T = self.recovered(mob)
        self.assertEqual(self.step(c, mob, T, (1244, 714)), WALK_L)
        with self.lock(c):
            rec, _ = D.apply(mob, SK.skill_def(STUN), T + 0.1)
        self.assertEqual(self.step(c, mob, T + 0.3, (1244, 714)), AI.STOP)   # 0x2A 00
        self.pin(c, mob, 1244, 714, T + 1.5)
        self.assertEqual(self.tick(c, T + 1.5), [])                          # no keep-alive for 00
        self.assertEqual(self.step(c, mob, rec['expires'] + 0.01, (1244, 714)), WALK_L)

    # ------------------------------------------------- server-side hits ---
    def test_a_skill_survivor_is_taken_over_at_once(self):
        """Desync fix M1 cast gate (MOB_HIT_CAST_GATE): taken over at once, but standing (the
        STOP top-up) - the caster's client is about to catch the same hit and start its hurt -
        for Ice Spear's L 1290 + 0.12 s; then the chase word. Off: the chase word at once."""
        self.hero(cls=1, level=40, weapon=WOODEN_BLADE, skills=[ICE_SPEAR])
        c, mob = self.chase(mob_xy=(1244, 714), player_xy=(1200, 714))
        with self.lock(c):
            c.session['facing'] = C.FACING_RIGHT
        t0 = time.monotonic()
        c.send_c2s(self.keys['skill'], {'skill_id': ICE_SPEAR})
        use, mp, take = c.expect(0x25, 0x44, 0x2A)
        t1 = time.monotonic()
        self.assertLess(mob.hp, 500)
        self.assertEqual(take.payload, self.cmd(mob, AI.STOP))
        self.assertEqual((mob.aggro_uid, mob.ai_owned), (1, True))
        T = mob.ai_recover_until
        self.assertTrue(t0 + 1.41 - 1e-6 <= T <= t1 + 1.41 + 1e-6, (T - t0, T - t1))
        self.pin(c, mob, 1244, 714, T - 0.01)
        self.assertEqual(self.tick(c, T - 0.01), [])
        # 44 px: within 65 the first direction is toward the attacker, and it walks on
        self.assertEqual(self.step(c, mob, T, (1244, 714)), WALK_L)
        # MOB_HIT_CAST_GATE false: the chase word at the cast, as before
        self.server.config['MOB_HIT_CAST_GATE'] = False
        with self.lock(c):
            AI.clear(mob)                                                  # a fresh, unowned mob
            mob.hp = 500
            c.session[W.GameServer.SKILL_CD_KEY].clear()
        c.send_c2s(self.keys['skill'], {'skill_id': ICE_SPEAR})
        use, mp, take = c.expect(0x25, 0x44, 0x2A)
        self.assertEqual(take.payload, self.cmd(mob, WALK_L))
        self.assertEqual(mob.ai_recover_until, 0.0)

    def test_a_dot_tick_takes_the_mob_over_once(self):
        self.hero(cls=4, level=40, weapon=DAGGER, skills=[POISON])
        c, mob = self.chase(mob_xy=(1244, 714), player_xy=(1200, 714))
        with self.lock(c):
            c.session['facing'] = C.FACING_RIGHT
        c.send_c2s(self.keys['skill'], {'skill_id': POISON})
        c.expect(0x25, 0x44, 0x41)
        self.assertEqual(mob.aggro_uid, 0)                                   # no damage yet
        rec = mob.debuffs[SK.skill_def(POISON).family]
        self.server._tick_debuffs(rec['next_tick'])
        self.assertEqual(c.expect(0x2A).payload, self.cmd(mob, WALK_L))
        self.assertEqual(mob.hate, {1: 500 - mob.hp})
        self.server._tick_debuffs(rec['next_tick'])                         # owned: the AI tick drives it
        c.expect_silence(0.15)
        self.assertEqual(mob.hate, {1: 500 - mob.hp})

    def test_the_dev_trap_takes_the_mob_over(self):
        self.hero(cls=4, level=35, weapon=DAGGER, skills=[BOOBY_TRAP])
        c, mob = self.chase(mob_xy=(1244, 714), player_xy=(1100, 714))
        c.send(0x15, struct.pack('<HHH', BOOBY_TRAP, 1244, 714))
        c.expect(0x3B, 0x44)
        text = '!trap'
        c.send_c2s(self.keys['chat'], {'msg_len': len(text), 'message': text})
        removal, take, line = c.expect(0x3C, 0x2A, 0x15)
        self.assertEqual(take.payload, self.cmd(mob, WALK_L))
        self.assertEqual(mob.aggro_uid, 1)

    def test_a_driver_swing_takes_the_mob_over(self):
        c, mob = self.chase(mob_xy=(1244, 714), player_xy=(1150, 714))
        self.server._memory_melee(1150.0, 714.0, uid=1)
        self.assertEqual(c.expect(0x2A).payload, self.cmd(mob, WALK_L))

    # ------------------------------------------------------- hate list ---
    def test_the_hate_list_switches_to_the_top_damager(self):
        c, mob = self.chase()
        w = self.enter('admin', 'admin', 'Watcher')
        self.drain(c, 0.2)
        self.hit(c, mob)
        # world-move-relay: the Watcher sees the hit report (0x1B on TestHero), then the hit
        # relay on the shared mob (the full 0x2A: action 7, target = the attacker, position)
        relay, feedback = w.expect(0x1B, 0x2A)
        self.assertEqual(w.s2c(relay)['uid'], 1)
        self.assertEqual(len(feedback.payload), 33)
        d1 = mob.hate[1]
        sock = c.session['sock']
        with self.lock(c):
            self.assertTrue(self.server._mob_aggro(sock, c.session, mob, 2, d1 + 10))
        self.assertEqual(mob.aggro_uid, 2)
        T = self.recovered(mob)
        # it chases the Watcher (1500, 714) now: to the right - on both clients (shared), each
        # 0x2A carrying its receiver's own uid
        self.assertEqual(self.step(c, mob, T, (1244, 714)), WALK_R)
        self.assertEqual([(op, p) for op, p in self.others[w]], [(0x2A, self.cmd(mob, WALK_R, receiver=2))])
        with self.lock(c):
            self.assertFalse(self.server._mob_aggro(sock, c.session, mob, 1, 3))
        self.assertEqual((mob.aggro_uid, mob.hate), (2, {1: d1 + 3, 2: d1 + 10}))
        # the target dies: the shared mob goes back to the wander on BOTH clients (0x9E), and
        # the next hit takes it again
        self.server.damage_player(w.session, 9999, reason='test')
        self.assertEqual(w.expect(0x3E, 0x9E)[1].payload, self.hand_back(mob))
        back, corpse = c.expect(0x9E, 0x29)                            # + the Watcher's own death
        self.assertEqual((back.payload, c.s2c(corpse)['uid']), (self.hand_back(mob), 2))
        self.assertEqual((mob.aggro_uid, mob.hate), (0, {}))
        with self.lock(c):
            self.assertTrue(self.server._mob_aggro(sock, c.session, mob, 1, 0))
        self.assertEqual(mob.aggro_uid, 1)

    def test_a_target_that_leaves_the_map_is_let_go(self):
        c, mob = self.chase()
        w = self.enter('admin', 'admin', 'Watcher')
        self.drain(c, 0.2)
        self.hit(c, mob)
        w.expect(0x1B, 0x2A)            # the hit report relay, then the hit relay on the mob
        with self.lock(c):
            self.server._mob_aggro(c.session['sock'], c.session, mob, 2, 999)
        w.send_c2s(self.keys['portal'], {'portal_line_index': PORTAL_102_TO_101})
        w.expect(0x08, 0x03, 0x07, 0x28, 0x44)
        self.assertTrue(w.wait_session(lambda s: s.get('current_map') == 101 and s.get('in_world')))
        self.drain(c, 0.2)
        self.assertEqual(self.tick(c, time.monotonic()), [(0x9E, self.hand_back(mob))])
        self.assertEqual((mob.aggro_uid, mob.hate, mob.ai_owned), (0, {}, False))

    # --------------------------------------------------------- releases ---
    def test_the_leash_releases_with_0x9E(self):
        c, mob = self.chase()
        self.hit(c, mob)
        T = self.recovered(mob)
        self.assertEqual(self.step(c, mob, T, (1244, 714)), WALK_L)
        leash = self.server.config.MOB_LEASH_PX
        self.pin(c, mob, 1244 + leash, 714, T + 0.1)                 # exactly the leash: still chasing
        self.assertEqual(self.tick(c, T + 0.1), [])
        self.pin(c, mob, 1244 + leash + 1, 714, T + 0.2)
        got = self.tick(c, T + 0.2)
        self.assertEqual(got, [(0x9E, bytes.fromhex('01 00 0f 00 00 00 00 00 00 00 00 00'))])
        self.assertEqual(c.s2c(P_Packet(*got[0])), {'mover_uid': mob.uid, 'move_bits': bytes(8)})
        self.assertEqual((mob.aggro_uid, mob.hate, mob.ai_owned, mob.ai_lo), (0, {}, False, -1))
        self.assertEqual(self.tick(c, T + 1.0), [])                  # the client wander has it

    def test_the_leash_counts_from_where_the_chase_began_not_the_spawn(self):
        # live 2009: a Ssiyo hit ~300 px from home gave up after 4 s with a spawn-based 600 px
        # leash; the chase now runs MOB_LEASH_PX from the point it was hit
        c, mob = self.chase()
        mob.x = mob.spawn_x + 500.0                                   # wandered away before the hit
        self.hit(c, mob)
        self.assertEqual(mob.aggro_x, mob.spawn_x + 500.0)
        T = time.monotonic()
        leash = self.server.config.MOB_LEASH_PX
        self.pin(c, mob, mob.aggro_x + 700, 714, T + 0.1)            # 1200 from spawn, 700 chased
        self.assertNotIn(0x9E, [op for op, _ in self.tick(c, T + 0.1)])
        self.pin(c, mob, mob.aggro_x + leash + 1, 714, T + 0.2)
        self.assertIn(0x9E, [op for op, _ in self.tick(c, T + 0.2)])
        self.assertEqual(mob.aggro_x, 0.0)                            # cleared with the aggro

    def test_the_timeout_needs_15_s_without_a_hit_and_400_px(self):
        c, mob = self.chase()
        self.hit(c, mob)
        t0 = mob.ai_hit_t
        self.assertEqual(self.step(c, mob, t0 + 14.9, (1244, 714), (1244 - 450, 714)), WALK_L)
        self.assertEqual(self.step(c, mob, t0 + 15.6, (1244, 714), (1244 - 300, 714)), WALK_L)
        self.put(c, 1244 - 450, 714)
        self.pin(c, mob, 1244, 714, t0 + 15.7)
        self.assertEqual(self.tick(c, t0 + 15.7), [(0x9E, self.hand_back(mob))])
        self.assertEqual(mob.aggro_uid, 0)

    def test_the_players_death_hands_back_every_mob_on_him(self):
        c, mob = self.chase()
        other = self.mob(c, 2)
        with self.lock(c):
            other.x, other.y = other.spawn_x, other.spawn_y = 1300.0, 714.0
            other.hp = other.max_hp = 500
        self.hit(c, mob)
        self.hit(c, other)
        self.assertEqual((mob.aggro_uid, other.aggro_uid), (1, 1))
        self.server.damage_player(c.session, 9999, reason='test')
        death, back1, back2 = c.expect(0x3E, 0x9E, 0x9E)                # level 1: no exp penalty
        self.assertEqual(sorted([back1.payload, back2.payload]),
                         sorted([self.hand_back(mob), self.hand_back(other)]))
        self.assertEqual((mob.aggro_uid, other.aggro_uid), (0, 0))
        self.assertEqual(self.tick(c, time.monotonic() + 1), [])

    def test_a_dead_or_unknown_target_is_let_go(self):
        c, mob = self.chase()
        self.hit(c, mob)
        with self.lock(c):
            mob.aggro_uid = 0x999
        self.assertEqual(self.tick(c, time.monotonic()), [(0x9E, self.hand_back(mob))])
        self.hit(c, mob)
        with self.lock(c):
            c.session['dead'] = True                                   # dead without the flow
        self.assertEqual(self.tick(c, time.monotonic()), [(0x9E, self.hand_back(mob))])

    def test_a_map_change_needs_no_packet(self):
        c, mob = self.chase()
        self.hit(c, mob)
        c.send_c2s(self.keys['portal'], {'portal_line_index': PORTAL_102_TO_101})
        c.expect(0x08, 0x03, 0x07, 0x28, 0x44)                         # no 0x9E: the map load freed it
        self.assertTrue(c.wait_session(lambda s: s.get('current_map') == 101 and s.get('in_world')))
        self.assertEqual(self.server._tick_monster_ai(time.monotonic()), 0)

    def test_a_stationary_template_is_not_aggroed_and_goes_back_to_the_wander(self):
        c, mob = self.chase(template=TOAD_CANNON)
        self.assertTrue(mob.flags.stationary)
        self.hit(c, mob)
        self.assertEqual((mob.aggro_uid, mob.hate, mob.ai_owned), (0, {}, True))
        t = mob.ai_sent_t
        self.assertEqual(self.tick(c, t + 0.5), [])                     # the flinch plays first
        self.assertEqual(self.tick(c, t + 1.0), [(0x9E, self.hand_back(mob))])
        self.assertFalse(mob.ai_owned)

    # --------------------------------------------------------------- kill ---
    def test_no_command_follows_a_kill(self):
        c, mob = self.chase(hp=5)
        self.hit(c, mob)
        T = self.recovered(mob)
        self.assertEqual(self.step(c, mob, T, (1244, 714)), WALK_L)
        opcodes = []
        for _ in range(10):
            self.report_hit(c, mob)
            opcodes = [p.opcode for p in c.recv_until_quiet(0.2)]
            if not mob.alive:
                break
            self.assertEqual(opcodes, [0x2A])
        self.assertFalse(mob.alive)
        self.assertEqual(opcodes, [0x29, 0x21, 0x18])                  # never a 0x2A after the 0x29
        self.assertEqual((mob.aggro_uid, mob.hate, mob.ai_owned), (0, {}, False))
        self.assertEqual(self.tick(c, T + 0.7), [])
        with self.lock(c):
            self.assertEqual(self.server._mob_ai_step(c.session['sock'], c.session, mob, T + 1.0), 0)
        c.expect_silence(0.1)

    def test_a_respawn_starts_with_no_aggro(self):
        c, mob = self.chase(hp=1)
        with self.lock(c):
            mob.aggro_uid, mob.hate, mob.ai_owned = 1, {1: 3}, True
            self.server._damage_monster(c.session['sock'], c.session, mob, 5, 'test')
        c.expect(0x29, 0x21, 0x18)
        now = time.monotonic()
        self.server.ticks.run_due(now + W.MOB_RESPAWN_SECS + 0.5)
        self.drain(c, 0.2)
        self.assertTrue(mob.alive)
        self.assertEqual((mob.aggro_uid, mob.hate, mob.ai_owned, mob.ai_lo), (0, {}, False, -1))

    # ------------------------------------------------------- damage in ---
    def test_contact_damage_follows_the_event_type(self):
        self.hero(level=40)
        c, mob = self.chase(template=RYNX)
        hp = [c.session['hp']]
        self.assertGreater(hp[0], 200)
        # The client's formula (damage.py): Rynx (Lv8: Body_Atk 18, Weak_Atk 17, Strong_Atk 25,
        # hni type 4 = Fire with Attri_Atk 5) on a Lv40 Novice with no armour (Def 0) is
        # level_scale(A, 0, 8, 40) = trunc(A x 2 x 8/48) = A / 3: contact 6, weak 5, strong 8.
        # Its fire 5 x the swing's share (weak 17/42, strong 25/42) = 2 scales to 0.
        CONTACT, WEAK, STRONG = 6, 5, 8

        def event(action, taken):
            with self.lock(c):
                c.session.pop('contact_t', None)                # the contact and swing slots
                c.session.pop('swing_t', None)
                if action in W.GameServer.MOB_SWING_EVENTS and mob.ai_attack_t:
                    now = time.monotonic()                  # as if its swing was just commanded
                    mob.ai_attack_t = now
                    # P13 boss-b3: only the kinds (A / B) it WAS commanded stay fresh
                    mob.ai_attack_a_t = now if mob.ai_attack_a_t else 0.0
                    mob.ai_attack_b_t = now if mob.ai_attack_b_t else 0.0
            self.move(c, action << 12, event_source_uid=mob.uid)
            if taken is None:
                c.expect_silence(0.15)
                return
            hp[0] -= taken
            self.assertEqual(c.s2c(c.expect(0x28, quiet=0.1)), {'hp': hp[0]})

        event(6, None)                                                  # un-provoked: harmless
        self.hit(c, mob)                                                # now it is after him
        event(6, CONTACT)                                               # contact
        event(1, CONTACT)                                               # contact while guarding
        for guarded in (2, 3):                                          # a weak swing into a guard
            event(guarded, None)
        event(7, None)                                                  # no swing commanded yet
        event(4, None)                                                  # nor its strong swing
        event(5, None)                                                  # into the guard
        event(11, None)                                                 # no monster hit
        # make it swing: a decision with the player in reach (on the real clock, which
        # _on_move_state compares against)
        with mock.patch.object(self.server, '_mob_rng_bit', return_value=1):
            self.assertEqual(self.step(c, mob, time.monotonic(), (1040, 714)), ATK_A_L)
        hate = dict(mob.hate)
        for action, taken in ((7, WEAK), (8, WEAK)):
            with self.subTest(action=action):
                event(action, taken)
        # P13 boss-b3: attack B was never commanded, so its events (a client's own swing) do
        # nothing yet; out of reach, then back in with the other rng bit: attack B
        for action in (9, 10, 4, 5):
            with self.subTest(action=action, commanded='A only'):
                event(action, None)
        with mock.patch.object(self.server, '_mob_rng_bit', return_value=0):
            self.assertEqual(self.step(c, mob, time.monotonic(), (1100, 714)), WALK_L)
            self.assertEqual(self.step(c, mob, time.monotonic(), (1040, 714)), ATK_B_L)
        # 4 / 5: attack B into his guard (5 is 4, 0x419549; no shield, so no guard Def)
        for action, taken in ((9, STRONG), (10, STRONG), (4, STRONG), (5, STRONG), (7, WEAK)):
            with self.subTest(action=action):
                event(action, taken)
        self.assertEqual((mob.aggro_uid, mob.hate), (1, hate))            # a hit on him: no change
        # at 0 HP: the death dialog, and the mob chasing him goes back to its wander
        with self.lock(c):
            c.session['hp'] = 2
            c.session.pop('contact_t', None)
        self.move(c, 6 << 12, event_source_uid=mob.uid)
        death, back = c.expect(0x3E, 0x9E)
        self.assertEqual(back.payload, self.hand_back(mob))
        self.assertTrue(c.session['dead'])

    def test_only_a_monster_that_is_after_him_hurts_on_touch(self):
        """Retail (2011 gameplay video): walking through un-hit wandering Ssiyo does nothing;
        once hit they chase and every bump hurts; handed back, they are harmless again."""
        self.hero(level=40)
        c, mob = self.chase()
        hp = c.session['hp']

        def touch():
            with self.lock(c):
                c.session.pop('contact_t', None)                # the contact slot
            self.move(c, 6 << 12, event_source_uid=mob.uid)

        touch()
        c.expect_silence(0.15)
        self.assertEqual(c.session['hp'], hp)
        self.hit(c, mob)
        touch()
        # the Lv1 Pupu's Body_Atk 3 on a Lv40 Novice: trunc(3 x 2 x 1/41) = 0 -> the client's
        # minimum 1
        hp -= 1
        self.assertEqual(c.s2c(c.expect(0x28, quiet=0.1)), {'hp': hp})
        # a mob chasing someone else still hurts whoever is on its hate list
        with self.lock(c):
            mob.aggro_uid = 2
        touch()
        hp -= 1
        self.assertEqual(c.s2c(c.expect(0x28, quiet=0.1)), {'hp': hp})
        # given up (0x9E, hate list cleared): harmless again
        with self.lock(c):
            mob.aggro_uid = 1
            self.server._release_aggro(c.session, 1, 'test')
        c.expect(0x9E)
        touch()
        c.expect_silence(0.15)
        self.assertEqual(c.session['hp'], hp)
        # MOB_CONTACT_AGGRO_ONLY false: every reported touch hurts (the old way)
        self.server.config = cfgmod.from_dict({'CLIENT_BUILD': self.build, **self.overrides,
                                                'MOB_CONTACT_AGGRO_ONLY': False})
        touch()
        hp -= 1
        self.assertEqual(c.s2c(c.expect(0x28, quiet=0.1)), {'hp': hp})

    def test_contact_and_swings_have_separate_rate_limits(self):
        """livetest bug 2: a chasing Poco / Monkey Soldier drained HP by body contact about
        every 0.54 s and starved its swings (441 contact hits against 5), because both shared
        one 0.5 s slot. Contact (events 1/6) now has its own MOB_CONTACT_MIN_SECS slot per
        (victim, monster), swings (4/5, 7..10) their own MOB_SWING_MIN_SECS one. A touch from a
        monster in its commanded attack is contact too (P7 live L2: the client checks contact
        before the swing, so it is all a pinned player's client ever reports)."""
        self.hero(level=40)
        c, mob = self.chase(template=RYNX)
        with self.lock(c):
            other = self.mob(c, 2)                                      # a second Pupu, after him too
            other.x, other.y = other.spawn_x, other.spawn_y = 1300.0, 714.0
            other.hp = other.max_hp = 500
        self.hit(c, mob)
        self.hit(c, other)
        window = self.server.config.MOB_CONTACT_MIN_SECS
        self.assertEqual(window, 1.2)
        hp = [c.session['hp']]

        def event(m, action, hurts):
            self.move(c, action << 12, event_source_uid=m.uid)
            if not hurts:
                c.expect_silence(0.15)
                self.assertEqual(c.session['hp'], hp[0])
                return
            new = c.s2c(c.expect(0x28, quiet=0.1))['hp']
            self.assertLess(new, hp[0])
            hp[0] = new

        event(mob, 6, True)                                             # contact
        event(mob, 6, False)                                            # the same mob, < 1.2 s later
        event(mob, 1, False)                                            # contact into a guard: same slot
        event(other, 6, True)                                           # another mob: its own slot
        # a swing is not starved by the contact that just landed
        with self.lock(c):
            mob.ai_attack_t = mob.ai_attack_a_t = mob.ai_attack_b_t = time.monotonic()   # A and B commanded
        event(mob, 7, True)
        event(mob, 9, False)                                            # < 0.5 s after that swing
        # a touch while its commanded attack runs is contact (P7 live L2): it hurts through
        # the contact slot, and the next one inside MOB_CONTACT_MIN_SECS does not
        with self.lock(c):
            mob.ai_attack_t = time.monotonic()
            del c.session['contact_t'][mob.uid]
        event(mob, 6, True)
        self.assertIn(mob.uid, c.session['contact_t'])
        event(mob, 6, False)                                            # < 1.2 s later
        # both slots open again once their time has passed
        with self.lock(c):
            c.session['contact_t'][other.uid] -= window
            c.session['swing_t'][mob.uid] -= W.GameServer.MOB_SWING_MIN_SECS
            mob.ai_attack_t = mob.ai_attack_b_t = time.monotonic()      # its attack B commanded
        event(other, 6, True)
        event(mob, 9, True)

    def test_a_monster_parked_in_its_attack_still_hurts_by_contact(self):
        """P7 live L2: a Monkey Soldier parked in "attack A right" on a pinned player got only
        keep-alives, and each renewed ai_attack_t, so the old rule "a touch within 1.5 s of a
        commanded attack is its swing passing through" dropped all 407 of its touches in
        226 s - while his client, which checks contact before the swing, never reported a
        swing at all. Its touches hurt now, through the contact slot."""
        self.hero(level=40)
        c, mob = self.chase(template=RYNX)
        self.hit(c, mob)
        T = self.recovered(mob)
        with mock.patch.object(self.server, '_mob_rng_bit', return_value=1):
            self.assertEqual(self.step(c, mob, T, (1040, 714)), ATK_A_L)
            for dt in (0.6, 1.2, 1.8):                                  # pinned: keep-alives only
                with self.subTest(dt=dt):
                    self.pin(c, mob, 1040, 714, T + dt)
                    self.assertEqual(self.tick(c, T + dt), [(0x2A, self.cmd(mob, ATK_A_L))])
        self.assertLess(T + 1.8 - mob.ai_attack_t, 0.6)                 # the clock never ages
        # Rynx Body_Atk 18 on a Lv40 Novice with no armour: level_scale = 18 / 3 (see
        # test_contact_damage_follows_the_event_type)
        CONTACT = 6
        hp = [c.session['hp']]

        def touch(hurts):
            self.move(c, 6 << 12, event_source_uid=mob.uid)
            if not hurts:
                c.expect_silence(0.15)
                self.assertEqual(c.session['hp'], hp[0])
                return
            hp[0] -= CONTACT
            self.assertEqual(c.s2c(c.expect(0x28, quiet=0.1)), {'hp': hp[0]})

        with self.assertLogs('WS', logging.DEBUG) as logs:
            with self.lock(c):
                mob.ai_attack_t = time.monotonic()                      # on the real clock
            touch(True)
            self.assertIn(mob.uid, c.session['contact_t'])
            with self.lock(c):
                c.session['contact_t'][mob.uid] -= 0.54                 # the client's next report
            touch(False)
            with self.lock(c):
                c.session['contact_t'][mob.uid] -= self.server.config.MOB_CONTACT_MIN_SECS   # 1.2
                mob.ai_attack_t = time.monotonic()
            touch(True)
        self.assertEqual([line for line in logs.output if 'commanded attack' in line], [])

    def test_two_monsters_swinging_together_both_hurt(self):
        """Review of livetest bug 2: the swing slot was one per victim, so when two aggroed
        monsters swung at the same player within MOB_SWING_MIN_SECS the second hit was dropped -
        while his client had drawn its digit and flinch and never takes HP off itself. Swings
        are limited per (victim, monster) now, like body contact."""
        self.hero(level=40)
        c, mob = self.chase(template=RYNX)
        with self.lock(c):
            other = self.mob(c, 2)                                      # a second Pupu, after him too
            other.x, other.y = other.spawn_x, other.spawn_y = 1300.0, 714.0
            other.hp = other.max_hp = 500
        self.hit(c, mob)
        self.hit(c, other)
        hp = [c.session['hp']]

        def swing(m, action, hurts):
            self.move(c, action << 12, event_source_uid=m.uid)
            if not hurts:
                c.expect_silence(0.15)
                self.assertEqual(c.session['hp'], hp[0])
                return
            new = c.s2c(c.expect(0x28, quiet=0.1))['hp']
            self.assertLess(new, hp[0])
            hp[0] = new
        with self.lock(c):
            # both attacks commanded, A and B (P13 boss-b3: a swing needs its own kind)
            now = time.monotonic()
            for m in (mob, other):
                m.ai_attack_t = m.ai_attack_a_t = m.ai_attack_b_t = now
        swing(mob, 7, True)
        swing(other, 7, True)                                           # < 0.5 s later: its own slot
        swing(mob, 9, False)                                            # the first one's slot is taken
        swing(other, 9, False)
        self.assertEqual(set(c.session['swing_t']), {mob.uid, other.uid})

    def test_the_swing_slot_is_per_monster_and_pruned(self):
        s = {'swing_t': 5.0}                                            # an older server's float
        slot = self.server._swing_slot                                  # MOB_SWING_MIN_SECS 0.5
        self.assertTrue(slot(s, 7, 100.0))
        self.assertTrue(slot(s, 8, 100.1))                              # another monster
        self.assertFalse(slot(s, 7, 100.4))
        self.assertTrue(slot(s, 7, 100.5))
        self.assertEqual(s['swing_t'], {7: 100.5, 8: 100.1})
        self.assertTrue(slot(s, 9, 101.0))                              # the stale entries go
        self.assertEqual(s['swing_t'], {9: 101.0})

    def test_the_contact_slot_is_per_monster_and_pruned(self):
        s = {}
        slot = self.server._contact_slot                               # MOB_CONTACT_MIN_SECS 1.2
        self.assertTrue(slot(s, 7, 100.0))
        self.assertFalse(slot(s, 7, 101.1))
        self.assertTrue(slot(s, 8, 101.1))                              # another monster
        self.assertTrue(slot(s, 7, 101.2))
        self.assertEqual(s['contact_t'], {7: 101.2, 8: 101.1})
        self.assertTrue(slot(s, 9, 103.0))                              # the stale entries go
        self.assertEqual(s['contact_t'], {9: 103.0})
        s['contact_t'] = 0.0                                            # anything else: no slot taken
        self.assertTrue(slot(s, 9, 103.1))
        self.server.config = cfgmod.from_dict({'CLIENT_BUILD': self.build, **self.overrides,
                                                'MOB_CONTACT_MIN_SECS': 0})
        self.assertTrue(slot(s, 9, 103.1))                              # 0: every touch

    # ---------------------------------------------------------- proximity ---
    def test_the_proximity_scan_is_2009_only(self):
        c = self.enter()
        if self.build == B9:
            c, mob = self.chase(template=SLOW_PEACH, player_xy=(1100, 714), c=c)
        else:
            c, mob = self.chase(player_xy=(1100, 714), c=c)
            with self.lock(c):
                mob.ai = AI.pad_ai([0, 0, 0, 0, 0, 1, 0, 1, 0, 0])      # AI[5] set on a 2008 mob
        self.assertTrue(mob.flags.proximity)
        T = time.monotonic()
        if self.build == B8:
            self.assertEqual(self.tick(c, T), [])                       # 2008 has no such scan
            self.assertEqual(mob.aggro_uid, 0)
            return
        self.put(c, 1044, 714)                                          # 200 px: outside the box
        self.assertEqual(self.tick(c, T), [])
        self.assertEqual(self.step(c, mob, T + 0.3, (1244, 714), (1045, 714)), WALK_L)
        self.assertEqual((mob.aggro_uid, mob.hate), (1, {}))


class Aggro2008(AggroFlows, AggroServer):
    build = B8


class Aggro2009(AggroFlows, AggroServer):
    build = B9


# ------------------------------------------------------------------ 0x1B fallback ---
class Fallback1BFlows:
    overrides = {'MOB_AI_COMMAND': '1B'}

    def node(self, mob, lo):
        return struct.pack('<II', mob.uid, 300) + blob(lo)

    def test_nodes_stream_after_the_release_only_when_the_hold_is_nearly_spent(self):
        c, mob = self.chase()
        self.hit(c, mob)                                                # the 0x2A take-over (lo 0)
        T = self.recovered(mob)
        self.pin(c, mob, 1244, 714, T)
        got = self.tick(c, T)
        self.assertEqual(got, [(0x1B, bytes.fromhex('01 00 0f 00 2c 01 00 00 01 00 00 00 00 00 00 00'))])
        self.assertEqual(c.s2c(P_Packet(*got[0])), {'uid': mob.uid, 'hold_ms': 300, 'state_blob': blob(WALK_L)})
        # each node queues behind the last one's remaining <= 30 ms: holds end at T+0.3, 0.6, 0.9
        for dt, sent in ((0.1, False), (0.26, False), (0.28, True), (0.5, False), (0.56, False),
                         (0.58, True)):
            with self.subTest(dt=dt):
                self.pin(c, mob, 1244, 714, T + dt)
                self.assertEqual(self.tick(c, T + dt), [(0x1B, self.node(mob, WALK_L))] if sent else [])
        self.assertAlmostEqual(mob.ai_hold_end, T + 0.9)
        # a change also waits for the hold (0x1B only appends: no backlog)
        self.put(c, 1500, 714)
        self.pin(c, mob, 1244, 714, T + 0.6)
        self.assertEqual(self.tick(c, T + 0.6), [])
        self.pin(c, mob, 1244, 714, T + 0.88)
        self.assertEqual(self.tick(c, T + 0.88), [(0x1B, self.node(mob, WALK_R))])

    def test_the_chase_waits_for_the_hurt_on_the_1B_path_too(self):
        """M1: the recovery gate holds the '1B' nodes as well."""
        c, mob = self.chase()
        self.hit(c, mob)
        T = mob.ai_recover_until
        self.pin(c, mob, 1244, 714, T - 0.1)
        self.assertEqual(self.tick(c, T - 0.1), [])
        self.pin(c, mob, 1244, 714, T)
        self.assertEqual(self.tick(c, T), [(0x1B, self.node(mob, WALK_L))])

    def test_a_server_side_hit_takes_over_with_0x2A_then_streams(self):
        c, mob = self.chase()
        with self.lock(c):
            self.server._damage_monster(c.session['sock'], c.session, mob, 3, 'test')
        take, node = c.expect(0x2A, 0x1B)
        self.assertEqual(take.payload, self.cmd(mob, AI.STOP))
        self.assertEqual(node.payload, self.node(mob, WALK_L))

    def test_the_release_is_still_0x9E(self):
        c, mob = self.chase()
        self.hit(c, mob)
        T = self.recovered(mob)
        self.pin(c, mob, 1244 - self.server.config.MOB_LEASH_PX - 1, 714, T)
        self.assertEqual(self.tick(c, T), [(0x9E, self.hand_back(mob))])


class Fallback1B2008(Fallback1BFlows, AggroServer):
    build = B8


class Fallback1B2009(Fallback1BFlows, AggroServer):
    build = B9


# ------------------------------------------------------------ MOB_AGGRO false ---
class AggroOffFlows:
    """MOB_AGGRO false: exactly the old behaviour - a surviving reported hit gets only the
    0x2A release, nothing is ever commanded or handed back, server-side hits send nothing."""
    overrides = {'MOB_AGGRO': False}

    def test_the_old_behaviour(self):
        self.hero(cls=1, level=40, weapon=WOODEN_BLADE, skills=[ICE_SPEAR], hp=100)
        c, mob = self.chase(mob_xy=(1244, 714), player_xy=(1200, 714))
        self.hit(c, mob)
        self.assertEqual((mob.aggro_uid, mob.hate), (0, {}))
        now = time.monotonic()
        for dt in (0.0, 0.5, 1.0, 20.0):
            self.assertEqual(self.server._tick_monster_ai(now + dt), 0)
        c.expect_silence(0.1)
        with self.lock(c):
            c.session['facing'] = C.FACING_RIGHT
        c.send_c2s(self.keys['skill'], {'skill_id': ICE_SPEAR})
        c.expect(0x25, 0x44)                                           # a survivor: no packet
        self.assertEqual(mob.aggro_uid, 0)
        # contact damage is its own switch (on by default): the Lv1 Pupu's Body_Atk 3 on the
        # Lv40 warrior, trunc(3 x 2 x 1/41) = 0 -> the client's minimum 1
        self.move(c, 6 << 12, event_source_uid=mob.uid)
        self.assertEqual(c.s2c(c.expect(0x28))['hp'], 100 - 1)
        self.server.damage_player(c.session, 9999, reason='test')
        c.expect(0x3E)                                                  # no 0x9E


class AggroOff2008(AggroOffFlows, AggroServer):
    build = B8


class AggroOff2009(AggroOffFlows, AggroServer):
    build = B9


if __name__ == '__main__':
    unittest.main(verbosity=2)
