#!/usr/bin/env python3
"""
test_guild.py - P14 part 1 (ROADMAP_2009_ADDENDUM P14 items guild-g0, arch09-receiver-mirror,
arch09-roster-record, guild-g1; systems_2009/guild.md), both client builds
=========================================================================================
  guild-g0              Codec: every 0xB3 sub-code builder and 0xB4..0xBB round-trip under the
                        2009 grammar (sub 3 with 2 members = 150 B, sub 13 = 27 B, 0xBA = 87 B,
                        0xBB's own field order), the swapped job1 / job2 wire positions, C2S 0x88 =
                        87 B, the 130-byte application record, every client limit refused, S2C
                        0xB9 and "sub 19 as the reply to 0x8A" refused for raw sends too.
  arch09-receiver-mirror Mirror: reset on the map load, the own 0x07's value, every 0xB3 / 0xB4 /
                        0xB6 / 0xB7 writer, and the guards (nothing before the first 0x03; subs
                        1/3/6/8/15 never before the own 0x07; never a second sub 3 per 0x03).
  arch09-roster-record  Records: the GM-or-guild block rules (visible GM / member / shadow GM /
                        `gmtag off`) in 0x07 / 0x04 / 0x05 / 0x52 and the roster form; 2008 rows
                        byte-identical with or without a guild.
  guild-g1              Store (guilds.json: create on first change, reload, the idempotent
                        migration with its one-time guilds.json.bak-pre-p12+p14, deleted / renamed
                        members, a malformed file moved aside); Flow2009 with fake 2009 clients:
                        `!guild seed`, tags in the own 0x07 and the peers' 0x05 / 0x04, the 0x8A
                        reply (sub 3 with online = channel, sub 4 to the master, sub 37 for a
                        visible GM, NO second sub 3 / sub 4 without a 0x03), the 0x21 tail exactly
                        when the receiver's client guild id > 1 (pre-multiplier points under an
                        EXP x2 event, none for a GM, kept until the next 0x03 after a disband),
                        0x96 -> sub 23 {0}, 0x9C -> sub 34 {0}, 0x52 with the guild name, the
                        fake online members; Moiba's billboard sale (P15 guild-g6: the Guild Billboard).
  2008                  no guild anywhere: no guilds.json, no route, no record change, `!guild`
                        refused.

Fake clients (fakeclient.MultiClient); no port is bound, no client is started, and the live
accounts.json / guilds.json are never opened (temp copies; the module checks both).
"""
import hashlib
import json
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
import records as R  # noqa: E402
import registry  # noqa: E402
import resync as RS  # noqa: E402

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
PORTAL = {B8: '0x42F76B/0x7E', B9: '0x431284/0x7E'}
FRIENDS, CARDS, GUILD, GUILD_AGAIN = '0x45353B/0x2F', '0x453557/0x63', '0x453581/0x8A', '0x48456D/0x8A'
GB_REGISTER, GB_ACCEPT = '0x485D93/0x96', '0x485FF4/0x9C'
INFO_REQ = {B8: '0x4484CC/0x2A', B9: '0x44A9D6/0x2A'}

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
        'accounts.json / guilds.json changed during test_guild.py (tests must only use temp copies)'


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _ops(pkts):
    return [p.opcode for p in pkts]


def _char(name, gm=0, exp=0):
    return {'name': name, 'level': 1, 'class': 0, 'exp': exp, 'map': 101, 'x': 1411.0,
            'y': 714.0, 'hp': 100, 'mp': 50, 'gm': gm}


def _accounts():
    return {'test': {'password': 'test', 'characters': [_char('TestHero', exp=5000)]},
            'admin': {'password': 'admin', 'characters': [_char('Watcher')]},
            'carol': {'password': 'carol', 'characters': [_char('Carol')]},
            'dave': {'password': 'dave', 'characters': [_char('Dave')]}}


# class / tier set after the load (the pre-P1 fixture's class 1..4 reads as a legacy body id,
# store.LEGACY_BODY_IDS): TestHero 2 / 1, Watcher 1 / 0, Carol 3 / 0.
CLASSES = {'TestHero': (2, 1), 'Watcher': (1, 0), 'Carol': (3, 0)}


def _set_classes(store):
    with store.lock:
        for name, (cls, job2) in CLASSES.items():
            char = store.character_by_name(name)[2]
            char['class'], char['job2'] = cls, job2


def b9(key, fields):
    return P.build(key, fields, client_build=B9)


def parse9(key, body):
    return P.parse(key, body, direction='S2C', client_build=B9)


