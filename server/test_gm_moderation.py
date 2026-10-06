#!/usr/bin/env python3
"""
test_gm_moderation.py - P6 stage 4 (GM moderation), offline, both client builds
===============================================================================
chat_mail_gm-gm-go, chat_mail_gm-gm-manner, chat_mail_gm-gm-stop-kick,
quest_cards_misc-admin-kick-maintenance (docs/systems/chat_mail_gm.md 1.5 / F10 / F11,
quest_cards_misc.md; spec 0x53 / 0x97 / 0x5D / 0x3A / 0x17 / 0x02; C26; roadmap P6 exit
criterion 8):

- /go (C2S 0x06 sub 0x08) and `!go`: the GM reloads next to the target - same map (a full
  reload in place: 0x08 0x03 0x07, the target's client sees 0x06 then 0x05) or another map;
  an offline name answers S2C 0x53; oneself is refused;
- /manner (sub 0x0A): S2C 0x97 {uid, delta} to the target AND to every client holding it,
  the account persisted, the target's Player Info (S2C 0x52) shows the new manner;
- /kick (sub 0x0C): S2C 0x5D with the build's kick text (2008 0, 2009 4), never 0x17 first,
  the socket closed KICK_CLOSE_SECS later;
- /stop N (sub 0x07) and `!stop`: 0x15 + S2C 0x3A to every in-world client, logins locked
  (S2C 0x02 result 0x0E, version code 0xEA61), the save at +160 s and the close at +180 s;
  /stop 0 does nothing; `!maintenance off` cancels and unlocks;
- the admin port: {"kick": ...} (0x5D, save, close after 1 s), {"shutdown": 1},
  {"maintenance": 0|1}.

Two fake clients (MultiClient: TestHero test/test uid 1 = client 1, the GM; Watcher
admin/admin uid 2 = client 2), both on map 101. No port is bound, no client is started and
the live accounts.json is never opened (temp copies; its hash is checked at the end).
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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import fakeclient as F  # noqa: E402
import gm  # noqa: E402
import packets as P  # noqa: E402

W = F.import_server()
cfgmod = W.cfgmod
EC = W.EC
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = cfgmod.BUILD_2008, cfgmod.BUILD_2009
DIRS = {B8: cfgmod.DEFAULTS['CLIENT_DIR'], B9: cfgmod.DEFAULTS['CLIENT_DIR_2009']}
HAVE = {b: os.path.exists(os.path.join(HERE, d, 'hs', 'windslayer.hii')) for b, d in DIRS.items()}
PORTAL_101_TO_102 = bytes.fromhex('17000000')

_LIVE_HASH = None


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def setUpModule():
    global _LIVE_HASH
    _LIVE_HASH = _sha(LIVE_ACCOUNTS) if os.path.exists(LIVE_ACCOUNTS) else None
    P.STRICT_FIELDS.add(B9)


def tearDownModule():
    P.STRICT_FIELDS.discard(B9)
    EC.configure(DIRS[B8], B8)
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_gm_moderation.py (tests must only use temp copies)'


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _ops(pkts):
    return [p.opcode for p in pkts]


def _text(c, pkt):
    return P.to_bytes(c.s2c(pkt)['text']).decode('cp949')


class Rules(unittest.TestCase):
    def test_kick_reasons_and_the_0x17_latch_builder(self):
        """2009 has the admin kick text (reason 4); 2008 shows its generic disconnect text for
        0. S2C 0x17 stays buildable (0 B) for a future transfer flow - never before a kick."""
        self.assertEqual((gm.kick_reason(B8), gm.kick_reason(B9)), (0, 4))
        for build in (B8, B9):
            self.assertEqual(P.build(gm.TRANSFER_LATCH_KEY, {}, client_build=build), b'')
            self.assertEqual(P.build('0x5D', {'reason': gm.kick_reason(build)}, client_build=build),
                             bytes([gm.kick_reason(build)]))
            self.assertEqual(P.build('0x3A', {}, client_build=build), b'')
        self.assertLess(gm.MAINTENANCE_SAVE_SECS, 170.0)            # before the clients' own exit (C26)
        self.assertGreaterEqual(gm.MAINTENANCE_CLOSE_SECS, 170.0)

    def test_go_and_stop_are_handled_in_both_builds(self):
        for build in (B8, B9):
            table = gm.subcommands(build)
            self.assertTrue(callable(getattr(W.GameServer, f'_gm_sub_{table[0x08].name}')))
            self.assertTrue(callable(getattr(W.GameServer, f'_gm_sub_{table[0x07].name}')))
        self.assertEqual(gm.check_commands(W.GameServer.DEV_COMMANDS, W.GameServer, gm.COMMANDS), [])


class _Base:
    build = B8

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        self.tmp = tempfile.mkdtemp(prefix=f'ws_gmmod_{self.build}_')
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False})
        accounts = F.two_player_accounts()
        accounts['test']['characters'][0]['gm'] = 1
        self.server = F.make_server(self.tmp, accounts=accounts, config=cfg)
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

    # ------------------------------------------------------------- helpers ---
    def sub(self, c, code, **fields):
        """One C2S 0x06 GM command through the build's own send site for that sub byte."""
        c.send_c2s(gm.subcommands(self.build)[code].key, {'gm_subcmd': code, **fields})

    def key(self, op, name=''):
        for v in P.variants(op, 'C2S', client_build=self.build):
            if name in v['name']:
                return v['key']
        raise KeyError(op)

    def line(self, c, text):
        c.send_c2s(self.key(0x03), {'msg_len': len(text), 'message': text})

    def portal(self, c):
        c.send(0x7E, PORTAL_101_TO_102)
        self.assertTrue(_wait(lambda: self.server.world.map_of(c.session) == 102))
        return c.recv_until_quiet()

    def account(self, name):
        return self.server.store.account(name)

    def audit(self):
        path = self.server.gm_audit_path
        if not os.path.exists(path):
            return []
        with open(path, encoding='utf-8') as f:
            return f.read().splitlines()

    def later(self, secs):
        """Run the scheduler as if `secs` had passed (the tick thread never runs offline)."""
        self.server.ticks.run_due(time.monotonic() + secs)

    def closed(self, c):
        return _wait(lambda: c.session is None or c.session.get('kicked') or c.session.get('closed'))


