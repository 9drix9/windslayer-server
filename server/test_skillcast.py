#!/usr/bin/env python3
"""
test_skillcast.py - learning, casting, utility replies and buffs (P3 stage 3: cs-skill-learn,
cs-skill-cast, cs-utility-skills, cs-buffs; combat_skill.md F1/F3/F4/F5, spec correction C4)

Three layers:
- the learned-list model (skills.learn = the client's FUN_00426b80 family rule) and the buff
  model (buffs.py = FUN_00424e20's replace/stack rules, deadlines, persistence), pure;
- the server through fakeclient (no port, no game client, temp accounts.json): !learn, the
  skill-master buy, quest-reward and !give mirrors surviving portal and relog; C2S 0x15
  answered with 0x3B / 0x25 / 0x5F by kind with the cost, cooldown and trailing 0x28/0x44;
  server-driven expiry 0x43 / 0x3C (the client never expires a buff); buffs kept with their
  remaining time across portal and relog; item buffs refreshed instead of stacked;
- the P3 store migration (.bak-pre-p3, idempotent).

The live bugs this pins: quest 26's Double Jump vanished on relog (never persisted), a skill
cast was only ever refused (S2C 0x5F), and a buff the client got never ended.
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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import buffs as B  # noqa: E402
import config as cfgmod  # noqa: E402
import en_content as EC  # noqa: E402
import fakeclient as F  # noqa: E402
import hpmp  # noqa: E402
import packets as P  # noqa: E402
import progression  # noqa: E402
import quests as Q  # noqa: E402
import skills as SK  # noqa: E402
import store as S  # noqa: E402
from wsproto import hexbytes  # noqa: E402

W = F.import_server()
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
HAVE_HII = os.path.exists(EC.hs_path('windslayer.hii'))
CAP_7E_101_TO_102 = hexbytes('17 00 00 00')

# Skills used below (EN hii; classes: 1 warrior, 3 archer, 4 thief, 6 priest).
IAP1, IAP2 = 292, 293            # Increase Attack Power Lv1/Lv2: warrior, Lv12, MP 5 HP 5, Con 30 s
ICE_SPEAR = 260                  # warrior attack, MP 6, CT 5000
OPEN_STALL = 194                 # traveler utility, Lv6, CT 3000, no cost
SELF_HEAL = 501                  # priest, HP +20, MP 10
BOOBY_TRAP = 2609                # thief trap, MP 16, Con 7020
ROARING_DOG = 2499               # archer summon, MP 19 (its HP -10 is the target's), slot 60 s
BERSERK = 2246                   # warrior, MP 40, Con 15000, self-damage 2 / 990 ms
VITALITY = 2798                  # Breath of Vitality: priest aura, mHP 50, Con 40000
DOUBLE_JUMP, DASH = 94, 80       # traveler passives (quest 26 / 28 rewards)
RELAX_HERB = 148                 # Type 0, Con 10000: the 0x42 item buff
WOODEN_BLADE, SHORT_BOW, DAGGER, CLERIC_WAND = 70, 11, 256, 248      # Kind 11 class weapons
QUEST26 = 26
# EN skill masters (hni UI 13; shop_storage-npc-buy checks the stock): Christina sells the
# warrior rows (292 IAP, 260 Ice Spear, ...), Sally the priest rows (501 Self Heal, ...).
CHRISTINA, SALLY = 25, 43

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
            'accounts.json changed during test_skillcast.py (tests must only use temp copies)'


def dec(pkt):
    return F.FakeClient.decode(pkt, allow_trailing=pkt.opcode == 0x03)


# =========================================================================== models
@unittest.skipUnless(HAVE_HII, 'needs the EN client hs/windslayer.hii (Skill_Lv / Job columns)')
class LearnModel(unittest.TestCase):
    """skills.learn / learn_slot: FUN_00426b80 over the persisted list."""

    def char(self, **kw):
        c = {'class': 1, 'exp': progression.exp_for_level(40), 'skills': []}
        c.update(kw)
        return c

    def test_the_family_rule_is_the_clients(self):
        c = self.char()
        self.assertEqual(SK.learn(c, IAP1), SK.Learned(True, IAP1, None, ''))
        self.assertEqual(SK.learn(c, ICE_SPEAR).ok, True)
        # a higher level of the family overwrites the lower one IN PLACE
        self.assertEqual(SK.learn(c, IAP2), SK.Learned(True, IAP2, IAP1, ''))
        self.assertEqual(c['skills'], [IAP2, ICE_SPEAR])
        # the same or a lower level changes nothing (the client prints nothing either)
        self.assertEqual(SK.learn(c, IAP1).why, f'already learned ({IAP2} >= {IAP1})')
        self.assertEqual(SK.learn(c, IAP2).why, 'already learned')
        # a family base record (Skill_Lv 0) is never learned: FUN_00426b80 returns 0
        self.assertFalse(SK.learn(c, 291, check=False).ok)
        self.assertEqual(c['skills'], [IAP2, ICE_SPEAR])

    def test_thirty_families_fill_the_list(self):
        c = self.char()
        c['skills'] = [sd.id for sd in SK.all_skills().values()
                       if sd.skill_lv == 1][:SK.MAX_LEARNED]
        res = SK.learn(c, DOUBLE_JUMP, check=False)
        self.assertEqual(res.why, 'all 30 skill slots are used')

    def test_level_and_class_gates_only_when_checked(self):
        novice = self.char(**{'class': 0, 'exp': 0})
        self.assertIn('needs level 12', SK.learn(novice, IAP1, level=1).why)
        self.assertIn('class 0 may not learn', SK.learn(novice, IAP1, level=40).why)
        self.assertEqual(novice['skills'], [])
        # a mirrored grant (quest reward, type-3 0x18) has neither gate, like the client
        self.assertTrue(SK.learn(novice, IAP1, check=False).ok)
        self.assertTrue(SK.learn(novice, DOUBLE_JUMP, level=1).ok)     # traveler passive, Lv1

    def test_ensure_is_structural_and_leaves_a_clean_list_alone(self):
        c = {'skills': [94, '292', 0, 94, 5000, 'x', {'skill_id': 80}]}
        SK.ensure(c)
        self.assertEqual(c['skills'], [94, 292, 80])
        same = c['skills']
        SK.ensure(c)
        self.assertIs(c['skills'], same)
        self.assertEqual(SK.ensure({}), [])


@unittest.skipUnless(HAVE_HII, 'needs the EN client hs/windslayer.hii')
class BuffModel(unittest.TestCase):
    """buffs.py: FUN_00424e20's slot rules, deadlines and persistence on a fake clock."""

    def test_groups_and_removal_opcodes(self):
        self.assertEqual(B.group_of(IAP1), B.group_of(IAP2))           # one family
        self.assertEqual(B.group_of(RELAX_HERB), 'i148')
        self.assertEqual(B.group_of(ROARING_DOG), B.group_of(2510))    # summons replace summons
        self.assertEqual(B.group_of(0x1D4), B.group_of(0xB1A))         # Magic Shield cross rule
        self.assertTrue(B.client_replaces(IAP1))
        self.assertFalse(B.client_replaces(RELAX_HERB))                # item buffs stack (C4)
        self.assertFalse(B.client_replaces(0xAA0))                     # fairies stack
        # 0x43 recomputes stats: Skill_P_A/P_D, mHP, auras; 0x3C only removes the slot
        self.assertEqual([B.removal_opcode(i) for i in (IAP1, VITALITY, BERSERK)], ['0x43'] * 3)
        self.assertEqual([B.removal_opcode(i) for i in (BOOBY_TRAP, ROARING_DOG, RELAX_HERB)],
                         ['0x3C'] * 3)
        self.assertEqual((B.duration_ms(IAP1), B.duration_ms(ROARING_DOG), B.duration_ms(RELAX_HERB)),
                         (30000, 60000, 10000))

    def test_recast_replaces_in_place_and_an_item_buff_is_displaced(self):
        s = {}
        a, gone = B.apply(s, IAP1, 0.0)
        self.assertIsNone(gone)
        B.apply(s, RELAX_HERB, 1.0)
        b, gone = B.apply(s, IAP2, 5.0)                                # Lv2 over Lv1: same slot
        self.assertIsNone(gone)
        self.assertEqual([x['id'] for x in s['buffs']], [IAP2, RELAX_HERB])
        self.assertEqual(b['expires'], 35.0)
        c, gone = B.apply(s, RELAX_HERB, 6.0)                          # the client would stack
        self.assertEqual(gone['id'], RELAX_HERB)
        self.assertEqual([x['id'] for x in s['buffs']], [IAP2, RELAX_HERB])
        self.assertEqual(c['expires'], 16.0)

    def test_deadlines_map_load_and_persistence(self):
        s = {}
        B.apply(s, IAP1, 0.0)
        B.apply(s, BOOBY_TRAP, 0.0, x=1200, y=700)
        B.apply(s, RELAX_HERB, 0.0)
        self.assertEqual(B.rows(s['buffs'], 1.0),
                         [{'id': IAP1, 'remaining_ms': 29000, 'x': 0, 'y': 0},
                          {'id': BOOBY_TRAP, 'remaining_ms': 6020, 'x': 1200, 'y': 700},
                          {'id': RELAX_HERB, 'remaining_ms': 9000, 'x': 0, 'y': 0}])
        self.assertEqual(B.pop_expired(s, 7.0), [])
        self.assertEqual([b['id'] for b in B.pop_expired(s, 10.0)], [BOOBY_TRAP, RELAX_HERB])
        B.apply(s, BOOBY_TRAP, 0.0, x=1200, y=700)
        self.assertEqual(B.persist(s, 1.0), [{'id': IAP1, 'remaining_ms': 29000, 'x': 0, 'y': 0,
                                              'src': 0}])              # traps are never kept
        dropped = B.prune_for_map_load(s, 2.0)
        self.assertEqual([b['id'] for b in dropped], [BOOBY_TRAP])
        stored = B.persist(s, 20.0)
        fresh = {}
        B.restore(fresh, stored + [{'id': BOOBY_TRAP, 'remaining_ms': 5000},
                                   {'id': 4356, 'remaining_ms': 5000},
                                   {'id': IAP2, 'remaining_ms': 5000},   # same group: dropped
                                   {'id': 303, 'remaining_ms': 0}], 100.0)
        self.assertEqual([(b['id'], B.remaining_ms(b, 100.0)) for b in fresh['buffs']], [(IAP1, 10000)])
        self.assertEqual(B.rows([{'id': IAP1, 'remaining_ms': 0}], 0.0), [])

    def test_a_full_table_drops_the_new_slot(self):
        s = {'buffs': []}
        fam, groups = [], set()
        for sd in SK.all_skills().values():
            if sd.castable and sd.slot_on_3b and B.group_of(sd.id) not in groups:
                groups.add(B.group_of(sd.id))
                fam.append(sd.id)
        self.assertEqual(len(fam), B.MAX_SLOTS)          # the EN hii has exactly 21 groups
        for sid in fam:
            self.assertIsNotNone(B.apply(s, sid, 0.0)[0], sid)
        self.assertEqual(B.apply(s, RELAX_HERB, 0.0), (None, None))
        self.assertEqual(len(s['buffs']), B.MAX_SLOTS)

    def test_an_aura_raises_max_hp_while_it_lasts(self):
        char = {'class': 6, 'exp': progression.exp_for_level(40), 'spr': 3, 'int': 1}
        s = {}
        base = hpmp.derive(s, char).max_hp
        B.apply(s, VITALITY, 0.0)
        self.assertEqual(s['slot_buffs'], [{'id': VITALITY}])
        self.assertEqual(hpmp.derive(s, char).max_hp, base + 50)
        B.pop_expired(s, 40.0)
        self.assertEqual((s['slot_buffs'], hpmp.derive(s, char).max_hp), ([], base))

    def test_berserk_ticks_every_990_ms_without_bursting(self):
        s = {}
        B.apply(s, BERSERK, 0.0)
        self.assertEqual(B.due_berserk(s, 0.98), [])
        self.assertEqual([d for _, d in B.due_berserk(s, 0.99)], [2])
        self.assertEqual(B.due_berserk(s, 1.5), [])
        self.assertEqual(len(B.due_berserk(s, 9.0)), 1)                # late: once, then on
        self.assertEqual(B.due_berserk(s, 9.5), [])

    def test_modifiers(self):
        s = {}
        B.apply(s, IAP1, 0.0)
        B.apply(s, BERSERK, 0.0)
        self.assertEqual(B.modifiers(s), B.Mods(30, -27, 0, 0))

    def test_ensure(self):
        c = {'buffs': [{'id': 292, 'remaining_ms': 5000}, {'id': 0, 'remaining_ms': 5},
                       {'id': 293, 'remaining_ms': -1}, 'x']}
        B.ensure(c)
        self.assertEqual(c['buffs'], [{'id': 292, 'remaining_ms': 5000, 'x': 0, 'y': 0, 'src': 0}])
        same = c['buffs']
        B.ensure(c)
        self.assertIs(c['buffs'], same)


