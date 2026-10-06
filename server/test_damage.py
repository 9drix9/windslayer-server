#!/usr/bin/env python3
"""
test_damage.py - the client's own damage formula (damage.py, config DAMAGE_FORMULA)
==================================================================================
The numbers pinned here are the worked examples of the reconciled damage spec (section 7),
which were computed by running the original EN 2009 exe bytes (FUN_0041b830 / FUN_004194f0)
under Unicorn at both x87 control words, plus hand-checked level-scale values. Four layers:

- the pure formula with hand-built records (no client files needed): derived stats, the
  level scale, every event, the skill cells, the element term, the clamp and the cap, the
  special branches (fire window, Nova, Vampiric Attack, Holy Protection, Magic Shield,
  Wicked Protection, the arg6 release), the precision modes and the one build difference;
- the same examples from the EN content of each build (item ids, hni templates), so the
  adapters read the right columns;
- the constants against the exes (pefile, when the exes are present);
- the server through fakeclient: TestHero's live case (a Lv14 Berserker hits a Monkey
  Soldier for 8, not 1), the monster's hits on him per event (event 5 is no longer free),
  the victim buffs, the fire window, and DAMAGE_FORMULA 'placeholder' bringing the old numbers
  back;
- the grade roll (config DAMAGE_GRADE_ROLL, livetest bug 10): damage.grade_roll's table,
  jitter and truncation with fixed dice, its distribution with a seeded RNG, and the server
  rolling a player's hits on a monster (reported swings, skills) but never a monster's hit
  on the player or a DoT tick - forced on for 2008, by the default (auto) for 2009 - and the
  default leaving a 2008 server's hits unrolled (no 2008 exe rolls its digit). The rig pins
  the roll off everywhere else (fakeclient).

No port is bound and the live accounts.json is never opened (temp copies; checked at the
end of the module).
"""
import copy
import hashlib
import logging
import os
import random
import shutil
import struct
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import buffs as B  # noqa: E402
import config as cfgmod  # noqa: E402
import damage as DMG  # noqa: E402
import debuffs as D  # noqa: E402
import en_content as EC  # noqa: E402
import fakeclient as F  # noqa: E402
import hpmp  # noqa: E402
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
EXE = {B8: os.path.join(os.path.dirname(HERE), 'WindSlayer.exe'),
       B9: os.path.join(HERE, DIRS[B9], 'WindSlayer.exe')}

_LIVE_HASH = None


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def setUpModule():
    global _LIVE_HASH
    _LIVE_HASH = _sha(LIVE_ACCOUNTS) if os.path.exists(LIVE_ACCOUNTS) else None
    P.STRICT_FIELDS.add(B9)


def tearDownModule():
    P.STRICT_FIELDS.discard(B9)
    EC.configure(DIRS[B8], B8)
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_damage.py (tests must only use temp copies)'


# ------------------------------------------------------------ hand-built records ---
# The hii columns the derivation reads (both EN builds carry the same values for these ids).
def weapon(job_index, w_att, s_att, **extra):
    job = [0] * 7
    job[job_index] = 1
    return dict(kind=11, job=job, w_att=w_att, s_att=s_att, **extra)


def armour(kind, defense, **extra):
    return dict(kind=kind, defense=defense, **extra)


WOODEN_BLADE = weapon(1, 9, 23)          # hii 70, sword (Job flag 1)
WOODEN_STICK = weapon(0, 1, 6)           # 179, Novice
CRUDE_CLUB = weapon(0, 3, 8)             # 121
WOODEN_SWORD = weapon(0, 6, 17)          # 9
SHORT_SWORD = weapon(1, 14, 37)          # 71
IRON_FIST = weapon(2, 9, 21)             # 258, fist (Job flag 2)
CRUSHING_FIST = weapon(2, 12, 25)        # 968
SHIRT_F, SKIRT_F = armour(6, 1), armour(5, 1)                       # 19, 21
LL_VEST, LL_LEGS, LL_SANDAL = armour(6, 8), armour(5, 8), armour(9, 6)  # 64 / 158, 65 / 159, 68

# hni templates (idx 1 / 2 / 15; 1 and 2 are "Pupu" / "Blue Pupu" in the 2008 name table)
SSIYO = {'lv': 1, 'type': 3, 'hp': 5, 'body_atk': 3, 'weak_atk': 4, 'strong_atk': 7, 'def': 2,
         'attri_atk': 0, 'attri_def': 0}
KORING = {'lv': 2, 'type': 3, 'hp': 10, 'body_atk': 4, 'weak_atk': 6, 'strong_atk': 9, 'def': 5,
          'attri_atk': 0, 'attri_def': 0}
MONKEY_SOLDIER = {'lv': 9, 'type': 6, 'hp': 104, 'body_atk': 14, 'weak_atk': 13, 'strong_atk': 21,
                  'def': 30, 'attri_atk': 5, 'attri_def': 9}

# skill terms: rank-1 records (Attribute, Attri_Atk, Skill_P_A)
ICE_SPEAR = {'attribute': 2, 'attri_atk': 9, 'skill_p_a': 0}         # 260 = 0x104
FIRE_BEAT = {'attribute': 1, 'attri_atk': 10, 'skill_p_a': 0}        # 271 = 0x10F
WIND_CUTTER = {'attribute': 4, 'attri_atk': 8, 'skill_p_a': 0}       # 314 = 0x13A
DOUBLE_ATTACK = {'attribute': 0, 'attri_atk': 0, 'skill_p_a': 15}    # 1898 = 0x76A
BLAZING_KICK = {'attribute': 1, 'attri_atk': 13, 'skill_p_a': 0}     # 556 = 0x22C
FLYING_KICK = {'attribute': 4, 'attri_atk': 11, 'skill_p_a': 0}      # 2008 = 0x7D8
TRIPLE_KICK = {'attribute': 3, 'attri_atk': 8, 'skill_p_a': 0}       # 2334 = 0x91E
MERCILESS_STRIKE = {'attribute': 3, 'attri_atk': 11, 'skill_p_a': 0}  # 2367 = 0x93F


def player(cls, level, stats, items, **kw):
    return DMG.derive_player(cls, level, stats, items, **kw)


def hit(att, vic, event, skill=None, **kw):
    return DMG.damage(att, vic, event, skill, **kw)


TESTHERO_ITEMS = [WOODEN_BLADE, SHIRT_F, SKIRT_F]


# =========================================================================== pure formula
class LevelScale(unittest.TestCase):
    def test_hand_checked_values(self):
        # trunc(A x A/(A+D) x 2 x La/(La+Lv)); spec row 9: La 11, Lv 9, D 30 -> A 30 = 16, A 29 = 15
        self.assertEqual(DMG.level_scale(30, 30, 11, 9), 16)       # 30 x 0.5 x 2 x 0.55 = 16.5
        self.assertEqual(DMG.level_scale(29, 30, 11, 9), 15)
        self.assertEqual(DMG.level_scale(18, 0, 8, 40), 6)          # 18 x 1 x 2 x 1/6
        self.assertEqual(DMG.level_scale(3, 0, 1, 40), 0)           # 3 x 2 x 1/41 = 0.15
        self.assertEqual(DMG.level_scale(3, 0, 1, 1), 3)
        self.assertEqual(DMG.level_scale(0, 5, 10, 10), 0)          # A == 0
        self.assertEqual(DMG.level_scale(10, 5, 0, 10), 0)          # La == 0

    def test_precision_modes(self):
        # PC_24 rounds every step to float32 (the main thread), PC_53 does not; they differ by
        # at most 1 - spec 5: class 0, Lv1, STR 0, a W_Att 9 Novice weapon -> Weak 12 vs 11
        novice_weapon = weapon(0, 9, 0)
        self.assertEqual(player(0, 1, (0, 0, 0, 0), [novice_weapon], pc=24)['weak'], 12)
        self.assertEqual(player(0, 1, (0, 0, 0, 0), [novice_weapon], pc=53)['weak'], 11)


