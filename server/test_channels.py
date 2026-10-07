#!/usr/bin/env python3
"""
test_channels.py - P12 stage 2 (ch-1, ch-4, arch09-session-continuity, ch-5,
arch09-channel-key), offline, both client builds
==============================================================================
Design: re_tools/docs/systems_2009/blacklist_channels.md Part B (blch) + C CH-1/CH-4/CH-5,
ROADMAP_2009_ADDENDUM P12 and 3.0 (channels.py, continuity.py).

- config: CHANNELS port / open / max_users, slots 1..10, a 2009 (ip, port) named twice, the
  2008 warnings, LOAD_SCALE / CHANNEL_HOP_SECS, the shipped default (ONE channel) and the
  dev setup config_channels_dev.json (channels 1-3 on 7022-7024, 4 closed);
- the S2C 0x01 table of both builds: slot count, only open AND up channels as entries
  ("(inspection)" otherwise, R8), each channel's own (ip, port), its own count x LOAD_SCALE,
  the load labels; listeners per port (a fake socket class: no port is bound) with a failed
  bind - or a port another process already listens on (Windows SO_REUSEADDR) - left out; the
  accepted connection's channel by port / local IP;
- the listener as the channel (fake clients "arriving" on a channel): 0x03 channel_id,
  "In channel N.", per-channel counts; the admin / GM open-close (0x02 0x0E on a closed
  channel, its players stay); a full channel answers 0x02 result 6;
- ch-4 + session continuity (2009): a channel hop keeps the friend rows online and sends
  only the new channel (one 0x60), no welcome, no event tail, "In channel N." of the new
  channel; a hop back to the same channel tells the friends nothing; a quit logs out at
  once; a held logout fires after CHANNEL_HOP_SECS; Change Avatar to another character and
  a relogin closed at select log the old one out, and so does one that idles at select for
  CHANNEL_HOP_SECS (a later entry is still a hop); a close the server / TCP stack made is
  never held back; a relogin replacing an open socket waits for its save; G-CHP (the party
  is left); E-B5 key adoption and the stale-0x408 log; the 2008 build never holds a logout
  back; an event that starts after a hop's first 0x63 is delivered at the next one;
- arch09-channel-key: key() and the boss ledger's channel = the shared world's.

No port is bound, no client is started and the live accounts.json is never opened (temp
copies; the module checks its hash at the end).
"""
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
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import channels as CH  # noqa: E402
import continuity as CONT  # noqa: E402
import fakeclient as F  # noqa: E402
import packets as P  # noqa: E402
import world as WM  # noqa: E402

W = F.import_server()
cfgmod = W.cfgmod
EC = W.EC
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
DEV_CONFIG = os.path.join(HERE, 'config_channels_dev.json')
B8, B9 = cfgmod.BUILD_2008, cfgmod.BUILD_2009
DIRS = {B8: cfgmod.DEFAULTS['CLIENT_DIR'], B9: cfgmod.DEFAULTS['CLIENT_DIR_2009']}
HAVE = {b: os.path.exists(os.path.join(HERE, d, 'hs', 'windslayer.hii')) for b, d in DIRS.items()}
ENTER = {B8: '0x42F904/0x2B', B9: '0x4315D7/0x2B'}
ENTER_RELOGIN = '0x452133/0x2B'                 # 2009: the automatic 0x2B of S2C 0x02 + gs+0x408
IP = '127.0.0.1'                                # every fake client's address
# The P12 exit-criteria channels: 2009 = one port each, 2008 = one IP each (port 7022 fixed).
CHANNELS = {
    B9: [{'no': 1, 'port': 7022}, {'no': 2, 'port': 7023}, {'no': 3, 'port': 7024},
         {'no': 4, 'port': 7025, 'open': False}],
    B8: [{'no': 1}, {'no': 2, 'ip': '127.0.0.2'}, {'no': 3, 'ip': '127.0.0.3'},
         {'no': 4, 'ip': '127.0.0.4', 'open': False}],
}
LOVE_POTION = 1282
EVENT = {'id': 'hop-test', 'enabled': True, 'start': None, 'end': None, 'exp_mult': 2.0,
         'announce': {'text': 'EXP x2 event! Visit Nicolas.', 'every_min': 0, 'on_enter': True},
         'popup_event_news': True, 'login_gift': [[LOVE_POTION, 5]], 'push_quests': []}

# An event that is off until a GM override starts it (EventsHop2009, P12 review).
LATE = {'id': 'late-start', 'enabled': False, 'start': None, 'end': None, 'exp_mult': 1.5,
        'announce': {'text': 'A late event!', 'every_min': 0, 'on_enter': True},
        'popup_event_news': True, 'login_gift': [], 'push_quests': []}

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
            'accounts.json changed during test_channels.py (tests must only use temp copies)'


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


def config(build, **over):
    return cfgmod.from_dict({'CLIENT_BUILD': build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False,
                             'CHANNELS': CHANNELS[build], **over})


def raw_entries(build, body, notice_len):
    """(slot_count, [(no, users, ip, port)]) read straight off the S2C 0x01 bytes - the walk
    ParseChannelList does (spec 0x01: entries ascending, one u8 channel_no before each; the
    spec-driven parser only models a full table). port None on 2008."""
    tail = body[5 + notice_len:]
    count = tail[3]
    size = 11 if build == B9 else 7
    out = []
    for i in range(count):
        e = tail[4 + i * size:4 + (i + 1) * size]
        users, ip = struct.unpack_from('<HI', e, 1)
        port = struct.unpack_from('<I', e, 7)[0] if build == B9 else None
        out.append((e[0], users, ip, port))
    return tail[2], out


NOTICE_LEN = len(cfgmod.defaults()['NOTICE'].encode('latin-1'))


