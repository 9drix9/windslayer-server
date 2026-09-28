#!/usr/bin/env python3
"""
test_skills.py - skill definitions and the client's skill-range tables (cs-skill-defs)

Pure unit tests, no sockets: the EN hii Type-3 records through skills.skill_def, the ports of
FUN_004257a0 / FUN_004258a0 / FUN_004258d0 / FUN_00424e20 / FUN_00403fa0 at their exact
range boundaries (read from the decomp for this stage), the kind classifier of
combat_skill.md 3.3 against the families it names, and the KR gamedef parity that lets the
server trust the EN numbers (open question 16).
"""
import os
import sqlite3
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import en_content as EC  # noqa: E402
import skills as SK  # noqa: E402

HAVE_HII = os.path.exists(EC.hs_path('windslayer.hii'))


@unittest.skipUnless(HAVE_HII, 'needs the EN client hs/windslayer.hii (numeric skill columns)')
class SkillRecords(unittest.TestCase):
    def test_not_a_skill(self):
        self.assertIsNone(SK.skill_def(179))          # Wooden Stick (Type 1)
        self.assertIsNone(SK.skill_def(5))            # Herb (Type 0)
        self.assertIsNone(SK.skill_def(0))
        self.assertIsNone(SK.skill_def(4249))         # past the EN table (KR-only ids)
        self.assertIsNone(SK.skill_def('x'))

    def test_increase_attack_power_lv1(self):
        """The design doc's T-3B-SELF example: HP -5 MP -5, Con 30000, CT 10000."""
        sd = SK.skill_def(292)
        self.assertEqual((sd.family, sd.level, sd.skill_lv), (291, 1, 1))
        self.assertEqual((sd.mp_cost, sd.hp_cost, sd.ct, sd.con, sd.lv), (5, 5, 10000, 30000, 12))
        self.assertEqual((sd.skill_p_a, sd.skill_p_d), (5, -2))
        self.assertEqual(sd.kind, 'self_buff')
        self.assertTrue(sd.castable and sd.slot_on_3b and sd.stat_mod and sd.hp_gated)
        self.assertEqual(sd.job, (0, 1, 0, 0, 0, 0, 0))
        self.assertTrue(sd.allows_job(1))
        self.assertFalse(sd.allows_job(0))
        self.assertEqual(sd.duration_ms, 30000)

    def test_heals(self):
        self_heal, heal = SK.skill_def(501), SK.skill_def(446)
        self.assertEqual((self_heal.kind, self_heal.caster_hp_delta, self_heal.mp_cost), ('self_heal', 20, 10))
        # FUN_004258d0 = 0: Heal's +40 is the TARGET's, the caster's client adds nothing.
        self.assertEqual((heal.kind, heal.caster_hp, heal.caster_hp_delta, heal.hp_cost), ('target_heal', False, 0, 0))

    def test_trap_debuff_summon_party(self):
        trap = SK.skill_def(2609)                     # Booby Trap Lv1
        self.assertEqual((trap.kind, trap.con, trap.duration_ms, trap.mp_cost), ('trap', 7020, 7020, 16))
        bomb = SK.skill_def(2631)                     # Time Bomb Lv1
        self.assertEqual(bomb.kind, 'debuff')
        self.assertTrue(bomb.detonates)
        self.assertFalse(bomb.slot_on_3b)             # 0x3B inserts nothing: 0x41 on the target
        poison = SK.skill_def(0x187)
        self.assertEqual((poison.kind, poison.hp, poison.hp_cost), ('debuff', -3, 0))
        self.assertTrue(poison.dot)
        summon = SK.skill_def(2500)                   # Summon Roaring Dog Lv2
        self.assertEqual((summon.kind, summon.con, summon.duration_ms), ('summon', 8010, SK.SUMMON_SLOT_MS))
        vitality = SK.skill_def(2798)                 # Breath of Vitality Lv1
        self.assertEqual((vitality.kind, vitality.mhp), ('aura', 50))
        self.assertTrue(vitality.party and vitality.stat_mod)
        self.assertEqual(SK.skill_def(2776).kind, 'party_heal')   # Group Heal Lv1
        self.assertEqual(SK.skill_def(260).kind, 'attack')        # Ice Spear Lv1
        self.assertEqual(SK.skill_def(194).kind, 'utility')       # Open Stall
        self.assertEqual(SK.skill_def(2188).kind, 'utility')      # Reinforce (0x88C)

    def test_passives_and_bases(self):
        dj = SK.skill_def(94)                         # Double Jump: a quest-26 reward
        self.assertEqual((dj.kind, dj.passive, dj.castable, dj.max_family_level), ('passive', True, False, 1))
        self.assertEqual(SK.skill_def(291).kind, 'base')
        self.assertEqual(SK.skill_def(496).mp, 34)    # Improve Mana Recovery Lv7 (regen)

    def test_catalog_counts(self):
        """EN hii: 1186 Type-3 records, 116 family bases, 66 passives, 420 castable slot
        skills (Con > 0, Skill_Lv > 0); every record's family base exists (id - |Skill_Lv|)."""
        every = SK.all_skills()
        self.assertEqual(len(every), 1186)
        self.assertEqual(sum(sd.base for sd in every.values()), 116)
        self.assertEqual(sum(sd.passive for sd in every.values()), 66)
        self.assertEqual(sum(1 for sd in every.values() if sd.con > 0 and sd.skill_lv > 0), 420)
        for sd in every.values():
            base = every.get(sd.family)
            self.assertIsNotNone(base, sd)
            self.assertEqual(base.skill_lv, 0, sd)
            self.assertIn(sd.kind, SK.KINDS)

    def test_override(self):
        SK.SKILL_KIND_OVERRIDE[291] = 'attack'
        try:
            self.assertEqual(SK.skill_def(292).kind, 'attack')
        finally:
            SK.SKILL_KIND_OVERRIDE.pop(291)
        self.assertEqual(SK.skill_def(292).kind, 'self_buff')

    def test_gamedef_parity(self):
        """combat_skill.md open question 16: every KR gamedef Type-3 row with id <= 4248
        carries the EN hii numbers (checked for this stage: 1186 rows, 0 differences)."""
        path = EC.GAMEDEF_PATH
        if not os.path.exists(path):
            self.skipTest('no gamedef.sqlite3')
        cols = ('HP', 'MP', 'mHP', 'mMP', 'Attribute', 'Attri_Atk', 'Attri_Def', 'Skill_P_A',
                'Skill_P_D', 'Skill_A_A', 'Skill_A_D', 'Lv', 'Buy', 'PMoney', 'CT', 'Con', 'Skill_Lv')
        attrs = ('hp', 'mp', 'mhp', 'mmp', 'attribute', 'attri_atk', 'attri_def', 'skill_p_a',
                 'skill_p_d', 'skill_a_a', 'skill_a_d', 'lv', 'buy', 'pmoney', 'ct', 'con', 'skill_lv')
        con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        try:
            rows = con.execute(f'SELECT idx, Job, {", ".join(cols)} FROM items '
                               f'WHERE Type = 3 AND idx <= {SK.SKILL_MAX_ID}').fetchall()
        finally:
            con.close()
        self.assertEqual(len(rows), 1186)
        for row in rows:
            sd = SK.skill_def(row[0])
            self.assertIsNotNone(sd, row[0])
            self.assertEqual(tuple(int(t) for t in str(row[1]).split()), sd.job, row[0])
            for attr, value in zip(attrs, row[2:]):
                self.assertEqual(int(value), getattr(sd, attr), f'{row[0]} {attr}')