class Derivation(unittest.TestCase):
    """Spec section 7 'Derived stats' (every row emulated on the exe at both control words)."""

    def check(self, st, M, wc, weak, strong, defense):
        self.assertEqual((round(st['M'], 4), st['wc'], st['weak'], st['strong'], st['defense']),
                         (M, wc, weak, strong, defense))

    def test_testhero(self):
        # cls 1 job 1 Lv14, STR/DEX/INT/SPR 12/6/6/10, Wooden Blade + 19 + 21:
        # M = (12 + 20) / 7 + 1.4 + 1 = 6.9714; Weak = trunc((32 + 100) x 9 / 100 + M) = 18,
        # Strong = trunc(132 x 23 / 100 + M) = 37, Def = trunc((1.2 x 10 + 25 + 100) x 2 / 100) = 2
        self.check(player(1, 14, (12, 6, 6, 10), TESTHERO_ITEMS), 6.9714, 1, 18, 37, 2)
        self.check(player(1, 14, (39, 6, 6, 10), TESTHERO_ITEMS), 10.8286, 1, 25, 47, 2)

    def test_novices(self):
        self.check(player(0, 1, (3, 2, 1, 3), []), 2.4, 0, 2, 2, 0)
        self.check(player(0, 1, (3, 2, 1, 3), [WOODEN_STICK]), 2.4, 0, 3, 9, 0)
        self.check(player(0, 1, (3, 2, 1, 3), [WOODEN_STICK, SHIRT_F, SKIRT_F]), 2.4, 0, 3, 9, 2)
        self.check(player(0, 1, (9, 0, 0, 0), [WOODEN_STICK]), 3.0, 0, 4, 10, 0)
        self.check(player(0, 3, (3, 2, 2, 2), [WOODEN_STICK]), 2.6, 0, 3, 9, 0)
        self.check(player(0, 3, (7, 4, 2, 4), [WOODEN_STICK]), 3.0, 0, 4, 10, 0)
        self.check(player(0, 3, (15, 2, 0, 0), [WOODEN_STICK]), 3.8, 0, 5, 11, 0)
        self.check(player(0, 3, (7, 4, 2, 4), [CRUDE_CLUB]), 3.0, 0, 6, 12, 0)

    def test_level_11_builds(self):
        self.check(player(0, 11, (30, 8, 5, 6), [WOODEN_SWORD, LL_VEST, LL_LEGS, LL_SANDAL]), 6.1, 0, 14, 29, 25)
        self.check(player(0, 11, (25, 8, 6, 10), [WOODEN_SWORD]), 5.6, 0, 13, 28, 0)
        self.check(player(1, 11, (25, 10, 4, 10), [SHORT_SWORD, LL_VEST, LL_LEGS, LL_SANDAL]), 8.5286, 1, 28, 62, 30)
        self.check(player(2, 11, (25, 10, 4, 10), [IRON_FIST, LL_VEST, LL_LEGS, LL_SANDAL]), 10.4333, 2, 23, 41, 29)

    def test_monsters(self):
        ms = DMG.derive_monster(MONKEY_SOLDIER)
        # hni type 6 -> Earth (3): Attri_Atk 5 is its element attack, Attri_Def 9 its resist
        self.assertEqual((ms['level'], ms['body'], ms['weak'], ms['strong'], ms['defense'], ms['elem'],
                          ms['elem_atk'], ms['res'], ms['guard_def']), (9, 14, 13, 21, 30, 3, 5, [0, 0, 0, 9, 0], 0))
        self.assertEqual((round(ms['r_strong'], 5), round(ms['r_weak'], 5)), (0.61765, 0.38235))
        self.assertEqual(DMG.derive_monster(SSIYO)['elem'], 0)       # type 3: no element
        zero = DMG.derive_monster(dict(SSIYO, weak_atk=0, strong_atk=0))
        self.assertEqual((zero['r_strong'], zero['r_weak']), DMG.RATIO_DEFAULT)
        self.assertEqual(DMG.RATIO_DEFAULT, (DMG.f32(0.7), DMG.f32(0.3)))

    def test_percentages_come_after_the_share_ratios(self):
        base = player(1, 14, (12, 6, 6, 10), TESTHERO_ITEMS)
        buffed = player(1, 14, (12, 6, 6, 10), TESTHERO_ITEMS, mods=[{'skill_p_a': 5, 'skill_p_d': 50}])
        # Strong 37 + trunc(37 x 5 / 100) = 38; Weak 18 + trunc(18 x 5 / 100 = 0.9) = 18; Def 2 + 1
        self.assertEqual((buffed['strong'], buffed['weak'], buffed['defense']), (38, 18, 3))
        self.assertEqual((buffed['r_strong'], buffed['r_weak']), (base['r_strong'], base['r_weak']))
        # a percentage that truncates to 0 still adds 1 to Strong (not to Weak)
        tiny = player(1, 14, (12, 6, 6, 10), TESTHERO_ITEMS, mods=[{'skill_p_a': 1}])
        self.assertEqual((tiny['strong'], tiny['weak']), (38, 18))

    def test_weapon_class_is_the_last_job_flag(self):
        both = dict(WOODEN_BLADE, job=[1, 1, 0, 1, 0, 0, 0])            # flags 0, 1, 3: bow wins
        self.assertEqual(player(1, 14, (12, 6, 6, 10), [both])['wc'], 3)
        self.assertEqual(player(1, 14, (12, 6, 6, 10), [])['wc'], 0)    # bare hands

    def test_the_shield_is_guard_def_and_kind_3_7_is_a_build_difference(self):
        shield = armour(12, 7)
        st = player(1, 14, (12, 6, 6, 10), TESTHERO_ITEMS + [shield])
        self.assertEqual((st['defense'], st['guard_def']), (2, 7))       # raw, not SPR-scaled
        glasses = armour(3, 5)
        self.assertEqual(player(0, 1, (3, 2, 1, 3), [glasses], build='2009')['defense'], 0)  # FUN_0041d640
        # 2008 FUN_0041ace0 counts it like armour: trunc((10 + 3 + 100) x 5 / 100) = 5
        self.assertEqual(player(0, 1, (3, 2, 1, 3), [glasses], build='2008')['defense'], 5)


