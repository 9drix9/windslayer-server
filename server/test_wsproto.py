#!/usr/bin/env python3
"""Regression tests for wsproto (run: python test_wsproto.py)."""
import json
import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from wsproto import Grammar, GrammarError  # noqa: E402

SPEC = os.path.join(HERE, 'protocol_spec.json')


_SPECS = None


def all_specs():
    global _SPECS
    if _SPECS is None:
        with open(SPEC, encoding='utf-8') as f:
            _SPECS = json.load(f)['specs']
    return _SPECS


def spec(key):
    return next(s for s in all_specs() if s['key'] == key)


class Expressions(unittest.TestCase):
    def test_hex_literal_condition_true(self):
        g = Grammar('u16 buff_id\nif(buff_id >= 0x0A31 && buff_id <= 0x0A3B) {\n  u16 param\n}')
        raw = g.encode({'buff_id': 0x0A31, 'param': 7})
        self.assertEqual(raw, struct.pack('<HH', 0x0A31, 7))
        self.assertEqual(g.decode(raw), {'buff_id': 0x0A31, 'param': 7})
        self.assertEqual(g.client_state_exprs(), [])

    def test_hex_literal_condition_false(self):
        g = Grammar('u16 buff_id\nif(buff_id >= 0x0A31) { u16 param }')
        self.assertEqual(g.encode({'buff_id': 5, 'param': 7}), struct.pack('<H', 5))

    def test_bit_mask_with_hex(self):
        g = Grammar('u32 state\nif(((state >> 12) & 0xF) != 0) {\n  u32 uid\n}')
        self.assertEqual(len(g.encode({'state': 0x3000, 'uid': 9})), 8)
        self.assertEqual(len(g.encode({'state': 0x0FFF, 'uid': 9})), 4)

    def test_c_cast_stripped(self):
        g = Grammar('u8 slot\nif((int)slot - 1 < 3) { u8 progress }')
        self.assertEqual(g.encode({'slot': 1, 'progress': 3}), bytes([1, 3]))

    def test_integer_division(self):
        g = Grammar('u8 n\nrepeat(n/2) { u8 b }')
        rec = g.decode(bytes([5, 1, 2]))
        self.assertEqual(len(rec['repeat[n/2]']), 2)

    def test_logical_not(self):
        g = Grammar('u8 f\nif(!f) { u8 x }')
        self.assertEqual(g.encode({'f': 0, 'x': 4}), bytes([0, 4]))
        self.assertEqual(g.encode({'f': 1, 'x': 4}), bytes([1]))


class ClientState(unittest.TestCase):
    def test_unknown_condition_defaults_false_and_is_listed(self):
        g = Grammar('u32 uid\nif(no entity with uid in scene list) {\n  stop\n}\nu8 x')
        self.assertEqual(g.client_state_exprs(), ['no entity with uid in scene list'])
        self.assertEqual(g.encode({'uid': 1, 'x': 2}), struct.pack('<IB', 1, 2))

    def test_assume_selects_branch(self):
        g = Grammar('u8 a\nif(scene_local_player != 0) { u16 hp } else { u8 z }')
        self.assertEqual(g.encode({'a': 1, 'hp': 300, '__assume__': {'scene_local_player != 0': True}}),
                         bytes([1]) + struct.pack('<H', 300))
        self.assertEqual(g.encode({'a': 1, 'z': 9}), bytes([1, 9]))
        self.assertEqual(g.decode(bytes([1]) + struct.pack('<H', 300), assume={'scene_local_player != 0': True}),
                         {'a': 1, 'hp': 300})


    def test_unresolved_collects_only_unstated_conditions(self):
        g = Grammar('u8 a\nif(room != 0) { u8 b }\nif(a == 1) { u8 c }\nif(spawned) { u8 d }')
        unresolved = []
        g.encode({'a': 1}, assume={'spawned': True}, unresolved=unresolved)
        self.assertEqual(unresolved, ['room != 0'])
        unresolved = []
        g.decode(bytes([1, 7]), unresolved=unresolved)
        self.assertEqual(unresolved, ['room != 0', 'spawned'])


class Threads(unittest.TestCase):
    def test_shared_grammar_decodes_concurrently(self):
        # packets.py caches one Grammar per key for every server thread; the read
        # cursor must not be shared between concurrent decodes.
        import threading
        g = Grammar('u8 n\nrepeat(n) { u32 v }')
        payloads = [bytes([n]) + b''.join(struct.pack('<I', n * 1000 + i) for i in range(n)) for n in range(1, 40)]
        errors = []

        def worker(p):
            try:
                for _ in range(200):
                    rec = g.decode(p)
                    n = p[0]
                    if [r['v'] for r in rec['repeat[n]']] != [n * 1000 + i for i in range(n)]:
                        errors.append(n)
            except Exception as e:                              # noqa: BLE001
                errors.append(repr(e))
        threads = [threading.Thread(target=worker, args=(p,)) for p in payloads]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


