#!/usr/bin/env python3
"""
test_combatsync.py - P6 stage 4 (combat sync), offline, both client builds
===========================================================================
cs-cast-anim-relay, cs-observer-sync, cs-party-skills, world-keyframe-2a (docs/systems/
combat_skill.md F3e/F3f/F10, world_movement_npc.md F8; spec 0x1B / 0x2A / 0x9E / 0x3B /
0x41 / 0x43; roadmap P6 exit criterion 6):

- the cast pose (C2S 0x0D motion 7 + variant, the live 2009 capture lo 0x0010003C) rides the
  movement relay to the clients holding the caster as one S2C 0x1B, never back to him;
- the keyframe / stop helpers: S2C 0x2A always 33 B with target 0 and no action / reaction
  nibble, S2C 0x9E a zero blob, only to the holders, never the mover; `!keyframe`;
- observer sync: a self buff's 0x3B and its 0x43 end on the other client, a skill learned
  (0x57) and a death (0x29) there too, a monster debuff's 0x41 and its end on every client
  holding the monster, nothing to a client on another map;
- party auras: Healing Aura covers the same-map party and heals it every 5010 ms (0x28 each,
  frames by the vitals tick), ends with 0x43 on both; Breath of Vitality raises a member's
  max HP while it covers it and a Break Party ends it with 0x43 {id, caster} and the clamp;
  Advance Aura's modifier counts for the member; another map is not covered.

Two fake clients (MultiClient: TestHero test/test uid 1 = client 1, a GM for `!` lines;
Watcher admin/admin uid 2 = client 2). No port is bound, no client is started and the live
accounts.json is never opened (temp copies; its hash is checked at the end).
"""
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
import combat  # noqa: E402
import debuffs as D  # noqa: E402
import fakeclient as F  # noqa: E402
import packets as P  # noqa: E402
import presence  # noqa: E402
import progression  # noqa: E402
import skills as SK  # noqa: E402

W = F.import_server()
cfgmod = W.cfgmod
EC = W.EC
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = cfgmod.BUILD_2008, cfgmod.BUILD_2009
DIRS = {B8: cfgmod.DEFAULTS['CLIENT_DIR'], B9: cfgmod.DEFAULTS['CLIENT_DIR_2009']}
HAVE = {b: os.path.exists(os.path.join(HERE, d, 'hs', 'windslayer.hii')) for b, d in DIRS.items()}
PORTAL_101_TO_102 = bytes.fromhex('17000000')   # live 0x7E capture: 101 line 23

# EN hii (classes: 1 warrior, 4 thief, 6 priest)
IAP1 = 292                   # Increase Attack Power Lv1: warrior self buff, Con 30 s
IAP2 = 293
HEALING_AURA = 2290          # warrior aura Lv33, +11 HP / 5010 ms, Con 21 s
ADVANCE_AURA = 2279          # warrior aura Lv30, Skill_P_A 5
VITALITY = 2798              # Breath of Vitality: priest aura Lv37, mHP 50, Con 40 s
POISON = 391                 # thief debuff Lv10, Con 9 s
WOODEN_BLADE, DAGGER, CLERIC_WAND = 70, 256, 248
CAST_LO_2009 = 0x0010003C    # live 2009 0x0D at an Ice Spear cast: motion 7, variant 1, facing 1
CAST_HI_2009 = 0xFA

_LIVE_HASH = None


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def setUpModule():
    global _LIVE_HASH
    _LIVE_HASH = _sha(LIVE_ACCOUNTS) if os.path.exists(LIVE_ACCOUNTS) else None
    P.STRICT_FIELDS.add(B9)                 # every 2009 S2C must use the 2009 field names


def tearDownModule():
    P.STRICT_FIELDS.discard(B9)
    EC.configure(DIRS[B8], B8)
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_combatsync.py (tests must only use temp copies)'


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _ops(pkts):
    return [p.opcode for p in pkts]


