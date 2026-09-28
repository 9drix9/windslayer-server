#!/usr/bin/env python3
"""
test_presence.py - P5 stage 2 (presence-movement), offline, both client builds
==============================================================================
- world-presence (+party-mp-presence, trade-mp-presence, social_friend-player-visibility):
  S2C 0x04 to the entrant (<= 5 rows and <= 2038 B per packet) and 0x05 to its peers after
  the own 0x07, with the real record (alive, clothed, name, GM tag, level, idle motion, y on
  the floor - C2); 0x06 to the old map's peers on a portal, a disconnect, a kick and a 2009
  relogin's old session - never a duplicate entity, never a dangling one, never the
  receiver's own uid; the GM shadow (gm_hidden) is not shown to a non-GM client.
- world-move-relay (+trade-move-relay): C2S 0x0D -> one S2C 0x1B node per packet to every
  peer that holds the mover (hold = logic_elapsed_ms capped at 990, masked state words,
  target / target point / position tails by ae / vb / ie), NEVER back to the mover; stale-map
  packets and pure idle keepalives are not relayed.
- world-position-estimate (spike S-1): dead reckoning at 0.25 px/ms along the EN floor
  lines, fixes from interact tails and arrival points, the estimate in late joiners'
  records and in the save; the dev driver's sample as a fix or only as an error
  measurement (POSITION_DRIVER_FIX); `!where`.
- the dropped-connection keepalive (DEAD_PEER_SECS) and en_maps.MapData.floor_near.

Every flow runs the real GameServer loop through fakeclient (two or more clients on one
server, socketpairs): no port is bound, no client is started and the live accounts.json is
never opened (temp copies; the module checks its hash at the end).
"""
import hashlib
import json
import logging
import os
import select
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import en_maps as M  # noqa: E402
import fakeclient as F  # noqa: E402
import packets as P  # noqa: E402
import presence as PR  # noqa: E402

W = F.import_server()
cfgmod = W.cfgmod
EC = W.EC
R = W.R
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = '2008', '2009'
HAVE_2009_INSTALL = os.path.exists(os.path.join(HERE, cfgmod.DEFAULTS['CLIENT_DIR_2009'], 'hs', 'windslayer.hii'))
MOVE_KEY = {B8: '0x42CE94/0x0D', B9: '0x42E704/0x0D'}
PORTAL_101_TO_102 = bytes.fromhex('17000000')          # live 0x7E capture: 101 line 23
PORTAL_102_TO_101 = bytes.fromhex('1f000000')          # live: 102 line 31
FLOOR_101 = 814.0                                      # live: arrival (1411,714) falls to 814
RIGHT, LEFT = 0x2, 0x1
GARBAGE_LO, GARBAGE_HI = 0x53400000, 0x75646000        # the live capture's stale scratch bits


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
            'accounts.json changed during test_presence.py (tests must only use temp copies)'


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _text(rec):
    return P.to_bytes(rec['text']).decode('cp949')


def _name(value):
    return P.cut_text(value, 16).decode('cp949')


class ClientModel:
    """What a client's scene holds, from the packets it got: every 0x04 / 0x05 row allocates
    an entity (no de-duplication, world doc 1.2), 0x06 frees every entity of that uid."""

    def __init__(self, client):
        self.client = client
        self.entities = []                  # uids, one per allocated entity

    def feed(self, pkts):
        for pkt in pkts:
            if pkt.opcode == 0x03:                  # a map load destroys every entity
                self.entities = []
            elif pkt.opcode == 0x04:
                self.entities += [row['uid'] for row in self.client.s2c(pkt)['repeat[player_count]']]
            elif pkt.opcode == 0x05:
                self.entities.append(self.client.s2c(pkt)['uid'])
            elif pkt.opcode == 0x06:
                uid = self.client.s2c(pkt)['uid']
                self.entities = [u for u in self.entities if u != uid]
        return self

    def count(self, uid):
        return self.entities.count(uid)


class _DroppingSocket:
    """The server's end of a connection whose peer vanished without a FIN/RST: every call
    goes to the real socket until drop(), then recv fails the way Windows reports a failed
    keepalive / TCP_MAXRT (WSAETIMEDOUT 10060 -> TimeoutError with an errno)."""

    def __init__(self, real):
        self._real = real
        self._dead = threading.Event()

    def __getattr__(self, name):
        return getattr(self._real, name)

    def drop(self):
        self._dead.set()

    def recv(self, n):
        while not self._dead.is_set():
            ready, _, _ = select.select([self._real], [], [], 0.02)
            if ready:
                return self._real.recv(n)
        raise TimeoutError(10060, 'A connection attempt failed because the connected party did not '
                                  'properly respond after a period of time')