# ================================================================ guild-g0 ===
class Codec(unittest.TestCase):
    def sample(self):
        members = [G.member_row(1, 'TestHero', 5, 1, 0, 46, 1), G.member_row(2, 'Ghost', 1, 2, 0, 18, 0)]
        return G.sub3(7, 'Testers', 434950, 20, '', 18, 18, 0, 4, 1, members)

    def test_sub3_size_and_the_swapped_job_positions(self):
        key, fields = self.sample()
        body = b9(key, fields)
        self.assertEqual(len(body), 150)                                 # guild.md 11 G0 exit test
        # record 0 starts after sub + 97 B: uid, grade, JOB1, JOB2, level, online, name[17]
        rec = body[98:98 + 26]
        self.assertEqual(struct.unpack_from('<I', rec, 0)[0], 1)
        self.assertEqual(tuple(rec[4:9]), (5, 1, 0, 46, 1))              # grade 5, job1 1, job2 0, Lv 46, online
        self.assertEqual(rec[9:26], P.name17('TestHero'))
        back = parse9('0xB3', body)
        self.assertEqual(back['repeat[member_count]'][1]['member_job1'], 2)
        self.assertEqual(back['guild_points'], 434950)
        # sub 13: ..., level, JOB2, JOB1, grade (a different order, 27 B)
        key, fields = G.sub13(9, 'Newbie', 1, 20, 3, 2, 1)
        body = b9(key, fields)
        self.assertEqual(len(body), 27)
        self.assertEqual(tuple(body[22:27]), (1, 20, 2, 3, 1))           # online, level, job2, job1, grade

    def test_every_sub_and_entity_builder_round_trips(self):
        rec = G.application_record('Alice', 20, 3, 1)
        board = {'master_char_id': 1, 'master_name': 'TestHero', 'board_item_id': 0x10BC, 'guild_id': 7,
                 'guild_name': 'Testers', 'emblem_symbol': 18, 'emblem_bg': 18, 'ad_text': 'Join us',
                 'pos_x': 2200.0, 'pos_y': 1300.0}
        packets = [
            G.sub1(1, 7), G.sub1(2), G.sub2(1), G.sub2(0x10, rec), self.sample(), G.sub4([rec, rec]),
            G.sub5(1), G.sub6(1), G.sub7(1), G.sub8(1), G.sub9(1, 6500), G.sub9(0), G.sub10(20), G.sub11(1),
            G.sub12('Watcher'), G.sub13(2, 'Watcher', 1, 18, 1, 0, 1), G.sub14('Watcher'), G.sub15(),
            G.sub16(0xD), G.sub17('Watcher', 4), G.sub18(1), G.sub19(), G.sub20('Watcher', 2),
            G.sub21('Watcher', 435000), G.sub22('Old', 'New'), G.sub23(0, 0), G.sub24(5, 'Rivals', 1),
            G.sub25(5, 'Watcher', 0xF0), G.sub26([(1, 'TestHero'), (2, 'Watcher')]), G.sub26(None), G.sub27(0),
            G.sub29([(3, 'Room', 'Testers')], 1), G.sub29([], 0, first=True), G.sub30(1, 2), G.sub30(2),
            G.sub30(1, 2, left=True), G.sub32(1, 0, 3, 'Room', 'Testers'), G.sub32(2, 0, 3, room_id2=4),
            G.sub33(3, 'Rivals'), G.sub34(0), G.sub35(3), G.sub37(1), G.sub185(1, 0x10BC), G.sub185(0),
            G.entity_set(2, 7, 'Testers', 18, 18), G.chat_line('Watcher : hi'), G.entity_clear(2),
            G.entity_clear(2, blank_name=False), G.board_remove(7), G.board_add(board),
            G.board_list([board, dict(board, board_item_id=0x10BB, guild_id=9)])]
        subs = set()
        for key, fields in packets:
            with self.subTest(key=key, sub=fields.get('sub')):
                self.assertEqual(P.unknown_fields(key, fields, client_build=B9), [])
                body = b9(key, fields)
                back = parse9(key, body)
                if key == '0xB3':
                    subs.add(fields['sub'])
                if fields.get('sub') in (2, 4):
                    # the application record is RAW bytes (byte 0 is a NUL): parse() reads the
                    # str[130] as a C string, so only the length round-trips here (bytes:
                    # test_the_application_record)
                    self.assertEqual(len(b9(key, back)), len(body))
                    continue
                self.assertEqual(b9(key, back), body)                    # byte-exact round trip
        self.assertEqual(subs, set(G.SUB_CODES))                        # every real sub-code
        self.assertEqual(len(b9(*G.board_add(board))), 87)
        listed = b9(*G.board_list([board]))
        self.assertEqual(len(listed), 1 + 87)
        # 0xBB's own order: guild id right after the master name, the item id after the text
        self.assertEqual(struct.unpack_from('<H', listed, 1 + 4 + 17)[0], 7)
        self.assertEqual(struct.unpack_from('<H', b9(*G.board_add(board)), 4 + 17)[0], 0x10BC)
        self.assertEqual(len(b9(*G.entity_set(2, 7, 'Testers', 18, 18))), 27)
        self.assertEqual(b9(*G.sub23(0, 0)), bytes([23, 0, 0, 0]))
        self.assertEqual(b9(*G.sub34(0)), bytes([34, 0]))
        self.assertEqual(b9(*G.sub19()), bytes([19]))

    def test_the_application_record(self):
        rec = G.application_record('Alice', 20, 3, 1)
        self.assertEqual(len(rec), 130)
        self.assertEqual((rec[0], rec[1:6], rec[6]), (0, b'Alice', 0))
        self.assertEqual(struct.unpack_from('<H', rec, 0x6E)[0], 20 * 100 + 1 * 10 + 3)
        self.assertEqual(G.parse_application(rec), ('Alice', 20, 3, 1))
        body = b9(*G.sub2(0x10, rec))
        self.assertEqual((len(body), body[2:]), (2 + 130, rec))         # not NUL-cut at byte 0
        body = b9(*G.sub4([rec, G.application_record('Bob', 31, 0, 2)]))
        self.assertEqual((body[1], len(body)), (2, 2 + 260))
        self.assertEqual(G.parse_application(body[2 + 130:]), ('Bob', 31, 0, 2))

    def test_client_limits_are_refused(self):
        rec = G.application_record('A', 1, 0, 0)
        row = G.member_row(3, 'X', 1, 0, 0, 1, 0)
        for bad in (lambda: G.sub1(1, 1), lambda: G.sub1(1, 0xFFFF), lambda: G.sub2(0x10),
                    lambda: G.sub3(1, 'X', 0, 15, '', 0, 0), lambda: G.sub3(7, 'X', 0, 15, '', 0, 0, members=[row] * 75),
                    lambda: G.sub3(7, 'X', 0, 15, '', 0, 0, members=[G.member_row(1, 'A', 5, 0, 0, 1, 0)] * 2),
                    lambda: G.sub4([rec] * 16), lambda: G.sub4([b'short']), lambda: G.sub26([(1, 'A')] * 7),
                    lambda: G.sub26([]), lambda: G.sub29([(1, 'a', 'b')] * 9),
                    lambda: G.board_add({'board_item_id': 4283, 'emblem_symbol': 0, 'emblem_bg': 0}, 'kr'),
                    lambda: G.board_add({'board_item_id': 0x10B8, 'emblem_symbol': 0, 'emblem_bg': 0}),
                    lambda: G.sub185(1, 0x10B8), lambda: G.sub185(1, 0x10BC, 'kr'),
                    lambda: G.board_add({'board_item_id': 0x10BC, 'emblem_symbol': 1000, 'emblem_bg': 0}),
                    lambda: G.board_list([{'board_item_id': 0x10BC, 'emblem_symbol': 0, 'emblem_bg': 600}])):
            with self.assertRaises(ValueError):
                bad()
        # the emblem range [V]: fg = shape*20 + colour (50 x 20), bg = pattern*20 + colour (30 x 20)
        self.assertTrue(G.emblem_ok(0, 0) and G.emblem_ok(999, 599) and G.emblem_ok(83, 376))
        self.assertFalse(G.emblem_ok(1000, 0) or G.emblem_ok(0, 600) or G.emblem_ok(-1, 0) or G.emblem_ok(True, 0))
        # job1 / job2 index the 3 x 7 class icon table with no bounds check: clamped (P14 review)
        self.assertEqual((G.member_row(1, 'A', 1, 9, 7, 1, 0)['member_job1'],
                          G.member_row(1, 'A', 1, 9, 7, 1, 0)['member_job2']), (G.JOB1_MAX, G.JOB2_MAX))
        self.assertEqual((G.sub13(1, 'A', 0, 1, 9, 7, 1)[1]['s13_job1'], G.sub13(1, 'A', 0, 1, 9, 7, 1)[1]['s13_job2']),
                         (G.JOB1_MAX, G.JOB2_MAX))
        self.assertEqual(len(b9(*G.sub3(7, 'X', 0, 15, '', 0, 0, members=[row] * 74))), 98 + 26 * 74)
        line = b9(*G.chat_line('x' * 200))
        self.assertEqual((line[0], len(line)), (87, 88))                 # 0xB5: 88-byte buffer
        with self.assertRaises(P.PacketError):
            b9('0xB5', {'text_len': 88, 'text': b'x' * 88})
        notice = b9(*G.sub3(7, 'X' * 30, 0, 15, 'n' * 90, 0, 0))
        back = parse9('0xB3', notice)
        self.assertEqual((len(back['guild_name']), len(back['guild_notice'])), (16, 60))

    def test_levels_and_the_c2s_0x88_length(self):
        for points, level in ((0, 1), (405449, 1), (405450, 2), (1419074, 2), (1419075, 3), (3344962, 4),
                              (6639241, 4), (6639242, 5), (10 ** 9, 5)):
            self.assertEqual(G.level_for_points(points), level, points)
        spec = P.spec('0x482C4D/0x88', 'C2S', client_build=B9)
        self.assertEqual(spec['length'], '87')
        board = {'master_char_id': 1, 'master_name': 'TestHero', 'board_item_id': 0x10BC, 'guild_id': 7,
                 'guild_name': 'Testers', 'emblem_symbol': 18, 'emblem_bg': 18, 'ad_text': 'hi',
                 'pos_x': 1.0, 'pos_y': 2.0}
        payload = P.build('0x482C4D/0x88', board, direction='C2S', client_build=B9)
        self.assertEqual(len(payload), 87)
        self.assertEqual(payload, b9('0xBA', board))                     # the body S2C 0xBA relays
        self.assertEqual(P.parse(0x88, payload, client_build=B9)['ad_text'], 'hi')

    def test_0xB9_and_sub19_as_a_0x8A_reply_are_refused(self):
        self.assertIsNotNone(P.forbidden_reason(0xB9, B9))
        self.assertIsNone(P.forbidden_reason(0xB9, B8))
        self.assertIsNotNone(P.forbidden_reply(0x8A, 0xB3, bytes([19]), B9))
        self.assertIsNone(P.forbidden_reply(0x8A, 0xB3, bytes([15, 0]), B9))
        self.assertIsNone(P.forbidden_reply(0x63, 0xB3, bytes([19]), B9))
        self.assertIsNone(P.forbidden_reply(None, 0xB3, bytes([19]), B9))
        self.assertIsNone(P.forbidden_reply(0x8A, 0xB3, bytes([19]), B8))
        with self.assertRaises(KeyError):
            P.spec('0xB9', client_build=B9)                              # no grammar either
        tmp = tempfile.mkdtemp(prefix='ws_guild_codec_')
        try:
            server = F.make_server(tmp, client_build=B9)
            with self.assertRaises(P.PacketError):
                server._send_encrypted(None, {}, 0xB9, b'\x00')         # raw / admin injection too
            # sub 19 while a C2S 0x8A of that session is dispatched on this thread
            session = {}
            req = registry._Request(session, 0x8A)
            registry._REQUEST.current = req
            try:
                with self.assertRaises(P.PacketError):
                    server._send_encrypted(None, session, 0xB3, bytes([19]))
            finally:
                registry._REQUEST.current = None
            with self.assertRaises(P.PacketError):
                server.guilds.send_sub(None, session, G.sub19(), reply_to=0x8A)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ================================================================== records ===
