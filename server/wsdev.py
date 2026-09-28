#!/usr/bin/env python3
"""
wsdev.py — WindSlayer one-command DEV HARNESS
=============================================
Brings the whole stack up and lets an AI agent (or dev) debug solo, no manual
clicking required for the common loop:

  up [--select]      stop stale procs -> start server -> launch client
                     NON-ELEVATED -> auto-login to in-world -> report state.
                     --select stops at character select (no slot/START click)
                     for create/delete/0x02/0x1C/0x1F/0x8C tests
  down               stop server + client
  restart            down + up (use after editing windslayer_server.py)
  login [--select]   run the auto-login sequence only (client already launched)
  status             server / client / in-world / combat-driver / player summary
  hit [n]            DEBUG: force the server to resolve n melee hits on the
                     nearest monster (via the _dbg_attack sentinel) - test combat
                     feedback / leveling without a real keypress
  logs [n]           tail the last n lines of server_live.log (default 40) -
                     see [ATK]/[KILL]/[LEVEL]/[COMBAT] after a hit
  cap [--all] <secs> <wsview cmd...>
                     run one UI action (key/hold/click/dclick/rclick/drag/type)
                     and print the exact client->server packets it produced +
                     server replies (C2S decoded with the selected build's
                     spec), e.g.  cap 2 key q     cap 3 click 700 575
                     cap 3 type --enter /w alice hi     cap 3 dclick 640 330
  send <opcode> [hex...] [--to uid|name|c:N] [--state in_world|select|all]
                     inject a raw server->client packet into the live client
                     (server admin port 127.0.0.1:7099). By default only
                     IN-WORLD sessions receive it (character-select sessions
                     crash on messenger packets); --state select targets the
                     character-select screen, --to one account/character/uid,
                     or c:N = client exe N by its P2P port (c:1 = the patched
                     exe on 42907, c:2 = WindSlayer_p2.exe on 42908), any
                     account (the colon: a character may be called "c2")
  sendspec <opcode> ['{json}' | ?] [--assume '{"cond": true}'] [--uid N] [--loose]
           [--to uid|name|c:N] [--state in_world|select|all]
                     build a server->client payload from the RE grammar
                     (packets.build over protocol_spec.json; --build 2009:
                     protocol_spec_2009.json) and inject it;
                     `?` prints the spec and its client-state conditions
  gm <character> [0|1]
                     set / clear a character's GM flag in the running server's
                     store (chat_mail_gm-gm-flag-manner). Makes the first GM:
                     accounts.json is owned by the server while it runs. The
                     client needs a portal or relog to see gm_level in its 0x07
                     (its own /not, /manner ...); server '!' commands work at once
  dev <character> <!command ...>
                     run a '!' dev command as that in-world character via the
                     admin port (e.g. dev TestHero !level 3) - no chat typing
  admin '{json}'     one raw admin-port line (P6 stage 4): {"kick": "Watcher"}
                     (0x5D, save, close after 1 s; target uid / "c:2" / name,
                     optional "reason"), {"shutdown": 1} (the /stop maintenance
                     countdown), {"maintenance": 0|1} (lift / lock logins)
  shot [path]        screenshot the game window (delegates to wsview)
  state              live entity dump (delegates to wsview)
  key/hold/click/dclick/rclick/drag/type/watch/win
                     input + view commands (delegate to wsview)

Options (any command):
  --client 2         drive the second client WindSlayer_p2.exe (UDP 42908). Its
                     up/down never touch the server or client 1.
  --build 2009       drive the EN 2009 client (WindSlayer2009, Outspark Build 14):
                     its exe (CLIENT_DIR_2009), memory layout (client_layout.py) and
                     auto-login, and its spec for sendspec/cap. Same as env
                     WS_BUILD=2009. Default: config.json CLIENT_BUILD (2008 unless
                     changed); `up` refuses a build the server is not configured for.
  --user U --pass P --char N
                     login account/character slot for up/login (default
                     test/test for client 1, admin/admin for client 2; slot 0).
                     2009: the account and password go on the exe's command line
                     (-U P -x -x) at launch; there is no in-game login form

Everything is built on wsview.py (same dir). Server logs to server_live.log.
Launch must be NON-ELEVATED for ReadProcessMemory; this tool always launches the
client with __COMPAT_LAYER=RunAsInvoker so combat/memory work.
"""
import os, sys, time, struct, subprocess, ctypes
import ctypes.wintypes as wt

# Emit UTF-8 so status/log output isn't mojibake'd on the default Windows console
# codepage (cp1252/437 renders non-ASCII as '?'). Best-effort; ignore if unsupported.
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
GAME_DIR = os.path.dirname(HERE)                      # ...\WindSlayer2Game
SERVER_PY = os.path.join(HERE, 'windslayer_server.py')
SENTINEL = os.path.join(HERE, '_dbg_attack')

sys.path.insert(0, HERE)
import wsview as V                                    # reuse window/mem/input helpers (parses --client, --build)
import client_layout as CL
BUILD = V.BUILD                                       # '2008' | '2009' (client-2009-tooling)
L = V.LAYOUT
PRIMARY = V.CLIENT == '1'

