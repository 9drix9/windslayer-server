#!/usr/bin/env python3
"""
test_party.py - P6 stage 3 (parties), offline, both client builds
=================================================================
party-core-state / -invite / -accept-join / -leave / -chat / -vitals-sync /
-map-change-hud / -skill-hooks / -exp-share (docs/systems/party.md F1-F10, combat_skill.md
F3c/F3e; live party#00-#14):

- pure rules (party.py): the exp split, the level gap, frame values never 0 (C11), the
  0x4F / 0x51 / 0x54 / 0x55 / 0x90 bytes of both builds, the routes and the reply policy;
- two / three fake clients on one server (MultiClient; TestHero test/test uid 1 = client 1,
  Watcher admin/admin uid 2 = client 2, Carol uid 3), for 2008 and 2009: Make Party 0x27 ->
  0x4E -> Accept 0x28 -> 0x4F both ways; vitals 0x54 / 0x55 after a potion, damage, a
  level-up and a map load; party chat 0x6A -> orange 0x90 to every member on any map;
  frames kept across a portal; Break Party 0x29 -> 0x51 (dissolve, three-member compaction
  rebuild); disconnect and 2009 relogin; every refusal (0x15 / 0x50 / 0x56 / 0x53); Group
  Heal 0x92 + 0x28 to a member; a Heal landing on the weakest member; the party exp split
  of a shared-monster kill; the map-load rebuild flag; `!party`.

No port is bound, no client is started and the live accounts.json is never opened (temp
copies; the module checks its hash at the end).
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

import fakeclient as F  # noqa: E402
import packets as P  # noqa: E402
import party as PT  # noqa: E402
import progression  # noqa: E402
import registry  # noqa: E402

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
MINOR_POTION = 7                                # Type 0, HP +60
GROUP_HEAL = 2776                               # priest Lv30, MP 44, HP +52 (party_heal)
HEAL = 446                                      # priest Lv10, MP 10, +40 on the target
CLERIC_WAND = 248

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
            'accounts.json changed during test_party.py (tests must only use temp copies)'


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _ops(pkts):
    return [p.opcode for p in pkts]


def _name(value):
    return P.cut_text(value, 16).decode('cp949')


def _char(name, cls=0):
    return {'name': name, 'level': 1, 'class': cls, 'map': 101, 'x': 1411.0, 'y': 714.0, 'hp': 100, 'mp': 50}


# ================================================================ pure rules ===
class Rules(unittest.TestCase):
    def test_the_exp_split(self):
        """party-exp-share: a pool of exp * (100 + bonus * (n - 1)) / 100 split evenly, the
        remainder to the killer, every share at least 1; alone = the whole kill."""
        self.assertEqual(PT.split_exp(100, 1, 20), [100])
        self.assertEqual(PT.split_exp(100, 2, 20), [60, 60])            # 120 in total
        self.assertEqual(PT.split_exp(101, 2, 20), [61, 60])            # 121: the odd one to the killer
        self.assertEqual(PT.split_exp(100, 3, 25), [50, 50, 50])
        self.assertEqual(PT.split_exp(1, 5, 0), [1, 1, 1, 1, 1])
        self.assertEqual(PT.split_exp(10, 2, 0), [5, 5])
        self.assertEqual(PT.split_exp(0, 3, 20), [0])

    def test_level_gap(self):
        self.assertTrue(PT.levels_ok([1, 11]))
        self.assertFalse(PT.levels_ok([1, 12]))
        self.assertTrue(PT.levels_ok([30, 25, 35]))
        self.assertFalse(PT.levels_ok([30, 19, 25]))

    def test_frame_values_are_never_zero_maxima(self):
        """C11 / hazard 1: a 0x4F with max 0 binds a frame that can never be freed and a
        0x54 / 0x55 with max 0 blanks the receiver's copy - maxima are at least 1, u16."""
        s = {'char_name': 'TestHero', 'uid': 1, 'hp': 70000, 'max_hp': 0, 'mp': 5}
        self.assertEqual(PT.vitals(s), (1, 0xFFFF, 1, 5))
        f = PT.member_fields(s)
        self.assertEqual((f['member_uid'], f['hp_max'], f['mp_max']), (1, 1, 1))
        for build in (B8, B9):
            raw = P.build('0x4F', PT.member_fields({'char_name': 'Watcher', 'uid': 2, 'hp': 80, 'max_hp': 100,
                                                    'mp': 25, 'max_mp': 50}), client_build=build)
            self.assertEqual(raw.hex(), '57617463686572' + '00' * 10 + '02000000' + '64005000' + '32001900')
            self.assertEqual(P.build('0x51', {'member_uid': 2}, client_build=build), b'\x02\x00\x00\x00')
            self.assertEqual(P.build('0x54', {'uid': 2, 'max_hp': 100, 'cur_hp': 30}, client_build=build).hex(),
                             '0200000064001e00')
            self.assertEqual(P.build('0x90', {'text': b'TestHero : hi'}, client_build=build),
                             b'\x0dTestHero : hi')

    def test_routes_and_policy(self):
        want = {0x27: '_handle_party_invite', 0x28: '_handle_party_accept', 0x29: '_handle_party_leave',
                0x6A: '_handle_party_chat'}
        for routes in (W.GameServer.ROUTES, W.GameServer.ROUTES_2009):
            self.assertEqual({op: routes[op].handler for op in want}, want)
            self.assertEqual(registry.check_routes(routes, W.GameServer), [])
        # 0x20 Trade left the popup's logging table with P7 stage 1 (trade.py, test_trade.py)
        self.assertEqual(set(W.GameServer.POPUP_REQUESTS), set())
        for build in (B8, B9):
            for op in want:                     # the client waits for nothing, and 0x90 /
                self.assertNotIn(op, registry.must_reply_table(build))      # 0x51 / 0x4F must
                self.assertNotIn(op, registry.never_reply_table(build))     # reach the sender
        self.assertEqual(W.GameServer.SKILL_EFFECT_HANDLERS['party_heal'], '_skill_party_heal')
        self.assertNotIn('party_heal', W.GameServer.SKILL_EFFECT_OWNERS)
        d = cfgmod.defaults()
        self.assertEqual((d['PARTY_EXP_SHARE'], d['PARTY_EXP_BONUS_PCT'], d['PARTY_HUD_REBUILD_ON_MAP_LOAD']),
                         (True, 20, False))
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.from_dict({'PARTY_EXP_BONUS_PCT': 101})

    def test_a_full_hud_gets_no_0x4F(self):
        """0x4F reads nothing when the four frames are bound and has no duplicate check: the
        mirror never sends one to a full HUD, a duplicate uid or the viewer itself."""
        sent = []

        class Server:
            @staticmethod
            def _push(target, key, fields, tag, flush=None):
                sent.append((key, fields.get('member_uid')))
                return True
        parties = PT.Parties(Server())
        viewer = {'uid': 1, 'char_name': 'A', 'sock': object(), 'party_hud': [2, 3, 4, 5]}
        self.assertFalse(parties._hud_add(viewer, {'uid': 6, 'char_name': 'F'}))
        viewer['party_hud'] = [2, 0, 0, 0]
        self.assertFalse(parties._hud_add(viewer, {'uid': 2, 'char_name': 'B'}))
        self.assertFalse(parties._hud_add(viewer, {'uid': 1, 'char_name': 'A'}))
        self.assertEqual(sent, [])
        self.assertTrue(parties._hud_add(viewer, {'uid': 7, 'char_name': 'G', 'max_hp': 9, 'hp': 9}))
        self.assertEqual((sent, viewer['party_hud']), ([('0x4F', 7)], [2, 7, 0, 0]))