class Records(unittest.TestCase):
    TAG = R.GuildTag(7, 'Testers', 18, 19)

    def block(self, char, guild=None, session=None, form=R.FORM_FIELD):
        return R.guild_block(char, guild, session, form)

    def test_the_gm_or_guild_rules(self):
        member = {'name': 'A'}
        gm = {'name': 'G', 'gm': 1, 'gm_hidden': 0}
        shadow = {'name': 'S', 'gm': 1, 'gm_hidden': 1}
        guild = {'gm_or_guild_id': 7, 'guild_name': 'Testers', 'guild_emblem_fg': 18, 'guild_emblem_bg': 19}
        self.assertEqual(self.block(member), {'gm_or_guild_id': 0})
        self.assertEqual(self.block(member, self.TAG), guild)
        self.assertEqual(self.block(gm), {'gm_or_guild_id': 1, 'gm_hidden': 0})
        self.assertEqual(self.block(gm, self.TAG), {'gm_or_guild_id': 1, 'gm_hidden': 0})      # the GM tag wins
        self.assertEqual(self.block(gm, self.TAG, {'gm_tag_off': True}), guild)                 # !guild gmtag off
        self.assertEqual(self.block(gm, None, {'gm_tag_off': True}), {'gm_or_guild_id': 0})
        self.assertEqual(self.block(shadow), {'gm_or_guild_id': 1, 'gm_hidden': 1})              # as before P14
        # P14 review: the shadow wins over the guild (the GM clients render a shadowed GM by gm_hidden)
        self.assertEqual(self.block(shadow, self.TAG), {'gm_or_guild_id': 1, 'gm_hidden': 1})
        self.assertEqual(self.block(shadow, self.TAG, {'gm_tag_off': True}), guild)              # gmtag off
        self.assertEqual(self.block(shadow, self.TAG, form=R.FORM_ROSTER)['guild_id'], 7)       # no gm_hidden there
        # rosters: no gm_hidden; 0x52: the name only
        self.assertEqual(self.block(member, self.TAG, form=R.FORM_ROSTER),
                         {'guild_id': 7, 'guild_name': 'Testers', 'guild_emblem_fg': 18, 'guild_emblem_bg': 19})
        self.assertEqual(self.block(gm, self.TAG, form=R.FORM_ROSTER), {'guild_id': 1})
        self.assertEqual(self.block(shadow, None, form=R.FORM_ROSTER), {'guild_id': 0})
        self.assertEqual(self.block(gm, self.TAG, form=R.FORM_INFO), {'guild_id': 7, 'guild_name': 'Testers'})
        self.assertEqual(self.block(member, None, form=R.FORM_INFO), {'guild_id': 0})
        for bad in (R.GuildTag(1, 'X', 0, 0), R.GuildTag(0xFFFF, 'X', 0, 0)):     # never a guild id
            self.assertEqual(self.block(member, bad), {'gm_or_guild_id': 0})
        self.assertEqual(R.tag_value(guild), 7)
        self.assertTrue(R.gm_tag_visible(1) and not R.gm_tag_visible(1, True) and not R.gm_tag_visible(1, False, True))

    def test_the_wire_records_of_a_member(self):
        char = {'name': 'TestHero', 'look': [0] * 14}
        own = R.player_record({'uid': 1}, char, {'uid': 1}, client_build=B9, guild=self.TAG)
        body = P.build('0x07', R.player_list(own), client_build=B9, receiver_uid=1)
        self.assertEqual(len(body), 1 + 382 + 21)                       # + name[17] + fg + bg
        row = P.parse('0x07', body, direction='S2C', client_build=B9)['repeat[player_count]'][0]
        self.assertEqual((row['gm_or_guild_id'], row['guild_name'], row['guild_emblem_bg']), (7, 'Testers', 19))
        remote = R.player_record({'uid': 1}, char, {'uid': 1}, client_build=B9, guild=self.TAG, remote=True)
        self.assertEqual(len(P.build('0x04', R.player_list(remote), client_build=B9, receiver_uid=2)), 1 + 383 + 21)
        appear = P.build('0x05', R.to_0x05(remote, B9), client_build=B9, receiver_uid=2)
        self.assertEqual(P.parse('0x05', appear, direction='S2C', client_build=B9)['guild_name'], 'Testers')
        info = R.player_info({'uid': 1}, char, {'uid': 1}, B9, guild=self.TAG)
        self.assertEqual(P.unknown_fields('0x52', info, client_build=B9), [])
        back = P.parse('0x52', P.build('0x52', info, client_build=B9), direction='S2C', client_build=B9)
        self.assertEqual((back['guild_id'], back['guild_name']), (7, 'Testers'))
        # no guild: the pre-P14 bytes
        plain = R.player_record({'uid': 1}, char, {'uid': 1}, client_build=B9)
        self.assertEqual(len(P.build('0x07', R.player_list(plain), client_build=B9, receiver_uid=1)), 1 + 382)

    def test_2008_records_ignore_the_guild(self):
        char = {'name': 'TestHero', 'look': [0] * 14, 'gm': 1}
        a = R.player_record({'uid': 1}, char, {'uid': 1}, guild=self.TAG)
        b = R.player_record({'uid': 1}, char, {'uid': 1})
        self.assertEqual(P.build('0x07', R.player_list(a)), P.build('0x07', R.player_list(b)))
        self.assertNotIn('guild_name', a)
        self.assertNotIn('guild_id', R.player_info({}, char, {}, None, guild=self.TAG))


