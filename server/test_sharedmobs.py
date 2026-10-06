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
- desync fix M1 (POSITION_SYNC_RE 2026-09-28): the hit relay carries the attacker's facing
  (facing2 bits 20-21: his +0x949 as his own words turned it - a key in a hurt turns
  nothing - or as his 0x07 spawn record left it (left); with no model his cast's, his held
  key, his tail's target_dx) and the hurt (hi),
  stays 33 bytes with action 7 and no reaction for a swing, never reaches the attacker (he
  gets only his 16-byte release), slides the server's point 2 ticks, and no chase word goes
  out before the hit + the stun + 0.12 s (a combo extends it; a client arriving in the hurt
  gets only the STOP top-up); MOB_HIT_RELAY_KNOCKBACK false = the old bytes;
- per-swing hurt (HIT_STUN_PER_SWING_RE 2026-09-28, hitstun.py) from the attacker's own 0x0D
  words, byte-exact: a basic swing from standing 490 (gate 0.55 s), a dash attack 760
  (0.82 s), Ice Spear 1020 in the 34-byte case-9 form with flag 1 (M3; 2020 ms of stun with
  the ice element: its chase word goes 0.6 s after the hit, inside the stun, on its own
  timer - live 2b: the 2.14 s gate froze both copies 3.3-3.5 s), a strong attack 1020 with
  flag 0 (1.14 s); a swing with no
  words counted gets MOB_HIT_RELAY_HURT_MS 490 (0.55 s), an event-9 hit the state-1
  default 1020 (1.14 s); a skill hit slides the server's point 4 ticks;
  MOB_HIT_RELAY_SKILL_VARIANT false = the old flinch in place, still gated;
- the attack key held through an Ice Spear cast or a strong attack (motion-1 words the
  client ignores in state 1) changes nothing: Ice Spear is still 1020 with flag 1, the
  strong attack 1020; a Crescent Slash hit on a mob BEHIND the caster slides it in his
  facing at the cast (19 wind ticks), not toward the side it is on;
- the cast-time gate (MOB_HIT_CAST_GATE): an Ice Spear the server's box lands takes the mob
  over standing (the STOP top-up, no chase word) for L 1290 + 0.12 s, the report then holds
  it 0.6 s from the hit (ice), no report lets it run out; repeated casts (one player or two)
  hold it only until the LAST cast + 1.41 s, a cast after a report gives the max of the
  two gates - nothing adds up; a guarded hit (4) and a trap catch (0xC) keep the old
  relay (flinch in place, facing2 0 / hi 0, no slide) and no gate;
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
import hpmp  # noqa: E402
import mobai as AI  # noqa: E402
import packets as P  # noqa: E402
import progression  # noqa: E402
import skills as SK  # noqa: E402
import world as WM  # noqa: E402
from test_hitstun import LIVE_2B  # noqa: E402  (A's own words of live session 2b)

W = F.import_server()
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = cfgmod.BUILD_2008, cfgmod.BUILD_2009
DIRS = {B8: cfgmod.DEFAULTS['CLIENT_DIR'], B9: cfgmod.DEFAULTS['CLIENT_DIR_2009']}
HAVE = {b: os.path.exists(os.path.join(HERE, d, 'hs', 'windslayer.hii')) for b, d in DIRS.items()}

KEYS = {
    B8: {'login': '0x44D8BF/0x01', 'enter': '0x42F904/0x2B', 'move': '0x42CE94/0x0D',
         'chat': '0x445CA7/0x03', 'portal': '0x42F76B/0x7E', 'skill': '0x44C239/0x15'},
    B9: {'login': '0x451CE5/0x01', 'enter': '0x4315D7/0x2B', 'move': '0x42E704/0x0D',
         'chat': '0x44790E/0x03', 'portal': '0x431284/0x7E', 'skill': '0x44FC61/0x15'},
}
ENTRY = {B8: (0x03, 0x07, 0x15, 0x28, 0x44), B9: (0x03, 0x07, 0x15, 0x65, 0x28, 0x44)}
PORTAL_101_TO_102, PORTAL_102_TO_101 = 23, 31
POISON, BOOBY_TRAP = 391, 2609
WALK_L, WALK_R = 0x01, 0x02
HIT = W.GameServer.MOB_HIT_RELAY_EVENT << 12
ICE_SPEAR, WOODEN_BLADE = 260, 70
CRESCENT_SLASH = 2224           # Warrior tier 1, variant 6, wind; its box reaches 300 px behind
# The attacker's C2S 0x0D words (live session 2, HIT_STUN_PER_SWING_RE): the swing press
# (motion 1, +0x8D0 bit 21), idle, a dash right (motion 6), the dash attack right (motion 1),
# the strong attack (motion 5), Ice Spear's cast words (motion 7, variant 1).
SWING, IDLE, DASH_R, DASH_ATTACK_R, STRONG, CAST_ICE = 0x200004, 0x200000, 0x1A, 0x06, 0x200014, 0x20003C
CAST_CRESCENT = 0x200000 | (6 << 5) | (7 << 2)                  # motion 7, variant 6

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

    def hero(self, cls=1, level=40, weapon=WOODEN_BLADE, skills=(ICE_SPEAR,), user='test'):
        """Shape TestHero (or `user`'s character) in the store before he enters: class, level,
        a class weapon in grid slot 5 (weapon type +0x11A: Wooden Blade = 1) and the learned
        list, full MP."""
        with self.server.store.lock:
            ch = self.char(user)
            ch['class'] = cls
            ch['exp'] = progression.exp_for_level(level)
            ch['equipped'] = {5: {'id': weapon, 'w': [0] * 6}} if weapon else {}
            ch['skills'] = list(skills)
            d = hpmp.derive({}, ch)
            ch['hp'], ch['mp'] = d.max_hp, d.max_mp
        self.server.store.mark_dirty('test setup')

    def face(self, c, facing):
        """`c`'s player turned to `facing` as a direction word standing leaves it: his +0x949
        model (hitstun, 'his words') and his held key (session['facing']). A client spawns
        facing left (the 0x07 record's direction byte, _map_transfer seeds the model), and a
        swing hits in front: these flows place him beside the mob he hits, so they turn him
        first (test_the_hit_relay_knockback_follows_the_attackers_facing sends the words)."""
        f949 = {W.combat.FACING_LEFT: 2, W.combat.FACING_RIGHT: 6}[facing]
        with self.server._combat(c.session):
            c.session['facing'] = facing
            track = c.session['hitstun']
            track['facing'] = track['report_facing'] = f949
            track['facing_from'] = track['report_from'] = 'words'

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

    def report_hit(self, c, mob, at=None, elapsed=30, tae=7):
        """The 61 B C2S 0x0D the client sends after its own hit detection caught a swing:
        interact event 7, the victim in the tail at `at` (default: the server's x/y),
        `elapsed` its logic_elapsed_ms."""
        px, py = c.session['pos']
        vx, vy = at if at is not None else (mob.x, mob.y)
        self.move(c, W.HIT_REPORT_EVENT << 16, target_uid=mob.uid, pos_x=float(px), pos_y=float(py),
                  target_dx=float(vx - px), target_dy=float(vy - py), flag_8db=0, flag_8e7=0,
                  timer_dac=0, target_action_event=tae, logic_elapsed_ms=elapsed)

    def touch(self, c, mob, action=6):
        """The victim's own client reports a monster bumping into him (action event 6)."""
        with self.server._combat_lock(c.session):
            c.session.pop('contact_t', None)                     # the contact and swing slots
            c.session.pop('swing_t', None)
        self.move(c, action << 12, event_source_uid=mob.uid)

    def drain(self, c, quiet=0.1):
        return [(p.opcode, p.payload) for p in c.recv_until_quiet(quiet)]

    def pin(self, c, mob, x, y, now):
        with self.server._combat(c.session):
            mob.x, mob.y, mob.fix_t = float(x), float(y), now

    @staticmethod
    def recovered(mob):
        """The first moment a chase word may follow the last client-caught hit on `mob`
        (desync fix M1: MOB_HIT_RECOVER_SECS after it, Monster.ai_recover_until)."""
        return max(time.monotonic(), mob.ai_recover_until)

    @staticmethod
    def knock(mob, ticks=2):
        """The server's estimate of the hurt slide (desync fix M1 / P3): 2 ticks of 30 ms at
        the template's walk speed (Pupu / Ssiyo 1e7 / 120000 px/s -> 5 px); 4 for a skill
        hit (M3)."""
        return ticks * 0.03 * mob.walk_px_s

    def reported(self, a, b, send):
        """Run `send` (a hit report of A's) and return (t0, t1, B's hit relay, the log lines):
        A gets exactly his 16-byte release, B A's 0x0D relay then the hit relay; t0 / t1
        bracket the server's hit time."""
        with self.assertLogs('WS', logging.INFO) as logs:
            t0 = time.monotonic()
            send()
            release = a.expect(0x2A)
            relay, hit = b.expect(0x1B, 0x2A)
            t1 = time.monotonic()
        self.assertEqual(b.s2c(relay)['uid'], 1)
        return t0, t1, release.payload, hit.payload, logs.output

    def assert_gate(self, a, b, mob, t0, t1, secs, word=WALK_L, xy=(1244, 714)):
        """The chase gate: `secs` after the hit (between t0 and t1), then no earlier - the
        tick just before it sends nothing, the tick at it sends the chase word to A and B."""
        gate = mob.ai_recover_until
        self.assertTrue(t0 + secs - 1e-6 <= gate <= t1 + secs + 1e-6, (gate - t0, gate - t1, secs))
        self.pin(a, mob, *xy, gate - 0.01)
        self.assertEqual(self.server._tick_monster_ai(gate - 0.01), 0)
        self.pin(a, mob, *xy, gate)
        self.assertEqual(self.server._tick_monster_ai(gate), 2)
        self.assertEqual(self.drain(a), [(0x2A, cmd(mob, word, 1))])
        self.assertEqual(self.drain(b), [(0x2A, cmd(mob, word, 2))])


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
        self.face(a, W.combat.FACING_RIGHT)                                # both face their mob
        self.face(b, W.combat.FACING_RIGHT)
        before = {m.uid: m.hp for m in a.session['monsters'].values()}
        x, y = m1.x, m1.y
        self.report_hit(a, m1)
        # the attacker: exactly the 16-byte release aimed at himself
        self.assertEqual(a.expect(0x2A).payload, cmd(m1, AI.STOP, 1))
        # the other client: A's 0x0D as a 0x1B node on A, then the hit on the mob - the full
        # 0x2A {mob, action 7 | facing2 right (A at 1000 hits the mob at 1244), hurt 490 (no
        # swing words counted: MOB_HIT_RELAY_HURT_MS), target = A, the hit point, airborne 0}
        # (desync fix M1), never back to A
        relay, hit = b.expect(0x1B, 0x2A)
        self.assertEqual(b.s2c(relay)['uid'], 1)
        self.assertEqual(len(hit.payload), 33)
        self.assertEqual(struct.unpack('<IIIIddB', hit.payload), (m1.uid, 0x207000, 490, 1, x, y, 0))
        # the server's point slid with every copy: 2 ticks right at the Pupu's speed
        self.assertAlmostEqual(m1.x, x + self.knock(m1))
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
        self.assertEqual(struct.unpack_from('<IIII', hit.payload), (m2.uid, 0x207000, 490, 2))
        self.assertEqual((m1.hp, m2.aggro_uid, m2.hate), (hp1, 2, {2: 50 - m2.hp}))

    # ---------------------------------------------- desync fix M1: knockback ---
    def test_the_hit_relay_knockback_follows_the_attackers_facing(self):
        """The watchers' copy slides in the attacker's facing +0x949, which his own words turn
        (hitstun.report_facing): a direction key counts only while he stands, walks or is in
        the air - not in a hurt, a dash or a swing. Before a word turned him: the facing his
        0x07 spawn record gave him (left: read into +0x949 at 0x453B80) - the mob here stands
        on his RIGHT (tail dx +30) and is still knocked left, as on his copy. With no model
        at all (no 0x07 sent to the session): his held key, else the side of the mob in his
        hit tail (target_dx = mob - attacker, |dx| >= 8). The log line names the choice and
        both values (checked live against A's +0x95B)."""
        a, b = self.two()
        self.assertEqual((a.session['hitstun']['facing'], a.session['hitstun']['map']), (2, 102))
        px = a.session['pos'][0]
        mob = self.ready(a, xy=(px + 30.0, 714.0))
        with self.assertLogs('WS', logging.INFO) as logs:
            self.report_hit(a, mob)                                        # tail dx +30: right
            self.assertEqual(a.expect(0x2A).payload, cmd(mob, AI.STOP, 1))
            relay, hit = b.expect(0x1B, 0x2A)
        self.assertEqual(struct.unpack_from('<IIII', hit.payload), (mob.uid, 0x107000, 490, 1))
        line = next(t for t in logs.output if '[ATK] hit on' in t)
        self.assertIn('0x2A lo 0x00107000 hi 490, synced to', line)
        self.assertIn('knock left, hurt 490 (facing from his +0x949 (left, his 0x07 spawn record): tail dx '
                      '+30.0, held none', line)
        self.assertIn('no swing words counted (last action: none): MOB_HIT_RELAY_HURT_MS 490, stun 430, '
                      'slide 2 ticks; estimate -5.0 px -> (1025,714); chase held 0.55 s)', line)
        # no model (a session no 0x07 went to): the tail's side
        with self.server._combat(a.session):
            a.session.pop('hitstun')
        self.drain(a)
        self.drain(b)
        with self.assertLogs('WS', logging.INFO) as logs:
            self.report_hit(a, mob)
            a.expect(0x2A)
            relay, hit = b.expect(0x1B, 0x2A)
        self.assertEqual(struct.unpack_from('<IIII', hit.payload), (mob.uid, 0x207000, 490, 1))
        line = next(t for t in logs.output if '[ATK] hit on' in t)
        self.assertIn('knock right, hurt 490 (facing from tail dx: tail dx +25.0, held none)', line)
        self.drain(a)
        self.drain(b)
        # A walked RIGHT standing (and let go): his words turned him, the mob 3 px behind him
        self.move(a, WALK_R)
        self.move(a, 0)
        self.assertEqual([op for op, _ in self.drain(b)], [0x1B, 0x1B])
        self.assertTrue(a.wait_session(lambda s: s.get('facing') == W.combat.FACING_RIGHT))
        with self.assertLogs('WS', logging.INFO) as logs:
            self.report_hit(a, mob, at=(a.session['pos'][0] - 3.0, 714.0))
            a.expect(0x2A)
            relay, hit = b.expect(0x1B, 0x2A)
        self.assertEqual(struct.unpack_from('<IIII', hit.payload), (mob.uid, 0x207000, 490, 1))
        line = next(t for t in logs.output if '[ATK] hit on' in t)
        self.assertIn('knock right, hurt 490 (facing from his +0x949 (right, his words): tail dx -3.0, '
                      'held right)', line)
        # Live 2b c1 / f1 77.42: the mob hurts him (contact, 250 ms), he presses LEFT in the
        # hurt (no turn), then the attack key held through it swings when it ends - the mob
        # 30 px behind him on the left: the held key and the tail both say left, he faces right.
        self.drain(a)
        self.move(a, 6 << 12, event_source_uid=mob.uid, state_hi=250, logic_elapsed_ms=400)
        self.move(a, WALK_L, state_hi=250, logic_elapsed_ms=60)
        self.move(a, 0, state_hi=250, logic_elapsed_ms=30)
        self.move(a, SWING, state_hi=250, logic_elapsed_ms=30)
        self.move(a, SWING, state_hi=250, logic_elapsed_ms=210)               # out at 270
        self.drain(b)
        self.assertTrue(a.wait_session(lambda s: s.get('facing') == W.combat.FACING_LEFT))
        x = a.session['pos'][0]
        with self.assertLogs('WS', logging.INFO) as logs:
            self.report_hit(a, mob, at=(x - 30.0, 714.0), elapsed=90)
            relay, hit = b.expect(0x1B, 0x2A)
        self.assertEqual(struct.unpack_from('<IIII', hit.payload), (mob.uid, 0x207000, 490, 1))
        line = next(t for t in logs.output if '[ATK] hit on' in t)
        self.assertIn('knock right, hurt 490 (facing from his +0x949 (right, his words): tail dx -30.0, '
                      'held left); swing: L 610 - E 120 (weapon 0), started at the end of his own hurt '
                      '(contact, hi 250), stun 430', line)

    def test_every_0x07_restarts_the_facing_model_at_its_record(self):
        """Every S2C 0x07 re-creates the local player with +0x949 = the record's direction byte
        (0x453B80; the local record's idle block: 2, left): the server's model restarts there
        (_map_transfer), so a hit before his first turn on the new map knocks left although
        his last key on the old map was right - not that key (session['facing'])."""
        a, b = self.two()
        track = a.session['hitstun']
        self.assertEqual((track['facing'], track['facing_from'], track['map']), (2, 'spawn', 102))
        self.move(a, WALK_R)
        self.move(a, 0)
        self.assertTrue(a.wait_session(lambda s: s['hitstun']['facing'] == 6))
        self.portal(a, PORTAL_102_TO_101, 101)
        track = a.session['hitstun']
        self.assertEqual((track['facing'], track['facing_from'], track['map'], track['anchor'], track['t']),
                         (2, 'spawn', 101, None, 0))
        self.assertEqual(a.session['facing'], W.combat.FACING_RIGHT)            # the old map's key
        mob = self.mob(b, 1)
        facing, why = self.server._hit_knock_facing(a.session, mob)
        self.assertEqual((facing, why), (W.combat.FACING_LEFT, 'facing from his +0x949 (left, his 0x07 '
                                                               'spawn record): tail dx none, held right'))

    def test_the_live_k2_words_relay_the_attackers_copy(self):
        """Live session 2b k2 (test_hitstun.LIVE_2B), A's own words through the server: a
        contact hurts him, the attack key pressed in the hurt swings at its end, a report in
        the same packet as the next contact is still that combo, the next swing starts over.
        B's relays carry what A's copy had - 490 / 500 / 490, knocked left (his facing; the
        third mob stood behind him) - where 2e2f05e sent 280 / 290 / 760 and knocked the
        third right (a lasting 15 px gap)."""
        a, b = self.two()
        px, py = a.session['pos']
        mobs = {1: self.ready(a, 1, xy=(px - 60.0, py)), 0: self.mob(a, 0)}
        with self.server._combat(a.session):
            mobs[0].x, mobs[0].y = px + 26.25, py
            mobs[0].hp = mobs[0].max_hp = 500
        self.move(a, WALK_L)                                               # he faces left
        self.move(a, 0)
        self.drain(a)
        self.drain(b)
        for w in LIVE_2B['k2']['words']:
            lo, el, hi = w[:3]
            extra = {'event_source_uid': mobs[1].uid} if (lo >> 12) & 0xF else {}
            if len(w) > 3:
                extra.update(target_uid=mobs[w[3]].uid, pos_x=float(px), pos_y=float(py), target_dx=float(w[4]),
                             target_dy=0.0, flag_8db=0, flag_8e7=1, timer_dac=0, target_action_event=w[5])
            self.move(a, lo, logic_elapsed_ms=el, state_hi=hi, **extra)
        relays = [struct.unpack_from('<IIII', p) for op, p in self.drain(b, 0.3) if op == 0x2A and len(p) == 33]
        self.assertEqual([(m, lo, hi) for m, lo, hi, _ in relays],
                         [(mobs[1].uid, 0x107000, 490), (mobs[1].uid, 0x107000, 500), (mobs[0].uid, 0x107000, 490)])

    def test_knockback_relay_off_restores_the_old_bytes(self):
        self.server.config['MOB_HIT_RELAY_KNOCKBACK'] = False
        a, b = self.two()
        mob = self.ready(a)
        self.report_hit(a, mob)
        self.assertEqual(a.expect(0x2A).payload, cmd(mob, AI.STOP, 1))
        relay, hit = b.expect(0x1B, 0x2A)
        self.assertEqual(struct.unpack('<IIIIddB', hit.payload), (mob.uid, HIT, 0, 1, 1244.0, 714.0, 0))
        self.assertEqual(mob.x, 1244.0)                                    # no slide in the estimate

    def test_the_attacker_gets_only_the_release(self):
        """Only target == receiver clears the hit-lock (0x459F04): a 33-byte 0x2A to the
        attacker would flush his copy's queue and freeze the mob in hurt. Every hit: exactly
        one 16-byte 0x2A to A, the 33-byte relay to B only."""
        a, b = self.two()
        mob = self.ready(a)
        for k in range(3):
            with self.subTest(hit=k):
                self.report_hit(a, mob)
                self.assertEqual(self.drain(a), [(0x2A, cmd(mob, AI.STOP, 1))])
                got = self.drain(b)
                self.assertEqual([op for op, _ in got], [0x1B, 0x2A])
                self.assertEqual(len(got[1][1]), 33)
                lo = struct.unpack_from('<I', got[1][1], 4)[0]
                self.assertEqual(((lo >> 12) & 0xF, (lo >> 16) & 0xF), (7, 0))   # action 7, no reaction

    def test_the_chase_waits_for_the_hurt(self):
        """No chase word until the stun (490 - 60 ms) + MOB_HIT_RECOVER_MARGIN_SECS (0.12) after
        the hit - above the MOB_HIT_RECOVER_SECS floor 0.45: then it reaches A and B in the same
        tick, both copies standing after their hurt."""
        a, b = self.two()
        mob = self.ready(a)
        self.report_hit(a, mob)
        self.drain(a)
        self.drain(b)
        self.assertEqual((self.server.config.MOB_HIT_RECOVER_SECS, self.server.config.MOB_HIT_RECOVER_MARGIN_SECS),
                         (0.45, 0.12))
        t = mob.ai_recover_until - 0.55                                    # the hit
        self.pin(a, mob, 1244, 714, t + 0.54)
        self.assertEqual(self.server._tick_monster_ai(t + 0.54), 0)
        with self.assertLogs('WS', logging.INFO) as logs:
            self.assertEqual(self.server._tick_monster_ai(t + 0.56), 2)
        self.assertEqual(self.drain(a), [(0x2A, cmd(mob, WALK_L, 1))])
        self.assertEqual(self.drain(b), [(0x2A, cmd(mob, WALK_L, 2))])
        self.assertTrue(any('-> walk left (0x2A lo 01 to 2 client(s))' in t for t in logs.output), logs.output)

    def test_a_combo_extends_the_recovery(self):
        a, b = self.two()
        mob = self.ready(a)
        self.report_hit(a, mob)
        self.drain(a)
        self.drain(b)
        first = mob.ai_recover_until
        self.report_hit(a, mob)                                            # the combo's next swing
        self.drain(a)
        self.drain(b)
        second = mob.ai_recover_until
        self.assertGreater(second, first + 0.05)
        self.pin(a, mob, 1244, 714, first + 0.01)
        self.assertEqual(self.server._tick_monster_ai(first + 0.01), 0)    # the second hurt runs on
        self.pin(a, mob, 1244, 714, second)
        self.assertEqual(self.server._tick_monster_ai(second), 2)
        self.assertEqual(self.drain(a), [(0x2A, cmd(mob, WALK_L, 1))])
        self.assertEqual(self.drain(b), [(0x2A, cmd(mob, WALK_L, 2))])

    def test_a_client_arriving_in_the_hurt_gets_only_the_stop_top_up(self):
        a = self.enter()
        mob = self.ready(a)
        self.report_hit(a, mob)
        a.expect(0x2A)
        t = mob.ai_recover_until
        b = self.enter('admin', 'admin', 'Watcher')                        # its 0x1A has the mob
        self.drain(a)
        self.pin(a, mob, 1244, 714, t - 0.1)
        self.assertEqual(self.server._tick_monster_ai(t - 0.1), 1)         # B only: taken over, standing
        self.assertEqual(self.drain(b), [(0x2A, cmd(mob, AI.STOP, 2))])
        a.expect_silence(0.05)
        self.assertEqual(self.server._tick_monster_ai(t - 0.05), 0)        # once
        self.pin(a, mob, 1244, 714, t)
        self.assertEqual(self.server._tick_monster_ai(t), 2)               # the chase: both, in step
        self.assertEqual(self.drain(a), [(0x2A, cmd(mob, WALK_L, 1))])
        self.assertEqual(self.drain(b), [(0x2A, cmd(mob, WALK_L, 2))])

    # ------------- desync fix M1 review: guarded hits and trap catches flinch in place ---
    def catch_report(self, c, mob, event, tae=9, held=0, **extra):
        """A C2S 0x0D whose interact event is NOT the basic swing's 7: a strong attack or
        skill hit (9), a guarded hit (4) or a trap catch (0xC, with item_id), the victim in
        the tail at the server's x/y, the tail's target_action_event `tae`; `held` the
        input bits of the same word (SWING: the attack key is down)."""
        px, py = c.session['pos']
        self.move(c, (event << 16) | held, target_uid=mob.uid, pos_x=float(px), pos_y=float(py),
                  target_dx=float(mob.x - px), target_dy=float(mob.y - py), flag_8db=0, flag_8e7=0,
                  timer_dac=0, target_action_event=tae, **extra)

    def assert_flinch_in_place(self, a, b, mob, x, y):
        """The relay from before M1 for a guarded hit or a trap catch: A gets only his
        16-byte release; B gets A's 0x0D (0x1B), then the 33-byte 0x2A with action 7, facing2
        0, hi 0, at the server's point - which does not slide - and no gate: the chase word
        goes out on the next 'monster-ai' tick, to both clients."""
        self.assertEqual(self.drain(a), [(0x2A, cmd(mob, AI.STOP, 1))])
        got = self.drain(b)
        self.assertEqual([op for op, _ in got], [0x1B, 0x2A])
        self.assertEqual(struct.unpack('<IIIIddB', got[1][1]), (mob.uid, HIT, 0, 1, x, y, 0))
        self.assertEqual((mob.x, mob.y), (x, y))
        self.assertEqual(mob.ai_recover_until, 0.0)
        self.assertLess(mob.hp, 500)
        T = time.monotonic()
        self.pin(a, mob, x, y, T)
        with self.assertLogs('WS', logging.INFO) as logs:
            self.assertEqual(self.server._tick_monster_ai(T), 2)
        self.assertEqual(self.drain(a), [(0x2A, cmd(mob, WALK_L, 1))])
        self.assertEqual(self.drain(b), [(0x2A, cmd(mob, WALK_L, 2))])
        self.assertTrue(any('-> walk left (0x2A lo 01 to 2 client(s))' in t for t in logs.output), logs.output)

    # ------------------------- per-swing hurt (HIT_STUN_PER_SWING_RE, hitstun.py) ---
    def test_a_basic_swing_from_standing_hurts_490_and_holds_the_chase_0_55_s(self):
        """Live c01: the swing words, idle 60 ms later, the report 90 ms after that: E 120,
        hurt 610 - 120 = 490 (A's +0x9E4, 10/10), stun 430, gate 0.43 + 0.12 s."""
        a, b = self.two()
        mob = self.ready(a)
        self.face(a, W.combat.FACING_RIGHT)
        self.move(a, SWING, logic_elapsed_ms=400)
        self.move(a, IDLE, logic_elapsed_ms=60)
        self.assertEqual([op for op, _ in self.drain(b)], [0x1B, 0x1B])
        t0, t1, release, hit, logs = self.reported(a, b, lambda: self.report_hit(a, mob, elapsed=90))
        self.assertEqual(release, cmd(mob, AI.STOP, 1))
        self.assertEqual(hit, bytes.fromhex('01 00 0F 00 00 70 20 00 EA 01 00 00 01 00 00 00 '
                                            '00 00 00 00 00 70 93 40 00 00 00 00 00 50 86 40 00'))
        self.assertEqual(struct.unpack('<IIIIddB', hit), (mob.uid, 0x207000, 490, 1, 1244.0, 714.0, 0))
        self.assertAlmostEqual(mob.x, 1244.0 + self.knock(mob))
        line = next(t for t in logs if '[ATK] hit on' in t)
        self.assertIn('(0x2A lo 0x00207000 hi 490, synced to (1244,714), action 7; knock right, hurt 490 '
                      '(facing from his +0x949 (right, his words): tail dx +244.0, held right); swing: L 610 - '
                      'E 120 (weapon 0), '
                      'stun 430, slide 2 ticks; estimate +5.0 px -> (1249,714); chase held 0.55 s)', line)
        self.assert_gate(a, b, mob, t0, t1, 0.55)

    def test_a_dash_attack_hurts_760_and_holds_the_chase_0_82_s(self):
        """Live d-series: the attack 120 ms into the dash (+0xE9C preset 1380, stage 2), the
        report one tick later: hurt 2140 - 1380 = 760, stun 700, gate 0.82 s - the 0.45 s gate
        of session 2 let the chase in 250 ms early (the 18-30 px offset)."""
        a, b = self.two()
        mob = self.ready(a)
        self.move(a, DASH_R, logic_elapsed_ms=500)
        self.move(a, DASH_ATTACK_R, logic_elapsed_ms=120)
        self.assertEqual([op for op, _ in self.drain(b)], [0x1B, 0x1B])
        t0, t1, release, hit, logs = self.reported(a, b, lambda: self.report_hit(a, mob, elapsed=30))
        self.assertEqual(release, cmd(mob, AI.STOP, 1))
        self.assertEqual(hit, bytes.fromhex('01 00 0F 00 00 70 20 00 F8 02 00 00 01 00 00 00 '
                                            '00 00 00 00 00 70 93 40 00 00 00 00 00 50 86 40 00'))
        line = next(t for t in logs if '[ATK] hit on' in t)
        self.assertIn('hurt 760 (facing from his +0x949 (right, his words): tail dx +', line)
        self.assertIn('dash attack: L 2140 - E 1380 (weapon 0), stun 700, slide 2 ticks', line)
        self.assertIn('chase held 0.82 s', line)
        self.assert_gate(a, b, mob, t0, t1, 0.82)

    def test_an_ice_spear_hit_is_the_case_9_relay_and_sends_the_chase_at_0_6_s(self):
        """Live s02, with the cast: the server's box lands the Ice Spear and takes the mob over
        STANDING - the 16-byte STOP to both, no chase word (it went out at cast time before) -
        for L 1290 + 0.12 s; the cast words, idle 60 ms later, the report 240 ms after that:
        E 270, hurt 1020 in the 34-byte case-9 form with the cast variant 1 as its flag (the
        watcher's copy then adds the ice 1000 ms itself), 4-tick slide of the server's point,
        the chase word 0.6 s after the hit (MOB_HIT_ICE_CHASE_SECS): before both copies'
        +0xE9C stalls at the end of the 960 ms node hold, which stretched the 2020 ms stun to
        3.3-3.5 s with the 2.14 s gate (live 2b). A gets only his 16-byte release."""
        self.hero()
        a, b = self.two()
        mob = self.ready(a, xy=(1244.0, 714.0))
        with self.server._combat(a.session):
            a.session['pos'] = (1200.0, 714.0)
        self.face(a, W.combat.FACING_RIGHT)
        with self.assertLogs('WS', logging.INFO) as logs:
            c0 = time.monotonic()
            a.send_c2s(self.keys['skill'], {'skill_id': ICE_SPEAR})
            use, mp, take = a.expect(0x25, 0x44, 0x2A)
            c1 = time.monotonic()
        self.assertEqual(take.payload, cmd(mob, AI.STOP, 1))               # taken over, standing
        self.assertEqual(self.drain(b), [(0x2A, cmd(mob, AI.STOP, 2))])
        self.assertLess(mob.hp, 500)
        line = next(t for t in logs.output if 'cs-skill-damage' in t)
        self.assertIn('; chase held 1.41 s from the cast (L 1290: weapon 1, variant 1)', line)
        self.assertFalse(any('-> walk' in t for t in logs.output), logs.output)
        cast_gate = mob.ai_recover_until
        self.assertTrue(c0 + 1.41 - 1e-6 <= cast_gate <= c1 + 1.41 + 1e-6)
        self.assertEqual(self.server._tick_monster_ai(cast_gate - 0.01), 0)  # nothing at cast time
        self.move(a, CAST_ICE, logic_elapsed_ms=40)
        self.move(a, IDLE, logic_elapsed_ms=60)
        self.assertEqual([op for op, _ in self.drain(b)], [0x1B, 0x1B])
        x = mob.x
        t0, t1, release, hit, logs = self.reported(
            a, b, lambda: self.catch_report(a, mob, 9, tae=9, logic_elapsed_ms=240))
        self.assertEqual(release, cmd(mob, AI.STOP, 1))
        a.expect_silence(0.05)
        self.assertEqual(len(hit), 34)
        self.assertEqual(struct.unpack('<IIIIBddB', hit), (mob.uid, 0x209000, 1020, 1, 1, x, 714.0, 0))
        self.assertEqual(hit[:17], bytes.fromhex('01 00 0F 00 00 90 20 00 FC 03 00 00 01 00 00 00 01'))
        self.assertAlmostEqual(mob.x, x + self.knock(mob, 4))
        line = next(t for t in logs if '[ATK] hit on' in t)
        self.assertIn('(0x2A lo 0x00209000 hi 1020 flag 1, synced to', line)
        self.assertIn('action 9; knock right, hurt 1020', line)
        self.assertIn('skill v1: L 1290 - E 270 (weapon 1), stun 2020 (ice), slide 4 ticks', line)
        self.assertIn("chase held 0.60 s (ice: the word goes inside the stun, before the copies' +0xE9C "
                      "stalls at the 0x2A hold's 960 ms; they stand at 2020 ms))", line)
        self.assert_gate(a, b, mob, t0, t1, 0.6)

    def test_the_cast_gate_runs_out_without_a_report(self):
        """The client never reports a catch (its view of the mob differs): the cast-time gate
        just runs out after L + 0.12 s, and MOB_HIT_CAST_GATE false sends the chase word at
        the cast, as before."""
        self.hero()
        a, b = self.two()
        mob = self.ready(a, xy=(1244.0, 714.0))
        with self.server._combat(a.session):
            a.session['pos'] = (1200.0, 714.0)
        self.face(a, W.combat.FACING_RIGHT)
        c0 = time.monotonic()
        a.send_c2s(self.keys['skill'], {'skill_id': ICE_SPEAR})
        a.expect(0x25, 0x44, 0x2A)
        c1 = time.monotonic()
        self.drain(b)
        self.assert_gate(a, b, mob, c0, c1, 1.41, word=WALK_L)
        self.server.config['MOB_HIT_CAST_GATE'] = False
        m2 = self.ready(a, 2, xy=(1244.0, 714.0))
        with self.server._combat(a.session):
            a.session[W.GameServer.SKILL_CD_KEY].clear()
        a.send_c2s(self.keys['skill'], {'skill_id': ICE_SPEAR})
        use, mp, take = a.expect(0x25, 0x44, 0x2A)
        self.assertEqual(take.payload, cmd(m2, WALK_L, 1))                 # the chase word at once
        self.assertEqual(m2.ai_recover_until, 0.0)

    def ice_report(self, a, b, mob):
        """Ice Spear cast by A (the server's box lands it), its words and the report 300 ms of
        logic later (E 270): returns (t0, t1) around the report, both clients drained."""
        self.cast(a, ICE_SPEAR)
        self.drain(b)
        self.move(a, CAST_ICE, logic_elapsed_ms=40)
        self.move(a, IDLE, logic_elapsed_ms=60)
        self.drain(b)
        t0, t1, release, hit, logs = self.reported(
            a, b, lambda: self.catch_report(a, mob, 9, tae=9, logic_elapsed_ms=240))
        self.assertEqual(struct.unpack_from('<IIIIB', hit), (mob.uid, 0x209000, 1020, 1, 1))
        self.drain(a)
        self.drain(b)
        return t0, t1

    def ice_timers(self):
        """The deadlines of the pending ice-chase one-shots (_arm_hit_gate)."""
        return sorted(w for w, _, t in list(self.server.ticks._heap) if t.name == 'mob-ice-chase' and not t.cancelled)

    def test_the_ice_chase_word_goes_on_its_own_timer_before_the_stall(self):
        """MOB_HIT_ICE_CHASE_SECS 0.6: the report arms a one-shot at the hit + 0.6 s that
        sends the chase word to A and B then - not on the next 'monster-ai' tick, up to 0.3 s
        later and past the copies' stall at the end of the 960 ms node hold. The copies stay
        in the stun (2.02 s) whatever word they hold, so the server's point stays too and
        walks only from the stun's end; the word is kept alive meanwhile as any chase word."""
        self.hero()
        a, b = self.two()
        mob = self.ready(a, xy=(1244.0, 714.0))
        t0, t1 = self.ice_report(a, b, mob)
        gate = mob.ai_recover_until
        self.assertTrue(t0 + 0.6 - 1e-6 <= gate <= t1 + 0.6 + 1e-6, (gate - t0, gate - t1))
        self.assertAlmostEqual(mob.ai_stun_until - gate, 2.02 - 0.6)
        self.assertEqual(mob.ai_ice_until, mob.ai_stun_until)
        x = mob.x
        self.assertEqual(self.ice_timers(), [gate])
        self.server.ticks.run_due(gate - 0.01)
        self.assertEqual((self.ice_timers(), self.drain(a), self.drain(b)), ([gate], [], []))
        self.server.ticks.run_due(gate)                                      # the one-shot
        self.assertEqual(self.ice_timers(), [])
        self.assertEqual(self.drain(a), [(0x2A, cmd(mob, WALK_L, 1))])
        self.assertEqual(self.drain(b), [(0x2A, cmd(mob, WALK_L, 2))])
        sent = mob.ai_sent_t                                  # the timer's own clock (or later)
        self.assertTrue(gate <= sent <= time.monotonic())
        self.assertEqual(self.server._tick_monster_ai(sent + 0.6), 2)       # keep-alive, still frozen
        self.drain(a)
        self.drain(b)
        self.assertEqual(mob.x, x)
        end = mob.ai_stun_until
        self.assertEqual(self.server._tick_monster_ai(end + 0.3), 2)
        self.assertAlmostEqual(mob.x, x - 0.3 * mob.walk_px_s, places=6)    # walks from the stun's end
        # a swing on the frozen mob clears the ice (FUN_004194f0, action 7): its own stun gate
        until = self.server._arm_hit_gate(self.server.world.map(102).monsters, mob, end,
                                          W.HS.classify(None, 7, 7, fallback_ms=490))
        self.assertEqual((mob.ai_ice_until, round(until - end, 6), round(mob.ai_stun_until - end, 6)),
                         (0.0, 0.55, 0.43))

    def test_ice_chase_off_keeps_the_stun_gate(self):
        """MOB_HIT_ICE_CHASE_SECS 0: the ice hit is gated like any other, 2.02 + 0.12 s, and
        no one-shot is armed."""
        self.server.config['MOB_HIT_ICE_CHASE_SECS'] = 0
        self.hero()
        a, b = self.two()
        mob = self.ready(a, xy=(1244.0, 714.0))
        t0, t1 = self.ice_report(a, b, mob)
        self.assertEqual((self.ice_timers(), mob.ai_ice_until), ([], 0.0))
        self.assert_gate(a, b, mob, t0, t1, 2.14, xy=(mob.x, 714))

    def test_a_later_gate_keeps_the_ice_stun_ticking(self):
        """Watcher casts on the frozen mob before the ice chase word went: his cast gate (his
        cast + 1.41 s) holds the chase past the stall point. While the ice stun runs, the word
        both copies hold (the release's STOP) is re-sent every keep-alive so their state
        machine keeps ticking - it cannot cut the stun short; no chase word before the gate."""
        self.hero()
        self.hero(user='admin')
        a, b = self.two()
        mob = self.ready(a, xy=(1244.0, 714.0))
        t0, t1 = self.ice_report(a, b, mob)
        self.cast(b, ICE_SPEAR, pos=(1300.0, 714.0), facing=W.combat.FACING_LEFT)
        self.drain(a)
        self.drain(b)
        gate = mob.ai_recover_until
        self.assertGreater(gate, t1 + 1.3)
        self.assertEqual(len(self.ice_timers()), 1)
        self.server.ticks.run_due(t1 + 0.61)                                 # the one-shot: gated
        self.assertEqual((self.ice_timers(), self.drain(a), self.drain(b)), ([], [], []))
        self.assertEqual(self.server._tick_monster_ai(t1 + 0.7), 2)
        self.assertEqual(self.drain(a), [(0x2A, cmd(mob, AI.STOP, 1))])
        self.assertEqual(self.drain(b), [(0x2A, cmd(mob, AI.STOP, 2))])
        self.assertEqual(self.server._tick_monster_ai(t1 + 0.9), 0)          # not before the keep-alive
        self.assertEqual(self.server._tick_monster_ai(t1 + 1.3), 2)
        self.drain(a)
        self.drain(b)
        self.pin(a, mob, 1244, 714, gate)
        self.assertEqual(self.server._tick_monster_ai(gate), 2)              # the chase, on both
        self.assertEqual(self.drain(a), [(0x2A, cmd(mob, WALK_L, 1))])

    def test_the_ice_chase_word_goes_even_unchanged_and_stop_is_kept_alive(self):
        """A control effect holds the mob at the ice chase time: the decision is STOP, the word
        both copies already hold (the release's) - it still goes out at the chase time
        (_ice_word_due), and again every keep-alive while the ice stun runs, so neither copy
        stalls at the end of the 960 ms node hold; past the stun STOP is not re-sent."""
        self.hero()
        a, b = self.two()
        mob = self.ready(a, xy=(1244.0, 714.0))
        t0, t1 = self.ice_report(a, b, mob)
        gate, end = mob.ai_recover_until, mob.ai_ice_until
        with mock.patch.object(D, 'controlled', return_value=True):
            self.server.ticks.run_due(gate)                                    # the one-shot
            self.assertEqual(self.drain(a), [(0x2A, cmd(mob, AI.STOP, 1))])
            self.assertEqual(self.drain(b), [(0x2A, cmd(mob, AI.STOP, 2))])
            sent = mob.ai_sent_t
            self.assertTrue(gate <= sent < end - 0.6)
            self.assertEqual(self.server._tick_monster_ai(sent + 0.3), 0)
            self.assertEqual(self.server._tick_monster_ai(sent + 0.6), 2)      # keep-alive: STOP
            self.assertEqual(self.drain(a), [(0x2A, cmd(mob, AI.STOP, 1))])
            self.drain(b)
            self.assertEqual(self.server._tick_monster_ai(end + 0.6), 0)       # the stun is over
        self.assertEqual((self.drain(a), self.drain(b)), ([], []))

    def test_a_strong_attack_hurts_1020_in_the_case_9_form_with_flag_0(self):
        """Motion 5 (no cast): state 1 with variant 0 - L 1290 for weapon type 0, E 270, no
        element, the flag byte 0; gate 1.02 + 0.12 s. The stun is past the 0x2A node's 960 ms
        hold: both copies stall at its end until the chase word, then run the last 60 ms - the
        server's point walks from the word + 0.06 s."""
        a, b = self.two()
        mob = self.ready(a)
        self.face(a, W.combat.FACING_RIGHT)
        self.move(a, STRONG, logic_elapsed_ms=400)
        self.move(a, IDLE, logic_elapsed_ms=60)
        self.drain(b)
        t0, t1, release, hit, logs = self.reported(
            a, b, lambda: self.catch_report(a, mob, 9, tae=9, logic_elapsed_ms=240))
        self.assertEqual(release, cmd(mob, AI.STOP, 1))
        self.assertEqual(struct.unpack('<IIIIBddB', hit), (mob.uid, 0x209000, 1020, 1, 0, 1244.0, 714.0, 0))
        self.assertAlmostEqual(mob.x, 1244.0 + self.knock(mob, 4))
        line = next(t for t in logs if '[ATK] hit on' in t)
        self.assertIn('strong attack: L 1290 - E 270 (weapon 0), stun 1020, slide 4 ticks', line)
        self.assertEqual((mob.ai_stun_secs, round(mob.ai_stun_until - mob.ai_stun_t, 6)), (1.02, 1.02))
        self.assert_gate(a, b, mob, t0, t1, 1.14)
        gate = mob.ai_recover_until
        self.assertEqual(mob.ai_stun_secs, 0.0)                              # the word took it
        self.assertAlmostEqual(mob.ai_stun_until, mob.ai_sent_t + 1.02 - AI.HOLD_2A_SECS)
        self.pin(a, mob, 1244, 714, gate)
        self.assertEqual(self.server._tick_monster_ai(gate + 0.3), 0)       # keep-alive not due
        self.assertAlmostEqual(mob.x, 1244 - (gate + 0.3 - mob.ai_stun_until) * mob.walk_px_s)

    def test_an_unclassified_event_9_gets_the_state_1_default_and_extends_the_gate(self):
        """Interact event 9 with no strong / cast words counted and no cast: every event-9 hit
        on the attacker's copy runs a state-1 length, so the guess is the default L - 270
        (1290 - 270 = 1020 for weapon type 0; it errs high - 490 let the chase in early) in
        the case-9 form (flag 0), stun 1020, gate 1.14 s; after a basic swing's gate it
        extends it (never shortens it)."""
        a, b = self.two()
        mob = self.ready(a)
        self.face(a, W.combat.FACING_RIGHT)
        self.report_hit(a, mob)                                            # a swing: 0.55 s
        self.drain(a)
        self.drain(b)
        swing_gate, x = mob.ai_recover_until, mob.x
        t0, t1, release, hit, logs = self.reported(a, b, lambda: self.catch_report(a, mob, 9))
        self.assertEqual(struct.unpack('<IIIIBddB', hit), (mob.uid, 0x209000, 1020, 1, 0, x, 714.0, 0))
        line = next(t for t in logs if '[ATK] hit on' in t)
        self.assertIn('no cast / strong attack words counted (last action: none): the state-1 default '
                      'L 1290 - E 270 (weapon 0), stun 1020, slide 4 ticks', line)
        self.assertGreater(mob.ai_recover_until, swing_gate)
        self.assert_gate(a, b, mob, t0, t1, 1.14)

    # --------- review 2026-09-28: the attack key held in state 1, a hit behind the caster ---
    def cast(self, c, skill, *, pos=(1200.0, 714.0), facing=W.combat.FACING_RIGHT):
        """C2S 0x15 `skill` from `pos` facing `facing` (its cooldown cleared first): the S2C
        0x25 / 0x44 reach the caster, then whatever the damage sends him (the 0x2A take-over
        of a mob nobody drove yet) - drained. Returns (c0, c1) around the cast: c1 is taken
        after the caster's traffic went quiet, so the server armed any gate before it."""
        with self.server._combat(c.session):
            c.session['pos'] = pos
            (c.session.get(W.GameServer.SKILL_CD_KEY) or {}).clear()
        self.face(c, facing)
        c0 = time.monotonic()
        c.send_c2s(self.keys['skill'], {'skill_id': skill})
        got = [c.recv(3.0), c.recv(3.0)] + c.recv_until_quiet(0.1)
        self.assertEqual([p.opcode for p in got[:2] if p is not None], [0x25, 0x44])
        return c0, time.monotonic()

    def test_the_attack_key_held_through_an_ice_spear_cast_is_still_the_cast(self):
        """The attack key held (or mashed) while Ice Spear is cast: FUN_0042c310 sends motion-1
        words on every tick, FUN_00414210 ignores them in state 1. Was: a swing anchor, hurt
        400 with flag 0 (no ice on the watchers) and a 0.52 s gate against the attacker's
        2020 ms stun - the live +190 px. Now: 1020, flag 1, the ice chase at 0.6 s."""
        self.hero()
        a, b = self.two()
        mob = self.ready(a, xy=(1244.0, 714.0))
        self.cast(a, ICE_SPEAR)
        self.drain(b)
        self.move(a, CAST_ICE, logic_elapsed_ms=40)
        self.move(a, SWING, logic_elapsed_ms=60)                           # the key held in state 1
        self.assertEqual([op for op, _ in self.drain(b)], [0x1B, 0x1B])
        x = mob.x
        t0, t1, release, hit, logs = self.reported(
            a, b, lambda: self.catch_report(a, mob, 9, tae=9, held=SWING, logic_elapsed_ms=240))
        self.assertEqual(release, cmd(mob, AI.STOP, 1))
        self.assertEqual(struct.unpack('<IIIIBddB', hit), (mob.uid, 0x209000, 1020, 1, 1, x, 714.0, 0))
        line = next(t for t in logs if '[ATK] hit on' in t)
        self.assertIn('skill v1: L 1290 - E 270 (weapon 1), stun 2020 (ice), slide 4 ticks', line)
        self.assert_gate(a, b, mob, t0, t1, 0.6)

    def test_the_attack_key_held_through_a_strong_attack_is_still_the_strong_attack(self):
        """Was: a swing anchor and hurt 430 instead of 1020 (the dash-attack failure mode)."""
        a, b = self.two()
        mob = self.ready(a)
        self.face(a, W.combat.FACING_RIGHT)
        self.move(a, STRONG, logic_elapsed_ms=400)
        self.move(a, SWING, logic_elapsed_ms=60)
        self.drain(b)
        t0, t1, release, hit, logs = self.reported(
            a, b, lambda: self.catch_report(a, mob, 9, tae=9, held=SWING, logic_elapsed_ms=240))
        self.assertEqual(struct.unpack('<IIIIBddB', hit), (mob.uid, 0x209000, 1020, 1, 0, 1244.0, 714.0, 0))
        line = next(t for t in logs if '[ATK] hit on' in t)
        self.assertIn('strong attack: L 1290 - E 270 (weapon 0), stun 1020, slide 4 ticks', line)
        self.assert_gate(a, b, mob, t0, t1, 1.14)

    def test_a_crescent_slash_hit_behind_the_caster_slides_in_his_facing(self):
        """Case 9 sets the mob's +0x95B to the attacker's own facing (0x4134DA..0x4134E0), not
        to the side the mob is on - and Crescent Slash's box reaches 300 px behind him. A
        right-facing Warrior hits a mob 100 px behind: his copy slides RIGHT 19 wind ticks,
        so the watchers are told right (the cast's facing; the tail dx says left) and the
        server's point slides right too."""
        self.hero(skills=(CRESCENT_SLASH,))
        a, b = self.two()
        mob = self.ready(a, xy=(1100.0, 714.0))
        with self.assertLogs('WS', logging.INFO) as logs:
            self.cast(a, CRESCENT_SLASH)
        self.assertTrue(any('Crescent Slash' in t and '1 monster(s) in the box facing right' in t
                            for t in logs.output), logs.output)
        self.assertEqual(a.session['last_cast']['facing'], W.combat.FACING_RIGHT)
        self.drain(b)
        self.move(a, CAST_CRESCENT, logic_elapsed_ms=40)
        self.move(a, IDLE, logic_elapsed_ms=60)
        self.drain(b)
        x = mob.x
        t0, t1, release, hit, logs = self.reported(
            a, b, lambda: self.catch_report(a, mob, 9, tae=9, logic_elapsed_ms=240))
        self.assertEqual(struct.unpack('<IIIIBddB', hit), (mob.uid, 0x209000, 1020, 1, 6, x, 714.0, 0))
        self.assertAlmostEqual(mob.x, x + self.knock(mob, 19))
        line = next(t for t in logs if '[ATK] hit on' in t)
        self.assertIn('knock right, hurt 1020 (facing from his +0x949 (right, his words): tail dx -100.0, '
                      'held right)', line)
        self.assertIn('skill v6: L 1290 - E 270 (weapon 1), stun 1020 (wind), slide 19 ticks', line)
        self.assert_gate(a, b, mob, t0, t1, 1.14, word=WALK_R, xy=(x + self.knock(mob, 19), 714))
        # no +0x949 model (no 0x07 went to the session): his facing at the cast
        mob = self.ready(a, 2, xy=(1100.0, 714.0))
        self.cast(a, CRESCENT_SLASH)
        with self.server._combat(a.session):
            a.session.pop('hitstun')
            a.session['facing'] = W.combat.FACING_LEFT                       # a later held key
        self.drain(b)
        self.move(a, CAST_CRESCENT, logic_elapsed_ms=40)
        self.move(a, IDLE, logic_elapsed_ms=60)
        self.drain(b)
        t0, t1, release, hit, logs = self.reported(
            a, b, lambda: self.catch_report(a, mob, 9, tae=9, logic_elapsed_ms=240))
        line = next(t for t in logs if '[ATK] hit on' in t)
        self.assertIn('knock right, hurt 1020 (facing from the cast (right at cast time): tail dx -100.0, '
                      'held left)', line)

    # --------------- review 2026-09-28: repeated casts never stack the cast-time gate ---
    def test_repeated_casts_hold_the_chase_only_until_the_last_cast_plus_l(self):
        """Two Ice Spear casts 0.25 s apart with no report: the gate is the SECOND cast +
        1.41 s, no more; a second player's later cast moves it to his cast + 1.41 s, no
        more - it never adds up, so it cannot stall the mob past the last cast + L + margin."""
        self.hero()
        self.hero(user='admin')
        a, b = self.two()
        mob = self.ready(a, xy=(1244.0, 714.0))
        c0, c1 = self.cast(a, ICE_SPEAR)
        first = mob.ai_recover_until
        self.assertTrue(c0 + 1.41 - 1e-6 <= first <= c1 + 1.41 + 1e-6)
        time.sleep(0.25)
        d0, d1 = self.cast(a, ICE_SPEAR)
        second = mob.ai_recover_until
        self.assertTrue(d0 + 1.41 - 1e-6 <= second <= d1 + 1.41 + 1e-6, (second - d0, second - d1))
        self.assertGreater(second, first + 0.2)
        time.sleep(0.25)
        self.drain(a)
        self.drain(b)
        e0, e1 = self.cast(b, ICE_SPEAR, pos=(1300.0, 714.0), facing=W.combat.FACING_LEFT)
        third = mob.ai_recover_until
        self.assertTrue(e0 + 1.41 - 1e-6 <= third <= e1 + 1.41 + 1e-6, (third - e0, third - e1))
        self.drain(a)
        self.drain(b)
        self.pin(a, mob, 1244, 714, third - 0.01)
        self.assertEqual(self.server._tick_monster_ai(third - 0.01), 0)
        self.pin(a, mob, 1244, 714, third)
        self.assertEqual(self.server._tick_monster_ai(third), 2)           # the chase, on both

    def test_a_cast_after_a_report_takes_the_max_of_the_two_gates(self):
        """Cast, its report (the gate: the hit + 0.6 s, ice), a second cast: the gate is the max
        of the report's and the second cast + 1.41 s - never their sum, never shorter."""
        self.hero()
        a, b = self.two()
        mob = self.ready(a, xy=(1244.0, 714.0))
        self.cast(a, ICE_SPEAR)
        self.drain(b)
        self.move(a, CAST_ICE, logic_elapsed_ms=40)
        self.move(a, IDLE, logic_elapsed_ms=60)
        self.drain(b)
        t0, t1, release, hit, logs = self.reported(
            a, b, lambda: self.catch_report(a, mob, 9, tae=9, logic_elapsed_ms=240))
        after_report = mob.ai_recover_until
        self.assertTrue(t0 + 0.6 - 1e-6 <= after_report <= t1 + 0.6 + 1e-6)
        d0, d1 = self.cast(a, ICE_SPEAR)
        gate = mob.ai_recover_until
        self.assertGreaterEqual(gate, after_report)
        self.assertGreaterEqual(gate, d0 + 1.41 - 1e-6)
        self.assertLessEqual(gate, max(after_report, d1 + 1.41) + 1e-6)
        self.drain(a)
        self.drain(b)
        self.pin(a, mob, 1244, 714, gate - 0.01)
        # the first hit's ice stun (to the hit + 2.02 s) under the second cast's gate: no chase
        # word, only the keep-alive of the STOP both copies hold (test_a_later_gate_...)
        stun = gate - 0.01 < mob.ai_ice_until
        self.assertEqual(self.server._tick_monster_ai(gate - 0.01), 2 if stun else 0)
        self.assertEqual(self.drain(a), [(0x2A, cmd(mob, AI.STOP, 1))] if stun else [])
        self.drain(b)
        self.pin(a, mob, 1244, 714, gate)
        self.assertEqual(self.server._tick_monster_ai(gate), 2)
        self.assertEqual(self.drain(a), [(0x2A, cmd(mob, WALK_L, 1))])

    def test_skill_variant_relay_off_flinches_in_place_but_still_holds_the_chase(self):
        """MOB_HIT_RELAY_SKILL_VARIANT false: the old 33-byte flinch in place (action 7,
        facing2 0, hi 0, no slide) - A's own copy is still in its hurt, so the gate stands."""
        self.server.config['MOB_HIT_RELAY_SKILL_VARIANT'] = False
        a, b = self.two()
        mob = self.ready(a)
        t0, t1, release, hit, logs = self.reported(a, b, lambda: self.catch_report(a, mob, 9))
        self.assertEqual(struct.unpack('<IIIIddB', hit), (mob.uid, HIT, 0, 1, 1244.0, 714.0, 0))
        self.assertEqual(mob.x, 1244.0)
        line = next(t for t in logs if '[ATK] hit on' in t)
        self.assertIn('(0x2A lo 0x00007000 hi 0, synced to (1244,714), action 7; interact event 0x9: flinch in '
                      'place, no knock (MOB_HIT_RELAY_SKILL_VARIANT off); no cast / strong attack words counted '
                      '(last action: none): the state-1 default L 1290 - E 270 (weapon 0), stun 1020; chase '
                      'held 1.14 s)', line)
        self.assert_gate(a, b, mob, t0, t1, 1.14)

    def test_an_airborne_victim_is_relayed_with_its_tail_action(self):
        """The tail's target_action_event 8 (the mob was in the air on A's screen): the relay
        carries action 8 (still 33 bytes, no flag), the stun is the swing's, and the server
        keeps the fix at the hit point (the launch is not modelled)."""
        a, b = self.two()
        mob = self.ready(a)
        self.face(a, W.combat.FACING_RIGHT)
        t0, t1, release, hit, logs = self.reported(a, b, lambda: self.report_hit(a, mob, tae=8))
        self.assertEqual(struct.unpack('<IIIIddB', hit), (mob.uid, 0x208000, 490, 1, 1244.0, 714.0, 0))
        self.assertEqual(mob.x, 1244.0)
        self.assert_gate(a, b, mob, t0, t1, 0.55)

    def test_a_guarded_hit_relay_flinches_in_place_with_no_gate(self):
        """Interact event 4 (the victim guarded) keeps the pre-M1 relay too."""
        a, b = self.two()
        mob = self.ready(a)
        self.catch_report(a, mob, 4)
        self.assertTrue(a.wait_session(lambda s: mob.hp < 500))
        self.assert_flinch_in_place(a, b, mob, 1244.0, 714.0)

    def test_a_trap_catch_relay_flinches_in_place_with_no_gate(self):
        """Interact event 0xC (A's Booby Trap caught the mob; its C2S 0x6C noted the trap)
        keeps the pre-M1 relay too."""
        a, b = self.two()
        mob = self.ready(a)
        with self.server._combat(a.session):
            a.session['pending_trap'] = {'id': BOOBY_TRAP, 'x': mob.x, 'y': mob.y, 't': time.monotonic()}
        self.catch_report(a, mob, W.GameServer.TRAP_INTERACT_EVENT, item_id=BOOBY_TRAP)
        self.assertTrue(a.wait_session(lambda s: 'pending_trap' not in s))
        self.assert_flinch_in_place(a, b, mob, 1244.0, 714.0)

    def test_the_mob_estimate_uses_the_template_speed(self):
        """P3: the chase is dead-reckoned at 1e7 / hni speed px/s (Pupu / Ssiyo 120000 -> 83.3;
        a Monkey Soldier 80000 -> 125 px/s); MOB_SPEED_FROM_TEMPLATE false = 82.5 for all."""
        a, b = self.two()
        mob = self.ready(a)
        self.assertAlmostEqual(mob.walk_px_s, 1e7 / 120000)
        self.report_hit(a, mob)
        self.drain(a)
        self.drain(b)
        T = self.recovered(mob)
        self.pin(a, mob, 1244, 714, T)
        self.assertEqual(self.server._tick_monster_ai(T), 2)                # walk left
        self.server._tick_monster_ai(T + 0.3)
        self.assertAlmostEqual(mob.x, 1244 - 0.3 * mob.walk_px_s)
        self.server.config['MOB_SPEED_FROM_TEMPLATE'] = False
        self.pin(a, mob, 1244, 714, T + 0.3)
        self.server._tick_monster_ai(T + 0.4)
        self.assertAlmostEqual(mob.x, 1244 - 0.1 * W.GameServer.MOB_WALK_PX_PER_SEC)
        self.drain(a)
        self.drain(b)

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
        T = self.recovered(mob)
        self.pin(a, mob, 1244, 714, T)
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
        T = self.recovered(mob)
        self.pin(a, mob, 1244, 714, T)
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
    def tail(self, c, mob, at, event=W.GameServer.TRAP_INTERACT_EVENT, **extra):
        """A C2S 0x0D of `c` whose interact tail names `mob` at `at` with no hit (a trap catch
        with no trap pending: a position report and nothing else); waits until it is taken."""
        px, py = c.session['pos']
        t0 = (c.session.get('last_hit') or (0, None, 0, 0.0))[3]
        self.move(c, event << 16, target_uid=mob.uid, pos_x=float(px), pos_y=float(py),
                  target_dx=float(at[0] - px), target_dy=float(at[1] - py), flag_8db=0, flag_8e7=0,
                  timer_dac=0, target_action_event=7, item_id=BOOBY_TRAP, **extra)
        self.assertTrue(c.wait_session(lambda s: (s.get('last_hit') or (0, None, 0, 0.0))[3] != t0))

    def test_the_chase_targets_reports_place_the_mob_and_the_others_do_not(self):
        a, b = self.two()
        mob = self.ready(a)
        self.face(a, W.combat.FACING_RIGHT)
        k = self.knock(mob)
        self.report_hit(a, mob, at=(1200.0, 714.0))                        # unchased: A's report fixes it
        self.drain(b)
        a.expect(0x2A)
        # ... and the hurt slide moves it on: A (1000) faced right (desync fix M1)
        self.assertEqual((mob.x, mob.aggro_uid), (1200.0 + k, 1))
        # B's copy is elsewhere: his tail is no fix of a mob that chases A, A's is
        self.tail(b, mob, (1300.0, 714.0))
        self.assertEqual(mob.x, 1200.0 + k)
        self.tail(a, mob, (1210.0, 714.0))
        self.assertEqual(mob.x, 1210.0)
        self.drain(a)
        self.drain(b)
        # ... but B's HIT is relayed from his own tail (_hitter_point): A's copy is synced
        # to B's point, where B's copy stands, and B (1500) hit it facing left, so every copy
        # - and the server's point - slides back by k from there
        self.report_hit(b, mob, at=(1400.0, 714.0))
        self.assertEqual(b.expect(0x2A).payload, cmd(mob, AI.STOP, 2))
        relay, hit = a.expect(0x1B, 0x2A)
        self.assertEqual(struct.unpack_from('<IIIIdd', hit.payload),
                         (mob.uid, 0x107000, 490, 2, 1400.0, 714.0))
        self.assertAlmostEqual(mob.x, 1400.0 - k)
        self.assertEqual(mob.aggro_uid, 1)
        # the dev driver's memory sample: only from the chase target's client
        self.assertEqual(self.server._track_driver_monsters(2, {mob.uid: (1300.0, 714.0)}), 0)
        self.assertEqual(self.server._track_driver_monsters(1, {mob.uid: (1250.0, 714.0)}), 1)
        self.assertEqual(mob.x, 1250.0)
        # a mob chasing nobody: any client's report places it
        m2 = self.mob(a, 2)
        self.face(b, W.combat.FACING_RIGHT)
        self.report_hit(b, m2, at=(1700.0, 714.0))
        self.drain(a)
        self.drain(b)
        self.assertEqual((m2.x, m2.aggro_uid), (1700.0 + self.knock(m2), 2))    # B faced right

    def test_a_hit_relay_syncs_the_watchers_to_the_hitters_tail(self):
        """Live session 3 (3d_free1 18:13:35.938): the mob chased B, so A's report tail (16)
        was no fix (_mob_fix_allowed) and the relay synced B's copy to the server's point (47)
        - 31 px off A's copy, which slid from 16 (7 of 54 relays, 11..69 px off). The relay's
        sync point is the hitter's own tail, the server's point takes it, and the log says
        so; the chase rule still keeps A's tail without a hit out (tail())."""
        a, b = self.two()
        mob = self.ready(a)
        self.face(b, W.combat.FACING_LEFT)
        self.report_hit(b, mob)                                            # B hits first: it chases B
        self.drain(a)
        self.drain(b)
        self.assertEqual(mob.aggro_uid, 2)
        self.tail(a, mob, (1213.0, 714.0))                                 # A's copy: 31 px left
        self.assertEqual(mob.x, 1244.0 - self.knock(mob))                  # the server's: B's
        server_x = mob.x
        self.drain(b)
        self.face(a, W.combat.FACING_LEFT)
        with self.assertLogs('WS', logging.INFO) as logs:
            self.report_hit(a, mob, at=(1213.0, 714.0))
            self.assertEqual(a.expect(0x2A).payload, cmd(mob, AI.STOP, 1))
            relay, hit = b.expect(0x1B, 0x2A)
        self.assertEqual(struct.unpack_from('<IIIIdd', hit.payload), (mob.uid, 0x107000, 490, 1, 1213.0, 714.0))
        self.assertAlmostEqual(mob.x, 1213.0 - self.knock(mob))
        line = next(t for t in logs.output if '[ATK] hit on' in t)
        self.assertIn(f'0x2A lo 0x00107000 hi 490, synced to (1213,714) (his tail; the server had '
                      f'({server_x:.0f},714)), action 7; knock left', line)
        # no fresh tail naming the mob (another mob's, or older than LAST_HIT_FRESH_SECS): the
        # server's point, as before
        now = time.monotonic()
        self.assertEqual(self.server._hitter_point(a.session, mob, now), (1213.0, 714.0))
        self.assertIsNone(self.server._hitter_point(a.session, self.mob(a, 2), now))
        self.assertIsNone(self.server._hitter_point(a.session, mob, now + W.GameServer.LAST_HIT_FRESH_SECS + 0.1))
        with self.server._combat(a.session):
            a.session.pop('last_hit', None)
        self.assertIsNone(self.server._hitter_point(a.session, mob, now))

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
                                                'MOB_MAP_KEEP_SECS': 0, 'GROUND_LOOT': False,
                                                'DAMAGE_GRADE_ROLL': False})         # the 1-HP kill
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
        T = self.recovered(mob)
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


