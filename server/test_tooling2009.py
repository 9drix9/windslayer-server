#!/usr/bin/env python3
"""
test_tooling2009.py - offline tests of the build-aware dev tooling (client-2009-tooling)
========================================================================================
wsview.py / wsdev.py / the server's local-memory combat driver drive either client build:

- Layouts: client_layout.LAYOUT_2009 equals client_map_2009.json field by field, the 2008
  column equals both the same file and the addresses the tools used before (2008 unchanged),
  and the PE timestamps that tell a running client's build apart match the exe headers.
- Build selection: --build / WS_BUILD / config CLIENT_BUILD precedence, default 2008, the
  exe, install dir, spec and layout each build selects (wsview/wsdev run in a subprocess
  with only their command line changed), `up` refusing a build the server is not
  configured for.
- Launch: the 2009 command line "<exe>" -<user> <pass> -x -x run through a port of the
  client's own SSO parser (FUN_00440e00), and its first field logging in on a 2009 server
  through fakeclient.
- Memory: wsview state / wsdev _player, is_charselect, status and the server driver
  (_driver_local_player, _driver_read_player, _find_client_pid) read fake client memory
  laid out per build; a 2009 flow feeds a driver read into position tracking and a melee
  hit whose packets decode exactly with the 2009 spec.
- Auto-login 2009: the click sequence launcher -> world select -> character select.
- sendspec / cap encode and decode with the selected build's spec.

No client is started, no window is touched, no port is bound (process/window/input calls are
replaced by recorders), and the live accounts.json is never opened (checked at module end).
"""
import hashlib
import io
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import client_layout as CL  # noqa: E402
import fakeclient as F  # noqa: E402
import packets as P  # noqa: E402
import wsview as V  # noqa: E402
import wsdev as D  # noqa: E402

W = F.import_server()
cfgmod = W.cfgmod
EC = W.EC
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
CLIENT_MAP = os.path.join(HERE, 'client_map_2009.json')
B8, B9 = '2008', '2009'
GAME_DIR_2008 = os.path.dirname(HERE)
INSTALL_2009 = os.path.normpath(os.path.join(HERE, cfgmod.DEFAULTS['CLIENT_DIR_2009']))
HAVE_2009_INSTALL = os.path.exists(os.path.join(INSTALL_2009, 'hs', 'windslayer.hii'))
needs_2009 = unittest.skipUnless(HAVE_2009_INSTALL, 'needs the EN 2009 client data (CLIENT_DIR_2009)')
LOGIN_2009 = '0x451CE5/0x01'
ENTER_2009 = '0x4315D7/0x2B'


def server_2009(tmp):
    """A 2009 server on a temp accounts copy (the 2008 data dir if the 2009 install is missing)."""
    if HAVE_2009_INSTALL:
        return F.make_server(tmp, client_build=B9)
    return F.make_server(tmp, config=cfgmod.from_dict({'CLIENT_BUILD': B9,
                                                      'CLIENT_DIR_2009': cfgmod.DEFAULTS['CLIENT_DIR']}))


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


_LIVE_HASH = None


def setUpModule():
    global _LIVE_HASH
    _LIVE_HASH = _sha(LIVE_ACCOUNTS) if os.path.exists(LIVE_ACCOUNTS) else None


def tearDownModule():
    EC.configure(cfgmod.DEFAULTS['CLIENT_DIR'], B8)
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_tooling2009.py (tests must only use temp copies)'


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


class FakeMem:
    """Byte-addressed fake client memory; rd() of a range not fully present returns None."""

    def __init__(self):
        self.b = {}

    def put(self, addr, data):
        for i, x in enumerate(data):
            self.b[addr + i] = x

    def u8(self, addr, v):
        self.put(addr, bytes([v & 0xFF]))

    def u32(self, addr, v):
        self.put(addr, struct.pack('<I', v & 0xFFFFFFFF))

    def f64(self, addr, v):
        self.put(addr, struct.pack('<d', v))

    def rd(self, addr, n):
        if all(addr + i in self.b for i in range(n)):
            return bytes(self.b[addr + i] for i in range(n))
        return None

    # reader functions in the shape the server driver passes around
    def r_u8(self, a):
        d = self.rd(a, 1); return d[0] if d else 0

    def r_u32(self, a):
        d = self.rd(a, 4); return struct.unpack('<I', d)[0] if d else 0

    def r_f64(self, a):
        d = self.rd(a, 8); return struct.unpack('<d', d)[0] if d else 0.0


SCENE, NODE1, NODE2, ENT_MOB, ENT_ME = 0x2000000, 0x3000000, 0x3100000, 0x4000000, 0x5000000


