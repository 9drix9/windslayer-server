#!/usr/bin/env python3
"""
test_guild_flows.py - P14 part 2 (ROADMAP_2009_ADDENDUM P14 items guild-g2, guild-g3, guild-g5,
guild-g4; systems_2009/guild.md F1-F12), both client builds
=========================================================================================
  guild-g2   Create at Moiba (9702): every sub 1 refusal in F1's order (other guild 5, GM 4,
             map 3, name 2 incl. a duplicate, gold 7, level 8, 2nd class 9, emblem 3), the
             success (50,000 gold, sub 1 {1, id} + sub 3 n=0 + 0xB4 to the holders, the mirror,
             guilds.json), the relog (still in it). Disband 0x8E(3): 0xA with members, 0xB under
             GUILD_DISBAND_MIN_DAYS, 1 + 0xB7 + 0xB8.
  guild-g3   Apply 0x89 (u32 menu / u16 board) -> sub 2 1 / 0x10 push / 0x11 / 6 / 0, FIFO and
             one per applicant, accept -> sub 5 + sub 13 + sub 3 to the new member + 0xB4, the
             new member's 0x8A after an admission gets no second sub 3, reject pops the first,
             the master's sub 4 after his relog; leave (sub 6 / 14 / 0xB7, master 0, 0xC under
             GUILD_LEAVE_MIN_HOURS), kick (sub 18 / 14 / 6 / 0xB7); deltas only to clients that
             hold the window (a member in a map load gets them in its sub 3, never twice).
             Login / logout lines sub 20 / 21 with the guild points credited, and a 2009 channel
             hop that sends neither but keeps the points for the real logout.
  guild-g5   Chat 0x8D -> 0xB5 to the OTHER members only (real name, <= 87 B, 700 ms, wrong
             guild / outsider dropped, blacklist skip, NEVER_REPLY), across channels; the
             GUILD_POINTS_BASE 'post' policy.
  guild-g4   Grades 0x92 (sub 16 exactly once, 4 Guardians / 10 Vanguards, who may:
             GUILD_GRADE_MIN_GRADE), master change 0x91 (sub 11 / 12, old master Trainee, sub 4
             to the new master, 8 for a non-member / GUILD_MASTER_NEEDS_CREATE_RULES), notice
             0x8F (sub 7, read at the next sub 3), capacity 0x90 (server-side tier / level /
             cost / gold, sub 9 + sub 10), the rename hook (sub 22).
  2008       no route, no hook, no guild packet; the backstop / never-reply rows are 2009 only.

Fake clients (fakeclient.MultiClient); no port is bound, no client is started, and the live
accounts.json / guilds.json are never opened (temp copies; the module checks both).
"""
import hashlib
import logging
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import clientview as CV  # noqa: E402
import fakeclient as F  # noqa: E402
import guild as G  # noqa: E402
import packets as P  # noqa: E402
import progression  # noqa: E402
import registry  # noqa: E402
import world as WM  # noqa: E402

W = F.import_server()
cfgmod = W.cfgmod
EC = W.EC
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
LIVE_GUILDS = os.path.join(HERE, 'guilds.json')
B8, B9 = cfgmod.BUILD_2008, cfgmod.BUILD_2009
DIRS = {B8: cfgmod.DEFAULTS['CLIENT_DIR'], B9: cfgmod.DEFAULTS['CLIENT_DIR_2009']}
HAVE = {b: os.path.exists(os.path.join(HERE, d, 'hs', 'windslayer.hii')) for b, d in DIRS.items()}

FRIENDS, CARDS, GUILD_INFO = '0x45353B/0x2F', '0x453557/0x63', '0x453581/0x8A'
CREATE, APPLY_MENU, APPLY_BOARD = '0x48451D/0x87', '0x482D64/0x89', '0x482DCD/0x89'
ACCEPT, REJECT, CHAT, ACTION = '0x484772/0x8B', '0x484707/0x8C', '0x47AD4F/0x8D', '0x484C32/0x8E'
NOTICE, CAPACITY, MASTER, GRADE = '0x484CD8/0x8F', '0x48306B/0x90', '0x484C87/0x91', '0x485447/0x92'
ENTER_RELOGIN = '0x452133/0x2B'
IP = '127.0.0.1'
PLAZA = (G.GUILD_PLAZA_MAP, 2200.0, 1300.0)        # Moiba's tile (guild.md 1.5)
LV30 = progression.exp_for_level(30)
CHANNELS = [{'no': 1, 'port': 7022}, {'no': 2, 'port': 7023}, {'no': 3, 'port': 7024}]

_LIVE = None


def _sha(path):
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def setUpModule():
    global _LIVE
    _LIVE = (_sha(LIVE_ACCOUNTS), _sha(LIVE_GUILDS))
    P.STRICT_FIELDS.add(B9)                 # every 2009 S2C must use the 2009 field names


def tearDownModule():
    P.STRICT_FIELDS.discard(B9)
    EC.configure(DIRS[B8], B8)
    assert (_sha(LIVE_ACCOUNTS), _sha(LIVE_GUILDS)) == _LIVE, \
        'accounts.json / guilds.json changed during test_guild_flows.py (tests must only use temp copies)'


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _char(name, exp=0):
    return {'name': name, 'level': 1, 'class': 0, 'exp': exp, 'map': 101, 'x': 1411.0,
            'y': 714.0, 'hp': 100, 'mp': 50}


def _accounts():
    return {'test': {'password': 'test', 'characters': [_char('TestHero', exp=LV30)]},
            'admin': {'password': 'admin', 'characters': [_char('Watcher')]},
            'carol': {'password': 'carol', 'characters': [_char('Carol', exp=LV30)]},
            'dave': {'password': 'dave', 'characters': [_char('Dave')]}}


# class / tier and gold after the load: TestHero (A) and Carol (C) can create, Watcher (B) not.
SETUP = {'TestHero': (2, 1, 60000), 'Watcher': (1, 0, 100), 'Carol': (3, 1, 60000), 'Dave': (0, 0, 0)}


def _b3(c, pkts):
    return [c.s2c(p) for p in pkts if p.opcode == 0xB3]


