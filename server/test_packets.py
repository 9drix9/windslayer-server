#!/usr/bin/env python3
"""Offline tests for packets.py, the spec-driven packet layer (run: python test_packets.py).

Covers roadmap P0 exit criterion 1 (sendspec 0x61 with assume = 23 B, 0x59 slot 1
progress 3 = 2 B), the 1.2.3 receiver-state assume table, name17 / S1-14 text clamps,
parse() of every hand capture in test_protocol.MANUAL_C2S and every C2S packet in
server_history.log, and send() through the real GameServer._send_encrypted over a
socketpair. Never binds a port and never imports windslayer_server with the server
directory as cwd (its logging setup truncates server_live.log of a running server).
"""
import io
import json
import os
import socket
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import packets as P  # noqa: E402
from wsproto import hexbytes  # noqa: E402

ROOM = 'client.messenger_room_id != 0'

# One real client capture per C2S (opcode, length) shape in server_history.log as of
# 2026-09-17 (payload after the opcode byte). The login's account/password text is
# replaced by same-length 'user'/'pass'. Fixed here because the live log keeps growing.
LOGGED_C2S = [
    (0x01, '75 73 65 72 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 '
           '00 00 00 00 00 00 00 00 00 70 61 73 73 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00'),
    (0x05, ''),                                                          # KeepAlive
    (0x0D, '00 00 00 00 65 00 D2 00 00 00 00 00 40 61 00 50 50 72'),     # PlayerMoveState
    (0x0D, '00 00 00 00 65 00 98 3A 00 00 00 00 40 61 00 50 50 72'),
    (0x0D, 'BC 60 01 00 65 00 98 3A 00 00 00 00 40 61 00 50 50 72'),
    (0x0D, '00 00 00 00 65 00 02 0D 00 00 10 00 40 6D 00 50 4F 70'),
    (0x0F, 'B3 00 00 00 00'),                                            # EquipItem
    (0x16, '1A 00'),                                                     # QuestAcceptRequest
    (0x2B, '00 00 00 00 00 31 32 37 2E 30 2E 30 2E 31 00 00 00 00 00 00 00 9B A7 00 00 54 65 73 74 48 65 '
           '72 6F 00 00 00 00 00 00 00 00 00'),                          # EnterWorldRequest
    (0x2F, ''),                                                          # MessengerFriendListRequest
    (0x63, ''),                                                          # CardDeckListRequest
    (0x7E, '17 00 00 00'),                                               # PortalEnterRequest
    (0x7E, '11 00 00 00'),
]


def _rebuild(rec):
    """Re-encode a parse() Record with the variant and client-state flips it matched."""
    return P.build(rec.key, dict(rec), assume=rec.assume, direction='C2S', allow_unassumed=True)