# ================================================================ pure rules ===
class Rules(unittest.TestCase):
    def test_the_cast_pose_words(self):
        """The live 2009 capture: motion (bits 2-4) 7, variant (bits 5-8) 1."""
        self.assertTrue(presence.is_cast_pose(CAST_LO_2009))
        self.assertEqual(presence.cast_variant(CAST_LO_2009), 1)
        self.assertFalse(presence.is_cast_pose(0x00100000))          # idle, facing only
        self.assertFalse(presence.is_cast_pose(0x004))               # a basic swing: motion 1
        self.assertTrue(presence.is_cast_pose((7 << 2) | (11 << 5)))
        self.assertEqual(presence.cast_variant((7 << 2) | (11 << 5)), 11)

    def test_keyframe_and_stop_bytes_are_the_same_in_both_builds(self):
        """0x2A: 4 uid + 8 words + 4 target 0 + f64 x + f64 y + u8 airborne = 33 B, the action
        and reaction nibbles cleared (a reaction nibble snaps the copy to (0,0)); 0x9E 12 B."""
        f = presence.keyframe_fields(2, 1500.0, 700.5, lo=0xFFFFFFFF, hi=0xFFFFFFFF)
        want = (struct.pack('<I', 2) + struct.pack('<II', 0x00300FFF, 0xFFF) + struct.pack('<I', 0)
                + struct.pack('<dd', 1500.0, 700.5) + b'\x00')
        # the receiver is never the mover and target 0 is never a receiver's uid (0x2A grammar)
        seen = {'mover.state_904 != 0x10 && target_uid != local_player_uid': True}
        for build in (B8, B9):
            self.assertEqual(P.build('0x2A', f, seen, client_build=build), want)
            self.assertEqual(P.build('0x9E', presence.stop_fields(2), client_build=build),
                             b'\x02\x00\x00\x00' + bytes(8))
        self.assertEqual(len(want), 33)

    def test_healing_aura_ticks_every_5010_ms_and_never_bursts(self):
        if not HAVE[B8]:
            self.skipTest('needs the EN 2008 client data')
        EC.configure(DIRS[B8], B8)
        s = {'uid': 1}
        buff, _ = B.apply(s, HEALING_AURA, 0.0)
        self.assertEqual(B.due_aura_heals(s, 5.0), [])
        self.assertEqual(B.due_aura_heals(s, 5.01), [(buff, 11)])
        self.assertEqual(B.due_aura_heals(s, 5.02), [])
        self.assertEqual(len(B.due_aura_heals(s, 19.0)), 1)          # late: one tick, no burst
        self.assertEqual(B.due_aura_heals(s, 21.5), [])              # the slot ran out

    def test_aura_cover_is_one_slot_per_group_and_own_first(self):
        mine = [{'id': ADVANCE_AURA, 'src': 1, 'group': 'aura'}]
        theirs = [[{'id': HEALING_AURA, 'src': 2, 'group': 'aura'},
                   {'id': VITALITY, 'src': 2, 'group': 'fVIT'}]]
        self.assertEqual(B.cover_of(mine, theirs), [{'id': VITALITY, 'src': 2}])
        self.assertEqual(B.cover_of([], theirs), [{'id': HEALING_AURA, 'src': 2}, {'id': VITALITY, 'src': 2}])
        self.assertEqual(B.cover_of([], []), [])


