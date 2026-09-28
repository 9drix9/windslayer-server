#!/usr/bin/env python3
"""
test_gm.py - GM identity, C2S 0x06 commands and the '!' dev commands (roadmap P1 stage 4:
chat_mail_gm-gm-flag-manner, chat_mail_gm-gm-dispatch, chat_mail_gm-dev-commands)

Same offline rig as test_handlers.py: the real GameServer._handle_fireway loop over a
socketpair (fakeclient.FakeClient), a fresh temp accounts.json per test, no port bound and
no game client. Every S2C reply is decoded with packets.parse and must consume exactly its
bytes.

What is pinned here
-------------------
- the store carries `gm` / `gm_hidden` per character and `manner` per account, and both
  reach the wire: 0x07 gm_level + gm_hidden (370 B for a GM), 0x07 karma and 0x02
  manner_points (chat_mail_gm.md F10.0, B11/B13);
- C2S 0x06 picks its grammar by payload[0] (1.5) and is dropped for a session without the
  flag (F10.0 step 4), including /업데이트, which cannot be typed without a Korean IME;
- /not broadcasts "[Announce] ..." to every in-world session, /manner moves the target
  account's manner and pushes 0x97, /kick N uses the !who slot numbers;
- the '!' dev-command router: every command, its refusals, and that another group can
  register its own command with gm.register().
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

import en_content as EC  # noqa: E402
import fakeclient as F  # noqa: E402
import gm  # noqa: E402
import packets as P  # noqa: E402
import progression  # noqa: E402
import store as storemod  # noqa: E402
from test_handlers import ServerTest, _wait  # noqa: E402  (the shared login / enter-world rig)

W = F.import_server()
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')

# test/TestHero is the GM, admin/Nova the ordinary second account (a second character on
# the GM's own account would still be the same session, and /manner is per account).
GM_ACCOUNTS = {
    'test': {
        'password': 'test',
        'characters': [{'name': 'TestHero', 'level': 1, 'class': 0, 'map': 0,
                        'x': 100, 'y': 100, 'hp': 100, 'mp': 50, 'gm': 1}],
    },
    'admin': {
        'password': 'admin',
        'characters': [{'name': 'Nova', 'level': 1, 'class': 0, 'map': 101,
                        'x': 1411, 'y': 714, 'hp': 100, 'mp': 50}],
    },
}

_LIVE_HASH = None


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def setUpModule():
    global _LIVE_HASH
    _LIVE_HASH = _sha(LIVE_ACCOUNTS) if os.path.exists(LIVE_ACCOUNTS) else None


def tearDownModule():
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_gm.py (tests must only use temp copies)'


def name17(text):
    return text.encode() + b'\x00' * (17 - len(text))


class GmTest(ServerTest):
    """A server whose test/TestHero is a GM, with that client in world."""
    ACCOUNTS = GM_ACCOUNTS

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_gm_')
        self.server = F.make_server(self.tmp, accounts=json.loads(json.dumps(self.ACCOUNTS)))
        self.clients = []
        self.c = self.client()
        self.login(self.c)
        self.enter_world(self.c)

    def tearDown(self):
        for c in self.clients:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------- helpers ---
    def enter_world(self, c, name='TestHero'):
        """Like ServerTest.enter_world, but a GM's 0x07 is one byte longer: gm_level 1 is
        the only value that makes the client read the gm_hidden bool after it (1.5)."""
        map_code = self._entry_map(c, name)
        c.send_c2s('0x42F904/0x2B', {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907, 'char_name': name})
        pkts = F.expect_entry(c, (0x03, 0x07, 0x15, 0x28, 0x44), self.clients, map_code)
        for pkt in pkts:
            self.s2c(pkt)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world')))
        return pkts

    def second_client(self, user='admin', password='admin', char='Nova'):
        c = self.client()
        self.login(c, user, password)
        self.enter_world(c, char)
        return c

    def gm_line(self, text, client=None):
        """Type one chat line as the GM (C2S 0x03)."""
        (client or self.c).send_c2s('0x445CA7/0x03', {'msg_len': len(text), 'message': text})

    def notices(self, client=None, quiet=0.25):
        """Every S2C 0x15 line that came back, as text."""
        out = []
        for pkt in (client or self.c).recv_until_quiet(quiet):
            self.assertEqual(pkt.opcode, 0x15, f'expected only 0x15, got 0x{pkt.opcode:02X}')
            out.append(self.s2c(pkt)['text'])
        return out

    def audit_lines(self):
        path = self.server.gm_audit_path
        if not os.path.exists(path):
            return []
        with open(path, encoding='utf-8') as f:
            return [line.rstrip('\n') for line in f]

    def account(self, username='test'):
        return self.server.store.account(username)

    def char(self, name='TestHero'):
        return self.server.store.character_by_name(name)[2]


# ============================================================ gm-flag-manner ===
class GmFlagAndManner(GmTest):
    """chat_mail_gm-gm-flag-manner (B11, B13; roadmap F4 schema "GM: gm, gm_hidden")."""

    def test_store_writes_gm_keys_and_keeps_a_hand_set_flag(self):
        acc = self.account()
        self.assertEqual(acc['characters'][0]['gm'], 1)       # hand-set in the fixture: kept
        self.assertEqual(acc['characters'][0]['gm_hidden'], 0)
        self.assertEqual(self.char('Nova')['gm'], 0)          # written, so F10.0 is a 0 -> 1 edit
        # Migration is idempotent: a second pass over the same record changes nothing.
        self.assertEqual(storemod.migrate_character(dict(acc['characters'][0])), [])
        # A string "1" from a hand edit is normalized to an int (the 0x07 field is a u16).
        char = {'name': 'X', 'gm': '1'}
        storemod.migrate_character(char)
        self.assertEqual(char['gm'], 1)

    def test_enter_world_sets_the_session_flag_and_the_0x07_carries_it(self):
        session = self.c.session
        self.assertEqual((session['gm'], session['gm_hidden']), (1, 0))
        # The own 0x07 of a GM is 370 B: u16 gm_level 1 + the bool gm_hidden after it.
        self.c.send_c2s('0x42F904/0x2B', {'p2p_ip': '', 'p2p_udp_port': 42907, 'char_name': 'TestHero'})
        pkt = self.c.expect(0x03, 0x07, 0x28, 0x44)[1]
        rec = self.s2c(pkt, 370)['repeat[player_count]'][0]
        self.assertEqual((rec['gm_level'], rec['gm_hidden']), (1, 0))
        c2 = self.second_client()
        self.assertEqual(c2.session['gm'], 0)

    def test_karma_and_0x02_manner_points_are_the_account_manner(self):
        self.server.store.adjust_manner('test', -30)
        # A second login of an online account is refused with result 4 (lc-uid-online), so
        # the session from setUp logs out first.
        self.c.close()
        self.assertTrue(_wait(lambda: self.server.world.session(1) is None))
        c = self.client()
        rec = self.login(c)                                   # S2C 0x02 before character select
        self.assertEqual(rec['manner_points'], -30)
        c.send_c2s('0x42F904/0x2B', {'p2p_ip': '', 'p2p_udp_port': 42907, 'char_name': 'TestHero'})
        pkts = c.expect(0x03, 0x07, 0x15, 0x28, 0x44)
        self.assertEqual(self.s2c(pkts[1])['repeat[player_count]'][0]['karma'], -30)

    def test_manner_survives_a_relog_and_is_clamped_to_the_i32_field(self):
        self.assertEqual(self.server.store.adjust_manner('test', -50), -50)
        self.c.close()
        self.assertTrue(_wait(lambda: self.server.world.session(1) is None))
        with open(self.server.store.path, encoding='utf-8') as f:     # the debounced save ran
            pass
        self.server.store.flush()
        with open(self.server.store.path, encoding='utf-8') as f:
            self.assertEqual(json.load(f)['test']['manner'], -50)
        self.assertEqual(self.server.store.adjust_manner('test', -0x7FFFFFFF), storemod.MANNER_MIN)
        self.assertIsNone(self.server.store.adjust_manner('nobody', 1))

    def test_chat_is_dropped_below_the_manner_limit(self):
        self.server.store.adjust_manner('test', gm.MANNER_CHAT_LIMIT)    # exactly -40: muted
        self.gm_line(b'hello')
        self.c.expect_silence()
        self.server.store.adjust_manner('test', 1)
        self.gm_line(b'hello')
        self.assertEqual(self.s2c(self.c.expect(0x16))['text'], 'hello')


# ============================================================== gm-dispatch ===
class GmDispatch(GmTest):
    """chat_mail_gm-gm-dispatch: C2S 0x06 decode by payload[0], authorization, audit."""

    # (label, payload, the send site that must decode it)
    SUBCOMMANDS = [
        ('/manner test -50', b'\x0a' + name17('test') + struct.pack('<i', -50), '0x444C52/0x06'),
        ('/업데이트 (sub 0x00)', b'\x00', '0x444C91/0x06'),
        ('/notice hi', b'\x01\x06ice hi', '0x444CFD/0x06'),
        ('/proom 5 -10', b'\x0b' + struct.pack('<IHi', 1, 5, -10), '0x444DE2/0x06'),
        ('/shadow', b'\x02', '0x444C91/0x06'),
        ('/reset a b', b'\x03' + b'a'.ljust(129, b'\x00') + b'b\x00\x00', '0x444F22/0x06'),
        ('/go test', b'\x08' + name17('test'), '0x444FC6/0x06'),
        ('/stop 3', b'\x07\x03', '0x444C91/0x06'),
        ('/kick 5', b'\x0c\x05', '0x444C91/0x06'),
    ]

    def test_every_sub_command_decodes_with_the_grammar_its_byte_0_names(self):
        for label, payload, key in self.SUBCOMMANDS:
            with self.subTest(label):
                self.assertEqual(gm.SUBCOMMANDS[payload[0]].key, key)
                rec = P.parse(key, payload, direction='C2S')      # exact: no trailing byte
                self.assertEqual(rec['gm_subcmd'], payload[0])

    def test_a_non_gm_session_is_dropped_and_logged(self):
        c2 = self.second_client()                                # admin/Nova: no gm flag
        for label, payload, _key in self.SUBCOMMANDS:
            with self.subTest(label), self.assertLogs('WS', logging.WARNING) as cm:
                c2.send(0x06, payload)
                c2.expect_silence(0.15)
                self.assertTrue(any('NON-GM session' in line for line in cm.output))
        self.assertEqual(self.audit_lines(), [])                 # nothing accepted, nothing audited

    def test_unknown_sub_command_and_undecodable_body_are_logged_not_answered(self):
        for payload in (b'\x7f', b'\x0a\x01', b''):
            with self.subTest(payload.hex()), self.assertLogs('WS', logging.WARNING):
                self.c.send(0x06, payload)
                self.c.expect_silence(0.15)

    def test_not_broadcasts_an_announce_line_to_every_in_world_session(self):
        c2 = self.second_client()
        self.c.send(0x06, b'\x01\x06ice hello')                  # the GM typed "/notice hello"
        self.assertEqual(self.notices(), ['[Announce] hello'])
        self.assertEqual(self.notices(c2), ['[Announce] hello'])
        self.c.send(0x06, b'\x01\x03 hi')                        # "/not hi": the space is stripped
        self.assertEqual(self.notices(), ['[Announce] hi'])
        # The u8 length is not trusted (it wraps at 260+ bytes): the text comes from the body.
        self.c.send(0x06, b'\x01\x00' + b'x' * 5)
        self.assertEqual(self.notices(), ['[Announce] xxxxx'])
        self.c.send(0x06, b'\x01\x02   ')                        # only spaces: dropped
        self.c.expect_silence()
        self.assertTrue(any('0x06/notice' in line for line in self.audit_lines()))

    def test_not_text_is_clamped_to_the_client_buffer(self):
        self.c.send(0x06, b'\x01\x00' + b'A' * 200)
        line = self.notices()[0]
        self.assertEqual(len(line), len('[Announce] ') + gm.ANNOUNCE_TEXT_MAX)

    def test_update_reloads_the_content_caches(self):
        # Every content cache lives in en_content now (F7 arch-content-loader), so /update
        # is one reload() call instead of five module globals.
        items, quests, portals = EC.items(), EC.quests(), EC.portals()
        self.c.send(0x06, b'\x00')
        self.assertEqual(self.notices(), ['Content reloaded.'])
        self.assertIsNot(EC.items(), items)
        self.assertIsNot(EC.quests(), quests)
        self.assertIsNot(EC.portals(), portals)
        self.assertEqual(len(EC.items()), len(items))
        self.assertEqual(self.account()['characters'][0]['gm'], 1)   # accounts.json is NOT reloaded

    def test_manner_moves_the_target_account_and_pushes_0x97_to_it(self):
        c2 = self.second_client()
        self.c.send(0x06, b'\x0a' + name17('nova') + struct.pack('<i', -50))   # any case resolves
        # P6 stage 4 (chat_mail_gm-gm-manner): uids are unique, so every client HOLDING Nova -
        # the GM's own, both on 101 - gets the 0x97 too (its copy's name tag / Player Info).
        held, line = self.c.expect(0x97, 0x15)
        self.assertEqual(self.s2c(held, 8), {'uid': P.session_uid(c2.session), 'manner_delta': -50})
        self.assertEqual(self.s2c(line)['text'], 'Manner Nova: -50 (now -50)')
        rec = self.s2c(c2.expect(0x97), 8)
        self.assertEqual((rec['uid'], rec['manner_delta']), (P.session_uid(c2.session), -50))
        self.assertEqual(self.account('admin')['manner'], -50)
        self.assertEqual(self.account('test')['manner'], 0)      # per account, not per GM
        self.assertTrue(any('0x06/manner\tNova -50 -> -50' in line for line in self.audit_lines()))

    def test_manner_for_an_unknown_target_answers_0x53(self):
        self.c.send(0x06, b'\x0a' + name17('ghost') + struct.pack('<i', 5))
        self.assertEqual(self.s2c(self.c.expect(0x53), 17)['char_name'], 'ghost')
        self.assertEqual(self.audit_lines(), [])

    def test_manner_of_an_offline_character_is_stored_without_a_push(self):
        self.c.send(0x06, b'\x0a' + name17('Nova') + struct.pack('<i', 7))
        self.assertEqual(self.notices(), ['Manner Nova: +7 (now 7)'])
        self.assertEqual(self.account('admin')['manner'], 7)

    def test_kick_takes_the_who_slot_number(self):
        c2 = self.second_client()
        slot = self.server._slot_of(c2.session)
        self.c.send(0x06, b'\x0c' + bytes([slot]))
        self.assertEqual(self.s2c(c2.expect(0x5D), 1)['reason'], 0)
        self.assertEqual(self.notices(), ['Kicked Nova'])
        # The socket is closed by a tick, KICK_CLOSE_SECS after the 0x5D, not under the
        # client's feet: it needs those seconds to show the message (F10 sub 0x0C).
        self.assertFalse(c2.session.get('kicked'))
        self.assertGreaterEqual(self.server.ticks.pending(), 1)
        self.server.ticks.run_due(time.monotonic() + gm.KICK_CLOSE_SECS + 0.5)
        self.assertTrue(_wait(lambda: c2.session is None or c2.session.get('kicked')))
        # world-presence (P5 stage 2): the kicked player despawns on the GM's screen
        self.assertEqual(self.s2c(self.c.expect(0x06)), {'uid': 2})
        self.c.send(0x06, b'\x0c\x63')                            # no such slot
        self.assertEqual(self.notices(), ['[Warning] No session in slot 99 (see !who).'])

    def test_unimplemented_sub_commands_say_which_item_owns_them(self):
        for label, payload, _key in self.SUBCOMMANDS:
            sub = gm.SUBCOMMANDS[payload[0]]
            if sub.name in ('manner', 'notice', 'update', 'kick', 'go', 'stop'):   # go / stop: P6 stage 4
                continue
            with self.subTest(label):
                self.c.send(0x06, payload)
                line = self.notices()[0]
                self.assertIn('[Warning] ', line)
                self.assertIn(sub.owner.split('-')[-1], line)
                self.assertTrue(any(f'0x06/{sub.name}' in a for a in self.audit_lines()))


# ============================================================= dev-commands ===
class DevCommands(GmTest):
    """chat_mail_gm-dev-commands F11: the '!' router, its registry and every command."""

    def test_only_a_gm_gets_commands_and_they_are_never_broadcast(self):
        c2 = self.second_client()
        self.gm_line(b'!who', c2)                                 # no flag: ordinary chat
        self.assertEqual(self.s2c(c2.expect(0x16))['text'], '!who')
        if self.c.session.get('current_map') == c2.session.get('current_map'):
            # map chat (P5 stage 4): the GM on the same map hears the non-GM's line too
            self.assertEqual(self.s2c(self.c.expect(0x16))['text'], '!who')
        self.gm_line(b'!who')
        for text in self.notices():
            self.assertNotIn('!who', text)                        # answered, not echoed
        c2.expect_silence(0.1)                                    # and never reaches the map

    def test_unknown_command_and_bad_arguments_answer_with_the_usage(self):
        self.gm_line(b'!nope')
        self.assertEqual(self.notices(), ['[Warning] Unknown command !nope (try !help).'])
        self.gm_line(b'!manner Nova')
        self.assertEqual(self.notices(), ['[Warning] needs 2 argument(s) - !manner <name> <n>'])
        self.gm_line(b'!manner Nova x')
        self.assertEqual(self.notices(), ["[Warning] n: 'x' is not a number - !manner <name> <n>"])
        self.assertEqual(self.audit_lines(), [])                  # a refused command is not audited

    def test_help_lists_every_command(self):
        self.gm_line(b'!help')
        lines = self.notices()
        self.assertTrue(lines[0].startswith('Commands: '))
        listed = ' '.join(lines).replace('Commands: ', '').split()
        for name in W.GameServer.DEV_COMMANDS:
            self.assertIn(name, listed)
        for line in lines:
            self.assertLessEqual(len(line), 88)                   # one S2C 0x15 each
        self.gm_line(b'!help warp')
        self.assertTrue(self.notices()[0].startswith('!warp <map> [x] [y]'))

    def test_who_lists_sessions_with_their_kick_slots(self):
        c2 = self.second_client()
        self.gm_line(b'!who')
        lines = self.notices()
        self.assertEqual(lines[0], '2 session(s) online:')
        self.assertIn(f'{self.server._slot_of(self.c.session)}: TestHero map 101', lines[1])
        self.assertIn('GM', lines[1])
        self.assertIn(f'{self.server._slot_of(c2.session)}: Nova map 101', lines[2])
        self.assertIn('[admin uid 2]', lines[2])

    def test_notice_and_kick_share_the_gm_command_paths(self):
        c2 = self.second_client()
        self.gm_line(b'!notice server restart soon')
        self.assertEqual(self.notices(c2), ['[Announce] server restart soon'])
        self.assertEqual(self.notices(), ['[Announce] server restart soon'])
        self.gm_line(b'!kick Nova')
        self.assertEqual(self.s2c(c2.expect(0x5D), 1)['reason'], 0)
        self.assertEqual(self.notices(), ['Kicked Nova'])
        self.gm_line(b'!kick TestHero')                           # never yourself
        self.assertEqual(self.notices(), ['[Warning] You cannot kick yourself.'])
        self.gm_line(b'!kick ghost')
        self.assertEqual(self.notices(), ["[Warning] 'ghost' is not online - !kick <name|slot>"])

    def test_manner_command_matches_the_slash_command(self):
        c2 = self.second_client()
        self.gm_line(b'!manner nova -50')
        # P6 stage 4 (chat_mail_gm-gm-manner): the GM's client holds Nova on 101, so its copy
        # gets the same 0x97 before the answer line.
        held, line = self.c.expect(0x97, 0x15)
        self.assertEqual(self.s2c(held, 8)['manner_delta'], -50)
        self.assertEqual(self.s2c(line)['text'], 'Manner Nova: -50 (now -50)')
        self.assertEqual(self.s2c(c2.expect(0x97), 8)['manner_delta'], -50)
        self.gm_line(b'!manner nova +50')
        self.assertEqual(self.s2c(self.c.expect(0x97, 0x15)[1])['text'], 'Manner Nova: +50 (now 0)')
        self.assertEqual(self.s2c(c2.expect(0x97), 8)['manner_delta'], 50)

    def test_gm_flag_command_persists_and_flags_a_live_session(self):
        c2 = self.second_client()
        self.gm_line(b'!gm Nova')
        self.assertIn('Nova (admin) gm = 1', self.notices()[0])
        self.assertEqual(self.char('Nova')['gm'], 1)
        self.assertEqual(c2.session['gm'], 1)                     # '!' commands work at once
        with open(self.server.store.path, encoding='utf-8') as f: # set_gm saves immediately
            self.assertEqual(json.load(f)['admin']['characters'][0]['gm'], 1)
        self.gm_line(b'!gm Nova 0')
        self.notices()                                            # let the handler finish
        self.assertEqual(self.char('Nova')['gm'], 0)
        self.assertEqual(c2.session['gm'], 0)
        self.gm_line(b'!gm ghost')
        self.assertEqual(self.notices(), ["[Warning] no character named 'ghost' - !gm <name> [0|1]"])

    def test_mail_and_gift_store_records_for_the_delivery_items(self):
        self.gm_line(b'!mail Nova hello there')
        self.assertIn('Memo 1 stored for Nova', self.notices()[0])
        memo = self.char('Nova')['memos'][0]
        self.assertEqual((memo['id'], memo['from'], memo['text']), (1, 'TestHero', 'hello there'))
        self.assertEqual(len(memo['t']), 8)                       # SYSTEMTIME shape (F6)
        self.gm_line(b'!mail Nova again')
        self.notices()
        self.assertEqual([m['id'] for m in self.char('Nova')['memos']], [1, 2])
        self.gm_line(b'!gift Nova 3327 enjoy')
        self.assertIn('Gift 3327 stored for admin', self.notices()[0])
        self.assertEqual(self.account('admin')['gift_inbox'],
                         [{'sender': 'TestHero', 'message': 'enjoy', 'item_id': 3327,
                           'serial': 0, 'delivered': False}])
        self.gm_line(b'!gift Nova 4249 x')                        # outside the EN catalog
        self.assertIn('outside', self.notices()[0])
        self.gm_line(b'!mail ghost hi')
        self.assertIn("no character named 'ghost'", self.notices()[0])

    def test_level_and_exp_grant_exp_so_the_client_levels_itself(self):
        self.gm_line(b'!level 2')
        exp = progression.exp_for_level(2)
        exp_pkt, notice = self.c.expect(0x21, 0x15)
        self.assertEqual(self.s2c(exp_pkt, 4)['exp_delta'], exp)
        self.assertEqual(self.s2c(notice)['text'], f'Level 2 (exp {exp}).')
        self.assertEqual(self.char()['exp'], exp)
        self.gm_line(b'!exp +10')
        exp_pkt, notice = self.c.expect(0x21, 0x15)
        self.assertEqual(self.s2c(exp_pkt, 4)['exp_delta'], 10)
        self.assertEqual(self.s2c(notice)['text'], f'Exp {exp + 10} (Lv.2).')
        self.gm_line(b'!exp 0')                                   # an absolute total, so negative
        exp_pkt, notice = self.c.expect(0x21, 0x15)
        self.assertEqual(self.s2c(exp_pkt, 4)['exp_delta'], -(exp + 10))
        self.assertEqual(self.char()['exp'], 0)
        self.gm_line(b'!level 100')
        self.assertEqual(self.notices(),
                         ['[Warning] level: 100 is outside 1..99 - !level <1-99>'])

    def test_give_validates_against_the_en_item_catalog(self):
        self.gm_line(b'!give 179 2')
        drop, notice = self.c.expect(0x18, 0x15)
        rec = self.s2c(drop)
        self.assertEqual((rec['item_id'], rec['count']), (179, 2))
        self.assertEqual(self.s2c(notice)['text'], 'Gave 2 x Wooden Stick (179).')
        # The bag is the character record now (item_inventory-model-persist), not a session key.
        self.assertEqual(self.server._inventory(self.c.session)[179], 2)
        self.gm_line(b'!give 4249')                               # KR-only id: never sent
        self.assertIn('outside', self.notices()[0])
        self.assertNotIn(4249, self.server._inventory(self.c.session))

    def test_warp_replays_the_map_transfer(self):
        self.gm_line(b'!warp 102')
        pkts = self.c.expect(0x15, 0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(8))
        # No point given: where the real portal 101_23 lands (shop_storage-flea-warp; it
        # used to be the start point, which is 101's)
        self.assertEqual(self.s2c(pkts[0])['text'],
                         'Warping to map 102 (stage01_02) at (50.0, 712.0).')
        self.assertEqual(struct.unpack_from('<H', pkts[1].payload)[0], 102)
        self.assertEqual(self.c.session['current_map'], 102)
        self.assertEqual(len(self.c.session['monsters']), 8)
        self.gm_line(b'!warp 102')
        self.assertEqual(self.notices(), ['[Warning] already on map 102 - !warp <map> [x] [y]'])
        self.gm_line(b'!warp 65000')
        self.assertIn('not in the map table', self.notices()[0])

    def test_aliases_reach_the_same_handler(self):
        self.gm_line(b'!not aliased')                             # alias of !notice
        self.assertEqual(self.notices(), ['[Announce] aliased'])

    def test_another_group_can_register_its_own_command(self):
        called = []
        W.GameServer._dev_test_mall = lambda self, session, args: called.append(args)
        self.addCleanup(lambda: delattr(W.GameServer, '_dev_test_mall'))
        self.addCleanup(gm.unregister, 'mall')
        gm.register('mall', gm.DevCommand('_dev_test_mall', '!mall', 'open the shop',
                                          owner='premium_cash-x', aliases=('cash',)))
        self.assertEqual(gm.check_commands(W.GameServer.DEV_COMMANDS, W.GameServer, gm.COMMANDS), [])
        self.gm_line(b'!mall now')
        self.assertTrue(_wait(lambda: called == ['now']))
        self.gm_line(b'!cash')                                    # its alias
        self.assertTrue(_wait(lambda: called == ['now', '']))
        self.gm_line(b'!help mall')
        self.assertEqual(self.notices(), ['!mall - open the shop'])
        with self.assertRaises(ValueError):                       # the name is taken
            gm.register('mall', gm.DevCommand('_dev_test_mall', '!mall'))
        with self.assertRaises(ValueError):                       # so is the alias
            gm.register('shop', gm.DevCommand('_dev_test_mall', '!shop', aliases=('cash',)))

    def test_every_command_is_audited_with_its_arguments(self):
        self.gm_line(b'!who')
        self.notices()
        self.assertTrue(any(line.endswith('!who\t') or line.endswith('!who\t\t')
                            or '\t!who\t' in line for line in self.audit_lines()))
        self.gm_line(b'!notice hi')
        self.notices()
        self.assertTrue(any('!notice\thi' in line for line in self.audit_lines()))
        for line in self.audit_lines():
            self.assertTrue(line.startswith('20'), line)          # timestamp first
            self.assertIn('test/TestHero', line)


class AdminGmVerb(GmTest):
    """The localhost admin port makes the FIRST GM (`wsdev gm <character> [0|1]`): while the
    server runs the store owns accounts.json, so F10.0 step 1's hand edit would be lost."""

    def test_admin_gm_flags_a_character_and_its_live_session(self):
        c2 = self.second_client()
        reply = self.server._admin_command(json.dumps({'gm': 'nova', 'level': 1}))
        self.assertIn('ok gm Nova (admin) = 1', reply)
        self.assertIn('live session flagged', reply)
        self.assertEqual(self.char('Nova')['gm'], 1)
        self.assertEqual(c2.session['gm'], 1)
        self.gm_line(b'!who', c2)                                 # the flag works at once
        self.assertEqual(self.notices(c2)[0], '2 session(s) online:')
        self.assertEqual(self.server._admin_command(json.dumps({'gm': 'Nova', 'level': 0})),
                         'ok gm Nova (admin) = 0; live session flagged, client needs a map '
                         'load or relog for its own commands')
        self.assertEqual(self.char('Nova')['gm'], 0)
        self.assertEqual(self.server._admin_command(json.dumps({'gm': 'ghost'})),
                         "error: no character named 'ghost'")
        # The injection verb still works next to it (AdminInjector, roadmap F9).
        self.assertIn('ok 0x15', self.server._admin_command(
            json.dumps({'opcode': 0x15, 'payload_hex': P.build('0x15', {'msg_type': 1, 'text': 'x'}).hex()})))