# ==================================================================== store ===
class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_guild_store_')
        self.server = F.make_server(self.tmp, accounts=_accounts(), client_build=B9)
        self.path = os.path.join(self.tmp, 'guilds.json')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cid(self, name):
        return self.server.store.character_by_name(name)[2]['cid']

    def reopen(self):
        self.server.guilds = G.Guilds(self.server)
        return self.server.guilds

    def test_created_on_the_first_change_and_reloaded(self):
        gs = self.server.guilds
        self.assertEqual(gs.db.path, self.path)
        self.assertFalse(os.path.exists(self.path))                      # nothing until a change
        g = gs.seed('Testers', 'TestHero', ['Watcher'])
        self.assertEqual((g.id, [m['grade'] for m in g.members]), (2, [5, 1]))
        self.assertTrue(gs.db.dirty)                                     # the tick thread writes it
        self.assertTrue(gs.flush())
        with open(self.path, encoding='utf-8') as f:
            data = json.load(f)
        self.assertEqual((data['version'], data['last_id'], data['guilds'][0]['name']), (1, 2, 'Testers'))
        self.assertEqual([m['cid'] for m in data['guilds'][0]['members']], [self.cid('TestHero'), self.cid('Watcher')])
        gs = self.reopen()
        self.assertEqual(gs.db.migration_changes, [])                    # a normalized file: no change
        self.assertFalse(os.path.exists(self.path + G.BACKUP_SUFFIX))
        tag = gs.tag_of(self.server.store.character_by_name('Watcher')[2])
        self.assertEqual(tag, R.GuildTag(2, 'Testers', 18, 18))
        self.assertIsNone(gs.tag_of(self.server.store.character_by_name('Carol')[2]))
        # refusals: a duplicate name (any case), a member of another guild, unknown names
        for args in (('testers', 'Carol'), ('Other', 'Watcher'), ('Other', 'Nobody'), ('Bad Name', 'Carol'),
                     ('Other', 'Carol', ['Carol']), ('W' * 17, 'Carol')):
            with self.assertRaises(ValueError):
                gs.seed(*args)
        # ids are never reused
        gs.disband('Testers')
        self.assertEqual(gs.seed('Second', 'Carol').id, 3)

    def test_the_migration_is_idempotent_and_backs_up_once(self):
        t, w, c = self.cid('TestHero'), self.cid('Watcher'), self.cid('Carol')
        raw = {'guilds': [
            {'id': 5, 'name': 'Alpha', 'points': -3, 'max_members': 99, 'notice': 'n' * 80, 'emblem_fg': 70000,
             'emblem_bg': 1009,
             'members': [{'cid': t, 'name': 'stale', 'grade': 5}, {'cid': w, 'grade': 5}, {'cid': 999999},
                         {'cid': 'x'}],
             'applications': [{'cid': c, 'level': 3}, {'cid': c}, {'cid': t}], 'extra': 1},
            {'id': 6, 'name': 'alpha', 'members': [{'cid': c, 'grade': 1}]},           # duplicate name
            {'id': 5, 'name': 'Dup', 'members': [{'cid': c}]},                          # duplicate id
            {'id': 1, 'name': 'GmId', 'members': [{'cid': c}]},                         # id 1 = the GM marker
            {'id': 8, 'name': 'Beta', 'members': [{'cid': w, 'grade': 1}, {'cid': c, 'grade': 2}]},
            'junk']}
        original = json.dumps(raw).encode('utf-8')
        with open(self.path, 'wb') as f:
            f.write(original)
        gs = self.reopen()
        self.assertTrue(gs.db.migration_changes)
        with open(self.path + G.BACKUP_SUFFIX, 'rb') as f:
            self.assertEqual(f.read(), original)                         # the one-time backup
        alpha, beta = gs.get('Alpha'), gs.get('Beta')
        self.assertEqual(alpha.emblem_bg, 599)                           # clamped to the sheets (live triage G3)
        self.assertEqual((beta.emblem_fg, beta.emblem_bg), (G.EMBLEM_DEFAULT, G.EMBLEM_DEFAULT))
        self.assertEqual(sorted(g.name for g in gs.all()), ['Alpha', 'Beta'])
        self.assertEqual((alpha.points, alpha.max_members, len(alpha.notice), alpha.emblem_fg), (0, 50, 60, 999))
        self.assertEqual([(m['cid'], m['grade'], m['name']) for m in alpha.members],
                         [(t, 5, 'TestHero'), (w, 1, 'Watcher')])            # one master; name from the store
        self.assertEqual([a['cid'] for a in alpha.applications], [c])
        # Watcher is Alpha's already: Beta keeps Carol, who becomes its master
        self.assertEqual([(m['cid'], m['grade']) for m in beta.members], [(c, 5)])
        # idempotent: the second load changes nothing and never rewrites the backup
        os.utime(self.path + G.BACKUP_SUFFIX, (1, 1))
        gs = self.reopen()
        self.assertEqual(gs.db.migration_changes, [])
        self.assertEqual(os.stat(self.path + G.BACKUP_SUFFIX).st_mtime, 1)

    def test_deleted_and_renamed_members_and_a_malformed_file(self):
        gs = self.server.guilds
        gs.seed('Testers', 'TestHero', ['Watcher', 'Carol'])
        gs.flush()
        with self.server.store.lock:
            self.server.store.accounts['carol']['characters'] = []      # Carol deleted
            self.server.store.find_character('admin', 'Watcher')['name'] = 'Watch2'
            self.server.store._rebuild_name_index()
        g = gs.get('Testers')
        _key, fields = gs.info_fields(g)                                 # skipped on the wire at once
        self.assertEqual([r['member_name'] for r in fields['repeat[member_count]']], ['TestHero', 'Watch2'])
        gs = self.reopen()                                                # dropped / renamed in the file
        self.assertEqual([m['name'] for m in gs.get('Testers').members], ['TestHero', 'Watch2'])
        self.assertTrue(os.path.exists(self.path + G.BACKUP_SUFFIX))
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write('{not json')
        with self.assertLogs('WS', logging.ERROR):
            gs = self.reopen()
        self.assertEqual(gs.all(), [])
        self.assertTrue(any(n.startswith('guilds.json.bad-') for n in os.listdir(self.tmp)))

    def test_a_later_destructive_load_keeps_its_own_backup(self):
        """P14 review: after the one-time backup exists, a load that drops rows (characters the
        account store does not know) writes guilds.json.bak-<time> of the file before it."""
        gs = self.server.guilds
        gs.seed('Testers', 'TestHero', ['Watcher', 'Carol'])
        gs.flush()
        snap = gs.db.snapshot()
        snap['guilds'][0]['extra'] = 1                                   # a hand edit: the one-time backup
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(snap, f)
        self.reopen()
        self.assertTrue(os.path.exists(self.path + G.BACKUP_SUFFIX))
        stamped = lambda: sorted(n for n in os.listdir(self.tmp)
                                 if n.startswith('guilds.json.bak-') and not n.endswith(G.BACKUP_SUFFIX))
        self.reopen()
        self.assertEqual(stamped(), [])                                  # nothing dropped: no copy
        with open(self.path, 'rb') as f:
            before = f.read()
        with self.server.store.lock:
            self.server.store.accounts['carol']['characters'] = []      # Carol deleted
            self.server.store._rebuild_name_index()
        with self.assertLogs('WS', logging.WARNING) as cm:
            gs = self.reopen()
        self.assertTrue(any('drops data' in line for line in cm.output))
        self.assertEqual([m['name'] for m in gs.get('Testers').members], ['TestHero', 'Watcher'])
        [copy] = stamped()
        with open(os.path.join(self.tmp, copy), 'rb') as f:
            self.assertEqual(f.read(), before)                           # the rows are not lost
        self.reopen()
        self.assertEqual(len(stamped()), 1)                              # a clean load writes none
        self.assertEqual(G.census(gs.db.guilds), (1, 2, 0))

    def test_memory_only_and_the_2008_server(self):
        tmp = tempfile.mkdtemp(prefix='ws_guild_mem_')
        try:
            server = F.make_server(tmp, accounts=_accounts(), config=cfgmod.from_dict(
                {'CLIENT_BUILD': B9, 'GUILDS_FILE': '', 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False}))
            server.guilds.seed('Testers', 'TestHero')
            self.assertFalse(os.path.exists(os.path.join(tmp, 'guilds.json')))
            s8 = F.make_server(tmp, accounts=_accounts())
            self.assertFalse(s8.guilds.supported)
            self.assertEqual(s8.guilds.db.path, '')
            with self.assertRaises(ValueError):
                s8.guilds.seed('Testers', 'TestHero')
            self.assertIsNone(s8.guilds.tag_of({'cid': 1}))
            self.assertFalse(os.path.exists(os.path.join(tmp, 'guilds.json')))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.from_dict({'GUILD_POINTS_PCT': -1})