# Server ports from server/config.json (arch-config, roadmap F12); defaults if it is unreadable.
import config as _cfgmod
try:
    CFG = _cfgmod.load()
except _cfgmod.ConfigError as _e:
    print(f'config.json unreadable ({_e}); using default ports')
    CFG = _cfgmod.defaults()


def client_dir(build=None, cfg=None):
    """The install the selected build launches from: 2008 = WindSlayer2Game (the parent of
    this directory, unchanged); 2009 = config CLIENT_DIR_2009 (default ../../WindSlayer2009,
    i.e. Desktop/WindSlayer2009 next to WindSlayer2Game)."""
    build = build or BUILD
    cfg = cfg if cfg is not None else CFG
    if build == CL.BUILD_2009:
        return cfg.resolve(cfg['CLIENT_DIR_2009'])
    return GAME_DIR


CLIENT_DIR = client_dir()
EXE = CL.install_exe(CLIENT_DIR, V.CLIENT)            # client 1 = WindSlayer_patched.exe, 2 = WindSlayer_p2.exe


def _server_build():
    """The build sendspec/cap encode and decode with: the selected build (--build, WS_BUILD,
    else config CLIENT_BUILD - the build the server speaks, client-2009-login)."""
    return BUILD


def build_mismatch(build=None, cfg=None):
    """Why `up` must not start a server for `build`, or None. The server speaks config
    CLIENT_BUILD; a 2009 client against a 2008 server (or the reverse) fails at the version
    reply (code 3 vs 14) or the login record."""
    build = build or BUILD
    cfg = cfg if cfg is not None else CFG
    if cfg.get('CLIENT_BUILD') == build:
        return None
    return (f'--build {build} but {cfg.path or "config.json"} has CLIENT_BUILD '
            f'{cfg.get("CLIENT_BUILD")!r}: set "CLIENT_BUILD": "{build}" there (README_2009.md) '
            f'or drop --build / WS_BUILD')


def _child_env():
    """Environment for wsview children: the same client and build as this process."""
    env = os.environ.copy()
    env['WS_CLIENT'] = V.CLIENT
    env[CL.ENV_BUILD] = BUILD
    return env


def _opt(name, default):
    """Pop `--name value` from argv (login options: --user --pass --char)."""
    if name in sys.argv:
        i = sys.argv.index(name)
        val = sys.argv[i + 1]
        del sys.argv[i:i + 2]
        return val
    return default


LOGIN_USER = _opt('--user', 'test' if PRIMARY else 'admin')
LOGIN_PASS = _opt('--pass', 'test' if PRIMARY else 'admin')
LOGIN_CHAR = int(_opt('--char', '0'))

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
GS_FLAG = L.receive_busy_flag   # game_state+0x408 (2009 +0x418): NOT an in-world flag, see _player
SCENE_PTR = L.scene_ptr


# ----------------------------------------------------------------- processes ---
def _ps(cmd):
    return subprocess.run(['powershell', '-NoProfile', '-Command', cmd],
                          capture_output=True, text=True).stdout.strip()