class _Presence:
    """TestHero (test/test, client 1, uid 1) and Watcher (admin/admin, client 2, uid 2) in
    world on map 101 at the portal arrival point (1411, 714) of one server of `build`."""
    build = B8

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix=f'ws_presence_{self.build}_')
        self.server = self.make_server()
        self.mc = F.MultiClient(self.server)
        self.a, self.b = self.mc
        self.pending = self.mc.drain()

    def tearDown(self):
        self.mc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def config(self, **extra):
        values = {'CLIENT_BUILD': self.build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False, **extra}
        if self.build == B9 and not HAVE_2009_INSTALL:
            values['CLIENT_DIR_2009'] = cfgmod.DEFAULTS['CLIENT_DIR']
        return cfgmod.from_dict(values)

    def make_server(self, accounts=None, **extra):
        return F.make_server(self.tmp, accounts=accounts or F.two_player_accounts(),
                             config=self.config(**extra))

    # ----------------------------------------------------------------- helpers ---
    def move(self, c, lo, elapsed=210, map_code=101, hi=0, **tail):
        fields = {'realtime_delta_ms': 0, 'map_code': map_code, 'logic_elapsed_ms': elapsed,
                  'state_lo': lo, 'state_hi': hi}
        if (lo >> 16) & 0xF:
            fields.update({'target_uid': 0, 'pos_x': 0.0, 'pos_y': 0.0, 'target_dx': 0.0, 'target_dy': 0.0,
                           'flag_8db': 0, 'flag_8e7': 0, 'timer_dac': 0, 'target_action_event': 0})
        fields.update(tail)
        c.send_c2s(MOVE_KEY[self.build], fields)

    def walk(self, c, lo, steps, elapsed=210):
        """`steps` packets holding `lo`, then the stop; returns the ms the input was held."""
        self.move(c, lo, elapsed=30)                   # the key goes down (prev input: idle)
        for _ in range(steps):
            self.move(c, lo, elapsed=elapsed)
        self.move(c, 0, elapsed=elapsed)               # released: this span was walked too
        return (steps + 1) * elapsed

    def relays(self, c, quiet=0.25):
        pkts = c.recv_until_quiet(quiet)
        self.assertEqual({p.opcode for p in pkts} - {0x1B}, set(), [hex(p.opcode) for p in pkts])
        return [c.s2c(p) for p in pkts]

    def row_of(self, pkt, client):
        rows = client.s2c(pkt)['repeat[player_count]']
        self.assertEqual(len(rows), 1)
        return rows[0]

    def motion_names(self):
        if self.build == B9:
            return {'input_state_93f': 8, 'input_state_940': 0, 'action_state_994': 8,
                    'anim_substate_95b': 8, 'direction_949': 2}
        return {'input_state_8b3': 8, 'input_state_8b4': 0, 'action_state_904': 8,
                'anim_substate_8cf': 8, 'direction_8bd': 2}

    def gm_field(self):
        return 'gm_or_guild_id' if self.build == B9 else 'gm_level'

    def ops(self, pkts):
        return [p.opcode for p in pkts]

    # ------------------------------------------------------------ presence ---
    def test_each_player_is_spawned_once_on_the_other_client(self):
        """Exit criterion 1 offline: B's map load carries A in one 0x04 after its own 0x07; A
        gets B as one 0x05. Alive (cur_hp), clothed (the store look), named, idle motion, y on
        the floor (C2: a remote record is not settled by gravity), never the receiver's uid."""
        self.assertNotIn(0x04, self.ops(self.a.entry))              # A was alone
        ops = self.ops(self.b.entry)
        self.assertEqual(ops.count(0x04), 1)
        self.assertLess(ops.index(0x07), ops.index(0x04))
        self.assertLess(ops.index(0x04), ops.index(0x28))           # before the HP/MP/monsters
        row = self.row_of(next(p for p in self.b.entry if p.opcode == 0x04), self.b)
        hero = self.server.store.find_character('test', 'TestHero')
        look = R.appearance_2009(hero) if self.build == B9 else R.appearance(hero)
        self.assertEqual((_name(row['name']), row['uid']), ('TestHero', 1))
        self.assertEqual([e['appearance_part'] for e in row[f'repeat[{len(look)}]']], look)
        self.assertGreater(row['cur_hp'], 0)
        self.assertEqual((row['pos_x'], row['pos_y']), (1411.0, FLOOR_101))
        for k, v in self.motion_names().items():
            self.assertEqual(row[k], v, k)
        self.assertEqual(row[self.gm_field()], 0)
        self.assertEqual(row['level'], R.level_of(hero))
        # A: exactly one 0x05 with Watcher
        (pkt,) = self.pending['TestHero']
        self.assertEqual(pkt.opcode, 0x05)
        rec = self.a.s2c(pkt)
        self.assertEqual((_name(rec['name']), rec['uid'], rec['pos_y']), ('Watcher', 2, FLOOR_101))
        self.assertGreater(rec['cur_hp'], 0)
        self.assertEqual(self.pending['Watcher'], [])
        # the server's books: each client holds exactly the other one
        self.assertEqual(PR.spawned(self.a.session), {2: self.b.session})
        self.assertEqual(PR.spawned(self.b.session), {1: self.a.session})

    def test_a_gm_record_carries_the_gm_tag_and_a_shadowed_gm_is_not_shown(self):
        accounts = F.two_player_accounts()
        accounts['test']['characters'][0]['gm'] = 1
        self.mc.close()
        self.server = self.make_server(accounts)
        with F.MultiClient(self.server) as mc:
            a, b = mc
            row = self.row_of(next(p for p in b.entry if p.opcode == 0x04), b)
            self.assertEqual((row[self.gm_field()], row['gm_hidden']), (1, 0))   # "Game Master"
        # the GM in shadow: a non-GM client gets no record and no relay; a GM client does
        accounts['test']['characters'][0]['gm_hidden'] = 1
        self.server = self.make_server(accounts)
        with F.MultiClient(self.server) as mc:
            a, b = mc
            mc.drain()
            self.assertNotIn(0x04, self.ops(b.entry))
            self.assertEqual(PR.spawned(b.session), {})
            self.assertEqual(PR.spawned(a.session), {2: b.session})              # A sees B
            self.move(a, RIGHT)
            b.expect_silence(0.2)
            self.assertTrue(PR.visible_to(a.session, {'gm': 1}))

    def test_portal_despawns_on_the_old_map_and_the_return_spawns_once(self):
        """Exit criterion 4 offline: A portals 101 -> 102: B gets exactly 0x06 {1}, A's 102 load
        shows nobody. Back on 101: B gets ONE 0x05, A's load has B once."""
        model = ClientModel(self.b).feed(self.b.entry)
        self.assertEqual(model.count(1), 1)
        self.a.send(0x7E, PORTAL_101_TO_102)
        self.assertTrue(_wait(lambda: self.server.world.map_of(self.a.session) == 102))
        on_102 = self.a.recv_until_quiet()
        self.assertNotIn(0x04, self.ops(on_102))
        got = self.b.recv_until_quiet()
        self.assertEqual(self.ops(got), [0x06])
        self.assertEqual(self.b.s2c(got[0]), {'uid': 1})
        self.assertEqual(model.feed(got).count(1), 0)
        self.assertEqual(PR.spawned(self.a.session), {})
        self.assertEqual(PR.spawned(self.b.session), {})
        self.move(self.a, RIGHT, map_code=102)                     # nobody on 102 to relay to
        self.b.expect_silence(0.2)
        self.a.send(0x7E, PORTAL_102_TO_101)
        self.assertTrue(_wait(lambda: self.server.world.map_of(self.a.session) == 101))
        back = self.a.recv_until_quiet()
        self.assertEqual([r['uid'] for p in back if p.opcode == 0x04
                          for r in self.a.s2c(p)['repeat[player_count]']], [2])
        got = self.b.recv_until_quiet()
        self.assertEqual(self.ops(got), [0x05])
        self.assertEqual(model.feed(got).count(1), 1)
        self.assertEqual(ClientModel(self.a).feed(back).entities, [2])

    def test_a_closed_client_despawns_on_its_peers_at_once(self):
        """Exit criterion 4 (second half): closing A -> B gets 0x06 {1} well inside 2 s, and
        B's books are empty (no dangling entity)."""
        a_session = self.a.session
        t0 = time.monotonic()
        self.a.close()
        pkt = self.b.recv(2.0)
        self.assertIsNotNone(pkt, 'no despawn within 2 s')
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual((pkt.opcode, self.b.s2c(pkt)), (0x06, {'uid': 1}))
        self.assertTrue(_wait(lambda: a_session.get('closed')))
        self.assertEqual(PR.spawned(self.b.session), {})
        self.b.expect_silence(0.2)

    def test_a_silently_dropped_connection_despawns_when_the_keepalive_gives_up(self):
        """A dropped peer (no FIN/RST) makes recv fail with the stack's WSAETIMEDOUT once the
        DEAD_PEER_SECS keepalive gives up: a TimeoutError WITH an errno, which the recv loop
        must not take for its own idle recv timeout (errno None) and keep waiting on."""
        self.mc.close()
        self.server = self.make_server()
        real_pair = socket.socketpair
        dropping = []

        def pair():
            client_end, server_end = real_pair()
            wrapped = _DroppingSocket(server_end)
            dropping.append(wrapped)
            return client_end, wrapped
        F.socket.socketpair = pair
        try:
            a = F.FakeClient(self.server)
        finally:
            F.socket.socketpair = real_pair
        b = F.FakeClient(self.server)
        self.mc = F.MultiClient.__new__(F.MultiClient)
        self.mc.clients = [a, b]
        a.login('test', 'test')
        a.enter_world('TestHero')
        b.login('admin', 'admin')
        b.enter_world('Watcher', port=F.P2P_PORT_BASE + 1)
        self.assertEqual(self.ops(a.recv_until_quiet()), [0x05])
        a_session = a.session
        t0 = time.monotonic()
        dropping[0].drop()
        pkt = b.recv(2.0)
        self.assertIsNotNone(pkt, 'no despawn within 2 s of the dropped connection')
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual((pkt.opcode, b.s2c(pkt)), (0x06, {'uid': 1}))
        self.assertTrue(_wait(lambda: a_session.get('closed') == 'connection timed out'))

    def test_a_duplicate_login_kick_despawns_the_old_session(self):
        c = F.FakeClient(self.server)
        self.mc.clients.append(c)
        self.assertEqual(c.login('test', 'test')['result'], 4)
        self.assertEqual(self.ops(self.b.recv_until_quiet()), [0x06])
        self.assertEqual(PR.spawned(self.b.session), {})

    def test_the_mid_map_load_receiver_and_a_departed_subject_get_nothing(self):
        """The receiver's own 0x03 wipes its scene: a record pushed after its map load began
        would duplicate the one its arrival 0x04 carries, so it is skipped (re-checked under
        the receiver's lock); a subject that already left the map is not shown either."""
        b = self.b.session
        PR.clear(b)
        b['in_world'] = False
        try:
            self.assertFalse(PR.spawn(self.server, self.a.session, b))
            self.assertEqual(PR.relay(self.server, self.a.session,
                                      {'map_code': 101, 'logic_elapsed_ms': 30, 'state_lo': RIGHT}), 0)
        finally:
            b['in_world'] = True
        self.b.expect_silence(0.2)
        departed = self.server.world.depart(self.a.session)
        try:
            self.assertFalse(PR.spawn(self.server, self.a.session, b))
        finally:
            self.server.world.arrive(self.a.session, departed)
        self.assertTrue(PR.spawn(self.server, self.a.session, b))       # both back: shown once
        self.assertFalse(PR.spawn(self.server, self.a.session, b))
        self.assertEqual(self.ops(self.b.recv_until_quiet()), [0x05])

    def test_portal_races_leave_no_duplicate_and_no_ghost(self):
        """Both clients portal 101 <-> 102 at the same time, over and over: whatever order the
        map loads, entries and departures interleave in, each client ends with exactly the
        other one (the per-receiver books + the checks under the receiver's lock)."""
        models = {c: ClientModel(c).feed(c.entry).feed(self.pending[c.char_name]) for c in (self.a, self.b)}
        errors = []

        def run(c, rounds):
            try:
                for _ in range(rounds):
                    for portal, dest in ((PORTAL_101_TO_102, 102), (PORTAL_102_TO_101, 101)):
                        c.send(0x7E, portal)
                        end = time.monotonic() + 3
                        while self.server.world.map_of(c.session) != dest or not c.session.get('in_world'):
                            pkt = c.recv(0.05)
                            if pkt is not None:
                                models[c].feed([pkt])
                            if time.monotonic() > end:
                                raise AssertionError(f'{c.char_name} never reached {dest}')
            except Exception as e:                                 # noqa: BLE001 - reported below
                errors.append(e)
        threads = [threading.Thread(target=run, args=(c, 6), daemon=True) for c in (self.a, self.b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        for c in (self.a, self.b):
            models[c].feed(c.recv_until_quiet(0.3))
        self.assertEqual(models[self.a].entities, [2])
        self.assertEqual(models[self.b].entities, [1])
        self.assertEqual(PR.spawned(self.a.session), {2: self.b.session})
        self.assertEqual(PR.spawned(self.b.session), {1: self.a.session})

    # -------------------------------------------------------------- relay ---
    def test_a_walk_is_streamed_to_the_peer_and_never_echoed_to_the_mover(self):
        """Exit criterion 2 offline: one 0x1B node per C2S 0x0D (C3), hold = logic_elapsed_ms,
        the state words masked to lo bits 0-21 / hi bits 0-11 (stale scratch cleared); A itself
        gets nothing at all (the 0x0D echo desynced the cipher)."""
        self.move(self.a, RIGHT | GARBAGE_LO, elapsed=30, hi=GARBAGE_HI | 250)
        self.move(self.a, RIGHT | GARBAGE_LO, elapsed=210, hi=GARBAGE_HI)
        self.move(self.a, 0x00C | RIGHT, elapsed=210)                      # jump while walking
        self.move(self.a, 0, elapsed=150)                                  # stop
        nodes = self.relays(self.b)
        self.assertEqual([(n['uid'], n['hold_ms']) for n in nodes], [(1, 30), (1, 210), (1, 210), (1, 150)])
        self.assertEqual([struct.unpack('<II', P.to_bytes(n['state_blob'])) for n in nodes],
                         [(RIGHT, 250), (RIGHT, 0), (0x00E, 0), (0, 0)])
        self.a.expect_silence(0.3)

    def test_relay_tails_follow_ae_vb_and_ie(self):
        # ae (a hit reaction): the source uid rides as target_uid (+0xD80)
        self.move(self.a, 6 << 12, elapsed=30, event_source_uid=0x12345)
        # ae 7 with vb 4: + the target point (+0x1338 / +0x1340)
        self.move(self.a, (7 << 12) | (4 << 9), elapsed=30, event_source_uid=0x12345,
                  f64_1338=10.5, f64_1340=-2.25)
        # ie (an interact tail): the position rides along, the rest of the tail does not
        self.move(self.a, 9 << 16, elapsed=30, pos_x=1500.0, pos_y=FLOOR_101, target_uid=7)
        first, second, third = self.relays(self.b)
        self.assertEqual(first['target_uid'], 0x12345)
        self.assertNotIn('target_x', first)
        self.assertEqual((second['target_x'], second['target_y']), (10.5, -2.25))
        self.assertEqual((third['pos_x'], third['pos_y']), (1500.0, FLOOR_101))
        self.assertNotIn('target_uid', third)
        self.a.expect_silence(0.2)

    def test_the_hold_is_capped_and_keepalives_and_stale_packets_are_not_relayed(self):
        self.move(self.a, RIGHT, elapsed=4000)                 # first step after a long idle
        self.move(self.a, RIGHT, elapsed=400)                  # a stalled client's long step
        self.move(self.a, 0, elapsed=210)                      # the stop ...
        self.move(self.a, 0, elapsed=210)                      # ... and the one after it: relayed
        self.move(self.a, 0, elapsed=15000)                    # idle keepalive: not relayed
        self.move(self.a, 0x00C, elapsed=3000)                 # a jump from standing
        self.move(self.a, 0, elapsed=1200)
        self.move(self.a, RIGHT, elapsed=30, map_code=102)     # sent before a map load: dropped
        holds = [n['hold_ms'] for n in self.relays(self.b)]
        # standing still moves nothing: a node that starts from idle waits one tick, not 990
        self.assertEqual(holds, [PR.START_HOLD_MS, 400, 210, 210, PR.START_HOLD_MS, PR.HOLD_MAX_MS])
        self.assertEqual(self.a.session['pos'][0], 1411.0 + 0.25 * (400 + 210))  # stale: no move

    # ----------------------------------------------------------- estimate ---
    def test_the_estimate_dead_reckons_the_walk_on_the_floor_and_takes_fixes(self):
        """world-position-estimate: 0.25 px/ms x the time the PREVIOUS input was held, y on
        the floor line (the arrival point is 100 px above it), the interact tail as a fix,
        the map's line span as the limit."""
        held = self.walk(self.a, RIGHT, steps=3)
        self.assertTrue(_wait(lambda: self.a.session['move'].get('stats', {}).get('packets') == 5))
        x, y = self.a.session['pos']
        self.assertAlmostEqual(x, 1411.0 + PR.WALK_PX_PER_MS * held)
        self.assertEqual(y, FLOOR_101)
        held_left = self.walk(self.a, LEFT, steps=1)
        self.assertTrue(_wait(lambda: self.a.session['move']['stats']['packets'] == 8))
        self.assertAlmostEqual(self.a.session['pos'][0], x - PR.WALK_PX_PER_MS * held_left)
        # an attack pose does not walk; an interact tail is the position outright
        self.move(self.a, RIGHT | (1 << 2), elapsed=30)
        self.move(self.a, 9 << 16, elapsed=210, pos_x=1234.5, pos_y=800.0)
        self.assertTrue(_wait(lambda: self.a.session['pos'] == (1234.5, 800.0)))
        self.assertEqual(self.a.session['move']['fix'], PR.FIX_INTERACT)
        # a long walk right stops at the map's last collision line
        self.move(self.a, RIGHT, elapsed=30)
        self.move(self.a, RIGHT, elapsed=5000)
        self.move(self.a, 0, elapsed=5000)
        bounds = M.load_map(101).x_bounds()
        self.assertTrue(_wait(lambda: self.a.session['pos'][0] == bounds[1]))
        self.relays(self.b)
        # the save and the relog use the estimate
        a_session = self.a.session
        self.a.close()
        self.assertTrue(_wait(lambda: a_session.get('closed')))
        self.server.store.flush()
        with open(self.server.db_file, encoding='utf-8') as f:
            stored = json.load(f)['test']['characters'][0]
        self.assertEqual((stored['x'], stored['y']), (bounds[1], FLOOR_101))

    def test_a_late_joiner_sees_the_walker_where_the_server_estimates_him(self):
        held = self.walk(self.a, RIGHT, steps=2)
        self.relays(self.b)
        # B leaves and comes back: its 0x04 carries A at the estimate, on the floor
        self.b.send(0x7E, PORTAL_101_TO_102)
        self.assertTrue(_wait(lambda: self.server.world.map_of(self.b.session) == 102))
        self.b.recv_until_quiet()
        self.b.send(0x7E, PORTAL_102_TO_101)
        self.assertTrue(_wait(lambda: self.server.world.map_of(self.b.session) == 101))
        back = self.b.recv_until_quiet()
        (row,) = [r for p in back if p.opcode == 0x04 for r in self.b.s2c(p)['repeat[player_count]']]
        self.assertEqual((row['uid'], row['pos_x'], row['pos_y']),
                         (1, 1411.0 + PR.WALK_PX_PER_MS * held, FLOOR_101))
        # A saw B leave and come back
        self.assertEqual(self.ops(self.a.recv_until_quiet()), [0x06, 0x05])

    def test_the_driver_sample_is_a_fix_or_only_a_measurement(self):
        """POSITION_DRIVER_FIX: the dev memory read replaces the estimate (default) or only
        measures its error (spike S-1). Never needed: the estimate runs without it."""
        s = self.a.session
        s['pos'] = (1500.0, FLOOR_101)
        s['map_confirmed'] = s['current_map']        # the client's 0x0D named this map
        self.server.config['POSITION_DRIVER_FIX'] = False
        self.assertTrue(self.server._track_driver_position(1, 1530.0, FLOOR_101 - 40))
        self.assertEqual(s['pos'], (1500.0, FLOOR_101))
        err = s['move']['error']
        self.assertEqual((err['n'], round(err['last'])), (1, 50))
        self.server.config['POSITION_DRIVER_FIX'] = True
        self.assertTrue(self.server._track_driver_position(1, 1530.0, FLOOR_101))
        self.assertEqual(s['pos'], (1530.0, FLOOR_101))
        self.assertEqual((s['move']['fix'], s['move']['error']['n'], round(s['move']['error']['max'])),
                         (PR.FIX_DRIVER, 2, 50))

    def test_where_prints_the_estimate_and_what_each_client_holds(self):
        self.walk(self.a, RIGHT, steps=1)
        self.relays(self.b)
        self.a.session['gm'] = 1
        self.a.send(0x03, bytes([len('!where')]) + b'!where')
        lines = [_text(self.a.s2c(p)) for p in self.a.recv_until_quiet(0.3) if p.opcode == 0x15]
        self.assertEqual(lines[0], '2 player(s) on map 101:')
        self.assertTrue(lines[1].startswith(f'TestHero uid 1 map 101 ({1411 + 0.25 * 420:.0f},814) floor 814 '
                                            f'fix dead reckoning'), lines[1])
        self.assertEqual(lines[2], '  0x0D 3 (ae 0, ie 0)')
        self.assertEqual(lines[3], '  holds 1: Watcher')
        self.assertTrue(lines[4].startswith('Watcher uid 2 map 101 (1411,714) floor 814 fix arrival'), lines[4])
        self.assertEqual(lines[6], '  holds 1: TestHero')
        # with the dev driver measuring (POSITION_DRIVER_FIX false) the error is listed too
        self.server.config['POSITION_DRIVER_FIX'] = False
        self.server._track_driver_position(1, 1411 + 0.25 * 420 + 12, 814.0)
        self.server._dev_where(self.a.session, '')
        lines = [_text(self.a.s2c(p)) for p in self.a.recv_until_quiet(0.3)]
        self.assertEqual(lines[2], '  0x0D 3 (ae 0, ie 0); err last 12 max 12 mean 12 px (n 1)')
        self.assertTrue(all(len(line.encode()) <= 87 for line in lines))


class Presence2008(_Presence, unittest.TestCase):
    build = B8


class Presence2009(_Presence, unittest.TestCase):
    build = B9

    def test_a_2009_relogin_leaves_exactly_one_entity_on_the_peer(self):
        """The 2009 client reconnects with its live key (channel change): the new session owns
        uid 1 before the old connection's finally runs. Whatever the order, B ends with ONE
        entity of uid 1 - the new session's - and the old session's leave never removes it."""
        model = ClientModel(self.b).feed(self.b.entry)
        old = self.a.session
        c = F.FakeClient(self.server)
        self.mc.clients.append(c)
        self.assertEqual(c.login('test', 'test', session_key=old['session_key'])['result'], 1)
        self.assertTrue(_wait(lambda: old.get('closed')))
        entry = c.enter_world('TestHero')
        self.assertEqual([r['uid'] for p in entry if p.opcode == 0x04
                          for r in c.s2c(p)['repeat[player_count]']], [2])
        model.feed(self.b.recv_until_quiet())
        self.assertEqual(model.count(1), 1)
        self.assertIs(PR.spawned(self.b.session)[1], c.session)
        self.move(c, RIGHT, elapsed=30)
        self.assertEqual([n['uid'] for n in self.relays(self.b)], [1])


class ManyPlayers(unittest.TestCase):
    """Seven players on one map: the seventh entrant's 0x04s hold at most 5 rows and 2038 B
    each (world_movement_npc.md F1 step 5), every other client gets it once as 0x05."""

    def run_build(self, build):
        tmp = tempfile.mkdtemp(prefix=f'ws_presence_many_{build}_')
        try:
            accounts = F.two_player_accounts()
            players = [('test', 'test', 'TestHero'), ('admin', 'admin', 'Watcher')]
            for i in range(3, 8):
                accounts[f'user{i}'] = {'password': 'pw', 'characters': [
                    {'name': f'Player{i}', 'level': 1, 'class': 0, 'map': 101,
                     'x': 1411.0, 'y': 714.0, 'hp': 100, 'mp': 50,
                     'skills': [], 'buffs': []}]}
                players.append((f'user{i}', 'pw', f'Player{i}'))
            values = {'CLIENT_BUILD': build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False}
            if build == B9 and not HAVE_2009_INSTALL:
                values['CLIENT_DIR_2009'] = cfgmod.DEFAULTS['CLIENT_DIR']
            server = F.make_server(tmp, accounts=accounts, config=cfgmod.from_dict(values))
            with F.MultiClient(server, players=players) as mc:
                last = mc[6]
                lists = [p for p in last.entry if p.opcode == 0x04]
                rows = [r['uid'] for p in lists for r in last.s2c(p)['repeat[player_count]']]
                self.assertEqual(sorted(rows), sorted(c.session['uid'] for c in mc.clients[:6]))
                self.assertTrue(all(len(p.payload) <= PR.MAX_S2C_PAYLOAD for p in lists))
                self.assertEqual([len(last.s2c(p)['repeat[player_count]']) for p in lists], [5, 1])
                pending = mc.drain()
                for c in mc.clients:
                    model = ClientModel(c).feed(c.entry).feed(pending[c.char_name])
                    others = sorted(x.session['uid'] for x in mc.clients if x is not c)
                    self.assertEqual(sorted(model.entities), others, c.char_name)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_2008(self):
        self.run_build(B8)

    def test_2009(self):
        P.STRICT_FIELDS.add(B9)
        self.run_build(B9)


class Units(unittest.TestCase):
    def test_state_words(self):
        self.assertEqual(PR.masked(0x53400002, 0x756460FA), (0x2, 0xFA))
        self.assertEqual((PR.direction(1), PR.direction(2), PR.direction(3), PR.direction(0)), (-1, 1, 0, 0))
        self.assertTrue(PR.is_moving(RIGHT) and PR.is_moving(0x00C | LEFT))
        self.assertFalse(PR.is_moving(RIGHT | (1 << 2)))          # attack pose
        self.assertFalse(PR.is_moving(RIGHT | (6 << 12)))         # a hit reaction
        self.assertTrue(PR.is_idle(0) and PR.is_idle(0x00200000))  # +0x8D0 alone is idle
        self.assertFalse(PR.is_idle(0x200))                       # +0x8B6 = 1 after a portal

    def test_floor_near_follows_floors_slopes_and_ledges(self):
        L = M.Line
        lines = [L(0, 0, 0, 812, 1000, 812, 0, 0, None),          # the ground
                 L(1, 0, 200, 790, 400, 790, 0, 0, None),         # a platform just overhead
                 L(2, 1, 1000, 812, 1100, 762, 0, 0, None),       # a slope up to the right
                 L(3, 0, 1100, 762, 1300, 762, 0, 0, None),       # its top
                 L(4, 0, 1300, 900, 1500, 900, 0, 0, None),       # a lower floor past a ledge
                 L(5, 3, 1500, 0, 1500, 900, 0, 0, None)]         # a wall
        m = M.MapData(1, 0, [], lines, [])
        self.assertEqual(m.floor_near(300, 812, 60), 812)          # stays under the platform
        self.assertEqual(m.floor_near(1050, 812, 60), 787)         # follows the slope up
        self.assertEqual(m.floor_near(1200, 787, 60), 762)
        self.assertEqual(m.floor_near(1400, 762, 60), 900)         # walks off the ledge
        self.assertEqual(m.floor_near(500, 712, 8), 812)           # an arrival point falls
        self.assertIsNone(m.floor_near(2000, 812, 60))
        self.assertEqual(m.x_bounds(), (0.0, 1500.0))

    def test_dead_peer_timeout_sets_tcp_keepalive(self):
        calls = []

        class Sock:
            def setsockopt(self, *a):
                calls.append(('setsockopt',) + a)

            def ioctl(self, *a):
                calls.append(('ioctl',) + a)
        self.assertFalse(PR.set_dead_peer_timeout(Sock(), 0))
        self.assertEqual(calls, [])
        self.assertTrue(PR.set_dead_peer_timeout(Sock(), 2.0))
        self.assertIn(('setsockopt', socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1), calls)
        if hasattr(socket, 'SIO_KEEPALIVE_VALS'):
            self.assertIn(('ioctl', socket.SIO_KEEPALIVE_VALS, (1, 1000, 100)), calls)
        a, b = socket.socketpair()
        try:
            self.assertTrue(PR.set_dead_peer_timeout(a, 2.0))    # a real socket accepts it
        finally:
            a.close()
            b.close()
        self.assertEqual(cfgmod.defaults()['DEAD_PEER_SECS'], 2.0)
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.from_dict({'DEAD_PEER_SECS': -1.0})


if __name__ == '__main__':
    unittest.main(verbosity=1)