# ============================================================== two players ===
class _Base:
    """TestHero (uid 1) and Watcher (uid 2) in world on map 101 of one server of `build`;
    Carol (uid 3) exists and is offline until a test logs her in."""
    build = B8
    config = {}

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        self.tmp = tempfile.mkdtemp(prefix=f'ws_party_{self.build}_')
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False,
                                **self.config})
        accounts = F.two_player_accounts()
        accounts['carol'] = {'password': 'carol', 'characters': [_char('Carol')]}
        self.server = F.make_server(self.tmp, accounts=accounts, config=cfg)
        self.prepare()
        self.extra = []
        self.mc = F.MultiClient(self.server)
        self.a, self.b = self.mc
        self.mc.drain()

    def prepare(self):
        """Store edits before anyone logs in (subclass hook)."""

    def tearDown(self):
        for c in self.extra:
            try:
                c.close()
            except OSError:
                pass
        self.mc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ----------------------------------------------------------------- helpers ---
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

    def login(self, account, password, name, number):
        c = F.FakeClient(self.server)
        self.extra.append(c)
        self.assertEqual(c.login(account, password)['result'], 1)
        c.char_name = name
        c.enter_world(name, port=F.P2P_PORT_BASE + number - 1)
        return c

    def carol(self):
        c = self.login('carol', 'carol', 'Carol', 3)
        self.mc.drain()
        return c

    def portal(self, c):
        c.send(0x7E, PORTAL_101_TO_102)
        self.assertTrue(_wait(lambda: self.server.world.map_of(c.session) == 102))
        return c.recv_until_quiet()

    def only(self, c, op, assume=None):
        """Exactly one S2C `op` among what `c` got (presence 0x05/0x06 may ride along)."""
        pkts = c.recv_until_quiet(0.3)
        mine = [p for p in pkts if p.opcode == op]
        self.assertEqual(len(mine), 1, f'expected one 0x{op:02X}, got {[hex(o) for o in _ops(pkts)]}')
        return c.s2c(mine[0], assume)

    def notice(self, c):
        return P.to_bytes(c.s2c(c.expect(0x15))['text']).decode('cp949')

    def uid(self, c):
        return c.session['uid']

    def frame(self, of):
        s = of.session
        return {'member_name': of.char_name, 'member_uid': s['uid'], 'hp_max': s['max_hp'], 'hp_cur': s['hp'],
                'mp_max': s['max_mp'], 'mp_cur': s['mp']}

    def added(self, c):
        pkt = c.recv(3.0)
        self.assertEqual(getattr(pkt, 'opcode', None), 0x4F, pkt)
        rec = c.s2c(pkt)
        rec['member_name'] = _name(rec['member_name'])
        return rec

    def form(self, host, guest):
        """host: Make Party on guest (0x27) -> guest 0x4E -> Accept (0x28) -> 0x4F both ways."""
        self.send(host, 0x27, {'target_uid': self.uid(guest)})
        got = guest.s2c(guest.expect(0x4E))
        self.assertEqual(_name(got['inviter_name']), host.char_name)
        self.send(guest, 0x28, {'inviter_name': host.char_name})

    def tick(self):
        return self.server.party.tick_vitals()

    def members(self, c):
        p = self.server.party.party_of(c.session)
        return [s['char_name'] for s in p.members] if p is not None else None

    def chat(self, c, text):
        self.send(c, 0x6A, {'text_len': len(text), 'text': text})

    def formed(self, host, guest):
        self.form(host, guest)
        guest.expect(0x4F)
        host.expect(0x4F)

    def kill_one(self, killer):
        mob = next(m for m in killer.session['monsters'].values() if m.alive)
        for _ in range(60):
            if not mob.alive:
                break
            self.server._memory_melee(mob.x, mob.y, uid=self.uid(killer))
        self.assertFalse(mob.alive)
        return mob

    @staticmethod
    def exp_of(pkts):
        return [struct.unpack_from('<i', p.payload)[0] for p in pkts if p.opcode == 0x21]

    def back_portal(self):
        index = next(int(k.split('_')[1]) for k, v in EC.portals().items()
                     if k.startswith('102_') and v[0] == 101)
        return index.to_bytes(4, 'little')