# ==================================================================== config ===
class ChannelConfig(unittest.TestCase):
    def test_the_shipped_default_is_one_channel_on_game_port(self):
        for build in (B8, B9):
            chans = CH.Channels(cfgmod.from_dict({'CLIENT_BUILD': build})).all()
            self.assertEqual([(c.no, c.ip, c.port, c.configured_open) for c in chans],
                             [(1, '127.0.0.1', 7022, True)])
        with open(os.path.join(HERE, 'config.json'), encoding='utf-8') as f:
            shipped = json.load(f)
        self.assertEqual(shipped['CHANNELS'], [{'no': 1, 'name': 'Channel 1'}])
        self.assertEqual(dict(cfgmod.from_dict(shipped)), dict(cfgmod.defaults()))

    def test_the_dev_config_is_the_p12_exit_setup(self):
        cfg = cfgmod.load(DEV_CONFIG)
        self.assertEqual(cfg.CLIENT_BUILD, B9)
        chans = CH.Channels(cfg)
        self.assertEqual([(p, [c.no for c in cs]) for p, cs in chans.listeners()],
                         [(7022, [1]), (7023, [2]), (7024, [3]), (7025, [4])])
        self.assertEqual([c.no for c in chans.advertised()], [1, 2, 3])          # 4 = "(inspection)"
        self.assertEqual(chans.slot_count(), 4)
        self.assertEqual(cfg.accounts_path, os.path.join(HERE, 'accounts.json'))

    def test_unusable_channel_entries_are_refused(self):
        bad = [
            {'CHANNELS': [{'no': 11}]},                                    # slot table holds 10
            {'CHANNELS': [{'no': 1}, {'no': 2}], 'CLIENT_BUILD': B9},       # both on PUBLIC_IP:GAME_PORT
            {'CHANNELS': [{'no': 1, 'port': 7023}, {'no': 2, 'port': 7023}], 'CLIENT_BUILD': B9},
            {'CHANNELS': [{'no': 1, 'port': 0}]}, {'CHANNELS': [{'no': 1, 'port': '7022'}]},
            {'CHANNELS': [{'no': 1, 'open': 'yes'}]}, {'CHANNELS': [{'no': 1, 'max_users': 0}]},
            {'LOAD_SCALE': -1.0}, {'LOAD_SCALE': float('nan')}, {'CHANNEL_HOP_SECS': -1.0},
        ]
        for overrides in bad:
            with self.subTest(overrides), self.assertRaises(cfgmod.ConfigError):
                cfgmod.from_dict(overrides)
        # the same IP on two ports is two 2009 channels
        cfgmod.from_dict({'CLIENT_BUILD': B9, 'CHANNELS': [{'no': 1}, {'no': 2, 'port': 7023}]})

    def test_2008_warnings(self):
        with self.assertLogs('WS', logging.WARNING) as cm:
            cfg = cfgmod.from_dict({'CHANNELS': [{'no': 1, 'port': 7023, 'colour': 'red'}]})
        text = '\n'.join(cm.output)
        self.assertIn('port 7023 ignored', text)
        self.assertIn("unknown key(s) ['colour']", text)
        self.assertEqual(CH.Channels(cfg).all()[0].port, 7022)                   # 2008: GAME_PORT


# ============================================================ channel table ===
class ChannelTable(unittest.TestCase):
    def test_load_labels(self):
        self.assertEqual([CH.load_label(n) for n in (0, 199, 200, 399, 400, 65535)],
                         ['Idle', 'Idle', 'Normal', 'Normal', 'Busy', 'Busy'])

    def test_2009_reply_lists_open_up_channels_with_their_ports_and_counts(self):
        cfg = config(B9)
        counts = {1: 1, 2: 0, 3: 2, 4: 5}
        vs = W.VersionServer(config=cfg, user_counts=lambda: counts)
        body = vs._build_version_body()
        slots, rows = raw_entries(B9, body, NOTICE_LEN)
        self.assertEqual(slots, 4)
        self.assertEqual(rows, [(1, 1, 0x7F000001, 7022), (2, 0, 0x7F000001, 7023), (3, 2, 0x7F000001, 7024)])
        cfg['LOAD_SCALE'] = 250.0                                               # one player = "Normal"
        _, rows = raw_entries(B9, vs._build_version_body(), NOTICE_LEN)
        self.assertEqual([(r[0], r[1], CH.load_label(r[1])) for r in rows],
                         [(1, 250, 'Normal'), (2, 0, 'Idle'), (3, 500, 'Busy')])
        cfg['LOAD_SCALE'] = 1.0
        vs.channels.set_open(3, False)                                         # admin close
        self.assertEqual([r[0] for r in raw_entries(B9, vs._build_version_body(), NOTICE_LEN)[1]], [1, 2])
        vs.channels.mark_up([2], False)                                         # R8: a dead listener
        self.assertEqual([r[0] for r in raw_entries(B9, vs._build_version_body(), NOTICE_LEN)[1]], [1])
        vs.channels.set_open(4, True)
        slots, rows = raw_entries(B9, vs._build_version_body(), NOTICE_LEN)
        self.assertEqual((slots, [r[0] for r in rows], rows[-1][3]), (4, [1, 4], 7025))
        with self.assertRaises(KeyError):
            vs.channels.set_open(9, True)

    def test_2008_reply_has_7_byte_entries_one_ip_each(self):
        vs = W.VersionServer(config=config(B8), user_counts=lambda: {2: 3})
        slots, rows = raw_entries(B8, vs._build_version_body(), NOTICE_LEN)
        self.assertEqual(slots, 4)
        self.assertEqual(rows, [(1, 0, 0x7F000001, None), (2, 3, 0x7F000002, None), (3, 0, 0x7F000003, None)])
        # every channel open and up, every count 0: the old body (the live-proven default shape)
        body = W.VersionServer(config=cfgmod.defaults())._build_version_body()
        self.assertEqual(body[-11:], bytes([1, 3, 1, 1, 1]) + struct.pack('<HI', 0, 0x7F000001))

    def test_listeners_and_the_channel_of_a_connection(self):
        nine = CH.Channels(config(B9))
        self.assertEqual([p for p, _ in nine.listeners()], [7022, 7023, 7024, 7025])
        self.assertEqual([nine.resolve(p, IP) for p in (7022, 7023, 7024, 7025, 7999)], [1, 2, 3, 4, None])
        eight = CH.Channels(config(B8))
        self.assertEqual([(p, [c.no for c in cs]) for p, cs in eight.listeners()], [(7022, [1, 2, 3, 4])])
        self.assertEqual([eight.resolve(7022, ip) for ip in ('127.0.0.1', '127.0.0.3', '10.9.9.9', None)],
                         [1, 3, 1, 1])
        self.assertEqual(CH.key(2, 101, (5, 6)), (2, 101, (5, 6)))              # arch09-channel-key

    def test_version_fetch_hint_window(self):
        chans = CH.Channels(config(B9))
        chans.note_version_fetch(IP, t=100.0)
        self.assertTrue(chans.version_fetched(IP, 100.2))                       # the close right after
        self.assertTrue(chans.version_fetched(IP, 100.0))
        # a fetch booked AFTER the close is not seen: the hint is read once, at the close (the
        # documented limit, continuity.py; VERSION_HINT_AFTER_SECS was removed - P12 review)
        self.assertFalse(chans.version_fetched(IP, 99.5))
        self.assertFalse(hasattr(CH, 'VERSION_HINT_AFTER_SECS'))
        self.assertFalse(chans.version_fetched(IP, 106.0))                       # too old: a quit
        self.assertFalse(chans.version_fetched('10.0.0.9', 100.2))
        chans.note_version_fetch('10.0.0.9', t=200.0)                           # prunes the old IP
        self.assertNotIn(IP, chans._fetches)