# ===================================================================== /go ===
class _Go(_Base):
    def test_exit8_go_lands_next_to_the_target_on_the_same_map(self):
        """/go Watcher on 101: a full reload in place next to B (x + 40, on the floor there -
        livetest bug 5: no longer 30 px above it); B's client sees A leave (0x06) and arrive
        (0x05)."""
        a, b = self.a, self.b
        code, x, y = self.server._go_point(b.session)
        fx, fy = W.presence.floor_point(b.session)
        self.assertEqual((code, x), (101, fx + gm.GO_OFFSET_PX))
        # ON the floor line: the map load's own settle keeps it where it is
        self.assertEqual(W.presence.settle(code, x, y, W.presence.SLOPE_SLACK_PX), (x, y))
        self.sub(a, 0x08, target_name='Watcher')
        pkts = a.recv_until_quiet(0.4)
        ops = _ops(pkts)
        self.assertEqual(ops[0], 0x15)
        self.assertIn('Going to Watcher on map 101', _text(a, pkts[0]))
        self.assertEqual(ops[1:4], [0x08, 0x03, 0x07])
        self.assertIn(0x04, ops)                               # B's record on the reloaded map
        self.assertEqual(a.session['pos'], (x, y))
        self.assertEqual(self.server.world.map_of(a.session), 101)
        got = b.recv_until_quiet(0.3)
        self.assertEqual(_ops(got), [0x06, 0x05])
        self.assertEqual(b.s2c(got[0]), {'uid': 1})
        self.assertTrue(any('0x06/go\tWatcher' in line for line in self.audit()))

    def test_go_follows_the_target_to_another_map_and_the_dev_command_is_the_same_path(self):
        a, b = self.a, self.b
        self.portal(b)
        self.mc.drain()
        self.line(a, b'!go watcher')                            # any case resolves
        pkts = a.recv_until_quiet(0.4)
        self.assertIn(0x08, _ops(pkts))
        self.assertEqual(self.server.world.map_of(a.session), 102)
        code, x, y = self.server._go_point(b.session)
        self.assertEqual((code, a.session['pos']), (102, (x, y)))
        self.assertEqual(_ops(b.recv_until_quiet(0.3)), [0x05])  # A arrives on B's map

    def test_go_refusals(self):
        a, b = self.a, self.b
        self.sub(a, 0x08, target_name='Nobody')
        self.assertEqual(P.to_bytes(a.s2c(a.expect(0x53))['char_name']).rstrip(b'\0'), b'Nobody')
        self.sub(a, 0x08, target_name='TestHero')
        self.assertEqual(_text(a, a.expect(0x15)), '[Warning] You are already there.')
        # a non-GM's 0x06 is dropped (F10.0 step 4)
        self.sub(b, 0x08, target_name='TestHero')
        b.expect_silence(0.15)
        a.expect_silence(0.05)
        self.assertEqual(self.server.world.map_of(b.session), 101)