class Hits(unittest.TestCase):
    """Spec section 7 'Hits' (the base value, before the grade roll: GradeRoll below)."""

    def setUp(self):
        self.hero = player(1, 14, (12, 6, 6, 10), TESTHERO_ITEMS)
        self.ms = DMG.derive_monster(MONKEY_SOLDIER)
        self.ssiyo = DMG.derive_monster(SSIYO)
        self.koring = DMG.derive_monster(KORING)

    def digits(self, att, vic, events, **kw):
        return tuple(hit(att, vic, ev, **kw).total for ev in events)

    def test_testhero_on_a_monkey_soldier(self):
        # weak: B = trunc(18 x 18/48 x 2 x 14/23) = 8 (the live client drew 7-10 = 8 x the grade
        # roll; the placeholder took 1); strong: trunc(37 x 37/67 x 2 x 14/23) = 24
        self.assertEqual(hit(self.hero, self.ms, 7), DMG.Hit(8, 8, 0, False, 0, False, ''))
        self.assertEqual(hit(self.hero, self.ms, 9).total, 24)
        # Ice Spear: Ice 9 x (1.3 x 6 + 15 + 100 = 122.8 %) = 11, E = level_scale(11, 0, 14, 9) = 13
        spear = hit(self.hero, self.ms, 9, ICE_SPEAR)
        self.assertEqual((spear.total, spear.b, spear.e), (37, 24, 13))
        self.assertEqual(hit(self.hero, self.ms, 4, ICE_SPEAR).total, 37)   # a monster has no guard
        self.assertEqual([(h.b, h.e, h.total) for h in (hit(self.hero, self.ms, 9, s) for s in
                          (FIRE_BEAT, WIND_CUTTER, DOUBLE_ATTACK))],
                         [(24, 14, 38), (24, 10, 34), (27, 0, 27)])         # Earth resists 9 vs Wind
        hero61 = player(1, 14, (39, 6, 6, 10), TESTHERO_ITEMS)
        self.assertEqual(self.digits(hero61, self.ms, (7, 9)), (13, 34))

    def test_a_monkey_soldier_on_testhero(self):
        # contact / attack A: Body 14 / Weak 13 against Def 2; attack B: Strong 21 -> B 15 plus
        # its Earth 5 x the strong share 0.62 = 3 -> E 2. 5 -> 4, 8 -> 7, 10 -> 9; 2 and 3 nothing.
        self.assertEqual(self.digits(self.ms, self.hero, (6, 1, 7, 8, 9, 10, 4, 5, 2, 3), cap=427),
                         (9, 9, 8, 8, 17, 17, 17, 17, 0, 0))
        strong = hit(self.ms, self.hero, 9)
        self.assertEqual((strong.b, strong.e), (15, 2))

    def test_novices_on_ssiyo_and_koring(self):
        bare = player(0, 1, (3, 2, 1, 3), [])
        stick = player(0, 1, (3, 2, 1, 3), [WOODEN_STICK])
        self.assertEqual(self.digits(bare, self.ssiyo, (7, 9)), (1, 1))      # 5 hits a kill
        self.assertEqual(self.digits(stick, self.ssiyo, (7, 9)), (1, 5))     # strong 7 capped at HP 5
        self.assertEqual(hit(stick, self.ssiyo, 9, cap=None).b, 7)
        self.assertEqual(hit(player(0, 1, (9, 0, 0, 0), [WOODEN_STICK]), self.ssiyo, 7).total, 2)
        self.assertEqual(self.digits(self.ssiyo, bare, (6, 7, 9)), (3, 4, 7))
        dressed = player(0, 1, (3, 2, 1, 3), [WOODEN_STICK, SHIRT_F, SKIRT_F])
        self.assertEqual(self.digits(self.ssiyo, dressed, (6, 7, 9)), (1, 2, 5))
        for stats, want in (((3, 2, 2, 2), 2), ((7, 2, 2, 2), 3)):       # Lv2 with the stick
            with self.subTest(stats=stats):
                self.assertEqual(hit(player(0, 2, stats, [WOODEN_STICK]), self.ssiyo, 7).total, want)
        koring_rows = (((3, 2, 2, 2), WOODEN_STICK, (1, 6)), ((7, 4, 2, 4), WOODEN_STICK, (2, 8)),
                       ((15, 2, 0, 0), WOODEN_STICK, (3, 9)), ((7, 4, 2, 4), CRUDE_CLUB, (3, 10)),
                       ((11, 2, 2, 2), CRUDE_CLUB, (4, 10)))                 # the last strong is capped
        for stats, wpn, want in koring_rows:
            with self.subTest(stats=stats):
                self.assertEqual(self.digits(player(0, 3, stats, [wpn]), self.koring, (7, 9)), want)

    def test_level_11_on_monkey_soldiers(self):
        armour_set = [LL_VEST, LL_LEGS, LL_SANDAL]
        rows = ((player(0, 11, (30, 8, 5, 6), [WOODEN_SWORD] + armour_set), (4, 15)),
                (player(0, 11, (25, 8, 6, 10), [WOODEN_SWORD]), (4, 14)),
                (player(1, 11, (25, 10, 4, 10), [SHORT_SWORD] + armour_set), (14, 45)),
                (player(2, 11, (25, 10, 4, 10), [IRON_FIST] + armour_set), (10, 26)))
        for st, want in rows:
            with self.subTest(cls=st['cls']):
                self.assertEqual(self.digits(st, self.ms, (7, 9)), want)
        taken = ((rows[0][0], (4, 4, 10)), (rows[1][0], (11, 12, 20)), (rows[2][0], (3, 4, 9)))
        for st, want in taken:
            with self.subTest(victim=st['defense']):
                self.assertEqual(self.digits(self.ms, st, (7, 6, 9), cap=500), want)

    def test_class_2_skill_cells(self):
        monk = player(2, 30, (60, 20, 10, 20), [CRUSHING_FIST])

        def cell(p5, rec):
            fam, div, nodmg = DMG.skill_cell(p5, 2, 1)
            return dict(rec or {}, div=div, nodmg=nodmg)

        self.assertEqual(hit(monk, self.ms, 9).total, 67)
        self.assertEqual(hit(monk, self.ms, 9, cell(1, BLAZING_KICK)).total, 91)
        self.assertEqual(hit(monk, self.ms, 9, cell(2, None)).total, 0)             # NODMG: no digit
        self.assertEqual(hit(monk, self.ms, 9, cell(4, FLYING_KICK)).total, 87)
        # Triple Kick / Merciless Strike: B 67 / 3 = 22 / 67 / 6 = 11, plus their Earth terms
        self.assertEqual(hit(monk, self.ms, 9, cell(6, TRIPLE_KICK)).total, 28)
        self.assertEqual(hit(monk, self.ms, 9, cell(8, MERCILESS_STRIKE)).total, 22)

    def test_clamp_cap_and_the_level_zero_rule(self):
        self.assertEqual(hit(self.ssiyo, self.hero, 6).total, 1)                     # B 0 -> 1
        self.assertEqual(hit(self.hero, self.ms, 9, cap=10).total, 10)
        self.assertEqual(hit(self.hero, self.ms, 9, cap=None).total, 24)             # ms['hp'] 104
        self.assertEqual(hit(self.hero, dict(self.ms, hp=None), 9, cap=None).total, 24)
        self.assertEqual(hit(dict(self.ms, level=0), self.hero, 9).total, 1)         # La 0: no B, no E