# =================================================================== units ===
class GmModule(unittest.TestCase):
    """gm.py helpers, without a server."""

    def test_notice_text_follows_the_client_cut(self):
        self.assertEqual(gm.notice_text(b'\x01\x06ice hi'), b'hi')      # typed /notice hi
        self.assertEqual(gm.notice_text(b'\x01\x03 hi'), b'hi')         # typed /not hi
        self.assertEqual(gm.notice_text(b'\x01\x00icehi'), b'icehi')    # no space: kept
        self.assertEqual(gm.notice_text(b'\x01\xff' + b'x' * 300), b'x' * 300)
        self.assertEqual(gm.notice_text(b'\x01'), b'')

    def test_split_and_args(self):
        self.assertEqual(gm.split(b'!who'), ('who', ''))
        self.assertEqual(gm.split('!MANNER Nova -50'), ('manner', 'Nova -50'))
        self.assertEqual(gm.split(b'!mail Nova hello there'), ('mail', 'Nova hello there'))
        self.assertEqual(gm.args('Nova hello there', 2, 'u'), ('Nova', 'hello there'))
        self.assertEqual(gm.args('Nova 3327 a b', 3, 'u'), ('Nova', '3327', 'a b'))
        with self.assertRaises(gm.DevCommandError):
            gm.args('Nova', 2, 'u')

    def test_parse_int_range_and_wrap_list(self):
        self.assertEqual(gm.parse_int('-50', 'n'), -50)
        self.assertEqual(gm.parse_int('0x10', 'n'), 16)
        with self.assertRaises(gm.DevCommandError):
            gm.parse_int('x', 'n')
        with self.assertRaises(gm.DevCommandError):
            gm.parse_int('5', 'n', lo=6)
        lines = gm.wrap_list([f'command{i}' for i in range(20)], 'Commands: ')
        self.assertTrue(all(len(line) <= 88 for line in lines))
        self.assertEqual(' '.join(lines).count('command'), 20)

    def test_sub_command_table_matches_the_spec(self):
        for sub, entry in gm.SUBCOMMANDS.items():
            spec = P.spec(entry.key, 'C2S')
            self.assertEqual(int(spec['opcode'], 16), 0x06)
        self.assertEqual(gm.check_commands(W.GameServer.DEV_COMMANDS, W.GameServer, gm.COMMANDS), [])

    def test_lookup_finds_names_then_aliases_and_check_commands_spots_a_clash(self):
        table = {'who': gm.DevCommand('_dev_who', '!who', aliases=('online',))}
        self.assertIs(gm.lookup('WHO', table), table['who'])
        self.assertIs(gm.lookup('online', table), table['who'])
        self.assertIsNone(gm.lookup('nope', table))
        clash = {'list': gm.DevCommand('_dev_who', '!list', owner='other', aliases=('online',))}
        problems = gm.check_commands(table, W.GameServer, clash)
        self.assertTrue(any('!online' in p for p in problems), problems)

    def test_audit_appends_one_tab_separated_line(self):
        tmp = tempfile.mkdtemp(prefix='ws_gm_audit_')
        self.addCleanup(shutil.rmtree, tmp, True)
        path = gm.audit_path(os.path.join(tmp, 'accounts.json'))
        gm.audit(path, 'test/TestHero', '0x06/notice', 'hello\nworld', when=0)
        gm.audit(path, 'test/TestHero', '!who', '')
        with open(path, encoding='utf-8') as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].split('\t')[1:], ['test/TestHero', '0x06/notice', 'hello world'])
        gm.audit(os.path.join(tmp, 'no such dir', 'x.log'), 'a', 'b')   # never raises


if __name__ == '__main__':
    unittest.main()