# =========================================================================== server
class SkillServer(unittest.TestCase):
    """A fresh GameServer on a temp accounts.json; TestHero is a GM (dev commands)."""
    config = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_skill_')
        self.server = F.make_server(self.tmp, accounts=copy.deepcopy(ACCOUNTS), config=self.config)
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- helpers ---
    def char(self, name='TestHero', user='test'):
        return self.server.store.find_character(user, name)

    def hero(self, cls=1, level=40, weapon=WOODEN_BLADE, hp=None, mp=None, skills=()):
        """Shape TestHero in the store before entering: class, level, a class weapon in grid
        slot 5 (EN Kind 11, C33) and the learned list."""
        with self.server.store.lock:
            ch = self.char()
            ch['class'] = cls
            ch['exp'] = progression.exp_for_level(level)
            ch['equipped'] = {5: {'id': weapon, 'w': [0] * 6}} if weapon else {}
            ch['skills'] = list(skills)
            d = hpmp.derive({}, ch)
            ch['hp'] = d.max_hp // 2 if hp is None else hp
            ch['mp'] = d.max_mp // 2 if mp is None else mp
        self.server.store.mark_dirty('test setup')
        return ch

    def enter(self, user='test', password='test', name='TestHero', monsters=0):
        c = F.FakeClient(self.server)
        self.clients.append(c)
        c.send_c2s('0x44D8BF/0x01', {'account_id': user, 'password': password})
        self.assertEqual(dec(c.expect(0x02))['result'], 1)
        map_code = int(self.char(name, user).get('map') or 101)
        c.send_c2s('0x42F904/0x2B', {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907, 'char_name': name})
        # world-presence: + 0x04 / 0x05 when another client is on that map (F.expect_entry)
        pkts = F.expect_entry(c, (0x03, 0x07, 0x15, 0x28, 0x44, *F.mob_packets(monsters)),
                              self.clients, map_code)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world')))
        return c, pkts

    def relog(self, c, **kw):
        c.close()
        c.thread.join(timeout=5.0)
        self.assertFalse(c.thread.is_alive())
        return self.enter(**kw)

    def portal(self, c):
        c.send(0x7E, CAP_7E_101_TO_102)
        return c.expect(0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(8))

    @staticmethod
    def row(pkt_07):
        return dec(pkt_07)['repeat[player_count]'][0]

    def skill_list(self, pkt_07):
        return [s['skill_id'] for s in self.row(pkt_07)['repeat[skill_count]']]

    def buff_list(self, pkt_07):
        return [(b['buff_skill_id'], b['buff_duration']) for b in self.row(pkt_07)['repeat[buff_count]']]

    def cast(self, c, skill, x=None, y=None):
        if x is None:
            c.send_c2s('0x44C239/0x15', {'skill_id': skill})
        else:
            c.send(0x15, struct.pack('<HHH', skill, x, y))

    def gm(self, c, text):
        c.send_c2s('0x445CA7/0x03', {'msg_len': len(text), 'message': text})

    @staticmethod
    def text(pkt):
        return dec(pkt)['text']

    def vitals(self, c):
        return c.session['hp'], c.session['mp']


