#!/usr/bin/env python3
"""
test_harness.py - offline tests for the live-harness input tooling (roadmap 1.10 F9:
chat_mail_gm-harness-typing, item_inventory-harness-drag-dblclick, lc-harness-charselect,
admin --to/--state targeting)

No game client, no window and no admin port are touched: every function that would send
input or open a socket is replaced by a recorder. VkKeyScanW / MapVirtualKeyW are real
(read-only layout queries).
"""
import io
import json
import os
import sys
import time
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import wsview as V  # noqa: E402
import wsdev as D  # noqa: E402


class _Patch:
    """Temporarily replace module attributes."""

    def __init__(self, module, **attrs):
        self.module, self.attrs, self.saved = module, attrs, {}

    def __enter__(self):
        for k, v in self.attrs.items():
            self.saved[k] = getattr(self.module, k)
            setattr(self.module, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.module, k, v)


class KeyTokens(unittest.TestCase):
    def test_named_oem_tokens(self):
        # chat_mail_gm B14: '/' used to map to 0x2F (VK_HELP, no scancode)
        self.assertEqual((V._vk_of('slash'), V._vk_of('minus'), V._vk_of('period')), (0xBF, 0xBD, 0xBE))
        for tok in ('slash', 'minus', 'period', 'comma', 'backspace', 'a', '7', 'enter', 'f1'):
            with self.subTest(tok=tok):
                self.assertNotEqual(V.user32.MapVirtualKeyW(V._vk_of(tok), 0), 0, 'no scancode')

    def test_single_symbol_tokens_use_the_layout(self):
        for ch in '/-.':
            with self.subTest(ch=ch):
                vk = V._vk_of(ch)
                self.assertNotIn(vk, (0x2F, 0x2D, 0x2E))     # VK_HELP / VK_INSERT / VK_DELETE
                self.assertNotEqual(V.user32.MapVirtualKeyW(vk, 0), 0)

    def test_letters_digits_unchanged(self):
        # wsdev auto-login types account names through _vk_of
        self.assertEqual([V._vk_of(c) for c in 'test19'], [0x54, 0x45, 0x53, 0x54, 0x31, 0x39])

    def test_chords(self):
        self.assertEqual(V.parse_chord('shift+a'), ([V.VK_SHIFT], 0x41))
        self.assertEqual(V.parse_chord('ctrl+shift+slash'), ([V.VK_CONTROL, V.VK_SHIFT], 0xBF))
        self.assertEqual(V.parse_chord('enter'), ([], 0x0D))
        mods, vk = V.parse_chord('?')                         # a shifted symbol brings its shift
        self.assertIn(V.VK_SHIFT, mods)
        with self.assertRaises(ValueError):
            V.parse_chord('hyper+a')
        with self.assertRaises(ValueError):
            V._vk_of('nonsense')

    def test_key_command_validates_before_sending(self):
        sent = []
        with _Patch(V, chord_tap=lambda mods, vk: sent.append((mods, vk)), _find_hwnd=lambda: None):
            out = io.StringIO()
            with redirect_stdout(out):
                V.cmd_key(['slash', 'bogus+x'])
            self.assertEqual(sent, [])
            self.assertIn('unknown modifier', out.getvalue())
            with redirect_stdout(io.StringIO()):
                V.cmd_key(['slash', 'w', 'shift+a'])
        self.assertEqual(sent, [([], 0xBF), ([], 0x57), ([V.VK_SHIFT], 0x41)])