def stop_all():
    # kill the selected client; the primary client's stop also stops the SERVER
    # (only python procs running windslayer_server.py, not us). Client 2 never
    # touches the server or client 1. Only the selected build's processes: the 2008 and
    # 2009 exes share the name WindSlayer_patched.exe (V._pids checks the PE timestamp).
    pids = V._pids()
    if pids:
        _ps(f"Stop-Process -Id {','.join(str(p) for p in pids)} -Force -ErrorAction SilentlyContinue")
    if not PRIMARY:
        time.sleep(0.5)
        return
    _ps("Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='python3.11.exe'\" | "
        "Where-Object { $_.CommandLine -like '*windslayer_server.py*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    time.sleep(0.9)


def server_up():
    return _ps(f"(Get-NetTCPConnection -State Listen -LocalPort {CFG.GAME_PORT} -ErrorAction SilentlyContinue | "
               "Measure-Object).Count") not in ('', '0')


def start_server():
    if server_up():
        return 'already running'
    DETACHED = 0x00000008 | 0x00000200            # DETACHED_PROCESS | NEW_PROCESS_GROUP
    subprocess.Popen([sys.executable or 'python', SERVER_PY], cwd=HERE,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=DETACHED, close_fds=True)
    for _ in range(20):
        time.sleep(0.3)
        if server_up():
            return 'started'
    return f'FAILED to bind {CFG.GAME_PORT}'


def launch_client():
    if V._pids():
        return 'already running'
    if not os.path.exists(EXE):
        hint = (' - build it: python ' + os.path.join(CLIENT_DIR, 'patch_2009.py')
                if BUILD == CL.BUILD_2009 else '')
        return f'FAILED: {EXE} not found{hint}'
    try:
        # 2009: one raw command line "<exe>" -<user> <pass> -x -x (client_layout.launch_command)
        cmd = CL.launch_command(BUILD, EXE, LOGIN_USER, LOGIN_PASS)
    except ValueError as e:
        return f'FAILED: {e}'
    env = os.environ.copy(); env['__COMPAT_LAYER'] = 'RunAsInvoker'
    subprocess.Popen(cmd, cwd=CLIENT_DIR, env=env, close_fds=True)
    for _ in range(20):
        time.sleep(0.3)
        if V._pids():
            return 'launched'
    return 'FAILED to launch'


# ------------------------------------------------------------------- memory ---
def _proc():
    pids = V._pids()
    if not pids:
        return None
    return kernel32.OpenProcess(0x10 | 0x400, False, pids[0])


def _rd(proc, a, n):
    b = (ctypes.c_ubyte * n)(); r = ctypes.c_size_t(0)
    kernel32.ReadProcessMemory(proc, ctypes.c_void_p(a), b, n, ctypes.byref(r))
    return bytes(b[:r.value]) if r.value else b''


def _u32(proc, a):
    d = _rd(proc, a, 4); return struct.unpack('<I', d)[0] if len(d) == 4 else 0


def _u16(proc, a):
    d = _rd(proc, a, 2); return struct.unpack('<H', d)[0] if len(d) == 2 else 0


def _u8(proc, a):
    d = _rd(proc, a, 1); return d[0] if d else 0


def _player(proc):
    """Return the local-player entity address, or 0 if not in-world.
    NOTE: game_state+0x408 (GS_FLAG) is NOT an in-world flag - OnReceive only
    processes packets while it is 0 (verified in the decompiled 0x44D5D0), so it
    reads 0 in-world. In-world = scene pointer set + an entity whose uid equals the
    client's own uid scene+0x220 (set from the S2C 0x02 account id). 2009: scene
    0x54F0C0, uid scene+0x224, entity uid +0x88 (client_layout.LAYOUT_2009)."""
    if not proc:
        return 0
    scene = _u32(proc, SCENE_PTR)
    if not scene:
        return 0
    my_uid = _u32(proc, scene + L.scene_local_uid)
    if not my_uid:
        return 0
    node = _u32(proc, scene + L.scene_entity_list); n = 0
    while node and 0x400000 <= node < 0x7FFF0000 and n < 80:
        e = _u32(proc, node + 8)
        if e and _u32(proc, e + L.ent_uid) == my_uid:
            return e
        node = _u32(proc, node); n += 1
    return 0


def is_inworld():
    proc = _proc()
    return bool(proc and _player(proc))


CHARSELECT_MODE = 5         # scene+0xF00 (2009 +0xF18): 5 character select, 6 field (login_character.md 1.2)


def is_charselect():
    """True on the character-select screen: the S2C 0x02 success handler loads
    main99_02.hmi and sets scene+0xF00 = 5 (2009: scene+0xF18, set at 0x4521E4); no local
    player entity exists yet."""
    proc = _proc()
    if not proc:
        return False
    scene = _u32(proc, SCENE_PTR)
    return bool(scene) and _u8(proc, scene + L.scene_mode) == CHARSELECT_MODE and not _player(proc)


# ---------------------------------------------------------------- auto-login ---
def _click_screen(x, y):
    user32.SetCursorPos(int(x), int(y)); time.sleep(0.05)
    user32.mouse_event(0x0002, 0, 0, 0, 0); time.sleep(0.05)
    user32.mouse_event(0x0004, 0, 0, 0, 0); time.sleep(0.15)


# Client-area coordinates measured from live screenshots (2026-09-17):
#   launcher 532x400: START (100,235)
#   login 800x600:    ID field (215,66), PASSWORD (253,97), Channel-1 (250,193), OK (189,468)
#   char-select:      slots at x=110/222/330/440, y=470; START (703,507)
LAUNCHER_START = (100, 235)
LOGIN_ID, LOGIN_PW, LOGIN_CHANNEL, LOGIN_OK = (215, 66), (253, 97), (250, 193), (189, 468)
CHAR_SLOTS_X, CHAR_SLOT_Y, CHARSEL_START = (110, 222, 330, 440), 470, (703, 507)

# 2009 auto-login (client-2009-tooling). Client-area coordinates; every one is a named
# constant for the lead to adjust after the first live run (README_2009.md live checks).
#   launcher: a modal dialog (resource 0x98, DialogProc FUN_00488190, title "WindSlayer
#     (Build: 14)") shown by WinMain 0x487D00 BEFORE the D3D window exists. Its START bitmap
#     is BitBlt'd at x 0x234..0x294, y 0x169..0x193 (564..660 x 361..403); 'Window' is the
#     windowed-mode radio above it (CheckRadioButton). Live-seen: (626,355), (613,381).
#   'Select World & Channel': the first screen of the game window; OK sends C2S 0x01 with
#     the launcher's first argument (there is no ID/password form in 2009).
#   character select: the 2008 slot / START positions as the first guess.
LAUNCHER_WINDOW_2009 = (626, 355)
LAUNCHER_START_2009 = (613, 381)
WORLD_CHANNEL_2009 = (250, 155)
WORLD_OK_2009 = (186, 422)
CHAR_SLOTS_X_2009, CHAR_SLOT_Y_2009, CHARSEL_START_2009 = CHAR_SLOTS_X, CHAR_SLOT_Y, CHARSEL_START
# The launcher is recognised by its window class (a DialogBoxParamA dialog) or its title.
LAUNCHER_CLASS_2009 = '#32770'
LAUNCHER_TITLE_2009 = '(Build:'
# Seconds: the version reply must be parsed before START (as in 2008, a START before it
# shows the version message in an "Error" box); then how long each screen may take.
LAUNCHER_SETTLE_2009 = 3.0
LAUNCHER_CLOSE_WAIT_2009 = 15.0
WORLD_LOGIN_WAIT_2009 = 10.0
ENTER_WORLD_WAIT_2009 = 20.0


def _wait(pred, secs, step=0.3):
    t0 = time.time()
    while time.time() - t0 < secs:
        if pred():
            return True
        time.sleep(step)
    return pred()


def _type_field(x, y, text):
    _click_screen(x, y)
    for _ in range(24):                       # clear remembered text
        V.key_tap(0x08, 0.01)
    for c in text:
        V.key_tap(V._vk_of(c))


def auto_login(timeout=90, user='test', password='test', char_index=0, stop_at_select=False):
    """Drive launcher -> login -> character select -> world. Returns True if in-world
    (stop_at_select: True once the character-select screen is up; no slot is clicked).
    The 2009 build has its own sequence (auto_login_2009)."""
    if BUILD == CL.BUILD_2009:
        return auto_login_2009(timeout, char_index=char_index, stop_at_select=stop_at_select)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if is_inworld():
            # already past character select; the client cannot go back without a relog
            return not stop_at_select
        if stop_at_select and is_charselect():
            return True
        h = V._find_hwnd()
        if not h:
            time.sleep(0.5); continue
        V._foreground(h)
        x, y, w, hgt = V._client_rect_screen(h)
        if w < 640:                            # launcher
            # Clicking START before the version-server reply is parsed makes the
            # client show the version message in an "Error" box - let it settle.
            time.sleep(3.0)
            _click_screen(x + LAUNCHER_START[0], y + LAUNCHER_START[1])
            _wait(lambda: (V._find_hwnd() and V._client_rect_screen(V._find_hwnd())[2] >= 640), 15)
            time.sleep(1.5)
            continue
        # login screen (the only full-size screen reachable before char-select)
        _type_field(x + LOGIN_ID[0], y + LOGIN_ID[1], user)
        _type_field(x + LOGIN_PW[0], y + LOGIN_PW[1], password)
        _click_screen(x + LOGIN_CHANNEL[0], y + LOGIN_CHANNEL[1])
        _click_screen(x + LOGIN_OK[0], y + LOGIN_OK[1])
        time.sleep(3.5)                        # char list arrives via S2C 0x02
        if stop_at_select:
            return _wait(is_charselect, 10)
        _click_screen(x + CHAR_SLOTS_X[char_index], y + CHAR_SLOT_Y)
        time.sleep(0.6)
        _click_screen(x + CHARSEL_START[0], y + CHARSEL_START[1])
        if _wait(is_inworld, 20):
            return True
    return is_inworld()


def is_launcher_2009(hwnd):
    """True while the 2009 launcher dialog is the client's window: class #32770 (it is a
    DialogBoxParamA dialog) or the "WindSlayer (Build: 14)" title FUN_00488190 sets (the
    string's only xref). The game window created after it is class "D3D Window"
    (CreateWindowExA in FUN_0040c140)."""
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    title = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, title, 256)
    return cls.value == LAUNCHER_CLASS_2009 or LAUNCHER_TITLE_2009 in title.value