# ============================================================ 2009 flows ===
class _Rig:
    """TestHero (c:1, uid 1, class 2 / job2 1), Watcher (c:2, uid 2) and Carol (c:3, uid 3) in
    world on map 101; Dave offline."""
    build = B9
    players = (('test', 'test', 'TestHero'), ('admin', 'admin', 'Watcher'), ('carol', 'carol', 'Carol'))
    extra_config = {}

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        self.tmp = tempfile.mkdtemp(prefix=f'ws_p14_{self.build}_')
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False,
                                **self.extra_config})
        self.server = F.make_server(self.tmp, accounts=_accounts(), config=cfg)
        _set_classes(self.server.store)
        self.mc = F.MultiClient(self.server, players=self.players)
        self.a, self.b, self.c = self.mc
        _wait(lambda: len(self.server.world.peers(self.a.session)) == 2)
        self.mc.drain(0.3)

    def tearDown(self):
        self.mc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def key(self, op, index=0):
        return P.variants(op, 'C2S', client_build=self.build)[index]['key']

    def gm(self, c, line):
        """A '!' line as its GM types it; returns the 0x15 texts it answered."""
        self.server._gm_chat_command(c.session['sock'], c.session, line.encode())
        return [P.to_bytes(c.s2c(p)['text']).decode('latin-1') for p in c.recv_until_quiet(0.3) if p.opcode == 0x15]

    def reload(self, c):
        """A map load in place (the server's MapTransfer, as `!warp` / a revive run it): returns
        the decoded own 0x07 row."""
        s = c.session
        c.recv_until_quiet(0.2)                       # whatever the peers' map loads queued
        self.server._map_transfer(s['sock'], s, s['current_map'], *s['pos'], reason='test reload')
        pkts = c.recv_until_quiet(0.3)
        ops = _ops(pkts)
        self.assertEqual(ops[:3], [0x08, 0x03, 0x07])
        return c.s2c(pkts[2])['repeat[player_count]'][0]

    def resync(self, c, again=False, raw=False):
        """What the 2009 client sends at the end of every 0x03; returns the 0xB3 replies
        (decoded, or their raw payloads)."""
        if not again:
            c.send_c2s(FRIENDS)
            c.send_c2s(CARDS)
        c.send_c2s(GUILD_AGAIN if again else GUILD)
        pkts = [p for p in c.recv_until_quiet(0.3) if p.opcode == 0xB3]
        return [p.payload for p in pkts] if raw else [c.s2c(p) for p in pkts]

    def exp_packet(self, c, amount=40, source='kill'):
        self.server.award_exp(c.session, amount, source)
        pkts = [p for p in c.recv_until_quiet(0.3) if p.opcode == 0x21]
        self.assertEqual(len(pkts), 1)
        return pkts[0]

    def char(self, name):
        return self.server.store.character_by_name(name)[2]