class Strings(unittest.TestCase):
    def test_str_is_always_nul_terminated(self):
        g = Grammar('str[4] name')
        self.assertEqual(g.encode({'name': 'abcdef'}), b'abc\x00')
        self.assertEqual(g.encode({'name': 'ab'}), b'ab\x00\x00')

    def test_length_prefixed_str_keeps_every_byte(self):
        # str[text_len] (0x15/0x16/0x90/0x91, 0x01 notice) has no NUL on the wire; the
        # client zero-fills its buffer. A forced NUL would drop the last character.
        g = Grammar('u8 text_len\nstr[text_len] text')
        self.assertEqual(g.encode({'text_len': 5, 'text': 'hello'}), b'\x05hello')
        self.assertEqual(g.decode(b'\x05hello'), {'text_len': 5, 'text': 'hello'})

    def test_bytes_are_raw(self):
        g = Grammar('bytes[4] blob')
        self.assertEqual(g.encode({'blob': b'\x01\x02\x03\x04\x05'}), b'\x01\x02\x03\x04')

    def test_variable_length_str(self):
        g = Grammar('u16 n\nstr[n] text')
        self.assertEqual(g.decode(b'\x03\x00hi\x00'), {'n': 3, 'text': 'hi'})


class Scalars(unittest.TestCase):
    def test_unsigned_wraps_like_c(self):
        self.assertEqual(Grammar('u16 v').encode({'v': -1}), b'\xff\xff')

    def test_floats_keep_fraction_and_sign(self):
        # Review round 1 (major): encode used int() for f32/f64, writing 5.5 as 5.0.
        g = Grammar('f64 x\nf32 y\nf64 z\nf32 w')
        rec = {'x': 5.5, 'y': 1.25, 'z': -123.456789, 'w': -0.5}
        raw = g.encode(rec)
        self.assertEqual(raw, struct.pack('<dfdf', 5.5, 1.25, -123.456789, -0.5))
        self.assertEqual(g.decode(raw), rec)

    def test_f32_rounds_like_c_float(self):
        g = Grammar('f32 v')
        raw = g.encode({'v': 0.1})
        self.assertEqual(raw, struct.pack('<f', 0.1))
        self.assertEqual(g.decode(raw)['v'], struct.unpack('<f', struct.pack('<f', 0.1))[0])

    def test_float_fields_accept_ints_and_default_to_zero(self):
        g = Grammar('f64 x\nf64 y')
        self.assertEqual(g.encode({'x': 3}), struct.pack('<dd', 3.0, 0.0))

    def test_underflow_and_trailing(self):
        g = Grammar('u32 v')
        with self.assertRaises(GrammarError):
            g.decode(b'\x01\x02')
        with self.assertRaises(GrammarError):
            g.decode(b'\x01\x02\x03\x04\x05')


class Parsing(unittest.TestCase):
    def test_multiple_statements_per_line(self):
        g = Grammar('repeat(2) { u16 item  repeat(6) { u16 opt } }')
        self.assertEqual(len(g.encode({})), 2 * 14)

    def test_name_starting_with_type_keyword(self):
        g = Grammar('f64 f64_1338\nu8 u8_count')
        self.assertEqual(len(g.encode({})), 9)

    def test_else_if_chain(self):
        g = Grammar('u8 k\nif(k == 1) { u8 a } else if(k == 2) { u16 b } else { u32 c }')
        self.assertEqual(len(g.encode({'k': 1})), 2)
        self.assertEqual(len(g.encode({'k': 2})), 3)
        self.assertEqual(len(g.encode({'k': 3})), 5)


