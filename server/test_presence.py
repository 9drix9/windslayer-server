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
- the desync fixes of 2026-09-28 (POSITION_SYNC_RE): P1 the start hold only after a real idle
  gap (RELAY_START_HOLD_GAP_MS; the recorded b_jatk1 / c_dash1 / c_dash3 / g_kb3 sequences
  as fixtures), P2 the settle node (RELAY_SETTLE_NODE, an explicit clock, a relay / settle
  race and a replay model of the observer's copy), P3 the dash / knockback / dash-attack
  estimate (POSITION_ESTIMATE_DASH_KNOCK).

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
from unittest import mock

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


# Recorded C2S 0x0D series of the live desync measurement (scratchpad livetest/desync, EN
# 2009, TestHero), decoded: (logic_elapsed_ms, state_lo, state_hi) per packet, in order.
RECORDED = {
    # seq 235-240: jump (Up), release, S (attack) at the top of the jump 150 ms later, idle
    # while the air attack plays out, the final idle packet
    'b_jatk1 235-240': [(8100, 0x00C, 0), (60, 0x000, 0), (150, 0x004, 0), (60, 0x000, 0),
                        (210, 0x000, 0), (210, 0x000, 0)],
    # seq 94-102: tap left, then a dash 90 ms after the release, held until it ends
    'c_dash1 94-102': [(3720, 0x001, 0), (60, 0x000, 0), (90, 0x019, 0), (210, 0x019, 0),
                       (210, 0x019, 0), (210, 0x019, 0), (210, 0x019, 0), (90, 0x000, 0),
                       (210, 0x000, 0)],
    # seq 198-223: dashes, jumps and an attack in a row (the capture ends at 223)
    'c_dash3 198-223': [(2340, 0x001, 0), (30, 0x000, 0), (90, 0x019, 0), (210, 0x019, 0),
                        (90, 0x002, 0), (150, 0x000, 0), (210, 0x000, 0), (1290, 0x001, 0),
                        (60, 0x000, 0), (90, 0x019, 0), (210, 0x019, 0), (30, 0x00C, 0),
                        (60, 0x000, 0), (210, 0x000, 0), (210, 0x000, 0), (210, 0x000, 0),
                        (870, 0x002, 0), (30, 0x000, 0), (90, 0x01A, 0), (210, 0x00C, 0),
                        (60, 0x000, 0), (150, 0x004, 0), (30, 0x000, 0), (210, 0x000, 0),
                        (210, 0x000, 0), (210, 0x000, 0)],
    # seq 10-47: knockbacks by a Monkey Soldier on map 402 (ae 6 / 7 with facing2 right)
    'g_kb3 10-47': [(570, 0x206000, 250), (210, 0x200000, 250), (210, 0x200000, 250),
                    (780, 0x206000, 250), (210, 0x200000, 250), (210, 0x200000, 250),
                    (540, 0x207000, 810), (210, 0x200000, 810), (210, 0x200000, 810),
                    (210, 0x200000, 810), (210, 0x200000, 810), (360, 0x206000, 250),
                    (210, 0x200000, 250), (210, 0x200000, 250), (120, 0x206000, 250),
                    (210, 0x200000, 250), (210, 0x200000, 250), (510, 0x206000, 250),
                    (210, 0x200000, 250), (210, 0x200000, 250), (120, 0x206000, 250),
                    (210, 0x200000, 250), (210, 0x200000, 250), (510, 0x206000, 250),
                    (210, 0x200000, 250), (210, 0x200000, 250), (780, 0x206000, 250),
                    (210, 0x200000, 250), (210, 0x200000, 250), (780, 0x206000, 250),
                    (210, 0x200000, 250), (210, 0x200000, 250), (750, 0x206000, 250),
                    (210, 0x200000, 250), (210, 0x200000, 250), (120, 0x206000, 250),
                    (210, 0x200000, 250), (210, 0x200000, 250)],
}


def _holds(series, gap_ms=PR.START_HOLD_GAP_MS):
    """The 0x1B hold of each packet of a recorded series, relayed as relay() does (the last
    relayed words are the previous packet's; the first one follows a spawn)."""
    holds, prev = [], None
    for el, lo, hi in series:
        rec = {'logic_elapsed_ms': el, 'state_lo': lo, 'state_hi': hi, 'event_source_uid': 0xF0009}
        holds.append(PR.relay_fields(1, rec, prev, gap_ms)['hold_ms'])
        prev = lo
    return holds


class CopyModel:
    """An observer's type-3 copy reduced to what decides a float (2009 FUN_004129f0 and the
    tick gates 0x412CB1 / 0x414278 / 0x415FEF / 0x416B0D): node k's input runs, in 30 ms
    ticks, exactly while node k+1's hold counts down; an empty queue freezes the copy; a
    copy that is fully idle (landed, idle input) has its hold zeroed at once
    (0x412ABC..0x412AF6). A jump (motion 3) keeps it airborne for `air_ms` of simulated
    time."""

    def __init__(self, air_ms):
        self.air_ms = air_ms
        self.air_left = 0
        self.input = 0

    @property
    def airborne(self):
        return self.air_left > 0

    def feed(self, hold, lo):
        remaining = hold
        while remaining > 0:
            if not self.airborne and PR.is_idle(self.input):
                break                                   # the idle check zeroes the hold
            step = min(30, remaining)
            remaining -= step
            if self.airborne:
                self.air_left -= step
        self.input = lo
        if PR.motion(lo) == PR.MOTION_JUMP and not self.airborne:
            self.air_left = self.air_ms
        return self

    def replay(self, nodes):
        for hold, lo in nodes:
            self.feed(hold, lo)
        return self


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

    # --------------------------------------------- desync fix P1: start hold ---
    def packets(self, c=None):
        """C2S 0x0D the server has taken from `c` (default A) so far (presence.advance)."""
        return ((c or self.a).session.get('move') or {}).get('stats', {}).get('packets', 0)

    def holds(self, c=None):
        return [n['hold_ms'] for n in self.relays(c or self.b)]

    def test_a_press_within_the_busy_cadence_keeps_its_hold(self):
        """P1, live b_jatk1 seq 235-240: Up, release 60 ms later, S 150 ms after that. The
        idle words were sent while the jump was still in the air, so the S keeps its 150 ms -
        the legacy clamp gave it 30 and B's copy stopped 120 ms of simulation short, floating."""
        for el, lo in ((1380, 0x00C), (60, 0), (150, 0x004), (30, 0), (210, 0)):
            self.move(self.a, lo, elapsed=el)
        self.assertEqual(self.holds(), [30, 60, 150, 30, 210])
        self.a.expect_silence(0.1)

    def test_the_start_hold_still_applies_after_the_final_idle_packet(self):
        """P1: more than RELAY_START_HOLD_GAP_MS (450) after idle words, the idle packet was
        the builder's final one - the mover stood still - and the start hold is 30 ms; at
        450 ms exactly the gap is still a busy one and relayed as it is."""
        self.assertEqual(self.server.config.RELAY_START_HOLD_GAP_MS, 450)
        for el, lo in ((210, 0), (451, RIGHT), (210, 0), (450, RIGHT)):
            self.move(self.a, lo, elapsed=el)
        self.assertEqual(self.holds(), [210, 30, 210, 450])

    def test_knockbacks_in_a_row_keep_their_holds(self):
        """P1, live g_kb3 seq 24 / 30 / 45: a knockback 120 ms after the hurt's idle words
        (facing2 right) keeps its 120 ms (legacy: 30, the copy fell a knockback behind)."""
        self.move(self.a, 0x200000, elapsed=210, hi=250)
        self.move(self.a, 0x206000, elapsed=120, hi=250, event_source_uid=0xF0009)
        nodes = self.relays(self.b)
        self.assertEqual([n['hold_ms'] for n in nodes], [210, 120])
        self.assertEqual(nodes[1]['target_uid'], 0xF0009)
        self.assertEqual(struct.unpack('<II', P.to_bytes(nodes[1]['state_blob'])), (0x206000, 250))

    def test_gap_zero_is_the_legacy_clamp(self):
        """RELAY_START_HOLD_GAP_MS 0: the old rule, 30 ms after every idle node."""
        self.server.config['RELAY_START_HOLD_GAP_MS'] = 0
        for el, lo in ((1380, 0x00C), (60, 0), (150, 0x004), (30, 0), (210, 0)):
            self.move(self.a, lo, elapsed=el)
        self.assertEqual(self.holds(), [30, 60, 30, 30, 210])

    # --------------------------------------------- desync fix P2: settle node ---
    SETTLE_IDLE = bytes.fromhex('01 00 00 00 DE 03 00 00 00 00 00 00 00 00 00 00')

    def rx_t(self, c=None):
        return (c or self.a).session['move']['rx_t']

    def test_a_settle_node_follows_idle_words_after_450_ms_of_silence(self):
        """P2: the mover's last relayed words idle and no 0x0D for RELAY_SETTLE_AFTER_MS ->
        exactly one S2C 0x1B {uid, hold 990, the same words} to each holder; never to the
        mover, never before 450 ms, never twice."""
        self.move(self.a, RIGHT, elapsed=30)
        self.move(self.a, 0, elapsed=210)
        self.assertEqual(len(self.relays(self.b)), 2)
        rx = self.rx_t()
        self.assertEqual(self.server._tick_presence_settle(rx + 0.3), 0)          # too soon
        with self.assertLogs('WS', logging.INFO) as logs:
            self.assertEqual(self.server._tick_presence_settle(rx + 0.46), 1)
        pkt = self.b.expect(0x1B)
        self.assertEqual(pkt.payload, self.SETTLE_IDLE)
        got = self.b.s2c(pkt)
        self.assertEqual((got['uid'], got['hold_ms'], P.to_bytes(got['state_blob'])), (1, 990, bytes(8)))
        self.assertTrue(any('[MOVE] settle node: TestHero/uid 1 quiet 460 ms' in line
                            and '0x1B hold 990 to 1 peer(s)' in line for line in logs.output), logs.output)
        self.a.expect_silence(0.1)                                               # never the mover
        self.assertEqual(self.server._tick_presence_settle(rx + 2.0), 0)          # once per stop
        self.b.expect_silence(0.1)

    def test_no_settle_while_walking_for_a_non_holder_or_with_the_flag_off(self):
        self.move(self.a, RIGHT, elapsed=30)                                      # walking words last
        self.relays(self.b)
        self.assertEqual(self.server._tick_presence_settle(self.rx_t() + 5.0), 0)
        self.move(self.a, 0, elapsed=210)
        self.relays(self.b)
        rx = self.rx_t()
        self.server.config['RELAY_SETTLE_NODE'] = False                           # the flag off
        self.assertEqual(self.server._tick_presence_settle(rx + 1.0), 0)
        self.server.config['RELAY_SETTLE_NODE'] = True
        with PR._lock(self.b.session):                                            # B stops holding A
            held = self.b.session['spawned_players'].pop(1)
        try:
            self.assertEqual(self.server._tick_presence_settle(rx + 1.0), 0)
        finally:
            with PR._lock(self.b.session):
                self.b.session['spawned_players'][1] = held
        self.b.expect_silence(0.1)
        self.a.expect_silence(0.05)

    def test_a_relay_after_the_settle_re_arms_it(self):
        self.move(self.a, 0x00C, elapsed=30)
        self.move(self.a, 0, elapsed=60, hi=0x0FA)
        self.relays(self.b)
        self.assertEqual(self.server._tick_presence_settle(self.rx_t() + 0.5), 1)
        self.assertEqual(P.to_bytes(self.b.s2c(self.b.expect(0x1B))['state_blob']), struct.pack('<II', 0, 0x0FA))
        self.move(self.a, LEFT, elapsed=2000)                                     # a new walk ...
        self.move(self.a, 0, elapsed=210)                                         # ... and its stop
        self.assertEqual(self.holds(), [PR.START_HOLD_MS, 210])
        self.assertEqual(self.server._tick_presence_settle(self.rx_t() + 0.5), 1)
        self.assertEqual(self.b.expect(0x1B).payload, self.SETTLE_IDLE)

    def test_a_keepalive_is_not_relayed_and_does_not_re_arm_the_settle(self):
        self.move(self.a, 0, elapsed=210)
        self.relays(self.b)
        self.assertEqual(self.server._tick_presence_settle(self.rx_t() + 0.5), 1)
        self.b.expect(0x1B)
        self.move(self.a, 0, elapsed=15000)                                      # the idle keepalive
        self.assertTrue(_wait(lambda: self.packets() == 2))
        self.b.expect_silence(0.15)
        self.assertEqual(self.server._tick_presence_settle(self.rx_t() + 5.0), 0)

    def test_a_settle_never_follows_a_non_idle_node(self):
        """P2 race: relay() and settle_node() decide and push under the mover's move lock, so
        a settle checked against idle words can never be queued behind a newer non-idle node
        (it would give that node up to 990 ms of extra simulation)."""
        server, mover = self.server, self.a.session
        done = threading.Event()
        errors = []

        def relayer():
            try:
                for _ in range(300):
                    PR.relay(server, mover, {'map_code': 101, 'logic_elapsed_ms': 30, 'state_lo': 0})
                    time.sleep(0)                          # idle words last: a settle may start
                    PR.relay(server, mover, {'map_code': 101, 'logic_elapsed_ms': 30, 'state_lo': RIGHT})
            except Exception as e:                                             # noqa: BLE001
                errors.append(e)
            finally:
                done.set()

        def settler():
            try:
                while not done.is_set():
                    PR.settle_node(server, mover, time.monotonic() + 10.0)
            except Exception as e:                                             # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=relayer, daemon=True), threading.Thread(target=settler, daemon=True)]
        switch = sys.getswitchinterval()
        sys.setswitchinterval(1e-5)                        # interleave the two as often as possible
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        finally:
            sys.setswitchinterval(switch)
        self.assertEqual(errors, [])
        nodes = [(n['hold_ms'], struct.unpack('<II', P.to_bytes(n['state_blob']))[0])
                 for n in self.relays(self.b, quiet=0.4)]
        settles = [k for k, (hold, lo) in enumerate(nodes) if hold == PR.SETTLE_HOLD_MS]
        self.assertGreater(len(settles), 0)
        self.assertEqual(len(nodes) - len(settles), 600)                       # every relay arrived
        for k in settles:
            self.assertEqual(nodes[k][1], 0, 'a settle carries idle words only')
            self.assertGreater(k, 0)
            self.assertTrue(PR.is_idle(nodes[k - 1][1]), f'settle #{k} follows {nodes[k - 1]}')
        self.a.expect_silence(0.1)

    def test_relay_survives_a_reset_the_moment_it_lets_go_of_the_move_lock(self):
        """P2 review: what relay() logs after its move lock is the words it decided under the
        lock, never state['relayed'] read again - a map transfer on another thread (a GM's
        !warp -> on_enter_world -> reset_estimate) may set that to None right then (it raised
        TypeError). Driven exactly: the lock's release runs the reset."""
        server, mover = self.server, self.a.session
        self.assertEqual(PR.relay(server, mover, {'map_code': 101, 'logic_elapsed_ms': 210, 'state_lo': 0}), 1)

        class ResetOnRelease:
            """The move lock, with the GM thread's reset_estimate landing as relay() lets go."""

            def __init__(self):
                self.inner = threading.Lock()

            def __enter__(self):
                self.inner.acquire()
                return self

            def __exit__(self, *exc):
                self.inner.release()
                PR.reset_estimate(mover)
                return False
        mover['move']['lock'] = ResetOnRelease()
        cast = 0x0010003C                              # live 2009 Ice Spear pose, 150 ms after idle words
        with self.assertLogs('WS', logging.INFO) as logs:
            self.assertEqual(PR.relay(server, mover, {'map_code': 101, 'logic_elapsed_ms': 150,
                                                      'state_lo': cast}), 1)
        self.assertIsNone(mover['move']['relayed'])                              # the reset landed
        self.assertTrue(any(f'[MOVE] start hold kept: TestHero/uid 1 lo {cast:#x} after idle words 150 ms'
                            in line for line in logs.output), logs.output)
        self.assertTrue(any(f'[MOVE] cast pose of TestHero/uid 1 (variant 1, lo {cast:#x})' in line
                            for line in logs.output), logs.output)
        nodes = self.relays(self.b)
        self.assertEqual([(n['hold_ms'], struct.unpack('<II', P.to_bytes(n['state_blob']))) for n in nodes],
                         [(210, (0, 0)), (150, (cast, 0))])
        self.a.expect_silence(0.05)

    def test_one_failing_mover_does_not_end_the_settle_pass(self):
        """P2 review: _tick_presence_settle runs each mover on his own. A's settle raises (a
        PacketError building B's 0x1B): one log line, and B's settle to A still goes out in the
        same pass. A mover failing on every 0.15 s pass is logged at most once per
        PRESENCE_SETTLE_FAIL_LOG_SECS; the next line counts the failures in between."""
        for c in (self.a, self.b):
            self.move(c, RIGHT, elapsed=30)
            self.move(c, 0, elapsed=210)
        self.assertTrue(_wait(lambda: self.a.session['move'].get('relayed') == (0, 0)
                              and self.b.session['move'].get('relayed') == (0, 0)))
        self.relays(self.a)
        self.relays(self.b)
        t = max(self.rx_t(self.a), self.rx_t(self.b)) + 0.5
        real_push = self.server._push

        def push(target, key, fields, tag='WORLD', *args, **kw):
            if target is self.b.session and key == '0x1B':
                raise P.PacketError('0x1B: forced by the test')
            return real_push(target, key, fields, tag, *args, **kw)
        movers = [self.a.session, self.b.session]                                # the failing one first
        with mock.patch.object(self.server.world, 'in_world_sessions', return_value=movers):
            with mock.patch.object(self.server, '_push', side_effect=push), \
                    self.assertLogs('WS', logging.WARNING) as logs:
                self.assertEqual(self.server._tick_presence_settle(t), 1)
            self.assertEqual(logs.output, ["WARNING:WS:[MOVE] settle node for 'TestHero' uid 1 failed: "
                                           "PacketError: 0x1B: forced by the test; the pass goes on"])
            got = self.a.s2c(self.a.expect(0x1B))                                # B's settle reached A
            self.assertEqual((got['uid'], got['hold_ms'], P.to_bytes(got['state_blob'])), (2, 990, bytes(8)))
            real_settle = PR.settle_node

            def settle(server, mover, now=None):
                if mover is self.a.session:
                    raise P.PacketError('0x1B: forced by the test')
                return real_settle(server, mover, now)
            every = self.server.PRESENCE_SETTLE_FAIL_LOG_SECS
            self.assertEqual(every, 30.0)
            passes = [t + 0.15 * k for k in range(1, 400) if 0.15 * k < every]
            with mock.patch.object(PR, 'settle_node', side_effect=settle):
                with self.assertNoLogs('WS', logging.WARNING):
                    self.assertEqual(sum(self.server._tick_presence_settle(now) for now in passes), 0)
                with self.assertLogs('WS', logging.WARNING) as logs:
                    self.server._tick_presence_settle(t + every + 0.01)
            self.assertEqual(len(logs.output), 1)
            self.assertIn(f'the pass goes on ({len(passes)} more in the 30 s since the last line)',
                          logs.output[0])
        self.b.expect_silence(0.1)
        self.a.expect_silence(0.05)

    def test_the_settle_tick_creates_nothing_for_a_session_not_in_the_world_yet(self):
        """P2 review: the registry can list a session before its on_enter_world ran (no move
        state yet), and a session that never relayed has no move lock. The tick thread skips
        both untouched - no session['move'], no lock (move_state / move_lock are the session's
        own paths) - and the pass goes on to the movers that have something to settle."""
        self.move(self.a, RIGHT, elapsed=30)
        self.move(self.a, 0, elapsed=210)
        self.assertEqual(len(self.relays(self.b)), 2)
        rx = self.rx_t()
        entering = {'uid': 3, 'char_name': 'Newcomer', 'in_world': True, 'world_map': 101,
                    'current_map': 101, 'sock': self.b.session['sock']}
        before = dict(entering)
        self.assertNotIn('lock', self.b.session['move'])                        # B never relayed
        movers = [entering, self.b.session, self.a.session]
        with mock.patch.object(self.server.world, 'in_world_sessions', return_value=movers):
            self.assertEqual(self.server._tick_presence_settle(rx + 0.5), 1)
        self.assertEqual(entering, before)
        self.assertNotIn('lock', self.b.session['move'])
        self.assertEqual(self.b.expect(0x1B).payload, self.SETTLE_IDLE)          # A's settle
        self.a.expect_silence(0.1)

    # ---------------------------------------- desync fix P3: dash / knockback ---
    def test_the_estimate_follows_a_dash(self):
        """P3, live session 3 (3b_dash1, A's words): 60 ms of wind-up, then 20.25 px per tick
        for at most 18 ticks (540 ms) = 364.5 px, across packets - A's client moved 885 ->
        520.5 (the old 420 ms cap stopped 81 px short)."""
        for el, lo in ((90, 0x019), (210, 0x019), (210, 0x019), (210, 0x019), (60, 0)):
            self.move(self.a, lo, elapsed=el)
        self.assertTrue(_wait(lambda: self.packets() == 5))
        x, y = self.a.session['pos']
        self.assertAlmostEqual(x, 1411.0 - 364.5)
        self.assertEqual(M.load_map(101).floor_near(x, y, 0), y)             # y on the floor line
        self.relays(self.b)

    def test_the_estimate_follows_knockbacks_and_a_dash_attack(self):
        s = self.a.session
        steps = [(0x206000, 1411.0 + 30), (0x200000, 1411.0 + 30),      # action 6 right; idle + facing2
                 (0x106000, 1411.0), (0x207000, 1411.0 + 15),             # action 6 left; action 7 right
                 (0x200000, 1411.0 + 15),                                 # its hurt (210 ms) runs out
                 (0x019, 1411.0 + 15),                                    # a dash left starts
                 # the attack 60 ms into it: the 60 ms wind-up is just over (no dash px yet),
                 # so it is a counted dash attack, -37.5 px (dash_knock_dx: 60 <= t <= 600)
                 (0x005, 1411.0 + 15 - 37.5)]
        for k, (lo, x) in enumerate(steps):
            el = 210 if k == 4 else 60
            self.move(self.a, lo, elapsed=el, hi=250, **({'event_source_uid': 0xF0009} if PR.action(lo) else {}))
            self.assertTrue(_wait(lambda: self.packets() == k + 1))
            self.assertAlmostEqual(s['pos'][0], x, msg=f'{lo:#x}')
        self.relays(self.b)

    def test_the_estimate_stands_still_in_his_own_hurt(self):
        """Live session 3 (3c_trip4_fight_kb, B's words at 18:11:03): a Monkey Soldier's swing
        hit him as he walked left (action 7, hi 810: state 3 for 750 ms from that packet's
        tick). The left words he sent in it moved the old estimate while his client stood in
        state 3 (85..112 px off); now only the walk before it, the knock (+15, facing2 right)
        and the walk after it count."""
        words = [(0x200001, 30, 750), (0x207001, 90, 810),          # the hit: 90 ms walked, +15
                 (0x200000, 90, 810), (0x200001, 150, 810),
                 (0x200001, 210, 810), (0x200000, 30, 810),         # left in the hurt: nothing
                 (0x200001, 120, 810),
                 (0x200001, 210, 810),                              # out 750 ms after the hit:
                 (0x200000, 60, 810)]                               # 60 + 60 ms walked
        for lo, el, hi in words:
            self.move(self.a, lo, elapsed=el, hi=hi, **({'event_source_uid': 0xF0009} if PR.action(lo) else {}))
        self.assertTrue(_wait(lambda: self.packets() == len(words)))
        self.assertAlmostEqual(self.a.session['pos'][0], 1411.0 - 22.5 + 15 - 15 - 15)
        self.relays(self.b)

    def test_a_record_spawned_after_a_knockback_carries_the_moved_x(self):
        self.move(self.a, 0x206000, elapsed=210, hi=250, event_source_uid=0xF0009)
        self.relays(self.b)
        self.assertTrue(_wait(lambda: self.a.session['pos'][0] == 1411.0 + 30))
        self.b.send(0x7E, PORTAL_101_TO_102)
        self.assertTrue(_wait(lambda: self.server.world.map_of(self.b.session) == 102))
        self.b.recv_until_quiet()
        self.b.send(0x7E, PORTAL_102_TO_101)
        self.assertTrue(_wait(lambda: self.server.world.map_of(self.b.session) == 101))
        (row,) = [r for p in self.b.recv_until_quiet() if p.opcode == 0x04
                  for r in self.b.s2c(p)['repeat[player_count]']]
        self.assertEqual((row['uid'], row['pos_x'], row['pos_y']), (1, 1411.0 + 30, FLOOR_101))

    def test_the_dash_knock_estimate_can_be_switched_off(self):
        self.server.config['POSITION_ESTIMATE_DASH_KNOCK'] = False
        for el, lo in ((30, 0x019), (90, 0x019), (210, 0x019), (210, 0x206000)):
            self.move(self.a, lo, elapsed=el, hi=250, **({'event_source_uid': 0xF0009} if PR.action(lo) else {}))
        self.assertTrue(_wait(lambda: self.packets() == 4))
        self.assertEqual(self.a.session['pos'][0], 1411.0)
        self.relays(self.b)

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
        # the arrival (1411, 714) was settled onto the floor at the map load (livetest bug 5)
        self.assertTrue(lines[4].startswith('Watcher uid 2 map 101 (1411,814) floor 814 fix arrival'), lines[4])
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
    def test_recorded_sequences_stay_in_lockstep(self):
        """P1 on the live captures: from node 2 on, every hold is the mover's own elapsed
        time - the observer's copy simulates exactly the mover's timeline - except the 30 ms
        start hold after a gap > 450 ms behind the FINAL idle packet (the mover stood idle;
        a copy in lockstep is idle too, so nothing is lost)."""
        kept_by_p1 = {'b_jatk1 235-240': [2], 'c_dash1 94-102': [2], 'c_dash3 198-223': [2, 9, 18, 21],
                      'g_kb3 10-47': [11, 14, 20, 35]}
        for name, series in RECORDED.items():
            with self.subTest(name):
                holds, legacy = _holds(series), _holds(series, 0)
                for k in range(1, len(series)):
                    el, lo, _ = series[k]
                    gap = PR.is_idle(series[k - 1][1]) and not PR.is_idle(lo) and el > PR.START_HOLD_GAP_MS
                    self.assertEqual(holds[k], PR.START_HOLD_MS if gap else min(el, PR.HOLD_MAX_MS), (name, k))
                # the nodes P1 gives back their time (the legacy clamp cut them to 30)
                self.assertEqual([k for k in range(len(series)) if holds[k] != legacy[k]], kept_by_p1[name])
                self.assertTrue(all(legacy[k] == PR.START_HOLD_MS for k in kept_by_p1[name]))
        # b_jatk1 seq 237 goes out with hold 150 instead of 30 (the plan's worked example)
        self.assertEqual(_holds(RECORDED['b_jatk1 235-240']), [30, 60, 150, 60, 210, 210])

    def test_the_replay_model_floats_without_the_settle_and_lands_with_it(self):
        """P2 on b_jatk1: with the legacy clamp (gap 0) the copy's stream ends 120 ms of
        simulation short (570 of the mover's 690 ms after the jump). The mover was still in
        the air at the busy idle packet 480 ms after the jump and down by the final one at
        690, so for any air time in (570, 690] the copy freezes in the air - and the settle
        node lands it. With P1 the stream is whole and it lands without one."""
        series = RECORDED['b_jatk1 235-240']
        legacy = list(zip(_holds(series, 0), [lo for _, lo, _ in series]))
        fixed = list(zip(_holds(series), [lo for _, lo, _ in series]))
        settle = PR.settle_fields(1, (series[-1][1], series[-1][2]))
        node = (settle['hold_ms'], struct.unpack('<II', settle['state_blob'])[0])
        self.assertEqual(node, (990, 0))
        for air_ms in (600, 630, 660, 690):
            with self.subTest(air_ms=air_ms):
                self.assertTrue(CopyModel(air_ms).replay(legacy).airborne)          # the float
                self.assertFalse(CopyModel(air_ms).replay(legacy + [node]).airborne)
                self.assertFalse(CopyModel(air_ms).replay(fixed).airborne)
                self.assertFalse(CopyModel(air_ms).replay(fixed + [node]).airborne)

    def test_relay_fields_start_hold_rule(self):
        idle, walk = 0, RIGHT
        rec = lambda el, lo: {'logic_elapsed_ms': el, 'state_lo': lo}                  # noqa: E731
        self.assertEqual(PR.relay_fields(1, rec(4000, walk))['hold_ms'], 30)          # after the spawn
        self.assertEqual(PR.relay_fields(1, rec(300, walk))['hold_ms'], 300)          # a busy gap
        self.assertEqual(PR.relay_fields(1, rec(300, walk), idle)['hold_ms'], 300)
        self.assertEqual(PR.relay_fields(1, rec(451, walk), idle)['hold_ms'], 30)
        self.assertEqual(PR.relay_fields(1, rec(451, walk), walk)['hold_ms'], 451)    # not after idle
        self.assertEqual(PR.relay_fields(1, rec(3000, idle), idle)['hold_ms'], 990)   # idle words: capped only
        self.assertEqual(PR.relay_fields(1, rec(300, walk), idle, 0)['hold_ms'], 30)  # legacy
        self.assertEqual(PR.relay_fields(1, rec(300, walk), idle, 240)['hold_ms'], 30)

    def test_the_dash_knock_estimate_rules(self):
        state = {}
        dx = [PR.dash_knock_dx(state, prev, lo, el) for prev, lo, el in
              ((0, 0x1A, 30), (0x1A, 0x1A, 90), (0x1A, 0x1A, 210), (0x1A, 0x1A, 210), (0x1A, 0, 210))]
        self.assertAlmostEqual(sum(dx), 364.5)                    # 0x1A for 720 ms: 18 ticks
        self.assertEqual(dx[:2], [0.0, 30 * 0.675])
        self.assertEqual((PR.DASH_WINDUP_MS + PR.DASH_MOVE_MS, PR.DASH_MOVE_MS // 30), (600, 18))
        self.assertEqual(state['dash_ms'], 720)
        more = PR.dash_knock_dx(state, 0x1A, 0x1A, 210)             # past 600 ms: no more
        self.assertEqual(more, 0.0)
        self.assertEqual(PR.dash_knock_dx({}, 0, 0x206000, 60), 30.0)
        self.assertEqual(PR.dash_knock_dx({}, 0, 0x106000, 60), -30.0)
        self.assertEqual(PR.dash_knock_dx({}, 0, 0x207000, 60), 15.0)
        self.assertEqual(PR.dash_knock_dx({}, 0, 0x209000, 60), 30.0)            # action 9
        self.assertEqual(PR.dash_knock_dx({}, 0, 0x208000, 60), 15.0)            # action 8
        self.assertEqual(PR.dash_knock_dx({}, 0, 0x200000, 60), 0.0)             # idle + facing2
        self.assertEqual(PR.dash_knock_dx({}, 0, 0x201000, 60), 0.0)             # action 1: no slide
        st = {}
        PR.dash_knock_dx(st, 0, 0x1A, 30)
        self.assertEqual(PR.dash_knock_dx(st, 0x1A, 0x006, 60), 37.5)            # dash attack right
        st = {}
        PR.dash_knock_dx(st, 0, 0x19, 30)
        self.assertEqual(PR.dash_knock_dx(st, 0x19, 0x015, 60), -37.5)           # strong attack left
        st = {}                                              # live s_mix1: dash left, S 330 ms in
        dx = [PR.dash_knock_dx(st, prev, lo, el) for prev, lo, el in
              ((0, 0x19, 90), (0x19, 0x19, 210), (0x19, 0x004, 120))]
        self.assertAlmostEqual(sum(dx), -(270 * 0.675 + 37.5))
        st = {}
        PR.dash_knock_dx(st, 0, 0x1A, 30)
        self.assertEqual(PR.dash_knock_dx(st, 0x1A, 0x006, 30), 0.0)             # in the wind-up: refused
        st = {'dash_ms': 590}
        self.assertAlmostEqual(PR.dash_knock_dx(st, 0x1A, 0x006, 30), 10 * 0.675)  # past the dash: no slide
        st = {'dash_ms': 470}
        self.assertAlmostEqual(PR.dash_knock_dx(st, 0x1A, 0x006, 30), 30 * 0.675 + 37.5)  # still in it

    def advance_all(self, words, dash_knock=True, x=1411.0):
        """A session at (x, FLOOR_101) on map 101 fed (lo, logic_elapsed_ms, hi) words through
        presence.advance; returns its x after each."""
        s = {'current_map': 101, 'pos': (x, FLOOR_101)}
        PR.reset_estimate(s, fix=PR.FIX_INTERACT)
        xs = []
        for lo, el, hi in words:
            PR.advance(s, {'state_lo': lo, 'state_hi': hi, 'logic_elapsed_ms': el}, dash_knock=dash_knock)
            xs.append(s['pos'][0])
        return s, xs

    def test_his_own_hurt_stops_the_walk_for_hurt_len(self):
        """Live session 3, 3c_trip4_fight_kb (B, 18:11:03.204..): the swing (action 7, hi 810)
        puts him in state 3 for hitstun.hurt_len = 750 ms from its packet's tick: of the left
        words after it only the 60 ms past 750 walk. fe746fa walked all of them (-112.5 px
        more) - and with POSITION_ESTIMATE_DASH_KNOCK off it still does."""
        words = [(0x200001, 210, 750), (0x207001, 90, 810), (0x200000, 90, 810), (0x200001, 150, 810),
                 (0x200001, 210, 810), (0x200000, 30, 810), (0x200001, 120, 810), (0x200001, 210, 810),
                 (0x200000, 60, 810)]
        s, xs = self.advance_all(words)
        self.assertEqual(xs, [1411.0, 1403.5, 1403.5, 1403.5, 1403.5, 1403.5, 1403.5, 1388.5, 1373.5])
        self.assertEqual((s['move']['stun_until'], s['move']['clock']), (210 + 90 + 750, 1170))
        self.assertEqual(PR.STUN_ACTIONS, (6, 7, 9))
        _, legacy = self.advance_all(words, dash_knock=False)
        self.assertEqual(legacy[-1], 1411.0 - 22.5 - 52.5 - 7.5 - 52.5 - 15)
        # contact (6, hi 250: 270 ms) and a monster's skill (9) stop it too; a guard (1..5)
        # and an airborne hurt (8 / 10: state 0x17, which moves in the air) do not
        for ae, hi, walked in ((6, 250, 0), (9, 300, 0), (2, 250, 210), (8, 810, 210)):
            with self.subTest(ae=ae):
                _, xs = self.advance_all([(0x1, 30, 0), ((ae << 12) | 0x1, 30, hi), (0x1, 30, hi),
                                          (0x1, 210, hi)])
                knock = PR.dash_knock_dx({}, 0, (ae << 12) | 0x1, 30)
                self.assertAlmostEqual(xs[-1], 1411.0 - 7.5 + knock - 0.25 * walked)
        # a new map's estimate starts with no hurt
        s, _ = self.advance_all(words[:2])
        PR.reset_estimate(s)
        self.assertIsNone(s['move']['stun_until'])

    def test_a_dash_word_in_the_hurt_dashes_from_its_end(self):
        """The motion case starts a dash only in state 8 / 0xC (0x414887): dash words that
        began in a contact's hurt (270 ms) dash from its end, wind-up included - and a dash
        the hurt cut short starts over there if its words are still held."""
        hurt = (0x106001, 60, 250)                                   # contact, knocked left
        words = [(0x1, 30, 0), hurt, (0x200000, 90, 250),            # stun until hurt + 270
                 (0x200019, 120, 250),                               # dash left 210 ms in
                 (0x200019, 210, 250), (0x200019, 210, 250), (0x200019, 210, 250),
                 (0x200000, 210, 250)]
        _, xs = self.advance_all(words)
        start = 1411.0 - 0.25 * 60 - 30
        # from hurt + 270: 60 ms of wind-up, then 18 ticks; the words end at hurt + 1050
        self.assertAlmostEqual(xs[2], start)
        self.assertAlmostEqual(xs[4], start - 0.675 * (420 - 270 - 60))
        self.assertAlmostEqual(xs[-1], start - 364.5)
        mid = [(0x1, 30, 0), (0x200019, 30, 0), (0x200019, 210, 0),   # a dash left, 210 ms in
               (0x106019, 30, 250),                                  # contact: it ends there
               (0x200019, 210, 250), (0x200019, 210, 250), (0x200019, 210, 250), (0x200019, 210, 250),
               (0x200000, 30, 250)]
        _, xs = self.advance_all(mid)
        self.assertAlmostEqual(xs[3], xs[2] - 0.675 * 30 - 30)          # the dash, then the knock
        self.assertAlmostEqual(xs[-1], xs[3] - 364.5)                   # a whole new dash at its end

    def test_the_desync_config_keys(self):
        d = cfgmod.defaults()
        self.assertEqual((d.RELAY_START_HOLD_GAP_MS, d.RELAY_SETTLE_NODE, d.RELAY_SETTLE_AFTER_MS,
                          d.RELAY_SETTLE_HOLD_MS, d.POSITION_ESTIMATE_DASH_KNOCK),
                         (450, True, 450, 990, True))
        for good in ({'RELAY_START_HOLD_GAP_MS': 0}, {'RELAY_START_HOLD_GAP_MS': 240},
                     {'RELAY_START_HOLD_GAP_MS': 990}, {'RELAY_SETTLE_NODE': False},
                     {'RELAY_SETTLE_AFTER_MS': 300}, {'RELAY_SETTLE_HOLD_MS': 30},
                     {'POSITION_ESTIMATE_DASH_KNOCK': False}):
            with self.subTest(good):
                cfgmod.from_dict(good)
        for bad in ({'RELAY_START_HOLD_GAP_MS': 239}, {'RELAY_START_HOLD_GAP_MS': 991},
                    {'RELAY_START_HOLD_GAP_MS': -1}, {'RELAY_START_HOLD_GAP_MS': 450.0},
                    {'RELAY_SETTLE_NODE': 1}, {'RELAY_SETTLE_AFTER_MS': 299},
                    {'RELAY_SETTLE_HOLD_MS': 29}, {'RELAY_SETTLE_HOLD_MS': 991},
                    {'POSITION_ESTIMATE_DASH_KNOCK': 'yes'}):
            with self.subTest(bad), self.assertRaises(cfgmod.ConfigError):
                cfgmod.from_dict(bad)
        with open(os.path.join(HERE, 'config.json'), encoding='utf-8') as f:
            shipped = json.load(f)
        for key in ('RELAY_START_HOLD_GAP_MS', 'RELAY_SETTLE_NODE', 'RELAY_SETTLE_AFTER_MS', 'RELAY_SETTLE_HOLD_MS',
                    'POSITION_ESTIMATE_DASH_KNOCK', 'MOB_SPEED_FROM_TEMPLATE', 'MOB_HIT_RELAY_KNOCKBACK',
                    'MOB_HIT_RELAY_HURT_MS', 'MOB_HIT_HURT_PER_SWING', 'MOB_HIT_RELAY_SKILL_VARIANT',
                    'MOB_HIT_RECOVER_SECS', 'MOB_HIT_RECOVER_MARGIN_SECS', 'MOB_HIT_CAST_GATE',
                    'MOB_HIT_ICE_CHASE_SECS'):
            self.assertEqual(shipped[key], cfgmod.DEFAULTS[key], key)
        self.assertIn('RELAY_START_HOLD_GAP_MS', shipped['_desync_comment'])
        cfgmod.from_dict(shipped)

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