class FakeListenSocket:
    """Stands in for socket.socket in bind_listeners: records binds, refuses some ports;
    `listening` = ports another process listens on (the Windows probe's connect succeeds)."""
    refuse = set()
    listening = set()
    bound = []
    probed = []

    def __init__(self, *args):
        self.addr = None

    def setsockopt(self, *args):
        pass

    def settimeout(self, secs):
        pass

    def connect_ex(self, addr):
        FakeListenSocket.probed.append(addr)
        return 0 if addr[1] in self.listening else 10061                      # WSAECONNREFUSED

    def bind(self, addr):
        if addr[1] in self.refuse:
            raise OSError(10048, 'address in use')
        self.addr = addr
        FakeListenSocket.bound.append(addr)

    def listen(self, n):
        pass

    def close(self):
        pass


class FakeAccepted:
    def __init__(self, local_ip):
        self.local_ip = local_ip

    def getsockname(self):
        if self.local_ip is None:
            raise OSError('gone')
        return (self.local_ip, 7022)


class Listeners(unittest.TestCase):
    """ch-1: GameServer.bind_listeners without a real port (socket.socket replaced)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_listeners_')
        FakeListenSocket.refuse, FakeListenSocket.bound = set(), []
        FakeListenSocket.listening, FakeListenSocket.probed = set(), []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def server(self, build):
        if not HAVE[build]:
            self.skipTest(f'needs the EN {build} client data')
        return F.make_server(self.tmp, config=config(build))

    def test_2009_one_listener_per_channel_port_and_a_failed_bind_is_never_advertised(self):
        server = self.server(B9)
        FakeListenSocket.refuse = {7024}
        with mock.patch.object(W.socket, 'socket', FakeListenSocket), self.assertLogs('WS', logging.ERROR) as cm:
            bound = server.bind_listeners()
        self.assertTrue(any('7024' in line and '(inspection)' in line for line in cm.output))
        self.assertEqual([port for _s, port, _c in bound], [7022, 7023, 7025])
        self.assertEqual([a[1] for a in FakeListenSocket.bound], [7022, 7023, 7025])
        self.assertIs(server.bind_listeners(), bound)                           # once
        vs = W.VersionServer(config=server.config, user_counts=server.channel_user_counts, channels=server.channels)
        self.assertEqual([r[0] for r in raw_entries(B9, vs._build_version_body(), NOTICE_LEN)[1]], [1, 2])
        self.assertEqual(server._accepted_channel(FakeAccepted(IP), 7023), 2)

    def test_a_port_another_process_listens_on_is_left_out(self):
        """P12 review (R8 on Windows): SO_REUSEADDR binds a port a live server listens on
        without an error, so the bind asks first - a loopback connect that is accepted means
        another listener (the dev server beside the live one)."""
        server = self.server(B9)
        FakeListenSocket.listening = {7022}
        with mock.patch.object(W.socket, 'socket', FakeListenSocket), \
                mock.patch.object(W.GameServer, 'PORT_PROBE', True), self.assertLogs('WS', logging.ERROR) as cm:
            bound = server.bind_listeners()
        self.assertTrue(any('7022 already has a listener' in line for line in cm.output))
        self.assertEqual([port for _s, port, _c in bound], [7023, 7024, 7025])
        self.assertEqual([a[1] for a in FakeListenSocket.bound], [7023, 7024, 7025])   # 7022 never bound
        self.assertEqual([a[1] for a in FakeListenSocket.probed], [7022, 7023, 7024, 7025])
        self.assertEqual(FakeListenSocket.probed[0][0], '127.0.0.1')
        self.assertFalse(server.channels.is_up(1))
        # POSIX refuses such a bind by itself: no probe there
        FakeListenSocket.probed = []
        with mock.patch.object(W.socket, 'socket', FakeListenSocket), \
                mock.patch.object(W.GameServer, 'PORT_PROBE', False):
            self.assertFalse(server._port_in_use(7022))
        self.assertEqual(FakeListenSocket.probed, [])

    def test_no_listener_at_all_raises(self):
        server = self.server(B9)
        FakeListenSocket.refuse = {7022, 7023, 7024, 7025}
        with mock.patch.object(W.socket, 'socket', FakeListenSocket), self.assertLogs('WS', logging.ERROR):
            with self.assertRaises(OSError):
                server.bind_listeners()

    def test_2008_one_listener_on_game_port_channel_by_local_ip(self):
        server = self.server(B8)
        with mock.patch.object(W.socket, 'socket', FakeListenSocket):
            bound = server.bind_listeners()
        self.assertEqual([(port, [c.no for c in chans]) for _s, port, chans in bound], [(7022, [1, 2, 3, 4])])
        self.assertEqual([server._accepted_channel(FakeAccepted(ip), 7022)
                          for ip in ('127.0.0.2', '127.0.0.3', '127.0.0.1', None)], [2, 3, 1, 1])


# ========================================================= the listener flows ===
class _ChannelFlows:
    """Both builds: the listener a fake client arrives on is its channel."""
    build = B8

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        EC.configure(DIRS[self.build], self.build)
        self.tmp = tempfile.mkdtemp(prefix=f'ws_channels_{self.build}_')
        self.server = F.make_server(self.tmp, accounts=F.two_player_accounts(), config=config(self.build))
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            try:
                c.close()
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)
        EC.configure(DIRS[B8], B8)

    def connect(self, channel=None):
        c = F.FakeClient(self.server, channel=channel)
        self.clients.append(c)
        return c

    def enter(self, account, password, name, channel, number=1):
        c = self.connect(channel)
        self.assertEqual(c.login(account, password)['result'], 1)
        pkts = c.enter_world(name, port=F.P2P_PORT_BASE + number - 1)
        return c, pkts

    def resync(self, c):
        c.send(0x63)
        return [p for p in c.recv_until_quiet(0.3)]

    def test_the_listener_is_the_channel_of_0x03_and_in_channel_n(self):
        a, pkts = self.enter('test', 'test', 'TestHero', channel=2)
        self.assertEqual(a.session['channel'], 2)
        state = a.s2c(next(p for p in pkts if p.opcode == 0x03))
        self.assertEqual(state['channel_id'], 2)                                 # blch B.4 / R6
        notice = [a.s2c(p) for p in self.resync(a) if p.opcode == 0x99]
        self.assertEqual(notice, [{'sub_type': 8, 'channel_no': 2}])            # "In channel 2."
        b, _ = self.enter('admin', 'admin', 'Watcher', channel=3, number=2)
        self.assertEqual(self.server.channel_user_counts(), {1: 0, 2: 1, 3: 1, 4: 0})
        vs = W.VersionServer(config=self.server.config, user_counts=self.server.channel_user_counts,
                             channels=self.server.channels)
        self.assertEqual([r[:2] for r in raw_entries(self.build, vs._build_version_body(), NOTICE_LEN)[1]],
                         [(1, 0), (2, 1), (3, 1)])
        # a connection with no listener (the rigs' default) is on the first channel
        self.assertEqual(self.server._channel_no({}), 1)

    def test_a_closed_channel_refuses_new_logins_and_keeps_its_players(self):
        a, _ = self.enter('test', 'test', 'TestHero', channel=2)
        reply = self.server._admin_command(json.dumps({'channel_open': [2, 0]}))
        self.assertTrue(reply.startswith('ok channels: '), reply)
        self.assertIn('ch2 Channel 2', reply)
        self.assertIn('CLOSED', reply)
        vs = W.VersionServer(config=self.server.config, user_counts=self.server.channel_user_counts,
                             channels=self.server.channels)
        self.assertEqual([r[0] for r in raw_entries(self.build, vs._build_version_body(), NOTICE_LEN)[1]], [1, 3])
        b = self.connect(channel=2)
        self.assertEqual(b.login('admin', 'admin')['result'], 0x0E)
        self.assertTrue(a.session.get('in_world'))                              # its players stay
        self.assertEqual(self.server._admin_command(json.dumps({'channel_open': [9, 1]})),
                         "error: 'no channel 9 (configured: [1, 2, 3, 4])'")
        self.assertTrue(self.server._admin_command('{"channel_open": [2]}').startswith('error'))
        # the GM path: `!channel open 2` (and the list) answers 0x15 lines on the caller
        a.recv_until_quiet(0.2)
        self.server._gm_chat_command(a.session['sock'], a.session, b'!channel open 2')
        lines = [_text(a, p) for p in a.recv_until_quiet(0.3) if p.opcode == 0x15]
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[1].startswith('ch2 Channel 2') and 'open' in lines[1] and lines[1].endswith('<- you'),
                        lines)
        self.assertIn('(inspection)', lines[3])                                 # ch4: configured closed
        c = self.connect(channel=2)
        self.assertEqual(c.login('admin', 'admin')['result'], 1)
        self.server._gm_chat_command(a.session['sock'], a.session, b'!channel scale 250')
        a.recv_until_quiet(0.2)
        _, rows = raw_entries(self.build, vs._build_version_body(), NOTICE_LEN)
        self.assertEqual([(r[0], r[1]) for r in rows], [(1, 0), (2, 500), (3, 0)])   # 2 accounts x 250
        self.server._gm_chat_command(a.session['sock'], a.session, b'!channel close 12')
        self.assertIn('channel', _text(a, a.expect(0x15)))

    def test_a_full_channel_answers_result_6(self):
        self.server.config['CHANNELS'] = [dict(c, max_users=1) if c['no'] == 2 else c for c in CHANNELS[self.build]]
        a, _ = self.enter('test', 'test', 'TestHero', channel=2)
        b = self.connect(channel=2)
        self.assertEqual(b.login('admin', 'admin')['result'], 6)
        c = self.connect(channel=1)
        self.assertEqual(c.login('admin', 'admin')['result'], 1)
        if self.build == B9:
            # the account's own session does not count: a relogin onto its full channel works
            d = self.connect(channel=2)
            self.assertEqual(d.login('test', 'test', session_key=a.session['session_key'])['result'], 1)


def _text(c, pkt):
    text = c.s2c(pkt)['text']
    return text.decode('latin-1') if isinstance(text, bytes) else text


class ChannelFlows2008(_ChannelFlows, unittest.TestCase):
    build = B8

    def test_2008_never_holds_a_logout_back(self):
        """The 2008 client has no session key and no Change Channel: a close is a logout, even
        with a version fetch from its IP right before it."""
        a, _ = self.enter('test', 'test', 'TestHero', channel=1)
        b, _ = self.enter('admin', 'admin', 'Watcher', channel=2, number=2)
        with self.server.store.lock:
            self.server.store.character_by_name('Watcher')[2]['friends'] = ['TestHero']
        b.recv_until_quiet(0.2)
        self.server.channels.note_version_fetch(IP)
        a.close()
        got = [b.s2c(p) for p in b.recv_until_quiet(0.4) if p.opcode == 0x60]
        self.assertEqual([(g['friend_uid'], g['presence']) for g in got], [(1, 1)])
        self.assertEqual(self.server.continuity.departures, {})


class ChannelFlows2009(_ChannelFlows, unittest.TestCase):
    build = B9


# ===================================================== ch-4 + session continuity ===
class Continuity2009(unittest.TestCase):
    """TestHero (A, uid 1) and Watcher (B, uid 2), friends, in world on 101, channel 1."""
    hop_secs = 30.0

    def setUp(self):
        if not HAVE[B9]:
            self.skipTest('needs the EN 2009 client data')
        EC.configure(DIRS[B9], B9)
        self.tmp = tempfile.mkdtemp(prefix='ws_continuity_')
        accounts = F.two_player_accounts()
        accounts['test']['characters'].append({'name': 'Second', 'level': 1, 'class': 0, 'map': 101,
                                               'x': 1411.0, 'y': 714.0, 'hp': 100, 'mp': 50})
        self.server = F.make_server(self.tmp, accounts=accounts,
                                    config=config(B9, CHANNEL_HOP_SECS=self.hop_secs))
        with self.server.store.lock:
            self.char('TestHero')['friends'] = ['Watcher']
            self.char('Watcher')['friends'] = ['TestHero']
        self.logouts = []
        self.server.world.hooks.register(WM.ON_LOGOUT, lambda server, s, char_name=None, **kw:
                                         self.logouts.append(char_name))
        self.extra = []
        self.mc = F.MultiClient(self.server)
        self.a, self.b = self.mc
        self.mc.drain()

    def tearDown(self):
        for c in self.extra:
            try:
                c.close()
            except OSError:
                pass
        self.mc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        EC.configure(DIRS[B8], B8)

    # ------------------------------------------------------------ helpers ---
    def char(self, name):
        return self.server.store.character_by_name(name)[2]

    def presence(self, c, quiet=0.4):
        """(friend uid, channel, presence) of every S2C 0x60 `c` got."""
        return [(g['friend_uid'], g['channel'], g['presence'])
                for g in (c.s2c(p) for p in c.recv_until_quiet(quiet) if p.opcode == 0x60)]

    def rows(self, c):
        """C2S 0x2F -> the decoded 0x0B rows (name, channel, status)."""
        c.send(0x2F)
        pkts = c.recv_until_quiet(0.3)
        self.assertEqual(_ops(pkts)[-1], 0xBD)
        rec = c.s2c(next(p for p in pkts if p.opcode == 0x0B))
        return [(_name(r['name']), r['channel'], r['status']) for r in rec['repeat[friend_count]']]

    def leave(self, c, fetch=True):
        """Change Channel / Change Avatar teardown (blch B.5 steps 2-3): the version fetch,
        then the game socket closes."""
        key, old = c.session['session_key'], c.session
        if fetch:
            self.server.channels.note_version_fetch(IP)
        else:
            self.server.channels._fetches.clear()                              # a plain quit
        c.close()
        self.assertTrue(_wait(lambda: old.get('closed') and old['closed_event'].is_set()))
        return key, old

    def relogin(self, key, channel, account='test', password='test'):
        n = F.FakeClient(self.server, channel=channel)
        self.extra.append(n)
        rec = n.login(account, password, session_key=key)
        self.assertEqual(rec['result'], 1)
        return n, rec

    def auto_enter(self, n, name='TestHero', number=1):
        """The gs+0x408 automatic 0x2B; the map load's packets."""
        n.send_c2s(ENTER_RELOGIN, {'p2p_ip': '127.0.0.1', 'p2p_udp_port': F.P2P_PORT_BASE + number - 1,
                                   'char_name': name})
        self.assertTrue(n.wait_session(lambda s: s.get('in_world')))
        return n.recv_until_quiet(0.3)

    def hop(self, c, channel, fetch=True):
        key, old = self.leave(c, fetch)
        n, rec = self.relogin(key, channel)
        self.assertEqual(rec['session_key'], key)
        return n, self.auto_enter(n), old

    # -------------------------------------------------------------- tests ---
    def test_a_channel_hop_continues_the_login(self):
        """P12 exit criterion 6 (offline): Change Channel 1 -> 2 - no character select, the
        friend sees no logout / login pair, only the new channel; no second welcome."""
        key, old = self.leave(self.a)
        self.assertEqual(self.presence(self.b), [])                             # no 0x60 offline
        self.assertEqual(self.logouts, [])
        self.assertIsNotNone(self.server.continuity.ghost('TestHero'))
        self.assertEqual(self.rows(self.b), [('TestHero', 1, 0)])               # the row stays online
        n, rec = self.relogin(key, channel=2)
        self.assertEqual(rec['session_key'], key)
        entry = self.auto_enter(n)
        self.assertEqual([op for op in _ops(entry) if op != 0x04], [0x03, 0x07, 0x65, 0x28, 0x44])
        self.assertEqual(n.s2c(entry[0])['channel_id'], 2)
        hop = n.session['channel_hop']
        self.assertEqual((hop.char_name, hop.from_channel, hop.to_channel, hop.logout_announced),
                         ('TestHero', 1, 2, False))
        self.assertEqual(self.presence(self.b), [(1, 2, 0)])                    # one 0x60: the new channel
        self.assertEqual([a for a in (n.s2c(p) for p in self.resync(n)) if a.get('sub_type') == 8],
                         [{'sub_type': 8, 'channel_no': 2}])                    # "In channel 2."
        self.assertEqual(self.rows(self.b), [('TestHero', 2, 0)])
        self.assertIsNone(self.server.continuity.ghost('TestHero'))
        self.assertEqual(self.logouts, [])
        self.assertEqual(self.server.channel_user_counts(), {1: 1, 2: 1, 3: 0, 4: 0})
        # ten quick hops in a row: never a hang, never a logout, never result 4
        for i in range(10):
            n, entry, _ = self.hop(n, channel=1 + (i % 3))
            self.assertNotIn(0x15, _ops(entry))
        self.assertEqual(self.logouts, [])
        self.assertTrue(all(p[2] == 0 for p in self.presence(self.b)))

    def resync(self, c):
        c.send(0x63)
        return [p for p in c.recv_until_quiet(0.3) if p.opcode == 0x99]

    def test_a_hop_back_to_the_same_channel_tells_the_friends_nothing(self):
        n, entry, _ = self.hop(self.a, channel=1)
        self.assertNotIn(0x15, _ops(entry))
        self.assertEqual(self.presence(self.b), [])
        self.assertEqual(self.logouts, [])

    def test_a_quit_logs_out_at_once_and_a_quick_relogin_still_skips_the_welcome(self):
        key, _ = self.leave(self.a, fetch=False)
        self.assertEqual(self.presence(self.b), [(1, 0, 1)])                    # offline, at once
        self.assertEqual(self.logouts, ['TestHero'])
        self.assertIsNone(self.server.continuity.ghost('TestHero'))
        n, _rec = self.relogin(key, channel=2)
        entry = self.auto_enter(n)
        self.assertNotIn(0x15, _ops(entry))                                    # still one login
        self.assertTrue(n.session['channel_hop'].logout_announced)
        self.assertEqual(self.presence(self.b), [(1, 2, 0)])                    # the login line again

    def test_a_held_logout_fires_when_no_relogin_comes(self):
        key, _ = self.leave(self.a)
        self.assertEqual(self.presence(self.b), [])
        self.server.ticks.run_due(time.monotonic() + self.hop_secs + 1)
        self.assertEqual(self.presence(self.b), [(1, 0, 1)])
        self.assertEqual(self.logouts, ['TestHero'])
        self.assertEqual(self.rows(self.b), [('TestHero', 0, 1)])
        # a relogin after the window is a login: the welcome and the login line
        n, _rec = self.relogin(key, channel=1)
        self.assertIn(0x15, _ops(self.auto_enter(n)))
        self.assertIsNone(n.session['channel_hop'])
        self.assertEqual(self.presence(self.b), [(1, 1, 0)])

    def test_change_avatar_to_another_character_logs_the_old_one_out(self):
        key, _ = self.leave(self.a)
        n, _rec = self.relogin(key, channel=1)                                  # character select
        self.assertEqual(self.presence(self.b), [])
        pkts = n.enter_world('Second')                                          # the select screen's 0x2B
        self.assertIn(0x15, _ops(pkts))                                         # a login of Second
        self.assertEqual(self.presence(self.b), [(1, 0, 1)])                    # TestHero is gone
        self.assertEqual(self.logouts, ['TestHero'])
        self.assertIsNone(n.session['channel_hop'])

    def test_change_avatar_back_to_the_same_character_is_a_hop(self):
        key, _ = self.leave(self.a)
        n, _rec = self.relogin(key, channel=1)
        self.assertNotIn(0x15, _ops(n.enter_world('TestHero')))
        self.assertEqual(self.presence(self.b), [])
        self.assertEqual(self.logouts, [])

    def test_change_avatar_then_idle_at_select_logs_out_after_the_window(self):
        """P12 review: the relogin re-arms the 'channel-hop' timer, so a player who sits at
        character select does not stay online to the watchers; entering the same character
        later is still a hop (no second welcome), with the login line again."""
        key, _ = self.leave(self.a)
        n, _rec = self.relogin(key, channel=1)                                  # character select
        self.assertEqual(self.presence(self.b), [])
        self.assertIsNotNone(self.server.continuity.ghost('TestHero'))
        self.server.ticks.run_due(time.monotonic() + self.hop_secs + 1)
        self.assertEqual(self.logouts, ['TestHero'])
        self.assertEqual(self.presence(self.b), [(1, 0, 1)])                    # the row goes grey
        self.assertIsNone(self.server.continuity.ghost('TestHero'))
        self.assertNotIn(0x15, _ops(n.enter_world('TestHero')))                  # still one login
        self.assertTrue(n.session['channel_hop'].logout_announced)
        self.assertEqual(self.presence(self.b), [(1, 1, 0)])                    # the login line again
        self.server.ticks.run_due(time.monotonic() + self.hop_secs + 1)
        self.assertEqual(self.logouts, ['TestHero'])                            # once

    def test_server_made_closes_are_never_held_back(self):
        """P12 review: the idle reaper, the dead-peer keepalive and a handler error close the
        connection themselves - never a Change Channel teardown, whatever the version hint says;
        a client's RST ('recv error') is still the client's close."""
        cont = self.server.continuity
        self.server.channels.note_version_fetch(IP)
        base = {'closing': 'client closed', 'uid': 1, 'addr': (IP, 50001)}
        self.assertEqual(cont._may_defer(base, cont.mono())[0], True)
        self.assertEqual(cont._may_defer(dict(base, closing='recv error'), cont.mono())[0], True)
        for reason in ('idle reap', 'connection timed out', 'error: boom'):
            with self.subTest(reason=reason):
                defer, why = cont._may_defer(dict(base, closing=reason), cont.mono())
                self.assertEqual((defer, 'server / TCP stack' in why), (False, True))

    def test_a_relogin_that_closes_at_select_logs_out(self):
        key, _ = self.leave(self.a)
        n, _rec = self.relogin(key, channel=2)
        self.assertEqual(self.presence(self.b), [])
        n.close()
        self.assertTrue(_wait(lambda: self.logouts == ['TestHero']))
        self.assertEqual(self.presence(self.b), [(1, 0, 1)])

    def test_a_relogin_replacing_an_open_socket_waits_for_its_save(self):
        """ch-4 / R3 / R5: the old socket is not reaped yet; the relogin's 0x02 goes out only
        after the old session's disconnect path saved its position, so the automatic 0x2B
        loads the point the player stood on."""
        old = self.a.session
        with self.server._combat_lock(old):
            old['pos'] = (1600.0, 812.0)
        real_save = self.server._save_world_state

        def slow_save(session, reason='world state'):
            if session is old and reason == 'disconnect':
                time.sleep(0.3)
            return real_save(session, reason=reason)
        self.server._save_world_state = slow_save
        n = F.FakeClient(self.server, channel=2)
        self.extra.append(n)
        self.assertEqual(n.login('test', 'test', session_key=old['session_key'])['result'], 1)
        self.assertTrue(old['closed_event'].is_set())                           # saved BEFORE the 0x02
        saved = (self.char('TestHero')['x'], self.char('TestHero')['y'])
        self.assertEqual(saved[0], 1600.0)                                      # (y: on the floor line)
        entry = self.auto_enter(n)
        self.assertNotIn(0x15, _ops(entry))
        self.assertEqual(n.session['pos'][0], 1600.0)                         # spawned where it stood
        self.assertEqual(self.presence(self.b), [(1, 2, 0)])                    # no offline line at all
        self.assertEqual(self.logouts, [])

    def test_g_chp_a_hop_leaves_the_party(self):
        """G-CHP decided (continuity.py): a party does not survive a channel change."""
        party = self.server.party
        party.invite(self.a.session, 2)
        party.accept(self.b.session, b'TestHero')
        self.assertIsNotNone(party.party_of(self.a.session))
        self.mc.drain(0.3)
        n, _entry, _old = self.hop(self.a, channel=2)
        self.assertIsNone(party.party_of(n.session))
        self.assertIsNone(party.party_of(self.b.session))

    def test_e_b5_a_non_zero_key_is_adopted_and_a_stale_408_is_logged(self):
        self.leave(self.a, fetch=False)
        self.server.ticks.run_due(time.monotonic() + self.hop_secs + 1)
        stale = 0x0BADC0DE
        n = F.FakeClient(self.server)
        self.extra.append(n)
        with self.assertLogs('WS', logging.INFO) as cm:
            self.assertEqual(n.login('test', 'test', session_key=stale)['session_key'], stale)
            n.send_c2s(ENTER_RELOGIN, {'p2p_ip': '127.0.0.1', 'p2p_udp_port': F.P2P_PORT_BASE,
                                       'char_name': 'TestHero'})
            self.assertTrue(n.wait_session(lambda s: s.get('in_world')))
        text = '\n'.join(cm.output)
        self.assertIn('adopted as the live key', text)
        self.assertIn('stale gs+0x408', text)
        self.assertEqual(self.server.session_keys[1], stale)
        # the client's next channel change presents it: a relogin, not result 4
        key, _ = self.leave(n)
        self.assertEqual(key, stale)
        m, _rec = self.relogin(stale, channel=3)
        self.assertNotIn(0x15, _ops(self.auto_enter(m)))
        # key 0 (the launcher's first login) still mints a fresh key
        self.leave(m, fetch=False)
        f = F.FakeClient(self.server)
        self.extra.append(f)
        self.assertNotIn(f.login('test', 'test')['session_key'], (0, stale))

    def test_a_departure_recorded_after_its_relogin_links_or_resolves(self):
        """continuity._superseded: the relogin's close wait ran out and relogged() is past;
        the replaced session's departure then links itself to the relogin (not entered yet),
        or - if it entered already - is one login (same character) or a logout (another)."""
        cont = self.server.continuity

        def dep():
            return CONT.Departure(uid=7, char_name='Ghost', channel=1, t=time.monotonic(),
                                  session={'uid': 7, 'msgr_announced': True, 'msgr_name': 'Ghost'})
        owner = {'uid': 7, 'relogged': True}
        d = dep()
        cont._superseded(d, owner)
        self.assertIs(owner['hop_from'], d)
        self.assertIs(cont.departures[7], d)
        self.assertEqual(cont.entering(owner, 'Ghost').from_channel, 1)        # the hop
        self.assertEqual(cont.departures, {})
        cont._superseded(dep(), {'uid': 7, 'relogged': True, 'login_char': 'ghost'})
        self.assertEqual((cont.departures, self.logouts), ({}, []))             # one login
        cont._superseded(dep(), {'uid': 7, 'relogged': True, 'login_char': 'Second'})
        self.assertEqual((cont.departures, self.logouts), ({}, ['Ghost']))      # Change Avatar
        cont._superseded(dep(), {'uid': 7})                                     # relogged() still to come
        self.assertEqual(cont.departures[7].char_name, 'Ghost')

    def test_logout_fires_once_per_login(self):
        n, _entry, _old = self.hop(self.a, channel=2)
        self.leave(n, fetch=False)
        self.assertEqual(self.logouts, ['TestHero'])
        self.assertEqual(self.presence(self.b), [(1, 2, 0), (1, 0, 1)])
        self.server.ticks.run_due(time.monotonic() + self.hop_secs + 1)
        self.assertEqual(self.logouts, ['TestHero'])


