#!/usr/bin/env python3
"""
test_sharedmobs.py - P5 stage 3 (shared-monsters-combat), offline, both client builds
====================================================================================
world-shared-monsters + party-dep-combat-multiclient, with two fake clients on one server
(TestHero uid 1 = WindSlayer_patched.exe, Watcher uid 2 = WindSlayer_p2.exe), each flow run
for the 2008 and the 2009 build:

- ONE set of monsters per map (world.MapMonsters): the same uids (server-wide ids.MONSTER
  block) in both clients' S2C 0x1A, one shared Monster object;
- a client's 61 B hit report damages only the mob it names, credited to that client's
  session; the attacker gets the 16-byte 0x2A release, everyone else the relay of his 0x0D
  (0x1B) and the hit relay on the mob (the full 0x2A: action 7, target = attacker, the
  server position - or the 0x1B node while MOB_AGGRO is off), never the attacker;
- a kill: 0x29 to every client, exp / gold only to the killer; the corpse's 0x06 and a
  single respawn 0x1A reach both;
- the monster AI on shared mobs: the chase words (16-byte 0x2A self-form, built per
  receiver with ITS uid) and the 0x9E hand-back go to every client; a client that arrives
  mid-chase gets the current word at once; the '1B' fallback takes it over first;
- contact damage only for a player the mob is after (MOB_CONTACT_AGGRO_ONLY);
- position authority: the chase target's reports (and the driver sample of his client);
- a DoT outlives its caster's stay and credits nobody; a driver swing with no uid hits each
  map once; `!mobs`;
- an empty map keeps its monsters (dead ones stay dead) for MOB_MAP_KEEP_SECS, then they
  are discarded and rebuilt fresh with the same uids (0 = at once).

No port is bound, no client or server is started; ticks run on explicit clocks and the live
accounts.json is never opened (temp copies; checked at the end of the module).
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
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import config as cfgmod  # noqa: E402
import debuffs as D  # noqa: E402
import en_content as EC  # noqa: E402
import fakeclient as F  # noqa: E402
import ids  # noqa: E402
import mobai as AI  # noqa: E402
import packets as P  # noqa: E402
import skills as SK  # noqa: E402
import world as WM  # noqa: E402

W = F.import_server()
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = cfgmod.BUILD_2008, cfgmod.BUILD_2009
DIRS = {B8: cfgmod.DEFAULTS['CLIENT_DIR'], B9: cfgmod.DEFAULTS['CLIENT_DIR_2009']}
HAVE = {b: os.path.exists(os.path.join(HERE, d, 'hs', 'windslayer.hii')) for b, d in DIRS.items()}

KEYS = {
    B8: {'login': '0x44D8BF/0x01', 'enter': '0x42F904/0x2B', 'move': '0x42CE94/0x0D',
         'chat': '0x445CA7/0x03', 'portal': '0x42F76B/0x7E'},
    B9: {'login': '0x451CE5/0x01', 'enter': '0x4315D7/0x2B', 'move': '0x42E704/0x0D',
         'chat': '0x44790E/0x03', 'portal': '0x431284/0x7E'},
}
ENTRY = {B8: (0x03, 0x07, 0x15, 0x28, 0x44), B9: (0x03, 0x07, 0x15, 0x65, 0x28, 0x44)}
PORTAL_101_TO_102, PORTAL_102_TO_101 = 23, 31
POISON = 391
WALK_L, WALK_R = 0x01, 0x02
HIT = W.GameServer.MOB_HIT_RELAY_EVENT << 12

# TestHero (uid 1, a GM) and Watcher (uid 2) both stand on map 102, Pupu #1 spawns at 1244.
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
            'accounts.json changed during test_sharedmobs.py (tests must only use temp copies)'


def blob(lo):
    return struct.pack('<II', lo, 0)


def cmd(mob, lo, receiver):
    """The 16-byte 0x2A self-form: mob uid, lo, hi 0, target = the RECEIVER's own uid."""
    return struct.pack('<I', mob.uid) + blob(lo) + struct.pack('<I', receiver)