# ================================================================= /manner ===
class _Manner(_Base):
    def test_exit8_manner_reaches_the_target_its_holders_and_the_player_info(self):
        """/manner Watcher -50: 0x97 {2, -50} to B (its Status Manner, red name, chat blocked
        at <= -40) and to A, whose copy of B carries the same value; the account keeps -50;
        A's Char. Info on B (another map: S2C 0x52 from the server) shows it."""
        a, b = self.a, self.b
        self.sub(a, 0x0A, target_name='Watcher', manner_delta=-50)
        want = {'uid': 2, 'manner_delta': -50}
        self.assertEqual(b.s2c(b.expect(0x97)), want)
        held, line = a.expect(0x97, 0x15)
        self.assertEqual(a.s2c(held), want)
        self.assertEqual(_text(a, line), 'Manner Watcher: -50 (now -50)')
        self.assertEqual(self.account('admin')['manner'], -50)
        self.portal(b)
        self.mc.drain()
        a.send_c2s(self.key(0x2A, 'CharacterInfoRequest'), {'target_name': 'Watcher'})
        info = a.s2c(a.expect(0x52))
        self.assertEqual(info['manner_points'], -50)
        # an unknown name: 0x53 to the GM, nothing stored
        self.sub(a, 0x0A, target_name='Nobody', manner_delta=5)
        a.expect(0x53)
        self.assertEqual(self.account('admin')['manner'], -50)
        # B on 102 is held by nobody on 101: only B gets the next 0x97
        self.sub(a, 0x0A, target_name='Watcher', manner_delta=50)
        self.assertEqual(_text(a, a.expect(0x15)), 'Manner Watcher: +50 (now 0)')
        self.assertEqual(b.s2c(b.expect(0x97)), {'uid': 2, 'manner_delta': 50})


# =================================================================== /kick ===
class _Kick(_Base):
    def test_exit8_kick_shows_the_kick_dialog_then_drops_the_socket(self):
        a, b = self.a, self.b
        slot = self.server._slot_of(b.session)
        self.sub(a, 0x0C, arg=slot)
        pkt = b.expect(0x5D)                                   # the only packet: never 0x17 first
        self.assertEqual(b.s2c(pkt), {'reason': gm.kick_reason(self.build)})
        self.assertEqual(_text(a, a.expect(0x15)), 'Kicked Watcher')
        self.assertFalse(b.session.get('kicked'))              # the client shows the dialog first
        self.later(gm.KICK_CLOSE_SECS + 0.5)
        self.assertTrue(self.closed(b))
        self.assertEqual(a.s2c(a.expect(0x06)), {'uid': 2})

    def test_admin_kick_saves_and_closes_after_one_second(self):
        a, b = self.a, self.b
        with self.server._combat_lock(b.session):
            b.session['pos'] = (1500.0, 812.0)
        reply = self.server._admin_command(json.dumps({'kick': 'Watcher'}))
        self.assertTrue(reply.startswith('ok kick admin/Watcher'), reply)
        self.assertEqual(b.s2c(b.expect(0x5D)), {'reason': gm.kick_reason(self.build)})
        ch = self.server.store.character_by_name('Watcher')[2]
        self.assertEqual((ch['x'], ch['y']), (1500.0, 812.0))   # saved before the close
        self.later(gm.ADMIN_KICK_CLOSE_SECS + 0.2)
        self.assertTrue(self.closed(b))
        a.expect(0x06)
        self.assertTrue(self.server._admin_command(json.dumps({'kick': 'nobody'})).startswith('error'))
        self.server._admin_command(json.dumps({'kick': 1, 'reason': 1}))
        self.assertEqual(a.s2c(a.expect(0x5D)), {'reason': 1})