class SkillCells(unittest.TestCase):
    def test_the_switch_table(self):
        self.assertEqual(DMG.skill_cell(1, 1, 0), (0x104, None, False))
        self.assertEqual(DMG.skill_cell(2, 2, 1), (None, None, True))
        self.assertEqual(DMG.skill_cell(2, 4, 2), (None, None, True))
        self.assertEqual(DMG.skill_cell(5, 3, 0), (None, None, True))
        self.assertEqual(DMG.skill_cell(6, 2, 1), (0x91E, 3, False))
        self.assertEqual(DMG.skill_cell(8, 2, 1), (0x93F, 6, False))
        self.assertEqual(DMG.skill_cell(6, 2, 2), (0x955, None, False))
        self.assertEqual(DMG.skill_cell(7, 5, 1), (0xA73, None, False))
        self.assertEqual(DMG.skill_cell(7, 5, 2), (0xACB, None, False))            # any other job
        self.assertEqual(DMG.skill_cell(9, 6, 1), (None, None, True))
        self.assertEqual(DMG.skill_cell(9, 6, 2), (None, None, False))             # Vampiric Attack
        self.assertEqual(DMG.skill_cell(10, 6, 2), (None, None, True))
        self.assertEqual(DMG.skill_cell(11, 6, 2), (None, None, True))
        self.assertEqual(DMG.skill_cell(5, 4, 2), (None, None, False))
        self.assertEqual(DMG.skill_cell(0, 1, 1), (None, None, False))             # a plain swing

    def test_a_cast_finds_its_cell(self):
        self.assertEqual(DMG.cell_for_skill(260, 1, 0), (1, 0x104, None))
        self.assertEqual(DMG.cell_for_skill(0x104 + 9, 1, 2), (1, 0x104, None))
        self.assertIsNone(DMG.cell_for_skill(0x104 + 10, 1, 0))
        self.assertEqual(DMG.cell_for_skill(0x91E + 3, 2, 1), (6, 0x91E, 3))
        self.assertIsNone(DMG.cell_for_skill(0x91E, 2, 2))                         # job 2 has 0x955
        self.assertIsNone(DMG.cell_for_skill(0x104, 2, 0))                         # another class

    def test_the_lowest_learned_rank(self):
        self.assertEqual(DMG.learned_rank(0x104, [0x10F, 0x106, 0x105]), 0x105)
        self.assertEqual(DMG.learned_rank(0x104, [0x10E]), 0)                     # out of the family
        self.assertEqual(DMG.learned_rank(0x104, [0, 0x104]), 0)                   # the list ends at 0