@unittest.skipUnless(os.path.exists(SPEC), 'protocol_spec.json not present')
class RealSpecs(unittest.TestCase):
    def test_c2s_0x0d_tails_are_read(self):
        s = spec('0x42CE94/0x0D')
        g = Grammar(s['grammar'])
        base = {'realtime_delta_ms': 0, 'map_code': 101, 'logic_elapsed_ms': 30, 'state_hi': 0}
        self.assertEqual(len(g.encode({**base, 'state_lo': 0})), 18)
        with_event = g.encode({**base, 'state_lo': 0x1000, 'event_source_uid': 7})
        self.assertEqual(len(with_event), 22)
        self.assertEqual(g.decode(with_event)['event_source_uid'], 7)

    # --- roadmap S1-01 / 1.2.1 regression vectors (hex literals, casts) ------------
    def test_c2s_0x0d_attack_event_tail_is_22_bytes(self):
        g = Grammar(spec('0x42CE94/0x0D')['grammar'])
        raw = g.encode({'map_code': 102, 'logic_elapsed_ms': 30, 'state_lo': 5 << 12, 'event_source_uid': 9})
        self.assertEqual(len(raw), 22)
        self.assertEqual(g.decode(raw)['event_source_uid'], 9)          # zero trailing bytes

    def test_c2s_0x0d_every_tail_length(self):
        # world-codec-hexfix: ae = (state_lo >> 12) & 0xF, vb = (state_lo >> 9) & 7,
        # ie = (state_lo >> 16) & 0xF. ae adds event_source_uid (+4), ae 7/9 with vb 4 the
        # f64 pair (+16), ie the target block (+43), ie 12 the item_id (+2).
        g = Grammar(spec('0x42CE94/0x0D')['grammar'])
        floats = {'f64_1338': 1.5, 'f64_1340': -2.25, 'pos_x': 123.75, 'pos_y': -0.5,
                  'target_dx': 0.125, 'target_dy': -7.0625}
        for (ae, vb, ie), size in {(0, 0, 0): 18, (5, 0, 0): 22, (9, 3, 0): 22, (9, 4, 0): 38,
                                   (7, 4, 0): 38, (0, 0, 1): 61, (0, 0, 12): 63,
                                   (9, 4, 12): 83}.items():
            with self.subTest(ae=ae, vb=vb, ie=ie):
                lo = (ae << 12) | (vb << 9) | (ie << 16)
                rec = {'realtime_delta_ms': 1, 'map_code': 101, 'logic_elapsed_ms': 30, 'state_lo': lo,
                       'state_hi': 0, 'event_source_uid': 7, 'target_uid': 0xF0001, 'flag_8db': 1,
                       'flag_8e7': 0, 'timer_dac': 99, 'target_action_event': 4, 'item_id': 0x0A31,
                       **floats}
                raw = g.encode(rec)
                self.assertEqual(len(raw), size)
                back = g.decode(raw)                                     # exact: zero trailing bytes
                self.assertEqual(g.encode(back), raw)
                for name, value in floats.items():
                    if name in back:
                        self.assertEqual(back[name], value)
                if ie == 12:
                    self.assertEqual(back['item_id'], 0x0A31)

    def test_s2c_0x1b_action_and_move_tails_are_36_bytes(self):
        g = Grammar(spec('0x1B')['grammar'])
        lo = (3 << 12) | (1 << 16)                                       # action 3 + move flag
        rec = {'uid': 2, 'state_blob': struct.pack('<II', lo, 0), 'state_blob_u32_0': lo,
               'target_uid': 7, 'pos_x': 10.0, 'pos_y': 20.0}
        self.assertEqual(len(g.encode(rec)), 36)

    def test_s2c_0x1b_every_tail_length(self):
        g = Grammar(spec('0x1B')['grammar'])
        for lo, size in {0: 16, 3 << 12: 20, (7 << 12) | (3 << 9): 20, 1 << 16: 32,
                         (3 << 12) | (1 << 16): 36, (9 << 12) | (4 << 9): 36,
                         (7 << 12) | (4 << 9) | (1 << 16): 52}.items():
            with self.subTest(state_blob_u32_0=hex(lo)):
                rec = {'uid': 2, 'hold_ms': 5, 'state_blob': struct.pack('<II', lo, 0), 'state_blob_u32_0': lo,
                       'target_uid': 7, 'target_x': -1.5, 'target_y': 2.75, 'pos_x': 123.75, 'pos_y': -0.5}
                raw = g.encode(rec)
                self.assertEqual(len(raw), size)
                if lo & (1 << 16):
                    self.assertEqual(struct.unpack_from('<dd', raw, size - 16), (123.75, -0.5))

    def test_s2c_0x04_buff_params_only_for_0a31_to_0a3b(self):
        g = Grammar(spec('0x04')['grammar'])

        def size(buff_id):
            row = {'name': 'TestHero', 'uid': 1, 'buff_count': 1, 'pos_x': 100.5, 'pos_y': -3.25,
                   'repeat[buff_count]': [{'buff_skill_id': buff_id, 'buff_duration': 5000,
                                           'buff_param_a': 1, 'buff_param_b': 2}]}
            raw = g.encode({'player_count': 1, 'repeat[player_count]': [row]})
            back = g.decode(raw)['repeat[player_count]'][0]
            self.assertEqual((back['pos_x'], back['pos_y']), (100.5, -3.25))
            return len(raw)
        self.assertEqual(size(0x0A31) - size(0x0A30), 4)
        self.assertEqual(size(0x0A3B) - size(0x0A3C), 4)
        self.assertEqual(size(0x0A35), size(0x0A31))

    def test_s2c_0x07_buff_param_block_is_379_bytes(self):
        g = Grammar(spec('0x07')['grammar'])
        row = {'name': 'TestHero', 'uid': 1, 'buff_count': 1,
               'repeat[buff_count]': [{'buff_skill_id': 0x0A32, 'buff_duration': 5000,
                                       'buff_param_a': 1, 'buff_param_b': 2}]}
        raw = g.encode({'player_count': 1, 'repeat[player_count]': [row]})
        self.assertEqual(len(raw), 379)
        self.assertEqual(g.decode(raw)['repeat[player_count]'][0]['repeat[buff_count]'][0]['buff_param_b'], 2)

    def test_s2c_0x2e_roster_row_with_buff_is_331_bytes(self):
        g = Grammar(spec('0x2E')['grammar'])
        row = {'name': 'TestHero', 'uid': 1, 'buff_count': 1,
               'repeat[buff_count]': [{'buff_id': 0x0A32, 'buff_remaining': 1, 'buff_param1': 1, 'buff_param2': 2}]}
        self.assertEqual(len(g.encode({'player_count': 1, 'repeat[player_count]': [row]})), 331)

    def test_hex_result_codes_pick_the_right_layout(self):
        self.assertEqual(len(Grammar(spec('0x0C')['grammar']).encode({'result': 0x0B, 'name': 'Bob'})), 24)
        self.assertEqual(len(Grammar(spec('0x7A')['grammar']).encode({'result': 0x64, 'mentor_name': 'Bob'})), 22)
        g09 = Grammar(spec('0x09')['grammar'])
        raw = g09.encode({'status': 0x66, 'target_name': 'Alice'})
        self.assertEqual(len(raw), 18)
        self.assertEqual(g09.decode(raw)['target_name'], 'Alice')

    def test_s2c_0x72_hair_dye_emits_hair_code(self):
        g = Grammar(spec('0x72')['grammar'])
        raw = g.encode({'player_uid': 2, 'item_id': 0x0D50, 'hair_code': 0x1234},
                       assume={'player_uid != local_player_uid': True})
        self.assertEqual(raw, struct.pack('<IHH', 2, 0x0D50, 0x1234))

    def test_c2s_0x15_trap_variant_decodes_ground_point(self):
        g = Grammar(spec('0x44C239/0x15')['grammar'])
        self.assertEqual(g.decode(struct.pack('<HHH', 0x0A31, 300, 400)),
                         {'skill_id': 0x0A31, 'pos_x': 300, 'pos_y': 400})

    def test_c2s_0x06_gm_stop_and_kick_decode(self):
        g = Grammar(spec('0x444C91/0x06')['grammar'])
        self.assertEqual(g.decode(bytes([0x07, 0x03])), {'gm_subcmd': 7, 'arg': 3})
        self.assertEqual(g.decode(bytes([0x0C, 0x01])), {'gm_subcmd': 12, 'arg': 1})
        self.assertEqual(g.decode(bytes([0x02])), {'gm_subcmd': 2})

    def test_s2c_0x59_quest_progress_is_two_bytes(self):
        # arch-spec-errata (b): grammar is if(slot < 4); the cast strip covers the old text too.
        s = spec('0x59')
        self.assertEqual(Grammar(s['grammar']).encode({'slot': 1, 'progress': 3}), bytes([1, 3]))
        self.assertEqual(Grammar(s['grammar']).encode({'slot': 4, 'progress': 3}), bytes([4]))
        old = s['spec_errata'][0]['old']
        self.assertEqual(Grammar(old).encode({'slot': 1, 'progress': 3}), bytes([1, 3]))

    def test_every_spec_grammar_roundtrips_empty_record(self):
        for s in all_specs():
            if s.get('grammar') is None:
                continue
            with self.subTest(key=s['key']):
                g = Grammar(s['grammar'])
                g.decode(g.encode({}))


if __name__ == '__main__':
    unittest.main(verbosity=1)
