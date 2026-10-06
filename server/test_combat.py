#!/usr/bin/env python3
"""
test_combat.py - skill damage, heal visuals, player death / revive, traps and debuffs
(P3 stage 4: cs-skill-damage, cs-heal-visuals, cs-player-death, cs-traps, cs-debuffs;
combat_skill.md F3a/F3b/F3c/F3h/F7/F8, spec corrections C4/C16/C28)

Two layers:
- the pure rules (combat.py hitboxes / damage / FUN_0041a230 death penalty / revive points,
  debuffs.py monster slots);
- the server through fakeclient (no port, no game client, temp accounts.json): an attack
  skill kills the Pupu in front with exp, heals reach the observers as S2C 0x40 and the
  healed player as 0x3D, a dev damage event reaches 0 -> S2C 0x3E death dialog -> C2S 0x2E
  -> MapTransfer to the revive point with partial HP, a Booby Trap trigger (C2S 0x6C + the
  0x0D interact-0xC victim, or the fallback, or !trap) damages and clears the trap icon
  (0x3C), and targeted debuffs show on the monster (0x41) and tick / detonate / end on the
  server's clock.
"""
import copy
import hashlib
import logging
import os
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
import combat as C  # noqa: E402
import config as cfgmod  # noqa: E402
import debuffs as D  # noqa: E402
import en_content as EC  # noqa: E402
import fakeclient as F  # noqa: E402
import hpmp  # noqa: E402
import progression  # noqa: E402
import registry  # noqa: E402
import skills as SK  # noqa: E402
from wsproto import hexbytes  # noqa: E402

W = F.import_server()
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
HAVE_HII = os.path.exists(EC.hs_path('windslayer.hii'))
CLIENT_EXE = os.path.join(os.path.dirname(HERE), 'WindSlayer.exe')
CAP_7E_101_TO_102 = hexbytes('17 00 00 00')

ICE_SPEAR = 260                  # warrior attack Lv10, MP 6, Attri_Atk 9
HEAVEN_STRIKE = 2268             # warrior Lv49, HP 47 / MP 47, wide 300/300/120 box
IAP1 = 292                       # Increase Attack Power Lv1: Skill_P_A 5 while it runs
SELF_HEAL = 501                  # priest, HP +20
HEAL = 446                       # priest Heal Lv1, HP +40 on the target (not the caster)
POISON = 391                     # thief Poison Lv1: DoT |-3|, Con 9000
TIME_BOMB = 2631                 # thief Lv37, detonates at Con 5010, Attri_Atk 45
STUN = 435                       # thief Lv12, control, Con 3000
BOOBY_TRAP = 2609                # thief Lv30, MP 16, Con 7020, Attri_Atk 32
WOODEN_BLADE, DAGGER, CLERIC_WAND = 70, 256, 248                 # W_Att 9 / 15 / 6
MINOR_POTION = 7                 # Type 0, HP +60
PUPU = [W.MOB_UID_BASE + i for i in range(8)]   # map 102: (1339,423) (1244,714) (1594,714) ...

ACCOUNTS = {
    'test': {'password': 'test',
             'characters': [{'name': 'TestHero', 'level': 1, 'class': 0, 'map': 0,
                             'x': 100, 'y': 100, 'hp': 100, 'mp': 50, 'gm': 1}]},
    'admin': {'password': 'admin',
              'characters': [{'name': 'Watcher', 'level': 1, 'class': 0, 'map': 0,
                              'x': 100, 'y': 100, 'hp': 100, 'mp': 50}]},
}
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
            'accounts.json changed during test_combat.py (tests must only use temp copies)'


def dec(pkt):
    return F.FakeClient.decode(pkt, allow_trailing=pkt.opcode == 0x03)


class Mob:
    """A stand-in with the Monster fields the pure rules read."""

    def __init__(self, uid, x, y, alive=True):
        self.uid, self.x, self.y, self.alive = uid, float(x), float(y), alive
        self.debuffs = {}


# =========================================================================== pure rules
class Hitboxes(unittest.TestCase):
    def test_the_box_lies_in_front_of_the_facing(self):
        box = C.DEFAULT_HITBOX
        self.assertEqual(box, C.Hitbox(150, 0, 60))
        self.assertTrue(C.in_hitbox((100, 500), C.FACING_RIGHT, (250, 560), box))
        self.assertFalse(C.in_hitbox((100, 500), C.FACING_RIGHT, (251, 500), box))
        self.assertFalse(C.in_hitbox((100, 500), C.FACING_RIGHT, (99, 500), box))   # behind
        self.assertFalse(C.in_hitbox((100, 500), C.FACING_RIGHT, (150, 561), box))  # too high
        self.assertTrue(C.in_hitbox((100, 500), C.FACING_LEFT, (0, 500), box))
        self.assertFalse(C.in_hitbox((100, 500), C.FACING_LEFT, (150, 500), box))

    def test_wide_sweeps_reach_both_sides(self):
        box = C.hitbox(SK.skill_def(HEAVEN_STRIKE).family)
        self.assertEqual(box, C.Hitbox(300, 300, 120))
        self.assertEqual(C.hitbox(SK.skill_def(ICE_SPEAR).family), C.DEFAULT_HITBOX)
        self.assertTrue(C.in_hitbox((1000, 500), C.FACING_RIGHT, (720, 600), box))

    def test_targets_are_live_and_nearest_first(self):
        mobs = [Mob(3, 240, 500), Mob(1, 130, 500), Mob(2, 120, 500, alive=False), Mob(4, 50, 500)]
        self.assertEqual([m.uid for m in C.targets(mobs, (100, 500), C.FACING_RIGHT, C.DEFAULT_HITBOX)],
                         [1, 3])
        self.assertIs(C.nearest(mobs, (230, 520)), mobs[0])
        self.assertIsNone(C.nearest(mobs, (400, 500), 40, 30))

    def test_facing_sources(self):
        self.assertEqual([C.facing_from_state(v) for v in (1, 2, 0, 3, 0x61400001)],
                         [C.FACING_LEFT, C.FACING_RIGHT, None, None, C.FACING_LEFT])
        self.assertEqual([C.facing_from_entity(v) for v in (2, 6, 8, 0)],
                         [C.FACING_LEFT, C.FACING_RIGHT, None, None])