class Flow2009(_Rig, unittest.TestCase):
    def test_seed_tags_and_the_0x8A_reply(self):
        a, b = self.a, self.b
        lines = self.gm(a, '!guild seed Testers TestHero Watcher')
        self.assertIn('Guild Testers (id 2) seeded', lines[0])
        self.assertEqual(lines[-1], 'Each member sees it after a portal or relog.')   # its own line
        self.assertTrue(all(len(line) <= 80 for line in lines))
        self.assertGreaterEqual(self.server.ticks.run_due(time.monotonic() + 1), 1)   # the save
        self.assertTrue(os.path.exists(os.path.join(self.tmp, 'guilds.json')))
        # nothing changes on a client until its next map load
        self.assertEqual(CV.guild_id(a.session), 0)
        own = self.reload(a)
        self.assertEqual((own['gm_or_guild_id'], own['guild_name'], own['guild_emblem_fg']), (2, 'Testers', 18))
        self.assertEqual(CV.guild_id(a.session), 2)                      # the own 0x07's value
        # the peers got the reloaded TestHero as 0x05 with the tag
        appear = [b.s2c(p) for p in b.recv_until_quiet(0.3) if p.opcode == 0x05]
        self.assertEqual([(r['name'], r['gm_or_guild_id'], r['guild_name']) for r in appear],
                         [('TestHero', 2, 'Testers')])
        with self.assertNoLogs('WS', logging.ERROR):
            replies = self.resync(a)
        self.assertEqual(len(replies), 1)
        info = replies[0]
        self.assertEqual((info['sub'], info['guild_id'], info['guild_name'], info['max_members'], info['member_count']),
                         (3, 2, 'Testers', 15, 2))
        rows = [(r['member_id'], r['member_name'], r['member_grade'], r['member_job1'], r['member_job2'],
                 r['member_level'], r['member_online']) for r in info['repeat[member_count]']]
        hero = self.char('TestHero')
        self.assertEqual(rows, [(1, 'TestHero', 5, 2, 1, R.level_of(hero), 1), (2, 'Watcher', 1, 1, 0, 1, 1)])
        self.assertEqual(CV.guild_id(a.session), 2)
        # a second 0x8A without a 0x03 (the re-request send site): NO second sub 3 - it would
        # list every member twice - and still never sub 19
        with self.assertLogs('WS', logging.WARNING) as logs:
            self.assertEqual(self.resync(a, again=True), [])
        self.assertTrue(any('second sub 3' in line for line in logs.output))
        # the next map load allows the next one; Watcher (offline) shows online 0
        b.close()
        _wait(lambda: self.server.world.by_char_name('Watcher') is None)
        self.reload(a)
        info = self.resync(a)[0]
        self.assertEqual([r['member_online'] for r in info['repeat[member_count]']], [1, 0])
        # a non-member is still "not in a guild"
        self.assertEqual(self.resync(self.c), [{'sub': 15, 's15_result': 0}])

    def test_the_0x21_tail_matches_the_client_under_concurrent_map_loads(self):
        """P14 review (the race the clientview lock closes): kills on another thread while map
        loads - with the guild seeded / disbanded between them - change what the client holds.
        A model of the client's own entity+0x12, replayed over the stream in wire order (0x03
        resets it, the own 0x07, sub 1 / 3 / 6 / 8 / 15 / 37, 0xB4 / 0xB6 / 0xB7 on the own uid),
        must expect the length of EVERY 0x21: 8 bytes (the guild_points tail) above 1, else 4."""
        a = self.a
        s = a.session
        uid = P.session_uid(s)
        pkts, stop = [], threading.Event()

        def reader():
            while not stop.is_set():
                p = a.recv(0.05)
                if p is not None:
                    pkts.append(p)

        def killer():
            for _ in range(400):
                if stop.is_set():
                    return
                self.server.award_exp(s, 10, 'kill')
                time.sleep(0.002)

        threads = [threading.Thread(target=reader, daemon=True), threading.Thread(target=killer, daemon=True)]
        for t in threads:
            t.start()
        try:
            for i in range(12):
                if i % 2 == 0:
                    self.server.guilds.seed('Testers', 'TestHero', ['Watcher'])
                else:
                    self.server.guilds.disband('Testers')
                self.server._map_transfer(s['sock'], s, s['current_map'], *s['pos'], reason='stress')
                a.send_c2s(FRIENDS)
                a.send_c2s(CARDS)
                a.send_c2s(GUILD)
                time.sleep(0.03)
        finally:
            threads[1].join(5)
            time.sleep(0.3)
            stop.set()
            threads[0].join(2)
        pkts += a.recv_until_quiet(0.3)
        model, sizes = 0, []
        for p in pkts:
            if p.opcode == 0x03:
                model = 0
            elif p.opcode == 0x07:
                for row in a.s2c(p)['repeat[player_count]']:
                    if row['uid'] == uid:
                        model = row['gm_or_guild_id']
            elif p.opcode == 0xB3:
                f = a.s2c(p)
                sub = f['sub']
                if sub == 1 and f['s1_result'] == 1:
                    model = f['s1_guild_id']
                elif sub == 3:
                    model = f['guild_id']
                elif (sub == 6 and f['s6_result'] not in (0, 0x0C)) or (sub == 8 and f['s8_result'] == 1):
                    model = 0
                elif sub == 15:
                    model = 0xFFFF if f['s15_result'] == 1 else 0
                elif sub == 37 and f['s37_uid'] == uid:
                    model = 1
            elif p.opcode in (0xB4, 0xB6, 0xB7):
                f = a.s2c(p)
                if f['uid'] == uid:
                    model = f['guild_id'] if p.opcode == 0xB4 else 0
            elif p.opcode == 0x21:
                sizes.append(len(p.payload))
                self.assertEqual(len(p.payload), 8 if model > 1 else 4,
                                 f'0x21 #{len(sizes)} with the client holding entity+0x12 = {model}')
        self.assertGreater(len(sizes), 50)
        self.assertEqual(set(sizes), {4, 8})                             # both states were exercised

    def test_a_map_load_between_the_sub3_and_the_sub4_drops_the_sub4(self):
        """P14 review: the 0x8A reply's sub 4 belongs to the map load of its sub 3 - a load
        another thread starts in between (a tick, a revive, a GM warp) gets none from it (it
        would land with no sub 3 in its epoch, and the next 0x8A would add every application
        a second time); that load's own 0x8A carries the whole state once."""
        a = self.a
        s, gs = a.session, self.server.guilds
        gs.seed('Testers', 'TestHero')
        gs.add_application('Testers', 'Dave')
        self.reload(a)
        epoch = gs.reply_info(s['sock'], s)                              # the 0x8A step 'guild'
        self.assertEqual(epoch, CV.view(s).loads)
        self.server._map_transfer(s['sock'], s, s['current_map'], *s['pos'], reason='a revive meanwhile')
        with self.assertLogs('WS', logging.INFO) as cm:
            gs.reply_applications(s['sock'], s, epoch=epoch)              # the step 'guild_apps'
        self.assertTrue(any('dropped: map load' in line for line in cm.output))
        got = a.recv_until_quiet(0.3)
        self.assertEqual([a.s2c(p)['sub'] for p in got if p.opcode == 0xB3], [3])   # no sub 4
        self.assertFalse(CV.view(s).apps_held)
        self.assertEqual([r['sub'] for r in self.resync(a)], [3, 4])     # the new load's own 0x8A
        self.assertTrue(CV.view(s).apps_held)

    def test_the_0x21_tail_follows_the_client_view(self):
        a, c = self.a, self.c
        self.server.guilds.seed('Testers', 'TestHero', ['Watcher'])
        self.assertEqual(len(self.exp_packet(a).payload), 4)              # tag not on the client yet
        self.reload(a)
        self.resync(a)
        pkt = self.exp_packet(a, 41)
        self.assertEqual(a.s2c(pkt), {'exp_delta': 41, 'guild_points': 20})   # 41 -> 20 (guild.md F12)
        self.assertEqual(a.session['guild_pending_gp'], 20)
        self.assertEqual(len(self.exp_packet(c).payload), 4)             # a non-member: no tail
        # an EXP x2 event: the tail stays, the points come from the PRE-multiplier exp (X9)
        self.gm(a, '!expmult 2')
        self.assertEqual(a.s2c(self.exp_packet(a, 40)), {'exp_delta': 80, 'guild_points': 20})
        self.gm(a, '!expmult off')
        # a GM / penalty grant (no guild_base) still carries the tail: +0
        self.server.grant_exp(a.session, 5)
        pkt = [p for p in a.recv_until_quiet(0.3) if p.opcode == 0x21][0]
        self.assertEqual(a.s2c(pkt), {'exp_delta': 5, 'guild_points': 0})
        # a loss never has it
        self.server.grant_exp(a.session, -5)
        pkt = [p for p in a.recv_until_quiet(0.3) if p.opcode == 0x21][0]
        self.assertEqual(len(pkt.payload), 4)
        # disbanded: the client still holds the guild until its next 0x03 -> the tail stays
        self.server.guilds.disband('Testers')
        self.assertEqual(len(self.exp_packet(a).payload), 8)
        own = self.reload(a)
        self.assertEqual(own['gm_or_guild_id'], 0)
        self.assertEqual(self.resync(a), [{'sub': 15, 's15_result': 0}])
        self.assertEqual(len(self.exp_packet(a).payload), 4)

    def test_a_gm_member_keeps_the_gm_tag_unless_switched_off(self):
        a = self.a
        with self.server.store.lock:
            self.char('TestHero')['gm'] = 1
        a.session['gm'], a.session['gm_hidden'] = 1, 0
        self.server.guilds.seed('Testers', 'TestHero', ['Watcher'])
        own = self.reload(a)
        self.assertEqual((own['gm_or_guild_id'], own['gm_hidden']), (1, 0))   # the GM tag wins
        self.assertEqual(CV.guild_id(a.session), 1)
        replies = self.resync(a)
        self.assertEqual([r['sub'] for r in replies], [3, 37])           # the window, then the tag
        self.assertEqual(replies[1], {'sub': 37, 's37_uid': 1})
        self.assertEqual(CV.guild_id(a.session), 1)
        self.assertEqual(len(self.exp_packet(a).payload), 4)             # entity+0x12 == 1: no tail
        # !guild gmtag off: the records show the guild, no sub 37, the tail comes back
        self.assertIn('OFF', self.gm(a, '!guild gmtag off')[0])
        own = self.reload(a)
        self.assertEqual((own['gm_or_guild_id'], own['guild_name']), (2, 'Testers'))
        self.assertEqual([r['sub'] for r in self.resync(a)], [3])
        self.assertEqual(len(self.exp_packet(a).payload), 8)
        # in shadow (gm_hidden) the records keep the shadow (P14 review: the GM clients render
        # a shadowed GM by gm_hidden), the 0x8A still gives the window (sub 3), no sub 37
        self.gm(a, '!guild gmtag on')
        with self.server.store.lock:
            self.char('TestHero')['gm_hidden'] = 1
        a.session['gm_hidden'] = 1
        own = self.reload(a)
        self.assertEqual((own['gm_or_guild_id'], own['gm_hidden']), (1, 1))
        self.assertEqual([r['sub'] for r in self.resync(a)], [3])
        self.assertEqual(CV.guild_id(a.session), 2)                      # the window's id: the tail
        self.assertEqual(len(self.exp_packet(a).payload), 8)

    def test_the_master_gets_the_applications_once_per_map_load(self):
        a, b = self.a, self.b
        self.gm(a, '!guild seed Testers TestHero')
        self.assertIn('1 pending', self.gm(a, '!guild apply Testers Carol')[0])
        self.gm(a, '!guild apply Testers Dave')
        self.reload(a)
        replies = self.resync(a, raw=True)
        self.assertEqual([p[0] for p in replies], [3, 4])
        sub4 = replies[1]
        self.assertEqual((sub4[1], len(sub4)), (2, 2 + 2 * 130))
        apps = [G.parse_application(sub4[2 + 130 * i:2 + 130 * (i + 1)]) for i in range(2)]
        self.assertEqual(apps, [('Carol', 1, 3, 0), ('Dave', 1, 0, 0)])  # FIFO
        self.assertEqual(self.resync(a, again=True), [])                 # no second sub 3 / sub 4
        # a member who is not the master gets none
        self.gm(a, '!guild disband Testers')
        self.server.guilds.seed('Testers', 'Watcher', ['TestHero'])
        self.server.guilds.add_application('Testers', 'Carol')
        self.reload(a)
        self.assertEqual([r['sub'] for r in self.resync(a)], [3])
        self.reload(b)
        self.assertEqual([r['sub'] for r in self.resync(b)], [3, 4])

    def test_fake_online_members_and_the_guild_battle_fallbacks(self):
        a = self.a
        self.gm(a, '!guild seed Testers TestHero Watcher')
        self.gm(a, '!guild fake Testers 5')
        self.reload(a)
        info = self.resync(a)[0]
        rows = info['repeat[member_count]']
        self.assertEqual(info['member_count'], 7)
        self.assertEqual(sum(1 for r in rows if r['member_online']), 7)  # >= 6 online: the GB gate passes
        self.assertEqual(rows[2]['member_name'], 'Dummy1')
        self.assertFalse(any(r['member_id'] < G.FAKE_UID_BASE for r in rows[2:]))
        with self.assertNoLogs('WS', logging.ERROR):
            a.send_c2s(GB_REGISTER)
            self.assertEqual(a.s2c(a.expect(0xB3)), {'sub': 23, 's23_result': 0, 's23_value': 0})
            a.send_c2s(GB_ACCEPT, {'room_id': 3})
            self.assertEqual(a.s2c(a.expect(0xB3)), {'sub': 34, 's34_result': 0})
        # the backstop rows carry the same bytes
        table = registry.must_reply_table(B9)
        for op, fields in ((0x96, {'sub': 23, 's23_result': 0, 's23_value': 0}), (0x9C, {'sub': 34, 's34_result': 0})):
            [(key, refusal, _assume)] = table[op].refusal(self.server, a.session, None)
            self.assertEqual((key, refusal), ('0xB3', fields))
        self.assertNotIn(0x96, registry.must_reply_table(B8))
        self.assertNotIn(0x9C, registry.must_reply_table(B8))

    def test_char_info_names_the_guild_and_moiba_sells_the_billboard(self):
        a, b = self.a, self.b
        self.server.guilds.seed('Testers', 'Watcher')
        # Watcher on another map: Char. Info goes through C2S 0x2A -> 0x52
        b.send_c2s(PORTAL[self.build], {'portal_line_index': 23})
        _wait(lambda: self.server.world.map_of(b.session) == 102)
        self.mc.drain(0.3)
        a.send_c2s(INFO_REQ[self.build], {'target_name': 'Watcher'})
        info = a.s2c(a.expect(0x52))
        self.assertEqual((info['guild_id'], info['guild_name']), (2, 'Testers'))
        a.send_c2s(INFO_REQ[self.build], {'target_name': 'Carol'})
        self.assertEqual(a.s2c(a.expect(0x52))['guild_id'], 0)
        # Moiba's billboard sale (C2S 0x0B from the guild menu's send site) was refused until
        # P15 (ADDENDUM X8); guild-g6 sells the Guild Billboard EN 4284 on the cp-2 exe (the
        # default CLIENT_ITEM_IDS 'en'; test_guild_boards.py), guild or not - one 0x18 and the
        # real price line. The wire's npc_id is 0 (p15 live triage 1): Moiba on 9702.
        moiba = 181                                                     # hni idx 181 (guild.md 1.5)
        self.assertEqual(EC.npcs().get(moiba).ui, EC.UI_GUILD_NPC_2009)
        self.server._map_transfer(b.session['sock'], b.session, G.GUILD_PLAZA_MAP, 2200.0, 1300.0, reason='test')
        _wait(lambda: self.server.world.map_of(b.session) == G.GUILD_PLAZA_MAP)
        self.mc.drain(0.3)
        b.send_c2s('0x474327/0x0B', {'item_id': 4283, 'qty': 1, 'npc_id': 0})
        pkts = b.recv_until_quiet(0.3)
        self.assertEqual(_ops(pkts), [0x18, 0x15])
        self.assertEqual((b.s2c(pkts[0])['item_id'], b.s2c(pkts[0])['count']), (4284, 1))

    def test_the_guards_and_the_mirror_writers(self):
        a = self.a
        s, sock = a.session, a.session['sock']
        gs = self.server.guilds
        v = CV.view(s)
        self.assertTrue(v.had_03 and v.own_07)
        # before the own 0x07 of a map load (mid map load): subs 1/3/6/8/15 are refused
        with v.lock:
            v.own_07 = False
        with self.assertLogs('WS', logging.ERROR):
            for packet in (G.sub1(1, 7), G.sub15(), G.sub6(1), G.sub8(1), G.sub3(7, 'X', 0, 15, '', 0, 0)):
                self.assertFalse(gs.send_sub(sock, s, packet))
        self.assertTrue(gs.send_sub(sock, s, G.sub2(1)))                 # no entity write: fine
        a.recv_until_quiet(0.2)
        with v.lock:
            v.own_07 = True
        # every writer of the mirror
        for packet, want in ((G.sub1(1, 7), 7), (G.sub15(), 0), (G.sub15(1), 0xFFFF), (G.sub37(1), 1),
                             (G.sub37(2), 1), (G.sub3(9, 'X', 0, 15, '', 0, 0), 9), (G.sub6(0), 9), (G.sub6(0xC), 9),
                             (G.sub6(1), 0), (G.sub8(1), 0)):
            with self.subTest(sub=packet[1]['sub']):
                self.assertTrue(gs.send_sub(sock, s, packet))
                self.assertEqual(CV.guild_id(s), want)
        for packet, want in ((G.entity_set(1, 7, 'T', 0, 0), 7), (G.entity_set(2, 9, 'T', 0, 0), 7),
                             (G.entity_clear(2), 7), (G.entity_clear(1, blank_name=False), 0)):
            self.assertTrue(gs.send_entity(sock, s, packet))
            self.assertEqual(CV.guild_id(s), want)
        a.recv_until_quiet(0.2)
        # sub 3 with members: once per 0x03; with none (the create trick) any time
        row = [G.member_row(1, 'TestHero', 5, 0, 0, 1, 1)]
        self.assertTrue(gs.send_sub(sock, s, G.sub3(7, 'X', 0, 15, '', 0, 0, members=row)))
        with self.assertLogs('WS', logging.WARNING):
            self.assertFalse(gs.send_sub(sock, s, G.sub3(7, 'X', 0, 15, '', 0, 0, members=row)))
        self.assertTrue(gs.send_sub(sock, s, G.sub3(7, 'X', 0, 15, '', 0, 0)))
        self.assertTrue(gs.send_sub(sock, s, G.sub6(1)))                 # M reset: a sub 3 may follow
        self.assertTrue(gs.send_sub(sock, s, G.sub3(7, 'X', 0, 15, '', 0, 0, members=row)))
        a.recv_until_quiet(0.2)
        # a map load resets the view; pet_info_seen is per map load too (P15 fills it)
        CV.note_pet_info(s, 5)
        self.reload(a)
        self.assertEqual((v.guild_rows, v.pet_info_seen, v.own_07, CV.guild_id(s)), (False, set(), True, 0))
        self.assertGreaterEqual(v.loads, 2)
        # nothing before the connection's first 0x03 (SubHandler3 drops it)
        fresh = {'uid': 9}
        self.assertFalse(gs.send_sub(sock, fresh, G.sub2(1)))
        # sub 19 is never the reply to 0x8A, whoever builds it during that dispatch
        rs = self.server.resync

        def rogue(server, sock_, session, ctx):
            P.send(server, sock_, session, '0xB3', {'sub': 19})

        rs.register(RS.STAGE_GUILD, 'rogue', rogue, before='gm_tag')
        with self.assertLogs('WS', logging.ERROR):
            replies = self.resync(a)
        self.assertEqual(replies, [{'sub': 15, 's15_result': 0}])
        # outside a 0x8A (an injection test, T-B3-19) it is allowed
        P.send(self.server, sock, s, '0xB3', {'sub': 19})
        self.assertEqual(a.s2c(a.expect(0xB3)), {'sub': 19})