class SpecialBranches(unittest.TestCase):
    def setUp(self):
        self.hero = player(1, 14, (12, 6, 6, 10), TESTHERO_ITEMS)
        self.ms = DMG.derive_monster(MONKEY_SOLDIER)

    def test_the_fire_window(self):
        first = hit(self.hero, self.ms, 9, FIRE_BEAT)
        self.assertEqual((first.total, first.fire), (38, True))
        later = hit(self.hero, self.ms, 9, FIRE_BEAT, splash=True)
        self.assertEqual((later.b, later.e, later.total), (0, 14 // 3, 4))
        self.assertFalse(hit(self.hero, self.ms, 4, FIRE_BEAT).fire)               # event 9 only
        self.assertFalse(hit(self.hero, self.ms, 9, ICE_SPEAR).fire)
        self.assertEqual(hit(self.hero, self.ms, 9, ICE_SPEAR, splash=True).total, 37)

    def test_nova_has_no_physical_term(self):
        wind = dict(WIND_CUTTER, nova=True)
        self.assertEqual((hit(self.hero, self.ms, 9, wind).b, hit(self.hero, self.ms, 9, wind).e), (0, 10))
        self.assertEqual(hit(self.hero, self.ms, 4, wind).b, 24)                   # event 9 only

    def test_vampiric_attack(self):
        self.assertEqual(hit(self.hero, self.ms, 9, {'vampiric_hp': 45}, cap=None).total, 90)
        self.assertEqual(hit(self.hero, self.ms, 7, {'vampiric_hp': 45}).total, 8)

    def test_victim_buffs(self):
        holy, shield = [0x913], [0x1D4]
        self.assertEqual(hit(self.ms, self.hero, 6, vic_buffs=holy), DMG.Hit(0, 9, 0, False, 0, False,
                                                                             'holy protection'))
        on_mp = hit(self.ms, self.hero, 9, vic_buffs=shield, vic_mp=40, cap=5)
        self.assertEqual((on_mp.total, on_mp.to_mp), (17, True))                   # not the HP cap
        self.assertEqual(hit(self.ms, self.hero, 9, vic_buffs=shield, vic_mp=10).total, 10)
        dry = hit(self.ms, self.hero, 9, vic_buffs=shield, vic_mp=0, cap=427)
        self.assertEqual((dry.total, dry.to_mp), (17, False))                      # no MP: HP
        # Wicked Protection rank 1 / 3: f = 0.30 / 0.40 of B 9 back -> 2 / 3, B 7 / 6
        r1 = hit(self.ms, self.hero, 6, vic_buffs=[0xB1A])
        r3 = hit(self.ms, self.hero, 6, vic_buffs=[0xB1C])
        self.assertEqual((r1.reflect, r1.total, r3.reflect, r3.total), (2, 7, 3, 6))

    def test_an_arg6_hit_adds_the_weapon_element_again(self):
        # the Monkey Soldier's Earth 5: x the strong share for event 9 (3 -> E 2), the weak one
        # otherwise (1 -> E 0); no event switch, no skill term, no reflection
        self.assertEqual(hit(self.ms, self.hero, 9, direct=10).total, 12)
        self.assertEqual(hit(self.ms, self.hero, 0, direct=10).total, 10)
        self.assertEqual(hit(self.ms, self.hero, 2, direct=10, vic_buffs=[0xB1A]).reflect, 0)


# =========================================================================== the exes
class ExeConstants(unittest.TestCase):
    TABLES = {B8: (0x6F1464, 0x6F13CC, 0x6F1480, 0x6F1410, 0x6F149C, 0x6F142C, 0x6F14B8, 0x6F1448),
              B9: (0x525F44, 0x525EAC, 0x525F60, 0x525EF0, 0x525F7C, 0x525F0C, 0x525F98, 0x525F28)}

    def test_the_class_tables_are_the_exe_values(self):
        try:
            import pefile
        except ImportError:
            self.skipTest('pefile not available')
        want = (DMG.K_STR, DMG.B_STR, DMG.K_DEX, DMG.B_DEX, DMG.K_INT, DMG.B_INT, DMG.K_SPR, DMG.B_SPR)
        checked = 0
        for build, vas in self.TABLES.items():
            if not os.path.exists(EXE[build]):
                continue
            pe = pefile.PE(EXE[build], fast_load=True)
            with self.subTest(build=build):
                self.assertEqual(tuple(struct.unpack('<7f', pe.get_data(va - pe.OPTIONAL_HEADER.ImageBase, 28))
                                       for va in vas), want)
            checked += 1
        if os.path.exists(EXE[B8]):
            pe = pefile.PE(EXE[B8], fast_load=True)
            rd = lambda va, n: pe.get_data(va - pe.OPTIONAL_HEADER.ImageBase, n)   # noqa: E731
            # the weapon-class divisors 10 / 7 / 6 / 0.125, the +1 and the level scale's 2.0
            self.assertEqual([struct.unpack('<d', rd(va, 8))[0] for va in
                              (0x6F8C68, 0x6F8A68, 0x6F8C88, 0x6F8C80, 0x6F8AC8, 0x6F8CA8)],
                             [10.0, 7.0, 6.0, 0.125, 1.0, 2.0])
            # jump table 0x41CA30: wc 0 STR/10, 1 STR/7, 2 STR/6, 3-4 DEX, 5-6 INT
            self.assertEqual(struct.unpack('<7I', rd(0x41CA30, 28)),
                             (0x41BB15, 0x41BB83, 0x41BBB6, 0x41BBE6, 0x41BC16, 0x41BB4C, 0x41BB4C))
        if not checked:
            self.skipTest('no client exe present')


# =========================================================================== EN content
class ContentFlows:
    """The worked examples from the client's own tables of `build` (item ids, hni templates)."""
    build = B8

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        EC.configure(DIRS[self.build], self.build)

    @staticmethod
    def char(cls, level, stats, equipped, job2=1, skills=()):
        return {'class': cls, 'job2': job2, 'exp': progression.exp_for_level(level),
                'str': stats[0], 'dex': stats[1], 'int': stats[2], 'spr': stats[3],
                'equipped': {slot: {'id': i, 'w': [0] * 6} for slot, i in equipped.items()},
                'skills': list(skills)}

    def test_the_records_read_the_right_columns(self):
        it = EC.items()
        self.assertEqual({k: v for k, v in DMG.item_record(it.get(70)).items() if v},
                         {'kind': 11, 'job': [0, 1, 0, 0, 0, 0, 0], 'w_att': 9, 's_att': 23})
        self.assertEqual(DMG.monster_template(EC.npcs().get(15)), MONKEY_SOLDIER)
        self.assertEqual(DMG.monster_template(EC.npcs().get(1)), SSIYO)
        self.assertEqual(DMG.monster_template(EC.npcs().get(2)), KORING)

    def test_testhero_from_the_tables(self):
        hero = self.char(1, 14, (12, 6, 6, 10), {5: 70, 4: 19, 8: 21}, skills=[260])
        st = DMG.player_stats(hero)
        self.assertEqual((st['weak'], st['strong'], st['defense'], st['job']), (18, 37, 2, 1))
        ms = DMG.monster_stats(EC.npcs().get(15))
        self.assertEqual(DMG.damage(st, ms, 7).total, 8)
        self.assertEqual(DMG.damage(st, ms, 9, DMG.cast_skill(hero, 260)).total, 37)
        self.assertEqual(DMG.damage(ms, st, 5, cap=427).total, 17)

    def test_class_2_casts_from_the_tables(self):
        cast_ids = (0x22C, 0x7D8, 0x91E, 0x93F)                 # Blazing / Flying / Triple Kick, Merciless
        monk = self.char(2, 30, (60, 20, 10, 20), {5: 968}, skills=cast_ids)
        st, ms = DMG.player_stats(monk), DMG.monster_stats(EC.npcs().get(15))
        self.assertEqual([DMG.damage(st, ms, 9, DMG.cast_skill(monk, i)).total for i in cast_ids],
                         [91, 87, 28, 22])
        self.assertIsNone(DMG.cast_skill(monk, 0x104))          # a warrior skill: no class-2 cell


class Content2008(ContentFlows, unittest.TestCase):
    build = B8


class Content2009(ContentFlows, unittest.TestCase):
    build = B9


@unittest.skipUnless(HAVE[B8], 'needs the EN 2008 client data')
class Adapters(unittest.TestCase):
    """damage.player_stats / cast_skill / buff_mods over the server's character and session."""

    def setUp(self):
        EC.configure(DIRS[B8], B8)

    def test_only_the_regular_slots_count(self):
        char = ContentFlows.char(1, 14, (12, 6, 6, 10), {5: 70})
        base = DMG.player_stats(char)
        char['equipped'][18] = {'id': 71, 'w': [0] * 6}          # a costume weapon slot: nothing
        self.assertEqual(DMG.player_stats(char)['strong'], base['strong'])
        char['equipped'][15] = {'id': 64, 'w': [0] * 6}          # 2008 slot 15 is regular
        self.assertEqual(DMG.player_stats(char, build=B8)['defense'], 10)   # trunc(137 x 8 / 100)
        self.assertEqual(DMG.player_stats(char, build=B9)['defense'], 0)    # 2009: 15 slots

    def test_buffs_and_the_evasion_passive(self):
        char = ContentFlows.char(1, 14, (12, 6, 6, 10), {5: 70, 4: 19, 8: 21})
        # Increase Attack Power Lv1: Skill_P_A 5 -> Strong 37 + trunc(1.85) = 38; its P_D -2 ->
        # Def 2 + trunc(-4 / 100) = 2 (C division truncates toward 0; no "at least 1" for Def)
        iap = DMG.player_stats(char, {'buffs': [{'id': 292}]})
        self.assertEqual((iap['strong'], iap['defense']), (38, 2))
        # the 21-slot loop skips the auras 0x8E7.. and the summons, the +0x111C slots do not
        self.assertEqual(DMG.player_stats(char, {'buffs': [{'id': 0x8E7}]})['strong'], 37)
        self.assertEqual(DMG.player_stats(char, {'buffs': [{'id': 0x8E7}],
                                                 'slot_buffs': [{'id': 0x8E7}]})['strong'], 38)
        self.assertEqual(DMG.buff_mods({'buffs': [{'id': 0x9CE}, {'id': 391}]}), [])   # summon, Poison
        # Summon Lazy Sloth (0x9D9, Skill_P_D -5): skipped while its slot's caster (+0x14) is the
        # holder - FUN_004262b0 writes the holder's own uid for a 0x3B insert, and the server's
        # own casts carry src 0 - but counted on a holder someone else cast it on
        for own in ({'id': 0x9D9, 'src': 0}, {'id': 0x9D9}, {'id': 0x9D9, 'src': 7}, 0x9D9):
            with self.subTest(own=own):
                self.assertEqual(DMG.buff_mods({'uid': 7, 'buffs': [own]}), [])
        other = {'uid': 7, 'buffs': [{'id': 0x9D9, 'src': 9}]}
        self.assertEqual([m['skill_p_d'] for m in DMG.buff_mods(other)], [-5])
        # SPR 100 in Light Leather (Def 22): trunc((1.2 x 100 + 25 + 100) x 22 / 100) = 53; the
        # Sloth someone else put on him: 53 + trunc(53 x -5 / 100) = 51; his own: still 53
        tank = ContentFlows.char(1, 14, (12, 6, 6, 100), {4: 64, 8: 65, 10: 68})
        self.assertEqual((DMG.player_stats(tank)['defense'],
                          DMG.player_stats(tank, {'uid': 7, 'buffs': [{'id': 0x9D9, 'src': 0}]})['defense'],
                          DMG.player_stats(tank, other)['defense']), (53, 53, 51))
        # a second fairy of one attribute keeps its P_A / P_D but not its A_A / A_D
        fairies = DMG.buff_mods({'buffs': [{'id': 0xA9F}, {'id': 0xAA0}]})
        self.assertEqual([m.get('skill_a_a', 0) for m in fairies], [10, 0])
        # the class 3 Evasion passive: Skill_P_D 2 (and A_D 1). SPR 100 in Light Leather (Def 22):
        # trunc((1.2 x 100 + 15 + 100) x 22 / 100) = 51, + trunc(51 x 2 / 100) = 52
        thief = ContentFlows.char(3, 14, (12, 6, 6, 100), {4: 64, 8: 65, 10: 68}, skills=[0x171])
        self.assertEqual(DMG.evasion_mods(thief), [{'skill_p_d': 2, 'skill_a_d': 1, 'attribute': 0}])
        self.assertEqual((DMG.player_stats(dict(thief, skills=[]))['defense'], DMG.player_stats(thief)['defense']),
                         (51, 52))
        self.assertEqual(DMG.evasion_mods(dict(thief, **{'class': 1})), [])

    def test_cast_skill(self):
        hero = ContentFlows.char(1, 14, (12, 6, 6, 10), {5: 70}, skills=[261])
        self.assertEqual(DMG.cast_skill(hero, 261)['id'], 261)          # rank 2 learned
        self.assertIsNone(DMG.cast_skill(dict(hero, skills=[]), 5))      # no cell
        unlearned = DMG.cast_skill(dict(hero, skills=[]), 261)
        self.assertEqual((unlearned['id'], unlearned['attri_atk']), (0, 0))  # the cell, no term
        priest = ContentFlows.char(6, 60, (5, 5, 30, 20), {}, job2=2, skills=[0xB25])
        self.assertEqual(DMG.cast_skill(priest, 0xB25)['vampiric_hp'], EC.items().get(0xB25).hp)
        self.assertIsNone(DMG.cast_skill(dict(priest, job2=1), 0xB25))
        mage = ContentFlows.char(5, 40, (5, 5, 30, 10), {}, job2=1, skills=[0xA73])
        self.assertTrue(DMG.cast_skill(mage, 0xA73)['nova'])
        thief = ContentFlows.char(4, 40, (3, 2, 1, 3), {}, job2=2, skills=[0xA47, 0xA31])
        self.assertTrue(DMG.cast_skill(thief, 0xA47)['delayed'])
        self.assertEqual(DMG.trap_skill(thief)['id'], 0xA31)
        self.assertIsNone(DMG.trap_skill(dict(thief, skills=[])))


# =========================================================================== grade roll
class Dice:
    """A random.Random stand-in for grade_roll: randrange(1000) gives `r`, randint(900, 1100)
    gives `j`; the calls are recorded."""

    def __init__(self, r, j=1000):
        self.r, self.j, self.calls = r, j, []

    def randrange(self, n):
        self.calls.append(('randrange', n))
        return self.r

    def randint(self, a, b):
        self.calls.append(('randint', a, b))
        return self.j


class GradeRoll(unittest.TestCase):
    """livetest bug 10: the client patch's digit roll (combo_hud_2009.py cave A) on the server."""

    def test_the_table_the_jitter_and_the_truncation(self):
        dice = Dice(0)
        self.assertEqual(DMG.grade_roll(8, dice), (DMG.GRADE_CRITICAL, 12))
        self.assertEqual(dice.calls, [('randrange', 1000), ('randint', 900, 1100)])
        # the thresholds, in the client patch's order: CRITICAL! < 50 <= BAD < 180 <= GOOD < 310
        for r, grade, value in ((49, DMG.GRADE_CRITICAL, 150), (50, DMG.GRADE_BAD, 75),
                                (179, DMG.GRADE_BAD, 75), (180, DMG.GRADE_GOOD, 125),
                                (309, DMG.GRADE_GOOD, 125), (310, DMG.GRADE_NONE, 100),
                                (999, DMG.GRADE_NONE, 100)):
            with self.subTest(r=r):
                self.assertEqual(DMG.grade_roll(100, Dice(r)), (grade, value))
        # jitter 0.9 .. 1.1, then trunc(value x M x J / 10^6)
        self.assertEqual(DMG.grade_roll(100, Dice(999, 900)), (DMG.GRADE_NONE, 90))
        self.assertEqual(DMG.grade_roll(100, Dice(999, 1100)), (DMG.GRADE_NONE, 110))
        self.assertEqual(DMG.grade_roll(11, Dice(100, 1099)), (DMG.GRADE_BAD, 9))     # 9.07
        self.assertEqual(DMG.grade_roll(11, Dice(200, 900)), (DMG.GRADE_GOOD, 12))    # 12.375
        # only BAD may reach 0; every other grade stays >= 1
        self.assertEqual(DMG.grade_roll(1, Dice(100, 900)), (DMG.GRADE_BAD, 0))
        for r in (0, 200, 500):
            self.assertEqual(DMG.grade_roll(1, Dice(r, 900))[1], 1)
        self.assertAlmostEqual(DMG.GRADE_MEAN, 1.025)

    def test_the_distribution_with_a_seeded_rng(self):
        rng = random.Random(20260925)
        rolls = [DMG.grade_roll(1000, rng) for _ in range(20000)]
        share = {g: sum(1 for x, _ in rolls if x == g) / len(rolls)
                 for g in (DMG.GRADE_CRITICAL, DMG.GRADE_BAD, DMG.GRADE_GOOD, DMG.GRADE_NONE)}
        for grade, want in ((DMG.GRADE_CRITICAL, 0.05), (DMG.GRADE_BAD, 0.13), (DMG.GRADE_GOOD, 0.13),
                            (DMG.GRADE_NONE, 0.69)):
            self.assertAlmostEqual(share[grade], want, delta=0.01, msg=grade)
        for grade, lo, hi in ((DMG.GRADE_CRITICAL, 1350, 1650), (DMG.GRADE_BAD, 675, 825),
                              (DMG.GRADE_GOOD, 1125, 1375), (DMG.GRADE_NONE, 900, 1100)):
            got = [v for g, v in rolls if g == grade]
            self.assertTrue(lo <= min(got) and max(got) <= hi, (grade, min(got), max(got)))
        mean = sum(v for _, v in rolls) / len(rolls) / 1000
        self.assertAlmostEqual(mean, DMG.GRADE_MEAN, delta=0.01)
        # a seed replays the same rolls (what the server tests pin)
        again = random.Random(20260925)
        self.assertEqual([DMG.grade_roll(1000, again) for _ in range(50)], rolls[:50])

    def test_damage_rolls_at_the_clamp_before_the_buffs_and_the_cap(self):
        hero, ms = player(1, 14, (12, 6, 6, 10), TESTHERO_ITEMS), DMG.derive_monster(MONKEY_SOLDIER)

        def crit(v):                                            # always CRITICAL! x1.5
            return DMG.grade_roll(v, Dice(0, 1000))
        h = hit(hero, ms, 7, roll=crit)
        self.assertEqual((h.total, h.b, h.grade, h.note), (12, 8, DMG.GRADE_CRITICAL, 'CRITICAL! 8->12'))
        self.assertEqual(hit(hero, ms, 7, roll=crit, cap=10).total, 10)          # the HP cap after it
        self.assertEqual(hit(hero, ms, 7).grade, None)                           # no roll, no grade
        self.assertEqual(hit(hero, ms, 7, roll=lambda v: (DMG.GRADE_BAD, 0)).total, 0)
        self.assertEqual(hit(hero, ms, 7, roll=lambda v: DMG.grade_roll(v, Dice(100, 900))).total,
                         5)                                                      # trunc(8 x 0.675)
        holy = hit(ms, hero, 6, roll=crit, vic_buffs=[0x913])                     # Holy Protection
        self.assertEqual((holy.total, holy.grade), (0, DMG.GRADE_CRITICAL))
        none = hit(hero, ms, 7, roll=lambda v: DMG.grade_roll(v, Dice(999, 1000)))
        self.assertEqual((none.total, none.grade, none.note), (8, DMG.GRADE_NONE, ''))


# =========================================================================== server
ACCOUNTS = {
    'test': {'password': 'test',
             'characters': [{'name': 'TestHero', 'level': 1, 'class': 0, 'map': 102,
                             'x': 1000, 'y': 714, 'hp': 100, 'mp': 50, 'gm': 1}]},
}
KEYS = {
    B8: {'login': '0x44D8BF/0x01', 'enter': '0x42F904/0x2B', 'move': '0x42CE94/0x0D'},
    B9: {'login': '0x451CE5/0x01', 'enter': '0x4315D7/0x2B', 'move': '0x42E704/0x0D'},
}
ENTRY = {B8: (0x03, 0x07, 0x15, 0x28, 0x44), B9: (0x03, 0x07, 0x15, 0x65, 0x28, 0x44)}
MONKEY_SOLDIER_IDX = 15


class DamageServer:
    """A fresh GameServer of `build` (defaults + `overrides`, monster AI off so a swing event
    needs no attack command) with TestHero - the live case: class 1 job 1, Lv14, 12/6/6/10,
    Wooden Blade, shirt and skirt - on map 102 and monster #1 made a Monkey Soldier."""
    build = B8
    overrides = {}
    grade_roll = False                  # the rig's pin (fakeclient.make_server); GradeRollFlows: on

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        self.tmp = tempfile.mkdtemp(prefix='ws_damage_')
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, 'MOB_AGGRO': False,
                                'MOB_CONTACT_AGGRO_ONLY': False, **self.overrides})
        self.server = F.make_server(self.tmp, accounts=copy.deepcopy(ACCOUNTS), config=cfg,
                                    grade_roll=self.grade_roll)
        self.keys = KEYS[self.build]
        self.clients = []
        with self.server.store.lock:
            ch = self.server.store.find_character('test', 'TestHero')
            ch.update({'class': 1, 'job2': 1, 'exp': progression.exp_for_level(14),
                       'str': 12, 'dex': 6, 'int': 6, 'spr': 10, 'skills': [260, 271],
                       'equipped': {5: {'id': 70, 'w': [0] * 6}, 4: {'id': 19, 'w': [0] * 6},
                                    8: {'id': 21, 'w': [0] * 6}}})
            d = hpmp.derive({}, ch)
            ch['hp'], ch['mp'] = d.max_hp, d.max_mp
        self.server.store.mark_dirty('test setup')
        self.c = self.enter()
        with self.server._combat_lock(self.c.session):
            npcs = EC.npcs()
            uid = W.MOB_UID_BASE + 1
            self.mob = self.server._make_monster(npcs.get(MONKEY_SOLDIER_IDX),
                                                 npcs.template_index(MONKEY_SOLDIER_IDX), uid, 1244.0, 714.0)
            self.c.session['monsters'][uid] = self.mob

    def tearDown(self):
        for c in self.clients:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def enter(self):
        c = F.FakeClient(self.server)
        self.clients.append(c)
        if self.build == B9:
            c.send_c2s(self.keys['login'], F.sso_login('test', 'test'))
        else:
            c.send_c2s(self.keys['login'], {'account_id': 'test', 'password': 'test'})
        self.assertEqual(c.s2c(c.expect(0x02))['result'], 1)
        c.send_c2s(self.keys['enter'], {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907, 'char_name': 'TestHero'})
        F.expect_entry(c, (*ENTRY[self.build], *F.mob_packets(8)), self.clients, 102)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world') and s.get('current_map') == 102))
        return c

    def move(self, state_lo, **extra):
        self.c.send_c2s(self.keys['move'], {'realtime_delta_ms': 0, 'map_code': 102, 'logic_elapsed_ms': 30,
                                            'state_lo': state_lo, 'state_hi': 0, **extra})

    def report(self, event):
        """The attacker's own hit report: interact event `event` (bits 16-19) on the mob."""
        self.move(event << 16, target_uid=self.mob.uid, pos_x=1200.0, pos_y=714.0, target_dx=44.0,
                  target_dy=0.0, flag_8db=0, flag_8e7=0, timer_dac=0, target_action_event=event)

    def struck(self, action):
        """The victim's report of the Monkey Soldier hitting him: action event `action`."""
        with self.server._combat_lock(self.c.session):
            self.c.session.pop('contact_t', None)                # the contact and swing slots
            self.c.session.pop('swing_t', None)
        self.move(action << 12, event_source_uid=self.mob.uid)

    def hp_after(self, action):
        self.struck(action)
        return self.c.s2c(self.c.expect(0x28, quiet=0.1))['hp']


