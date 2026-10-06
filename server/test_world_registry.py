#!/usr/bin/env python3
"""
test_world_registry.py - P5 stage 1 (registry-harness-privacy), offline, both client builds
===========================================================================================
- world-registry (+party-mp-registry, trade-mp-registry, chat_mail_gm-online-registry,
  social_friend-session-identity): world.World map instances, peers and the online views,
  the lifecycle hooks (before_server_map_load, on_map_change, on_enter_world,
  on_leave_world, on_disconnect) fired exactly once in order, and the world.Outbox
  outbound path - a send to ANOTHER session never blocks the sender (a stalled peer cannot
  hold the caller's combat lock), while queue order = cipher order.
- party-test-harness (+trade-harness, social_friend-harness-multiclient):
  fakeclient.MultiClient (two players on one server in either build), admin targets by
  uid / name / client exe ("c:2" = the P2P port 42908 of WindSlayer_p2.exe; a character
  called "c2" or "12" stays targetable by name) and `wsdev sendspec 15 ... --to 2|c:2` end
  to end (P5 exit criterion 8 offline).
- lock order (review fix): World's index lock is a private leaf, so a tick holding
  world_lock that waits for a combat lock never deadlocks with that combat lock's holder
  asking World for its map audience; the disconnect path runs every step and always
  closes the outbox and the socket; a kick closes the outbox before the socket.
- chat_mail_gm-privacy-flags (+party-privacy, trade-privacy-flags,
  quest_cards_misc-privacy-flags): C2S 0x2B bytes 0-4 and C2S 0x40 into session['refuse']
  and the character record, never answered; refuses() for online and offline targets;
  the P5 store migration with its one-time accounts.json.bak-pre-p5.

Every server runs the real GameServer loop through fakeclient over socketpairs: no port is
bound, no client is started, and the live accounts.json is never opened (temp copies; the
module checks its hash at the end).
"""
import hashlib
import io
import json
import logging
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import fakeclient as F  # noqa: E402
import packets as P  # noqa: E402
import privacy  # noqa: E402
import store as S  # noqa: E402
import world as WM  # noqa: E402
import debuffs as D  # noqa: E402
import skills as SK  # noqa: E402

W = F.import_server()
cfgmod = W.cfgmod
EC = W.EC
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = '2008', '2009'
HAVE_2009_INSTALL = os.path.exists(os.path.join(HERE, cfgmod.DEFAULTS['CLIENT_DIR_2009'], 'hs', 'windslayer.hii'))

PRIVACY_KEY = {B8: '0x4414BA/0x40', B9: '0x44206A/0x40'}
PORTAL_101_TO_102 = bytes.fromhex('17000000')          # the live 0x7E capture (map 101 line 23)
ALL_HOOKS = WM.HOOK_NAMES


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


_LIVE_HASH = None


def setUpModule():
    global _LIVE_HASH
    _LIVE_HASH = _sha(LIVE_ACCOUNTS) if os.path.exists(LIVE_ACCOUNTS) else None
    P.STRICT_FIELDS.add(B9)                 # every 2009 S2C must use the 2009 field names


def tearDownModule():
    P.STRICT_FIELDS.discard(B9)
    EC.configure(cfgmod.DEFAULTS['CLIENT_DIR'], B8)
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_world_registry.py (tests must only use temp copies)'


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _text(rec):
    return P.to_bytes(rec['text']).decode('cp949')


class _Gate:
    """A socket stand-in whose sendall blocks until `gate` is set: a client that stopped
    reading (its TCP window is full)."""

    def __init__(self, real, gate):
        self.real, self.gate = real, gate
        self.shut = False

    def sendall(self, data):
        if not self.gate.wait(10):
            raise socket.timeout('gate never opened')
        self.real.sendall(data)

    def shutdown(self, how):
        self.shut = True