class Flow2009Points(_Rig, unittest.TestCase):
    extra_config = {'GUILD_POINTS_PCT': 100}

    def test_the_points_percentage_is_config(self):
        self.server.guilds.seed('Testers', 'TestHero')
        self.reload(self.a)
        self.resync(self.a)
        self.assertEqual(self.a.s2c(self.exp_packet(self.a, 41, 'quest')), {'exp_delta': 41, 'guild_points': 41})


# ===================================================================== 2008 ===
class Flow2008(_Rig, unittest.TestCase):
    build = B8

    def test_no_guild_on_a_2008_server(self):
        a = self.a
        self.assertFalse(self.server.guilds.supported)
        self.assertIn('2009 client', self.gm(a, '!guild seed Testers TestHero')[0])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'guilds.json')))
        self.assertNotIn('guild_apps', self.server.resync.order(B8)[RS.STAGE_GUILD])
        routes = self.server.routes
        self.assertNotEqual(getattr(routes.get(0x96), 'handler', None), '_handle_guild_battle_register')
        self.assertNotEqual(getattr(routes.get(0x9C), 'handler', None), '_handle_guild_battle_accept')
        own = self.reload(a)
        self.assertIn('gm_level', own)
        self.assertNotIn('gm_or_guild_id', own)
        pkt = self.exp_packet(a)
        self.assertEqual(len(pkt.payload), 4)


if __name__ == '__main__':
    unittest.main()