class ExitCriterion1(unittest.TestCase):
    """Roadmap P0 exit 1: `wsdev sendspec 61 --assume ...` injects 23 B and
    `sendspec 59 '{"slot":1,"progress":3}'` sends 2 B."""

    def test_0x61_room_message_with_assume_is_23_bytes(self):
        fields = {'sender_name': 'Alice', 'msg_len': 5, 'message': 'hello'}
        raw = P.build(0x61, fields, assume={ROOM: True})
        self.assertEqual(len(raw), 23)
        self.assertEqual(raw, P.name17('Alice') + b'\x05hello')
        # the server's table commits to the in-room form by default ...
        self.assertEqual(P.build('0x61', fields), raw)
        # ... and the old wsproto default-False behaviour is the 0 B packet of bug S1-02
        self.assertEqual(P.build(0x61, fields, assume={ROOM: False}), b'')

    def test_0x59_slot1_progress3_is_2_bytes(self):
        self.assertEqual(P.build(0x59, {'slot': 1, 'progress': 3}), b'\x01\x03')

    def _sendspec(self, *args):
        import wsdev
        sent = []
        orig = wsdev._inject
        # *targeting: stage 2 added the admin --to / --state arguments to _inject
        wsdev._inject = lambda op, payload, *targeting: sent.append((op, payload)) or 'ok (test)'
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                wsdev.cmd_sendspec(list(args))
        finally:
            wsdev._inject = orig
        return sent, out.getvalue()

    def test_wsdev_sendspec_61_with_assume_flag(self):
        sent, out = self._sendspec('61', '{"sender_name":"Alice","msg_len":5,"message":"hello"}',
                                   '--assume', json.dumps({ROOM: True}))
        self.assertEqual(sent, [(0x61, P.name17('Alice') + b'\x05hello')], out)
        self.assertIn('23B', out)

    def test_wsdev_sendspec_assume_inside_json_and_key_value_form(self):
        sent, _ = self._sendspec('61', json.dumps({'sender_name': 'Alice', 'msg_len': 5, 'message': 'hello',
                                                   '__assume__': {ROOM: True}}))
        self.assertEqual(len(sent[0][1]), 23)
        sent, _ = self._sendspec('58', '{"uid":1,"class":2,"class_tier":1}',
                                 '--assume', 'find_entity_by_uid(uid) != null=1')
        self.assertEqual(sent[0][1], struct.pack('<IBB', 1, 2, 1))

    def test_wsdev_sendspec_59(self):
        sent, _ = self._sendspec('59', '{"slot":1,"progress":3}')
        self.assertEqual(sent, [(0x59, b'\x01\x03')])

    def test_wsdev_sendspec_refuses_unstated_receiver_form(self):
        sent, out = self._sendspec('76', '{"target_uid":1,"str":5}')
        self.assertEqual(sent, [])
        self.assertIn('target_uid == local_player_uid', out)
        sent, _ = self._sendspec('76', '{"target_uid":1,"str":5}', '--uid', '1')
        self.assertEqual(len(sent[0][1]), 18)
        sent, _ = self._sendspec('76', '{"target_uid":1,"str":5}', '--loose')
        self.assertEqual(len(sent[0][1]), 12)

    def test_wsdev_sendspec_question_lists_client_state_exprs(self):
        sent, out = self._sendspec('61', '?')
        self.assertEqual(sent, [])
        self.assertIn(ROOM, out)
        self.assertIn('default: True', out)

    def test_wsdev_sendspec_question_describes_resolvers(self):
        _, out = self._sendspec('72', '?')
        self.assertIn('<gamedef items.Cash_T(item_id) == 2', out)
        _, out = self._sendspec('3B', '?')
        self.assertIn('<0x0A31 <= item_or_skill_id <= 0x0A3B', out)

    def test_wsdev_sendspec_0x61_zero_byte_form_needs_assume_false(self):
        # --loose only fills conditions with no value; DEFAULT_ASSUME already says True for 0x61.
        fields = '{"sender_name":"Alice","message":"hello"}'
        sent, out = self._sendspec('61', fields, '--loose')
        self.assertEqual(len(sent[0][1]), 23, out)
        sent, out = self._sendspec('61', fields, '--assume', json.dumps({ROOM: False}))
        self.assertEqual(sent, [(0x61, b'')], out)
        self.assertIn('0B', out)

    def test_wsdev_sendspec_refuses_udp_and_c2s_keys(self):
        for key in ('UDP-S2C:0x06', 'UDP-C2S:0x4236F5/0x03', '0x4484CC/0x27', '0x470018/0x32'):
            with self.subTest(key=key):
                sent, out = self._sendspec(key, '{"uid":1,"cur_hp":0}')
                self.assertEqual(sent, [])
                self.assertIn('no TCP S2C spec', out)

    def test_wsdev_sendspec_bad_arguments_print_instead_of_crashing(self):
        for args in (('61', '{"sender_name":"a"}', '--assume'), ('76', '{"target_uid":1}', '--uid'),
                     ('76', '--uid', '--loose', '{"target_uid":1}'), ('76', '{"target_uid":1}', '--uid', 'x'),
                     ('61', '{not json'), ('61', '[1, 2]'), ('61', '--assume', 'no equals sign'),
                     ('1B', '{"uid":2,"state_blob_u32_0":65536,"pos_x":"abc"}')):
            with self.subTest(args=args):
                sent, out = self._sendspec(*args)                        # must not raise
                self.assertEqual(sent, [])
                self.assertTrue(out.strip())