class ContinuityOff2009(Continuity2009):
    """CHANNEL_HOP_SECS 0: no hop detection - every relogin is a login (the old behaviour)."""
    hop_secs = 0.0

    def test_a_channel_hop_continues_the_login(self):
        key, _ = self.leave(self.a)
        self.assertEqual(self.presence(self.b), [(1, 0, 1)])
        n, _rec = self.relogin(key, channel=2)
        self.assertIn(0x15, _ops(self.auto_enter(n)))
        self.assertEqual(self.presence(self.b), [(1, 2, 0)])

    # the rest of the hop behaviour does not apply with detection off
    test_a_hop_back_to_the_same_channel_tells_the_friends_nothing = None
    test_a_quit_logs_out_at_once_and_a_quick_relogin_still_skips_the_welcome = None
    test_a_held_logout_fires_when_no_relogin_comes = None
    test_change_avatar_to_another_character_logs_the_old_one_out = None
    test_change_avatar_back_to_the_same_character_is_a_hop = None
    test_a_relogin_that_closes_at_select_logs_out = None
    test_a_relogin_replacing_an_open_socket_waits_for_its_save = None
    test_e_b5_a_non_zero_key_is_adopted_and_a_stale_408_is_logged = None
    test_logout_fires_once_per_login = None
    test_change_avatar_then_idle_at_select_logs_out_after_the_window = None
    test_server_made_closes_are_never_held_back = None