def hand_back(mob):
    return struct.pack('<I', mob.uid) + bytes(8)


class SharedServer(unittest.TestCase):
    build = B8
    overrides = {}

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        self.tmp = tempfile.mkdtemp(prefix='ws_sharedmobs_')
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, **self.overrides})
        self.server = F.make_server(self.tmp, accounts=copy.deepcopy(ACCOUNTS), config=cfg)
        self.keys = KEYS[self.build]
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ setup ---
    def char(self, user='test'):
        return self.server.store.characters(user)[0]

    def enter(self, user='test', password='test', name='TestHero', mobs=8):
        """Login + enter world on map 102: the entry burst and its 0x1A (world-presence adds
        a 0x04 / 0x05 when the other client is there, F.expect_entry). Returns the client;
        its .uids are the monster uids of its 0x1A."""
        c = F.FakeClient(self.server)
        self.clients.append(c)
        if self.build == B9:
            c.send_c2s(self.keys['login'], F.sso_login(user, password))
        else:
            c.send_c2s(self.keys['login'], {'account_id': user, 'password': password})
        self.assertEqual(c.s2c(c.expect(0x02))['result'], 1)
        c.send_c2s(self.keys['enter'], {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907, 'char_name': name})
        pkts = F.expect_entry(c, (*ENTRY[self.build], *F.mob_packets(mobs)), self.clients, 102)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world') and s.get('current_map') == 102))
        c.uids = self.mob_uids(c, pkts)
        return c

    def two(self):
        a = self.enter()
        b = self.enter('admin', 'admin', 'Watcher')
        self.drain(a, 0.1)
        return a, b

    def mob_uids(self, c, pkts):
        return [r['uid'] for p in pkts if p.opcode == 0x1A for r in c.s2c(p)['repeat[count]']]

    def portal(self, c, index, dest, mobs=0):
        """A portal and its map load (other clients: their presence 0x05/0x06 is theirs)."""
        c.send_c2s(self.keys['portal'], {'portal_line_index': index})
        pkts = F.expect_entry(c, (0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(mobs)), self.clients, dest)
        self.assertTrue(c.wait_session(lambda s: s.get('current_map') == dest and s.get('in_world')))
        return self.mob_uids(c, pkts)

    def mob(self, c, i=1):
        return c.session['monsters'][c.uids[i]]

    def ready(self, c, i=1, hp=500, xy=(1244.0, 714.0)):
        """Monster #i with `hp` at `xy` (its spawn too), the other seven parked far away."""
        with self.server._combat(c.session):
            for k, m in enumerate(c.session['monsters'].values()):
                m.x, m.y = 5000.0 + 300 * k, 100.0
            mob = self.mob(c, i)
            x, y = float(xy[0]), float(xy[1])
            mob.x, mob.y = mob.spawn_x, mob.spawn_y = x, y
            mob.hp = mob.max_hp = hp
        return mob

    # -------------------------------------------------------------- traffic ---
    def move(self, c, state_lo, **extra):
        c.send_c2s(self.keys['move'], {'realtime_delta_ms': 0, 'map_code': 102, 'logic_elapsed_ms': 30,
                                       'state_lo': state_lo, 'state_hi': 0, **extra})

    def report_hit(self, c, mob, at=None):
        """The 61 B C2S 0x0D the client sends after its own hit detection caught a swing:
        interact event 7, the victim in the tail at `at` (default: the server's x/y)."""
        px, py = c.session['pos']
        vx, vy = at if at is not None else (mob.x, mob.y)
        self.move(c, W.HIT_REPORT_EVENT << 16, target_uid=mob.uid, pos_x=float(px), pos_y=float(py),
                  target_dx=float(vx - px), target_dy=float(vy - py), flag_8db=0, flag_8e7=0,
                  timer_dac=0, target_action_event=7)

    def touch(self, c, mob, action=6):
        """The victim's own client reports a monster bumping into him (action event 6)."""
        with self.server._combat_lock(c.session):
            c.session['contact_t'] = 0.0
        self.move(c, action << 12, event_source_uid=mob.uid)

    def drain(self, c, quiet=0.1):
        return [(p.opcode, p.payload) for p in c.recv_until_quiet(quiet)]

    def pin(self, c, mob, x, y, now):
        with self.server._combat(c.session):
            mob.x, mob.y, mob.fix_t = float(x), float(y), now