# ================================================================ two clients ===
class _Base:
    build = B8
    config = {}

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        self.tmp = tempfile.mkdtemp(prefix=f'ws_csync_{self.build}_')
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False,
                                **self.config})
        accounts = F.two_player_accounts()
        accounts['test']['characters'][0]['gm'] = 1
        self.server = F.make_server(self.tmp, accounts=accounts, config=cfg)
        self.prepare()
        self.mc = F.MultiClient(self.server)
        self.a, self.b = self.mc
        self.mc.drain()

    def prepare(self):
        """Store edits before anyone logs in (subclass hook)."""

    def tearDown(self):
        self.mc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------- helpers ---
    def key(self, op, name=''):
        for v in P.variants(op, 'C2S', client_build=self.build):
            if name in v['name']:
                return v['key']
        raise KeyError(op)

    def send(self, c, op, fields=None, name=''):
        c.send_c2s(self.key(op, name), fields or {})

    def rec(self, name):
        found = self.server.store.character_by_name(name)
        return found[2] if found else None

    def shape(self, name, cls, level, weapon, skills=()):
        with self.server.store.lock:
            ch = self.rec(name)
            ch.update({'class': cls, 'exp': progression.exp_for_level(level), 'skills': list(skills),
                       'equipped': {5: {'id': weapon, 'w': [0] * 6}} if weapon else {}})
        self.server.store.mark_dirty('test setup')

    def cast(self, c, skill):
        self.send(c, 0x15, {'skill_id': skill}, name='UseSkill')

    def gm(self, c, text):
        self.send(c, 0x03, {'msg_len': len(text), 'message': text})

    def move(self, c, lo, hi=0, elapsed=210, map_code=101):
        self.send(c, 0x0D, {'realtime_delta_ms': 0, 'map_code': map_code, 'logic_elapsed_ms': elapsed,
                            'state_lo': lo, 'state_hi': hi})

    def portal(self, c):
        c.send(0x7E, PORTAL_101_TO_102)
        self.assertTrue(_wait(lambda: self.server.world.map_of(c.session) == 102))
        return c.recv_until_quiet()

    def formed(self, host, guest):
        self.send(host, 0x27, {'target_uid': guest.session['uid']})
        guest.expect(0x4E)
        self.send(guest, 0x28, {'inviter_name': host.char_name})
        guest.expect(0x4F)
        host.expect(0x4F)

    def only(self, c, op, assume=None):
        pkts = c.recv_until_quiet(0.3)
        mine = [p for p in pkts if p.opcode == op]
        self.assertEqual(len(mine), 1, f'expected one 0x{op:02X}, got {[hex(o) for o in _ops(pkts)]}')
        return c.s2c(mine[0], assume)


class _CastRelay(_Base):
    def test_exit6_the_cast_pose_reaches_the_other_client_as_one_0x1B(self):
        """cs-cast-anim-relay: the caster's client sends its cast words itself (motion 7 +
        variant, immediately on the change); the server relays them as one S2C 0x1B to B,
        whose copy of A plays the cast - START_HOLD_MS after an idle stretch - and the idle
        words 60 ms later as the next node. Nothing goes back to A."""
        a, b = self.a, self.b
        self.move(a, CAST_LO_2009, CAST_HI_2009, elapsed=6510)
        got = b.s2c(b.expect(0x1B))
        self.assertEqual(got['uid'], 1)
        self.assertEqual(got['hold_ms'], presence.START_HOLD_MS)
        self.assertEqual(P.to_bytes(got['state_blob']), struct.pack('<II', CAST_LO_2009, CAST_HI_2009))
        self.move(a, 0x00100000, CAST_HI_2009, elapsed=60)
        got = b.s2c(b.expect(0x1B))
        self.assertEqual((got['hold_ms'], P.to_bytes(got['state_blob'])[:4]), (60, struct.pack('<I', 0x00100000)))
        a.expect_silence(0.1)
        # a 2008-style variant (Group Heal family 6) with a walk in progress keeps its hold
        self.move(a, 0x2, elapsed=210)
        b.expect(0x1B)
        lo = (7 << 2) | (6 << 5)
        self.move(a, lo, elapsed=90)
        got = b.s2c(b.expect(0x1B))
        self.assertEqual((got['hold_ms'], struct.unpack('<I', P.to_bytes(got['state_blob'])[:4])[0]), (90, lo))
        # a client on another map holds nobody from 101: nothing
        self.portal(b)
        self.mc.drain()
        self.move(a, CAST_LO_2009, elapsed=300)
        b.expect_silence(0.15)