def client_memory(lay, uid=1, pos=(1411.0, 714.0), swing=1, render=4, facing=6, mode=6,
                  level=7, hp=(90, 100), mp=(20, 30)):
    """A running client of layout `lay`: the scene, a monster and the local player (uid)
    in the entity list, every field at that build's offset."""
    m = FakeMem()
    m.u32(lay.scene_ptr, SCENE)
    m.u32(lay.anim_ptr, 0x6000000)
    m.u32(SCENE + lay.scene_local_uid, uid)
    m.u8(SCENE + lay.scene_mode, mode)
    m.u32(SCENE + lay.scene_entity_list, NODE1)
    m.u32(NODE1, NODE2); m.u32(NODE1 + 8, ENT_MOB)
    m.u32(NODE2, 0); m.u32(NODE2 + 8, ENT_ME)
    for ent, name, euid, x, y in ((ENT_MOB, b'Pupu', W.MOB_UID_BASE, 900.0, 700.0),
                                  (ENT_ME, b'TestHero', uid, pos[0], pos[1])):
        m.put(ent, name.ljust(32, b'\0'))
        m.u32(ent + lay.ent_uid, euid)
        m.u8(ent + lay.ent_type, 4)
        m.u32(ent + lay.ent_anim, 3)
        m.f64(ent + lay.ent_pos_x, x)
        m.f64(ent + lay.ent_pos_y, y)
    m.u8(ENT_ME + lay.ent_level, level)
    m.u32(ENT_ME + lay.ent_hp, hp[0]); m.u32(ENT_ME + lay.ent_max_hp, hp[1])
    m.u32(ENT_ME + lay.ent_mp, mp[0]); m.u32(ENT_ME + lay.ent_max_mp, mp[1])
    m.u8(ENT_ME + lay.ent_swing, swing)
    m.u32(ENT_ME + lay.ent_render_state, render)
    m.u8(ENT_ME + lay.ent_facing, facing)
    return m


# ==================================================================== layouts ===
class Layouts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(CLIENT_MAP, encoding='utf-8') as f:
            cls.map = json.load(f)

    def column(self, field, build):
        section, key, index = CL.SOURCES[field]
        return CL.column_values(self.map[section][key][build])[index]

    def test_every_field_has_a_client_map_source(self):
        self.assertEqual(set(CL.SOURCES), set(CL.layout_fields()))

    def test_2009_offsets_equal_client_map_2009(self):
        for field in CL.layout_fields():
            with self.subTest(field=field):
                self.assertEqual(getattr(CL.LAYOUT_2009, field), self.column(field, B9),
                                 f'{field}: 0x{getattr(CL.LAYOUT_2009, field):X}')

    def test_2008_column_equals_client_map_2008_column(self):
        for field in CL.layout_fields():
            with self.subTest(field=field):
                self.assertEqual(getattr(CL.LAYOUT_2008, field), self.column(field, B8))

    def test_2008_layout_is_the_live_proven_set_the_tools_used(self):
        # the constants wsview/wsdev/windslayer_server hard-coded before this stage
        legacy = dict(game_state=0x70EA00, scene_ptr=0x70EECC, anim_ptr=0x70EEE0, receive_busy_flag=0x70EE08,
                      scene_entity_list=0xC, scene_local_uid=0x220, scene_mode=0xF00, ent_uid=0x84,
                      ent_type=0x98, ent_level=0x99, ent_hp=0x9C, ent_mp=0xA0, ent_max_hp=0x110C,
                      ent_max_mp=0x1110, ent_anim=0xE60, ent_pos_x=0x11F8, ent_pos_y=0x1288,
                      ent_swing=0x8B8, ent_render_state=0x15B4, ent_facing=0x8BD)
        for field, value in legacy.items():
            with self.subTest(field=field):
                self.assertEqual(getattr(CL.LAYOUT_2008, field), value)

    def test_the_task_facts(self):
        L9 = CL.LAYOUT_2009
        self.assertEqual((L9.game_state, L9.scene_ptr, L9.scene_local_uid, L9.ent_uid, L9.ent_type,
                          L9.ent_pos_x, L9.ent_pos_y), (0x54EBD0, 0x54F0C0, 0x224, 0x88, 0x9C, 0x1298, 0x1328))
        self.assertEqual(CL.column_values('+0x1298 (x f64) / +0x1328 (y f64)'), [0x1298, 0x1328])
        self.assertEqual(CL.column_values('+0 name, +0x9D level')[:2], [0, 0x9D])
        self.assertIn('0x400000', self.map['meta']['image_base'])        # no ASLR: the stamp read

    def test_layout_lookup(self):
        self.assertIs(CL.layout(None), CL.LAYOUT_2008)
        self.assertIs(CL.layout('2009'), CL.LAYOUT_2009)
        with self.assertRaises(ValueError):
            CL.layout('2010')
        self.assertEqual((CL.build_of_timestamp(0x48240544), CL.build_of_timestamp(0x49797E29),
                          CL.build_of_timestamp(0)), (B8, B9, None))

    def test_pe_timestamps_match_the_installed_exes(self):
        def stamp(path):
            with open(path, 'rb') as f:
                head = f.read(4096)
            pe = struct.unpack_from('<I', head, 0x3C)[0]
            self.assertEqual(head[pe:pe + 4], b'PE\0\0')
            return struct.unpack_from('<I', head, pe + 8)[0]
        checked = 0
        for lay, folder, names in ((CL.LAYOUT_2008, GAME_DIR_2008, ('WindSlayer.exe', 'WindSlayer_patched.exe', 'WindSlayer_p2.exe')),
                                   (CL.LAYOUT_2009, INSTALL_2009, ('WindSlayer.exe', 'WindSlayer_patched.exe'))):
            for name in names:
                path = os.path.join(folder, name)
                if os.path.exists(path):
                    with self.subTest(path=path):
                        self.assertEqual(stamp(path), lay.pe_timestamp)
                    checked += 1
        if not checked:
            self.skipTest('no client exe installed')