@unittest.skipUnless(HAVE_HII, 'needs the EN client hs/windslayer.hii')
class Learn(SkillServer):
    """cs-skill-learn: every way a skill is learned lands in char['skills'] and the 0x07."""

    def test_dev_learn_survives_portal_and_relog(self):
        self.hero()
        c, _ = self.enter()
        self.gm(c, f'!learn {IAP1}')
        grant, line = c.expect(0x18, 0x15)
        g = dec(grant)
        self.assertEqual((g['item_id'], g['count'], g['gold']), (IAP1, 1, S.DEFAULT_GOLD))
        self.assertIn('Learned Increase Attack Power Lv1', self.text(line))
        self.assertEqual(self.char()['skills'], [IAP1])
        pkts = self.portal(c)
        self.assertEqual(self.skill_list(pkts[2]), [IAP1])
        c2, pkts = self.relog(c, monsters=8)
        self.assertEqual(self.skill_list(pkts[1]), [IAP1])
        self.server.store.flush()
        with open(self.server.db_file, encoding='utf-8') as f:
            stored = json.load(f)
        self.assertEqual(stored['test']['characters'][0]['skills'], [IAP1])

    def test_dev_learn_gates_and_force(self):
        self.hero(cls=0, level=1, weapon=0)
        c, _ = self.enter()
        self.gm(c, f'!learn {IAP1}')
        self.assertIn('needs level 12', self.text(c.expect(0x15)))
        self.gm(c, f'!learn {IAP1} force')
        c.expect(0x18, 0x15)
        self.gm(c, f'!learn {IAP1} force')
        self.assertIn('already learned', self.text(c.expect(0x15)))
        self.gm(c, '!learn 291')
        self.assertIn('Skill_Lv 0', self.text(c.expect(0x15)))
        self.gm(c, '!learn 179')
        self.assertIn('not an EN skill record', self.text(c.expect(0x15)))
        self.assertEqual(self.char()['skills'], [IAP1])

    def test_skills_and_unlearn(self):
        self.hero(skills=[IAP1, DOUBLE_JUMP])
        c, _ = self.enter()
        self.gm(c, '!skills')
        skills_line, buffs_line = c.expect(0x15, 0x15)
        self.assertIn(f'{IAP1} {DOUBLE_JUMP}', self.text(skills_line))
        self.assertIn('Buffs: none', self.text(buffs_line))
        self.gm(c, f'!unlearn {IAP1}')
        c.expect(0x15)
        self.assertEqual(self.char()['skills'], [DOUBLE_JUMP])
        self.assertEqual(self.skill_list(self.portal(c)[2]), [DOUBLE_JUMP])

    def test_buying_a_skill_book_learns_it_and_charges_once(self):
        """B8: the buy used to charge and send 0x18 but record nothing (a phantom)."""
        self.hero()
        c, _ = self.enter()
        c.send(0x0B, struct.pack('<HHH', IAP1, 1, CHRISTINA))
        g = dec(c.expect(0x18))
        self.assertEqual((g['item_id'], g['count'], g['gold']), (IAP1, 1, S.DEFAULT_GOLD - 560))
        self.assertEqual(self.char()['skills'], [IAP1])
        self.assertEqual(self.server._inventory(c.session), {})        # no bag entry
        c.send(0x0B, struct.pack('<HHH', IAP1, 1, CHRISTINA))            # again: refused
        resync, why = c.expect(0x18, 0x15)
        self.assertEqual((dec(resync)['item_id'], dec(resync)['gold']), (0, S.DEFAULT_GOLD - 560))
        self.assertEqual(self.text(why), '[Warning] You already have learned this skill.')
        c.send(0x0B, struct.pack('<HHH', SELF_HEAL, 1, SALLY))          # priest skill
        self.assertEqual(self.text(c.expect(0x18, 0x15)[1]), '[Warning] Your class cannot learn this skill.')
        c.send(0x0B, struct.pack('<HHH', 301, 1, CHRISTINA))            # IAP Lv10 needs Lv57
        self.assertEqual(self.text(c.expect(0x18, 0x15)[1]),
                         '[Warning] Your level is too low to learn this skill.')
        with self.server.store.lock:
            self.char()['gold'] = 100
        c.send(0x0B, struct.pack('<HHH', IAP2, 1, CHRISTINA))
        self.assertEqual(self.text(c.expect(0x18, 0x15)[1]), '[Warning] Not enough gold.')
        self.assertEqual(self.char()['skills'], [IAP1])

    def test_quest_26_double_jump_survives_relog(self):
        """The P2 live finding: the client learned Double Jump on the 0x27 and lost it on
        the next relog, because the server never recorded it."""
        c, _ = self.enter()
        c.send_c2s('0x47734D/0x16', {'quest_id': QUEST26})
        c.expect(0x26, 0x59)
        Q.QuestState(self.char()).set_progress(0, 1)
        c.send_c2s('0x477F3E/0x17', {'quest_id': QUEST26})
        c.expect(0x27, 0x21)                                   # no 0x18: the client learned it
        self.assertEqual(self.char()['skills'], [DOUBLE_JUMP])
        c2, pkts = self.relog(c)
        self.assertEqual(self.skill_list(pkts[1]), [DOUBLE_JUMP])

    def test_give_of_a_type_3_id_is_a_persisted_learn(self):
        c, _ = self.enter()
        self.gm(c, f'!give {DASH}')
        grant, _line = c.expect(0x18, 0x15)
        self.assertEqual(dec(grant)['item_id'], DASH)
        self.assertEqual(self.char()['skills'], [DASH])