class _Party(_Base):
    # ------------------------------------------------------ F1 / F2 exit 5 ---
    def test_exit5_make_party_frames_potion_chat_portal_and_break(self):
        """Exit criterion 5: A's Make Party -> 0x4E "<A> wants to party with you" on B;
        Accept -> each gets a frame (0x4F) of the other with real HP/MP; B's potion moves
        A's frame (0x54 at the next vitals tick); /p is an orange 0x90 on both, on any map;
        A's portal keeps the frames (nothing re-sent but the missed vitals); Break Party
        clears both (0x51 self to the leaver, the dissolve 0x51 self to the other)."""
        a, b = self.a, self.b
        self.send(a, 0x27, {'target_uid': 2})
        pkt = b.expect(0x4E)
        self.assertEqual(pkt.payload, P.name17('TestHero'))
        a.expect_silence(0.1)                           # the client printed its own line
        self.send(b, 0x28, {'inviter_name': 'TestHero'})
        pkt = b.expect(0x4F)
        self.assertEqual(len(pkt.payload), 29)
        got = b.s2c(pkt)
        got['member_name'] = _name(got['member_name'])
        self.assertEqual(got, self.frame(a))
        self.assertEqual(self.added(a), self.frame(b))
        self.assertTrue(all(v > 0 for k, v in self.frame(b).items() if k.endswith('max')))
        self.assertEqual((a.session['party_hud'], b.session['party_hud']), ([2, 0, 0, 0], [1, 0, 0, 0]))
        self.assertEqual(self.members(a), ['TestHero', 'Watcher'])
        self.assertEqual(self.tick(), 0)                # the 0x4F carried the current values
        # B drinks a potion: B's own 0x25; A (same map) sees the heal visual 0x40 and, at
        # the next tick, its frame of B: one 0x54 for the damage + potion together.
        self.server._set_vitals(b.session, hp=10)
        b.expect(0x28)
        self.assertIsNotNone(self.server._inv_add(b.session, MINOR_POTION, 1, 'test'))
        self.send(b, 0x15, {'item_id': MINOR_POTION}, name='UseItem (')
        b.expect(0x25)
        a.expect(0x40)
        self.assertEqual(self.tick(), 1)
        self.assertEqual(a.s2c(a.expect(0x54)), {'uid': 2, 'max_hp': b.session['max_hp'], 'cur_hp': 70})
        b.expect_silence(0.05)
        self.assertEqual(self.tick(), 0)
        # party chat: the sender's own line comes from the server too (no local echo)
        self.chat(a, b'TestHero : hi there')
        for c in (a, b):
            self.assertEqual(P.to_bytes(c.s2c(c.expect(0x90))['text']), b'TestHero : hi there')
        # A portals: no 0x51 / 0x4F in its map load (the frames survive 0x08/0x03, live
        # party#14); the next tick re-sends B's vitals to A, which missed pushes meanwhile.
        burst = self.portal(a)
        self.assertFalse({0x4F, 0x51, 0x54, 0x55} & set(_ops(burst)), _ops(burst))
        self.assertEqual(self.only(b, 0x06), {'uid': 1})
        self.assertEqual(a.session['party_hud'], [2, 0, 0, 0])
        self.assertEqual(self.tick(), 2)
        self.assertEqual(_ops(a.expect(0x54, 0x55)), [0x54, 0x55])
        b.expect_silence(0.05)
        # chat from B (map 101) reaches A (map 102)
        self.chat(b, b'Watcher : over here')
        for c in (a, b):
            self.assertEqual(P.to_bytes(c.s2c(c.expect(0x90))['text']), b'Watcher : over here')
        # Break Party from A's menu: the party dissolves
        self.send(a, 0x29)
        self.assertEqual(a.s2c(a.expect(0x51)), {'member_uid': 1})
        self.assertEqual(b.s2c(b.expect(0x51)), {'member_uid': 2})
        self.assertIsNone(self.members(a))
        self.assertIsNone(self.members(b))
        self.assertEqual(self.server.party.parties, {})
        self.assertEqual((a.session['party_hud'], b.session['party_hud']), ([0] * 4, [0] * 4))
        self.chat(a, b'TestHero : alone')              # a patched client: dropped
        a.expect_silence(0.1)
        b.expect_silence(0.05)

    def test_vitals_follow_damage_level_up_and_death(self):
        a, b = self.a, self.b
        self.formed(a, b)
        self.server.damage_player(b.session, 5)
        b.expect(0x28)
        self.assertEqual(self.tick(), 1)
        self.assertEqual(a.s2c(a.expect(0x54))['cur_hp'], b.session['hp'])
        # a level-up: the owner's 0x21, the holders' 0x22 (P5), and the frame's new maxima
        char = self.rec('Watcher')
        self.server.grant_exp(b.session, progression.exp_for_level(3) - char.get('exp', 0))
        b.expect(0x21)
        self.assertEqual(a.s2c(a.expect(0x22)), {'uid': 2, 'level': 3})
        self.assertEqual(self.tick(), 2)
        hp, mp = a.expect(0x54, 0x55)
        self.assertEqual(a.s2c(hp), {'uid': 2, 'max_hp': b.session['max_hp'], 'cur_hp': b.session['max_hp']})
        self.assertEqual(a.s2c(mp), {'uid': 2, 'max_mp': b.session['max_mp'], 'cur_mp': b.session['max_mp']})
        # death: the frame empties, the maximum stays (never 0: C11)
        self.server.damage_player(b.session, 100000)
        b.recv_until_quiet(0.3)
        a.recv_until_quiet(0.2)
        self.tick()
        self.assertEqual(a.s2c(a.expect(0x54)), {'uid': 2, 'max_hp': b.session['max_hp'], 'cur_hp': 0})

    # ------------------------------------------------------------- F4 leave ---
    def three(self):
        carol = self.carol()
        self.form(self.a, self.b)
        self.b.expect(0x4F)
        self.a.expect(0x4F)
        self.form(self.a, carol)
        self.assertEqual([self.added(carol)['member_name'] for _ in range(2)], ['TestHero', 'Watcher'])
        for c in (self.a, self.b):
            self.assertEqual(self.added(c)['member_name'], 'Carol')
        self.assertEqual([c.session['party_hud'] for c in (self.a, self.b, carol)],
                         [[2, 3, 0, 0], [1, 3, 0, 0], [1, 2, 0, 0]])
        return carol

    def test_the_first_member_leaves_and_the_others_frames_are_rebuilt(self):
        """C51 / F4 step 5: 0x51 {A} empties frame 1 on B and C while frame 2 stays - their
        clients would count themselves partyless - so each gets 0x51 self + 0x4F of the
        other remaining member: frame 1 bound again."""
        carol = self.three()
        a, b = self.a, self.b
        self.send(a, 0x29)
        self.assertEqual(a.s2c(a.expect(0x51)), {'member_uid': 1})
        for c, me, other in ((b, 2, 'Carol'), (carol, 3, 'Watcher')):
            gone, clear, add = c.expect(0x51, 0x51, 0x4F)
            self.assertEqual((c.s2c(gone), c.s2c(clear)), ({'member_uid': 1}, {'member_uid': me}))
            self.assertEqual(_name(c.s2c(add)['member_name']), other)
        self.assertEqual((b.session['party_hud'], carol.session['party_hud']), ([3, 0, 0, 0], [2, 0, 0, 0]))
        self.assertEqual(self.members(b), ['Watcher', 'Carol'])
        self.chat(carol, b'Carol : still here')
        for c in (b, carol):
            c.expect(0x90)
        a.expect_silence(0.1)
        # Carol closes her client: Watcher is alone - the party dissolves
        carol.close()
        pkts = b.recv_until_quiet(0.4)
        self.assertIn(0x06, _ops(pkts))
        self.assertEqual([b.s2c(p) for p in pkts if p.opcode == 0x51], [{'member_uid': 2}])
        self.assertEqual(self.server.party.parties, {})

    def test_the_last_member_leaves_without_a_rebuild(self):
        carol = self.three()
        self.send(carol, 0x29)
        self.assertEqual(carol.s2c(carol.expect(0x51)), {'member_uid': 3})
        for c, hud in ((self.a, [2, 0, 0, 0]), (self.b, [1, 0, 0, 0])):
            self.assertEqual(c.s2c(c.expect(0x51)), {'member_uid': 3})
            self.assertEqual(c.session['party_hud'], hud)
        self.assertEqual(self.members(self.a), ['TestHero', 'Watcher'])
        self.send(carol, 0x29)                            # in no party: its frames are cleared
        self.assertEqual(carol.s2c(carol.expect(0x51)), {'member_uid': 3})

    def test_a_disconnect_leaves_the_party(self):
        a, b = self.a, self.b
        self.formed(a, b)
        b.close()
        pkts = a.recv_until_quiet(0.4)
        self.assertEqual([a.s2c(p) for p in pkts if p.opcode == 0x51], [{'member_uid': 1}])
        self.assertIsNone(self.members(a))
        # the relogged character starts partyless and can be invited again
        b = self.login('admin', 'admin', 'Watcher', 2)
        a.recv_until_quiet(0.3)
        self.form(a, b)
        self.assertEqual(self.added(b)['member_name'], 'TestHero')
        self.assertEqual(self.added(a)['member_name'], 'Watcher')

    # ------------------------------------------------------------ refusals ---
    def test_invite_refusals(self):
        a, b = self.a, self.b
        for uid in (99, 1):                                   # nobody / oneself
            self.send(a, 0x27, {'target_uid': uid})
            self.assertEqual(self.notice(a), '[Warning] ' + PT.NOT_ONLINE_TEXT)
        self.send(b, 0x40, {'refuse_party': 1})               # Options: refuse party
        self.assertTrue(_wait(lambda: b.session.get('refuse', {}).get('party')))
        self.send(a, 0x27, {'target_uid': 2})
        self.assertEqual(self.notice(a), '[Warning] Watcher is refusing party invitations.')
        b.expect_silence(0.1)
        self.send(b, 0x40, {})
        self.assertTrue(_wait(lambda: not b.session.get('refuse', {}).get('party')))
        b.session['level'] = 12                               # 1 vs 12: over 10 apart
        self.send(a, 0x27, {'target_uid': 2})
        self.assertEqual(a.expect(0x56).payload, b'')         # "... must be within 10."
        b.expect_silence(0.1)
        b.session['level'] = 11
        self.formed(a, b)
        # Make Party on a member of our own party (a stale frame): the frames are rebuilt
        self.send(a, 0x27, {'target_uid': 2})
        clear, add = a.expect(0x51, 0x4F)
        self.assertEqual((a.s2c(clear), a.s2c(add)['member_uid']), ({'member_uid': 1}, 2))
        b.expect_silence(0.1)
        # someone else's invite to a member: 0x50 "The player is already in a party."
        carol = self.carol()
        self.send(carol, 0x27, {'target_uid': 2})
        self.assertEqual(carol.expect(0x50).payload, b'')
        # full: five members (four stand-ins without a client)
        party = self.server.party.party_of(a.session)
        party.members.extend({'uid': 90 + i, 'char_name': f'Stub{i}', 'level': 1} for i in range(3))
        self.send(a, 0x27, {'target_uid': 3})
        self.assertEqual(self.notice(a), '[Warning] ' + PT.FULL_TEXT)
        carol.expect_silence(0.1)

    def test_accept_refusals(self):
        a, b = self.a, self.b
        self.send(b, 0x28, {'inviter_name': 'TestHero'})      # nothing pending
        self.assertEqual(self.notice(b), '[Warning] ' + PT.EXPIRED_TEXT)
        self.send(a, 0x27, {'target_uid': 2})
        b.expect(0x4E)
        self.send(a, 0x27, {'target_uid': 2})                 # a repeat re-opens the dialog
        b.expect(0x4E)
        with self.server.party.lock:
            self.server.party.invites[2]['testhero'].t -= PT.INVITE_TTL
        self.send(b, 0x28, {'inviter_name': 'TestHero'})      # 60 s later
        self.assertEqual(self.notice(b), '[Warning] ' + PT.EXPIRED_TEXT)
        # the level gap is checked again at accept time: 0x56 to both
        self.send(a, 0x27, {'target_uid': 2})
        b.expect(0x4E)
        b.session['level'] = 20
        self.send(b, 0x28, {'inviter_name': 'testhero'})      # the echo may differ in case
        self.assertEqual(b.expect(0x56).payload, b'')
        self.assertEqual(a.expect(0x56).payload, b'')
        b.session['level'] = 1
        # B formed another party meanwhile: "You are already in a party."
        carol = self.carol()
        self.send(a, 0x27, {'target_uid': 2})
        b.expect(0x4E)
        self.form(b, carol)
        carol.expect(0x4F)
        b.expect(0x4F)
        self.send(b, 0x28, {'inviter_name': 'TestHero'})
        self.assertEqual(self.notice(b), '[Warning] ' + PT.ALREADY_TEXT)
        self.assertEqual(self.members(b), ['Watcher', 'Carol'])
        # the inviter left before the answer: 0x53 "<name>is not in server."
        self.send(b, 0x29)
        b.recv_until_quiet(0.2)
        carol.recv_until_quiet(0.1)
        self.send(a, 0x27, {'target_uid': 2})
        b.expect(0x4E)
        a.close()
        b.recv_until_quiet(0.3)                               # 0x06
        self.send(b, 0x28, {'inviter_name': 'TestHero'})
        pkt = b.expect(0x53)
        self.assertEqual(pkt.payload, P.name17('TestHero'))

    # ------------------------------------------------------------- chat ---
    def test_party_chat_rebuilds_the_name_and_obeys_the_gates(self):
        a, b = self.a, self.b
        self.formed(a, b)
        self.chat(a, b'Nobody : spoofed')
        for c in (a, b):
            self.assertEqual(P.to_bytes(c.s2c(c.expect(0x90))['text']), b'TestHero : spoofed')
        long = b'TestHero : ' + b'x' * 90
        self.chat(a, long)
        for c in (a, b):
            self.assertEqual(len(P.to_bytes(c.s2c(c.expect(0x90))['text'])), PT.CHAT_MAX)
        with self.server.store.lock:
            self.server.store.account('test')['manner'] = -40
        self.chat(a, b'TestHero : muted')
        a.expect_silence(0.1)
        b.expect_silence(0.05)

    # --------------------------------------------------------- map change ---
    def test_frames_persist_across_both_members_portals(self):
        a, b = self.a, self.b
        self.formed(a, b)
        self.portal(a)
        b.recv_until_quiet(0.2)
        self.portal(b)
        a.recv_until_quiet(0.2)
        self.assertEqual((a.session['party_hud'], b.session['party_hud']), ([2, 0, 0, 0], [1, 0, 0, 0]))
        self.assertEqual(self.tick(), 4)                      # each re-synced with the other
        for c, other in ((a, 2), (b, 1)):
            hp, mp = c.expect(0x54, 0x55)
            self.assertEqual((c.s2c(hp)['uid'], c.s2c(mp)['uid']), (other, other))

    # ----------------------------------------------------------- exp share ---
    def test_a_kill_is_shared_with_the_members_on_the_map(self):
        """party-exp-share over the P5 shared monsters: the killer (last hit, on the map)
        gets his 0x21 at once, each other member on that map his share from the tick
        thread; 20 % bonus per extra member, split evenly."""
        a, b = self.a, self.b
        self.formed(a, b)
        self.portal(a)
        self.portal(b)
        self.mc.drain()
        exp_a, exp_b = self.rec('TestHero').get('exp', 0), self.rec('Watcher').get('exp', 0)
        mob = self.kill_one(a)
        want = PT.split_exp(mob.exp, 2, 20)
        self.assertEqual(self.exp_of(a.recv_until_quiet(0.3)), [want[0]])
        self.assertEqual(self.exp_of(b.recv_until_quiet(0.2)), [])           # deferred
        self.server.ticks.run_due()
        self.assertEqual(self.exp_of(b.recv_until_quiet(0.3)), [want[1]])
        self.assertEqual((self.rec('TestHero')['exp'], self.rec('Watcher')['exp']),
                         (exp_a + want[0], exp_b + want[1]))
        # a member on another map gets nothing; the killer gets the whole kill
        b.send(0x7E, self.back_portal())
        self.assertTrue(_wait(lambda: self.server.world.map_of(b.session) == 101))
        self.mc.drain()
        mob = self.kill_one(a)
        self.assertEqual(self.exp_of(a.recv_until_quiet(0.3)), [mob.exp])
        self.server.ticks.run_due()
        self.assertEqual(self.exp_of(b.recv_until_quiet(0.2)), [])

    # ------------------------------------------------------------- dev view ---
    def test_dev_party_command(self):
        """`!party` (wsdev dev <char> '!party ...' in live checks): the server view, and the
        same invite / accept / say / leave paths as C2S 0x27 / 0x28 / 0x6A / 0x29. Every
        command answers its GM with one 0x15 line."""
        a, b = self.a, self.b
        a.session['gm'] = b.session['gm'] = 1

        def dev(c, line):
            line = line.encode()
            self.send(c, 0x03, {'msg_len': len(line), 'message': line})
        dev(a, '!party invite watcher')
        self.assertEqual(_name(b.s2c(b.expect(0x4E))['inviter_name']), 'TestHero')
        self.assertIn(b'invited', P.to_bytes(a.s2c(a.expect(0x15))['text']))
        dev(b, '!party accept TestHero')
        b.expect(0x4F, 0x15)
        a.expect(0x4F)
        dev(a, '!party')
        lines = [P.to_bytes(a.s2c(p)['text']) for p in a.recv_until_quiet(0.3)]
        self.assertTrue(any(b'TestHero(uid 1) map 101' in t for t in lines), lines)
        self.assertTrue(any(b'Watcher(uid 2) map 101' in t for t in lines), lines)
        self.assertTrue(any(b'frames: [2, 0, 0, 0]' in t for t in lines), lines)
        dev(a, '!party say hello all')
        a.expect(0x90, 0x15)
        self.assertEqual(P.to_bytes(b.s2c(b.expect(0x90))['text']), b'TestHero : hello all')
        dev(b, '!party leave')
        self.assertEqual(b.s2c(b.expect(0x51, 0x15)[0]), {'member_uid': 2})
        self.assertEqual(a.s2c(a.expect(0x51)), {'member_uid': 1})
        dev(a, '!party invite Nobody')
        self.assertIn(b'not online', P.to_bytes(a.s2c(a.expect(0x15))['text']).lower())