# ============================================================ build selection ===
class BuildSelection(unittest.TestCase):
    def test_precedence(self):
        self.assertEqual(CL.resolve_build('2009', config_build='2008'), B9)       # --build / WS_BUILD
        self.assertEqual(CL.resolve_build(None, config_build='2009'), B9)         # config CLIENT_BUILD
        self.assertEqual(CL.resolve_build('', config_build='2008'), B8)
        with mock.patch.object(CL, '_config_build', return_value=None):
            self.assertEqual(CL.resolve_build(None), B8)                          # the default
        self.assertEqual(CL.resolve_build(None, cfgmod.defaults().CLIENT_BUILD), B8)
        for bad in ('2010', 'x', '09'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                CL.resolve_build(bad)

    def test_pop_build_arg(self):
        argv = ['wsview.py', 'state', '--build', '2009', '--x']
        self.assertEqual(V.pop_build_arg(argv), '2009')
        self.assertEqual(argv, ['wsview.py', 'state', '--x'])
        self.assertIsNone(V.pop_build_arg(['wsview.py', 'shot']))
        for bad in (['wsview.py', '--build'], ['wsview.py', '--build', '--client', '2']):
            with self.subTest(argv=bad), self.assertRaises(ValueError):
                V.pop_build_arg(bad)

    def _run(self, module, argv, env_build=None, config=None):
        """Import wsview/wsdev in a child python with `argv` and print what it selected. The
        child reads a temp config (WS_CONFIG), never the live one; nothing is launched."""
        tmp = tempfile.mkdtemp(prefix='ws_tool_cfg_')
        try:
            cfg = {'CLIENT_DIR_2009': INSTALL_2009, **(config or {})}
            path = os.path.join(tmp, 'config.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(cfg, f)
            env = {k: v for k, v in os.environ.items() if k not in (CL.ENV_BUILD, 'WS_CLIENT')}
            env['WS_CONFIG'] = path
            if env_build:
                env[CL.ENV_BUILD] = env_build
            probe = (f'import sys, json; sys.argv = {[module + ".py", *argv]!r}; import {module} as M; '
                     'L = M.LAYOUT if hasattr(M, "LAYOUT") else M.L; '
                     'print(json.dumps({"build": M.BUILD, "scene": L.scene_ptr, "argv": sys.argv, '
                     '"exe": getattr(M, "EXE", None), "spec": getattr(M, "_server_build", lambda: None)(), '
                     '"proc": getattr(M, "PROC_NAME", None) or M.V.PROC_NAME}))')
            out = subprocess.run([sys.executable, '-c', probe], cwd=HERE, env=env, capture_output=True,
                                 text=True, timeout=120)
            self.assertEqual(out.returncode, 0, out.stderr)
            return json.loads(out.stdout.strip().splitlines()[-1])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_wsview_build_flag(self):
        got = self._run('wsview', ['state'])
        self.assertEqual((got['build'], got['scene'], got['argv']), (B8, 0x70EECC, ['wsview.py', 'state']))
        got = self._run('wsview', ['state', '--build', '2009'])
        self.assertEqual((got['build'], got['scene'], got['argv']), (B9, 0x54F0C0, ['wsview.py', 'state']))
        self.assertEqual(self._run('wsview', ['shot'], env_build='2009')['build'], B9)
        self.assertEqual(self._run('wsview', ['shot'], config={'CLIENT_BUILD': '2009'})['build'], B9)
        self.assertEqual(self._run('wsview', ['shot', '--build', '2008'], config={'CLIENT_BUILD': '2009'})['build'], B8)

    def test_wsdev_build_flag_selects_exe_spec_and_layout(self):
        got = self._run('wsdev', ['status'])
        self.assertEqual((got['build'], got['spec'], got['scene']), (B8, B8, 0x70EECC))
        self.assertEqual(os.path.normcase(got['exe']),
                         os.path.normcase(os.path.join(GAME_DIR_2008, 'WindSlayer_patched.exe')))
        got = self._run('wsdev', ['up', '--build', '2009'])
        self.assertEqual((got['build'], got['spec'], got['scene'], got['argv']),
                         (B9, B9, 0x54F0C0, ['wsdev.py', 'up']))
        self.assertEqual(os.path.normcase(got['exe']),
                         os.path.normcase(os.path.join(INSTALL_2009, 'WindSlayer_patched.exe')))
        got = self._run('wsdev', ['status', '--client', '2', '--build', '2009'])
        self.assertEqual((got['build'], got['proc']), (B9, 'WindSlayer_p2.exe'))

    def test_client_dir_of_the_shipped_config(self):
        cfg = cfgmod.from_dict({}, os.path.join(HERE, 'config.json'))
        self.assertEqual(os.path.normcase(D.client_dir(B9, cfg)),
                         os.path.normcase(os.path.abspath(os.path.join(HERE, '..', '..', 'WindSlayer2009'))))
        self.assertEqual(D.client_dir(B8, cfg), GAME_DIR_2008)

    def test_up_refuses_a_build_the_server_is_not_configured_for(self):
        cfg08 = cfgmod.from_dict({})
        cfg09 = cfgmod.from_dict({'CLIENT_BUILD': '2009'})
        self.assertIsNone(D.build_mismatch(B8, cfg08))
        self.assertIsNone(D.build_mismatch(B9, cfg09))
        self.assertIn('CLIENT_BUILD', D.build_mismatch(B9, cfg08))
        calls = []
        rec = lambda *a, **k: calls.append(a) or 'ok'           # noqa: E731
        with _Patch(D, BUILD=B9, CFG=cfg08, stop_all=rec, start_server=rec, launch_client=rec,
                    auto_login=rec, status=rec), redirect_stdout(io.StringIO()) as out:
            D.cmd_up([])
            D.cmd_restart([])
        self.assertEqual(calls, [])
        self.assertIn('refused', out.getvalue())

    def test_children_inherit_the_build(self):
        with _Patch(D, BUILD=B9):
            env = D._child_env()
        self.assertEqual((env[CL.ENV_BUILD], env['WS_CLIENT']), (B9, V.CLIENT))


# ===================================================================== launch ===
class Launch2009(unittest.TestCase):
    EXE9 = os.path.join(INSTALL_2009, 'WindSlayer_patched.exe')

    def test_2008_launch_is_unchanged(self):
        self.assertEqual(CL.launch_command(B8, r'C:\g\WindSlayer_patched.exe', 'test', 'test'),
                         [r'C:\g\WindSlayer_patched.exe'])

    def test_2009_command_line_through_the_client_parser(self):
        cmd = CL.launch_command(B9, self.EXE9, 'test', 'test')
        self.assertEqual(cmd, f'"{self.EXE9}" -test test -x -x')
        # FUN_00440e00: argument 1 keeps its trailing space (live-verified), 2 and 3 non-empty
        self.assertEqual(CL.parse_sso_command_line(cmd), ('test test ', 'x ', 'x'))
        self.assertEqual(CL.parse_sso_command_line(CL.launch_command(B9, r'C:\a b\W.exe', 'admin', 'pw9')),
                         ('admin pw9 ', 'x ', 'x'))
        # why a raw string: list2cmdline quotes "-test test", so the parser starts inside it
        broken = subprocess.list2cmdline([r'C:\g\WindSlayer_patched.exe', '-test test', '-x', '-x'])
        self.assertNotEqual(CL.parse_sso_command_line(broken)[0], 'test test ')

    def test_2009_refuses_credentials_the_parser_would_split(self):
        for user, pw in (('te st', 'x'), ('test', 'a b'), ('"t', 'x'), ('-t', 'x'), ('', 'x'), ('test', '')):
            with self.subTest(user=user, pw=pw), self.assertRaises(ValueError):
                CL.launch_command(B9, self.EXE9, user, pw)

    def test_wsdev_launches_2009_non_elevated_with_the_sso_args(self):
        tmp = tempfile.mkdtemp(prefix='ws_tool_exe_')
        try:
            exe = os.path.join(tmp, 'WindSlayer_patched.exe')
            open(exe, 'wb').close()
            popens = []

            class _SP:
                @staticmethod
                def Popen(cmd, **kw):
                    popens.append((cmd, kw))
            polls = iter([[], [], [4242]])
            with _Patch(D, BUILD=B9, EXE=exe, CLIENT_DIR=tmp, LOGIN_USER='admin', LOGIN_PASS='admin',
                        subprocess=_SP), _Patch(D.V, _pids=lambda: next(polls)):
                self.assertEqual(D.launch_client(), 'launched')
            (cmd, kw), = popens
            self.assertEqual(cmd, f'"{exe}" -admin admin -x -x')
            self.assertEqual((kw['cwd'], kw['env']['__COMPAT_LAYER']), (tmp, 'RunAsInvoker'))
            with _Patch(D, BUILD=B9, EXE=os.path.join(tmp, 'missing.exe'), CLIENT_DIR=tmp, subprocess=_SP), \
                    _Patch(D.V, _pids=lambda: []):
                self.assertIn('patch_2009.py', D.launch_client())
            self.assertEqual(len(popens), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_launch_field_logs_in_on_a_2009_server(self):
        """The first SSO field of the command line wsdev starts is exactly what the 2009
        client sends in C2S 0x01; the server splits it into account + password."""
        tmp = tempfile.mkdtemp(prefix='ws_tool_login_')
        try:
            server = server_2009(tmp)
            for pw, ok in (('test', True), ('wrong', False)):
                field1 = CL.parse_sso_command_line(CL.launch_command(B9, self.EXE9, 'test', pw))[0]
                with F.FakeClient(server) as c, self.subTest(pw=pw):
                    c.send_c2s(LOGIN_2009, {'sso_account': field1, 'session_key': 0})
                    self.assertEqual(c.s2c(c.expect(0x02))['result'] == 1, ok)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ================================================================== processes ===
class Processes(unittest.TestCase):
    STAMPS = {11: CL.LAYOUT_2008.pe_timestamp, 12: CL.LAYOUT_2009.pe_timestamp, 13: None}

    def test_select_pids_by_build_stamp(self):
        stamp = self.STAMPS.get
        self.assertEqual(CL.select_pids([11, 12, 13], stamp, CL.LAYOUT_2008.pe_timestamp), [11, 13])
        self.assertEqual(CL.select_pids([11, 12, 13], stamp, CL.LAYOUT_2009.pe_timestamp), [12, 13])
        self.assertEqual(CL.select_pids([11, 12, 13], stamp, CL.LAYOUT_2009.pe_timestamp, strict=True), [12])

    def test_the_driver_attaches_only_to_its_build(self):
        tmp = tempfile.mkdtemp(prefix='ws_tool_pid_')
        try:
            s08, s09 = F.make_server(tmp), server_2009(tmp)
            stamp = self.STAMPS.get
            self.assertEqual(s08._find_client_pid([12, 13, 11], stamp), 11)
            self.assertEqual(s09._find_client_pid([11, 13, 12], stamp), 12)
            self.assertEqual(s09._find_client_pid([11, 13], stamp), 0)       # a 2008 client only
            self.assertEqual(s08.CLIENT_EXE, 'WindSlayer_patched.exe')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_process_enumeration_runs(self):
        self.assertEqual(CL.process_ids('no_such_windslayer_client.exe'), [])
        self.assertEqual(CL.find_client_pids('no_such_windslayer_client.exe', CL.LAYOUT_2009), [])


# ===================================================================== memory ===
class ToolMemory(unittest.TestCase):
    def _patch_reads(self, mem, lay):
        return _Patch(D, L=lay, SCENE_PTR=lay.scene_ptr, _rd=lambda proc, a, n: mem.rd(a, n) or b'',
                      _proc=lambda: 1)

    def test_wsdev_player_and_charselect_per_layout(self):
        for lay, other in ((CL.LAYOUT_2009, CL.LAYOUT_2008), (CL.LAYOUT_2008, CL.LAYOUT_2009)):
            with self.subTest(build=lay.build):
                mem = client_memory(lay)
                with self._patch_reads(mem, lay):
                    self.assertEqual(D._player(1), ENT_ME)
                    self.assertTrue(D.is_inworld())
                    self.assertFalse(D.is_charselect())
                with self._patch_reads(mem, other):
                    self.assertEqual(D._player(1), 0)                # the other build's layout sees nothing
                sel = client_memory(lay, uid=77, mode=D.CHARSELECT_MODE)
                sel.u32(ENT_ME + lay.ent_uid, 1)                     # no entity carries uid 77 yet
                with self._patch_reads(sel, lay):
                    self.assertTrue(D.is_charselect())

    def test_wsdev_status_reads_the_2009_offsets(self):
        lay = CL.LAYOUT_2009
        mem = client_memory(lay, pos=(1234.0, 567.0))
        with self._patch_reads(mem, lay), _Patch(D, BUILD=B9, server_up=lambda: False, _tail_log=lambda n=40: ''), \
                _Patch(D.V, _pids=lambda: [4242]), redirect_stdout(io.StringIO()) as out:
            D.status()
        text = out.getvalue()
        self.assertIn('IN-WORLD', text)
        self.assertIn('Lv 7  HP 90/100  MP 20/30  pos (1234,567)', text)
        self.assertIn('monsters visible: 1', text)

    def test_wsview_state_reads_the_2009_offsets(self):
        lay = CL.LAYOUT_2009
        mem = client_memory(lay, pos=(1500.0, 700.0))
        with _Patch(V, LAYOUT=lay, SCENE_PTR=lay.scene_ptr, ANIM_PTR=lay.anim_ptr,
                    _open_inworld=lambda: (1, 4242), _reader=lambda proc: mem.rd), \
                redirect_stdout(io.StringIO()) as out:
            V.cmd_state([])
        text = out.getvalue()
        self.assertIn('build 2009', text)
        self.assertIn('local uid (scene+0x224) = 1', text)
        # alive 4, uid 1, lv 7 (entity +0x9D, the P5 stage 4 column), anim 3, position
        self.assertRegex(text, r'TestHero\s+4 0x00000001\s+7\s+3\s+\(1500,700\)')
        self.assertIn('Pupu', text)

    def test_no_2008_address_is_hard_coded_outside_the_layout(self):
        pattern = re.compile(r'0x70E[A-F0-9]{3}\b|\+ ?0x(?:8b8|15b4|11f8|1288|e60)\b|0x220\)|0x84\)', re.I)
        for path in ('wsview.py', 'wsdev.py', 'windslayer_server.py'):
            with open(os.path.join(HERE, path), encoding='utf-8') as f:
                text = re.sub(r'"""[\s\S]*?"""', '', f.read())
            code = [ln.split('#', 1)[0] for ln in text.splitlines()]
            hits = [ln.strip() for ln in code if pattern.search(ln)]
            with self.subTest(path=path):
                self.assertEqual(hits, [])


# ============================================================== server driver ===
class Driver2009(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_tool_drv_')
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def server(self, build):
        return server_2009(self.tmp) if build == B9 else F.make_server(self.tmp)

    def test_reads_per_build(self):
        for build, lay, other in ((B9, CL.LAYOUT_2009, CL.LAYOUT_2008), (B8, CL.LAYOUT_2008, CL.LAYOUT_2009)):
            with self.subTest(build=build):
                s = self.server(build)
                self.assertIs(s.client_layout, lay)
                s.world.session = lambda uid: {'uid': uid} if uid == 1 else None
                mem = client_memory(lay, pos=(321.0, 654.0), facing=2)
                self.assertEqual(s._driver_local_player(mem.r_u32), (ENT_ME, 1))
                self.assertEqual(s._driver_read_player(ENT_ME, mem.r_u8, mem.r_u32, mem.r_f64),
                                 (True, 321.0, 654.0, 2))
                idle = client_memory(lay, render=8)                           # swing flag but idle
                self.assertFalse(s._driver_read_player(ENT_ME, idle.r_u8, idle.r_u32, idle.r_f64)[0])
                wrong = client_memory(other)
                self.assertEqual(s._driver_local_player(wrong.r_u32), (0, 0))  # never the other layout

    def test_a_bare_server_reads_the_2008_layout(self):
        self.assertIs(W.GameServer.__new__(W.GameServer).client_layout, CL.LAYOUT_2008)

    @needs_2009
    def test_2009_flow_driver_position_and_hit(self):
        """login -> enter map 102 on the 2009 spec, then one driver pass over a 2009 client's
        memory: the local player is found by scene+0x224 / +0x88, its +0x1298/+0x1328 point and
        +0x949 facing reach the session, and a hit there answers with exact 2009 packets."""
        server = self.server(B9)
        c = F.FakeClient(server)
        self.clients.append(c)
        with server.store.lock:
            hero = server.store.find_character('test', 'TestHero')
            hero['map'], (hero['x'], hero['y']) = 102, EC.portal(101, 23)[1:]
        c.send_c2s(LOGIN_2009, F.sso_login('test', 'test'))
        self.assertEqual(c.s2c(c.expect(0x02))['result'], 1)
        c.send_c2s(ENTER_2009, {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907, 'char_name': 'TestHero'})
        for pkt in c.expect(0x03, 0x07, 0x15, 0x65, 0x28, 0x44, *F.mob_packets(8)):   # 0x65: 2009 bank
            c.s2c(pkt)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world')))
        uid = P.session_uid(c.session)
        mob = c.session['monsters'][W.MOB_UID_BASE + 2]
        hp0 = {m.uid: m.hp for m in c.session['monsters'].values()}

        mem = client_memory(CL.LAYOUT_2009, uid=uid, pos=(mob.x, mob.y), facing=2)
        player, my_uid = server._driver_local_player(mem.r_u32)
        self.assertEqual((player, my_uid), (ENT_ME, uid))
        attacking, px, py, facing = server._driver_read_player(player, mem.r_u8, mem.r_u32, mem.r_f64)
        self.assertEqual((attacking, px, py, facing), (True, mob.x, mob.y, 2))
        c.session['map_confirmed'] = c.session['current_map']       # its first 0x0D named this map
        self.assertTrue(server._track_driver_position(my_uid, px, py, facing))
        self.assertEqual(c.session['pos'], (mob.x, mob.y))
        self.assertEqual(c.session['facing'], W.combat.facing_from_entity(2))

        # the `wsdev hit` path: a surviving mob gets no packet (the flinch is the client's
        # own), the kill answers 0x29 + 0x21 exp + 0x18, all exact under the 2009 grammar
        dead = []
        with mock.patch.object(W.random, 'random', return_value=0.0),                 mock.patch.object(W.random, 'choice', side_effect=lambda seq: seq[0]):
            for _ in range(40):
                server._memory_melee(px, py, uid=my_uid)
                dead = [m for m in c.session['monsters'].values() if not m.alive]
                if dead:
                    break
                c.expect_silence(0.05)
        self.assertEqual(len(dead), 1)
        self.assertLess(abs(dead[0].x - px) + abs(dead[0].y - py), 1e-6 + W.MELEE_RANGE_X)
        self.assertLess(sum(m.hp for m in c.session['monsters'].values()), sum(hp0.values()))
        pkts = c.recv_until_quiet()
        self.assertEqual([p.opcode for p in pkts][:2], [0x29, 0x21])
        for pkt in pkts:
            c.s2c(pkt)                                                    # exact 2009 decode


# ================================================================ auto-login ===
class AutoLogin2009(unittest.TestCase):
    """The 2009 click sequence against a scripted client (no window, no input)."""

    def run_login(self, start, **kw):
        state = {'screen': start}
        clicks = []
        advance = {D.LAUNCHER_START_2009: ('launcher', 'world_select'),
                   D.WORLD_OK_2009: ('world_select', 'select'),
                   D.CHARSEL_START_2009: ('select', 'world')}

        def click(hwnd, pt):
            clicks.append(tuple(pt))
            frm, to = advance.get(tuple(pt), (None, None))
            if state['screen'] == frm:
                state['screen'] = to

        class _Time:
            time = staticmethod(time.time)
            sleep = staticmethod(lambda s: None)
        with _Patch(D, time=_Time, _click_client=click,
                    is_inworld=lambda: state['screen'] == 'world',
                    is_charselect=lambda: state['screen'] == 'select',
                    is_launcher_2009=lambda h: state['screen'] == 'launcher'), \
                _Patch(D.V, _find_hwnd=lambda: 0x1234, _foreground=lambda h: None):
            ok = D.auto_login_2009(timeout=2, **kw)
        return ok, clicks

    def test_full_sequence(self):
        ok, clicks = self.run_login('launcher')
        self.assertTrue(ok)
        self.assertEqual(clicks, [D.LAUNCHER_WINDOW_2009, D.LAUNCHER_START_2009, D.WORLD_CHANNEL_2009,
                                  D.WORLD_OK_2009, (D.CHAR_SLOTS_X_2009[0], D.CHAR_SLOT_Y_2009),
                                  D.CHARSEL_START_2009])

    def test_second_slot_and_stop_at_select(self):
        ok, clicks = self.run_login('world_select', char_index=2)
        self.assertTrue(ok)
        self.assertEqual(clicks[:2], [D.WORLD_CHANNEL_2009, D.WORLD_OK_2009])      # resumes: no launcher
        self.assertEqual(clicks[2], (D.CHAR_SLOTS_X_2009[2], D.CHAR_SLOT_Y_2009))
        ok, clicks = self.run_login('launcher', stop_at_select=True)
        self.assertTrue(ok)
        self.assertEqual(clicks[-1], D.WORLD_OK_2009)                               # no slot click
        ok, clicks = self.run_login('world', stop_at_select=True)
        self.assertEqual((ok, clicks), (False, []))

    def test_dispatch_by_build(self):
        seen = []
        with _Patch(D, BUILD=B9, auto_login_2009=lambda *a, **k: seen.append(k) or True):
            self.assertTrue(D.auto_login(timeout=1, char_index=1, stop_at_select=True))
        self.assertEqual(seen, [{'char_index': 1, 'stop_at_select': True}])

    def test_launcher_detection(self):
        class _U32:
            def __init__(self, cls, title):
                self.cls, self.title = cls, title

            def GetClassNameW(self, h, buf, n):
                buf.value = self.cls

            def GetWindowTextW(self, h, buf, n):
                buf.value = self.title
        for cls, title, want in (('#32770', 'WindSlayer (Build: 14)', True), ('#32770', '', True),
                                 ('WindSlayer', 'WindSlayer (Build: 14)', True), ('WindSlayer', 'WindSlayer', False)):
            with self.subTest(cls=cls, title=title), _Patch(D, user32=_U32(cls, title)):
                self.assertEqual(D.is_launcher_2009(1), want)

    def test_coordinates_are_named_constants(self):
        self.assertEqual((D.LAUNCHER_WINDOW_2009, D.LAUNCHER_START_2009, D.WORLD_CHANNEL_2009, D.WORLD_OK_2009),
                         ((626, 355), (613, 381), (250, 155), (186, 422)))
        self.assertEqual((D.CHAR_SLOTS_X_2009, D.CHAR_SLOT_Y_2009, D.CHARSEL_START_2009),
                         (D.CHAR_SLOTS_X, D.CHAR_SLOT_Y, D.CHARSEL_START))
        # START lies on the launcher's START bitmap (FUN_00488190 BitBlt x 0x234 w 0x60, y 0x169 h 0x2a)
        x, y = D.LAUNCHER_START_2009
        self.assertTrue(0x234 <= x < 0x234 + 0x60 and 0x169 <= y < 0x169 + 0x2A)


# ============================================================= sendspec / cap ===
class SpecPerBuild(unittest.TestCase):
    def sendspec(self, build, *args):
        sent = []
        with _Patch(D, BUILD=build, _inject=lambda op, payload, *t: sent.append((op, payload)) or 'ok (test)'), \
                redirect_stdout(io.StringIO()) as out:
            D.cmd_sendspec(list(args))
        return sent, out.getvalue()

    def test_sendspec_uses_the_selected_spec(self):
        sent, out = self.sendspec(B9, '08', '{"reason":0,"map_code":102,"game_time_ms":5}')
        self.assertEqual(sent, [(0x08, bytes.fromhex('00 6600 05000000'))], out)
        sent, out = self.sendspec(B8, '08', '{"map_code":102,"game_time_ms":5}')
        self.assertEqual(sent, [(0x08, bytes.fromhex('6600 05000000'))], out)
        _, out = self.sendspec(B9, '08', '?')
        self.assertIn('u8 reason', out)
        _, out = self.sendspec(B8, '08', '?')
        self.assertNotIn('reason', out.split('\n\n')[0])

    def test_cap_decodes_c2s_with_the_selected_spec(self):
        login = P.build(LOGIN_2009, F.sso_login('test', 'test'), direction='C2S', client_build=B9)
        self.assertEqual(len(login), 135)
        self.assertTrue(D.decode_c2s(0x01, login.hex(' '), B9).startswith(LOGIN_2009))
        self.assertTrue(D.decode_c2s(0x01, login.hex(' '), B8).startswith('['))     # not a 2008 login
        tmp = tempfile.mkdtemp(prefix='ws_tool_cap_')
        try:
            log_path = os.path.join(tmp, 'server_live.log')
            with open(log_path, 'w', encoding='utf-8') as f:
                f.write('2026-09-23 [INFO] earlier line\n')
            raw = bytes(9) + login
            dump = W.hexdump(raw)

            class _SP:
                DEVNULL = subprocess.DEVNULL

                @staticmethod
                def run(cmd, **kw):
                    self.assertEqual(kw['env'][CL.ENV_BUILD], B9)
                    with open(log_path, 'a', encoding='utf-8') as f:
                        f.write(f'2026-09-23 [INFO] [FIREWAY] Decode result:\n{dump}\n'
                                f'2026-09-23 [INFO] [FIREWAY] Pkt: opcode=0x01 size={len(raw)} seq=2 '
                                f'no_enc=False payload={len(login)}B\n'
                                f'2026-09-23 [INFO] [FIREWAY] Send: opcode=0x02 seq=3 size=91 by_array=False\n')
            with _Patch(D, BUILD=B9, LIVE_LOG=log_path, subprocess=_SP), redirect_stdout(io.StringIO()) as out:
                D.cmd_cap(['0', 'click', '186', '422'])
            text = out.getvalue()
            self.assertIn('[spec 2009]', text)
            self.assertIn(f'= {LOGIN_2009}', text)
            self.assertIn("sso_account='test test '", text)
            self.assertIn('S2C 0x02', text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main(verbosity=1)