def screen_2009(hwnd):
    """Which 2009 screen is up: 'world' (local player registered), 'select' (scene mode 5),
    'launcher' (the dialog), 'world_select' (the game window before C2S 0x01), or None
    when there is no window yet."""
    if is_inworld():
        return 'world'
    if is_charselect():
        return 'select'
    if not hwnd:
        return None
    return 'launcher' if is_launcher_2009(hwnd) else 'world_select'


def _click_client(hwnd, pt):
    x, y, _w, _h = V._client_rect_screen(hwnd)
    _click_screen(x + pt[0], y + pt[1])


def auto_login_2009(timeout=90, char_index=0, stop_at_select=False):
    """2009: launcher dialog ('Window', START) -> 'Select World & Channel' (channel, OK:
    C2S 0x01 carries the account from the command line) -> character select (slot, START)
    -> world. Each pass acts on the screen that is up, so it resumes from wherever the
    client is. Returns like auto_login."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        h = V._find_hwnd()
        screen = screen_2009(h)
        if screen == 'world':
            # already past character select; the client cannot go back without a relog
            return not stop_at_select
        if screen is None:
            time.sleep(0.5); continue
        V._foreground(h)
        if screen == 'select':
            if stop_at_select:
                return True
            _click_client(h, (CHAR_SLOTS_X_2009[char_index], CHAR_SLOT_Y_2009))
            time.sleep(0.6)
            _click_client(h, CHARSEL_START_2009)
            if _wait(is_inworld, ENTER_WORLD_WAIT_2009):
                return True
            continue
        if screen == 'launcher':
            time.sleep(LAUNCHER_SETTLE_2009)
            _click_client(h, LAUNCHER_WINDOW_2009)
            time.sleep(0.3)
            _click_client(h, LAUNCHER_START_2009)
            _wait(lambda: not (V._find_hwnd() and is_launcher_2009(V._find_hwnd())), LAUNCHER_CLOSE_WAIT_2009)
            time.sleep(1.5)
            continue
        # 'Select World & Channel'
        _click_client(h, WORLD_CHANNEL_2009)
        time.sleep(0.3)
        _click_client(h, WORLD_OK_2009)
        _wait(lambda: is_charselect() or is_inworld(), WORLD_LOGIN_WAIT_2009)
    return is_charselect() if stop_at_select else is_inworld()


# -------------------------------------------------------------------- status ---
def status():
    print(f'server ({CFG.VERSION_PORT}/{CFG.GAME_PORT} listening): {server_up()}  '
          f'config CLIENT_BUILD {CFG.get("CLIENT_BUILD")}')
    pids = V._pids()
    print(f'client build {BUILD} ({EXE}): {pids or "NOT running"}')
    proc = _proc()
    if not proc:
        print('  (cannot open client for memory read - launched? non-elevated?)'); return
    p = _player(proc)
    scene = _u32(proc, SCENE_PTR)
    where = 'IN-WORLD' if p else ('character select' if is_charselect()
                                  else 'not in world (launcher/login/loading)')
    print(f'  scene=0x{scene:08X}  local player: {where}')
    if p:
        lvl = _u8(proc, p + L.ent_level); hp = _u32(proc, p + L.ent_hp); mhp = _u32(proc, p + L.ent_max_hp)
        mp = _u32(proc, p + L.ent_mp); mmp = _u32(proc, p + L.ent_max_mp)
        x = struct.unpack('<d', _rd(proc, p + L.ent_pos_x, 8))[0] if len(_rd(proc, p + L.ent_pos_x, 8)) == 8 else 0
        y = struct.unpack('<d', _rd(proc, p + L.ent_pos_y, 8))[0] if len(_rd(proc, p + L.ent_pos_y, 8)) == 8 else 0
        print(f'  player @0x{p:08X}  Lv {lvl}  HP {hp}/{mhp}  MP {mp}/{mmp}  pos ({x:.0f},{y:.0f})')
        # count nearby monsters in the scene list
        scene = _u32(proc, SCENE_PTR); node = _u32(proc, scene + L.scene_entity_list); mob = 0; n = 0
        while node and 0x400000 <= node < 0x7FFF0000 and n < 80:
            e = _u32(proc, node + 8)
            if e and _u32(proc, e + L.ent_uid) >= 0x000F0000:
                mob += 1
            node = _u32(proc, node); n += 1
        print(f'  monsters visible: {mob}')
    drv = 'yes' if 'attached to client' in _tail_log(40) else 'unknown'
    print(f'  combat driver attached (recent log): {drv}')


def _tail_log(n=40):
    lp = os.path.join(GAME_DIR, '..', 'Windslayer 2', 'server_live.log')
    lp2 = os.path.join(HERE, 'server_live.log')
    for p in (os.path.join(os.getcwd(), 'server_live.log'), lp2, lp):
        if os.path.exists(p):
            try:
                return ''.join(open(p, encoding='utf-8', errors='replace').readlines()[-n:])
            except OSError:
                pass
    return ''


# --------------------------------------------------------------------- main ---
def _login_report(ok, select):
    if select:
        return 'AT CHARACTER SELECT' if ok else 'NOT at character select (in world already, or login failed)'
    return 'IN-WORLD' if ok else 'NOT in-world (try: wsdev login, or log in manually)'


def cmd_up(args):
    select = '--select' in args
    why = build_mismatch()
    if why:
        print(f'refused: {why}'); return
    print('stop...'); stop_all()
    print('server:', start_server())
    print(f'client {V.CLIENT} build {BUILD} ({EXE}):', launch_client())
    print(f'auto-login as {LOGIN_USER}' + (' (stop at character select)...' if select
                                          else f' char #{LOGIN_CHAR}...'), flush=True)
    ok = auto_login(user=LOGIN_USER, password=LOGIN_PASS, char_index=LOGIN_CHAR, stop_at_select=select)
    print(_login_report(ok, select))
    status()


def cmd_down(args):
    stop_all(); print('stopped server + client')


def cmd_restart(args):
    why = build_mismatch()
    if why:
        print(f'refused: {why}'); return
    stop_all(); cmd_up(args)


def cmd_login(args):
    select = '--select' in args
    ok = auto_login(user=LOGIN_USER, password=LOGIN_PASS, char_index=LOGIN_CHAR, stop_at_select=select)
    print(_login_report(ok, select)); status()


def cmd_hit(args):
    n = int(args[0]) if args else 1
    if not is_inworld():
        print('not in-world - nothing to hit'); return
    for i in range(n):
        open(SENTINEL, 'w').close()
        # wait for the driver to consume it (it polls ~20Hz)
        for _ in range(20):
            time.sleep(0.05)
            if not os.path.exists(SENTINEL):
                break
        time.sleep(0.15)
    print(f'forced {n} hit(s) - check server_live.log [ATK]/[KILL]/[LEVEL] and wsdev status')


def cmd_logs(args):
    n = int(args[0]) if args else 40
    txt = _tail_log(n)
    if not txt.strip():
        print('server_live.log not found or empty (server not started yet?)'); return
    sys.stdout.write(txt if txt.endswith('\n') else txt + '\n')


LIVE_LOG = os.path.join(HERE, 'server_live.log')
NOISE_OPCODES = {0x0D, 0x05}          # movement + heartbeat: hidden unless --all


def _packets_since(offset):
    """Parse server_live.log from byte offset: client->server packets (decoded, with
    payload bytes) and server->client opcodes the server sent, in order."""
    import re
    with open(LIVE_LOG, 'rb') as f:
        f.seek(offset)
        lines = f.read().decode('utf-8', 'replace').splitlines()
    events = []
    for i, line in enumerate(lines):
        m = re.search(r'\[FIREWAY\] Pkt: opcode=0x([0-9A-Fa-f]+) size=(\d+) seq=(\d+) no_enc=(\w+) payload=(\d+)B', line)
        if m:
            j = i - 1
            while j >= 0 and re.match(r'\s+[0-9A-F]{4}:', lines[j]):
                j -= 1
            hexb = []
            for k in range(j + 1, i):
                hexb += re.findall(r'\b[0-9A-F]{2}\b', lines[k].split(':', 1)[1][:49])
            plen = int(m.group(5))
            events.append(('C2S', int(m.group(1), 16), plen, ' '.join(hexb[9:9 + plen])))
            continue
        m = re.search(r'\[FIREWAY\] Send: opcode=0x([0-9A-Fa-f]+) seq=(\d+) size=(\d+)', line)
        if m:
            events.append(('S2C', int(m.group(1), 16), int(m.group(3)) - 9, ''))
    return events


def decode_c2s(op, hexstr, build=None):
    """One captured C2S payload decoded with the selected build's spec (registry.decode over
    protocol_spec.json / protocol_spec_2009.json): '<key> <name>: fields', or why not."""
    import registry
    try:
        payload = bytes.fromhex(hexstr.replace(' ', ''))
    except ValueError:
        return '[payload hex unreadable]'
    rec, err = registry.decode(op, payload, build or _server_build())
    if rec is None:
        return f'[{err}]' if err else f'[no {build or _server_build()} C2S spec for 0x{op:02X}]'
    return f'{rec.key} {rec.name}: {registry.format_fields(rec)}'


def cmd_cap(args):
    """cap [--all] <wait_secs> <wsview command...>  e.g.  cap 2 key q   |  cap 3 click 700 575
    Runs one UI action and prints the packets it produced (ground truth for RE specs); each
    client->server payload is also decoded with the selected build's spec."""
    show_all = '--all' in args
    args = [a for a in args if a != '--all']
    if len(args) < 2:
        print(cmd_cap.__doc__); return
    wait = float(args[0])
    start = os.path.getsize(LIVE_LOG) if os.path.exists(LIVE_LOG) else 0
    subprocess.run([sys.executable or 'python', os.path.join(HERE, 'wsview.py'), *args[1:]],
                   stdout=subprocess.DEVNULL, env=_child_env())
    time.sleep(wait)
    evs = _packets_since(start)
    shown = [e for e in evs if show_all or not (e[0] == 'C2S' and e[1] in NOISE_OPCODES)]
    hidden = len(evs) - len(shown)
    print(f'action: {" ".join(args[1:])}  ->  {len(shown)} packet(s)' + (f' (+{hidden} move/heartbeat hidden)' if hidden else '')
          + f'  [spec {BUILD}]')
    for d, op, plen, hx in shown:
        print(f'  {d} 0x{op:02X} payload={plen}B {hx}')
        if d == 'C2S':
            print(f'      = {decode_c2s(op, hx)}')