class _Keyframe(_Base):
    def test_keyframe_and_stop_go_to_the_holders_never_the_mover(self):
        """world-keyframe-2a: the helpers used for GM moves / server corrections."""
        a, b = self.a, self.b
        presence.move_state(a.session)['relayed'] = (0x000F7002, 0xFFFFF0FA)   # walking right + tails
        self.assertEqual(presence.send_keyframe(self.server, a.session, 1500.0, 700.0), 1)
        pkt = b.expect(0x2A)
        self.assertEqual(len(pkt.payload), 33)
        got = b.s2c(pkt)
        self.assertEqual((got['mover_uid'], got['target_uid'], got['pos_x'], got['pos_y'], got['airborne']),
                         (1, 0, 1500.0, 700.0, 0))
        self.assertEqual(P.to_bytes(got['move_bits']), struct.pack('<II', 0x2, 0xFA))   # ae / ie cleared
        a.expect_silence(0.1)
        self.assertEqual(presence.send_stop(self.server, a.session), 1)
        self.assertEqual(b.expect(0x9E).payload, b'\x01\x00\x00\x00' + bytes(8))
        a.expect_silence(0.1)
        # the dev command: the server's own point of the player (estimate on the floor)
        self.gm(a, b'!keyframe Watcher')
        pkt, line = a.expect(0x2A, 0x15)
        self.assertIn('0x2A keyframe of Watcher', P.to_bytes(a.s2c(line)['text']).decode('cp949'))
        got = a.s2c(pkt)
        self.assertEqual((got['mover_uid'], (got['pos_x'], got['pos_y'])),
                         (2, presence.floor_point(b.session)))
        b.expect_silence(0.1)
        self.gm(a, b'!keyframe stop')
        self.assertEqual(b.expect(0x9E).payload, b'\x01\x00\x00\x00' + bytes(8))
        a.expect(0x15)
        # on another map nobody holds A
        self.portal(b)
        self.mc.drain()
        self.assertEqual(presence.send_keyframe(self.server, a.session), 0)
        self.assertEqual(presence.send_stop(self.server, a.session), 0)
        b.expect_silence(0.1)


class _Observer(_Base):
    def prepare(self):
        self.shape('TestHero', 1, 40, WOODEN_BLADE, [IAP1])

    def test_a_self_buff_its_end_a_learned_skill_and_a_death_reach_the_other_client(self):
        """cs-observer-sync (F10): 0x3B on cast, 0x43 at the end, 0x57 on a learn, 0x29 on a
        death - to the client holding A, never back to A as an observer packet; the record
        revision moves so a spawn record in flight is rebuilt with the change."""
        a, b = self.a, self.b
        self.server._set_vitals(a.session, hp=200, mp=100)
        a.expect(0x28, 0x44)
        rev = presence.revision(a.session)
        self.cast(a, IAP1)
        a.expect(0x3B, 0x28, 0x44)
        self.assertEqual(b.s2c(b.expect(0x3B)), {'item_or_skill_id': IAP1, 'target_uid': 1})
        self.assertGreater(presence.revision(a.session), rev)
        buff = B.find(a.session, IAP1)
        self.server._tick_buffs(buff['expires'])
        self.assertEqual(a.s2c(a.expect(0x43)), {'buff_item_id': IAP1, 'target_uid': 1})
        self.assertEqual(b.s2c(b.expect(0x43)), {'buff_item_id': IAP1, 'target_uid': 1})
        # a learned skill (the GM's `!learn`): 0x57 to B
        self.gm(a, b'!learn 293 force')
        a.recv_until_quiet(0.3)
        self.assertEqual(b.s2c(b.expect(0x57)), {'uid': 1, 'skill_item_id': IAP2})
        # a death: 0x29 on B only (A gets its own 0x3E / 0x21)
        self.server.damage_player(a.session, 100000)
        self.assertIn(0x3E, _ops(a.recv_until_quiet(0.3)))
        got = b.s2c(b.expect(0x29))
        self.assertEqual((got['uid'], got['respawn_tick']), (1, 0x7FFFFFFF))

    def test_a_client_on_another_map_sees_nothing(self):
        a, b = self.a, self.b
        self.portal(b)
        self.mc.drain()
        self.server._set_vitals(a.session, hp=200, mp=100)
        a.expect(0x28, 0x44)
        self.cast(a, IAP1)
        a.expect(0x3B, 0x28, 0x44)
        self.server._tick_buffs(B.find(a.session, IAP1)['expires'])
        a.expect(0x43)
        b.expect_silence(0.15)