class AssumeTable(unittest.TestCase):
    def test_table_conditions_match_the_grammars(self):
        self.assertEqual(P.check_assume_table(), [])

    def test_every_s2c_gate_has_a_decision(self):
        """Each client-state condition of an S2C grammar has a table entry, is a
        packet-derived helper (0x1B/0x2A), or the whole key is deliberately excluded."""
        db = P._db()
        derived = {n for entry in P._DERIVED_U32.values() for n in entry[1:]}
        for s in list(db.s2c.values()) + list(db.udp_s2c.values()):
            key = s['key']
            if key in P.NO_DEFAULT_ASSUME:
                continue
            for expr in db.grammar(key).client_state_exprs():
                with self.subTest(key=key, expr=expr):
                    uses_derived = any(name in expr for name in derived)
                    self.assertTrue(expr in P.DEFAULT_ASSUME.get(key, {}) or uses_derived)

    def test_every_tcp_s2c_builds_from_an_empty_record(self):
        for op, s in sorted(P._db().s2c.items()):
            with self.subTest(key=s['key']):
                P.build(s['key'], {}, receiver_uid=1)

    def test_roadmap_1_2_3_forms(self):
        self.assertEqual(len(P.build(0x37, {'entry_count': 1, 'repeat[entry_count]': [{'uid': 2, 'score': 1}]})), 15)
        self.assertEqual(len(P.build(0x5B, {'uid': 2, 'room_no': 3, 'room_title': 'x'})), 24)
        self.assertEqual(len(P.build(0x58, {'uid': 2, 'class': 1, 'class_tier': 1})), 6)
        self.assertEqual(len(P.build(0x74, {'uid': 2, 'new_name': 'Bob'})), 21)
        self.assertEqual(len(P.build(0x62, {'member_name': 'Bob'})), 17)
        self.assertEqual(len(P.build(0x60, {'friend_uid': 2, 'friend_name': 'Bob', 'channel': 1, 'presence': 1})), 23)
        self.assertEqual(len(P.build(0x96, {'target_uid': 1, 'manner_delta': 1, 'complimenter_name': 'Bob'})), 25)
        self.assertEqual(len(P.build(0x6F, {'count': 1, 'repeat[count]': [{'serial': 0x1001, 'item_id': 1894}]})), 33)
        self.assertEqual(len(P.build(0x57, {'uid': 2, 'skill_item_id': 2188})), 6)
        self.assertEqual(len(P.build(0x29, {'uid': 0xF0001, 'respawn_tick': 0x7FFFFFFF})), 16)

    def test_0x76_local_form_follows_receiver(self):
        self.assertEqual(len(P.build(0x76, {'target_uid': 1}, receiver_uid=1)), 18)
        self.assertEqual(len(P.build(0x76, {'target_uid': 1}, receiver_uid=2)), 12)
        with self.assertRaises(P.MissingAssume) as cm:
            P.build(0x76, {'target_uid': 1})
        self.assertEqual(cm.exception.conditions, ['target_uid == local_player_uid'])
        self.assertEqual(len(P.build(0x76, {'target_uid': 1}, assume={'target_uid == local_player_uid': True})), 18)
        self.assertEqual(len(P.build(0x76, {'target_uid': 1}, allow_unassumed=True)), 12)

    def test_0x72_owner_period_item_and_observer(self):
        if P.cash_duration_type(3321) is None:
            self.skipTest('gamedef.sqlite3 not present')
        self.assertEqual(P.cash_duration_type(3321), 2)                  # +50% EXP 60 days
        own = P.build(0x72, {'player_uid': 1, 'item_id': 3321, 'item_serial': 0x1001}, receiver_uid=1)
        self.assertEqual(len(own), 26)                                   # uid+id+serial+SYSTEMTIME
        self.assertEqual(len(P.build(0x72, {'player_uid': 1, 'item_id': 3321}, receiver_uid=2)), 6)
        dye = P.build(0x72, {'player_uid': 1, 'item_id': 0x0D50, 'hair_code': 7, 'item_serial': 5}, receiver_uid=1)
        self.assertEqual(dye, struct.pack('<IHHI', 1, 0x0D50, 7, 5))

    def test_0x2a_position_block_skipped_for_the_action_target(self):
        rec = {'mover_uid': 2, 'target_uid': 1, 'move_bits_lo': 9 << 12, 'action_flag': 1, 'pos_x': 5.0}
        self.assertEqual(len(P.build(0x2A, rec, receiver_uid=3)), 34)
        self.assertEqual(len(P.build(0x2A, rec, receiver_uid=1)), 17)

    def test_0x1b_state_blob_helper_is_derived(self):
        lo = (7 << 12) | (4 << 9) | (1 << 16)
        by_blob = P.build(0x1B, {'uid': 2, 'state_blob': struct.pack('<II', lo, 0)})
        by_lo = P.build(0x1B, {'uid': 2, 'state_blob_u32_0': lo})
        self.assertEqual(len(by_blob), 52)
        self.assertEqual(by_blob, by_lo)
        with self.assertRaises(P.PacketError):
            P.build(0x1B, {'uid': 2, 'state_blob': struct.pack('<II', lo, 0), 'state_blob_u32_0': 1})
        rec = P.parse(0x1B, by_blob, direction='S2C')                   # tails decoded from the bytes
        self.assertEqual(len(rec), len(P._db().grammar('0x1B').decode(by_blob, assume={
            '((state_blob_u32_0 >> 12) & 0xF) != 0': True,
            '(((state_blob_u32_0 >> 9) & 0x7) == 4) && ((((state_blob_u32_0 >> 12) & 0xF) == 7) || '
            '(((state_blob_u32_0 >> 12) & 0xF) == 9))': True,
            '((state_blob_u32_0 >> 16) & 0xF) != 0': True})))

    def test_udp_loopback_snapshot_has_no_default(self):
        with self.assertRaises(P.MissingAssume):
            P.build('UDP-S2C:0x04', {}, direction='UDP-S2C')
        self.assertEqual(len(P.build('UDP-S2C:0x06', {'uid': 2, 'cur_hp': 50}, direction='UDP-S2C')), 8)