ADMIN_PORT = CFG.ADMIN_PORT


ADMIN_STATES = ('in_world', 'select', 'all')


def admin_line(opcode, payload, target=None, state=None):
    """One admin-port JSON command (GameServer._admin_command). target: uid int or
    account/character name; state: in_world (server default) | select | all."""
    import json as _json
    cmd = {'opcode': opcode, 'payload_hex': payload.hex()}
    if target is not None:
        cmd['target'] = int(target) if str(target).isdigit() else str(target)
    if state is not None:
        cmd['state'] = state
    return _json.dumps(cmd) + '\n'


def pop_target_opts(args):
    """Strip --to <uid|name|c:N> and --state <in_world|select|all> from args.
    Returns (target, state, remaining args); raises ValueError on a bad value."""
    target, state, rest = None, None, []
    i = 0
    while i < len(args):
        if args[i] in ('--to', '--state'):
            if i + 1 >= len(args) or args[i + 1].startswith('--'):
                raise ValueError(f'{args[i]} needs a value')
            if args[i] == '--to':
                target = args[i + 1]
            else:
                state = args[i + 1]
                if state not in ADMIN_STATES:
                    raise ValueError(f'--state must be one of {", ".join(ADMIN_STATES)}')
            i += 2
        else:
            rest.append(args[i]); i += 1
    return target, state, rest