class SharedFlows:
    """Run for each build by the concrete classes below."""

    # ------------------------------------------------------------ one set ---
    def test_one_set_of_monsters_with_the_same_uids_on_both_clients(self):
        a, b = self.two()
        self.assertEqual(len(a.uids), 8)
        self.assertEqual(a.uids, b.uids)                              # the same 8 uids, once each
        self.assertTrue(all(uid in ids.MONSTER for uid in a.uids))
        self.assertEqual(a.uids, list(range(W.MOB_UID_BASE, W.MOB_UID_BASE + 8)))
        self.assertIs(a.session['monsters'], b.session['monsters'])
        mons = self.server.world.map(102).monsters
        self.assertIsInstance(mons, WM.MapMonsters)
        self.assertIs(a.session['monsters'], mons)
        self.assertEqual(set(mons.viewers), {1, 2})
        self.assertEqual(mons.held, {1: set(a.uids), 2: set(a.uids)})

    def test_the_uid_allocator_is_server_wide(self):
        world = self.server.world
        first = world.alloc_monster_uids(3)
        self.assertEqual(world.alloc_monster_uids(2), [first[-1] + 1, first[-1] + 2])
        world._next_monster_uid = ids.MONSTER.hi
        with self.assertRaises(ids.IdSpaceExhausted):
            world.alloc_monster_uids(2)

    # --------------------------------------------------- per-client combat ---
    def test_a_hit_report_damages_only_the_mob_it_names_and_the_others_see_it(self):
        a, b = self.two()
        m1, m2 = self.ready(a, 1, hp=50), self.mob(a, 2)
        m2.hp = m2.max_hp = 50
        before = {m.uid: m.hp for m in a.session['monsters'].values()}
        self.report_hit(a, m1)
        # the attacker: exactly the 16-byte release aimed at himself
        self.assertEqual(a.expect(0x2A).payload, cmd(m1, AI.STOP, 1))
        # the other client: A's 0x0D as a 0x1B node on A, then the hit on the mob - the full
        # 0x2A {mob, action 7, target = A, x, y, airborne 0}, never back to A
        relay, hit = b.expect(0x1B, 0x2A)
        self.assertEqual(b.s2c(relay)['uid'], 1)
        self.assertEqual(struct.unpack('<IIIIddB', hit.payload), (m1.uid, HIT, 0, 1, m1.x, m1.y, 0))
        self.assertLess(m1.hp, 50)
        self.assertEqual({u: hp for u, hp in before.items() if u != m1.uid},
                         {m.uid: m.hp for m in a.session['monsters'].values() if m.uid != m1.uid})
        self.assertEqual((m1.aggro_uid, m1.hate), (1, {1: 50 - m1.hp}))
        # B's report names mob #2: only #2 is hurt, and it is B's hit
        hp1 = m1.hp
        self.report_hit(b, m2)
        self.assertEqual(b.expect(0x2A).payload, cmd(m2, AI.STOP, 2))
        relay, hit = a.expect(0x1B, 0x2A)
        self.assertEqual(a.s2c(relay)['uid'], 2)
        self.assertEqual(struct.unpack_from('<IIII', hit.payload), (m2.uid, HIT, 0, 2))
        self.assertEqual((m1.hp, m2.aggro_uid, m2.hate), (hp1, 2, {2: 50 - m2.hp}))

    def test_a_kill_reaches_every_client_and_only_the_killer_is_rewarded(self):
        a, b = self.two()
        mob = self.ready(a, 1, hp=1)
        exp_a, exp_b = self.char('test').get('exp', 0), self.char('admin').get('exp', 0)
        gold_b = W.invmod.Wallet(self.char('admin')).gold
        with mock.patch.object(W.random, 'random', return_value=1.0):      # no drop roll
            self.report_hit(a, mob)
            self.assertEqual([op for op, _ in self.drain(a)], [0x29, 0x21, 0x18])
        relay, death = b.expect(0x1B, 0x29)
        self.assertEqual(b.s2c(death), {'uid': mob.uid, 'respawn_tick': 0x7FFFFFFF,
                                        'respawn_x': 1244, 'respawn_y': 714})
        self.assertFalse(mob.alive)
        self.assertEqual(self.char('test')['exp'], exp_a + mob.exp)
        self.assertEqual((self.char('admin').get('exp', 0), W.invmod.Wallet(self.char('admin')).gold),
                         (exp_b, gold_b))
        self.report_hit(b, mob)                                            # a corpse takes no hit
        a.expect(0x1B)
        b.expect_silence(0.15)

    def test_the_corpse_goes_and_a_single_respawn_comes_on_both(self):
        a, b = self.two()
        mob = self.ready(a, 1, hp=1)
        self.report_hit(b, mob)                                            # B's kill this time
        self.drain(a)
        self.drain(b)
        now = time.monotonic()
        self.server.ticks.run_due(now + W.MOB_CORPSE_SECS + 0.5)
        for c in (a, b):
            self.assertEqual(c.s2c(c.expect(0x06)), {'uid': mob.uid})
        self.server.ticks.run_due(now + W.MOB_RESPAWN_SECS + 0.5)
        for c in (a, b):
            self.assertEqual([r['uid'] for r in c.s2c(c.expect(0x1A))['repeat[count]']], [mob.uid])
        self.assertTrue(mob.alive)
        self.server.ticks.run_due(now + 60.0)                              # one respawn, not two
        a.expect_silence(0.1)
        b.expect_silence(0.1)

    def test_a_driver_swing_without_a_uid_hits_each_map_once(self):
        a, b = self.two()
        mob = self.ready(a, 1, hp=500)
        self.server._memory_melee(mob.x, mob.y)
        self.assertEqual(mob.max_hp - mob.hp, self.server._compute_damage(a.session, 0, mob))

    # ------------------------------------------------------------ the AI ---
    def test_chase_words_reach_every_client_each_with_its_own_uid(self):
        a, b = self.two()
        mob = self.ready(a)
        self.report_hit(a, mob)
        self.drain(a)
        self.drain(b)
        T = time.monotonic()
        self.assertEqual(self.server._tick_monster_ai(T), 2)
        self.assertEqual(self.drain(a), [(0x2A, cmd(mob, WALK_L, 1))])
        self.assertEqual(self.drain(b), [(0x2A, cmd(mob, WALK_L, 2))])
        # the keep-alive: both again
        self.pin(a, mob, 1244, 714, T + 0.6)
        self.assertEqual(self.server._tick_monster_ai(T + 0.6), 2)
        self.assertEqual(self.drain(a), [(0x2A, cmd(mob, WALK_L, 1))])
        self.assertEqual(self.drain(b), [(0x2A, cmd(mob, WALK_L, 2))])
        # past the leash: the hand-back reaches both
        self.pin(a, mob, 1244 + self.server.config.MOB_LEASH_PX + 1, 714, T + 0.7)
        self.assertEqual(self.server._tick_monster_ai(T + 0.7), 2)
        self.assertEqual(self.drain(a), [(0x9E, hand_back(mob))])
        self.assertEqual(self.drain(b), [(0x9E, hand_back(mob))])
        self.assertEqual((mob.aggro_uid, mob.ai_owned, mob.ai_takers), (0, False, set()))

    def test_a_client_arriving_mid_chase_gets_the_current_word_at_once(self):
        a = self.enter()
        mob = self.ready(a)
        self.report_hit(a, mob)
        a.expect(0x2A)
        T = time.monotonic()
        self.assertEqual(self.server._tick_monster_ai(T), 1)
        self.assertEqual(self.drain(a), [(0x2A, cmd(mob, WALK_L, 1))])
        b = self.enter('admin', 'admin', 'Watcher')                        # its 0x1A has the chaser
        self.drain(a)
        self.assertIn(mob.uid, b.uids)
        sent_t = mob.ai_sent_t
        self.pin(a, mob, 1244, 714, T + 0.3)
        self.assertEqual(self.server._tick_monster_ai(T + 0.3), 1)         # B only: A's keep-alive is not due
        self.assertEqual(self.drain(b), [(0x2A, cmd(mob, WALK_L, 2))])
        a.expect_silence(0.05)
        self.assertEqual(mob.ai_sent_t, sent_t)                            # A's clock untouched
        self.pin(a, mob, 1244, 714, T + 0.6)
        self.assertEqual(self.server._tick_monster_ai(T + 0.6), 2)         # then both, in step

    def test_a_players_death_hands_his_mobs_back_on_every_client(self):
        a, b = self.two()
        mob = self.ready(a)
        self.report_hit(a, mob)
        self.drain(a)
        self.drain(b)
        self.server.damage_player(a.session, 9999, reason='test')
        death, back = a.expect(0x3E, 0x9E)
        self.assertEqual(back.payload, hand_back(mob))
        back, corpse = b.expect(0x9E, 0x29)                                # + A's own corpse
        self.assertEqual((back.payload, b.s2c(corpse)['uid']), (hand_back(mob), 1))
        self.assertEqual(mob.aggro_uid, 0)

    # -------------------------------------------------------- contact ---
    def test_contact_hurts_only_a_player_the_mob_is_after(self):
        a, b = self.two()
        mob = self.ready(a)
        self.report_hit(a, mob)                                            # it chases A now
        self.drain(a)
        self.drain(b)
        hp_a, hp_b = a.session['hp'], b.session['hp']
        # red and touchable on B's screen too, but B is not on its hate list: nothing
        self.touch(b, mob)
        self.assertEqual([op for op, _ in self.drain(a)], [0x1B])            # only B's 0x0D relay
        b.expect_silence(0.15)
        self.assertEqual(b.session['hp'], hp_b)
        # A is its target: A takes the Body_Atk hit - the client's formula (damage.py) for the
        # Lv1 Pupu's Body_Atk 3 on a Lv1 Novice with no armour (Def 0):
        # trunc(3 x 3/3 x 2 x 1/2) = 3
        self.touch(a, mob)
        dmg = 3
        self.assertEqual(a.s2c(a.expect(0x28, quiet=0.1)), {'hp': hp_a - dmg})
        self.drain(b)
        # once B has hit it too (on the hate list), B's touches hurt B
        self.report_hit(b, mob)
        self.drain(a)
        self.drain(b)
        self.assertIn(2, mob.hate)
        self.touch(b, mob)
        self.assertEqual(b.s2c(b.expect(0x28, quiet=0.1)), {'hp': hp_b - dmg})

    # ------------------------------------------------ position authority ---
    def test_the_chase_targets_reports_place_the_mob_and_the_others_do_not(self):
        a, b = self.two()
        mob = self.ready(a)
        self.report_hit(a, mob, at=(1200.0, 714.0))                        # unchased: A's report fixes it
        self.drain(b)
        a.expect(0x2A)
        self.assertEqual((mob.x, mob.aggro_uid), (1200.0, 1))
        self.report_hit(b, mob, at=(1400.0, 714.0))                        # B's copy is elsewhere
        self.assertEqual(b.expect(0x2A).payload, cmd(mob, AI.STOP, 2))
        relay, hit = a.expect(0x1B, 0x2A)
        self.assertEqual(mob.x, 1200.0)                                    # not B's point ...
        self.assertEqual(struct.unpack_from('<dd', hit.payload, 16), (1200.0, 714.0))  # ... A is synced to it
        # the dev driver's memory sample: only from the chase target's client
        self.assertEqual(self.server._track_driver_monsters(2, {mob.uid: (1300.0, 714.0)}), 0)
        self.assertEqual(self.server._track_driver_monsters(1, {mob.uid: (1250.0, 714.0)}), 1)
        self.assertEqual(mob.x, 1250.0)
        # a mob chasing nobody: any client's report places it
        m2 = self.mob(a, 2)
        self.report_hit(b, m2, at=(1700.0, 714.0))
        self.drain(a)
        self.drain(b)
        self.assertEqual((m2.x, m2.aggro_uid), (1700.0, 2))

    # --------------------------------------------------------------- DoT ---
    def test_a_dot_runs_on_after_its_caster_left_and_credits_nobody(self):
        a, b = self.two()
        mob = self.ready(a)
        rec, _ = D.apply(mob, SK.skill_def(POISON), time.monotonic() - 1.0, src=1, tick_damage=3)
        self.assertEqual(self.server._tick_debuffs(), 1)
        self.assertEqual(mob.hp, 497)
        self.assertEqual(self.drain(a), [(0x2A, cmd(mob, WALK_L, 1))])     # it turns on A, on both
        self.assertEqual(self.drain(b), [(0x2A, cmd(mob, WALK_L, 2))])
        self.portal(a, PORTAL_102_TO_101, 101)                             # A leaves; the DoT runs on
        self.drain(b)
        mob.hp = 1
        exp_a = self.char('test').get('exp', 0)
        self.server._tick_debuffs(rec['next_tick'])
        self.assertEqual([op for op, _ in self.drain(b)], [0x29])
        a.expect_silence(0.1)
        self.assertFalse(mob.alive)
        self.assertEqual(self.char('test').get('exp', 0), exp_a)

    # ------------------------------------------------------------ !mobs ---
    def test_the_dev_mobs_line_lists_the_shared_set(self):
        a, b = self.two()
        line = '!mobs'
        a.send_c2s(self.keys['chat'], {'msg_len': len(line), 'message': line})
        texts = [P.to_bytes(a.s2c(p)['text']).decode('cp949') for p in a.recv_until_quiet(0.2)
                 if p.opcode == 0x15]
        self.assertEqual(texts[0], 'map 102: 8 monster(s), 2 client(s) attached')
        self.assertEqual(len(texts), 9)
        self.assertTrue(all(t.endswith('held 2') for t in texts[1:]), texts)
        self.drain(b)

    # --------------------------------------------------------- retention ---
    def test_an_empty_map_keeps_its_monsters_then_discards_them(self):
        a = self.enter()
        uids = list(a.uids)
        mob = self.ready(a, 1, hp=1)
        self.report_hit(a, mob)
        self.drain(a)
        self.assertFalse(mob.alive)
        self.portal(a, PORTAL_102_TO_101, 101)
        inst = self.server.world.map(102)
        mons = inst.monsters
        self.assertTrue(mons.populated)
        self.assertEqual(mons.viewers, {})
        # back before the respawn: no free respawn - the dead one is not sent ...
        got = self.portal(a, PORTAL_101_TO_102, 102, mobs=7)
        self.assertEqual(got, [u for u in uids if u != mob.uid])
        now = time.monotonic()
        self.server.ticks.run_due(now + W.MOB_CORPSE_SECS + 0.5)
        a.expect_silence(0.1)                                              # its corpse was never here
        self.server.ticks.run_due(now + W.MOB_RESPAWN_SECS + 0.5)
        self.assertEqual([r['uid'] for r in a.s2c(a.expect(0x1A))['repeat[count]']], [mob.uid])
        # ... and a map left empty for MOB_MAP_KEEP_SECS is discarded
        self.portal(a, PORTAL_102_TO_101, 101)
        keep = self.server.config.MOB_MAP_KEEP_SECS
        t = inst.empty_since
        self.assertEqual(self.server._tick_monster_maps(t + keep - 1.0), 0)
        self.assertEqual(self.server._tick_monster_maps(t + keep + 1.0), 1)
        self.assertEqual((mons.populated, len(mons)), (False, 0))
        # the next arrival rebuilds it fresh, with the same uids
        self.assertEqual(self.portal(a, PORTAL_101_TO_102, 102, mobs=8), uids)
        self.assertIsNot(a.session['monsters'][mob.uid], mob)

    def test_keep_zero_discards_when_the_last_player_leaves(self):
        self.server.config = cfgmod.from_dict({'CLIENT_BUILD': self.build, **self.overrides,
                                                'MOB_MAP_KEEP_SECS': 0, 'GROUND_LOOT': False})
        a, b = self.two()
        mob = self.ready(a, 1, hp=1)
        self.report_hit(a, mob)
        self.drain(a)
        self.drain(b)
        timers = list(mob.timers)
        self.assertEqual(len(timers), 2)
        self.portal(a, PORTAL_102_TO_101, 101)                             # B is still there: kept
        self.assertTrue(self.server.world.map(102).monsters.populated)
        self.drain(b)
        self.portal(b, PORTAL_102_TO_101, 101)                             # the last one: discarded
        self.assertFalse(self.server.world.map(102).monsters.populated)
        self.assertTrue(all(t.cancelled for t in timers))
        self.drain(a)
        self.assertEqual(self.portal(a, PORTAL_101_TO_102, 102, mobs=8), a.uids)