class _Rig:
    """TestHero (A, uid 1), Watcher (B, uid 2) and Carol (C, uid 3) in world on map 101; Dave
    offline. A and C are Lv 30 with a 2nd class and 60,000 gold."""
    build = B9
    players = (('test', 'test', 'TestHero'), ('admin', 'admin', 'Watcher'), ('carol', 'carol', 'Carol'))
    extra_config = {}

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        self.tmp = tempfile.mkdtemp(prefix=f'ws_p14g_{self.build}_')
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False,
                                **self.extra_config})
        self.server = F.make_server(self.tmp, accounts=_accounts(), config=cfg)
        with self.server.store.lock:
            for name, (cls, job2, gold) in SETUP.items():
                char = self.server.store.character_by_name(name)[2]
                char['class'], char['job2'], char['gold'] = cls, job2, gold
        self.extra = []
        self.mc = F.MultiClient(self.server, players=self.players)
        self.a, self.b, self.c = self.mc
        _wait(lambda: len(self.server.world.peers(self.a.session)) == 2)
        self.mc.drain(0.3)

    def tearDown(self):
        for c in self.extra:
            try:
                c.close()
            except OSError:
                pass
        self.mc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers ---
    def char(self, name):
        return self.server.store.character_by_name(name)[2]

    def send(self, c, key, fields=None):
        c.send_c2s(key, fields or {})

    def b3(self, c, quiet=0.3):
        return _b3(c, c.recv_until_quiet(quiet))

    def resync(self, c):
        """What the 2009 client sends at the end of every 0x03; the 0xB3 replies."""
        c.send_c2s(FRIENDS)
        c.send_c2s(CARDS)
        c.send_c2s(GUILD_INFO)
        return self.b3(c)

    def warp(self, c, map_code, x, y, sync=True):
        """A server map load (as `!warp` runs it), then (sync) the client's resync."""
        s = c.session
        c.recv_until_quiet(0.2)
        self.server._map_transfer(s['sock'], s, map_code, x, y, reason='test warp')
        pkts = c.recv_until_quiet(0.3)
        self.assertEqual([p.opcode for p in pkts][:3], [0x08, 0x03, 0x07])
        return self.resync(c) if sync else pkts

    def to_plaza(self, *clients):
        for c in clients:
            self.warp(c, *PLAZA)
        self.mc.drain(0.3)

    def create(self, c, name='Testers', fg=18, bg=18):
        self.send(c, CREATE, {'guild_name': name, 'master_char_id': P.session_uid(c.session) or 0,
                              'emblem_symbol': fg, 'emblem_bg': bg})
        return self.b3(c)

    def seed(self, name, master, *members):
        """`!guild seed` + a map load and resync of every member in world (their windows)."""
        g = self.server.guilds.seed(name, master, list(members))
        for c in self.mc:
            if c.char_name in (master,) + members and c.session and c.session.get('in_world'):
                self.warp(c, c.session['current_map'], *c.session['pos'])
        self.mc.drain(0.3)
        return g

    def apply(self, c, gid, board=False):
        self.send(c, APPLY_BOARD if board else APPLY_MENU, {'guild_id': gid})
        return self.b3(c)

    def action(self, c, gid, act, name='', uid=0):
        self.send(c, ACTION, {'guild_id': gid, 'char_id': uid, 'char_name': name, 'action': act})

    def chat(self, c, gid, text):
        raw = text.encode('latin-1')
        self.send(c, CHAT, {'guild_id': gid, 'text_len': len(raw), 'text': raw})

    def guild(self, name='Testers'):
        return self.server.guilds.get(name)

    def gm(self, c, line):
        """A '!' line typed by `c` (a GM): (its 0xB3 payloads, its 0x15 texts), in order."""
        self.server._gm_chat_command(c.session['sock'], c.session, line.encode())
        got = c.recv_until_quiet(0.3)
        return ([p.payload for p in got if p.opcode == 0xB3],
                [P.to_bytes(c.s2c(p)['text']).decode('latin-1') for p in got if p.opcode == 0x15])

    def rename(self, c, account, old, new):
        """The rename ticket's two halves (store + ON_RENAME), as test_rename_hook runs them."""
        self.server.store.rename_character(account, old, new, save=False)
        c.session['char_name'] = new
        self.server.world.enter(c.session, new)
        self.server.world.hooks.fire(WM.ON_RENAME, self.server, c.session, old=old, new=new,
                                     cid=self.char(new)['cid'])
        self.mc.drain(0.3)

    def raw_resync(self, c):
        """A map load in place + the client's resync: the raw 0xB3 payloads."""
        self.warp(c, c.session['current_map'], *c.session['pos'], sync=False)
        c.send_c2s(FRIENDS)
        c.send_c2s(CARDS)
        c.send_c2s(GUILD_INFO)
        return [p.payload for p in c.recv_until_quiet(0.3) if p.opcode == 0xB3]

    def gold(self, name):
        with self.server.store.lock:
            return self.char(name)['gold']


def _sub4_names(payload):
    """The applicant names of a raw 0xB3 sub 4 payload, in order."""
    return [G.parse_application(payload[2 + 130 * i:2 + 130 * (i + 1)])[0] for i in range(payload[1])]