def _inject(opcode, payload, target=None, state=None):
    import socket
    with socket.create_connection(('127.0.0.1', ADMIN_PORT), timeout=5) as s:
        s.sendall(admin_line(opcode, payload, target, state).encode())
        s.shutdown(socket.SHUT_WR)
        return s.recv(4096).decode().strip()


def _parse_assume(text):
    """--assume value: JSON {"condition text": true} or `condition text=1` (split on the
    last '=', handy where the shell mangles JSON quotes)."""
    import json as _json
    text = text.strip()
    if text.startswith('{'):
        return {str(k): bool(v) for k, v in _json.loads(text).items()}
    cond, sep, val = text.rpartition('=')
    if not sep or not cond.strip():
        raise ValueError(f'--assume expects JSON or "condition=1", got {text!r}')
    return {cond.strip(): val.strip().lower() in ('1', 'true', 'yes', 'y')}


def cmd_send(args):
    """send <opcode> [payload hex bytes...] [--to uid|name|c:N] [--state in_world|select|all]
    inject a raw S2C packet into the live client (default: in-world sessions only)"""
    try:
        target, state, args = pop_target_opts(args)
    except ValueError as e:
        print(e); return
    if not args:
        print(cmd_send.__doc__); return
    op = int(args[0], 16)
    payload = bytes.fromhex(''.join(args[1:]))
    print(_inject(op, payload, target, state))