class Shared2008(SharedFlows, SharedServer):
    build = B8


class Shared2009(SharedFlows, SharedServer):
    build = B9


# ------------------------------------------------------------ MOB_AGGRO false ---
class AggroOffFlows:
    overrides = {'MOB_AGGRO': False}

    def test_the_hit_relay_is_a_0x1B_node_when_nothing_drives_the_mob(self):
        a, b = self.two()
        mob = self.ready(a)
        self.report_hit(a, mob)
        self.assertEqual(a.expect(0x2A).payload, cmd(mob, AI.STOP, 1))     # the release: A only
        relay, hit = b.expect(0x1B, 0x1B)
        self.assertEqual(b.s2c(relay)['uid'], 1)
        self.assertEqual(struct.unpack('<IIIII', hit.payload),
                         (mob.uid, W.GameServer.MOB_HIT_RELAY_HOLD_MS, HIT, 0, 1))
        self.assertEqual(b.s2c(hit), {'uid': mob.uid, 'hold_ms': W.GameServer.MOB_HIT_RELAY_HOLD_MS,
                                      'state_blob': blob(HIT), 'target_uid': 1})
        self.assertEqual(self.server._tick_monster_ai(time.monotonic()), 0)


class AggroOff2008(AggroOffFlows, SharedServer):
    build = B8