class Floats(unittest.TestCase):
    """Review round 1 (major): wsproto wrote f32/f64 through int(), so fractional
    positions left build() as whole numbers."""

    def test_0x1b_position_survives_build_and_parse(self):
        raw = P.build(0x1B, {'uid': 2, 'state_blob_u32_0': 1 << 16, 'pos_x': 123.75, 'pos_y': -0.5})
        self.assertEqual(raw[-16:], struct.pack('<dd', 123.75, -0.5))
        rec = P.parse(0x1B, raw, direction='S2C')
        self.assertEqual((rec['pos_x'], rec['pos_y']), (123.75, -0.5))

    def test_0x2a_peer_keyframe_position_survives_build_and_parse(self):
        raw = P.build(0x2A, {'mover_uid': 2, 'target_uid': 1, 'pos_x': 123.75, 'pos_y': -0.5, 'airborne': 1},
                      receiver_uid=3)
        self.assertEqual(len(raw), 33)
        rec = P.parse(0x2A, raw, direction='S2C')
        self.assertEqual((rec['pos_x'], rec['pos_y'], rec['airborne']), (123.75, -0.5, 1))

    def test_c2s_0x0d_full_tail_rebuilds_byte_exact(self):
        lo = (9 << 12) | (4 << 9) | (12 << 16)
        fields = {'map_code': 101, 'state_lo': lo, 'event_source_uid': 7, 'f64_1338': 0.5, 'f64_1340': -1.25,
                  'target_uid': 0xF0001, 'pos_x': 812.375, 'pos_y': -64.5, 'target_dx': 0.1, 'target_dy': -0.2,
                  'item_id': 0x0A31}
        raw = P.build('0x42CE94/0x0D', fields, direction='C2S')
        self.assertEqual(len(raw), 83)
        rec = P.parse(0x0D, raw)
        for name in ('f64_1338', 'f64_1340', 'pos_x', 'pos_y', 'target_dx', 'target_dy'):
            self.assertEqual(rec[name], fields[name])
        self.assertEqual(_rebuild(rec), raw)

    def test_0x07_spawn_position_keeps_fraction(self):
        row = {'name': 'TestHero', 'uid': 1, 'pos_x': 400.5, 'pos_y': 300.25}
        raw = P.build(0x07, {'player_count': 1, 'repeat[player_count]': [row]}, receiver_uid=1)
        back = P.parse(0x07, raw, direction='S2C')['repeat[player_count]'][0]
        self.assertEqual((back['pos_x'], back['pos_y']), (400.5, 300.25))


class Directions(unittest.TestCase):
    """A full key resolves only in its own direction (review round 1)."""

    def test_full_keys_are_bound_to_their_direction(self):
        for key, wrong in (('UDP-S2C:0x06', 'S2C'), ('UDP-S2C:0x06', 'UDP-C2S'), ('0x4484CC/0x27', 'S2C'),
                           ('0x470018/0x32', 'S2C'), ('0x42CE94/0x0D', 'UDP-C2S'),
                           ('UDP-C2S:0x4236F5/0x03', 'C2S')):
            with self.subTest(key=key, direction=wrong):
                with self.assertRaises(KeyError):
                    P.spec(key, wrong)
                with self.assertRaises(KeyError):
                    P.variants(key, wrong)
        with self.assertRaises(KeyError):
            P.build('UDP-S2C:0x06', {'uid': 1})                          # default direction is S2C
        with self.assertRaises(KeyError):
            P.parse('0x42CE94/0x0D', b'\x00' * 18, direction='S2C')

    def test_full_keys_resolve_in_their_own_direction(self):
        self.assertEqual(P.spec('UDP-S2C:0x06', 'UDP-S2C')['key'], 'UDP-S2C:0x06')
        self.assertEqual(P.spec('UDP-C2S:0x4236F5/0x03', 'UDP-C2S')['key'], 'UDP-C2S:0x4236F5/0x03')
        self.assertEqual(P.spec('0x4484CC/0x27', 'C2S')['key'], '0x4484CC/0x27')

    def test_opcode_keys_still_resolve_through_direction(self):
        self.assertEqual(P.spec(0x06, 'S2C')['key'], '0x06')
        self.assertEqual(P.spec('0x06', 'UDP-S2C')['key'], 'UDP-S2C:0x06')
        with self.assertRaises(ValueError):
            P.variants(0x06, 'TCP')