class EventsHop2009(unittest.TestCase):
    """arch09-session-continuity x P13 events: a hop repeats no entry announcement, gift or
    Event News popup - even with EVENT_RELOGIN_QUIET_SECS 0, where a plain relog would."""

    def setUp(self):
        if not HAVE[B9]:
            self.skipTest('needs the EN 2009 client data')
        EC.configure(DIRS[B9], B9)
        self.tmp = tempfile.mkdtemp(prefix='ws_events_hop_')
        path = os.path.join(self.tmp, 'events.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'events': [EVENT, LATE]}, f)
        self.server = F.make_server(self.tmp, config=config(B9, EVENTS_FILE=path, EVENT_RELOGIN_QUIET_SECS=0.0))
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        EC.configure(DIRS[B8], B8)

    def tail(self, c):
        """The C2S 0x63 reply after 0x8A / 0x99 sub 8: the event effects."""
        c.send(0x63)
        pkts = c.recv_until_quiet(0.3)
        self.assertEqual(_ops(pkts)[:2], [0x8A, 0x99])
        return _ops(pkts)[2:]

    def test_the_hop_skips_the_event_tail(self):
        a = F.FakeClient(self.server)
        self.clients.append(a)
        key = a.login('test', 'test')['session_key']
        self.assertIn(0x15, _ops(a.enter_world('TestHero')))
        self.assertEqual(self.tail(a), [0x15, 0x99, 0x80])                       # announce, gift, popup
        self.server.channels.note_version_fetch(IP)
        a.close()
        n = F.FakeClient(self.server, channel=2)
        self.clients.append(n)
        self.assertEqual(n.login('test', 'test', session_key=key)['result'], 1)
        n.send_c2s(ENTER_RELOGIN, {'p2p_ip': '127.0.0.1', 'p2p_udp_port': F.P2P_PORT_BASE, 'char_name': 'TestHero'})
        self.assertTrue(n.wait_session(lambda s: s.get('in_world')))
        self.assertNotIn(0x15, _ops(n.recv_until_quiet(0.3)))
        self.assertEqual(self.tail(n), [])                                      # nothing once per login
        self.assertEqual(self.server.events.tick(), 0)                          # nor from the tick
        # a plain relog (a quit, then a fresh login) with quiet 0 repeats the line and the popup
        self.server.channels._fetches.clear()
        n.close()
        self.assertTrue(_wait(lambda: n.session is None))
        self.server.ticks.run_due(time.monotonic() + 31)
        c = F.FakeClient(self.server)
        self.clients.append(c)
        self.assertEqual(c.login('test', 'test')['result'], 1)
        self.assertIn(0x15, _ops(c.enter_world('TestHero')))
        self.assertEqual(self.tail(c), [0x15, 0x80])                            # the gift is claimed

    def test_an_event_that_starts_after_the_hop_is_delivered(self):
        """P12 review: the hop's skip is once per connection - an event that starts later, even
        while that connection is in a map load, reaches it at the load's C2S 0x63."""
        a = F.FakeClient(self.server)
        self.clients.append(a)
        key = a.login('test', 'test')['session_key']
        a.enter_world('TestHero')
        self.tail(a)
        self.server.channels.note_version_fetch(IP)
        a.close()
        n = F.FakeClient(self.server, channel=2)
        self.clients.append(n)
        self.assertEqual(n.login('test', 'test', session_key=key)['result'], 1)
        n.send_c2s(ENTER_RELOGIN, {'p2p_ip': '127.0.0.1', 'p2p_udp_port': F.P2P_PORT_BASE, 'char_name': 'TestHero'})
        self.assertTrue(n.wait_session(lambda s: s.get('in_world')))
        n.recv_until_quiet(0.3)
        self.assertEqual(self.tail(n), [])                                      # the hop: nothing again
        # a portal: the load's hook suspends the events; LATE starts during the load
        s = n.session
        self.server._map_transfer(s['sock'], s, s['current_map'], *s['pos'], reason='test portal')
        n.recv_until_quiet(0.3)
        with self.server.events.lock:
            self.server.events.overrides[LATE['id']] = (True, None)
        self.assertEqual(self.server.events.tick(), 0)                          # not ready: mid-load
        self.assertEqual(self.later_tail(n), [0x15, 0x80])                      # its line and popup now
        self.assertEqual(self.later_tail(n), [])                                # once

    def later_tail(self, c):
        """A later map load's C2S 0x63 reply (0x8A; no 0x99 sub 8 after the first): the event
        effects."""
        c.send(0x63)
        ops = _ops(c.recv_until_quiet(0.3))
        self.assertEqual(ops[:1], [0x8A])
        return [op for op in ops[1:] if op != 0x99]


# =========================================================== arch09-channel-key ===
class ChannelKey(unittest.TestCase):
    def test_the_boss_ledger_keys_by_the_shared_world_channel(self):
        if not HAVE[B9]:
            self.skipTest('needs the EN 2009 client data')
        tmp = tempfile.mkdtemp(prefix='ws_chkey_')
        try:
            EC.configure(DIRS[B9], B9)
            server = F.make_server(tmp, config=config(B9, BOSS_LEDGER_FILE=''))
            # every channel shares channel 1's map instances until P18 ch-2: a kill seen from
            # channel 2 books the one shared boss under (1, map, tile)
            self.assertEqual(server.bosses.channel_of(None), 1)
            self.assertEqual(server.channels.world_channel(), 1)
            server.config['CHANNELS'] = [{'no': 3, 'port': 7024}]
            self.assertEqual(server.bosses.channel_of(None), 3)
            self.assertEqual(CONT.Continuity(server).departures, {})                # keyed by uid: global
        finally:
            EC.configure(DIRS[B8], B8)
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main(verbosity=1)