class _Priest(_Base):
    """TestHero is a Lv40 priest with a Cleric Wand and Group Heal / Heal learned."""

    def prepare(self):
        with self.server.store.lock:
            ch = self.rec('TestHero')
            ch.update({'class': 6, 'exp': progression.exp_for_level(40), 'skills': [GROUP_HEAL, HEAL],
                       'equipped': {5: {'id': CLERIC_WAND, 'w': [0] * 6}}})
            w = self.rec('Watcher')
            w['exp'] = progression.exp_for_level(35)
        self.server.store.mark_dirty('test setup')

    def cast(self, c, skill):
        self.send(c, 0x15, {'skill_id': skill}, name='UseSkill')

    def test_exit6_group_heal_heals_the_member_and_its_frame(self):
        """Exit criterion 6 (Group Heal half; party-skill-hooks): the caster's 0x25 heals
        the party entities in ITS scene client-side; each other same-map member gets S2C
        0x92 {id} (its client heals its own party slots and itself) and the absolute 0x28
        pin; the next tick moves every frame (0x54)."""
        a, b = self.a, self.b
        self.formed(a, b)
        self.server._set_vitals(a.session, hp=100, mp=a.session['max_mp'])
        self.server._set_vitals(b.session, hp=10)
        a.expect(0x28, 0x44)
        b.expect(0x28)
        self.tick()
        self.mc.drain()
        self.cast(a, GROUP_HEAL)
        self.assertEqual(_ops(a.expect(0x25, 0x28, 0x44)), [0x25, 0x28, 0x44])
        self.assertEqual(a.session['hp'], 152)
        effect, pin = b.expect(0x92, 0x28)
        self.assertEqual((b.s2c(effect), b.s2c(pin)), ({'skill_id': GROUP_HEAL}, {'hp': 62}))
        self.assertEqual(b.session['hp'], 62)
        self.assertEqual(self.tick(), 3)                      # B: A's HP + MP; A: B's HP
        self.assertEqual(a.s2c(a.expect(0x54)), {'uid': 2, 'max_hp': b.session['max_hp'], 'cur_hp': 62})
        self.assertEqual(_ops(b.expect(0x54, 0x55)), [0x54, 0x55])
        # a member on another map is not reached
        self.portal(b)
        self.mc.drain()
        self.server._set_vitals(a.session, hp=100, mp=a.session['max_mp'])
        a.expect(0x28, 0x44)
        a.session['skill_cd'].clear()
        self.cast(a, GROUP_HEAL)
        a.expect(0x25, 0x28, 0x44)
        b.expect_silence(0.15)
        self.assertEqual(b.session['hp'], 62)

    def test_a_heal_lands_on_the_weakest_member_in_range(self):
        """party-skill-hooks F3c interim: Heal has no target on the wire; with a party it goes
        to the lowest-HP% member within 400 px on the map (S2C 0x3D on him, 0x40 to the
        observers), else the caster."""
        a, b = self.a, self.b
        self.formed(a, b)
        self.server._set_vitals(b.session, hp=10)
        b.expect(0x28)
        self.mc.drain()
        self.cast(a, HEAL)
        a.expect(0x25, 0x44, 0x40)                            # cost; the heal visual on B
        self.assertEqual(b.s2c(b.expect(0x3D)), {'has_hp': 1, 'has_mp': 0, 'hp': 50})
        self.assertEqual(b.session['hp'], 50)
        # out of range: the caster heals himself
        self.server._set_vitals(a.session, hp=100)
        a.expect(0x28)
        b.session['pos'] = (5000.0, 714.0)
        a.session['skill_cd'].clear()
        self.cast(a, HEAL)
        a.expect(0x25, 0x44, 0x3D)
        self.assertEqual(a.session['hp'], 140)