# =================================================================== Outbox ===
class OutboxUnit(unittest.TestCase):
    def setUp(self):
        self.a, self.b = socket.socketpair()
        self.b.settimeout(3)

    def tearDown(self):
        self.a.close()
        self.b.close()

    def read(self, n):
        buf = b''
        while len(buf) < n:
            buf += self.b.recv(65536)
        return buf

    def test_flush_and_writer_keep_the_queue_order(self):
        ob = WM.Outbox(self.a, 'order')
        parts = [b'%04d' % i for i in range(600)]
        for i, part in enumerate(parts):
            ob.put(part)
            if i % 3:
                ob.kick()                        # another thread's push
            else:
                ob.flush()                       # the connection thread's own reply
        self.assertEqual(self.read(len(parts) * 4), b''.join(parts))
        self.assertTrue(ob.wait_idle())
        ob.close()

    def test_kick_never_blocks_on_a_stalled_socket(self):
        gate = threading.Event()
        ob = WM.Outbox(_Gate(self.a, gate), 'stall')
        t0 = time.monotonic()
        for i in range(50):
            ob.put(b'%03d' % i)
            ob.kick()
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertGreaterEqual(ob.pending(), 49)           # the writer holds at most one
        gate.set()
        self.assertEqual(self.read(150), b''.join(b'%03d' % i for i in range(50)))
        ob.close()

    def test_a_closed_outbox_refuses_and_drops_its_backlog(self):
        gate = threading.Event()
        ob = WM.Outbox(_Gate(self.a, gate), 'closed')
        ob.put(b'x')
        ob.close()
        self.assertEqual(ob.pending(), 0)
        with self.assertRaises(OSError):
            ob.put(b'y')
        ob.kick()                                            # no thread for a closed outbox
        gate.set()

    def test_a_write_error_closes_the_connection(self):
        class Broken:
            shut = False

            def sendall(self, data):
                raise ConnectionResetError('peer gone')

            def shutdown(self, how):
                Broken.shut = True
        ob = WM.Outbox(Broken(), 'broken')
        with self.assertLogs('WS', logging.WARNING):
            ob.put(b'x')
            ob.kick()
            self.assertTrue(_wait(lambda: ob.closed))
        self.assertTrue(Broken.shut)
        with self.assertRaises(OSError):
            ob.put(b'y')
        ob2 = WM.Outbox(Broken(), 'broken-sync')
        ob2.put(b'x')
        with self.assertLogs('WS', logging.WARNING), self.assertRaises(OSError):
            ob2.flush()                                      # the connection thread sees it
        self.assertTrue(ob2.closed)

    def test_a_backlog_over_the_cap_closes_the_connection(self):
        gate = threading.Event()
        sock = _Gate(self.a, gate)
        ob = WM.Outbox(sock, 'cap')
        ob.MAX_BACKLOG_BYTES = 100
        with self.assertLogs('WS', logging.WARNING), self.assertRaises(OSError):
            for _ in range(20):
                ob.put(b'x' * 10)
        self.assertTrue(ob.closed)
        self.assertTrue(sock.shut)
        gate.set()


# ==================================================================== World ===
def _session(uid, name, *, in_world=True):
    return {'uid': uid, 'char_name': name, 'username': name.lower(), 'in_world': in_world,
            'sock': object()}