@unittest.skipUnless(HAVE_HII, 'needs the EN client hs/windslayer.hii')
class Damage(unittest.TestCase):
    def test_skill_damage_placeholder(self):
        spear = SK.skill_def(ICE_SPEAR)                  # Skill_P_A 0, Attri_Atk 9
        self.assertEqual(C.skill_damage(spear, 12, 2), 11)           # int(12 * 1.09) - 2
        self.assertEqual(C.skill_damage(spear, 12, 2, buff_p_a=5), 16)
        self.assertEqual(C.skill_damage(spear, 1, 50), 1)            # never below 1
        double = SK.skill_def(1898)                     # Double Attack: Skill_P_A 15
        self.assertEqual(C.skill_damage(double, 20, 0), 23)
        self.assertEqual(C.dot_damage(SK.skill_def(POISON), 40), 9)  # |-3| * (1 + 40/20)
        self.assertEqual(C.dot_damage(SK.skill_def(STUN), 1), 1)
        self.assertEqual(C.body_damage(3, 0), 3)
        self.assertEqual(C.body_damage(3, 10), 1)

    def test_attack_power_is_str_plus_weapon(self):
        char = {'str': 3, 'equipped': {5: {'id': WOODEN_BLADE, 'w': [0] * 6}}}
        self.assertEqual(C.attack_power(char), 12)
        self.assertEqual(C.attack_power({'str': 3, 'equipped': {5: DAGGER}}), 18)
        self.assertEqual(C.attack_power({}), 0)


class DeathPenalty(unittest.TestCase):
    """FUN_0041a230: min(exp into the level, level span x bracket rate), levels 10..98."""

    def test_vectors(self):
        lv12 = progression.exp_for_level(12)
        self.assertEqual(C.death_penalty(lv12 + 500), 443)           # 8869 x 0.05 -> 443
        self.assertEqual(C.death_penalty(lv12 + 100), 100)           # never below the level
        self.assertEqual(C.death_penalty(progression.exp_for_level(9) + 300), 0)
        lv25 = progression.exp_for_level(25)
        self.assertEqual(C.death_penalty(lv25 + 10000), int(progression.EXP_INC[24] * 0.05999999865889549))
        self.assertEqual(C.death_penalty(progression.EXP_MAX), 0)     # level 99
        self.assertEqual(C.death_penalty(lv12 + 500, waived=True), 0)
        for exp in (lv12 + 500, lv25 + 10000, progression.exp_for_level(70) + 5):
            self.assertEqual(progression.level_for_exp(exp - C.death_penalty(exp)),
                             progression.level_for_exp(exp))

    def test_rates_are_the_exe_constants(self):
        try:
            import pefile
        except ImportError:
            self.skipTest('pefile not available')
        if not os.path.exists(CLIENT_EXE):
            self.skipTest(f'{CLIENT_EXE} not available')
        pe = pefile.PE(CLIENT_EXE, fast_load=True)
        rd = lambda va, n: pe.get_data(va - pe.OPTIONAL_HEADER.ImageBase, n)   # noqa: E731
        vas = (0x6F8C98, 0x6F8D98, 0x6F8D90, 0x6F8D88, 0x6F8D80, 0x6F8D78)
        self.assertEqual(tuple(struct.unpack('<d', rd(va, 8))[0] for va in vas),
                         tuple(rate for _top, rate in C.PENALTY_RATES))
        # [0x6F0EAC + 4 * level] is the span of the current level (EXP_INC[level - 1])
        self.assertEqual(struct.unpack('<12i', rd(0x6F0EAC + 4, 48)), progression.EXP_INC[:12])


class RevivePoints(unittest.TestCase):
    START = (101, 1411.0, 714.0)

    def point(self, map_code, overrides=None):
        return C.revive_point(map_code, EC.portals(), start=self.START, overrides=overrides)

    def test_nearest_town_by_portal_hops(self):
        self.assertEqual(sorted(C.towns(EC.portals())), [201, 401, 501, 601, 701, 801, 901, 1001, 1101])
        self.assertEqual(self.point(102), (101, 1405.0, 714.0))      # through 102_31
        self.assertEqual(self.point(103)[0], 101)                    # two hops
        self.assertEqual(self.point(202)[0], 201)
        self.assertEqual(self.point(9701)[0], 101)                   # the flea market's exit

    def test_towns_start_and_overrides(self):
        self.assertEqual(self.point(101), self.START)
        # dying in a town revives there, where its first portal from another map arrives
        first = min((int(k.split('_')[0]), int(k.split('_')[1]), float(v[1]), float(v[2]))
                    for k, v in EC.portals().items()
                    if int(v[0]) == 201 and int(k.split('_')[0]) != 201)
        self.assertEqual(self.point(201), (201, first[2], first[3]))
        self.assertEqual(self.point(55555), self.START)              # no route
        self.assertEqual(self.point(102, {'102': [201, 10, 20]}), (201, 10.0, 20.0))