class Typing(unittest.TestCase):
    def test_plan_text_shift_for_capitals_and_symbols(self):
        plan = V.plan_text('/w Bob hi-50.')
        self.assertTrue(all(kind == 'key' for kind, *_ in plan))
        by_char = {ch: (vk, mods) for _, vk, mods, ch in plan}
        self.assertEqual(by_char['B'], (0x42, [V.VK_SHIFT]))
        self.assertEqual(by_char['b'], (0x42, []))
        self.assertEqual(by_char[' '], (0x20, []))
        for ch in '/-.':
            self.assertEqual(by_char[ch][1], [], f'{ch} needs no modifier on this layout')
        self.assertEqual(''.join(ch for *_, ch in plan), '/w Bob hi-50.')

    def test_untypeable_characters_fall_back_to_unicode(self):
        plan = V.plan_text('a한')                         # Hangul needs an IME
        self.assertEqual(plan[1][0], 'unicode')

    def test_type_text_sends_one_press_per_character(self):
        taps, uni = [], []
        with _Patch(V, chord_tap=lambda mods, vk: taps.append((tuple(mods), vk)),
                    _send_unicode=lambda ch, up: uni.append((ch, up))):
            fallbacks = V.type_text('Hi!한', delay=0)
        self.assertEqual(taps[0], ((V.VK_SHIFT,), 0x48))
        self.assertEqual(taps[1], ((), 0x49))
        self.assertIn(V.VK_SHIFT, taps[2][0])
        self.assertEqual(fallbacks, ['한'])
        self.assertEqual(uni, [('한', False), ('한', True)])

    def test_parse_type_args(self):
        self.assertEqual(V.parse_type_args(['--enter', '/manner', 'test', '-50']), ('/manner test -50', True, 0.03))
        self.assertEqual(V.parse_type_args(['--delay', '80', 'hi']), ('hi', False, 0.08))
        self.assertEqual(V.parse_type_args(['--', '--enter']), ('--enter', False, 0.03))


class _FakeUser32:
    def __init__(self, log):
        self.log = log

    def SetCursorPos(self, x, y):
        self.log.append(('pos', x, y))


class Mouse(unittest.TestCase):
    def record(self, fn, *args):
        log = []
        with _Patch(V, user32=_FakeUser32(log), _mouse_event=lambda f: log.append(('btn', f))):
            t0 = time.monotonic()
            fn(*args)
            elapsed = time.monotonic() - t0
        return log, elapsed

    def test_rclick_is_right_down_up(self):
        log, _ = self.record(V.mouse_rclick_screen, 10, 20)
        self.assertEqual(log, [('pos', 10, 20), ('btn', V.MOUSEEVENTF_RIGHTDOWN), ('btn', V.MOUSEEVENTF_RIGHTUP)])

    def test_dclick_is_two_clicks_inside_the_double_click_time(self):
        log, elapsed = self.record(V.mouse_dclick_screen, 30, 40)
        down, up = V.MOUSEEVENTF_LEFTDOWN, V.MOUSEEVENTF_LEFTUP
        self.assertEqual(log, [('pos', 30, 40), ('btn', down), ('btn', up), ('btn', down), ('btn', up)])
        self.assertLess(elapsed, V.ctypes.windll.user32.GetDoubleClickTime() / 1000.0 * 0.6)

    def test_drag_presses_moves_and_releases_at_target(self):
        log, _ = self.record(V.mouse_drag_screen, 100, 200, 160, 230)
        self.assertEqual(log[0], ('pos', 100, 200))
        self.assertEqual(log[1], ('btn', V.MOUSEEVENTF_LEFTDOWN))
        self.assertEqual(log[-2], ('pos', 160, 230))
        self.assertEqual(log[-1], ('btn', V.MOUSEEVENTF_LEFTUP))
        self.assertGreater(len([e for e in log if e[0] == 'pos']), 5)
        self.assertEqual(V.drag_path(0, 0, 10, 5, 5)[-1], (10, 5))

    def test_commands_are_registered(self):
        with open(os.path.join(HERE, 'wsview.py'), encoding='utf-8') as f:
            src = f.read()
        for name in ('type', 'dclick', 'rclick', 'drag'):
            self.assertIn(f"'{name}': cmd_{name}", src)


