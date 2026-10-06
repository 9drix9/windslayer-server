#!/usr/bin/env python3
"""
fakeclient.py - offline fake WindSlayer client for handler tests (roadmap 1.10 F9)
=================================================================================
Drives the real GameServer._handle_fireway loop over socket.socketpair(): no port is
bound, no game client is needed, and the live accounts.json is never opened.

    server = make_server(tmpdir)                      # GameServer on a temp accounts.json
    with FakeClient(server) as c:                     # reads S2C 0x5A, keys the cipher
        c.send_c2s('0x44D8BF/0x01', {'account_id': 'test', 'password': 'test'})
        pkt = c.expect(0x02)                          # decoded Packet
        rec = c.decode(pkt)                           # packets.parse, exact length

Framing matches the real EN client: every C2S goes out with the NoEncode bit and
CEncMsg.encode_by_array (all live captures so far), so the server answers the same way.
S2C packets are checksum-verified and decoded with packets.parse (allow_trailing=False),
which fails on any trailing or missing byte.

EN 2009 build (client-2009-login): make_server(tmpdir, client_build='2009') runs the server
on protocol_spec_2009.json; a FakeClient takes the build from its server, so send_c2s()
builds 2009 send-site keys and c.s2c(pkt) decodes with the 2009 grammars:

    server = make_server(tmpdir, client_build='2009')
    with FakeClient(server) as c:
        c.send_c2s('0x451CE5/0x01', sso_login('test', 'test'))    # SSO field + session key
        rec = c.s2c(c.expect(0x02))                                  # 2009 0x02 grammar

FakeClient.decode (static) stays the 2008 decoder unless client_build is passed.

Several clients on one server (party-test-harness, P5): MultiClient logs in and enters the
world with each (account, password, character) in either build; client k sends P2P port
42907 + k - 1 like the real exes, so admin targets "c:1"/"c:2" and uids 1/2 both work:

    server = make_server(tmpdir, accounts=two_player_accounts(), client_build='2009')
    with MultiClient(server) as (a, b):             # TestHero c:1 uid 1, Watcher c:2 uid 2
        server.world.peers(a.session) == [b.session]
"""
import json
import os
import socket
import struct
import sys
import threading
import time
from collections import namedtuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from cencmsg import CEncMsg  # noqa: E402
import packets as P  # noqa: E402

HEADER_SIZE = 8
SIZE_MASK = 0x7FF
NO_ENCODE_FLAG = 0x800

Packet = namedtuple('Packet', 'opcode payload no_enc seq')

# Default fixture: the accounts the live server ships with, minimal fields only.
DEFAULT_ACCOUNTS = {
    'test': {
        'password': 'test',
        'characters': [
            {'name': 'TestHero', 'level': 1, 'class': 0, 'map': 0,
             'x': 100, 'y': 100, 'hp': 100, 'mp': 50},
        ],
    },
    'admin': {'password': 'admin', 'characters': []},
}


# party-test-harness (+trade-harness, social_friend-harness-multiclient): the C2S send-site
# keys a fake client logs in and enters the world with, per client build.
LOGIN_KEYS = {'2008': '0x44D8BF/0x01', '2009': '0x451CE5/0x01'}
ENTER_KEYS = {'2008': '0x42F904/0x2B', '2009': '0x4315D7/0x2B'}
# The client's own UDP P2P port in C2S 0x2B: client 1 = WindSlayer_patched.exe (42907),
# client 2 = WindSlayer_p2.exe (42908). world.client_number reads it back, so the fake
# clients of multi_client() are targetable as c:1 / c:2 exactly like the real exes.
P2P_PORT_BASE = 42907


def two_player_accounts():
    """DEFAULT_ACCOUNTS plus a character on the admin account, both on map 101: the P5
    two-client setup (test/test TestHero on WindSlayer_patched.exe, admin/admin on
    WindSlayer_p2.exe) for multi_client()."""
    accounts = json.loads(json.dumps(DEFAULT_ACCOUNTS))
    accounts['admin']['characters'] = [
        {'name': 'Watcher', 'level': 1, 'class': 0, 'map': 101,
         'x': 1411.0, 'y': 714.0, 'hp': 100, 'mp': 50},
    ]
    accounts['test']['characters'][0].update({'map': 101, 'x': 1411.0, 'y': 714.0})
    return accounts


def login_fields(client_build, account, password, session_key=0):
    """(C2S key, fields) of the login request of `client_build` (2009: the SSO field)."""
    if str(client_build or '2008') == '2009':
        return LOGIN_KEYS['2009'], sso_login(account, password, session_key)
    return LOGIN_KEYS['2008'], {'account_id': account, 'password': password}