class ListCaps(unittest.TestCase):
    """trade-codec guard: item descriptor option lists never exceed 5 words."""

    def _stall_list(self, opt_count, **kw):
        item = {'item_id': 179, 'qty': 1, 'price': 100, 'opt_count': opt_count,
                'repeat[opt_count]': [{'opt': 10 + i} for i in range(opt_count)], 'item_ext': 0}
        return P.build(0x87, {'result': 1, 'item_count': 1, 'owner_uid': 2, 'stall_name': 'shop',
                              'repeat[item_count]': [item]}, **kw)

    def test_table_matches_the_grammars(self):
        self.assertEqual(P.check_list_caps(), [])

    def test_nested_option_count_over_5_is_refused(self):
        self.assertEqual(len(self._stall_list(5)), 1 + 1 + 4 + 25 + 2 + 2 + 4 + 1 + 10 + 2)
        with self.assertRaises(P.PacketError):
            self._stall_list(6)
        self.assertEqual(len(self._stall_list(6, unsafe_counts=True)), 54)   # deliberate overflow tests only

    def test_top_level_counts(self):
        with self.assertRaises(P.PacketError):
            P.build(0x4B, {'item_id': 179, 'opt_count': 6})
        with self.assertRaises(P.PacketError):
            P.build(0x4B, {'item_id': 179, 'opt_count': -1})                 # u8 wrap would send 255
        with self.assertRaises(P.PacketError):
            P.build(0x1D, {'uid': 1, 'item_id': 179, 'stone_count': 7})
        with self.assertRaises(P.PacketError):
            P.build(0x19, {'opt_count': 63})
        self.assertEqual(len(P.build(0x1D, {'uid': 1, 'item_id': 179, 'stone_count': 5})), 4 + 2 + 1 + 10 + 2)

    def test_canonical_options(self):
        self.assertEqual(P.canonical_options([5, 0, 7, 0, 0]), [5])
        self.assertEqual(P.canonical_options([1, 2, 3, 4, 5, 6]), [1, 2, 3, 4, 5])
        self.assertEqual(P.canonical_options([0, 9]), [])
        self.assertEqual(P.canonical_options([]), [])
        opts = P.canonical_options([3437, 12, 0, 99, 1])
        raw = P.build(0x4B, {'item_id': 179, 'count': 1, 'owner_uid': 2, 'opt_count': len(opts),
                             'repeat[opt_count]': [{'opt': o} for o in opts], 'opt_extra': 0})
        self.assertEqual(raw, struct.pack('<HHIBHHH', 179, 1, 2, 2, 3437, 12, 0))