class RangeFunctions(unittest.TestCase):
    """Boundaries exactly as the decomp compares them (u16 ids)."""

    def test_fun_004257a0(self):
        for lo, hi in [(0x187, 0x191), (0x1B3, 0x1BD), (0x242, 0x24C), (0x78B, 0x795), (0x7B7, 0x7C1),
                       (0x8BB, 0x8C5), (0xA05, 0xA0F), (0x981, 0x98B), (0xA47, 0xA51), (0xA3C, 0xA46),
                       (0xA52, 0xA5C), (0xA5D, 0xA67), (0x997, 0x9A1), (0xB0F, 0xB19), (0xB30, 0xB3A),
                       (0xB3B, 0xB45)]:
            self.assertFalse(SK.slot_inserting(lo), hex(lo))
            self.assertFalse(SK.slot_inserting(hi), hex(hi))
        for i in (0x186, 0x192, 0x124, 0xA31, 0xA3B, 0xB46, 1):
            self.assertTrue(SK.slot_inserting(i), hex(i))

    def test_fun_004258a0(self):
        self.assertEqual([SK.forced_slot(i) for i in (0x9CD, 0x9CE, 0x9D8, 0x9D9, 0x9E3, 0x9E4, 0x9F9, 0x9FA)],
                         [False, True, True, False, False, True, True, False])

    def test_fun_004258d0(self):
        for i in (0x187, 0x191, 0x1BE, 0x1C8, 0x24D, 0x257, 0x8C6, 0x8D0, 0x8F2, 0x8FC, 0x997, 0x9A1,
                  0x9C3, 0x9F9, 0xA5D, 0xA67, 0xAF9, 0xB03, 0xB0F, 0xB19, 0xB25, 0xB2F):
            self.assertFalse(SK.caster_hp(i), hex(i))
        for i in (0x186, 0x1BD, 0x1F5, 0x258, 0x9C2, 0x9FA, 0xB30, 0xAD8):
            self.assertTrue(SK.caster_hp(i), hex(i))

    def test_fun_00424e20_party(self):
        self.assertEqual([SK.party_buff(i) for i in (0x8E6, 0x8E7, 0x912, 0x913, 0xAE2, 0xAE3, 0xAF8, 0xAF9)],
                         [False, True, True, False, False, True, True, False])

    def test_fun_00403fa0_family_max_level(self):
        for i in (0x50, 0x52, 0x56, 0x58, 0x5A, 0x5E, 0xC2, 0x88C, 0xAD6, 0xC2C):
            self.assertEqual(SK.family_max_level(i), 1, hex(i))
        for i in (0x51, 0x5F, 0x124, 0xAD7, 0x88B):
            self.assertEqual(SK.family_max_level(i), 10, hex(i))

    def test_learned_in_family(self):
        """FUN_00426c50(first) with EDI = 10: lowest learned id in [first, first+9]; the
        client's list scan stops at the first 0."""
        self.assertEqual(SK.learned_in_family([292, 496], 0x1EA), 496)
        self.assertEqual(SK.learned_in_family([499, 490], 0x1EA), 490)
        self.assertEqual(SK.learned_in_family([500, 489], 0x1EA), 0)
        self.assertEqual(SK.learned_in_family([0, 496], 0x1EA), 0)
        self.assertEqual(SK.learned_in_family([{'skill_id': 111}], 0x6F), 111)
        self.assertEqual(SK.learned_in_family(None, 0x6F), 0)


if __name__ == '__main__':
    unittest.main()