class WorldUnit(unittest.TestCase):
    def setUp(self):
        self.w = WM.World()
        self.a, self.b, self.c = _session(1, 'Alice'), _session(2, 'Bob'), _session(3, 'Cid')
        for s in (self.a, self.b, self.c):
            self.assertIsNone(self.w.claim(s, s['uid'], s['username']))
            self.assertTrue(self.w.enter(s, s['char_name']))

    def test_map_instances_peers_and_empty_since(self):
        for s, code in ((self.a, 101), (self.b, 101), (self.c, 102)):
            self.assertTrue(self.w.arrive(s, code, now=1.0))
        self.assertEqual(self.w.peers(self.a), [self.b])
        self.assertEqual(self.w.peers(self.c), [])
        self.assertEqual(self.w.map_sessions(101, exclude=self.b), [self.a])
        self.assertIsNone(self.w.maps[101].empty_since)
        self.assertEqual(self.w.describe(), {101: ['Alice', 'Bob'], 102: ['Cid']})
        # a map change: off 101, onto 102 (arrive departs first)
        self.assertTrue(self.w.arrive(self.a, 102, now=2.0))
        self.assertEqual((self.w.map_of(self.a), self.w.peers(self.a)), (102, [self.c]))
        self.assertEqual(self.w.peers(self.b), [])
        self.assertEqual(self.w.depart(self.b, now=3.0), 101)
        self.assertEqual(self.w.maps[101].empty_since, 3.0)
        self.assertIsNone(self.w.depart(self.b))                       # already off
        self.assertEqual(self.w.peers(self.b), [])

    def test_unreachable_sessions_are_never_peers(self):
        for s in (self.a, self.b, self.c):
            self.w.arrive(s, 101)
        self.b['kicked'] = 'dup'
        self.c['in_world'] = False                                     # a map load in flight
        self.assertEqual(self.w.peers(self.a), [])
        self.b.pop('kicked')
        self.b['closed'] = 'client closed'
        self.assertEqual(self.w.in_world_sessions(), [self.a])

    def test_a_kicked_or_replaced_session_is_never_indexed_or_unindexes_the_new_one(self):
        self.w.arrive(self.a, 101)
        new = _session(1, 'Alice')
        old = self.w.claim(new, 1, 'alice', replace=True)                # 2009 relogin
        self.assertIs(old, self.a)
        self.assertTrue(self.w.superseded(self.a))
        self.assertFalse(self.w.arrive(self.a, 102))                   # not the uid's owner
        self.assertTrue(self.w.arrive(new, 101))
        self.assertIs(self.w.maps[101].sessions[1], new)
        # the old session's late departure (its finally) reports its map but must not
        # remove the new session that holds the same uid there now
        self.assertEqual(self.w.depart(self.a), 101)
        self.assertIs(self.w.maps[101].sessions[1], new)
        self.assertEqual(self.w.peers(self.b, map_code=101), [new])

    def test_online_views(self):
        for s in (self.a, self.b):
            self.w.arrive(s, 101)
        self.assertIs(self.w.find('bob'), self.b)
        self.assertIs(self.w.find('  BOB '), self.b)
        self.assertIs(self.w.find(2), self.b)
        self.assertIsNone(self.w.find('2'))                             # a str is a NAME, never a uid
        self.assertIsNone(self.w.find('Cid'))                           # never arrived: not in world
        self.c['in_world'] = False
        self.assertIsNone(self.w.find('Cid'))
        self.assertIs(self.w.find('Cid', in_world=False), self.c)
        self.assertIsNone(self.w.find('nobody'))
        self.assertIsNone(self.w.find(True))
        self.assertEqual(self.w.online_names(['ALICE', 'cid', 'zed']), {'alice': self.a})

    def test_an_all_digit_character_name_is_a_name_not_a_uid(self):
        """names.ALLOWED_RE allows a character called '2': a whisper to it must reach it,
        not account uid 2 (review minor: find('12') read by_uid[12])."""
        digits = _session(7, '2')
        self.assertIsNone(self.w.claim(digits, 7, 'digits'))
        self.assertTrue(self.w.enter(digits, '2'))
        for s in (self.b, digits):
            self.w.arrive(s, 101)
        self.assertIs(self.w.find('2'), digits)
        self.assertIs(self.w.find(' 2 '), digits)
        self.assertIs(self.w.find(2), self.b)
        self.assertIs(self.w.find(7), digits)

    def test_the_index_lock_is_a_private_leaf(self):
        self.assertIsNot(WM.World().lock, self.w.lock)
        with self.assertRaises(TypeError):
            WM.World(lock=threading.RLock())                           # never a shared lock

    def test_client_numbers_come_from_the_p2p_port(self):
        # the colon form only: 'c2' / 'client2' are legal character names (names.ALLOWED_RE)
        self.assertEqual([WM.parse_client_target(t) for t in
                          ('c:2', 'C:1', 'client:2', ' Client : 3 ', 'c2', 'client2', '2', 'c:x', 'cx', 7)],
                         [2, 1, 2, 3, None, None, None, None, None, None])
        self.assertEqual(WM.client_number({'p2p': ('', 42907)}), 1)
        self.assertEqual(WM.client_number({'p2p': ('127.0.0.1', 42908)}), 2)
        self.assertIsNone(WM.client_number({'p2p': ('', 7022)}))
        self.assertIsNone(WM.client_number({}))

    def test_hook_names(self):
        # on_rename: the rename hook (ROADMAP_2009_ADDENDUM C4; test_carryins.C4RenameHook2009)
        self.assertEqual(ALL_HOOKS, ('before_server_map_load', 'on_map_change', 'on_enter_world',
                                     'on_leave_world', 'on_disconnect', 'on_rename'))
        with self.assertRaises(KeyError):
            self.w.hooks.register('on_teleport', print)
        fn = self.w.hooks.register(WM.ON_LEAVE_WORLD, lambda *a, **k: None)
        self.assertTrue(self.w.hooks.unregister(WM.ON_LEAVE_WORLD, fn))
        self.assertFalse(self.w.hooks.unregister(WM.ON_LEAVE_WORLD, fn))


# ================================================================== privacy ===
class PrivacyUnit(unittest.TestCase):
    def test_record_and_normalize(self):
        rec = {'refuse_whisper': 1, 'refuse_exchange': 0, 'refuse_party': 1, 'refuse_talk': 0,
               'refuse_friend': 1}
        self.assertEqual(privacy.from_record(rec), {'whisper': True, 'exchange': False, 'party': True,
                                                    'talk': False, 'friend': True})
        self.assertEqual(privacy.normalize('junk'), privacy.default())
        self.assertEqual(privacy.normalize({'talk': 1, 'bogus': 1}),
                         {**privacy.default(), 'talk': True})
        char = {'refuse': {'friend': 'yes'}}
        privacy.ensure(char)
        self.assertEqual(char['refuse'], {**privacy.default(), 'friend': True})
        self.assertEqual(privacy.describe(char['refuse']), 'friend')
        self.assertEqual(privacy.describe(None), 'none')

    def test_refuses(self):
        live = {'refuse': {'whisper': True}, 'char': {'refuse': {'party': True}}}
        self.assertTrue(privacy.refuses(live, 'whisper'))
        self.assertFalse(privacy.refuses(live, 'party'))               # the live copy wins
        self.assertTrue(privacy.refuses({'char': {'refuse': {'party': True}}}, 'party'))
        self.assertTrue(privacy.refuses({'name': 'Bob', 'refuse': {'exchange': True}}, 'exchange'))
        self.assertFalse(privacy.refuses(None, 'friend'))              # unresolved: "not found"
        gm_actor, player = {'gm': 1}, {'gm': 0}
        self.assertFalse(privacy.refuses(live, 'whisper', gm_actor))   # chat_mail_gm F2 step 4
        self.assertTrue(privacy.refuses(live, 'whisper', player))
        self.assertTrue(privacy.refuses({'refuse': {'friend': True}}, 'friend', gm_actor))
        with self.assertRaises(ValueError):
            privacy.refuses(live, 'trade')