class ClientFormulaFlows(DamageServer):
    def test_testhero_hits_a_monkey_soldier_for_8_then_24(self):
        # live 2026-09-24: the placeholder took 1 while his client drew 7-10 (8 x the grade roll)
        self.report(7)
        self.c.expect(0x2A)                                      # the hit-lock release
        self.assertEqual(self.mob.hp, 104 - 8)
        self.report(9)                                           # a strong attack, no cast
        self.c.expect(0x2A)
        self.assertEqual(self.mob.hp, 96 - 24)

    def test_the_monkey_soldier_hits_testhero_per_event(self):
        hp = self.c.session['hp']
        for action, taken in ((6, 9), (1, 9), (7, 8), (8, 8), (9, 17), (10, 17), (4, 17), (5, 17)):
            with self.subTest(action=action):
                hp -= taken
                self.assertEqual(self.hp_after(action), hp)
        for guarded in (2, 3):                                   # a weak swing into his guard
            self.struck(guarded)
            self.c.expect_silence(0.15)
        self.assertEqual(self.c.session['hp'], hp)

    def test_victim_buffs(self):
        now = time.monotonic()
        with self.server._combat_lock(self.c.session):
            B.apply(self.c.session, 0x913, now)                  # Holy Protection
        hp = self.c.session['hp']
        self.struck(6)
        self.c.expect_silence(0.15)
        self.assertEqual(self.c.session['hp'], hp)
        with self.server._combat_lock(self.c.session):
            B.remove(self.c.session, 0x913)
            B.apply(self.c.session, 0x1D4, now)                  # Magic Shield: the hit takes MP
        mp = self.c.session['mp']
        self.struck(9)
        self.assertEqual(self.c.s2c(self.c.expect(0x44, quiet=0.1)), {'mp': mp - 17})
        self.assertEqual(self.c.session['hp'], hp)
        with self.server._combat_lock(self.c.session):
            B.remove(self.c.session, 0x1D4)
            B.apply(self.c.session, 0xB1A, now)                  # Wicked Protection Lv1
        # contact B 9: trunc(0.3 x 9) = 2 goes back (an arg6 hit: 2, TestHero has no element)
        self.assertEqual(self.hp_after(6), hp - 7)
        self.assertEqual(self.mob.hp, 104 - 2)

    def test_the_fire_window(self):
        npcs = EC.npcs()
        mobs = [self.server._make_monster(npcs.get(MONKEY_SOLDIER_IDX), npcs.template_index(MONKEY_SOLDIER_IDX),
                                          W.MOB_UID_BASE + 30 + i, 1244.0, 714.0) for i in range(3)]
        hero = self.server._session_char(self.c.session)
        fire = DMG.cast_skill(hero, 271)                        # Fire Beat Lv1
        T = 1000.0
        with self.server._combat_lock(self.c.session):
            first = self.server._formula_hit(self.c.session, mobs[0], 9, fire, now=T)
            splash = self.server._formula_hit(self.c.session, mobs[1], 9, fire, now=T + 0.1)
            again = self.server._formula_hit(self.c.session, mobs[2], 9, fire, now=T + 0.8)
        self.assertEqual([(h.b, h.e, h.total) for h in (first, splash, again)],
                         [(24, 14, 38), (0, 4, 4), (24, 14, 38)])

    def test_compute_damage_is_the_formula(self):
        self.assertEqual((self.server._compute_damage(self.c.session, 0, self.mob),
                          self.server._compute_damage(self.c.session, 1, self.mob)), (8, 24))