def cmd_sendspec(args):
    """sendspec <opcode> ['{json fields}'] [--assume '{"condition": true}'] [--uid N] [--loose]
    Build an S2C payload with packets.build (grammar from the selected build's spec:
    protocol_spec.json, or protocol_spec_2009.json with --build 2009) and inject it.
    Nested repeat blocks use keys like "repeat[count]": [{...}]; "__assume__": {...} inside the
    JSON is honoured too. Client-state conditions start from the server's assume table
    (packets.DEFAULT_ASSUME); a condition that would change the bytes but has no value is
    refused. Supply it with --assume (repeatable; JSON or "condition=1"), --uid <receiving
    player's uid> for receiver-dependent forms (0x72/0x76/0x2A), or --loose (a condition
    with no value anywhere = False; DEFAULT_ASSUME values still apply, so use
    --assume '{"cond": false}' to force the other form).
    Only TCP S2C keys are accepted: UDP-* and C2S send-site keys are refused, since their
    bodies would reach the client as the TCP packet with the same opcode byte.
    `sendspec <opcode> ?` prints the grammar, its client-state conditions and the defaults.
    --to <uid|name|c:N> / --state in_world|select|all pick the receiving sessions (default:
    every in-world session); a numeric --to is the account uid and also serves as --uid when
    --uid is not given. c:N is the client exe N by the P2P port its 0x2B sent (c:2 =
    WindSlayer_p2.exe, 42908), whatever account it runs: `sendspec 15 '{...}' --to c:2`
    reaches client B only. The colon keeps it apart from a character called "c2"."""
    import json as _json
    sys.path.insert(0, HERE)
    import packets as P
    cb = _server_build()
    try:
        target, state, args = pop_target_opts(args)
    except ValueError as e:
        print(e); return
    assume, uid, loose, rest = {}, None, False, []
    i = 0
    while i < len(args):
        if args[i] in ('--assume', '--uid'):
            if i + 1 >= len(args) or args[i + 1].startswith('--'):
                # a value-less flag used to fall through into the JSON fields and crash
                print(f'{args[i]} needs a value (see: wsdev sendspec)'); return
            try:
                if args[i] == '--assume':
                    assume.update(_parse_assume(args[i + 1]))
                else:
                    uid = int(args[i + 1], 0)
            except ValueError as e:
                print(f'bad {args[i]} value {args[i + 1]!r}: {e}'); return
            i += 2
        elif args[i] == '--loose':
            loose = True; i += 1
        else:
            rest.append(args[i]); i += 1
    if not rest:
        print(cmd_sendspec.__doc__); return
    try:
        spec = P.spec(rest[0], 'S2C', client_build=cb)
    except KeyError as e:
        print(f'no TCP S2C spec: {e}'); return
    op = int(spec['opcode'], 16)
    if len(rest) > 1 and rest[1] == '?':
        print(f'{spec["key"]} {spec["name"]} ({spec.get("confidence")})\n{spec["grammar"]}\n')
        print('client-state conditions (supply with --assume):')
        defaults = P.assume_defaults(spec['key'], client_build=cb)
        for expr in P.client_state_exprs(spec['key'], client_build=cb):
            print(f'  {expr!r}  default: {defaults.get(expr, "none - REQUIRED if it changes the bytes")}')
        if not P.client_state_exprs(spec['key'], client_build=cb):
            print('  (none)')
        print(f'\n{spec.get("semantics", "")}'); return
    try:
        fields = _json.loads(' '.join(rest[1:])) if len(rest) > 1 else {}
    except ValueError as e:
        print(f'fields are not JSON ({e}): {" ".join(rest[1:])!r}'); return
    if not isinstance(fields, dict):
        print('fields must be a JSON object'); return
    if uid is None and target is not None and str(target).isdigit():
        uid = int(target)
    try:
        payload = P.build(spec['key'], fields, assume, receiver_uid=uid, allow_unassumed=loose,
                          client_build=cb)
    except P.MissingAssume as e:
        print(f'refused: {e}')
        for expr in e.conditions:
            print(f'  --assume \'{_json.dumps({expr: True})}\'')
        return
    except P.PacketError as e:
        print(f'refused: {e}'); return
    except (ValueError, TypeError, struct.error) as e:          # e.g. a value out of range for its field
        print(f'bad field value: {e}'); return
    print(f'0x{op:02X} {spec["name"]}: {len(payload)}B {payload.hex(" ")}')
    print(_inject(op, payload, target, state))