class Text(unittest.TestCase):
    def test_name17(self):
        self.assertEqual(P.name17('TestHero'), b'TestHero'.ljust(17, b'\x00'))
        self.assertEqual(P.name17('A' * 20), b'A' * 16 + b'\x00')
        self.assertEqual(P.name17(b'ab\x00cd'), b'ab'.ljust(17, b'\x00'))
        kr = '한' * 9                                               # 9 hangul = 18 cp949 bytes
        self.assertEqual(P.name17(kr), ('한' * 8).encode('cp949') + b'\x00')
        odd = 'a' + '한' * 8                                         # 17 bytes: never split a char
        self.assertEqual(P.name17(odd), ('a' + '한' * 7).encode('cp949').ljust(17, b'\x00'))

    def test_str17_fields_always_end_in_nul(self):
        raw = P.build(0x62, {'member_name': 'X' * 30})
        self.assertEqual(raw, b'X' * 16 + b'\x00')

    def test_str_fields_are_cp949_and_never_split_a_character(self):
        # Review round 1: a non-latin-1 str raised UnicodeEncodeError, and the forced NUL
        # cut ([:16]) could split a cp949 double-byte character.
        self.assertEqual(P.build(0x62, {'member_name': 'a' + '한' * 8}), P.name17('a' + '한' * 8))
        self.assertEqual(P.build(0x62, {'member_name': ('a' + '한' * 8).encode('cp949')}),
                         ('a' + '한' * 7).encode('cp949').ljust(17, b'\x00'))
        # str[25] stall title: 25 bytes of text -> 23 (the 24th byte would split a character) + NUL pad
        raw = P.build(0x87, {'result': 1, 'item_count': 1, 'owner_uid': 2, 'stall_name': 'b' + '글' * 12,
                             'repeat[item_count]': [{'item_id': 179, 'qty': 1}]})
        self.assertEqual(raw[6:31], ('b' + '글' * 11).encode('cp949') + b'\x00\x00')

    def test_str_fields_inside_repeats_are_cut_without_touching_the_caller(self):
        row = {'name': 'N' * 20, 'uid': 1}
        fields = {'player_count': 1, 'repeat[player_count]': [row]}
        raw = P.build(0x07, fields, receiver_uid=1)
        self.assertEqual(raw[1:18], b'N' * 16 + b'\x00')
        self.assertEqual(row['name'], 'N' * 20)                         # caller's nested dict untouched

    def test_parsed_cp949_name_rebuilds_byte_exact(self):
        raw = '한글'.encode('cp949').ljust(17, b'\x00')
        rec = P.parse(0x62, raw, direction='S2C', assume={'messenger_room_id != 0': True})
        self.assertEqual(P.build(0x62, dict(rec)), raw)                  # latin-1 str -> the same bytes
        self.assertEqual(P.to_bytes('\xc7\xd1'), b'\xc7\xd1')
        self.assertEqual(P.to_bytes('한'), '한'.encode('cp949'))

    def test_0x16_chat_line(self):
        raw = P.build(0x16, {'sender_name': P.name17('TestHero'), 'text': b'hi'})
        self.assertEqual(raw, P.name17('TestHero') + b'\x02hi')          # no count byte, no lost char
        self.assertEqual(len(P.build(0x16, {'sender_name': 'a', 'text': 'x' * 200})), 17 + 1 + 60)
        with self.assertRaises(P.PacketError):
            P.build(0x16, {'sender_name': 'a', 'text_len': 61, 'text': 'x' * 61})
        self.assertEqual(len(P.build(0x16, {'text_len': 61, 'text': 'x' * 61}, unsafe_text=True)), 79)

    def test_s1_14_limits(self):
        self.assertEqual(P.text_limit(0x16), 60)
        self.assertEqual(P.text_limit(0x61), 58)
        self.assertEqual(P.text_limit(0x15), 88)
        self.assertEqual(P.text_limit(0x90), 87)
        self.assertEqual(P.text_limit(0x91), 87)
        name16 = 'N' * 16
        self.assertEqual(P.text_limit(0x0A, {'channel': 0x65, 'sender_name': name16}), 60)
        self.assertEqual(P.text_limit(0x0A, {'channel': 101, 'sender_name': 'N' * 16}), 60)
        self.assertEqual(P.text_limit(0x0A, {'channel': 3, 'sender_name': name16}), 51)
        self.assertEqual(P.text_limit(0x0A, {'channel': 200, 'sender_name': name16}), 49)   # spec example
        self.assertEqual(P.text_limit(0x09, {'status': 0x65, 'target_name': name16}), 60)
        self.assertEqual(P.text_limit(0x09, {'status': 200, 'target_name': name16}), 51)    # spec example
        self.assertIsNone(P.text_limit(0x09, {'status': 0x66}))
        self.assertIsNone(P.text_limit(0x03))

    def test_whisper_packets(self):
        self.assertEqual(len(P.build(0x09, {'status': 0x66, 'target_name': 'Bob'})), 18)
        raw = P.build(0x0A, {'channel': 3, 'sender_name': 'N' * 16, 'message': 'm' * 80})
        self.assertEqual(len(raw), 19 + 51)
        self.assertEqual(raw[18], 51)
        self.assertEqual(len(P.build(0x15, {'msg_type': 2, 'text': '[Announce] ' + 'x' * 100})), 2 + 88)
        self.assertEqual(len(P.build(0x90, {'text': 'Bob : hello'})), 12)

    def test_clamp_text_cuts_at_nul_and_char_boundary(self):
        self.assertEqual(P.clamp_text(0x61, b'hi\x00there'), b'hi')
        self.assertEqual(P.clamp_text(0x61, '한' * 40), ('한' * 29).encode('cp949'))