class _Debuff(_Base):
    def prepare(self):
        self.shape('TestHero', 4, 40, DAGGER, [POISON])

    def test_a_monster_debuff_and_its_end_reach_every_client_holding_the_monster(self):
        """cs-observer-sync (F10 "debuff 0x41"): the 0x41 on the monster goes to B as well
        (B's copy shows the slot with A at +0x14), and its end (0x3C / 0x43) to both."""
        a, b = self.a, self.b
        self.portal(a)
        self.portal(b)
        self.mc.drain()
        mob = next(m for m in a.session['monsters'].values() if m.alive)
        mob.hp = mob.max_hp = 5000
        with self.server._combat_lock(a.session):
            a.session['pos'] = (mob.x - 20.0, mob.y)
            a.session['facing'] = combat.FACING_RIGHT
        self.server._set_vitals(a.session, mp=100)
        a.expect(0x44)
        self.cast(a, POISON)
        pkts = a.recv_until_quiet(0.3)
        self.assertEqual(_ops(pkts)[0], 0x25)
        mine = [a.s2c(p) for p in pkts if p.opcode == 0x41]
        want = {'target_uid': mob.uid, 'source_uid': 1, 'item_id': POISON}
        self.assertEqual(mine, [want])
        self.assertEqual(self.only(b, 0x41), want)
        rec = mob.debuffs[next(iter(mob.debuffs))]
        key = int(B.removal_opcode(POISON), 16)
        self.server._tick_debuffs(rec['expires'] + 0.01)
        for c in (a, b):
            ends = [c.s2c(p) for p in c.recv_until_quiet(0.3) if p.opcode == key]
            self.assertEqual(ends, [{'buff_item_id': POISON, 'target_uid': mob.uid}], c.char_name)
        self.assertFalse(mob.debuffs)


class _Auras(_Base):
    """TestHero: Lv40 warrior with Healing Aura / Advance Aura; Watcher Lv35."""

    def prepare(self):
        self.shape('TestHero', 1, 40, WOODEN_BLADE, [HEALING_AURA, ADVANCE_AURA])
        with self.server.store.lock:
            self.rec('Watcher')['exp'] = progression.exp_for_level(35)
        self.server.store.mark_dirty('test setup')

    def test_exit6_healing_aura_covers_and_heals_the_party_on_the_map(self):
        """cs-party-skills F3f: the aura is one 0x3B on A, the same 0x3B on B (its client fans
        it out); B is covered at once; every 5010 ms A and B get +11 HP (0x28 each) and A's
        frame of B follows (0x54); the end is a 0x43 on both and B's cover goes."""
        a, b = self.a, self.b
        self.formed(a, b)
        self.server._set_vitals(a.session, hp=100, mp=200)
        self.server._set_vitals(b.session, hp=20)
        self.server.party.tick_vitals()
        self.mc.drain()
        self.cast(a, HEALING_AURA)
        a.expect(0x3B, 0x44)                                  # the cost only (FUN_004258d0 = 0)
        self.assertEqual(b.s2c(b.expect(0x3B)), {'item_or_skill_id': HEALING_AURA, 'target_uid': 1})
        self.assertEqual(b.session.get('aura_cover'), [{'id': HEALING_AURA, 'src': 1}])
        buff = B.find(a.session, HEALING_AURA)
        self.assertEqual(self.server._tick_buffs(buff['next_heal']), 2)
        self.assertEqual(a.s2c(a.expect(0x28)), {'hp': 111})
        self.assertEqual(b.s2c(b.expect(0x28)), {'hp': 31})
        self.server.party.tick_vitals()
        self.assertEqual(a.s2c(a.expect(0x54))['cur_hp'], 31)
        hp, _mp = b.expect(0x54, 0x55)                        # A's HP, and its MP after the cost
        self.assertEqual(b.s2c(hp)['cur_hp'], 111)
        # B on another map: A's next tick heals A alone, B's cover is gone
        self.portal(b)
        self.mc.drain()
        self.server._tick_buffs(buff['next_heal'])
        self.assertIsNone(b.session.get('aura_cover'))
        self.assertEqual(a.s2c(a.expect(0x28)), {'hp': 122})
        b.expect_silence(0.1)
        self.server._tick_buffs(buff['expires'])
        self.assertEqual(a.s2c(a.expect(0x43)), {'buff_item_id': HEALING_AURA, 'target_uid': 1})
        b.expect_silence(0.1)

    def test_a_covering_aura_counts_in_the_members_modifiers_and_ends_with_the_party(self):
        """Advance Aura (Skill_P_A 5) on A covers B: B's damage modifiers include it; B
        breaks the party -> 0x43 {aura, A} to B and B's cover is gone; A keeps its aura."""
        a, b = self.a, self.b
        self.formed(a, b)
        self.server._set_vitals(a.session, mp=200)
        a.expect(0x44)
        self.cast(a, ADVANCE_AURA)
        a.expect(0x3B, 0x44)
        b.expect(0x3B)
        self.assertEqual(B.modifiers(b.session).p_a, 5)
        self.send(b, 0x29)
        pkts = b.recv_until_quiet(0.3)
        self.assertEqual([b.s2c(p) for p in pkts if p.opcode == 0x43],
                         [{'buff_item_id': ADVANCE_AURA, 'target_uid': 1}])
        self.assertIn(0x51, _ops(pkts))
        self.assertIsNone(b.session.get('aura_cover'))
        self.assertEqual(B.modifiers(b.session).p_a, 0)
        self.assertEqual(B.modifiers(a.session).p_a, 5)
        a.expect(0x51)                                        # the dissolve; nothing about B's auras