class ClientFormula2008(ClientFormulaFlows, unittest.TestCase):
    build = B8


class ClientFormula2009(ClientFormulaFlows, unittest.TestCase):
    build = B9


class PlaceholderFlows(DamageServer):
    """DAMAGE_FORMULA 'placeholder' brings the old rules back (rollback)."""
    overrides = {'DAMAGE_FORMULA': 'placeholder'}

    def test_the_old_numbers(self):
        # STR 12 + W_Att 9 - Def 30 -> the 1 of the live report; x1.5 for the strong attack
        self.assertEqual((self.server._compute_damage(self.c.session, 0, self.mob),
                          self.server._compute_damage(self.c.session, 1, self.mob)), (1, 1))
        self.report(7)
        self.c.expect(0x2A)
        self.assertEqual(self.mob.hp, 103)
        # Body_Atk 14 / Strong_Atk 21 - the equipment Def sum 2; event 5 is a strong hit now too
        hp = self.c.session['hp']
        self.assertEqual(self.hp_after(6), hp - 12)
        self.assertEqual(self.hp_after(5), hp - 12 - 19)


class Placeholder2008(PlaceholderFlows, unittest.TestCase):
    build = B8


class GradeRollFlows(DamageServer):
    """config DAMAGE_GRADE_ROLL on: the server's value of a player's hit on a monster is rolled
    with GameServer.DAMAGE_RNG - a seeded Random here - and nothing else is."""
    grade_roll = True
    SEED = 1025

    def test_reported_hits_follow_the_seeded_dice(self):
        self.assertIsNotNone(self.server._grade_roller())
        self.server.DAMAGE_RNG = random.Random(self.SEED)
        twin = random.Random(self.SEED)
        hp = self.mob.hp
        for event, base in ((7, 8), (9, 24), (7, 8), (7, 8), (9, 24)):
            with self.subTest(event=event):
                grade, rolled = DMG.grade_roll(base, twin)
                self.report(event)
                self.c.expect(0x2A)
                hp = max(0, hp - rolled)
                self.assertEqual(self.mob.hp, hp, grade)
        # the formula's own value is the base: off again, the numbers are back
        self.server.config = cfgmod.from_dict({**dict(self.server.config), 'DAMAGE_GRADE_ROLL': False})
        self.mob.hp = self.mob.max_hp                            # no HP cap in the way
        self.assertEqual((self.server._compute_damage(self.c.session, 0, self.mob),
                          self.server._compute_damage(self.c.session, 1, self.mob)), (8, 24))

    def test_skills_are_rolled_but_monster_hits_and_dot_ticks_are_not(self):
        self.server.DAMAGE_RNG = Dice(0, 1100)                   # always CRITICAL! x1.65
        # a player's hits on the monster: swing 8 -> 13, strong 24 -> 39, Ice Spear 37 -> 61
        self.assertEqual(self.server._compute_damage(self.c.session, 0, self.mob), 13)
        self.assertEqual(self.server._compute_damage(self.c.session, 1, self.mob), 39)
        with self.server._combat_lock(self.c.session):
            dmg, text = self.server._skill_hit_damage(self.c.session, SK.skill_def(260), self.mob)
        self.assertEqual(dmg, 61)
        self.assertIn('CRITICAL! 37->61', text)
        self.assertEqual(self.server._graded(10), 16)            # the placeholder formula's path
        # the Monkey Soldier's hits on TestHero: never rolled (contact 9, strong 17)
        hp = self.c.session['hp']
        self.assertEqual(self.hp_after(6), hp - 9)
        self.assertEqual(self.hp_after(9), hp - 9 - 17)
        # a DoT tick: the rule's own number (Poison 9 a tick), never rolled
        now = time.monotonic()
        with self.server._combat(self.c.session):
            rec, _ = D.apply(self.mob, SK.skill_def(391), now, src=1, tick_damage=9)
        hp = self.mob.hp
        self.assertEqual(self.server._tick_debuffs(rec['next_tick']), 1)
        self.assertEqual(self.mob.hp, hp - 9)


