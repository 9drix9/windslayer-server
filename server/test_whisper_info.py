#!/usr/bin/env python3
"""
test_whisper_info.py - P6 stage 1 (chat-whisper-playerinfo), offline, both client builds
=======================================================================================
Two fake clients on one server - TestHero (test/test, uid 1, client 1 =
WindSlayer_patched.exe) and Watcher (admin/admin, uid 2, client 2 = WindSlayer_p2.exe), both
on map 101 - for the 2008 and the 2009 build, plus an offline character 'Late':

- chat_mail_gm-whisper (F2): C2S 0x02 -> S2C 0x0A {0x65, sender, text} on the target and
  S2C 0x09 {0x65, target, text} on the sender, on any map, name case-insensitive with the
  canonical name in the echo; 0x09 0x66 (18 B) for an offline or unknown name and for a GM
  in shadow; 0x09 0x67 (18 B) when the target refuses whispers (C2S 0x40), a GM exempt;
  a 0x15 warning for the own name; drops (no reply) for an empty name/text, manner <= -20
  and the 11th whisper in 5 s; the shared text clamp;
- chat_mail_gm-room-builders (F5): S2C 0x0F subtype 1 with the receiver first (C52), the
  room-id-only restore form, the six failure subtypes and nothing else, S2C 0x61 with the
  room assume, NUL cut and 58-byte clamp;
- chat_mail_gm-friend-chat-relay (F3): C2S 0x6B -> S2C 0x91 to the online STORED friends
  among the client's list only, real-name prefix (anti-spoof), 87-byte clamp, never back
  to the sender (registry.NEVER_REPLY);
- lc-player-info (F9): C2S 0x2A -> S2C 0x52 with the target's real record (name, manner,
  level, fame rank, class, STR/DEX/INT/SPR, 25 grid entries with their option words; 2009
  guild_id 0 = "N/A") or S2C 0x53 for an offline / unknown / shadowed name; the on-map
  0x04/0x05 record carries the same values (the client's local Char. Info path); the fame
  rank table against both client exes.

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
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import fakeclient as F  # noqa: E402
import chat  # noqa: E402
import packets as P  # noqa: E402
import presence as PR  # noqa: E402
import progression  # noqa: E402
import registry  # noqa: E402

W = F.import_server()
cfgmod = W.cfgmod
EC = W.EC
R = W.R
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = cfgmod.BUILD_2008, cfgmod.BUILD_2009
DIRS = {B8: cfgmod.DEFAULTS['CLIENT_DIR'], B9: cfgmod.DEFAULTS['CLIENT_DIR_2009']}
HAVE = {b: os.path.exists(os.path.join(HERE, d, 'hs', 'windslayer.hii')) for b, d in DIRS.items()}

RE_TOOLS = os.path.join(os.path.dirname(os.path.dirname(HERE)), 'Windslayer 2', 're_tools')
EXES = {B8: (os.path.join(RE_TOOLS, 'WindSlayer.exe'), 0x6F11D8, 0x70BDC8),
        B9: (os.path.join(RE_TOOLS, 'corpus_2009', 'WindSlayer_2009.exe'), 0x525B70, 0x54A848)}

STICK = 179                                     # Wooden Stick (Kind 11)
PORTAL_101_TO_102 = bytes.fromhex('17000000')   # live 0x7E capture: 101 line 23
WHISPER_ECHO = 0x65

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
    EC.configure(DIRS[B8], B8)              # a 2009 server may have left en_content on 2009
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_whisper_info.py (tests must only use temp copies)'


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _name(value):
    return P.cut_text(value, 16).decode('cp949')


def _text(value):
    return P.to_bytes(value).decode('cp949')


def read_exe(path, va, n):
    """n bytes at virtual address `va` of a PE file (its section table), or None."""
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as f:
        data = f.read()
    pe = struct.unpack_from('<I', data, 0x3C)[0]
    sections = struct.unpack_from('<H', data, pe + 6)[0]
    opt_size = struct.unpack_from('<H', data, pe + 20)[0]
    base = struct.unpack_from('<I', data, pe + 24 + 28)[0]
    table = pe + 24 + opt_size
    for i in range(sections):
        _, vsize, vaddr, rsize, raddr = struct.unpack_from('<8sIIII', data, table + 40 * i)
        rva = va - base
        if vaddr <= rva < vaddr + max(vsize, rsize):
            off = raddr + rva - vaddr
            return data[off:off + n]
    return None


# ------------------------------------------------------------------ pure rules ---
class Rules(unittest.TestCase):
    """chat.py / records.py / progression.py without a server."""

    def test_whisper_clamp_is_the_tighter_of_both_packets(self):
        self.assertEqual(chat.whisper_limit('A' * 16, 'B' * 16), 60)
        self.assertEqual(chat.whisper_limit(b'A' * 30, b'B'), 60 - 0)          # names cut to 16
        self.assertEqual(chat.whisper_text('TestHero', 'Watcher', b'x' * 90), b'x' * 60)
        self.assertEqual(chat.whisper_text('TestHero', 'Watcher', b'hi\x00junk'), b'hi')
        # the formula the design gives (F2 step 5) for a longer-named build
        with mock.patch.object(chat, 'NAME_MAX', 30):
            self.assertEqual(chat.whisper_limit('S' * 25, 'T' * 10), 78 - 25)
            self.assertEqual(chat.whisper_limit('S' * 5, 'T' * 25), 80 - 25)

    def test_whisper_failures_are_18_bytes_and_only_66_67(self):
        for status in (chat.WHISPER_NOT_FOUND, chat.WHISPER_REFUSED):
            for build in (B8, B9):
                raw = P.build('0x09', chat.whisper_failed(status, 'Bob'), client_build=build)
                self.assertEqual(len(raw), 18)
        with self.assertRaises(ValueError):
            chat.whisper_failed(WHISPER_ECHO, 'Bob')

    def test_friend_line_anti_spoof_and_clamp(self):
        self.assertEqual(chat.friend_line('TestHero', b'TestHero : hi'), b'TestHero : hi')
        self.assertEqual(chat.friend_line('TestHero', b'Watcher : hi'), b'TestHero : hi')
        self.assertEqual(chat.friend_line('TestHero', b'hi'), b'TestHero : hi')
        self.assertEqual(chat.friend_line('TestHero', b'GM : a : b'), b'TestHero : a : b')
        self.assertEqual(chat.friend_line('TestHero', b'TestHero : hi\x00junk'), b'TestHero : hi')
        self.assertEqual(len(chat.friend_line('TestHero', b'TestHero : ' + b'x' * 100)), 87)
        self.assertEqual(chat.stored_friends({'friends': ['Watcher', {'name': 'Late'}, '', None]}),
                         {'watcher', 'late'})
        self.assertEqual(chat.stored_friends({}), set())

    def test_room_builders_refuse_what_the_client_cannot_show(self):
        fields = chat.room_joined(7, ['TestHero', 'Watcher'], receiver='Watcher')
        self.assertEqual([e['member_name'] for e in fields['repeat[member_count]']], [b'Watcher', b'TestHero'])
        self.assertEqual(chat.room_joined(7, ['A', 'B', 'C'], receiver='C')['repeat[member_count]'][0],
                         {'member_name': b'C'})
        for bad in (0, 1, 7, 9, 255):
            with self.subTest(subtype=bad), self.assertRaises(ValueError):
                chat.room_refused(bad, 'Bob')
        for good in (2, 3, 4, 5, 6, 8):
            for build in (B8, B9):
                self.assertEqual(len(P.build('0x0F', chat.room_refused(good, 'Bob'), client_build=build)), 18)
        with self.assertRaises(ValueError):
            chat.room_joined(0, ['A'])                                  # 0 = "no room"
        with self.assertRaises(ValueError):
            chat.room_joined(1, [f'P{i}' for i in range(chat.ROOM_MAX + 1)])
        with self.assertRaises(ValueError):
            chat.room_joined(1, ['A', ''])
        fields, assume = chat.room_line('TestHero', 'x' * 70)
        self.assertEqual(len(fields['message']), 58)
        for build in (B8, B9):
            self.assertEqual(len(P.build('0x61', fields, assume, client_build=build)), 17 + 1 + 58)
            # the gate is why the assume exists: a receiver in no room reads nothing (B10)
            self.assertEqual(P.build('0x61', fields, {'client.messenger_room_id != 0': False},
                                     client_build=build), b'')

    def test_info_entries_keep_the_stored_option_block(self):
        e = R.info_entry(STICK, [1081, 0, 1086, 0, 0, 7])
        self.assertEqual((e['item_id'], e['option_count'], e['option_last']), (STICK, 3, 7))
        self.assertEqual([o['option'] for o in e['repeat[option_count]']], [1081, 0, 1086])
        self.assertEqual(R.info_entry(0, None),
                         {'item_id': 0, 'option_count': 0, 'repeat[option_count]': [], 'option_last': 0})
        self.assertEqual(R.info_entry(STICK, [0, 0, 0, 0, 0, 9])['option_count'], 0)
        self.assertEqual(R.info_entry(STICK, [1, 2, 3, 4, 5, 6])['option_count'], 5)

    def test_info_equipment_is_the_25_slot_grid_of_each_build(self):
        char = {'equipped': {'4': {'id': STICK, 'w': [5, 0, 0, 0, 0, 0]}, '15': 1234, '16': 3000},
                'cash_equip': [0, 3001]}
        rows8 = R.info_equipment({}, char, B8)
        rows9 = R.info_equipment({}, char, B9)
        self.assertEqual(len(rows8), R.INFO_EQUIP_ENTRIES)
        self.assertEqual(len(rows9), R.INFO_EQUIP_ENTRIES)
        self.assertEqual(rows8[4], (STICK, [5, 0, 0, 0, 0, 0]))
        self.assertEqual(rows8[15][0], 1234)                 # 2008: slot 15 is a regular slot
        self.assertEqual(rows9[15][0], 0)                    # 2009: the pet slot (no pets)
        self.assertEqual((rows8[16][0], rows8[17][0]), (3000, 3001))   # grid, else legacy cash list
        self.assertEqual((rows9[16][0], rows9[17][0]), (3000, 3001))

    def test_fame_rank_is_the_client_loop(self):
        self.assertEqual(progression.rank_for_fame(0), 0)                  # 'Trainee'
        self.assertEqual(progression.rank_for_fame(10), 0)                 # retail: Reputation 10, Trainee
        self.assertEqual(progression.rank_for_fame(29), 0)
        self.assertEqual(progression.rank_for_fame(30), 1)
        self.assertEqual(progression.rank_for_fame(16172929), 97)
        self.assertEqual(progression.rank_for_fame(16172930), 98)
        self.assertEqual(progression.rank_for_fame(-1), 98)                # a u32 in the client
        self.assertEqual(R.rank_of({'fame': 100}), 2)
        self.assertEqual(R.rank_of({}), 0)

    def test_fame_rank_table_is_the_exe_table_in_both_builds(self):
        for build, (path, table_va, names_va) in EXES.items():
            with self.subTest(build=build):
                raw = read_exe(path, table_va, 99 * 4)
                if raw is None:
                    self.skipTest(f'{path} not available')
                self.assertEqual(struct.unpack('<99I', raw), progression.FAME_RANK)
                names = read_exe(path, names_va, 99 * 20)
                label = [names[i * 20:(i + 1) * 20].split(b'\x00', 1)[0] for i in (0, 1, 98)]
                self.assertEqual(label, [b'Trainee', b'Trainee Lv.1', b'Windslayer'])

    def test_routes_and_policy(self):
        for routes in (W.GameServer.ROUTES, W.GameServer.ROUTES_2009):
            self.assertEqual(routes[0x02].handler, '_handle_whisper')
            self.assertEqual(routes[0x6B].handler, '_handle_friend_chat')
            self.assertEqual(routes[0x2A].handler, '_handle_player_info')
        for build in (B8, B9):
            self.assertIn(0x6B, registry.never_reply_table(build))
            self.assertNotIn(0x02, registry.must_reply_table(build))
            self.assertNotIn(0x2A, registry.must_reply_table(build))


# ------------------------------------------------------------- two clients ---
class _TwoPlayers:
    """TestHero (uid 1) and Watcher (uid 2) in world on map 101 of one server of `build`."""
    build = B8

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        self.tmp = tempfile.mkdtemp(prefix=f'ws_whisper_{self.build}_')
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False})
        accounts = F.two_player_accounts()
        # an offline character: known to the store, never logged in
        accounts['late'] = {'password': 'late', 'characters': [
            {'name': 'Late', 'level': 1, 'class': 0, 'map': 101, 'x': 1411.0, 'y': 714.0, 'hp': 100, 'mp': 50}]}
        self.server = F.make_server(self.tmp, accounts=accounts, config=cfg)
        self.mc = F.MultiClient(self.server)
        self.a, self.b = self.mc
        self.mc.drain()

    def tearDown(self):
        self.mc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ----------------------------------------------------------------- helpers ---
    def key(self, op, index=0):
        return P.variants(op, 'C2S', client_build=self.build)[index]['key']

    def whisper(self, c, target, text):
        target, text = P.to_bytes(target), P.to_bytes(text)
        c.send_c2s(self.key(0x02), {'target_name': target, 'msg_len': len(text), 'message': text})

    def friend_chat(self, c, uids, text):
        text = P.to_bytes(text)
        c.send_c2s(self.key(0x6B), {'recipient_count': len(uids),
                                    'repeat[recipient_count]': [{'friend_channel': 1, 'friend_id': u} for u in uids],
                                    'msg_len': len(text), 'message': text})

    def info(self, c, name):
        c.send_c2s(self.key(0x2A), {'target_name': name})

    def char(self, user):
        c = self.a if user == 'test' else self.b
        return self.server._session_char(c.session)

    def portal(self, c):
        c.send(0x7E, PORTAL_101_TO_102)
        self.assertTrue(_wait(lambda: self.server.world.map_of(c.session) == 102))
        c.recv_until_quiet()
        self.mc.drain()

    def privacy(self, c, **flags):
        fields = {f'refuse_{k}': int(bool(flags.get(k))) for k in ('whisper', 'exchange', 'party', 'talk', 'friend')}
        c.send_c2s(self.key(0x40), fields)
        want = {k: bool(flags.get(k)) for k in ('whisper', 'exchange', 'party', 'talk', 'friend')}
        self.assertTrue(_wait(lambda: c.session.get('refuse') == want))

    # ----------------------------------------------------------------- whisper ---
    def test_whisper_reaches_the_target_and_echoes_to_the_sender(self):
        """Exit criterion 1: '/w B hello' from A -> '<From: A> hello' on B, '<To: B> hello'
        on A; and the other way round."""
        self.whisper(self.a, 'Watcher', 'hello')
        got = self.b.s2c(self.b.expect(0x0A))
        self.assertEqual((got['channel'], _name(got['sender_name']), _text(got['message'])),
                         (WHISPER_ECHO, 'TestHero', 'hello'))
        echo = self.a.s2c(self.a.expect(0x09))
        self.assertEqual((echo['status'], _name(echo['target_name']), _text(echo['message'])),
                         (WHISPER_ECHO, 'Watcher', 'hello'))
        self.whisper(self.b, 'TestHero', 'hi back')
        self.assertEqual(_text(self.a.s2c(self.a.expect(0x0A))['message']), 'hi back')
        self.assertEqual(_name(self.b.s2c(self.b.expect(0x09))['target_name']), 'TestHero')

    def test_the_name_is_case_insensitive_and_the_echo_canonical(self):
        self.whisper(self.a, 'wAtChEr', 'yo')
        self.b.expect(0x0A)
        self.assertEqual(_name(self.a.s2c(self.a.expect(0x09))['target_name']), 'Watcher')

    def test_offline_and_unknown_names_are_not_found(self):
        """Exit criterion 1: an offline name -> '<name>can not be found.' (0x09 0x66, 18 B)."""
        for name in ('Late', 'Nobody'):
            with self.subTest(name):
                self.whisper(self.a, name, 'anyone?')
                pkt = self.a.expect(0x09)
                self.assertEqual(len(pkt.payload), 18)
                rec = self.a.s2c(pkt)
                self.assertEqual((rec['status'], _name(rec['target_name'])), (chat.WHISPER_NOT_FOUND, name))
                self.b.expect_silence(0.1)

    def test_a_refused_whisper_says_is_rejecting_and_a_gm_passes(self):
        """Exit criterion 1: B ticks refuse-whisper (C2S 0x40) -> A gets '<B>is rejecting
        whispers.' (0x09 0x67, 18 B) and B gets nothing; a GM sender is exempt (F2 step 4)."""
        self.privacy(self.b, whisper=True)
        self.whisper(self.a, 'watcher', 'psst')
        pkt = self.a.expect(0x09)
        self.assertEqual(len(pkt.payload), 18)
        rec = self.a.s2c(pkt)
        self.assertEqual((rec['status'], _name(rec['target_name'])), (chat.WHISPER_REFUSED, 'Watcher'))
        self.b.expect_silence(0.15)
        self.a.session['gm'] = 1
        self.whisper(self.a, 'Watcher', 'gm here')
        self.assertEqual(_text(self.b.s2c(self.b.expect(0x0A))['message']), 'gm here')
        self.assertEqual(self.a.s2c(self.a.expect(0x09))['status'], WHISPER_ECHO)
        self.a.session['gm'] = 0
        self.privacy(self.b)                                      # unticked: delivered again
        self.whisper(self.a, 'Watcher', 'again')
        self.b.expect(0x0A)
        self.assertEqual(self.a.s2c(self.a.expect(0x09))['status'], WHISPER_ECHO)

    def test_a_refusal_sent_at_enter_world_counts_too(self):
        """The same flag from C2S 0x2B bytes 0-4 (the client's registry at login)."""
        self.mc.close()
        self.mc = F.MultiClient(self.server, refuse={'Watcher': {'whisper': True}})
        self.a, self.b = self.mc
        self.mc.drain()
        self.whisper(self.a, 'Watcher', 'psst')
        self.assertEqual(self.a.s2c(self.a.expect(0x09))['status'], chat.WHISPER_REFUSED)

    def test_a_whisper_to_the_own_name_is_a_warning(self):
        self.whisper(self.a, 'testhero', 'me?')
        rec = self.a.s2c(self.a.expect(0x15))
        self.assertEqual((rec['msg_type'], _text(rec['text'])),
                         (2, "[Warning] You can't send a message to yourself."))
        self.b.expect_silence(0.1)

    def test_a_whisper_crosses_maps(self):
        self.portal(self.b)
        self.whisper(self.a, 'Watcher', 'where are you')
        self.assertEqual(_text(self.b.s2c(self.b.expect(0x0A))['message']), 'where are you')
        self.assertEqual(self.a.s2c(self.a.expect(0x09))['status'], WHISPER_ECHO)

    def test_a_shadowed_gm_is_not_found_by_a_player(self):
        self.b.session['gm'], self.b.session['gm_hidden'] = 1, 1
        self.whisper(self.a, 'Watcher', 'hello?')
        self.assertEqual(self.a.s2c(self.a.expect(0x09))['status'], chat.WHISPER_NOT_FOUND)
        self.b.expect_silence(0.1)
        self.a.session['gm'] = 1                                   # another GM sees the shadow
        self.whisper(self.a, 'Watcher', 'hello GM')
        self.b.expect(0x0A)
        self.assertEqual(self.a.s2c(self.a.expect(0x09))['status'], WHISPER_ECHO)

    def test_dropped_whispers_get_no_reply(self):
        """Empty name (the whisper-tab path's zeros) or text, manner <= -20, the 11th in 5 s."""
        self.whisper(self.a, b'', 'nobody')
        self.whisper(self.a, 'Watcher', b'')
        self.a.expect_silence(0.15)
        self.b.expect_silence(0.05)
        with self.server.store.lock:
            self.server.store.accounts['test']['manner'] = -20
        self.whisper(self.a, 'Watcher', 'muted')
        self.a.expect_silence(0.15)
        self.b.expect_silence(0.05)
        with self.server.store.lock:
            self.server.store.accounts['test']['manner'] = -19
        for i in range(chat.WHISPER_RATE_COUNT + 1):
            self.whisper(self.a, 'Watcher', f'w{i}')
        got = [_text(self.b.s2c(p)['message']) for p in self.b.recv_until_quiet(0.3)]
        self.assertEqual(got, [f'w{i}' for i in range(chat.WHISPER_RATE_COUNT)])
        self.assertEqual(len(self.a.recv_until_quiet(0.2)), chat.WHISPER_RATE_COUNT)

    def test_long_whispers_are_clamped_the_same_on_both_ends(self):
        self.whisper(self.a, 'Watcher', 'x' * 80)
        mine, theirs = self.a.s2c(self.a.expect(0x09)), self.b.s2c(self.b.expect(0x0A))
        self.assertEqual(mine['message'], theirs['message'])
        self.assertEqual(len(theirs['message']), 60)

    # --------------------------------------------------------------- friend chat ---
    def test_friend_chat_reaches_stored_friends_that_are_online_only(self):
        """F3: recipients = the client's uids AND the sender's stored friends; the sender
        gets nothing (it printed the line itself)."""
        with self.server.store.lock:
            self.char('test')['friends'] = ['Watcher']
        with self.assertNoLogs('WS', logging.ERROR):                  # no NEVER_REPLY violation
            self.friend_chat(self.a, [2], 'TestHero : hi')
            rec = self.b.s2c(self.b.expect(0x91))
            self.a.expect_silence(0.15)
        self.assertEqual(_text(rec['text']), 'TestHero : hi')
        # a spoofed name is replaced, the uids of an offline account and of the sender skipped
        self.friend_chat(self.a, [3, 1, 2, 2], 'Watcher : fake')
        self.assertEqual(_text(self.b.s2c(self.b.expect(0x91))['text']), 'TestHero : fake')
        self.a.expect_silence(0.1)
        # Watcher is not in the list any more: the client's own list is not trusted
        with self.server.store.lock:
            self.char('test')['friends'] = []
        self.friend_chat(self.a, [2], 'TestHero : hello?')
        self.b.expect_silence(0.15)
        self.a.expect_silence(0.05)

    def test_friend_chat_crosses_maps_and_is_clamped(self):
        with self.server.store.lock:
            self.char('admin')['friends'] = ['TestHero']
        self.portal(self.b)
        self.friend_chat(self.b, [1], 'Watcher : ' + 'y' * 80)
        text = P.to_bytes(self.a.s2c(self.a.expect(0x91))['text'])
        self.assertEqual(len(text), 87)
        self.assertTrue(text.startswith(b'Watcher : yyy'))
        self.b.expect_silence(0.1)

    def test_a_muted_sender_reaches_nobody(self):
        with self.server.store.lock:
            self.char('test')['friends'] = ['Watcher']
            self.server.store.accounts['test']['manner'] = -40
        self.friend_chat(self.a, [2], 'TestHero : muted')
        self.b.expect_silence(0.15)

    # ---------------------------------------------------------- room builders ---
    def test_room_packets_reach_the_members_as_the_client_reads_them(self):
        """F5 display half: 0x0F subtype 1 with the receiver first ('<other> has entered.'),
        the restore form, a refusal popup and a 0x61 room line (read with the room gate)."""
        self.assertTrue(self.server._room_joined(self.b.session, 7, ['TestHero', 'Watcher']))
        self.assertTrue(self.server._room_joined(self.a.session, 7, ['TestHero', 'Watcher']))
        for c, order in ((self.b, ['Watcher', 'TestHero']), (self.a, ['TestHero', 'Watcher'])):
            rec = c.s2c(c.expect(0x0F))
            self.assertEqual((rec['subtype'], rec['room_id'], rec['member_count']), (1, 7, 2))
            self.assertEqual([_name(e['member_name']) for e in rec['repeat[member_count]']], order)
        self.assertTrue(self.server._room_joined(self.a.session, 7, []))
        pkt = self.a.expect(0x0F)
        self.assertEqual(pkt.payload, bytes([1]) + (7).to_bytes(4, 'little') + bytes([0]))
        self.assertTrue(self.server._room_refused(self.a.session, chat.ROOM_REJECTING, 'Watcher'))
        rec = self.a.s2c(self.a.expect(0x0F))
        self.assertEqual((rec['subtype'], _name(rec['target_name'])), (4, 'Watcher'))
        room = {'client.messenger_room_id != 0': True}
        self.assertTrue(self.server._room_line(self.b.session, 'TestHero', b'hi\x00junk'))
        rec = self.b.s2c(self.b.expect(0x61), room)
        self.assertEqual((_name(rec['sender_name']), rec['message']), ('TestHero', b'hi'))
        self.assertTrue(self.server._room_line(self.b.session, 'TestHero', 'z' * 70))
        self.assertEqual(len(self.b.s2c(self.b.expect(0x61), room)['message']), 58)
        self.assertFalse(self.server._room_line(self.b.session, 'TestHero', b'\x00x'))
        self.b.expect_silence(0.1)

    # ------------------------------------------------------------- player info ---
    def dress_watcher(self):
        with self.server.store.lock:
            self.server.store.accounts['admin']['manner'] = -5
            char = self.char('admin')
            char.update({'exp': 177, 'class': 1, 'job2': 1, 'str': 13, 'dex': 4, 'int': 2, 'spr': 6,
                         'fame': 100})
            # the bag model keeps option words 0-4 packed (no zero between them; the 0 gap
            # case is Rules.test_info_entries_keep_the_stored_option_block)
            char['equipped'] = {'4': {'id': STICK, 'w': [1081, 1086, 0, 0, 0, 7]}}
        return char

    def test_char_info_of_a_player_elsewhere_opens_player_info(self):
        """Exit criterion 9 (off-map path): C2S 0x2A -> 0x52 with Name, Lv, Class, Guild N/A,
        STR/INT/DEX/SPR, Rank, Manner and the equipment row; nothing to the target."""
        self.dress_watcher()
        self.portal(self.b)
        self.info(self.a, 'watcher')
        rec = self.a.s2c(self.a.expect(0x52))
        self.assertEqual((_name(rec['char_name']), rec['manner_points'], rec['level'], rec['rank']),
                         ('Watcher', -5, 3, 2))
        self.assertEqual((rec['job1'], rec['job2']), (1, 1))
        self.assertEqual((rec['stat_str'], rec['stat_dex'], rec['stat_int'], rec['stat_spr']), (13, 4, 2, 6))
        self.assertEqual(rec['equip_count'], 25)
        worn = [(e['item_id'], [o['option'] for o in e['repeat[option_count]']], e['option_last'])
                for e in rec['repeat[equip_count]'] if e['item_id']]
        self.assertEqual(worn, [(STICK, [1081, 1086], 7)])
        if self.build == B9:
            self.assertEqual(rec['guild_id'], 0)                        # "N/A"
        else:
            self.assertNotIn('guild_id', rec)                          # 2008: " Not in the Guild"
        self.b.expect_silence(0.1)

    def test_char_info_of_an_offline_or_unknown_name_is_not_in_server(self):
        for name in ('Late', 'Nobody'):
            with self.subTest(name):
                self.info(self.a, name)
                rec = self.a.s2c(self.a.expect(0x53))
                self.assertEqual(_name(rec['char_name']), name)

    def test_char_info_hides_a_shadowed_gm_from_players(self):
        self.b.session['gm'], self.b.session['gm_hidden'] = 1, 1
        self.info(self.a, 'Watcher')
        self.a.expect(0x53)
        self.a.session['gm'] = 1
        self.info(self.a, 'Watcher')
        self.a.expect(0x52)

    def test_char_info_of_a_player_mid_map_load_is_answered(self):
        self.b.session['in_world'] = False                          # between its 0x03 and 0x07
        try:
            self.info(self.a, 'Watcher')
            self.assertEqual(_name(self.a.s2c(self.a.expect(0x52))['char_name']), 'Watcher')
        finally:
            self.b.session['in_world'] = True

    def test_the_on_map_record_carries_what_the_window_shows(self):
        """Exit criterion 9 (on-map path): the client fills window 0x72 from its entity, i.e.
        from B's 0x04/0x05 record - karma, level, rank_icon, job, stats, grid must be the
        0x52 values (a rank_icon of 99 would index past the rank names)."""
        self.dress_watcher()
        row = PR.record_of(self.server, self.b.session)
        info = R.player_info(self.b.session, self.char('admin'), self.server._session_account(self.b.session),
                             self.build)
        self.assertEqual((row['karma'], row['level'], row['rank_icon'], row['job'], row['job2']),
                         (info['manner_points'], info['level'], info['rank'], info['job1'], info['job2']))
        self.assertEqual((row['stat_str'], row['stat_dex'], row['stat_int'], row['stat_tol']),
                         (info['stat_str'], info['stat_dex'], info['stat_int'], info['stat_spr']))
        grid = [e['equip_item_id'] for k, v in row.items() if k.startswith('repeat[') and v
                and isinstance(v[0], dict) and 'equip_item_id' in v[0] for e in v]
        self.assertEqual(grid[4], STICK)
        self.assertLess(row['rank_icon'], R.RANK_ICON_NONE)
        # and the record a newly arriving peer gets says the same
        self.portal(self.b)
        self.b.send(0x7E, bytes.fromhex('1f000000'))                   # back to 101
        self.assertTrue(_wait(lambda: self.server.world.map_of(self.b.session) == 101))
        pkts = self.a.recv_until_quiet()
        rows = [r for p in pkts if p.opcode in (0x04, 0x05)
                for r in (self.a.s2c(p).get('repeat[player_count]') or [self.a.s2c(p)])]
        # 2008 0x05 names the byte rank_emblem (records.KEY_MAP_05), 0x04 / 2009 rank_icon
        self.assertEqual([r.get('rank_icon', r.get('rank_emblem')) for r in rows], [2])


class TwoPlayers2008(_TwoPlayers, unittest.TestCase):
    build = B8


class TwoPlayers2009(_TwoPlayers, unittest.TestCase):
    build = B9


if __name__ == '__main__':
    unittest.main()