def cmd_gm(args):
    """gm <character> [0|1]
    Set (default 1) or clear a character's GM flag through the running server's store
    (chat_mail_gm-gm-flag-manner). This is how the FIRST GM is made: accounts.json is owned
    by the server while it runs, so a hand edit would be overwritten by the next save.
    The client only reads gm_level from its S2C 0x07 record, so the target must take a
    portal or relog before its own /manner, /not, /go ... work; the server-side '!'
    commands work as soon as the session is flagged."""
    import json as _json
    import socket
    if not args:
        print(cmd_gm.__doc__); return
    cmd = {'gm': args[0], 'level': int(args[1], 0) if len(args) > 1 else 1}
    with socket.create_connection(('127.0.0.1', ADMIN_PORT), timeout=5) as s:
        s.sendall((_json.dumps(cmd) + '\n').encode())
        s.shutdown(socket.SHUT_WR)
        print(s.recv(4096).decode().strip())


def cmd_dev(args):
    """dev <character> <!command ...>
    Run one '!' dev command as that in-world character through the running server's admin
    port (GameServer._admin_dev), e.g.  dev TestHero !level 3   dev Watcher !where
    The answer is the usual 0x15 line on that character's client. Use it instead of typing
    into the 2009 chat box, which is unreliable (stray keys reach game hotkeys)."""
    import json as _json
    import socket
    if len(args) < 2:
        print(cmd_dev.__doc__); return
    cmd = {'dev': args[0], 'cmd': ' '.join(args[1:])}
    with socket.create_connection(('127.0.0.1', ADMIN_PORT), timeout=5) as s:
        s.sendall((_json.dumps(cmd) + '\n').encode())
        s.shutdown(socket.SHUT_WR)
        print(s.recv(4096).decode().strip())


def cmd_admin(args):
    """admin '{json}'
    Send one raw JSON line to the running server's admin port and print the reply
    (GameServer._admin_command): {"kick": "Watcher", "reason": 4}, {"shutdown": 1},
    {"maintenance": 0}, {"gm": ...}, {"dev": ...}, or an injection line."""
    import json as _json
    import socket
    if not args:
        print(cmd_admin.__doc__); return
    line = ' '.join(args)
    try:
        _json.loads(line)
    except ValueError as e:
        print(f'not JSON ({e}): {line!r}'); return
    with socket.create_connection(('127.0.0.1', ADMIN_PORT), timeout=5) as s:
        s.sendall((line + '\n').encode())
        s.shutdown(socket.SHUT_WR)
        print(s.recv(4096).decode().strip())


def main():
    if len(sys.argv) < 2:
        print(__doc__); return
    cmd, rest = sys.argv[1], sys.argv[2:]
    table = {'up': cmd_up, 'down': cmd_down, 'restart': cmd_restart, 'login': cmd_login,
             'status': lambda a: status(), 'hit': cmd_hit, 'logs': cmd_logs, 'cap': cmd_cap,
             'send': cmd_send, 'sendspec': cmd_sendspec, 'gm': cmd_gm, 'dev': cmd_dev,
             'admin': cmd_admin}
    if cmd in table:
        table[cmd](rest)
    elif cmd in ('shot', 'state', 'key', 'hold', 'click', 'dclick', 'rclick', 'drag', 'type', 'watch', 'win'):
        # delegate to wsview (same --client / --build)
        subprocess.run([sys.executable or 'python', os.path.join(HERE, 'wsview.py'), cmd, *rest],
                       env=_child_env())
    else:
        print(__doc__)


if __name__ == '__main__':
    main()