class HitRelayBytes(unittest.TestCase):
    def test_the_m1_packet_is_byte_exact(self):
        """Desync fix M1, the plan's example: monster 0x000F0009 hit by uid 1 facing right,
        hurt 360, at (656.25, 1901.375) -> the 33-byte S2C 0x2A every other holder gets
        (GameServer.hit_relay_fields, the builder _relay_hit sends), in both builds."""
        want = bytes.fromhex('09 00 0F 00 00 70 20 00 68 01 00 00 01 00 00 00 00 00 00 00 00 82 84 40 '
                             '00 00 00 00 80 B5 9D 40 00')
        fields = W.GameServer.hit_relay_fields(0x000F0009, 1, 656.25, 1901.375, W.combat.FACING_RIGHT, 360)
        for build in (B8, B9):
            with self.subTest(build=build):
                self.assertEqual(P.build('0x2A', dict(fields), receiver_uid=2, client_build=build), want)
        words = W.GameServer.hit_relay_words
        self.assertEqual(words(W.combat.FACING_RIGHT, 360), (0x00207000, 0x168))
        self.assertEqual(words(W.combat.FACING_LEFT, 360), (0x00107000, 0x168))
        self.assertEqual(words(None, 0), (HIT, 0))                         # MOB_HIT_RELAY_KNOCKBACK off
        self.assertEqual(words(W.combat.FACING_RIGHT, 0x1168), (0x00207000, 0x168))   # hi: 12 bits
        for f in (W.combat.FACING_LEFT, W.combat.FACING_RIGHT, None):
            lo = words(f, 360)[0]
            self.assertEqual(((lo >> 12) & 0xF, (lo >> 16) & 0xF), (7, 0))  # action 7, reaction 0

    def test_the_per_swing_and_case_9_packets_are_byte_exact(self):
        """HIT_STUN_PER_SWING_RE 6.3, attacker uid 1: the basic swing left (490) and the dash
        attack right (760), 33 bytes; Ice Spear left (1020, flag = variant 1), 34 bytes - the
        u8 after the target, before the position block. To the attacker himself an action-9
        0x2A would be 17 bytes (the flag is read in the self-form too): he never gets one."""
        F_ = W.GameServer.hit_relay_fields
        L, R = W.combat.FACING_LEFT, W.combat.FACING_RIGHT
        cases = [
            (F_(0x000F0001, 1, 1446.25, 1887.0, L, 490, action=7),
             '01 00 0F 00 00 70 10 00 EA 01 00 00 01 00 00 00 00 00 00 00 00 99 96 40 00 00 00 00 00 7C 9D 40 00'),
            (F_(0x000F0001, 1, 1075.0, 1887.0, R, 760),
             '01 00 0F 00 00 70 20 00 F8 02 00 00 01 00 00 00 00 00 00 00 00 CC 90 40 00 00 00 00 00 7C 9D 40 00'),
            (F_(0x000F0000, 1, 861.0, 1920.0, L, 1020, action=9, flag=1),
             '00 00 0F 00 00 90 10 00 FC 03 00 00 01 00 00 00 01 00 00 00 00 00 E8 8A 40 00 00 00 00 00 00 9E 40 00'),
        ]
        for fields, want in cases:
            for build in (B8, B9):
                with self.subTest(want=want[:23], build=build):
                    self.assertEqual(P.build('0x2A', dict(fields), receiver_uid=2, client_build=build),
                                     bytes.fromhex(want))
        ice = cases[2][0]
        self.assertEqual(ice['action_flag'], 1)
        self.assertNotIn('action_flag', cases[0][0])
        self.assertEqual(len(P.build('0x2A', dict(ice), receiver_uid=1, client_build=B9)), 17)
        self.assertEqual(W.GameServer.hit_relay_words(L, 1020, 9), (0x00109000, 0x3FC))
        self.assertEqual(W.GameServer.hit_relay_words(R, 490, 10), (0x0020A000, 0x1EA))
        self.assertEqual(F_(1, 1, 0, 0, R, 490, action=10, flag=0x1FF)['action_flag'], 0xFF)


class Config(unittest.TestCase):
    def test_keep_secs_default_and_validation(self):
        self.assertEqual(cfgmod.defaults().MOB_MAP_KEEP_SECS, 300.0)
        self.assertEqual(cfgmod.from_dict({'MOB_MAP_KEEP_SECS': 0}).MOB_MAP_KEEP_SECS, 0)
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.from_dict({'MOB_MAP_KEEP_SECS': -1})


if __name__ == '__main__':
    unittest.main(verbosity=2)