class AdminTargeting(unittest.TestCase):
    def test_pop_target_opts(self):
        self.assertEqual(D.pop_target_opts(['59', '--to', 'test', '01', '--state', 'select', '03']),
                         ('test', 'select', ['59', '01', '03']))
        self.assertEqual(D.pop_target_opts(['59']), (None, None, ['59']))
        for bad in (['--to'], ['--state', 'everyone'], ['--to', '--state', 'all']):
            with self.subTest(args=bad), self.assertRaises(ValueError):
                D.pop_target_opts(bad)

    def test_admin_line_matches_the_server_command(self):
        self.assertEqual(json.loads(D.admin_line(0x59, b'\x01\x03')), {'opcode': 0x59, 'payload_hex': '0103'})
        self.assertEqual(json.loads(D.admin_line(0x59, b'', '2', 'all')),
                         {'opcode': 0x59, 'payload_hex': '', 'target': 2, 'state': 'all'})
        self.assertEqual(json.loads(D.admin_line(0x59, b'', 'TestHero'))['target'], 'TestHero')
        self.assertEqual(D.ADMIN_STATES, ('in_world', 'select', 'all'))

    def _capture(self, fn, args):
        sent = []
        with _Patch(D, _inject=lambda op, payload, target=None, state=None:
                    sent.append((op, payload, target, state)) or 'ok (test)'):
            out = io.StringIO()
            with redirect_stdout(out):
                fn(args)
        return sent, out.getvalue()

    def test_send_passes_target_and_state(self):
        sent, _ = self._capture(D.cmd_send, ['59', '01', '03', '--to', 'admin', '--state', 'select'])
        self.assertEqual(sent, [(0x59, b'\x01\x03', 'admin', 'select')])
        sent, out = self._capture(D.cmd_send, ['59', '--state', 'nowhere'])
        self.assertEqual(sent, [])
        self.assertIn('--state must be one of', out)

    def test_sendspec_numeric_to_is_the_receiver_uid(self):
        # 0x76 local (18 B) vs observer (12 B) form depends on the receiver uid
        sent, _ = self._capture(D.cmd_sendspec, ['76', '{"target_uid":1,"str":5}', '--to', '1'])
        self.assertEqual([(op, len(p), t) for op, p, t, _ in sent], [(0x76, 18, '1')])
        sent, out = self._capture(D.cmd_sendspec, ['76', '{"target_uid":1,"str":5}', '--to', 'TestHero'])
        self.assertEqual(sent, [])
        self.assertIn('refused', out)


class CharSelect(unittest.TestCase):
    def _login(self, inworld, charselect, **kw):
        def no_input(*a, **k):
            raise AssertionError('auto_login clicked although the target screen was already up')
        with _Patch(D, is_inworld=lambda: inworld, is_charselect=lambda: charselect, _click_screen=no_input):
            return D.auto_login(timeout=1, **kw)

    def test_stop_at_select_returns_once_select_is_up(self):
        self.assertTrue(self._login(False, True, stop_at_select=True))

    def test_stop_at_select_from_in_world_reports_failure(self):
        self.assertFalse(self._login(True, False, stop_at_select=True))
        self.assertTrue(self._login(True, False))

    def test_up_and_login_accept_select(self):
        calls = []
        fake = dict(stop_all=lambda: None, start_server=lambda: 'ok', launch_client=lambda: 'ok',
                    status=lambda: None,
                    auto_login=lambda **kw: calls.append(kw['stop_at_select']) or True)
        with _Patch(D, **fake), redirect_stdout(io.StringIO()) as out:
            D.cmd_up(['--select'])
            D.cmd_login([])
        self.assertEqual(calls, [True, False])
        self.assertIn('AT CHARACTER SELECT', out.getvalue())
        self.assertIn('IN-WORLD', out.getvalue())

    def test_wsdev_delegates_new_input_commands(self):
        runs = []

        class _SP:
            @staticmethod
            def run(cmd, *a, **k):
                runs.append(cmd[2:])
        argv = sys.argv
        try:
            with _Patch(D, subprocess=_SP):
                for cmd in (['dclick', '5', '6'], ['rclick', '5', '6'], ['drag', '1', '2', '3', '4'],
                            ['type', '--enter', 'hi']):
                    sys.argv = ['wsdev.py', *cmd]
                    D.main()
        finally:
            sys.argv = argv
        self.assertEqual(runs, [['dclick', '5', '6'], ['rclick', '5', '6'], ['drag', '1', '2', '3', '4'],
                                ['type', '--enter', 'hi']])


if __name__ == '__main__':
    unittest.main(verbosity=1)