@unittest.skipUnless(HAVE_HII, 'needs the EN client hs/windslayer.hii')
class StartingSkills(SkillServer):
    config = cfgmod.from_dict({'STARTING_SKILLS': [DASH, DOUBLE_JUMP]})

    def test_a_new_character_is_born_with_them(self):
        c = F.FakeClient(self.server)
        self.clients.append(c)
        c.send_c2s('0x44D8BF/0x01', {'account_id': 'admin', 'password': 'admin'})
        c.expect(0x02)
        c.send_c2s('0x449384/0x0E', {'look_slot10': 3, 'look_slot1': 2, 'look_slot6': 4,
                                     'look_slot5': 5, 'look_slot9': 3, 'name': 'Nova',
                                     'str': 3, 'dex': 2, 'int': 1, 'tol': 3})
        self.assertEqual(dec(c.expect(0x1C))['result'], 1)
        self.assertEqual(self.char('Nova', 'admin')['skills'], [DASH, DOUBLE_JUMP])

    def test_bad_config_is_refused(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.from_dict({'STARTING_SKILLS': [0]})


@unittest.skipUnless(HAVE_HII, 'needs the EN client hs/windslayer.hii')
class Cast(SkillServer):
    """cs-skill-cast: one reply per cast, the cost, the cooldown, the trailing absolutes."""

    def test_self_buff_cost_cooldown_and_server_expiry(self):
        """P3 exit criterion 3 offline: MP drops by the cost, the cooldown runs, the buff
        icon appears (0x3B) and the SERVER ends it on time (0x43, C4)."""
        self.hero(hp=300, mp=100, skills=[IAP1])
        c, _ = self.enter()
        self.cast(c, IAP1)
        apply, hp, mp = c.expect(0x3B, 0x28, 0x44)                  # no 0x25: 0x3B is the reply
        self.assertEqual(len(apply.payload), 6)
        self.assertEqual(dec(apply), {'item_or_skill_id': IAP1, 'target_uid': 1})
        self.assertEqual((dec(hp)['hp'], dec(mp)['mp']), (295, 95))
        self.assertEqual(self.vitals(c), (295, 95))
        buff = B.find(c.session, IAP1)
        self.assertIsNotNone(buff)

        self.cast(c, IAP1)                                          # inside CT 10000
        self.assertEqual(c.expect(0x5F).payload, b'')
        self.assertEqual(self.vitals(c), (295, 95))                 # a refusal costs nothing

        self.assertEqual(self.server._tick_buffs(buff['expires'] - 0.01), 0)
        c.expect_silence(0.15)
        self.assertEqual(self.server._tick_buffs(buff['expires']), 1)
        self.assertEqual(dec(c.expect(0x43)), {'buff_item_id': IAP1, 'target_uid': 1})
        self.assertEqual(c.session['buffs'], [])
        self.assertEqual(self.server._tick_buffs(buff['expires'] + 5), 0)

    def test_a_recast_refreshes_the_slot_in_place(self):
        self.hero(hp=300, mp=100, skills=[IAP1])
        c, _ = self.enter()
        self.cast(c, IAP1)
        c.expect(0x3B, 0x28, 0x44)
        first = B.find(c.session, IAP1)['expires']
        c.session[W.GameServer.SKILL_CD_KEY][291] -= 10.0           # cooldown over
        self.cast(c, IAP1)
        c.expect(0x3B, 0x28, 0x44)                                  # no removal: the client
        self.assertEqual(len(c.session['buffs']), 1)                # overwrites the family slot
        self.assertGreater(B.find(c.session, IAP1)['expires'], first)

    def test_cooldown_tolerance(self):
        self.hero(mp=100, skills=[ICE_SPEAR])
        c, _ = self.enter()
        self.cast(c, ICE_SPEAR)
        c.expect(0x25, 0x44)
        c.session[W.GameServer.SKILL_CD_KEY][ICE_SPEAR - 1] -= (5000 - 140) / 1000.0
        self.cast(c, ICE_SPEAR)                                     # 140 ms early: accepted
        c.expect(0x25, 0x44)

    def test_the_cooldown_survives_portal_and_relog(self):
        """The map load resets the client's own stamps; the server's must not reset, or a
        portal / relog would skip any CT."""
        self.hero(mp=100, skills=[ICE_SPEAR])
        c, _ = self.enter()
        self.cast(c, ICE_SPEAR)
        c.expect(0x25, 0x44)
        self.portal(c)
        self.cast(c, ICE_SPEAR)
        c.expect(0x5F)
        c2, _ = self.relog(c, monsters=8)
        self.cast(c2, ICE_SPEAR)
        c2.expect(0x5F)

    def test_every_refusal_is_one_0x5F_and_costs_nothing(self):
        self.hero(hp=300, mp=100, skills=[IAP1, DOUBLE_JUMP])
        c, _ = self.enter()
        cases = [
            ('not learned', lambda: None, ICE_SPEAR, 'not learned'),
            ('passive', lambda: None, DOUBLE_JUMP, 'is never cast'),
            ('base record', lambda: None, 291, 'is never cast'),
            ('no class weapon', lambda: self.char().update(equipped={}), IAP1, 'class weapon'),
            ('MP low', lambda: c.session.update(mp=4), IAP1, 'MP low'),
            ('HP gate', lambda: c.session.update(mp=100, hp=5), IAP1, 'Not enough HP'),
            ('dead', lambda: c.session.update(hp=300, dead=True), IAP1, 'dead'),
            ('PvP room map', lambda: c.session.update(dead=False, current_map=9801), IAP1, 'PvP room'),
        ]
        for label, setup, skill, why in cases:
            with self.subTest(label):
                with self.server._combat_lock(c.session):
                    setup()
                before = self.vitals(c)
                with self.assertLogs('WS', logging.INFO) as cm:
                    self.cast(c, skill)
                    self.assertEqual(c.expect(0x5F).payload, b'')
                self.assertTrue(any(why in line for line in cm.output), cm.output)
                self.assertEqual(self.vitals(c), before)
                if label == 'no class weapon':
                    with self.server.store.lock:
                        self.char()['equipped'] = {5: {'id': WOODEN_BLADE, 'w': [0] * 6}}
        self.assertEqual(c.session['buffs'], [])

    def test_class_gate(self):
        self.hero(cls=6, weapon=CLERIC_WAND, skills=[IAP1])        # a priest with a warrior skill
        c, _ = self.enter()
        with self.assertLogs('WS', logging.INFO) as cm:
            self.cast(c, IAP1)
            c.expect(0x5F)
        self.assertTrue(any('class 6 may not use it' in line for line in cm.output))

    def test_attack_skill_is_0x25_and_mp_only(self):
        self.hero(hp=300, mp=100, skills=[ICE_SPEAR])
        c, _ = self.enter()
        with self.assertLogs('WS', logging.INFO) as cm:
            self.cast(c, ICE_SPEAR)
            use, mp = c.expect(0x25, 0x44)
        self.assertEqual(dec(use), {'item_or_skill_id': ICE_SPEAR})
        self.assertEqual(dec(mp)['mp'], 94)
        self.assertTrue(any('cs-skill-damage' in line for line in cm.output))
        self.assertEqual(c.session['buffs'], [])

    def test_utility_open_stall_is_0x25_alone(self):
        """cs-utility-skills: no cost, so no absolute follows; the client opens window 0x259."""
        self.hero(cls=0, level=10, weapon=0, skills=[OPEN_STALL])
        c, _ = self.enter()
        with self.assertLogs('WS', logging.INFO) as cm:
            self.cast(c, OPEN_STALL)
            self.assertEqual(dec(c.expect(0x25)), {'item_or_skill_id': OPEN_STALL})
        self.assertTrue(any('stall window 0x259' in line for line in cm.output))

    def test_self_heal_adds_its_hp(self):
        self.hero(cls=6, weapon=CLERIC_WAND, hp=100, mp=100, skills=[SELF_HEAL])
        c, _ = self.enter()
        self.cast(c, SELF_HEAL)
        use, hp, mp = c.expect(0x25, 0x28, 0x44)
        self.assertEqual((dec(hp)['hp'], dec(mp)['mp']), (120, 90))

    def test_booby_trap_echoes_its_point_expires_with_0x3C_and_stays_behind(self):
        self.hero(cls=4, weapon=DAGGER, mp=100, skills=[BOOBY_TRAP])
        c, _ = self.enter()
        self.cast(c, BOOBY_TRAP, 1200, 700)
        apply, mp = c.expect(0x3B, 0x44)
        self.assertEqual(len(apply.payload), 10)
        self.assertEqual(dec(apply), {'item_or_skill_id': BOOBY_TRAP, 'target_uid': 1,
                                      'ground_x': 1200, 'ground_y': 700})
        self.assertEqual(dec(mp)['mp'], 84)
        trap = B.find(c.session, BOOBY_TRAP)
        self.assertEqual((trap['x'], trap['y']), (1200, 700))
        self.server._tick_buffs(trap['expires'])
        self.assertEqual(dec(c.expect(0x3C)), {'buff_item_id': BOOBY_TRAP, 'target_uid': 1})
        # a placed trap is not carried through a map load (F2 step 2)
        c.session[W.GameServer.SKILL_CD_KEY].clear()
        self.cast(c, BOOBY_TRAP, 1200, 700)
        c.expect(0x3B, 0x44)
        self.assertEqual(self.buff_list(self.portal(c)[2]), [])
        self.assertEqual(c.session['buffs'], [])

    def test_summon_gets_a_60_s_slot(self):
        self.hero(cls=3, weapon=SHORT_BOW, hp=200, mp=100, skills=[ROARING_DOG])
        c, _ = self.enter()
        self.cast(c, ROARING_DOG)
        apply, mp = c.expect(0x3B, 0x44)                            # the HP -10 is the pet's
        self.assertEqual(dec(mp)['mp'], 81)
        dog = B.find(c.session, ROARING_DOG)
        self.assertAlmostEqual(B.remaining_ms(dog, time.monotonic()), 60000, delta=500)
        self.server._tick_buffs(dog['expires'])
        self.assertEqual(dec(c.expect(0x3C))['buff_item_id'], ROARING_DOG)

    def test_a_cast_restarts_the_idle_hp_regen_window(self):
        self.hero(mp=100, skills=[ICE_SPEAR])
        c, _ = self.enter()
        before = c.session['regen']['hp_due']
        time.sleep(0.02)
        self.cast(c, ICE_SPEAR)
        c.expect(0x25, 0x44)
        self.assertGreater(c.session['regen']['hp_due'], before)


@unittest.skipUnless(HAVE_HII, 'needs the EN client hs/windslayer.hii')
class Buffs(SkillServer):
    """cs-buffs: the running slots survive portal and relog with their remaining time."""

    def test_a_buff_survives_a_portal_with_its_remaining_time(self):
        self.hero(hp=300, mp=100, skills=[IAP1])
        c, _ = self.enter()
        self.cast(c, IAP1)
        c.expect(0x3B, 0x28, 0x44)
        pkts = self.portal(c)
        [(bid, left)] = self.buff_list(pkts[2])
        self.assertEqual(bid, IAP1)
        self.assertTrue(28000 < left <= 30000, left)
        # and the server still ends it on time on the new map
        self.server._tick_buffs(B.find(c.session, IAP1)['expires'])
        c.expect(0x43)

    def test_a_buff_survives_a_relog_with_its_remaining_time(self):
        """Frozen while offline: the relog 0x07 carries what was left at the disconnect."""
        self.hero(hp=300, mp=100, skills=[IAP1])
        c, _ = self.enter()
        self.cast(c, IAP1)
        c.expect(0x3B, 0x28, 0x44)
        c.close()
        c.thread.join(timeout=5.0)
        stored = self.char()['buffs']
        self.assertEqual([b['id'] for b in stored], [IAP1])
        at_logout = stored[0]['remaining_ms']
        self.assertTrue(28000 < at_logout <= 30000, at_logout)
        time.sleep(0.3)                                             # offline time is not counted
        c2, pkts = self.enter()
        [(bid, left)] = self.buff_list(pkts[1])
        self.assertEqual(bid, IAP1)
        self.assertTrue(at_logout - 250 <= left <= at_logout, (left, at_logout))
        self.assertIsNotNone(B.find(c2.session, IAP1))

    def test_identical_item_buffs_do_not_stack(self):
        """item_inventory#11: a second 0x42 for item 148 adds a second record client-side, so
        the running one is removed first (0x3C: no stat columns) - one icon, refreshed."""
        c, _ = self.enter()
        self.assertIsNotNone(self.server._inv_add(c.session, RELAX_HERB, 2, 'test'))
        c.send_c2s('0x44C2B3/0x15', {'item_id': RELAX_HERB})
        c.expect(0x25, 0x42)
        first = B.find(c.session, RELAX_HERB)['expires']
        c.session[W.GameServer.ITEM_COOLDOWN_KEY].clear()           # past the item's 5 s CT
        time.sleep(0.02)
        c.send_c2s('0x44C2B3/0x15', {'item_id': RELAX_HERB})
        use, removal, again = c.expect(0x25, 0x3C, 0x42)
        self.assertEqual(dec(removal), {'buff_item_id': RELAX_HERB, 'target_uid': 1})
        self.assertEqual([b['id'] for b in c.session['buffs']], [RELAX_HERB])
        buff = B.find(c.session, RELAX_HERB)
        self.assertGreater(buff['expires'], first)
        self.server._tick_buffs(buff['expires'])
        self.assertEqual(dec(c.expect(0x3C))['buff_item_id'], RELAX_HERB)

    def test_berserk_self_damage_every_990_ms(self):
        self.hero(hp=300, mp=100, skills=[BERSERK])
        c, _ = self.enter()
        self.cast(c, BERSERK)
        c.expect(0x3B, 0x44)                                        # its HP column is not a cost
        tick = B.find(c.session, BERSERK)['next_tick']
        self.server._tick_buffs(tick)
        self.assertEqual(dec(c.expect(0x28))['hp'], 298)
        with self.server._combat_lock(c.session):
            c.session['hp'] = 1
        self.server._tick_buffs(tick + 0.99)                        # never the last point
        c.expect_silence(0.15)
        self.server._tick_buffs(B.find(c.session, BERSERK)['expires'])
        self.assertEqual(dec(c.expect(0x43))['buff_item_id'], BERSERK)

    def test_breath_of_vitality_raises_then_clamps_max_hp(self):
        self.hero(cls=6, weapon=CLERIC_WAND, mp=200, skills=[VITALITY])
        c, _ = self.enter()
        base = c.session['max_hp']
        self.cast(c, VITALITY)
        c.expect(0x3B, 0x44)
        self.assertEqual(c.session['max_hp'], base + 50)
        with self.server._combat_lock(c.session):
            c.session['hp'] = base + 50                             # e.g. regenerated to the top
        self.server._tick_buffs(B.find(c.session, VITALITY)['expires'])
        removal, hp = c.expect(0x43, 0x28)
        self.assertEqual(dec(hp)['hp'], base)
        self.assertEqual(c.session['max_hp'], base)

    def test_observers_see_the_slot_and_its_end(self):
        """F10: another client on the map gets the same 0x3B and removal, never 0x28/0x44."""
        self.hero(hp=300, mp=100, skills=[IAP1])
        c, _ = self.enter()
        other, _ = self.enter('admin', 'admin', 'Watcher')
        self.cast(c, IAP1)
        c.expect(0x3B, 0x28, 0x44)
        self.assertEqual(dec(other.expect(0x3B)), {'item_or_skill_id': IAP1, 'target_uid': 1})
        self.server._tick_buffs(B.find(c.session, IAP1)['expires'])
        c.expect(0x43)
        self.assertEqual(dec(other.expect(0x43))['target_uid'], 1)


# =========================================================================== store
class Migration(unittest.TestCase):
    """The P3 schema (skills, buffs) arrives through an idempotent migration that first
    writes the one-time accounts.json.bak-pre-p3 (hard rule for this phase)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_skill_store_')
        self.path = os.path.join(self.tmp, 'accounts.json')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_p3_backup_and_idempotent(self):
        # a P2-shaped file: already migrated once, but without skills/buffs
        st = S.Store(self.path).load()
        with st.lock:
            for acc in st.accounts.values():
                for ch in acc['characters']:
                    del ch['skills'], ch['buffs']
        st.save_now()
        with open(self.path, 'rb') as f:
            original = f.read()
        for suffix in S.BACKUP_SUFFIXES:
            if os.path.exists(self.path + suffix):
                os.remove(self.path + suffix)
        st = S.Store(self.path).load()
        self.assertIn('test/TestHero: skills created', st.migration_changes)
        self.assertIn('test/TestHero: buffs created', st.migration_changes)
        with open(self.path + '.bak-pre-p3', 'rb') as f:
            self.assertEqual(f.read(), original)
        hero = st.find_character('test', 'TestHero')
        self.assertEqual((hero['skills'], hero['buffs']), ([], []))
        again = S.Store(self.path).load()
        self.assertEqual((again.migration_changes, again.saves), ([], 0))

    def test_a_malformed_learned_list_is_repaired(self):
        st = S.Store(self.path).load()
        with st.lock:
            hero = st.find_character('test', 'TestHero')
            hero['skills'] = [94, 94, 0, 'x']
            hero['buffs'] = [{'id': 292, 'remaining_ms': 9000}, {'id': 292}]
        st.save_now()
        st = S.Store(self.path).load()
        self.assertIn('test/TestHero: skills normalized', st.migration_changes)
        hero = st.find_character('test', 'TestHero')
        self.assertEqual(hero['skills'], [94])
        self.assertEqual(hero['buffs'], [{'id': 292, 'remaining_ms': 9000, 'x': 0, 'y': 0, 'src': 0}])


if __name__ == '__main__':
    unittest.main(verbosity=2)