class _Vitality(_Base):
    """TestHero: Lv40 priest with Breath of Vitality; Watcher Lv35."""

    def prepare(self):
        self.shape('TestHero', 6, 40, CLERIC_WAND, [VITALITY])
        with self.server.store.lock:
            self.rec('Watcher')['exp'] = progression.exp_for_level(35)
        self.server.store.mark_dirty('test setup')

    def test_breath_of_vitality_raises_the_members_max_hp_until_the_party_breaks(self):
        """The aura's mHP reaches the covered member's maximum (the +0x107C copy, FUN_00427d40)
        - its current HP stays - and when A leaves the party B loses it again: 0x43 {2798,
        A} to B, the maximum drops and the HP is clamped with an absolute 0x28."""
        a, b = self.a, self.b
        self.formed(a, b)
        base = b.session['max_hp']
        self.server._set_vitals(a.session, mp=200)
        a.expect(0x44)
        self.cast(a, VITALITY)
        a.expect(0x3B, 0x44)
        b.expect(0x3B)
        self.assertEqual(b.session['max_hp'], base + 50)
        self.server._set_vitals(b.session, hp=base + 50)
        b.expect(0x28)
        self.send(a, 0x29)                                    # A breaks the party
        a.expect(0x51)
        pkts = b.recv_until_quiet(0.3)
        self.assertEqual([b.s2c(p) for p in pkts if p.opcode == 0x43],
                         [{'buff_item_id': VITALITY, 'target_uid': 1}])
        self.assertEqual([b.s2c(p) for p in pkts if p.opcode == 0x28], [{'hp': base}])
        self.assertEqual(b.session['max_hp'], base)
        self.assertIsNotNone(B.find(a.session, VITALITY))    # A keeps its own slot and maximum
        self.assertEqual(a.session['slot_buffs'], [{'id': VITALITY}])


def _builds(cls):
    out = {}
    for build in (B8, B9):
        name = f'{cls.__name__.lstrip("_")}{build}'
        out[name] = type(name, (cls, unittest.TestCase), {'build': build})
    return out


for _cls in (_CastRelay, _Keyframe, _Observer, _Debuff, _Auras, _Vitality):
    globals().update(_builds(_cls))


if __name__ == '__main__':
    unittest.main(verbosity=1)