class GradeRoll2008(GradeRollFlows, unittest.TestCase):
    """DAMAGE_GRADE_ROLL true forced on a 2008 server (a 2008 exe given a rolling digit)."""
    build = B8


class GradeRoll2009(GradeRollFlows, unittest.TestCase):
    """The shipped default (null, auto) on a 2009 server: its combo HUD v2 digit rolls, so
    the server rolls too."""
    build = B9
    grade_roll = None                   # the config's own value: the default null


class GradeRollAuto2008(DamageServer, unittest.TestCase):
    """The shipped default (null, auto) on a 2008 server: no 2008 exe rolls its digit, so the
    server takes exactly the formula's value the client draws (review of livetest bug 10: a
    roll here made the HP taken disagree with the exact digit - bug 10 in reverse)."""
    build = B8
    grade_roll = None                   # the config's own value: the default null

    def test_the_default_keeps_the_formula_value(self):
        self.assertIsNone(self.server.config.DAMAGE_GRADE_ROLL)
        self.assertIsNone(self.server._grade_roller())
        self.server.DAMAGE_RNG = Dice(0, 1100)                   # would be CRITICAL! x1.65
        self.assertEqual(self.server._compute_damage(self.c.session, 0, self.mob), 8)
        self.assertEqual(self.server._compute_damage(self.c.session, 1, self.mob), 24)
        with self.server._combat_lock(self.c.session):
            dmg, text = self.server._skill_hit_damage(self.c.session, SK.skill_def(260), self.mob)
        self.assertEqual(dmg, 37)                                # Ice Spear, unrolled
        self.assertNotIn('CRITICAL', text)
        self.assertEqual(self.server._graded(10), 10)            # the placeholder formula's path
        self.report(7)
        self.c.expect(0x2A)
        self.assertEqual(self.mob.hp, 104 - 8)                   # the digit the 2008 exe draws
        self.assertEqual(self.server.DAMAGE_RNG.calls, [])       # the dice were never touched


class ConfigKey(unittest.TestCase):
    def test_the_grade_roll_key(self):
        self.assertIsNone(cfgmod.defaults().DAMAGE_GRADE_ROLL)  # auto
        for value in (True, False, None):
            with self.subTest(value=value):
                self.assertIs(cfgmod.from_dict({'DAMAGE_GRADE_ROLL': value}).DAMAGE_GRADE_ROLL, value)
        for bad in (1, 0, 'auto', 'true'):
            with self.subTest(bad=bad), self.assertRaises(cfgmod.ConfigError):
                cfgmod.from_dict({'DAMAGE_GRADE_ROLL': bad})
        # auto follows the build; true / false force it on either build
        on = cfgmod.grade_roll_on
        self.assertEqual([on(cfgmod.from_dict({'CLIENT_BUILD': b})) for b in (B8, B9)], [False, True])
        for value in (True, False):
            self.assertEqual([on(cfgmod.from_dict({'CLIENT_BUILD': b, 'DAMAGE_GRADE_ROLL': value}))
                              for b in (B8, B9)], [value, value])

    def test_the_formula_key(self):
        self.assertEqual(cfgmod.defaults().DAMAGE_FORMULA, 'client')
        self.assertEqual(cfgmod.from_dict({'DAMAGE_FORMULA': 'placeholder'}).DAMAGE_FORMULA, 'placeholder')
        for bad in ('Client', 'exe', 1):
            with self.subTest(bad), self.assertRaises(cfgmod.ConfigError):
                cfgmod.from_dict({'DAMAGE_FORMULA': bad})


if __name__ == '__main__':
    unittest.main(verbosity=2)