class Parse(unittest.TestCase):
    def test_every_manual_capture(self):
        import test_protocol
        for op, hx in test_protocol.MANUAL_C2S:
            with self.subTest(opcode=hex(op)):
                rec = P.parse(op, hexbytes(hx) if hx else b'')
                self.assertTrue(rec.key.endswith(f'/0x{op:02X}'))
                self.assertIn(rec.key, rec.candidates)

    def test_manual_capture_fields(self):
        self.assertEqual(dict(P.parse(0x03, hexbytes('02 68 69'))), {'msg_len': 2, 'message': b'hi'})
        rec = P.parse(0x16, hexbytes('1A 00'))
        self.assertEqual((rec.key, rec['quest_id']), ('0x47734D/0x16', 26))
        self.assertEqual(P.parse(0x0F, hexbytes('B3 00 00 00 00'))['item_id'], 179)
        rec = P.parse('0x7E', hexbytes('17 00 00 00'))
        self.assertEqual((rec.name, rec['portal_line_index']), ('PortalEnterRequest', 23))
        self.assertEqual(P.parse(0x38, b'').name, 'BattlefieldInfoRequest')
        self.assertEqual(P.parse(0x63, b'').name, 'CardDeckListRequest')

    def test_logged_c2s_corpus_parses_and_rebuilds_byte_exact(self):
        for op, hx in LOGGED_C2S:
            payload = hexbytes(hx) if hx else b''
            with self.subTest(opcode=hex(op), size=len(payload)):
                rec = P.parse(op, payload)                               # raises on any mismatch
                self.assertEqual(_rebuild(rec), payload)

    def test_every_manual_capture_rebuilds_byte_exact(self):
        import test_protocol
        for op, hx in test_protocol.MANUAL_C2S:
            payload = hexbytes(hx) if hx else b''
            with self.subTest(opcode=hex(op)):
                self.assertEqual(_rebuild(P.parse(op, payload)), payload)

    def test_live_log_c2s_is_reported_not_enforced(self):
        """server_history.log grows with every manual session, so a new opcode or shape
        captured there is printed as a warning (as test_protocol reports it) instead of
        failing this suite; LOGGED_C2S above is the fixed regression corpus."""
        import test_protocol
        log = os.path.join(HERE, 'server_history.log')
        if not os.path.exists(log):
            self.skipTest('server_history.log not present')
        n, problems = 0, []
        for direction, op, payload in test_protocol.log_packets(log):
            if direction != 'C2S':
                continue
            n += 1
            try:
                if _rebuild(P.parse(op, payload)) != payload:
                    problems.append(f'0x{op:02X} {len(payload)}B does not rebuild byte-exact')
            except (P.PacketError, KeyError) as e:
                problems.append(f'0x{op:02X} {len(payload)}B: {str(e)[:160]}')
        if problems:
            unique = list(dict.fromkeys(problems))
            sys.stderr.write(f'\nWARNING server_history.log: {len(problems)} of {n} C2S packet(s) not '
                             f'covered by the spec (add a capture to LOGGED_C2S once specced):\n  '
                             + '\n  '.join(unique[:10]) + '\n')

    def test_same_length_send_sites_are_reported(self):
        rec = P.parse(0x04, b'\x01')
        self.assertEqual(set(rec.candidates), {'0x44840E/0x04', '0x4484CC/0x04'})
        self.assertEqual(P.parse('0x4484CC/0x04', b'\x02').key, '0x4484CC/0x04')

    def test_variable_send_sites_resolve_by_length(self):
        self.assertEqual(P.parse(0x15, struct.pack('<H', 5)).candidates, ('0x44C239/0x15', '0x44C2B3/0x15'))
        trap = P.parse(0x15, struct.pack('<HHH', 0x0A31, 3, 4))
        self.assertEqual((trap.key, trap['pos_y']), ('0x44C239/0x15', 4))

    def test_client_state_branch_found_by_length(self):
        plain = P.parse('0x469D9C/0x3C', struct.pack('<HHBH', 10, 1, 0, 0))
        self.assertIsNone(plain.assume)
        socketed = P.parse('0x469D9C/0x3C', struct.pack('<HHBHH', 179, 1, 1, 3437, 0))
        self.assertEqual(socketed.assume, {'item_type == 1': True})
        self.assertEqual(socketed['repeat[socket_count]'], [{'socket_stone_id': 3437}])

    def test_wrong_length_is_refused(self):
        with self.assertRaises(P.ParseError):
            P.parse(0x7E, b'\x17\x00\x00')
        with self.assertRaises(P.ParseError):
            P.parse(0x7E, b'\x17\x00\x00\x00\x00')
        self.assertEqual(P.parse(0x7E, b'\x17\x00\x00\x00\x00', allow_trailing=True)['portal_line_index'], 23)
        with self.assertRaises(KeyError):
            P.parse(0xFE, b'')

    def test_build_parse_roundtrip_c2s_key(self):
        raw = P.build('0x42CE94/0x0D', {'map_code': 102, 'state_lo': 5 << 12, 'event_source_uid': 9},
                      direction='C2S')
        self.assertEqual(len(raw), 22)
        self.assertEqual(P.parse(0x0D, raw)['event_source_uid'], 9)