@unittest.skipUnless(HAVE_HII, 'needs the EN client hs/windslayer.hii')
class DebuffModel(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual([D.kind_of(SK.skill_def(i)) for i in (POISON, TIME_BOMB, STUN, 1975)],
                         ['dot', 'detonate', 'control', 'mark'])

    def test_dot_ticks_every_990_ms_for_con_and_then_ends(self):
        mob = Mob(9, 0, 0)
        rec, replaced = D.apply(mob, SK.skill_def(POISON), 0.0, src=1, tick_damage=9)
        self.assertIsNone(replaced)
        ticks, t = 0, 0.0
        while mob.debuffs:
            t = round(t + 0.05, 2)
            ticks += len(D.due_ticks(mob, t))
            D.pop_expired(mob, t)
        self.assertEqual((ticks, t), (9, 9.0))                       # 0.99 .. 8.91, Con 9 s

    def test_a_recast_refreshes_the_family_in_place_and_control_is_a_window(self):
        mob = Mob(9, 0, 0)
        D.apply(mob, SK.skill_def(STUN), 0.0)
        self.assertTrue(D.controlled(mob, 1.0))
        _rec, replaced = D.apply(mob, SK.skill_def(STUN), 2.0)
        self.assertEqual(replaced['id'], STUN)
        self.assertEqual(len(mob.debuffs), 1)
        self.assertTrue(D.controlled(mob, 4.9))
        self.assertEqual([r['id'] for r in D.pop_expired(mob, 5.0)], [STUN])
        self.assertFalse(D.controlled(mob, 5.0))
        D.apply(mob, SK.skill_def(TIME_BOMB), 0.0, hit_damage=24)
        D.clear(mob)
        self.assertEqual(mob.debuffs, {})


# =========================================================================== server
class CombatServer(unittest.TestCase):
    """A fresh GameServer on a temp accounts.json; TestHero is a GM (dev commands)."""
    config = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_combat_')
        self.server = F.make_server(self.tmp, accounts=copy.deepcopy(ACCOUNTS), config=self.config)
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def char(self, name='TestHero', user='test'):
        return self.server.store.find_character(user, name)

    def hero(self, cls=1, level=40, weapon=WOODEN_BLADE, hp=None, mp=None, skills=(), exp=None, job2=0):
        with self.server.store.lock:
            ch = self.char()
            ch['class'] = cls
            ch['job2'] = job2
            ch['exp'] = progression.exp_for_level(level) if exp is None else exp
            ch['equipped'] = {5: {'id': weapon, 'w': [0] * 6}} if weapon else {}
            ch['skills'] = list(skills)
            d = hpmp.derive({}, ch)
            ch['hp'] = d.max_hp // 2 if hp is None else hp
            ch['mp'] = d.max_mp if mp is None else mp
        self.server.store.mark_dirty('test setup')
        return ch

    def enter(self, user='test', password='test', name='TestHero'):
        c = F.FakeClient(self.server)
        self.clients.append(c)
        c.send_c2s('0x44D8BF/0x01', {'account_id': user, 'password': password})
        self.assertEqual(dec(c.expect(0x02))['result'], 1)
        map_code = int(self.char(name, user).get('map') or 101)
        c.send_c2s('0x42F904/0x2B', {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907, 'char_name': name})
        # world-presence: + 0x04 / 0x05 when another client is on that map (F.expect_entry)
        pkts = F.expect_entry(c, (0x03, 0x07, 0x15, 0x28, 0x44), self.clients, map_code)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world')))
        return c, pkts

    def portal(self, c):
        c.send(0x7E, CAP_7E_101_TO_102)
        return c.expect(0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(8))

    def at(self, c, x, y, facing=C.FACING_RIGHT):
        with self.server._combat_lock(c.session):
            c.session['pos'] = (float(x), float(y))
            c.session['facing'] = facing

    def mob(self, c, i):
        return c.session['monsters'][PUPU[i]]

    def cast(self, c, skill, x=None, y=None):
        if x is None:
            c.send_c2s('0x44C239/0x15', {'skill_id': skill})
        else:
            c.send(0x15, struct.pack('<HHH', skill, x, y))

    def gm(self, c, text):
        c.send_c2s('0x445CA7/0x03', {'msg_len': len(text), 'message': text})

    def move(self, c, state_lo, map_code=102, **extra):
        c.send_c2s('0x42CE94/0x0D', {'realtime_delta_ms': 0, 'map_code': map_code,
                                     'logic_elapsed_ms': 30, 'state_lo': state_lo, 'state_hi': 0,
                                     **extra})

    def interact(self, c, event, target, pos, delta, item_id=0, map_code=102):
        """A C2S 0x0D with the interact tail (state_lo bits 16-19 = event)."""
        fields = {'target_uid': target, 'pos_x': float(pos[0]), 'pos_y': float(pos[1]),
                  'target_dx': float(delta[0]), 'target_dy': float(delta[1]), 'flag_8db': 0,
                  'flag_8e7': 0, 'timer_dac': 0, 'target_action_event': 9}
        if event == 0xC:
            fields['item_id'] = item_id
        self.move(c, event << 16, map_code=map_code, **fields)

    @staticmethod
    def text(pkt):
        return dec(pkt)['text']

    def kill_packets(self, c):
        """0x29 (the hold tick at the spawn point), 0x21 exp, 0x18 gold/drop."""
        die, exp, drop = c.expect(0x29, 0x21, 0x18)
        return dec(die), dec(exp), dec(drop)


@unittest.skipUnless(HAVE_HII, 'needs the EN client hs/windslayer.hii')
class SkillDamage(CombatServer):
    """cs-skill-damage: P3 exit criterion 3 "an attack skill damages a Pupu in front and kills
    it with exp", offline."""

    def test_an_attack_skill_kills_the_pupu_in_front_with_exp(self):
        self.hero(skills=[ICE_SPEAR])
        c, _ = self.enter()
        self.portal(c)
        self.at(c, 1200, 714)                                       # Pupu #1 44 px ahead
        exp0 = self.char()['exp']
        self.cast(c, ICE_SPEAR)
        use, mp, die, exp, drop = c.expect(0x25, 0x44, 0x29, 0x21, 0x18)
        self.assertEqual(dec(use), {'item_or_skill_id': ICE_SPEAR})
        die, exp, drop = dec(die), dec(exp), dec(drop)
        self.assertEqual(die, {'uid': PUPU[1], 'respawn_tick': 0x7FFFFFFF,
                               'respawn_x': 1244, 'respawn_y': 714})
        self.assertEqual(exp['exp_delta'], 5)                       # EN Pupu exp
        self.assertEqual(drop['gold'], W.storemod.DEFAULT_GOLD + 7)
        self.assertFalse(self.mob(c, 1).alive)
        self.assertEqual(self.char()['exp'], exp0 + 5)
        # only the one in the box: #2 is 350 px ahead, #7 is 172 px higher
        self.assertEqual([m.uid for m in c.session['monsters'].values() if not m.alive], [PUPU[1]])

    def test_facing_left_hits_behind_the_right_side(self):
        self.hero(skills=[ICE_SPEAR])
        c, _ = self.enter()
        self.portal(c)
        self.at(c, 1300, 714, C.FACING_LEFT)                        # #1 at 1244: 56 px ahead-left
        self.cast(c, ICE_SPEAR)
        die = c.expect(0x25, 0x44, 0x29, 0x21, 0x18)[2]
        self.assertEqual(dec(die)['uid'], PUPU[1])
        self.assertTrue(self.mob(c, 2).alive)

    def test_a_survivor_takes_the_formula_damage_and_gets_no_packet(self):
        self.hero(skills=[ICE_SPEAR])
        c, _ = self.enter()
        self.portal(c)
        self.at(c, 1200, 714)
        self.mob(c, 1).hp = 200
        self.cast(c, ICE_SPEAR)
        c.expect(0x25, 0x44)
        # The client's formula (damage.py). Lv40 Warrior STR 3 with the Wooden Blade (sword:
        # M = (3 + 20) / 7 + 40/10 + 1 = 8.29; S_Att 23 -> Strong = trunc(123 x 23 / 100 + 8.29)
        # = 36) on the Lv1 Pupu (Def 2): B = trunc(36 x 36/38 x 2 x 40/41) = 66; Ice Spear Lv1
        # is Ice (2) with Attri_Atk 9, scaled by INT 1 (1.3 x 1 + 15 + 100 = 116.3 %) = 10,
        # E = trunc(10 x 10/10 x 2 x 40/41) = 19 -> 85
        self.assertEqual(self.mob(c, 1).hp, 200 - 85)
        # a running Increase Attack Power adds its Skill_P_A 5 % to Strong_Atk: 36 + trunc(36 x
        # 5 / 100) = 37 (a zero percentage would still add 1) -> B = trunc(37 x 37/39 x 2 x 40/41)
        # = 68, E 19 -> 87
        with self.server._combat_lock(c.session):
            B.apply(c.session, IAP1, time.monotonic())
            c.session[W.GameServer.SKILL_CD_KEY].clear()
        self.cast(c, ICE_SPEAR)
        c.expect(0x25, 0x44)
        self.assertEqual(self.mob(c, 1).hp, 115 - 87)

    def test_nothing_in_front_is_still_a_cast(self):
        self.hero(skills=[ICE_SPEAR])
        c, _ = self.enter()
        self.portal(c)
        self.at(c, 1200, 714, C.FACING_LEFT)                        # nothing within 150 px left
        with self.assertLogs('WS', logging.INFO) as cm:
            self.cast(c, ICE_SPEAR)
            c.expect(0x25, 0x44)
        self.assertTrue(any('no monster in the 150/0/60 px box facing left' in l for l in cm.output))
        self.assertTrue(all(m.alive for m in c.session['monsters'].values()))

    def release(self, uid, receiver=1):
        return struct.pack('<I', uid) + bytes(8) + struct.pack('<I', receiver)

    def test_a_client_caught_skill_victim_the_box_missed_is_damaged_and_released(self):
        # live 2026-09-24: an Ice Spear drew its hit on a Ssiyo the server's box missed (its
        # estimate of the mob was elsewhere); the ignored event-9 report left the Ssiyo
        # hit-locked, flashing in its hurt pose for good
        self.hero(skills=[ICE_SPEAR])
        c, _ = self.enter()
        self.portal(c)
        self.at(c, 1200, 714, C.FACING_LEFT)                        # the server's box: nothing
        self.mob(c, 1).hp = 300
        self.cast(c, ICE_SPEAR)
        c.expect(0x25, 0x44)
        self.interact(c, 9, PUPU[1], (1200.0, 714.0), (44.0, 0.0))   # the client caught #1
        self.assertEqual(c.expect(0x2A).payload, self.release(PUPU[1]))
        # the cast's skill damage: B 66 + E 19 (test_a_survivor_takes_the_formula_damage...)
        self.assertEqual(self.mob(c, 1).hp, 300 - 85)
        # the same victim again in this cast (a multi-hit frame): the release only
        self.interact(c, 9, PUPU[1], (1200.0, 714.0), (44.0, 0.0))
        self.assertEqual(c.expect(0x2A).payload, self.release(PUPU[1]))
        self.assertEqual(self.mob(c, 1).hp, 215)
        # a victim the box did hit: its report only releases the client's hit-lock
        with self.server._combat_lock(c.session):
            c.session[W.GameServer.SKILL_CD_KEY].clear()
        self.at(c, 1200, 714)                                       # facing right: #1 in the box
        self.cast(c, ICE_SPEAR)
        c.expect(0x25, 0x44)
        self.assertEqual(self.mob(c, 1).hp, 130)
        self.interact(c, 9, PUPU[1], (1200.0, 714.0), (44.0, 0.0))
        self.assertEqual(c.expect(0x2A).payload, self.release(PUPU[1]))
        self.assertEqual(self.mob(c, 1).hp, 130)
        # a caught victim that dies gets its 0x29 (which clears the hit-lock), no release
        self.mob(c, 2).hp = 1
        self.interact(c, 9, PUPU[2], (1550.0, 714.0), (44.0, 0.0))
        self.assertEqual(dec(c.expect(0x29, 0x21, 0x18)[0])['uid'], PUPU[2])

    def test_a_strong_attack_report_without_a_cast_hits_and_releases(self):
        self.hero()
        c, _ = self.enter()
        self.portal(c)
        mob = self.mob(c, 1)
        mob.hp = 100
        dmg = self.server._compute_damage(c.session, 1, mob)
        self.interact(c, 9, PUPU[1], (1200.0, 714.0), (44.0, 0.0))
        self.assertEqual(c.expect(0x2A).payload, self.release(PUPU[1]))
        self.assertEqual(mob.hp, 100 - dmg)

    def test_a_wide_sweep_hits_both_sides(self):
        self.hero(level=60, hp=300, skills=[HEAVEN_STRIKE])
        c, _ = self.enter()
        self.portal(c)
        self.at(c, 1400, 714)                                       # #1 156 px behind, #2 194 ahead
        self.cast(c, HEAVEN_STRIKE)
        c.expect(0x25, 0x28, 0x44, 0x29, 0x21, 0x18, 0x29, 0x21, 0x18)
        dead = sorted(m.uid for m in c.session['monsters'].values() if not m.alive)
        self.assertEqual(dead, [PUPU[1], PUPU[2]])

    def test_facing_and_position_come_from_the_driver_and_0x0D(self):
        self.hero(skills=[ICE_SPEAR])
        c, _ = self.enter()
        self.portal(c)
        self.move(c, 0x0)                                           # the client is on the new map
        self.assertTrue(c.wait_session(lambda s: s.get('map_confirmed') == s.get('current_map')))
        self.assertTrue(self.server._track_driver_position(1, 1300.0, 714.0, 2))
        self.assertEqual((c.session['pos'], c.session['facing']), ((1300.0, 714.0), C.FACING_LEFT))
        self.move(c, 0x2)                                           # right held
        self.assertTrue(c.wait_session(lambda s: s['facing'] == C.FACING_RIGHT))
        self.move(c, 0x0)                                           # released: kept
        self.move(c, 0x1, map_code=101)                             # an old map's packet: ignored
        time.sleep(0.2)
        self.assertEqual(c.session['facing'], C.FACING_RIGHT)
        # an interact tail moves the server's estimate of the named monster
        self.interact(c, 7, PUPU[3], (700.0, 912.0), (140.0, 0.0))
        self.assertTrue(c.wait_session(lambda s: s['monsters'][PUPU[3]].x == 840.0))

    def test_no_driver_sample_before_the_client_reports_from_the_new_map(self):
        # live P5 2026-09-23: 0.1 s after a warp the client still held the old map's entity;
        # the driver wrote its x over the arrival point and the next arrival's 0x04 showed
        # the player 600 px away. Samples wait for the first 0x0D naming the new map.
        c, _ = self.enter()
        self.portal(c)
        arrival = c.session['pos']
        self.assertFalse(self.server._track_driver_position(1, 1600.0, 814.0, 2))
        self.assertEqual(c.session['pos'], arrival)
        self.move(c, 0x0, map_code=101)                             # still the old map: no
        time.sleep(0.2)
        self.assertFalse(self.server._track_driver_position(1, 1600.0, 814.0, 2))
        self.move(c, 0x0)                                           # the new map: from now on
        self.assertTrue(c.wait_session(lambda s: s.get('map_confirmed') == s.get('current_map')))
        self.assertTrue(self.server._track_driver_position(1, 1300.0, 714.0, 2))
        self.assertEqual(c.session['pos'], (1300.0, 714.0))

    def test_a_turn_in_0x0D_outranks_an_older_driver_sample(self):
        # live 2026-09-23: turn left + cast 270 ms later laid the box facing right, because
        # the driver's memory sample in between still read the old +0x8BD (6 = right)
        self.hero(skills=[ICE_SPEAR])
        c, _ = self.enter()
        self.portal(c)
        self.move(c, 0x1)                                           # left pressed
        self.assertTrue(c.wait_session(lambda s: s.get('facing') == C.FACING_LEFT))
        self.assertTrue(self.server._track_driver_position(1, 1300.0, 714.0, 6))
        self.assertEqual((c.session['pos'], c.session['facing']), ((1300.0, 714.0), C.FACING_LEFT))
        c.session['facing_t'] -= W.FACING_PACKET_HOLD_SECS          # once the packet is old
        self.assertTrue(self.server._track_driver_position(1, 1300.0, 714.0, 6))
        self.assertEqual(c.session['facing'], C.FACING_RIGHT)       # the driver fills in again

    def test_driver_sampled_positions_move_roaming_monsters(self):
        # the client wander AI moves the mobs; the driver's scene read is what puts a roaming
        # Pupu into a skill box (live: Ice Spear "KRAKK" on a Pupu the server did not see)
        self.hero(skills=[ICE_SPEAR])
        c, _ = self.enter()
        self.portal(c)
        mobs = c.session['monsters']
        dead = mobs[PUPU[2]]
        dead.alive = False
        before = (dead.x, dead.y)
        moved = self.server._track_driver_monsters(1, {
            PUPU[1]: (1500.0, 714.0), PUPU[2]: (10.0, 10.0),        # dead: not moved
            PUPU[3]: (float('nan'), 900.0), 0x12345: (1.0, 1.0)})   # junk read / unknown uid
        self.assertEqual(moved, 1)
        self.assertEqual((mobs[PUPU[1]].x, mobs[PUPU[1]].y), (1500.0, 714.0))
        self.assertEqual((dead.x, dead.y), before)
        self.assertEqual(self.server._track_driver_monsters(999, {PUPU[1]: (1.0, 1.0)}), 0)


@unittest.skipUnless(HAVE_HII, 'needs the EN client hs/windslayer.hii')
class HealVisuals(CombatServer):
    """cs-heal-visuals: the healed player gets 0x25 / 0x3D, observers S2C 0x40 once."""

    def two(self, **hero):
        self.hero(**hero)
        c, _ = self.enter()
        other, _ = self.enter('admin', 'admin', 'Watcher')
        return c, other

    def test_self_heal_reaches_observers_as_0x40(self):
        c, other = self.two(cls=6, weapon=CLERIC_WAND, hp=100, skills=[SELF_HEAL])
        self.cast(c, SELF_HEAL)
        c.expect(0x25, 0x28, 0x44)                                  # never a 0x40 to the healer
        notice = other.expect(0x40)
        self.assertEqual(dec(notice), {'uid': 1, 'has_hp': 1, 'has_mp': 0, 'hp': 120})
        self.assertEqual(len(notice.payload), 8)

    def test_a_target_heal_lands_on_the_caster_with_0x3D(self):
        c, other = self.two(cls=6, weapon=CLERIC_WAND, hp=100, skills=[HEAL])
        self.cast(c, HEAL)
        use, mp, heal = c.expect(0x25, 0x44, 0x3D)                  # the 0x25 adds no HP
        self.assertEqual(dec(heal), {'has_hp': 1, 'has_mp': 0, 'hp': 140})
        self.assertEqual(c.session['hp'], 140)
        self.assertEqual(dec(other.expect(0x40))['hp'], 140)

    def test_a_potion_is_seen_by_observers(self):
        c, other = self.two(cls=0, weapon=0, hp=50)
        self.assertIsNotNone(self.server._inv_add(c.session, MINOR_POTION, 1, 'test'))
        c.send_c2s('0x44C2B3/0x15', {'item_id': MINOR_POTION})
        c.expect(0x25)
        self.assertEqual(dec(other.expect(0x40)), {'uid': 1, 'has_hp': 1, 'has_mp': 0, 'hp': 110})


@unittest.skipUnless(HAVE_HII, 'needs the EN client hs/windslayer.hii')
class Death(CombatServer):
    """cs-player-death: P3 exit criterion 5 offline."""

    def test_damage_death_dialog_and_revive(self):
        self.hero(level=12, exp=progression.exp_for_level(12) + 500, hp=100, mp=40,
                  skills=[ICE_SPEAR])
        c, _ = self.enter()
        self.portal(c)
        max_hp = c.session['max_hp']
        self.gm(c, '!damage 30')
        hp, line = c.expect(0x28, 0x15)
        self.assertEqual(dec(hp)['hp'], 70)
        self.gm(c, '!die')
        death, exp, line = c.expect(0x3E, 0x21, 0x15)               # never a 0x28 hp=0
        self.assertEqual(death.payload, b'')
        self.assertEqual(dec(exp)['exp_delta'], -443)               # FUN_0041a230's penalty
        self.assertEqual(self.text(line), 'You died.')
        self.assertTrue(c.session['dead'])
        self.assertEqual(self.char()['hp'], 0)                       # saved dead at once
        self.assertEqual(progression.level_for_exp(self.char()['exp']), 12)

        # a corpse: no regen, no cast, no portal, no potion
        self.assertEqual(self.server._tick_regen(c.session['regen']['mp_due'] + 60), 0)
        self.cast(c, ICE_SPEAR)
        c.expect(0x5F)
        c.send(0x7E, hexbytes('1F 00 00 00'))
        c.expect_silence(0.2)

        # "Revived" -> C2S 0x2E -> MapTransfer to 102's revive point (101 via 102_31)
        c.send(0x2E)
        lead, state, me, hp, mp = c.expect(0x08, 0x03, 0x07, 0x28, 0x44)
        self.assertEqual(dec(lead)['map_code'], 101)
        self.assertEqual(dec(hp)['hp'], max_hp // 2)
        self.assertEqual(dec(mp)['mp'], 40)                          # MP as it was
        row = dec(me)['repeat[player_count]'][0]
        self.assertEqual(row['cur_hp'], max_hp // 2)
        self.assertFalse(c.session['dead'])
        self.assertEqual((self.char()['map'], self.char()['hp']), (101, max_hp // 2))
        c.send(0x2E)                                                 # a second click: nothing
        c.expect_silence(0.2)
        self.portal(c)                                               # control restored

    def test_dying_on_the_start_map_reloads_it(self):
        self.hero(level=5, hp=100)
        c, _ = self.enter()
        self.gm(c, '!die')
        c.expect(0x3E, 0x15)                                         # Lv5: no penalty
        c.send(0x2E)
        lead = c.expect(0x08, 0x03, 0x07, 0x28, 0x44)[0]
        self.assertEqual(dec(lead)['map_code'], 101)
        # the start point (700, 812) settled onto 101's floor (livetest bug 5)
        self.assertEqual(c.session['pos'], (700.0, 912.0))

    def test_logging_out_dead_comes_back_revived(self):
        self.hero(level=5, hp=100)
        c, _ = self.enter()
        self.portal(c)
        self.gm(c, '!die')
        c.expect(0x3E, 0x15)
        c.close()
        c.thread.join(timeout=5.0)
        self.assertEqual((self.char()['hp'], self.char()['map']), (0, 102))
        c2, pkts = self.enter()
        # the revive point (1405, 714) is 100 px above 101's floor: he lands on it (livetest bug 5)
        self.assertEqual((c2.session['current_map'], c2.session['pos']), (101, (1405.0, 814.0)))
        self.assertEqual(dec(pkts[3])['hp'], c2.session['max_hp'] // 2)
        self.assertFalse(c2.session['dead'])

    def test_a_client_side_death_opens_the_dialog_without_a_penalty(self):
        self.hero(level=12, exp=progression.exp_for_level(12) + 500, hp=100)
        c, _ = self.enter()
        self.move(c, 0xD << 12, map_code=101, event_source_uid=0)
        self.assertEqual(c.expect(0x3E).payload, b'')                # no 0x21
        self.assertTrue(c.session['dead'])
        self.move(c, 0xD << 12, map_code=101, event_source_uid=0)    # the confirmation: nothing
        c.expect_silence(0.2)
        c.send(0x2E)
        c.expect(0x08, 0x03, 0x07, 0x28, 0x44)
        self.move(c, 0xD << 12, map_code=101, event_source_uid=0)    # the old corpse's packet
        c.expect_silence(0.2)
        self.assertFalse(c.session['dead'])

    def test_a_failed_revive_gives_the_dialog_back(self):
        """The client closes window 0x79 itself (live #21): a revive that cannot be made must
        not leave a dead player with no UI - the MUST_REPLY refusal sends 0x3E again."""
        self.hero(level=5, hp=100)
        c, _ = self.enter()
        self.gm(c, '!die')
        c.expect(0x3E, 0x15)
        self.server._map_transfer = lambda *a, **k: False
        c.send(0x2E)
        c.expect(0x3E)
        self.assertTrue(c.session['dead'])
        self.assertEqual(c.session['hp'], 0)

    def test_death_forgets_buffs_and_observers_see_0x29(self):
        self.hero(level=40, hp=300, skills=[IAP1])
        c, _ = self.enter()
        other, _ = self.enter('admin', 'admin', 'Watcher')
        self.cast(c, IAP1)
        c.expect(0x3B, 0x28, 0x44)
        other.expect(0x3B)
        deadline = B.find(c.session, IAP1)['expires']
        self.gm(c, '!die')
        c.expect(0x3E, 0x15)                          # 0 exp into Lv40: nothing to lose, no 0x21
        corpse = dec(other.expect(0x29))
        self.assertEqual((corpse['uid'], corpse['respawn_tick']), (1, 0x7FFFFFFF))
        self.assertEqual(c.session['buffs'], [])
        self.assertEqual(self.server._tick_buffs(deadline + 1), 0)   # the client memset them

    def test_dev_revive(self):
        self.hero(level=5, hp=100)
        c, _ = self.enter()
        self.gm(c, '!revive')
        self.assertIn('not dead', self.text(c.expect(0x15)))
        self.gm(c, '!die')
        c.expect(0x3E, 0x15)
        # livetest bug 11: !hp / !mp refuse on a corpse, as !damage does (no 0x28 / 0x44), and
        # so do !level / !exp, whose level-up healed the corpse (review of bug 11; no 0x21)
        char = self.server._session_char(c.session)
        exp = char['exp']
        for line in ('!hp 50', '!mp 10', '!hp +5', '!level 6', '!exp +500', '!exp 99999'):
            with self.subTest(line):
                self.gm(c, line)
                self.assertIn('dead (click Revived, or !revive)', self.text(c.expect(0x15)))
                c.expect_silence(0.1)
        self.assertEqual((c.session['hp'], char['exp']), (0, exp))
        # exp credited to a corpse (a DoT kill whose caster died since) that crosses a level:
        # the corpse is only re-clamped, never healed to the new maxima
        mp, lv = c.session['mp'], progression.level_for_exp(exp)
        self.assertGreater(self.server.grant_exp(c.session, progression.exp_for_level(lv + 1) - exp), 0)
        c.expect(0x21)
        self.assertGreater(c.session['max_mp'], mp)                  # the new maximum is higher...
        self.assertEqual((c.session['hp'], c.session['mp'], c.session['dead']), (0, mp, True))
        self.assertEqual(char['hp'], 0)                              # ...and the record stays dead
        self.gm(c, '!revive')
        line = c.expect(0x08, 0x03, 0x07, 0x28, 0x44, 0x15)[-1]
        self.assertFalse(c.session['dead'])
        # the start point (700, 812) on 101's floor, and the reply says so (livetest bug 5)
        self.assertEqual(c.session['pos'], (700.0, 912.0))
        self.assertIn('Revived on map 101 at (700, 912)', self.text(line))
        self.gm(c, '!hp 7')                                          # alive again: allowed
        self.assertEqual(dec(c.expect(0x28, 0x15)[0])['hp'], 7)


@unittest.skipUnless(HAVE_HII, 'needs the EN client hs/windslayer.hii')
class Traps(CombatServer):
    """cs-traps: P3 exit criterion 8 offline - the trap fires, damage applies, 0x3C clears it."""

    def place(self, x=1244, y=714, pupu_hp=None):
        self.hero(cls=4, level=35, weapon=DAGGER, skills=[BOOBY_TRAP])
        c, _ = self.enter()
        self.portal(c)
        if pupu_hp is not None:
            self.mob(c, 1).hp = pupu_hp
        self.cast(c, BOOBY_TRAP, x, y)
        c.expect(0x3B, 0x44)
        return c

    def test_0x6C_then_the_0x0D_victim(self):
        c = self.place()
        with self.assertNoLogs('WS', logging.ERROR):                # no NEVER_REPLY violation
            c.send_c2s('0x417DB0/0x6C', {'trap_skill_id': BOOBY_TRAP})
            c.expect_silence(0.15)                                   # the client waits for nothing
            self.interact(c, 0xC, PUPU[1], (1100.0, 714.0), (144.0, 0.0), BOOBY_TRAP)
            removal = c.expect(0x3C, 0x29, 0x21, 0x18)[0]
        self.assertEqual(dec(removal), {'buff_item_id': BOOBY_TRAP, 'target_uid': 1})
        self.assertFalse(self.mob(c, 1).alive)
        self.assertIsNone(B.find(c.session, BOOBY_TRAP))
        self.assertNotIn('pending_trap', c.session)

    def test_a_survivor_gets_the_hit_lock_release(self):
        c = self.place(pupu_hp=300)
        c.send_c2s('0x417DB0/0x6C', {'trap_skill_id': BOOBY_TRAP})
        self.interact(c, 0xC, PUPU[1], (1100.0, 714.0), (144.0, 0.0), BOOBY_TRAP)
        removal, release = c.expect(0x3C, 0x2A)
        self.assertEqual(release.payload, struct.pack('<I', PUPU[1]) + bytes(8) + struct.pack('<I', 1))
        # a trap catch is an event-9 hit whose skill term is the Booby Trap family 0xA31. Lv35
        # thief, DEX 2 with the Dagger (dagger: M = (2 + 20) / 8 + 3.5 + 1 = 7.25; S_Att 18 ->
        # Strong = trunc(122 x 18 / 100 + 7.25) = 29): B = trunc(29 x 29/31 x 2 x 35/36) = 52;
        # Booby Trap Lv1 is Fire with Attri_Atk 32 x (1.2 x 1 + 15 + 100 = 116.2 %) = 37,
        # E = trunc(37 x 2 x 35/36) = 71 -> 123
        self.assertEqual(self.mob(c, 1).hp, 300 - 123)

    def test_no_0x0D_falls_back_to_the_monster_on_the_trap(self):
        c = self.place()
        c.send_c2s('0x417DB0/0x6C', {'trap_skill_id': BOOBY_TRAP})
        pending = c.wait_session(lambda s: s.get('pending_trap'))['pending_trap']
        self.server._trap_pair_timeout(c.session, pending)          # the 0.5 s tick
        c.expect(0x3C, 0x29, 0x21, 0x18)
        self.assertFalse(self.mob(c, 1).alive)

    def test_a_victim_far_from_the_trap_is_not_believed(self):
        c = self.place(x=600, y=300)                                 # nothing near the trap
        c.send_c2s('0x417DB0/0x6C', {'trap_skill_id': BOOBY_TRAP})
        self.interact(c, 0xC, PUPU[1], (1100.0, 714.0), (144.0, 0.0), BOOBY_TRAP)
        c.expect(0x3C)                                               # consumed, no damage
        self.assertTrue(self.mob(c, 1).alive)

    def test_a_stale_trigger_is_ignored(self):
        self.hero(cls=4, level=35, weapon=DAGGER, skills=[BOOBY_TRAP])
        c, _ = self.enter()
        self.portal(c)
        with self.assertLogs('WS', logging.INFO) as cm:
            c.send_c2s('0x417DB0/0x6C', {'trap_skill_id': BOOBY_TRAP})
            c.expect_silence(0.2)
        self.assertTrue(any('[0x6C] trap triggered - consumed: no live trap' in l for l in cm.output))

    def test_the_dev_trigger(self):
        c = self.place(x=1300, y=714)
        self.gm(c, '!trap')
        removal, _die, _exp, _drop, line = c.expect(0x3C, 0x29, 0x21, 0x18, 0x15)
        self.assertEqual(dec(removal)['buff_item_id'], BOOBY_TRAP)
        self.assertIn(f'fired on Pupu uid {PUPU[1]:#x} (killed)', self.text(line))
        self.gm(c, '!trap')
        self.assertIn('no Booby Trap placed', self.text(c.expect(0x15)))


@unittest.skipUnless(HAVE_HII, 'needs the EN client hs/windslayer.hii')
class Debuffs(CombatServer):
    """cs-debuffs: 0x41 on the monster, DoT / detonation / control on the server clock."""

    def thief(self, skill, pupu_hp=None, job2=0):
        self.hero(cls=4, level=40, weapon=DAGGER, skills=[skill], job2=job2)
        c, _ = self.enter()
        self.portal(c)
        self.at(c, 1200, 714)
        if pupu_hp is not None:
            self.mob(c, 1).hp = pupu_hp
        self.cast(c, skill)
        use, mp, show = c.expect(0x25, 0x44, 0x41)
        self.assertEqual(dec(show), {'target_uid': PUPU[1], 'source_uid': 1, 'item_id': skill})
        return c, self.mob(c, 1)

    def test_poison_ticks_every_990_ms_and_ends_with_0x3C(self):
        c, pupu = self.thief(POISON, pupu_hp=200)
        rec = pupu.debuffs[SK.skill_def(POISON).family]
        self.assertEqual(rec['tick_damage'], 9)
        self.assertEqual(self.server._tick_debuffs(rec['next_tick'] - 0.01), 0)
        self.assertEqual(self.server._tick_debuffs(rec['next_tick']), 1)
        c.expect_silence(0.1)                                        # no HP bar: no packet
        self.assertEqual(pupu.hp, 191)
        t = rec['next_tick']
        while pupu.debuffs:
            t += 0.05
            self.server._tick_debuffs(t)
        self.assertEqual(pupu.hp, 200 - 9 * 9)
        self.assertEqual(dec(c.expect(0x3C)), {'buff_item_id': POISON, 'target_uid': PUPU[1]})

    def test_a_dot_kill_goes_through_the_lifecycle(self):
        c, pupu = self.thief(POISON)
        rec = pupu.debuffs[SK.skill_def(POISON).family]
        self.server._tick_debuffs(rec['next_tick'])
        die, _exp, _drop = self.kill_packets(c)
        self.assertEqual(die['uid'], PUPU[1])
        self.assertEqual(pupu.debuffs, {})
        self.server._tick_debuffs(rec['expires'] + 1)
        c.expect_silence(0.1)

    def test_time_bomb_detonates_at_con(self):
        c, pupu = self.thief(TIME_BOMB, job2=2)                     # Time Bomb is a job-2 skill
        rec = pupu.debuffs[SK.skill_def(TIME_BOMB).family]
        # the (p5 6, class 4, job 2) cell's event-9 hit, stored at cast time and released at
        # Con: Lv40 thief Strong 29 (Dagger, DEX 2) on the Lv1 Pupu (Def 2): B = trunc(29 x
        # 29/31 x 2 x 40/41) = 52; Time Bomb Lv1 is Fire with Attri_Atk 45 x 116.2 % = 52,
        # E = trunc(52 x 2 x 40/41) = 101 -> 153; the release adds the weapon element again
        # (the Dagger has none)
        self.assertEqual(rec['hit_damage'], 153)
        self.server._tick_debuffs(rec['expires'] - 0.01)
        c.expect_silence(0.1)
        self.server._tick_debuffs(rec['expires'])
        removal, die, _exp, _drop = c.expect(0x3C, 0x29, 0x21, 0x18)  # explosion, then death
        self.assertEqual(dec(removal)['buff_item_id'], TIME_BOMB)

    def test_stun_is_a_control_window(self):
        c, pupu = self.thief(STUN)
        rec = pupu.debuffs[SK.skill_def(STUN).family]
        self.assertTrue(D.controlled(pupu, rec['expires'] - 0.1))
        self.server._tick_debuffs(rec['expires'])
        self.assertEqual(dec(c.expect(0x3C))['buff_item_id'], STUN)
        self.assertFalse(D.controlled(pupu, rec['expires']))

    def test_nothing_in_front_lands_nothing(self):
        self.hero(cls=4, level=40, weapon=DAGGER, skills=[POISON])
        c, _ = self.enter()
        self.portal(c)
        self.at(c, 1200, 714, C.FACING_LEFT)
        self.cast(c, POISON)
        c.expect(0x25, 0x44)

    def test_a_respawn_forgets_the_debuffs(self):
        c, pupu = self.thief(STUN)
        with self.server._combat_lock(c.session):
            self.server._damage_monster(c.session['sock'], c.session, pupu, 99, 'test')
        self.kill_packets(c)
        self.assertEqual(pupu.debuffs, {})


@unittest.skipUnless(HAVE_HII, 'needs the EN client hs/windslayer.hii')
class ContactDamage(CombatServer):
    """config MOB_CONTACT_DAMAGE: the victim's own 0x0D names the monster that hit him. Any
    touch counts here (MOB_CONTACT_AGGRO_ONLY off; test_mobai covers the aggro-only rule)."""
    config = cfgmod.from_dict({'MOB_CONTACT_DAMAGE': True, 'MOB_CONTACT_AGGRO_ONLY': False})

    def test_a_reported_touch_hurts_and_a_stunned_monster_does_not(self):
        self.hero(cls=4, level=40, weapon=DAGGER, hp=10, skills=[STUN])
        c, _ = self.enter()
        self.portal(c)
        self.move(c, 6 << 12, event_source_uid=PUPU[1])
        # the Lv1 Pupu's Body_Atk 3 against a Lv40 thief (Def 0): trunc(3 x 3/3 x 2 x 1/41) = 0,
        # clamped to the client's minimum 1
        self.assertEqual(dec(c.expect(0x28))['hp'], 9)
        self.move(c, 6 << 12, event_source_uid=PUPU[1])              # < 0.5 s later: no stack
        c.expect_silence(0.15)
        self.move(c, 6 << 12, event_source_uid=0x12345)              # not one of ours
        c.expect_silence(0.15)
        self.at(c, 1200, 714)
        self.cast(c, STUN)
        c.expect(0x25, 0x44, 0x41)
        c.session['contact_t'] = {}                                # its contact slot is free again
        self.move(c, 6 << 12, event_source_uid=PUPU[1])
        c.expect_silence(0.15)
        self.assertEqual(c.session['hp'], 9)

    def test_contact_can_kill(self):
        # a Lv1 Pupu's guarded contact on a Lv5 thief: trunc(3 x 3/3 x 2 x 1/6) = 1
        self.hero(cls=4, level=5, weapon=DAGGER, hp=1)
        c, _ = self.enter()
        self.portal(c)
        self.move(c, 1 << 12, event_source_uid=PUPU[0])
        self.assertEqual(c.expect(0x3E).payload, b'')
        self.assertTrue(c.session['dead'])


@unittest.skipUnless(HAVE_HII, 'needs the EN client hs/windslayer.hii')
class ContactOff(CombatServer):
    # MOB_CONTACT_DAMAGE is on by default since monster aggro (test_mobai.py covers the event
    # types); switched off, a reported touch still does nothing.
    config = cfgmod.from_dict({'MOB_CONTACT_DAMAGE': False})

    def test_no_damage_without_the_flag(self):
        self.hero(level=5, hp=50)
        c, _ = self.enter()
        self.portal(c)
        self.move(c, 6 << 12, event_source_uid=PUPU[1])
        c.expect_silence(0.2)
        self.assertEqual(c.session['hp'], 50)


class Config(unittest.TestCase):
    def test_new_keys_are_validated(self):
        d = cfgmod.defaults()
        self.assertEqual((d.REVIVE_HP_PCT, d.REVIVE_POINTS, d.DEATH_EXP_PENALTY, d.MOB_CONTACT_DAMAGE),
                         (50, {}, True, True))
        for bad in ({'REVIVE_HP_PCT': 0}, {'REVIVE_HP_PCT': 101}, {'REVIVE_POINTS': {'x': [1, 2, 3]}},
                    {'REVIVE_POINTS': {'102': [101, 5]}}, {'REVIVE_POINTS': {'102': [0, 1, 2]}}):
            with self.subTest(bad):
                with self.assertRaises(cfgmod.ConfigError):
                    cfgmod.from_dict(bad)
        self.assertEqual(cfgmod.from_dict({'REVIVE_POINTS': {'102': [201, 1.5, 2]}}).REVIVE_POINTS,
                         {'102': [201, 1.5, 2]})

    def test_the_0x2E_refusal_is_the_dialog_only_while_dead(self):
        self.assertEqual(registry.MUST_REPLY[0x2E].refusal(None, {'dead': True, 'in_world': True}, None),
                         [('0x3E', {}, None)])
        self.assertEqual(registry.MUST_REPLY[0x2E].refusal(None, {'dead': True}, None), [])
        self.assertEqual(registry.MUST_REPLY[0x2E].refusal(None, {'in_world': True}, None), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