class AggroOff2009(AggroOffFlows, SharedServer):
    build = B9


# ------------------------------------------------------------ the 0x1B fallback ---
class Fallback1BFlows:
    overrides = {'MOB_AI_COMMAND': '1B'}

    def test_a_late_client_is_taken_over_with_0x2A_before_its_first_node(self):
        a = self.enter()
        mob = self.ready(a)
        self.report_hit(a, mob)
        a.expect(0x2A)
        T = time.monotonic()
        self.pin(a, mob, 1244, 714, T)
        self.assertEqual(self.server._tick_monster_ai(T), 1)
        node = struct.pack('<II', mob.uid, W.GameServer.MOB_1B_HOLD_MS) + blob(WALK_L)
        self.assertEqual(self.drain(a), [(0x1B, node)])
        b = self.enter('admin', 'admin', 'Watcher')
        self.drain(a)
        self.pin(a, mob, 1244, 714, T + 0.28)
        self.assertEqual(self.server._tick_monster_ai(T + 0.28), 3)
        self.assertEqual(self.drain(b), [(0x2A, cmd(mob, AI.STOP, 2)), (0x1B, node)])
        self.assertEqual(self.drain(a), [(0x1B, node)])


class Fallback1B2008(Fallback1BFlows, SharedServer):
    build = B8


class Fallback1B2009(Fallback1BFlows, SharedServer):
    build = B9


class Config(unittest.TestCase):
    def test_keep_secs_default_and_validation(self):
        self.assertEqual(cfgmod.defaults().MOB_MAP_KEEP_SECS, 300.0)
        self.assertEqual(cfgmod.from_dict({'MOB_MAP_KEEP_SECS': 0}).MOB_MAP_KEEP_SECS, 0)
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.from_dict({'MOB_MAP_KEEP_SECS': -1})


if __name__ == '__main__':
    unittest.main(verbosity=2)