# =================================================================== /stop ===
class _Stop(_Base):
    def test_stop_warns_every_client_locks_logins_saves_then_closes(self):
        a, b = self.a, self.b
        self.sub(a, 0x07, arg=0)                               # "/stop x": atol gives 0
        self.assertIn('/stop needs a number', _text(a, a.expect(0x15)))
        b.expect_silence(0.05)
        self.assertEqual(self.server.maintenance_state(), 'off')
        self.sub(a, 0x07, arg=3)
        for c in (a, b):
            pkts = c.recv_until_quiet(0.3)
            self.assertEqual(_ops(pkts)[:2], [0x15, 0x3A], c.char_name)
            self.assertEqual(_text(c, pkts[0]), f'[Announce] {gm.MAINTENANCE_NOTICE}')
        self.assertEqual(self.server.maintenance_state(), 'countdown')
        self.assertTrue(any('0x06/stop\tN=3' in line for line in self.audit()))
        # logins are locked: the game server's 0x02 and the version server's code
        c = F.FakeClient(self.server)
        self.extra.append(c)
        self.assertEqual(c.login('admin', 'admin')['result'], 0x0E)
        body = W.VersionServer(config=self.server.config)._build_version_body()
        self.assertEqual(struct.unpack_from('<H', body, 1)[0], cfgmod.VERSION_MAINTENANCE)
        # a second /stop does not restart the countdown
        self.sub(a, 0x07, arg=1)
        self.assertIn('already running', _text(a, a.expect(0x15)))
        # the save before the clients' own exit, then the close
        with self.server._combat_lock(b.session):
            b.session['pos'] = (1600.0, 812.0)
        self.later(gm.MAINTENANCE_SAVE_SECS + 0.1)
        self.assertEqual(self.server.store.character_by_name('Watcher')[2]['x'], 1600.0)
        self.assertFalse(b.session.get('kicked'))
        self.later(gm.MAINTENANCE_CLOSE_SECS + 0.1)
        self.assertTrue(self.closed(a) and self.closed(b))
        self.assertEqual(self.server.maintenance_state(), 'locked')   # until an admin lifts it
        self.assertEqual(self.server._admin_command(json.dumps({'maintenance': 0})), 'ok maintenance off')

    def test_the_dev_stop_and_the_admin_lift_cancel_it(self):
        a, b = self.a, self.b
        self.line(a, b'!stop 2')
        for c in (a, b):
            self.assertEqual(_ops(c.recv_until_quiet(0.3))[:2], [0x15, 0x3A])
        self.line(a, b'!maintenance off')
        self.assertIn('countdown cancelled', _text(a, a.expect(0x15)))
        self.later(gm.MAINTENANCE_CLOSE_SECS + 1)
        self.assertFalse(a.session.get('kicked') or b.session.get('kicked'))
        c = F.FakeClient(self.server)
        self.extra.append(c)
        self.server._admin_command(json.dumps({'maintenance': 1}))
        self.assertEqual(self.server.maintenance_state(), 'locked')
        self.assertEqual(c.login('admin', 'admin')['result'], 0x0E)
        # the admin shutdown is /stop's path
        reply = self.server._admin_command(json.dumps({'shutdown': 1}))
        self.assertTrue(reply.startswith('ok shutdown: 0x3A sent'), reply)
        for c2 in (a, b):
            self.assertEqual(_ops(c2.recv_until_quiet(0.3))[:2], [0x15, 0x3A])
        self.assertEqual(self.server._admin_command(json.dumps({'maintenance': 0})), 'ok maintenance off')
        self.later(gm.MAINTENANCE_CLOSE_SECS + 1)
        self.assertFalse(a.session.get('kicked'))


def _builds(cls):
    out = {}
    for build in (B8, B9):
        name = f'{cls.__name__.lstrip("_")}{build}'
        out[name] = type(name, (cls, unittest.TestCase), {'build': build})
    return out


for _cls in (_Go, _Manner, _Kick, _Stop):
    globals().update(_builds(_cls))


if __name__ == '__main__':
    unittest.main(verbosity=1)