class _Rebuild(_Base):
    config = {'PARTY_HUD_REBUILD_ON_MAP_LOAD': True}

    def test_the_flag_rebuilds_the_frames_after_a_map_load(self):
        a, b = self.a, self.b
        self.formed(a, b)
        burst = self.portal(a)
        ops = _ops(burst)
        i = ops.index(0x51)
        self.assertEqual(ops[i:i + 2], [0x51, 0x4F])
        self.assertLess(ops.index(0x07), i)
        self.assertEqual(a.s2c(burst[i]), {'member_uid': 1})
        self.assertEqual(a.session['party_hud'], [2, 0, 0, 0])


class _NoShare(_Base):
    config = {'PARTY_EXP_SHARE': False}

    def test_the_flag_off_keeps_the_whole_kill_with_the_killer(self):
        a, b = self.a, self.b
        self.formed(a, b)
        self.portal(a)
        self.portal(b)
        self.mc.drain()
        mob = self.kill_one(a)
        self.assertEqual(self.exp_of(a.recv_until_quiet(0.3)), [mob.exp])
        self.server.ticks.run_due()
        self.assertEqual(self.exp_of(b.recv_until_quiet(0.2)), [])


class Party2008(_Party, unittest.TestCase):
    build = B8


class Party2009(_Party, unittest.TestCase):
    build = B9

    def test_a_2009_relogin_leaves_the_party(self):
        """The 2009 client reconnects with its session key (channel change): the old
        session is superseded and leaves the party (0x51 dissolve on A); the new one is
        partyless."""
        a, b = self.a, self.b
        self.formed(a, b)
        old = b.session
        c = F.FakeClient(self.server)
        self.extra.append(c)
        self.assertEqual(c.login('admin', 'admin', session_key=old['session_key'])['result'], 1)
        self.assertTrue(_wait(lambda: old.get('closed')))
        pkts = a.recv_until_quiet(0.4)
        self.assertEqual([a.s2c(p) for p in pkts if p.opcode == 0x51], [{'member_uid': 1}])
        c.enter_world('Watcher', port=F.P2P_PORT_BASE + 1)
        self.assertIsNone(self.server.party.party_of(c.session))


class Priest2008(_Priest, unittest.TestCase):
    build = B8


class Priest2009(_Priest, unittest.TestCase):
    build = B9


class Rebuild2008(_Rebuild, unittest.TestCase):
    build = B8


class Rebuild2009(_Rebuild, unittest.TestCase):
    build = B9


class NoShare2008(_NoShare, unittest.TestCase):
    build = B8


class NoShare2009(_NoShare, unittest.TestCase):
    build = B9


if __name__ == '__main__':
    unittest.main(verbosity=1)