# =============================================================== guild-g2 ===
class Create2009(_Rig, unittest.TestCase):
    def test_every_create_refusal_in_order(self):
        a, b = self.a, self.b
        # not in the Guild Plaza: "Fail to register the guild."
        self.assertEqual(self.create(a), [{'sub': 1, 's1_result': 3}])
        self.to_plaza(a, b)
        cases = (
            (lambda: self.char('TestHero').update(gold=49999), {'sub': 1, 's1_result': 7}),
            (lambda: self.char('TestHero').update(gold=60000, exp=LV30 - 1), {'sub': 1, 's1_result': 8}),
            (lambda: self.char('TestHero').update(exp=LV30, job2=0), {'sub': 1, 's1_result': 9}),
            (lambda: self.char('TestHero').update(job2=1), None),
        )
        for change, want in cases:
            with self.server.store.lock:
                change()
            if want is not None:
                self.assertEqual(self.create(a), [want])
        self.assertEqual(self.create(a, 'bad name'), [{'sub': 1, 's1_result': 2}])
        self.assertEqual(self.create(a, 'GameMasters'), [{'sub': 1, 's1_result': 2}])  # reserved word
        self.assertEqual(self.create(a, ''), [{'sub': 1, 's1_result': 2}])
        # the emblem: fg 0..999 (sprite 0x148), bg 0..599 (sprite 0x149); an empty picker cell
        # gives more and is refused (live triage G3)
        self.assertEqual(self.create(a, 'Testers', fg=1000), [{'sub': 1, 's1_result': 3}])
        self.assertEqual(self.create(a, 'Testers', bg=600), [{'sub': 1, 's1_result': 3}])
        self.assertEqual(self.create(a, 'Testers', bg=1009), [{'sub': 1, 's1_result': 3}])
        self.assertEqual(self.gold('TestHero'), 60000)                    # the gate runs before the debit
        # a visible GM (the client itself says "Wind master can't register as guild.")
        a.session['gm'], a.session['gm_hidden'] = 1, 0
        self.assertEqual(self.create(a), [{'sub': 1, 's1_result': 4}])
        a.session['gm'] = 0
        self.assertEqual(self.gold('TestHero'), 60000)                    # nothing debited
        self.assertEqual(self.server.guilds.all(), [])
        self.assertEqual(CV.guild_id(a.session), 0)

    def test_create_relog_and_the_plates(self):
        a, b, c = self.a, self.b, self.c
        self.to_plaza(a, b, c)
        with self.assertNoLogs('WS', logging.ERROR):
            self.send(a, CREATE, {'guild_name': 'Testers', 'master_char_id': 1, 'emblem_symbol': 18, 'emblem_bg': 20})
            got = a.recv_until_quiet(0.3)
        self.assertEqual([p.opcode for p in got], [0xB3, 0xB3])
        self.assertEqual(a.s2c(got[0]), {'sub': 1, 's1_result': 1, 's1_guild_id': 2})
        info = a.s2c(got[1])
        self.assertEqual((info['sub'], info['guild_id'], info['guild_name'], info['max_members'], info['member_count'],
                          info['emblem_fg'], info['emblem_bg'], info['guild_notice']),
                         (3, 2, 'Testers', 15, 0, 18, 20, ''))            # n = 0: the pre-fill stays alone
        v = CV.view(a.session)
        self.assertEqual((v.guild_id, v.guild_list, v.apps_held, v.guild_rows), (2, 2, True, False))
        self.assertEqual(self.gold('TestHero'), 10000)                    # 50,000 debited (client: locally)
        # the holders of A see the tag at once (0xB4); A's own plate came with the sub 3
        for peer in (b, c):
            tags = [peer.s2c(p) for p in peer.recv_until_quiet(0.3) if p.opcode == 0xB4]
            self.assertEqual(tags, [{'uid': 1, 'guild_id': 2, 'guild_name': 'Testers', 'emblem_fg': 18, 'emblem_bg': 20}])
        g = self.guild()
        self.assertEqual(([m['name'] for m in g.members], g.master()['name'], g.points, g.max_members),
                         (['TestHero'], 'TestHero', 0, 15))
        # the name is taken (case-insensitively); A is in a guild now
        self.assertEqual(self.create(c, 'TESTERS'), [{'sub': 1, 's1_result': 2}])
        self.assertEqual(self.create(a, 'Other'), [{'sub': 1, 's1_result': 5}])
        self.assertEqual(self.gold('Carol'), 60000)
        # guilds.json, and a relog: still in the guild (own record + sub 3 with the member)
        self.server.guilds.flush()
        self.assertTrue(os.path.exists(os.path.join(self.tmp, 'guilds.json')))
        store = G.GuildStore(os.path.join(self.tmp, 'guilds.json'))
        store.load(self.server.store)
        self.assertEqual([x.name for x in store.guilds.values()], ['Testers'])
        replies = self.warp(a, *PLAZA)
        self.assertEqual([(r['sub'], r['member_count']) for r in replies], [(3, 1)])
        self.assertEqual(replies[0]['repeat[member_count]'][0]['member_grade'], 5)

    def test_create_accepts_every_picker_value(self):
        """Live triage G3: fg = shape*20 + colour (0..999), bg = pattern*20 + colour (0..599)."""
        a, b = self.a, self.b
        self.assertEqual((G.EMBLEM_FG_MAX, G.EMBLEM_BG_MAX), (49 * 20 + 19, 29 * 20 + 19))
        self.to_plaza(a, b)
        self.send(a, CREATE, {'guild_name': 'Testers', 'master_char_id': 1, 'emblem_symbol': 999, 'emblem_bg': 599})
        got = [a.s2c(p) for p in a.recv_until_quiet(0.3) if p.opcode == 0xB3]
        self.assertEqual(got[0], {'sub': 1, 's1_result': 1, 's1_guild_id': 2})
        self.assertEqual((got[1]['sub'], got[1]['emblem_fg'], got[1]['emblem_bg']), (3, 999, 599))
        tags = [p for p in b.recv_until_quiet(0.3) if p.opcode == 0xB4]
        self.assertEqual(len(tags), 1)
        self.assertEqual(b.s2c(tags[0])['emblem_fg'], 999)
        self.assertEqual(tags[0].payload[-4:], struct.pack('<HH', 999, 599))   # u16 fg, u16 bg last
        self.assertEqual((self.guild().emblem_fg, self.guild().emblem_bg), (999, 599))
        self.assertEqual(self.gold('TestHero'), 10000)
        # the live pair: star / yellow (83 = shape 4, colour 3) on pattern 18, colour 16 (376)
        self.action(a, 2, G.ACT_DISBAND, 'TestHero', 1)
        self.assertEqual(self.b3(a)[0], {'sub': 8, 's8_result': 1})
        self.mc.drain(0.3)
        with self.server.store.lock:
            self.char('TestHero')['gold'] = 60000
        self.send(a, CREATE, {'guild_name': 'Testers', 'master_char_id': 1, 'emblem_symbol': 83, 'emblem_bg': 376})
        got = [a.s2c(p) for p in a.recv_until_quiet(0.3) if p.opcode == 0xB3]
        self.assertEqual((got[0]['s1_result'], got[1]['emblem_fg'], got[1]['emblem_bg']), (1, 83, 376))
        tags = [p for p in b.recv_until_quiet(0.3) if p.opcode == 0xB4]
        self.assertEqual(tags[0].payload[-4:], struct.pack('<HH', 83, 376))
        # stored and reloaded as is (in range: the loader's clamp changes nothing)
        self.server.guilds.flush()
        store = G.GuildStore(os.path.join(self.tmp, 'guilds.json'))
        store.load(self.server.store)
        self.assertEqual(([(g.emblem_fg, g.emblem_bg) for g in store.guilds.values()], store.migration_changes),
                         ([(83, 376)], []))

    def test_disband(self):
        a, b = self.a, self.b
        self.to_plaza(a, b)
        self.create(a)
        self.mc.drain(0.3)
        self.server.guilds.add_application('Testers', 'Carol')            # dropped with the guild
        self.apply(b, 2)
        self.mc.drain(0.3)
        self.send(a, ACCEPT, {'applicant_name': 'Watcher'})
        self.mc.drain(0.3)
        # Break Guild with a member left: 0xA (the client refuses it itself first)
        self.action(a, 2, G.ACT_DISBAND, 'TestHero', 1)
        self.assertEqual(self.b3(a), [{'sub': 8, 's8_result': 0x0A}])
        # a non-master's "Break Guild": silent 0
        self.action(b, 2, G.ACT_DISBAND, 'Watcher', 2)
        self.assertEqual(self.b3(b), [{'sub': 8, 's8_result': 0}])
        self.action(a, 2, G.ACT_KICK, 'Watcher', 2)
        self.mc.drain(0.3)
        self.action(a, 2, G.ACT_DISBAND, 'TestHero', 1)
        got = a.recv_until_quiet(0.3)
        self.assertEqual([(p.opcode, a.s2c(p)) for p in got if p.opcode in (0xB3, 0xB8)],
                         [(0xB3, {'sub': 8, 's8_result': 1}), (0xB8, {'guild_id': 2})])
        self.assertEqual(CV.guild_id(a.session), 0)
        seen = {p.opcode: b.s2c(p) for p in b.recv_until_quiet(0.3)}
        self.assertEqual(seen.get(0xB7), {'uid': 1})                       # the plate goes
        self.assertEqual(seen.get(0xB8), {'guild_id': 2})                  # its boards (guild-g6)
        self.assertEqual(self.server.guilds.all(), [])
        self.assertEqual([r['sub'] for r in self.warp(a, *PLAZA)], [15])


class Disband7Days2009(_Rig, unittest.TestCase):
    extra_config = {'GUILD_DISBAND_MIN_DAYS': 7.0}

    def test_a_young_guild_cannot_be_deleted(self):
        self.to_plaza(self.a)
        self.create(self.a)
        self.action(self.a, 2, G.ACT_DISBAND, 'TestHero', 1)
        self.assertEqual(self.b3(self.a), [{'sub': 8, 's8_result': 0x0B}])
        with self.server.guilds.lock:
            self.server.guilds.db.guilds[2].created_at -= 8 * 86400
        self.action(self.a, 2, G.ACT_DISBAND, 'TestHero', 1)
        self.assertEqual(self.b3(self.a)[0], {'sub': 8, 's8_result': 1})