def mob_packets(monsters):
    """The S2C 0x1A opcodes a map load of `monsters` monsters sends: batched, as many records
    per packet as fit the 2038-byte payload (world-1a-defaults; 23 with server_controlled).
    Map 102's eight Pupu arrive in ONE 0x1A."""
    W = import_server()
    rec = {'effect_count': 0, 'server_controlled': 1}
    return [0x1A] * len(W.GameServer.monster_spawn_batches([rec] * int(monsters)))


def sso_login(account, password, session_key=0):
    """C2S 0x01 fields of the 2009 client (spec_2009 0x451CE5/0x01): the launcher's first
    SSO argument as the patched client sends it - "<account> <password>" with the trailing
    space the live capture kept (play_2009.bat: -<account> <password> -x -x) - plus the u32
    session key (0 on a first login, the last S2C 0x02 key on a relogin)."""
    return {'sso_account': f'{account} {password} ', 'session_key': int(session_key) & 0xFFFFFFFF}


def import_server():
    """Import windslayer_server. Logging is configured only by its main(), so importing
    it here neither truncates server_live.log nor appends to server_history.log."""
    import windslayer_server
    return windslayer_server


def make_server(tmpdir, accounts=None, config=None, portal_cooldown=0.0, client_build=None,
                ground_loot=False, grade_roll=False, drop_mode='legacy'):
    """A GameServer whose accounts file is a fresh copy in tmpdir (never the live one).
    Nothing is bound and no background thread (combat driver, admin port, ticks) starts.
    The fixture is the pre-P1 schema, so every server also runs the store migration (its
    one-time backup lands in tmpdir). config defaults to config.defaults(): tests never
    depend on the live server/config.json.

    portal_cooldown: the server's 2 s portal debounce (world-portal-guards) is off by
    default, because a test portals back and forth in milliseconds where a player needs
    seconds. Pass the real GameServer.PORTAL_COOLDOWN_SECS to exercise the guard itself
    (test_maptransfer.PortalGuards does).

    client_build: '2009' runs the server as the EN 2009 build (config CLIENT_BUILD) on top
    of `config` (or the defaults); None keeps the config's own build ('2008' by default).

    ground_loot: the rig pins GROUND_LOOT false - the P0 loot-into-the-bag path - because the
    byte-exact kill sequences of the older suites (0x29 0x21 0x18, the drop in the 0x18) were
    written for it and a random drop would otherwise add a 0x12 to some runs. True runs the
    shipped default (monster loot on the ground, test_ground.py); None keeps the config's
    own value.

    grade_roll: the rig pins DAMAGE_GRADE_ROLL false - the suites assert the formula's exact
    numbers (a Monkey Soldier hit for 8 then 24, a Pupu killed in one hit), which the server's
    grade roll (livetest bug 10: x0.75..x1.5, x0.9..1.1 jitter) would scatter. True forces it
    on (test_damage seeds GameServer.DAMAGE_RNG); None keeps the config's value - the shipped
    default null is auto: on for a 2009 server only (config.grade_roll_on).

    drop_mode: the rig pins DROP_MODE 'legacy' - the P0 roll (60 %, one entry picked with
    random.choice) the older suites force with random.random 0.0 + random.choice; under the
    shipped per-entry rolls (P13 boss-b2, 'rates') that 0.0 would drop the whole table.
    test_bosses.py runs 'rates' / 'single'; None keeps the config's own value.

    With no `config` the server also runs without the monster AI (MOB_AGGRO false): the
    rig's byte-exact flows were written for the server that answers a hit with nothing but
    the release, and test_mobai.py drives the aggro on both builds with explicit configs."""
    W = import_server()
    import config as cfgmod
    if client_build is not None:
        overrides = {k: v for k, v in dict(config or cfgmod.defaults()).items()}
        if config is None:
            overrides['MOB_AGGRO'] = False
        overrides['CLIENT_BUILD'] = str(client_build)
        config = cfgmod.from_dict(overrides, getattr(config, 'path', None))
    db = os.path.join(tmpdir, 'accounts.json')
    with open(db, 'w', encoding='utf-8') as f:
        json.dump(DEFAULT_ACCOUNTS if accounts is None else accounts, f, indent=2)
    # The rig pins the server-controlled 0x1A encoding its byte-exact tests were written for;
    # the shipped default is the client wander AI (MOB_SERVER_CONTROLLED false), which
    # test_world_content.SpawnRecordBytes covers explicitly.
    if config is None:
        config = cfgmod.from_dict({'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False})
    if ground_loot is not None and bool(config.get('GROUND_LOOT')) != bool(ground_loot):
        config = cfgmod.from_dict({**dict(config), 'GROUND_LOOT': bool(ground_loot)}, getattr(config, 'path', None))
    # Compared by identity: the default null (auto) is neither pin, so it is always replaced.
    if grade_roll is not None and config.get('DAMAGE_GRADE_ROLL') is not bool(grade_roll):
        config = cfgmod.from_dict({**dict(config), 'DAMAGE_GRADE_ROLL': bool(grade_roll)},
                                  getattr(config, 'path', None))
    if drop_mode is not None and config.get('DROP_MODE') != drop_mode:
        config = cfgmod.from_dict({**dict(config), 'DROP_MODE': str(drop_mode)}, getattr(config, 'path', None))
    server = W.GameServer(host='127.0.0.1', port=0, db_file=db, config=config)
    server.PORTAL_COOLDOWN_SECS = float(portal_cooldown)
    return server


class FakeClient:
    _next_port = [50000]

    def __init__(self, server, addr=None, timeout=5.0):
        self.server = server
        # The server's client build: send_c2s() and s2c() use its spec (client-2009-login).
        self.client_build = getattr(server, 'client_build', None)
        if addr is None:
            self._next_port[0] += 1
            addr = ('127.0.0.1', self._next_port[0])
        self.addr = addr
        self.sock, self._server_sock = socket.socketpair()
        self.sock.settimeout(timeout)
        self._buf = bytearray()
        self.seq = 1
        self.thread = threading.Thread(target=server._handle_fireway, args=(self._server_sock, addr),
                                       name=f'fakeclient-{addr[1]}', daemon=True)
        self.thread.start()
        hello = self.recv(timeout)
        if hello is None or hello.opcode != 0x5A or not hello.no_enc or len(hello.payload) != 4:
            raise AssertionError(f'expected S2C 0x5A key exchange (NoEncode, 4 B), got {hello}')
        self.seed = struct.unpack('<i', hello.payload)[0]
        # The client keys its MT cipher with the seed (SetCodeKey); only needed if the
        # server ever answers without the NoEncode bit.
        self.rx = CEncMsg()
        self.rx.set_code_key(self.seed)
        # _handle_fireway registers the session right after sending 0x5A; wait for it so
        # callers (and the admin injector) see this connection.
        if self.wait_session(lambda s: True, timeout) is None:
            raise AssertionError(f'server never registered session {addr}')

    # -------------------------------------------------------------- session ---
    @property
    def session(self):
        return self.server.sessions.get(self.addr)

    def wait_session(self, pred, timeout=2.0):
        """Poll until pred(session) is true (server state is written on its thread)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            s = self.session
            if s is not None and pred(s):
                return s
            time.sleep(0.01)
        return None

    # ----------------------------------------------------------------- send ---
    def send(self, opcode, payload=b''):
        """One C2S packet exactly as the EN client frames it (NoEncode + EncodebyArray)."""
        body = bytes([opcode & 0xFF]) + bytes(payload)
        total = HEADER_SIZE + len(body)
        dw0 = (total & SIZE_MASK) | NO_ENCODE_FLAG | ((self.seq & 0xFF) << 12)
        pkt = bytearray(struct.pack('<II', dw0, 0) + body)
        CEncMsg().encode_by_array(pkt)
        self.seq += 1
        self.sock.sendall(bytes(pkt))

    def send_c2s(self, key, fields=None):
        """Build a C2S payload from its send-site grammar (the server's build) and send it.
        Returns the payload."""
        payload = P.build(key, fields or {}, direction='C2S', client_build=self.client_build)
        self.send(P.opcode(key, 'C2S', client_build=self.client_build), payload)
        return payload

    # -------------------------------------------------------------- receive ---
    def _fill(self, n, deadline):
        while len(self._buf) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return False
            if not chunk:
                raise ConnectionError('server closed the connection')
            self._buf.extend(chunk)
        return True

    def recv(self, timeout=2.0):
        """Next S2C packet (decoded, checksum verified), or None on timeout."""
        deadline = time.monotonic() + timeout
        if not self._fill(HEADER_SIZE, deadline):
            return None
        dw0 = struct.unpack_from('<I', self._buf, 0)[0]
        size = dw0 & SIZE_MASK
        if size < HEADER_SIZE:
            raise AssertionError(f'bad S2C header 0x{dw0:08X}')
        if not self._fill(size, deadline):
            raise AssertionError(f'S2C packet truncated: header says {size} B, have {len(self._buf)}')
        raw = bytearray(self._buf[:size])
        del self._buf[:size]
        no_enc = bool(dw0 & NO_ENCODE_FLAG)
        ok = CEncMsg().decode_by_array(raw) if no_enc else self.rx.decode(raw)
        if not ok:
            raise AssertionError(f'S2C checksum failed (no_enc={no_enc}): {bytes(raw).hex(" ")}')
        return Packet(raw[HEADER_SIZE], bytes(raw[HEADER_SIZE + 1:size]), no_enc, (dw0 >> 12) & 0xFF)

    def recv_until_quiet(self, quiet=0.25, timeout=5.0):
        """Every packet until the server has been silent for `quiet` seconds."""
        out, end = [], time.monotonic() + timeout
        while time.monotonic() < end:
            pkt = self.recv(min(quiet, max(0.0, end - time.monotonic())))
            if pkt is None:
                break
            out.append(pkt)
        return out

    def expect(self, *opcodes, timeout=3.0, quiet=0.25):
        """Receive exactly `opcodes` in order and then silence. Returns the packets
        (a single Packet when one opcode is given)."""
        got = []
        for op in opcodes:
            pkt = self.recv(timeout)
            if pkt is None:
                raise AssertionError(f'expected S2C {_ops(opcodes)}, got {_ops(p.opcode for p in got)} then nothing')
            got.append(pkt)
            if pkt.opcode != op:
                extra = self.recv_until_quiet(quiet)
                raise AssertionError(f'expected S2C {_ops(opcodes)}, got '
                                     f'{_ops([p.opcode for p in got] + [p.opcode for p in extra])}')
        extra = self.recv_until_quiet(quiet)
        if extra:
            raise AssertionError(f'expected S2C {_ops(opcodes)}, then got extra {_ops(p.opcode for p in extra)}')
        return got[0] if len(got) == 1 else got

    def expect_silence(self, secs=0.3):
        extra = self.recv_until_quiet(secs, timeout=secs + 0.5)
        if extra:
            raise AssertionError(f'expected no S2C, got {_ops(p.opcode for p in extra)}')

    # ------------------------------------------------------- login / enter ---
    def login(self, account, password, session_key=0, timeout=3.0):
        """C2S 0x01 in this client's build (2009: with `session_key`, the relogin key of the
        last 0x02); returns the decoded S2C 0x02 (any result)."""
        key, fields = login_fields(self.client_build, account, password, session_key)
        self.send_c2s(key, fields)
        pkt = self.recv(timeout)
        if pkt is None or pkt.opcode != 0x02:
            raise AssertionError(f'expected S2C 0x02 after the login of {account!r}, got {pkt}')
        return self.s2c(pkt)

    def enter_world(self, char_name, *, port=P2P_PORT_BASE, refuse=None, timeout=3.0, quiet=0.25):
        """C2S 0x2B in this client's build (refuse: {kind: bool} for bytes 0-4, port: the
        P2P port the exe would send) and every S2C of the map load. Waits until the server
        has the session in world; returns the packets."""
        fields = {'p2p_ip': '127.0.0.1', 'p2p_udp_port': int(port), 'char_name': char_name}
        for kind, on in (refuse or {}).items():
            fields[f'refuse_{kind}'] = int(bool(on))
        self.send_c2s(ENTER_KEYS['2009' if self.client_build == '2009' else '2008'], fields)
        if self.wait_session(lambda s: s.get('in_world'), timeout) is None:
            raise AssertionError(f'{char_name!r} never got in world')
        return self.recv_until_quiet(quiet, timeout)

    # --------------------------------------------------------------- decode ---
    @staticmethod
    def decode(pkt, assume=None, allow_trailing=False, client_build=None):
        """packets.parse of an S2C packet: every byte consumed unless allow_trailing.
        Static, so the 2008 grammars unless client_build is given (see s2c())."""
        return P.parse(pkt.opcode, pkt.payload, assume, direction='S2C', allow_trailing=allow_trailing,
                       client_build=client_build)

    def s2c(self, pkt, assume=None, allow_trailing=False):
        """decode() with this client's (= its server's) build."""
        return self.decode(pkt, assume, allow_trailing, client_build=self.client_build)

    # ---------------------------------------------------------------- close ---
    def close(self, timeout=2.0):
        try:
            self.sock.close()
        finally:
            self.thread.join(timeout)
            try:
                self._server_sock.close()
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class MultiClient:
    """Two or more FakeClients in the world of ONE server (party-test-harness; aliases
    trade-harness, social_friend-harness-multiclient) - the offline form of the P5 setup
    "WindSlayer_patched.exe as test/test + WindSlayer_p2.exe as admin/admin".

        server = make_server(tmp, accounts=two_player_accounts(), client_build='2009')
        with MultiClient(server) as mc:                  # TestHero (c:1), Watcher (c:2)
            a, b = mc                                    # FakeClient each, in world on 101
            mc.drain()                                   # whatever the entries pushed
            server._admin_command('{"opcode": 21, "payload_hex": "...", "target": "c:2"}')

    players: (account, password, character) per client, logged in and entered in order.
    Client k (1-based) sends P2P port 42907 + k - 1 in its C2S 0x2B, like the real exes,
    so `target: "c:2"` / `wsdev ... --to c:2` reaches exactly client 2. The build is the
    server's (both builds share every step through login_fields / ENTER_KEYS). Each
    client gets .number, .char_name and .entry (the packets of its own map load)."""

    DEFAULT_PLAYERS = (('test', 'test', 'TestHero'), ('admin', 'admin', 'Watcher'))

    def __init__(self, server, players=DEFAULT_PLAYERS, refuse=None):
        self.server = server
        self.clients = []
        try:
            for i, (account, password, name) in enumerate(players):
                c = FakeClient(server)
                self.clients.append(c)
                c.number, c.char_name = i + 1, name
                result = c.login(account, password).get('result')
                if result != 1:
                    raise AssertionError(f'login {account!r} -> 0x02 result {result}')
                c.entry = c.enter_world(name, port=P2P_PORT_BASE + i, refuse=(refuse or {}).get(name))
        except BaseException:
            self.close()
            raise

    def __iter__(self):
        return iter(self.clients)

    def __len__(self):
        return len(self.clients)

    def __getitem__(self, i):
        return self.clients[i]

    def by_name(self, name):
        for c in self.clients:
            if c.char_name.lower() == str(name).lower():
                return c
        raise KeyError(name)

    def drain(self, quiet=0.2):
        """{character: [packets]} of everything pending on every client."""
        return {c.char_name: c.recv_until_quiet(quiet) for c in self.clients}

    def close(self):
        for c in self.clients:
            try:
                c.close()
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def presence_peers(c, others, map_code):
    """The clients of `others` whose session is in world on `map_code` with another uid than
    `c`'s: the players whose records `c` gets in its arrival S2C 0x04, and who each get `c`
    as one S2C 0x05 (world-presence, P5 stage 2)."""
    import world as worldmod
    mine = (c.session or {}).get('uid')
    out = []
    for o in others:
        s = o.session if o is not c else None
        if s is not None and worldmod.reachable(s) and s.get('world_map') == int(map_code) \
                and s.get('uid') != mine:
            out.append(o)
    return out


def close_seen(c, others=(), timeout=5.0):
    """Close client `c` and consume the S2C 0x06 {its uid} that each of `others` in world on
    its map gets (world-presence: on_leave_world). Returns those clients."""
    s = c.session or {}
    uid, code = s.get('uid'), s.get('world_map')
    peers = presence_peers(c, others, code) if code is not None else []
    c.close()
    c.thread.join(timeout=timeout)
    for o in peers:
        got = o.s2c(o.expect(0x06, timeout=timeout))
        if got != {'uid': uid}:
            raise AssertionError(f'expected 0x06 {{uid: {uid}}} on {o.addr}, got {got}')
    return peers


def expect_entry(c, ops, others=(), map_code=101, timeout=3.0):
    """c.expect(*ops) for a map load (enter world, portal) that knows about world-presence:
    when some of `others` are in world on `map_code`, `c` also gets ONE S2C 0x04 with their
    records right before its 0x28 (after the own 0x07 and the on_enter_world lines) and each
    of them gets `c` as one S2C 0x05. Returns c's packets WITHOUT the 0x04, so the callers'
    positional unpacking of the single-player sequence stays valid; test_presence.py checks
    the presence packets themselves."""
    peers = presence_peers(c, others, map_code)
    want = list(ops)
    if peers:
        want.insert(want.index(0x28) if 0x28 in want else len(want), 0x04)
    pkts = c.expect(*want, timeout=timeout)
    if not isinstance(pkts, list):
        pkts = [pkts]
    if peers:
        rows = [r['uid'] for p in pkts if p.opcode == 0x04 for r in c.s2c(p)['repeat[player_count]']]
        if sorted(rows) != sorted(o.session['uid'] for o in peers):
            raise AssertionError(f'0x04 carried uids {rows}, expected {[o.session["uid"] for o in peers]}')
        for o in peers:
            o.expect(0x05)
    return [p for p in pkts if p.opcode != 0x04]


def _ops(opcodes):
    return '[' + ' '.join(f'0x{o:02X}' for o in opcodes) + ']'