# ================================================================ migration ===
class MigrationP5(unittest.TestCase):
    """The refuse flags arrive through an idempotent migration that first writes the
    one-time accounts.json.bak-pre-p5 (hard rule for a persisted-schema change)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_p5_store_')
        self.path = os.path.join(self.tmp, 'accounts.json')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pre_p5_file(self, build):
        st = S.Store(self.path, client_build=build).load()            # a current P4 file
        with st.lock:
            for acc in st.accounts.values():
                for ch in acc['characters']:
                    del ch['refuse']
        st.save_now()
        for suffix in S.BACKUP_SUFFIXES + (S.BACKUP_SUFFIX_2009,):
            path = self.path + suffix
            if suffix == '.bak-pre-p5':
                if os.path.exists(path):
                    os.remove(path)
            elif not os.path.exists(path):
                open(path, 'wb').close()                                # earlier phases: done
        with open(self.path, 'rb') as f:
            return f.read()

    def test_refuse_is_added_once_with_its_backup(self):
        for build in (B8, B9):
            with self.subTest(build=build):
                original = self._pre_p5_file(build)
                st = S.Store(self.path, client_build=build).load()
                self.assertEqual(st.migration_changes, ['test/TestHero: refuse created'])
                with open(self.path + '.bak-pre-p5', 'rb') as f:
                    self.assertEqual(f.read(), original)
                with open(self.path, encoding='utf-8') as f:
                    hero = json.load(f)['test']['characters'][0]
                self.assertEqual(hero['refuse'], privacy.default())
                again = S.Store(self.path, client_build=build).load()
                self.assertEqual((again.migration_changes, again.saves), ([], 0))
                # a second pre-P5 state never overwrites the first backup
                self._pre_p5_file(build)
                with open(self.path + '.bak-pre-p5', 'wb') as f:
                    f.write(b'first')
                S.Store(self.path, client_build=build).load()
                with open(self.path + '.bak-pre-p5', 'rb') as f:
                    self.assertEqual(f.read(), b'first')
                os.remove(self.path + '.bak-pre-p5')

    def test_new_characters_and_save_refuse(self):
        st = S.Store(self.path).load()
        ch = st.new_character('Nova', s10=1, s1=1, s6=2, s5=2, s9=2, stats=(3, 2, 1, 3))
        self.assertEqual(ch['refuse'], privacy.default())
        st.add_character('admin', ch)
        self.assertTrue(st.save_refuse(ch, {'party': 1}))
        self.assertFalse(st.save_refuse(ch, {'party': True}))           # unchanged: no dirty
        st.flush()
        disk = S.Store(self.path).load()
        self.assertEqual(disk.find_character('admin', 'Nova')['refuse'], {**privacy.default(), 'party': True})
        self.assertEqual(disk.migration_changes, [])


# ====================================================== server, both builds ===
class _TwoPlayers:
    """TestHero (test/test, client 1, P2P 42907) and Watcher (admin/admin, client 2, P2P
    42908) in world on map 101 of one server of `build`."""
    build = B8

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix=f'ws_registry_{self.build}_')
        self.server = self.make_server()
        self.seen = []
        for name in ALL_HOOKS:
            self.server.world.hooks.register(name, self._recorder(name))
        self.mc = F.MultiClient(self.server)
        self.a, self.b = self.mc
        self.mc.drain()

    def tearDown(self):
        self.mc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_server(self):
        overrides = {'CLIENT_BUILD': self.build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False}
        if self.build == B9 and not HAVE_2009_INSTALL:
            overrides['CLIENT_DIR_2009'] = cfgmod.DEFAULTS['CLIENT_DIR']
        return F.make_server(self.tmp, accounts=F.two_player_accounts(),
                             config=cfgmod.from_dict(overrides))

    def _recorder(self, name):
        def hook(server, session, **kw):
            keep = {k: kw[k] for k in ('map_code', 'old_map', 'reason', 'first', 'superseded') if k in kw}
            self.seen.append((name, session.get('char_name'), keep))
        hook.__name__ = f'record_{name}'
        return hook

    def hooks_of(self, name):
        return [(h, kw) for h, who, kw in self.seen if who == name]

    def extra_client(self):
        c = F.FakeClient(self.server)
        self.mc.clients.append(c)
        return c

    # ------------------------------------------------------------------ tests ---
    def test_both_players_are_indexed_on_their_map(self):
        w = self.server.world
        self.assertEqual({s['char_name'] for s in w.map_sessions(101)}, {'TestHero', 'Watcher'})
        self.assertEqual(w.peers(self.a.session), [self.b.session])
        self.assertEqual(w.peers(self.b.session), [self.a.session])
        self.assertIs(w.find('watcher'), self.b.session)
        self.assertIs(w.find(2), self.b.session)
        self.assertIs(w.find(1), self.a.session)
        self.assertEqual((WM.client_number(self.a.session), WM.client_number(self.b.session)), (1, 2))
        # on_enter_world ran once per player after its own 0x07, with first=True
        self.assertEqual(self.hooks_of('Watcher'),
                         [('before_server_map_load', {'map_code': 101, 'reason': 'enter_world'}),
                          ('on_enter_world', {'map_code': 101, 'reason': 'enter_world', 'old_map': None,
                                              'first': True})])

    def test_portal_moves_the_session_between_map_instances(self):
        self.seen.clear()
        self.a.send(0x7E, PORTAL_101_TO_102)
        self.assertTrue(_wait(lambda: self.server.world.map_of(self.a.session) == 102))
        self.a.recv_until_quiet()
        w = self.server.world
        self.assertEqual(w.peers(self.a.session), [])
        self.assertEqual(w.map_sessions(101), [self.b.session])
        self.assertEqual(self.hooks_of('TestHero'), [
            ('before_server_map_load', {'map_code': 102, 'reason': 'portal'}),
            ('on_map_change', {'old_map': 101, 'map_code': 102, 'reason': 'portal'}),
            ('on_enter_world', {'map_code': 102, 'reason': 'portal', 'old_map': 101, 'first': False})])
        self.assertEqual(self.hooks_of('Watcher'), [])
        # back on 101: peers again
        self.server._map_transfer(self.a.session['sock'], self.a.session, 101, 1411.0, 714.0, reason='test')
        self.a.recv_until_quiet()
        self.assertEqual(w.peers(self.b.session), [self.a.session])

    def test_disconnect_fires_leave_and_disconnect_exactly_once(self):
        self.seen.clear()
        a_session = self.a.session
        self.a.close()
        self.assertTrue(_wait(lambda: a_session.get('closed')))
        self.assertEqual(self.hooks_of('TestHero'), [
            ('on_leave_world', {'map_code': 101, 'reason': 'client closed', 'superseded': False}),
            ('on_disconnect', {'reason': 'client closed'})])
        w = self.server.world
        self.assertEqual(w.map_sessions(101), [self.b.session])
        self.assertIsNone(w.find('TestHero', in_world=False))
        self.assertIsNone(w.session(1))
        self.assertTrue(a_session['outbox'].closed)
        # a second close path (a kick racing the finally) changes nothing
        self.server._session_closed(a_session, 'again')
        self.assertEqual(len(self.hooks_of('TestHero')), 2)
        self.assertEqual(self.hooks_of('Watcher'), [])

    def test_a_duplicate_login_kick_leaves_the_world_once(self):
        self.seen.clear()
        old = self.a.session
        c = self.extra_client()
        self.assertEqual(c.login('test', 'test')['result'], 4)
        self.assertTrue(_wait(lambda: old.get('closed')))
        (leave, kw_leave), (disc, kw_disc) = self.hooks_of('TestHero')
        self.assertEqual((leave, kw_leave['map_code'], kw_leave['superseded']), ('on_leave_world', 101, False))
        self.assertIn('duplicate login', kw_leave['reason'])
        self.assertEqual(disc, 'on_disconnect')
        self.assertTrue(old['outbox'].closed)
        self.assertEqual(self.server.world.map_sessions(101), [self.b.session])

    def test_observer_broadcast_reaches_only_map_peers(self):
        fields = self.server.notice_fields('seen by the map')
        self.assertEqual(self.server._send_to_observers(self.a.session, '0x15', fields), 1)
        self.assertEqual(_text(self.b.s2c(self.b.expect(0x15))), 'seen by the map')
        self.a.expect_silence(0.2)                                       # never the source
        self.server._map_transfer(self.a.session['sock'], self.a.session, 102, 50.0, 712.0, reason='test')
        self.a.recv_until_quiet()
        # P5 stage 2 (world-presence): A leaving 101 despawns it on B - the only packet B gets
        self.assertEqual(self.b.s2c(self.b.expect(0x06)), {'uid': 1})
        self.assertEqual(self.server._send_to_observers(self.a.session, '0x15', fields), 0)
        self.assertEqual(self.server._send_to_observers(self.b.session, '0x15', fields), 0)
        self.b.expect_silence(0.2)

    def test_a_stalled_peer_never_blocks_the_sender(self):
        """F5 outbound path: A's handler pushes to B while holding A's combat lock; B's
        client has stopped reading. The push returns at once, A's own replies still flow,
        and B gets every packet in order (cipher order) once it reads again."""
        gate = threading.Event()
        outbox = self.b.session['outbox']
        real = outbox.sock
        outbox.sock = _Gate(real, gate)
        took = []
        try:
            def handler():
                t0 = time.monotonic()
                with self.server._combat_lock(self.a.session):
                    for i in range(40):
                        self.server._push(self.b.session, '0x15', self.server.notice_fields(f'line {i}'))
                took.append(time.monotonic() - t0)
            t = threading.Thread(target=handler)
            t.start()
            t.join(3)
            self.assertFalse(t.is_alive(), 'the push blocked on the stalled peer')
            self.assertLess(took[0], 1.0)
            self.assertGreaterEqual(outbox.pending(), 39)
            # A's combat lock is free and A's own connection thread still answers at once
            self.assertTrue(self.server._combat_lock(self.a.session).acquire(timeout=1))
            self.server._combat_lock(self.a.session).release()
            self.a.send(0x03, bytes([2]) + b'hi')
            self.assertEqual(P.to_bytes(self.a.s2c(self.a.expect(0x16))['text']), b'hi')
        finally:
            gate.set()
        pkts = self.b.recv_until_quiet(0.3)
        # the map chat broadcast (P5 stage 4) queued A's 'hi' behind the 40 on B's stalled
        # outbox without holding A up: B gets it last, in cipher order
        self.assertEqual([_text(self.b.s2c(p)) for p in pkts], [f'line {i}' for i in range(40)] + ['hi'])
        self.assertEqual(pkts[-1].opcode, 0x16)
        self.assertTrue(outbox.wait_idle())
        outbox.sock = real

    def test_admin_targets_by_client_exe_uid_and_name(self):
        line = lambda target: json.dumps({'opcode': 0x15, 'target': target,          # noqa: E731
                                          'payload_hex': P.build('0x15', self.server.notice_fields('x'),
                                                                 client_build=self.build).hex()})
        for target, want in (('c:2', self.b), ('client:1', self.a), (2, self.b), ('2', self.b),
                             ('TestHero', self.a)):
            with self.subTest(target=target):
                reply = self.server._admin_command(line(target))
                self.assertTrue(reply.startswith('ok 0x15'), reply)
                self.assertIn('sessions=1', reply)
                want.expect(0x15)
                other = self.a if want is self.b else self.b
                other.expect_silence(0.15)
        self.assertIn('no connected session matches', self.server._admin_command(line('c:3')))
        self.assertIn('no connected session matches', self.server._admin_command(line('c2')))

    def test_admin_names_that_look_like_targets_stay_names(self):
        """names.ALLOWED_RE lets a character be called 'c1' or '1' (review minor): the name
        wins for a string target, 'c:N' is always the exe, an int always the uid."""
        line = lambda target: json.dumps({'opcode': 0x15, 'target': target,          # noqa: E731
                                          'payload_hex': P.build('0x15', self.server.notice_fields('x'),
                                                                 client_build=self.build).hex()})
        self.b.session['char_name'] = 'c1'                   # client 2 (uid 2) called "c1"
        self.a.session['char_name'] = '2'                    # client 1 (uid 1) called "2"
        try:
            for target, want in (('c1', self.b), ('C1', self.b), ('c:1', self.a), ('2', self.a),
                                 (2, self.b), ('1', self.a), (1, self.a)):
                with self.subTest(target=target):
                    reply = self.server._admin_command(line(target))
                    self.assertIn('sessions=1', reply)
                    want.expect(0x15)
                    (self.a if want is self.b else self.b).expect_silence(0.15)
        finally:
            self.a.session['char_name'], self.b.session['char_name'] = 'TestHero', 'Watcher'

    def test_a_tick_under_the_world_lock_never_deadlocks_with_a_combat_lock_holder(self):
        """Review blocker: the tick scheduler runs callbacks under world_lock and they take
        session combat locks (_tick_debuffs / _tick_buffs every 0.05 s); a handler holding
        its combat lock asks World for its audience (skill-cast 0x3B observers, kill loot
        via _ground_sessions, drop-worn 0x24). World's leaf lock keeps both threads moving."""
        server = self.server
        self.assertIsNot(server.world.lock, server.world_lock)
        # both on map 102 (8 Pupu), so the debuff / AI ticks visit A's combat lock
        for c, x in ((self.a, 50.0), (self.b, 80.0)):
            server._map_transfer(c.session['sock'], c.session, 102, x, 712.0, reason='test')
            c.recv_until_quiet()
        self.assertTrue(self.a.session.get('monsters'))
        # world-shared-monsters: the debuff tick takes a caster's combat lock (then the map's
        # monster lock) for his running slots, so A poisons a Pupu whose tick is due now
        mob = next(iter(self.a.session['monsters'].values()))
        mob.hp = mob.max_hp = 500
        D.apply(mob, SK.skill_def(391), time.monotonic() - 1.0, src=self.a.session['uid'], tick_damage=1)
        holding, done = threading.Event(), {}
        lock_calls = []
        real_lock = server._combat_lock

        def spy(session):
            if threading.current_thread().name == 'tick':
                lock_calls.append(session.get('char_name'))
            return real_lock(session)
        server._combat_lock = spy

        def connection():
            with real_lock(self.a.session):
                holding.set()
                time.sleep(0.25)                               # the tick now waits for this lock
                done['observers'] = server._send_to_observers(self.a.session, '0x15',
                                                              server.notice_fields('under lock'))
                done['ground'] = len(server._ground_sessions(102))
                done['online'] = len(server._in_world_sessions())
            done['conn'] = True

        def tick():
            holding.wait(3)
            with server.world_lock:                            # ticks.Scheduler runs callbacks so
                server._tick_debuffs()
                server._tick_buffs()
                server._tick_regen()
            done['tick'] = True
        try:
            threads = [threading.Thread(target=connection, name='conn', daemon=True),
                       threading.Thread(target=tick, name='tick', daemon=True)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
            self.assertFalse(any(t.is_alive() for t in threads), f'DEADLOCK: {done}')
        finally:
            del server._combat_lock                            # back to the class method
        self.assertTrue(done.get('conn') and done.get('tick'))
        self.assertIn('TestHero', lock_calls)                  # the tick did contend for A's lock
        self.assertEqual((done['observers'], done['ground'], done['online']), (1, 2, 2))
        self.assertEqual(_text(self.b.s2c(self.b.expect(0x15))), 'under lock')

    def test_a_failing_disconnect_step_still_closes_the_outbox_and_socket(self):
        """Review minor: a raising save must not skip outbox.close() (its writer thread
        would wait for ever) or leave the account online; the socket closes too."""
        def broken_save(session, reason=None):
            raise RuntimeError('disk full')
        self.server._save_world_state = broken_save
        self.seen.clear()
        a_session = self.a.session
        server_sock = self.a._server_sock
        with self.assertLogs('WS', logging.ERROR) as logs:
            self.a.sock.close()                                # client side only: the server
            self.assertTrue(_wait(lambda: a_session['outbox'].closed))   # closes its own end
        self.assertTrue(any('save failed' in line for line in logs.output))
        self.assertEqual(a_session.get('closed'), 'client closed')
        self.assertIsNone(self.server.world.session(1))
        self.assertEqual(self.server.world.map_sessions(101), [self.b.session])
        self.assertEqual([h for h, _ in self.hooks_of('TestHero')], ['on_leave_world', 'on_disconnect'])
        self.assertTrue(_wait(lambda: server_sock.fileno() == -1))

    def test_the_socket_closes_even_when_the_disconnect_path_raises(self):
        caught = []
        saved_hook = threading.excepthook
        threading.excepthook = lambda args: caught.append(args.exc_type)

        def broken(session, reason):
            raise RuntimeError('boom')
        self.server._session_closed = broken
        server_sock = self.a._server_sock
        try:
            self.a.sock.close()                                # client side only (FakeClient.close
            self.assertTrue(_wait(lambda: server_sock.fileno() == -1))   # would close ours)
            self.assertTrue(_wait(lambda: caught == [RuntimeError]))
            self.assertTrue(_wait(lambda: not self.a.thread.is_alive()))
        finally:
            threading.excepthook = saved_hook

    def test_a_kick_closes_the_outbox_so_ticks_log_no_dead_connection_warning(self):
        """Review minor: a tick that still walks self.sessions after a kick gets the OSError
        it handles at put(), not a queued packet whose write fails with the WARNING that
        live check #6 reserves for a dead connection."""
        a_session = self.a.session
        records = []
        handler = logging.Handler(logging.WARNING)
        handler.emit = records.append
        log = logging.getLogger('WS')
        log.addHandler(handler)
        try:
            self.server._kick(a_session, 'test kick')
            self.assertTrue(a_session['outbox'].closed)
            self.assertFalse(self.server._push(a_session, '0x15', self.server.notice_fields('late')))
            a_session['hp'] = 1                                # regen would send 0x28 now
            self.server._tick_regen()
            self.assertTrue(_wait(lambda: a_session.get('closed')))
        finally:
            log.removeHandler(handler)
        self.assertEqual([r.getMessage() for r in records if 'outbox' in r.getMessage()], [])

    def test_wsdev_sendspec_to_client_2_reaches_only_client_b(self):
        """P5 exit criterion 8 offline: `wsdev sendspec 15 '{...}' --to 2` (uid) and
        `--to c:2` (the p2 exe) put the line on client B only."""
        import wsdev as D
        saved = D.BUILD, D._inject
        D.BUILD = self.build
        D._inject = lambda op, payload, target=None, state=None: self.server._admin_command(
            D.admin_line(op, payload, target, state))
        try:
            for to in ('2', 'c:2'):
                with self.subTest(to=to):
                    out = io.StringIO()
                    with redirect_stdout(out):
                        D.cmd_sendspec(['15', '{"msg_type": 0, "text": "only B"}', '--to', to])
                    self.assertIn('sessions=1', out.getvalue())
                    self.assertEqual(_text(self.b.s2c(self.b.expect(0x15))), 'only B')
                    self.a.expect_silence(0.15)
        finally:
            D.BUILD, D._inject = saved

    def test_who_names_the_client_exe(self):
        self.a.session['gm'] = 1
        self.server._dev_who(self.a.session, '')
        lines = [_text(self.a.s2c(p)) for p in self.a.recv_until_quiet(0.3)]
        self.assertEqual(lines[0], '2 session(s) online:')
        self.assertIn('TestHero map 101 [test uid 1] c:1 GM', lines[1])
        self.assertIn('Watcher map 101 [admin uid 2] c:2', lines[2])

    def test_privacy_flags_from_0x2B_and_0x40_are_stored_and_never_answered(self):
        c = self.extra_client()
        self.a.close()
        self.assertTrue(_wait(lambda: self.server.world.session(1) is None))
        self.assertEqual(c.login('test', 'test')['result'], 1)
        c.enter_world('TestHero', refuse={'whisper': True, 'friend': True})
        want = {**privacy.default(), 'whisper': True, 'friend': True}
        self.assertEqual(c.session['refuse'], want)
        hero = self.server.store.find_character('test', 'TestHero')
        self.assertEqual(hero['refuse'], want)
        # C2S 0x40: whisper off, exchange + party on - no reply of any kind
        c.send_c2s(PRIVACY_KEY[self.build], {'refuse_whisper': 0, 'refuse_exchange': 1, 'refuse_party': 1,
                                             'refuse_talk': 0, 'refuse_friend': 0})
        c.expect_silence(0.3)
        now = {**privacy.default(), 'exchange': True, 'party': True}
        self.assertTrue(_wait(lambda: c.session['refuse'] == now))
        self.assertEqual(hero['refuse'], now)
        self.server.store.flush()
        with open(self.server.db_file, encoding='utf-8') as f:
            self.assertEqual(json.load(f)['test']['characters'][0]['refuse'], now)
        # the consumer API, by session and by name
        self.assertTrue(self.server.refuses(c.session, 'exchange'))
        self.assertTrue(self.server.refuses('testhero', 'party'))
        self.assertFalse(self.server.refuses('TestHero', 'whisper'))
        self.assertFalse(self.server.refuses('nobody', 'whisper'))
        # a malformed 0x40 (4 of 5 bytes) changes nothing and is not answered either
        c.send(0x40, b'\x01\x01\x01\x01')
        c.expect_silence(0.3)
        self.assertEqual(c.session['refuse'], now)

    def test_refuses_reads_the_stored_flags_of_an_offline_character(self):
        self.b.send(0x40, bytes([0, 0, 0, 1, 1]))                        # talk + friend
        self.assertTrue(_wait(lambda: self.server.refuses('Watcher', 'friend')))
        b_session = self.b.session
        self.b.close()
        self.assertTrue(_wait(lambda: b_session.get('closed')))
        self.assertIsNone(self.server.world.by_char_name('Watcher'))
        self.assertTrue(self.server.refuses('Watcher', 'talk'))           # from the record
        self.assertFalse(self.server.refuses('Watcher', 'exchange'))


class Registry2008(_TwoPlayers, unittest.TestCase):
    build = B8


class Registry2009(_TwoPlayers, unittest.TestCase):
    build = B9

    def test_a_2009_relogin_leaves_the_old_session_superseded(self):
        """The 2009 client reconnects with its live session key: the new session owns uid 1
        before the old connection's finally runs, so on_leave_world says superseded (a
        presence 0x06 for uid 1 would despawn the NEW session on the peers' screens)."""
        self.seen.clear()
        old = self.a.session
        key = old['session_key']
        c = self.extra_client()
        self.assertEqual(c.login('test', 'test', session_key=key)['result'], 1)
        self.assertTrue(_wait(lambda: old.get('closed')))
        (leave, kw), (disc, _) = self.hooks_of('TestHero')
        self.assertEqual((leave, kw['map_code'], kw['superseded'], disc),
                         ('on_leave_world', 101, True, 'on_disconnect'))
        c.enter_world('TestHero')
        self.assertIs(self.server.world.find(1), c.session)
        self.assertEqual(set(id(s) for s in self.server.world.map_sessions(101)),
                         {id(c.session), id(self.b.session)})


if __name__ == '__main__':
    unittest.main(verbosity=1)