# =============================================================== guild-g3 ===
class Membership2009(_Rig, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.to_plaza(self.a, self.b, self.c)
        self.create(self.a)
        self.mc.drain(0.3)

    def test_apply_push_accept_and_the_tags(self):
        a, b, c = self.a, self.b, self.c
        # B applies from the context menu (u32): result 1, and the master's synced list gets it
        self.assertEqual(self.apply(b, 2), [{'sub': 2, 's2_result': 1}])
        push = a.recv_until_quiet(0.3)
        self.assertEqual([p.opcode for p in push], [0xB3])
        self.assertEqual(push[0].payload[:2], bytes((2, 0x10)))
        self.assertEqual(G.parse_application(push[0].payload[2:]), ('Watcher', 1, 1, 0))
        # once per applicant: again = result 1, nothing new for the master
        self.assertEqual(self.apply(b, 2), [{'sub': 2, 's2_result': 1}])
        a.expect_silence()
        self.assertEqual([x['name'] for x in self.guild().applications], ['Watcher'])
        # C applies from a Guild Plaza board (u16)
        self.assertEqual(self.apply(c, 2, board=True), [{'sub': 2, 's2_result': 1}])
        self.assertEqual(len(self.b3(a)), 1)
        # accept B: sub 5 {1} + sub 13 to the window (A), B gets the whole list, the holders 0xB4
        with self.assertNoLogs('WS', logging.WARNING):
            self.send(a, ACCEPT, {'applicant_name': 'Watcher'})
            got = a.recv_until_quiet(0.3)
        self.assertEqual([(p.opcode, a.s2c(p).get('sub')) for p in got], [(0xB3, 5), (0xB3, 13), (0xB4, None)])
        self.assertEqual(a.s2c(got[0]), {'sub': 5, 's5_result': 1})
        self.assertEqual(a.s2c(got[1]), {'sub': 13, 's13_member_id': 2, 's13_member_name': 'Watcher', 's13_online': 1,
                                         's13_level': 1, 's13_job2': 0, 's13_job1': 1, 's13_grade': 1})
        self.assertEqual(a.s2c(got[2])['uid'], 2)
        mine = b.recv_until_quiet(0.3)
        info = [b.s2c(p) for p in mine if p.opcode == 0xB3]
        self.assertEqual([(i['sub'], i['member_count']) for i in info], [(3, 2)])
        self.assertEqual(CV.guild_id(b.session), 2)
        self.assertEqual([c_.s2c(p)['uid'] for c_ in (c,) for p in c.recv_until_quiet(0.3) if p.opcode == 0xB4], [2])
        self.assertEqual([(m['name'], m['grade']) for m in self.guild().members], [('TestHero', 5), ('Watcher', 1)])
        # B's next map load: its own record carries the tag, one sub 3
        replies = self.warp(b, *PLAZA)
        self.assertEqual([(r['sub'], r['member_count']) for r in replies], [(3, 2)])
        # A rejects C (the client popped its FIRST: Carol): no reply, the FIFO is empty
        self.mc.drain(0.3)
        self.send(a, REJECT)
        a.expect_silence()
        self.assertEqual(self.guild().applications, [])

    def test_gm_apply_runs_the_0x89_path_for_an_online_character(self):
        """Live triage G4: EN Build 14's player menu has no join entry (hui window 0x50 has 11
        controls), so `!guild apply` is how a live test applies - and for an ONLINE character
        it is Guilds.apply itself: the applicant's sub 2 and the 0x10 push to the synced master,
        the same bytes a raw 0x89 gives; an offline one is only queued."""
        a, b, c = self.a, self.b, self.c
        self.assertEqual(self.apply(c, 2), [{'sub': 2, 's2_result': 1}])       # the reference 0x89
        raw_push = [p.payload for p in a.recv_until_quiet(0.3) if p.opcode == 0xB3]
        self.assertEqual(G.parse_application(raw_push[0][2:]), ('Carol', 30, 3, 1))
        push, lines = self.gm(a, '!guild apply Testers Watcher')
        self.assertEqual(self.b3(b), [{'sub': 2, 's2_result': 1}])              # "You have applied ..."
        self.assertEqual([p[:2] for p in push], [raw_push[0][:2]])             # sub 2, 0x10 to the master
        self.assertEqual(G.parse_application(push[0][2:]), ('Watcher', 1, 1, 0))
        self.assertIn('2 pending', lines[0])
        self.assertIn('sub 2 result 1', lines[0])
        self.assertIn('pushed to the master', lines[1])
        self.assertEqual([x['name'] for x in self.guild().applications], ['Carol', 'Watcher'])
        # again: the 0x89 rule (already pending: result 1, no second push)
        push, lines = self.gm(a, '!guild apply Testers Watcher')
        self.assertEqual((push, self.b3(b)), ([], [{'sub': 2, 's2_result': 1}]))
        self.assertIn('already pending', lines[1])
        # a character in a guild: the 0x89 refusal 0 goes to ITS client (here the GM's own)
        push, lines = self.gm(a, '!guild apply Testers TestHero')
        self.assertEqual(push, [bytes((2, 0))])
        self.assertIn('sub 2 result 0', lines[0])
        # offline (Dave): queued only - no sub 2 to anyone, the master's next sub 4 carries it
        push, lines = self.gm(a, '!guild apply Testers Dave')
        self.assertEqual(push, [])
        self.assertIn('3 pending', lines[0])
        self.assertIn('offline', lines[0])
        b.expect_silence()
        self.assertEqual([x['name'] for x in self.guild().applications], ['Carol', 'Watcher', 'Dave'])
        # unknown names
        push, lines = self.gm(a, '!guild apply Nope Watcher')
        self.assertIn('no guild named', lines[0])
        push, lines = self.gm(a, '!guild apply Testers Nobody')
        self.assertIn('no character named', lines[0])

    def test_a_renamed_applicant_keeps_the_name_the_master_holds(self):
        """P14 review: no sub-code renames a row of the master's application list, so the row
        keeps the name his client got until his next sub 4 sync; the Accept that names it then
        finds it (the FIFO and the client list stay paired)."""
        a, b, c = self.a, self.b, self.c
        self.apply(b, 2)
        self.mc.drain(0.3)                                                 # A's list: [Watcher]
        self.rename(b, 'admin', 'Watcher', 'Watch2')
        self.assertEqual([x['name'] for x in self.guild().applications], ['Watcher'])
        self.send(a, ACCEPT, {'applicant_name': 'Watcher'})                # what A's client pops
        got = self.b3(a)
        self.assertEqual(got[0], {'sub': 5, 's5_result': 1})
        self.assertEqual(got[1]['s13_member_name'], 'Watch2')              # admitted under its name now
        self.assertEqual([m['name'] for m in self.guild().members], ['TestHero', 'Watch2'])
        self.mc.drain(0.3)
        # renamed BEFORE the master's sync: the sub 4 carries - and the FIFO keeps - the new name
        self.apply(c, 2)
        self.mc.drain(0.3)
        self.rename(c, 'carol', 'Carol', 'Caro2')
        sub4 = [p for p in self.raw_resync(a) if p[0] == 4]
        self.assertEqual([_sub4_names(p) for p in sub4], [['Caro2']])
        self.assertEqual(self.guild().applications[0]['name'], 'Caro2')
        self.send(a, ACCEPT, {'applicant_name': 'Caro2'})
        self.assertEqual(self.b3(a)[0], {'sub': 5, 's5_result': 1})
        self.assertEqual(self.guild().applications, [])

    def test_no_socket_write_under_flow_lock(self):
        """P14 review: a flow answers its requester on the requester's connection thread, where
        the flush writes the socket - it runs after flow_lock is let go (Guilds._flow)."""
        a, b, c = self.a, self.b, self.c
        gs, seen = self.server.guilds, []
        real = self.server._flush_outbox

        def spy(session):
            seen.append((session.get('char_name'), gs.flow_lock._is_owned()))
            return real(session)
        self.server._flush_outbox = spy
        self.addCleanup(lambda: self.server.__dict__.pop('_flush_outbox', None))
        self.assertEqual(self.apply(b, 2), [{'sub': 2, 's2_result': 1}])      # a flow on B's thread
        self.mc.drain(0.3)
        self.send(a, ACCEPT, {'applicant_name': 'Watcher'})                # one on A's thread
        self.assertEqual(self.b3(a)[0], {'sub': 5, 's5_result': 1})
        self.mc.drain(0.3)
        self.warp(c, *PLAZA)                                               # the 0x8A reply on C's thread
        self.assertIn('Watcher', [n for n, _owned in seen])
        self.assertIn('TestHero', [n for n, _owned in seen])
        self.assertEqual([n for n, owned in seen if owned], [])

    def test_admission_during_a_map_load_and_no_second_sub3(self):
        a, b = self.a, self.b
        self.apply(b, 2)
        self.mc.drain(0.3)
        # B's map load is out, its 0x8A not yet: the admission's sub 3 goes now ...
        self.warp(b, *PLAZA, sync=False)
        self.mc.drain(0.3)
        self.send(a, ACCEPT, {'applicant_name': 'Watcher'})
        self.mc.drain(0.3)
        # ... and that 0x8A gets no second one (no member twice), and no "not in a guild"
        with self.assertNoLogs('WS', logging.WARNING):
            self.assertEqual(self.resync(b), [])
        self.assertEqual(CV.guild_id(b.session), 2)

    def test_a_member_in_a_map_load_gets_the_change_in_its_sub3_only(self):
        a, b, c = self.a, self.b, self.c
        self.apply(b, 2)
        self.mc.drain(0.3)
        self.send(a, ACCEPT, {'applicant_name': 'Watcher'})
        self.mc.drain(0.3)
        self.apply(c, 2)
        self.mc.drain(0.3)
        self.warp(b, *PLAZA, sync=False)                                   # B: 0x03 out, no sub 3 yet
        self.mc.drain(0.3)
        self.send(a, ACCEPT, {'applicant_name': 'Carol'})
        self.assertNotIn(13, [x['sub'] for x in self.b3(b)])               # no delta on top ...
        info = self.resync(b)                                              # ... its sub 3 has Carol once
        self.assertEqual([r['member_name'] for r in info[0]['repeat[member_count]']], ['TestHero', 'Watcher', 'Carol'])

    def test_apply_refusals_and_the_offline_master(self):
        a, b, c = self.a, self.b, self.c
        self.assertEqual(self.apply(b, 99), [{'sub': 2, 's2_result': 6}])  # no such guild
        self.assertEqual(self.apply(a, 2), [{'sub': 2, 's2_result': 0}])   # in a guild already
        b.session['gm'], b.session['gm_hidden'] = 1, 0
        self.assertEqual(self.apply(b, 2), [{'sub': 2, 's2_result': 0}])   # a visible GM
        b.session['gm'] = 0
        self.server._gm_chat_command(a.session['sock'], a.session, b'!guild max Testers 1')
        self.assertIn('1/1', P.to_bytes(a.s2c(a.expect(0x15))['text']).decode())
        self.assertEqual(self.apply(b, 2), [{'sub': 2, 's2_result': 6}])   # full
        self.server.guilds.flush()
        reread = G.GuildStore(self.server.guilds.db.path)
        reread.load(self.server.store)
        self.assertEqual((reread.guilds[2].max_members, reread.migration_changes), (1, []))   # a kept dev cap
        self.server.guilds.set_max('Testers', 15)
        # the master offline: "admission is being delayed"; he gets sub 4 at his next login
        a.close()
        _wait(lambda: self.server.world.by_char_name('TestHero') is None)
        self.assertEqual(self.apply(b, 2), [{'sub': 2, 's2_result': 0x11}])
        self.assertEqual(self.apply(c, 2), [{'sub': 2, 's2_result': 0x11}])
        n = F.FakeClient(self.server)
        self.extra.append(n)
        self.assertEqual(n.login('test', 'test')['result'], 1)
        n.enter_world('TestHero')
        replies = self.resync(n)
        self.assertEqual([r['sub'] for r in replies], [3, 4])
        self.assertEqual(replies[1]['s4_count'], 2)
        # accept by name although Carol is second (a stale client order is survived)
        self.send(n, ACCEPT, {'applicant_name': 'Carol'})
        self.assertEqual(self.b3(n)[0], {'sub': 5, 's5_result': 1})
        self.assertEqual([x['name'] for x in self.guild().applications], ['Watcher'])
        # an unknown name pops nothing
        self.send(n, ACCEPT, {'applicant_name': 'Nobody'})
        self.assertEqual(self.b3(n), [{'sub': 5, 's5_result': 0}])
        self.assertEqual(len(self.guild().applications), 1)
        # Watcher joins another guild meanwhile (his application here stays until popped):
        # accepting him says "Already in the other guild."
        apps = self.guild().applications
        self.server.guilds.seed('Others', 'Watcher')
        with self.server.guilds.lock:
            self.server.guilds.db.guilds[2].applications = apps
        self.send(n, ACCEPT, {'applicant_name': 'Watcher'})
        self.assertEqual(self.b3(n), [{'sub': 5, 's5_result': 5}])
        self.assertEqual(self.guild().applications, [])
        # a non-master's 0x8B / 0x8C change nothing
        self.server.guilds.add_application('Testers', 'Dave')
        c.recv_until_quiet(0.3)                                            # Carol's admission sub 3
        self.send(c, ACCEPT, {'applicant_name': 'Dave'})
        self.assertEqual(self.b3(c), [{'sub': 5, 's5_result': 0}])
        self.send(c, REJECT)
        c.expect_silence()
        self.assertEqual([x['name'] for x in self.guild().applications], ['Dave'])

    def test_leave_and_kick(self):
        a, b, c = self.a, self.b, self.c
        for who in (b, c):
            self.apply(who, 2)
            self.mc.drain(0.3)
            self.send(a, ACCEPT, {'applicant_name': who.char_name})
            self.mc.drain(0.3)
        # the master cannot leave
        self.action(a, 2, G.ACT_LEAVE, 'TestHero', 1)
        self.assertEqual(self.b3(a), [{'sub': 6, 's6_result': 0}])
        # B leaves: sub 6 {1} to B, sub 14 to A and C, 0xB7 to the holders
        self.action(b, 2, G.ACT_LEAVE, 'Watcher', 2)
        self.assertEqual(self.b3(b), [{'sub': 6, 's6_result': 1}])
        self.assertEqual(CV.guild_id(b.session), 0)
        for peer in (a, c):
            got = {p.opcode: peer.s2c(p) for p in peer.recv_until_quiet(0.3)}
            self.assertEqual((got[0xB3], got[0xB7]), ({'sub': 14, 's14_member_name': 'Watcher'}, {'uid': 2}))
        # a kick by a non-master / of the master / of a stranger: sub 18 {0}
        self.action(c, 2, G.ACT_KICK, 'TestHero', 1)
        self.assertEqual(self.b3(c), [{'sub': 18, 's18_result': 0}])
        self.action(a, 2, G.ACT_KICK, 'TestHero', 1)
        self.assertEqual(self.b3(a), [{'sub': 18, 's18_result': 0}])
        self.action(a, 2, G.ACT_KICK, 'Watcher', 2)
        self.assertEqual(self.b3(a), [{'sub': 18, 's18_result': 0}])
        # A kicks C: sub 18 {1} + sub 14 (A's window), sub 6 {1} to C, 0xB7 to the holders
        self.action(a, 2, G.ACT_KICK, 'Carol', 3)
        got = [(p.opcode, a.s2c(p)) for p in a.recv_until_quiet(0.3)]
        self.assertEqual(got, [(0xB3, {'sub': 18, 's18_result': 1}), (0xB3, {'sub': 14, 's14_member_name': 'Carol'}),
                               (0xB7, {'uid': 3})])
        self.assertEqual(self.b3(c), [{'sub': 6, 's6_result': 1}])
        self.assertEqual(CV.guild_id(c.session), 0)
        self.assertEqual([p.opcode for p in b.recv_until_quiet(0.3)], [0xB7])
        self.assertEqual([m['name'] for m in self.guild().members], ['TestHero'])
        # the 0x21 tail follows: a kicked member's kill has none any more
        self.server.award_exp(c.session, 40, 'kill')
        self.assertEqual(len([p for p in c.recv_until_quiet(0.3) if p.opcode == 0x21][0].payload), 4)

    def test_a_gm_member_gets_its_gm_tag_back(self):
        a, b = self.a, self.b
        self.apply(b, 2)
        self.mc.drain(0.3)
        b.session['gm'], b.session['gm_hidden'] = 1, 0                      # promoted after applying
        with self.server.store.lock:
            self.char('Watcher')['gm'] = 1
        self.send(a, ACCEPT, {'applicant_name': 'Watcher'})
        a_got = a.recv_until_quiet(0.3)
        self.assertNotIn(0xB4, [p.opcode for p in a_got])                  # the GM tag wins on A's client
        self.assertEqual([x['sub'] for x in self.b3(b)], [3, 37])
        self.assertEqual(CV.guild_id(b.session), 1)
        self.action(b, 2, G.ACT_LEAVE, 'Watcher', 2)
        self.assertEqual([x['sub'] for x in self.b3(b)], [6, 37])
        self.assertNotIn(0xB7, [p.opcode for p in a.recv_until_quiet(0.3)])


class LeaveAfterADay2009(_Rig, unittest.TestCase):
    extra_config = {'GUILD_LEAVE_MIN_HOURS': 24.0}

    def test_one_day_rule(self):
        self.server.guilds.seed('Testers', 'TestHero', ['Watcher'])
        self.action(self.b, 2, G.ACT_LEAVE, 'Watcher', 2)
        self.assertEqual(self.b3(self.b), [{'sub': 6, 's6_result': 0x0C}])
        with self.server.guilds.lock:
            self.server.guilds.db.guilds[2].members[1]['joined_at'] -= 25 * 3600
        self.action(self.b, 2, G.ACT_LEAVE, 'Watcher', 2)
        self.assertEqual(self.b3(self.b), [{'sub': 6, 's6_result': 1}])


# ================================================== guild-g3 / g5: lines + points ===
class LoginLogout2009(unittest.TestCase):
    """A (TestHero) master, B (Watcher) member of "Testers"; channels 1-3 (the P12 dev setup,
    no listener bound)."""

    def setUp(self):
        if not HAVE[B9]:
            self.skipTest('needs the EN 2009 client data')
        self.tmp = tempfile.mkdtemp(prefix='ws_p14g_hop_')
        cfg = cfgmod.from_dict({'CLIENT_BUILD': B9, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False,
                                'CHANNELS': CHANNELS, 'CHANNEL_HOP_SECS': 30.0})
        self.server = F.make_server(self.tmp, accounts=_accounts(), config=cfg)
        self.server.guilds.seed('Testers', 'TestHero', ['Watcher'])
        self.extra = []
        self.mc = F.MultiClient(self.server)
        self.a, self.b = self.mc
        for c in self.mc:
            c.send_c2s(FRIENDS)
            c.send_c2s(CARDS)
            c.send_c2s(GUILD_INFO)
        self.mc.drain(0.3)

    def tearDown(self):
        for c in self.extra:
            try:
                c.close()
            except OSError:
                pass
        self.mc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def lines(self, c, quiet=0.4):
        return [x for x in _b3(c, c.recv_until_quiet(quiet)) if x['sub'] in (20, 21)]

    def kill(self, c, exp=41):
        self.server.award_exp(c.session, exp, 'kill')
        pkt = [p for p in c.recv_until_quiet(0.3) if p.opcode == 0x21][0]
        return c.s2c(pkt)

    def close(self, c, fetch):
        key, old = c.session['session_key'], c.session
        if fetch:
            self.server.channels.note_version_fetch(IP)
        else:
            self.server.channels._fetches.clear()
        c.close()
        self.assertTrue(_wait(lambda: old.get('closed') and old['closed_event'].is_set()))
        return key

    def relogin(self, key, channel):
        n = F.FakeClient(self.server, channel=channel)
        self.extra.append(n)
        self.assertEqual(n.login('admin', 'admin', session_key=key)['result'], 1)
        n.send_c2s(ENTER_RELOGIN, {'p2p_ip': IP, 'p2p_udp_port': F.P2P_PORT_BASE + 1, 'char_name': 'Watcher'})
        self.assertTrue(n.wait_session(lambda s: s.get('in_world')))
        n.recv_until_quiet(0.3)
        n.send_c2s(FRIENDS)
        n.send_c2s(CARDS)
        n.send_c2s(GUILD_INFO)
        return n, _b3(n, n.recv_until_quiet(0.3))

    def test_quit_relogin_and_the_points(self):
        a, b = self.a, self.b
        self.assertEqual(self.kill(b), {'exp_delta': 41, 'guild_points': 20})
        self.assertEqual(self.kill(b, 10), {'exp_delta': 10, 'guild_points': 5})
        self.assertEqual(self.server.guilds.get('Testers').points, 0)      # credited at the logout
        key = self.close(b, fetch=False)                                    # a quit
        self.assertEqual(self.lines(a), [{'sub': 21, 's21_member_name': 'Watcher', 's21_guild_points': 25}])
        self.assertEqual(self.server.guilds.get('Testers').points, 25)
        # the login: "[Watcher] has logged in." with the channel byte
        n = F.FakeClient(self.server, channel=3)
        self.extra.append(n)
        self.assertEqual(n.login('admin', 'admin')['result'], 1)
        n.enter_world('Watcher')
        self.assertEqual(self.lines(a), [{'sub': 20, 's20_member_name': 'Watcher', 's20_online': 3}])
        del key

    def test_a_channel_hop_sends_no_line_and_keeps_the_points(self):
        a, b = self.a, self.b
        self.assertEqual(self.kill(b)['guild_points'], 20)
        key = self.close(b, fetch=True)                                     # Change Channel teardown
        self.assertEqual(self.lines(a), [])
        n, replies = self.relogin(key, channel=2)
        self.assertIsNotNone(n.session['channel_hop'])
        self.assertEqual(self.lines(a), [])                                 # no login line either
        self.assertEqual([r['member_online'] for r in replies[0]['repeat[member_count]']], [1, 2])
        # guild chat crosses the channels
        raw = b'Watcher : hi from 2'
        n.send_c2s(CHAT, {'guild_id': 2, 'text_len': len(raw), 'text': raw})
        got = [a.s2c(p) for p in a.recv_until_quiet(0.3) if p.opcode == 0xB5]
        self.assertEqual(got, [{'text_len': len(raw), 'text': raw.decode()}])
        self.assertEqual(self.kill(n, 10)['guild_points'], 5)
        self.assertEqual(self.server.guilds.get('Testers').points, 0)
        # the real logout: both connections' points, one line
        self.close(n, fetch=False)
        self.assertEqual(self.lines(a), [{'sub': 21, 's21_member_name': 'Watcher', 's21_guild_points': 25}])
        self.assertEqual(self.server.guilds.get('Testers').points, 25)

    def test_a_held_logout_credits_when_no_relogin_comes(self):
        a, b = self.a, self.b
        self.kill(b)
        self.close(b, fetch=True)
        self.assertEqual(self.lines(a), [])
        self.server.ticks.run_due(time.monotonic() + 31)
        self.assertEqual(self.lines(a), [{'sub': 21, 's21_member_name': 'Watcher', 's21_guild_points': 20}])
        self.assertEqual(self.server.guilds.get('Testers').points, 20)


# =============================================================== guild-g5 ===
class Chat2009(_Rig, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.seed('Testers', 'TestHero', 'Watcher')

    def test_relay_to_the_others_only(self):
        a, b, c = self.a, self.b, self.c
        with self.assertNoLogs('WS', logging.ERROR):                       # no NEVER_REPLY violation
            self.chat(b, 2, 'Watcher : hi')
            self.chat(b, 2, 'Watcher : again')                             # the 700 ms anti-spam drops it
            self.assertEqual([a.s2c(p) for p in a.recv_until_quiet(0.3)],
                             [{'text_len': 12, 'text': 'Watcher : hi'}])
        b.expect_silence()                                                 # its own echo only
        c.expect_silence()                                                 # not a member
        time.sleep(0.75)
        # a spoofed name is rebuilt; a long line is cut to 87 bytes
        self.chat(b, 2, 'TestHero : ' + 'x' * 90)
        got = a.s2c(a.expect(0xB5))
        self.assertEqual(len(got['text']), 87)
        self.assertTrue(got['text'].startswith('Watcher : xxx'))

    def test_dropped_lines(self):
        a, b, c = self.a, self.b, self.c
        self.chat(b, 9, 'Watcher : wrong guild')                           # gid != its guild
        self.chat(c, 2, 'Carol : not a member')                            # /g is not gated client-side
        self.chat(b, 2, 'Watcher : ')                                      # empty
        a.expect_silence()
        # A blacklists B: B's guild lines skip A (the 0x91 fan-out rule)
        a.send_c2s(registry_key(0x93), {'name': 'Watcher'})
        a.recv_until_quiet(0.3)
        self.chat(b, 2, 'Watcher : hello')
        a.expect_silence()

    def test_never_reply_row(self):
        self.assertIn(0x8D, registry.never_reply_table(B9))
        self.assertNotIn(0x8D, registry.never_reply_table(B8))


def registry_key(op):
    return P.variants(op, 'C2S', client_build=B9)[0]['key']


class PointsPost2009(_Rig, unittest.TestCase):
    extra_config = {'GUILD_POINTS_BASE': 'post'}

    def test_the_post_multiplier_policy(self):
        self.seed('Testers', 'TestHero')
        self.server._gm_chat_command(self.a.session['sock'], self.a.session, b'!expmult 2')
        self.a.recv_until_quiet(0.3)
        self.server.award_exp(self.a.session, 40, 'kill')
        pkt = [p for p in self.a.recv_until_quiet(0.3) if p.opcode == 0x21][0]
        self.assertEqual(self.a.s2c(pkt), {'exp_delta': 80, 'guild_points': 40})
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.from_dict({'GUILD_POINTS_BASE': 'later'})


# =============================================================== guild-g4 ===
class Management2009(_Rig, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.seed('Testers', 'TestHero', 'Watcher', 'Carol')

    def fake_rows(self, grade, n, start=9000):
        """n extra member rows of `grade` (cids no character has: counted for the caps only)."""
        with self.server.guilds.lock:
            g = self.server.guilds.db.guilds[2]
            g.members += [{'cid': start + i, 'name': f'Ghost{i}', 'grade': grade, 'joined_at': 0.0} for i in range(n)]
            g.max_members = 50

    def test_grades(self):
        a, b, c = self.a, self.b, self.c
        self.send(a, GRADE, {'guild_id': 2, 'member_id': 2, 'member_name': 'Watcher', 'grade': 4})
        self.assertEqual(self.b3(a), [{'sub': 16, 's16_result': 1}, {'sub': 17, 's17_member_name': 'Watcher', 's17_grade': 4}])
        for peer in (b, c):
            self.assertEqual(self.b3(peer), [{'sub': 17, 's17_member_name': 'Watcher', 's17_grade': 4}])
        self.assertEqual(self.guild().members[1]['grade'], 4)
        # a non-master (default GUILD_GRADE_MIN_GRADE 5), grade 5, the master: exactly one sub 16 {0}
        for who, name, grade in ((b, 'Carol', 3), (a, 'Carol', 5), (a, 'TestHero', 1), (a, 'Nobody', 2)):
            self.send(who, GRADE, {'guild_id': 2, 'member_id': 0, 'member_name': name, 'grade': grade})
            self.assertEqual(self.b3(who), [{'sub': 16, 's16_result': 0}])
        self.mc.drain(0.3)
        # the caps: 4 Guardians, 10 Vanguards
        self.fake_rows(4, 3)
        self.send(a, GRADE, {'guild_id': 2, 'member_id': 3, 'member_name': 'Carol', 'grade': 4})
        self.assertEqual(self.b3(a), [{'sub': 16, 's16_result': 0x0D}])
        self.fake_rows(3, 10, start=9100)
        self.send(a, GRADE, {'guild_id': 2, 'member_id': 3, 'member_name': 'Carol', 'grade': 3})
        self.assertEqual(self.b3(a), [{'sub': 16, 's16_result': 0x0E}])
        self.send(a, GRADE, {'guild_id': 2, 'member_id': 3, 'member_name': 'Carol', 'grade': 2})
        self.assertEqual([x['sub'] for x in self.b3(a)], [16, 17])
        # the backstop row carries the same refusal
        [(key, fields, _)] = registry.must_reply_table(B9)[0x92].refusal(self.server, a.session, None)
        self.assertEqual((key, fields), ('0xB3', {'sub': 16, 's16_result': 0}))
        self.assertNotIn(0x92, registry.must_reply_table(B8))

    def test_master_change(self):
        a, b, c = self.a, self.b, self.c
        self.server.guilds.add_application('Testers', 'Dave')
        self.send(a, MASTER, {'guild_id': 2, 'new_master_name': 'Nobody'})
        self.assertEqual(self.b3(a), [{'sub': 11, 's11_result': 8}])
        self.send(b, MASTER, {'guild_id': 2, 'new_master_name': 'Carol'})
        self.assertEqual(self.b3(b), [{'sub': 11, 's11_result': 0}])
        self.send(a, MASTER, {'guild_id': 2, 'new_master_name': 'Watcher'})
        self.assertEqual(self.b3(a), [{'sub': 11, 's11_result': 1}, {'sub': 12, 's12_new_master': 'Watcher'}])
        got = self.b3(b)
        self.assertEqual([x['sub'] for x in got], [12, 4])                 # + Dave's application
        self.assertEqual(got[1]['s4_count'], 1)
        self.assertEqual(self.b3(c), [{'sub': 12, 's12_new_master': 'Watcher'}])
        g = self.guild()
        self.assertEqual([(m['name'], m['grade']) for m in g.members], [('TestHero', 1), ('Watcher', 5), ('Carol', 1)])
        # the new master acts: accepts Dave (offline: stored), kicks the old master
        self.send(b, ACCEPT, {'applicant_name': 'Dave'})
        self.assertEqual(self.b3(b)[0], {'sub': 5, 's5_result': 1})
        self.mc.drain(0.3)
        self.action(b, 2, G.ACT_KICK, 'TestHero', 1)
        self.assertEqual(self.b3(b)[:2], [{'sub': 18, 's18_result': 1}, {'sub': 14, 's14_member_name': 'TestHero'}])
        self.assertEqual(self.b3(a), [{'sub': 6, 's6_result': 1}])

    def test_a_master_change_back_and_forth_keeps_the_application_lists_paired(self):
        """P14 review: sub 12 does not clear the old master's M+0xE0, so his list is stale until
        his next map load - no sub 4 on a re-promotion (it would append), his Deny pops nothing."""
        a, b = self.a, self.b
        self.server.guilds.add_application('Testers', 'Dave')
        self.assertEqual([_sub4_names(p) for p in self.raw_resync(a) if p[0] == 4], [['Dave']])
        va, vb = CV.view(a.session), CV.view(b.session)
        self.assertEqual((va.apps_held, va.apps_stale), (True, False))
        # A -> B: B's list is synced (sub 4), A's is stale now
        self.send(a, MASTER, {'guild_id': 2, 'new_master_name': 'Watcher'})
        self.assertEqual(self.b3(a), [{'sub': 11, 's11_result': 1}, {'sub': 12, 's12_new_master': 'Watcher'}])
        self.assertEqual([x['sub'] for x in self.b3(b)], [12, 4])
        self.assertEqual((va.apps_held, va.apps_stale, vb.apps_held, vb.apps_stale), (False, True, True, False))
        self.mc.drain(0.3)
        # B -> A before A's next map load: A's client still lists Dave - no second copy on top
        self.send(b, MASTER, {'guild_id': 2, 'new_master_name': 'TestHero'})
        self.assertEqual(self.b3(b), [{'sub': 11, 's11_result': 1}, {'sub': 12, 's12_new_master': 'TestHero'}])
        self.assertEqual(self.b3(a), [{'sub': 12, 's12_new_master': 'TestHero'}])
        self.assertEqual((va.apps_held, va.apps_stale, vb.apps_held, vb.apps_stale), (False, True, False, True))
        # A's Deny pops a row of its stale list: nothing server-side
        self.send(a, REJECT)
        a.expect_silence()
        self.assertEqual([x['name'] for x in self.guild().applications], ['Dave'])
        # A's next map load rebuilds the list (once); now the Deny pairs with the FIFO again
        replies = self.raw_resync(a)
        self.assertEqual([_sub4_names(p) for p in replies if p[0] == 4], [['Dave']])
        self.assertEqual((va.apps_held, va.apps_stale), (True, False))
        self.send(a, REJECT)
        a.expect_silence()
        self.assertEqual(self.guild().applications, [])

    def test_notice(self):
        a, b = self.a, self.b
        self.send(b, NOTICE, {'notice': 'not the master'})
        self.assertEqual(self.b3(b), [{'sub': 7, 's7_result': 0}])
        self.send(a, NOTICE, {'notice': ''})
        self.assertEqual(self.b3(a), [{'sub': 7, 's7_result': 0}])
        self.send(a, NOTICE, {'notice': 'Hunt at 9'})
        self.assertEqual(self.b3(a), [{'sub': 7, 's7_result': 1}])
        b.expect_silence()                                                 # no push sub-code [V]
        self.assertEqual(self.guild().notice, 'Hunt at 9')
        replies = self.warp(b, b.session['current_map'], *b.session['pos'])
        self.assertEqual(replies[0]['guild_notice'], 'Hunt at 9')

    def test_capacity(self):
        a, b = self.a, self.b
        cap = {'guild_id': 2, 'add_members': 5, 'gold_cost': 6500}
        self.send(a, CAPACITY, cap)                                        # Lv.1: no tier
        self.assertEqual(self.b3(a), [{'sub': 9, 's9_ok': 0}])
        self.server.guilds.set_points('Testers', 405450)                   # Lv.2
        self.send(b, CAPACITY, cap)                                        # not the master
        self.assertEqual(self.b3(b), [{'sub': 9, 's9_ok': 0}])
        self.send(a, CAPACITY, dict(cap, gold_cost=6000))                  # not the client's table
        self.assertEqual(self.b3(a), [{'sub': 9, 's9_ok': 0}])
        with self.server.store.lock:
            self.char('TestHero')['gold'] = 6499
        self.send(a, CAPACITY, cap)
        self.assertEqual(self.b3(a), [{'sub': 9, 's9_ok': 0}])
        self.assertEqual(self.gold('TestHero'), 6499)
        with self.server.store.lock:
            self.char('TestHero')['gold'] = 10000
        self.send(a, CAPACITY, cap)
        self.assertEqual(self.b3(a), [{'sub': 9, 's9_ok': 1, 's9_cost': 6500}, {'sub': 10, 's10_max_members': 20}])
        self.assertEqual(self.b3(b), [{'sub': 10, 's10_max_members': 20}])
        self.assertEqual((self.gold('TestHero'), self.guild().max_members), (3500, 20))
        # 20 -> 30 needs Lv.3
        self.send(a, CAPACITY, {'guild_id': 2, 'add_members': 10, 'gold_cost': 20000})
        self.assertEqual(self.b3(a), [{'sub': 9, 's9_ok': 0}])
        self.assertEqual(G.next_tier(20), (30, 20000, 3))
        self.assertIsNone(G.next_tier(50))

    def test_rename_hook(self):
        a, b = self.a, self.b
        self.server.store.rename_character('admin', 'Watcher', 'Watch2', save=False)
        b.session['char_name'] = 'Watch2'                                  # cashuse.CashUse.rename's live half
        self.server.world.enter(b.session, 'Watch2')
        self.server.world.hooks.fire(WM.ON_RENAME, self.server, b.session, old='Watcher', new='Watch2',
                                     cid=self.char('Watch2')['cid'])
        for c in (a, b, self.c):
            self.assertEqual(self.b3(c), [{'sub': 22, 's22_old_name': 'Watcher', 's22_new_name': 'Watch2'}])
        self.assertEqual(self.guild().members[1]['name'], 'Watch2')


class GradePolicy2009(_Rig, unittest.TestCase):
    extra_config = {'GUILD_GRADE_MIN_GRADE': 4, 'GUILD_MASTER_NEEDS_CREATE_RULES': True}

    def test_guardians_may_change_lower_grades_and_the_master_rule(self):
        a, b = self.a, self.b
        self.seed('Testers', 'TestHero', 'Watcher', 'Carol')
        self.send(a, GRADE, {'guild_id': 2, 'member_id': 2, 'member_name': 'Watcher', 'grade': 4})
        self.mc.drain(0.3)
        self.send(b, GRADE, {'guild_id': 2, 'member_id': 3, 'member_name': 'Carol', 'grade': 3})
        self.assertEqual([x['sub'] for x in self.b3(b)], [16, 17])
        self.send(b, GRADE, {'guild_id': 2, 'member_id': 3, 'member_name': 'Carol', 'grade': 4})
        self.assertEqual(self.b3(b), [{'sub': 16, 's16_result': 0}])       # not up to his own grade
        self.mc.drain(0.3)
        # Watcher is Lv 1 without a 2nd class: "This member can't be the guild master."
        self.send(a, MASTER, {'guild_id': 2, 'new_master_name': 'Watcher'})
        self.assertEqual(self.b3(a), [{'sub': 11, 's11_result': 8}])
        self.send(a, MASTER, {'guild_id': 2, 'new_master_name': 'Carol'})
        self.assertEqual(self.b3(a)[0], {'sub': 11, 's11_result': 1})
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.from_dict({'GUILD_GRADE_MIN_GRADE': 0})


# ===================================================================== 2008 ===
class No2008(_Rig, unittest.TestCase):
    build = B8

    def test_no_guild_flow_on_a_2008_server(self):
        routes = self.server.routes
        for op in (0x87, 0x89, 0x8B, 0x8C, 0x8D, 0x8E, 0x8F, 0x90, 0x91, 0x92):
            self.assertFalse(str(getattr(routes.get(op), 'handler', '') or '').startswith('_handle_guild'), hex(op))
        hooks = self.server.world.hooks
        for name in (WM.ON_LOGOUT, WM.ON_ENTER_WORLD):
            self.assertFalse(any(getattr(fn, '__qualname__', '').startswith('register.') and 'guild' in fn.__module__
                                 for fn in hooks.registered(name)))
        self.assertNotIn(0x87, registry.must_reply_table(B8))
        self.b.close()
        _wait(lambda: self.server.world.by_char_name('Watcher') is None)
        self.assertFalse([p for p in self.a.recv_until_quiet(0.3) if p.opcode == 0xB3])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'guilds.json')))


if __name__ == '__main__':
    unittest.main()