class _FakeServer:
    def __init__(self):
        self.calls = []

    def _send_encrypted(self, sock, session, opcode, payload=b'', use_by_array=False):
        self.calls.append((sock, opcode, payload, use_by_array))


def _import_server_safely():
    """Import windslayer_server with a throwaway cwd: its module-level logging opens
    server_live.log with mode='w' relative to cwd, which would wipe a live server's log."""
    if 'windslayer_server' in sys.modules:
        return sys.modules['windslayer_server']
    old = os.getcwd()
    os.chdir(tempfile.mkdtemp(prefix='ws_test_'))
    try:
        with redirect_stdout(io.StringIO()):
            import windslayer_server
    finally:
        os.chdir(old)
    import logging
    logging.getLogger('WS').setLevel(logging.WARNING)
    return windslayer_server


class Send(unittest.TestCase):
    def test_by_array_follows_the_receiving_session(self):
        srv = _FakeServer()
        P.send(srv, 'sockA', {'no_enc': True, 'account_id': 1}, 0x59, {'slot': 1, 'progress': 3})
        P.send(srv, 'sockB', {'no_enc': False, 'account_id': 1}, '0x59', {'slot': 2, 'progress': 1})
        P.send(srv, 'sockC', {'account_id': 1}, 0x59, {'slot': 3, 'progress': 0})
        self.assertEqual(srv.calls, [('sockA', 0x59, b'\x01\x03', True),
                                     ('sockB', 0x59, b'\x02\x01', False),
                                     ('sockC', 0x59, b'\x03\x00', True)])

    def test_receiver_uid_comes_from_the_session(self):
        srv = _FakeServer()
        P.send(srv, None, {'account_id': 1}, 0x76, {'target_uid': 1})
        P.send(srv, None, {'uid': 2, 'account_id': 1}, 0x76, {'target_uid': 1})
        self.assertEqual([len(c[2]) for c in srv.calls], [18, 12])
        with self.assertRaises(P.MissingAssume):
            P.send(srv, None, {'account_id': 0}, 0x76, {'target_uid': 1})
        self.assertEqual(len(srv.calls), 2)

    def test_only_tcp_s2c_keys_are_sent(self):
        # Review round 1: UDP-S2C:0x06 went out as TCP 0x06 EntityRemove and the C2S
        # FriendDelete body as TCP 0x32 (roadmap 4.2 never-inject hazards).
        srv = _FakeServer()
        for key in ('UDP-S2C:0x06', '0x470018/0x32', '0x4484CC/0x27'):
            with self.subTest(key=key), self.assertRaises(KeyError):
                P.send(srv, None, {'account_id': 1}, key, {'uid': 1})
        with self.assertRaises(P.PacketError):
            P.send(srv, None, {'account_id': 1}, 0x06, {'uid': 2}, direction='UDP-S2C')
        self.assertEqual(srv.calls, [])

    def test_real_send_encrypted_over_socketpair(self):
        W = _import_server_safely()
        from cencmsg import CEncMsg
        server = W.GameServer.__new__(W.GameServer)                   # no accounts.json, no sockets
        server.sessions = {}
        a, b = socket.socketpair()
        try:
            session = {'enc': CEncMsg(), 'send_seq': 2, 'no_enc': True, 'account_id': 1, 'sock': a}
            session['enc'].set_code_key(0x1234)
            payload = P.send(server, a, session, 0x61, {'sender_name': 'Alice', 'message': b'hello'})
            b.settimeout(2)
            pkt = bytearray(b.recv(4096))
            self.assertEqual(len(pkt), 8 + 1 + 23)
            self.assertTrue(struct.unpack_from('<I', pkt)[0] & W.NO_ENCODE_FLAG)
            self.assertTrue(CEncMsg().decode_by_array(pkt))
            self.assertEqual(pkt[8], 0x61)
            self.assertEqual(bytes(pkt[9:]), payload)
            self.assertEqual(dict(P.parse(0x61, payload, direction='S2C', assume={ROOM: True})),
                             {'sender_name': 'Alice', 'msg_len': 5, 'message': b'hello'})
            self.assertEqual(session['send_seq'], 3)
        finally:
            a.close()
            b.close()


if __name__ == '__main__':
    unittest.main(verbosity=1)
