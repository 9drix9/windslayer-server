#!/usr/bin/env python3
"""
wsdev.py — WindSlayer one-command DEV HARNESS
=============================================
Brings the whole stack up and lets an AI agent (or dev) debug solo, no manual
clicking required for the common loop:

  up                 stop stale procs -> start server -> launch client
                     NON-ELEVATED -> auto-login to in-world -> report state
  down               stop server + client
  restart            down + up (use after editing windslayer_server.py)
  login              run the auto-login sequence only (client already launched)
  status             server / client / in-world / combat-driver / player summary
  hit [n]            DEBUG: force the server to resolve n melee hits on the
                     nearest monster (via the _dbg_attack sentinel) — test combat
                     feedback / leveling without a real keypress
  shot [path]        screenshot the game window (delegates to wsview)
  state              live entity dump (delegates to wsview)

Everything is built on wsview.py (same dir). Server logs to server_live.log.
Launch must be NON-ELEVATED for ReadProcessMemory; this tool always launches the
client with __COMPAT_LAYER=RunAsInvoker so combat/memory work.
"""
import os, sys, time, struct, subprocess, ctypes
import ctypes.wintypes as wt

HERE = os.path.dirname(os.path.abspath(__file__))
GAME_DIR = os.path.dirname(HERE)                      # ...\WindSlayer2Game
EXE = os.path.join(GAME_DIR, 'WindSlayer_patched.exe')
SERVER_PY = os.path.join(HERE, 'windslayer_server.py')
SENTINEL = os.path.join(HERE, '_dbg_attack')

sys.path.insert(0, HERE)
import wsview as V                                    # reuse window/mem/input helpers

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
GS_FLAG = 0x70EE08          # game_state+0x408: != 0 once truly in-world
SCENE_PTR = 0x70EECC


# ----------------------------------------------------------------- processes ---
def _ps(cmd):
    return subprocess.run(['powershell', '-NoProfile', '-Command', cmd],
                          capture_output=True, text=True).stdout.strip()


def stop_all():
    # kill the client, and only the python procs running the SERVER (not us)
    _ps("Get-Process -Name WindSlayer_patched -ErrorAction SilentlyContinue | Stop-Process -Force")
    _ps("Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='python3.11.exe'\" | "
        "Where-Object { $_.CommandLine -like '*windslayer_server.py*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    time.sleep(0.9)


def server_up():
    return _ps("(Get-NetTCPConnection -State Listen -LocalPort 7022 -ErrorAction SilentlyContinue | "
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
    return 'FAILED to bind 7022'


def launch_client():
    if V._pids():
        return 'already running'
    env = os.environ.copy(); env['__COMPAT_LAYER'] = 'RunAsInvoker'
    subprocess.Popen([EXE], cwd=GAME_DIR, env=env, close_fds=True)
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
    """Return the local-player entity address (uid==1), or 0 if not in-world."""
    if not proc or _u32(proc, GS_FLAG) == 0:
        return 0
    scene = _u32(proc, SCENE_PTR)
    node = _u32(proc, scene + 0xc); n = 0
    while node and 0x400000 <= node < 0x7FFF0000 and n < 80:
        e = _u32(proc, node + 8)
        if e and _u32(proc, e + 0x84) == 1:
            return e
        node = _u32(proc, node); n += 1
    return 0


def is_inworld():
    proc = _proc()
    return bool(proc and _player(proc))


# ---------------------------------------------------------------- auto-login ---
def _click_screen(x, y):
    user32.SetCursorPos(int(x), int(y)); time.sleep(0.05)
    user32.mouse_event(0x0002, 0, 0, 0, 0); time.sleep(0.05)
    user32.mouse_event(0x0004, 0, 0, 0, 0); time.sleep(0.15)


def auto_login(timeout=70):
    """Best-effort: get the client from launcher/login/char-select into the world.
    Polls the in-world memory flag; drives the known sequence relative to the
    game window. The client often remembers creds, so START/OK/Enter usually
    suffice. Returns True if in-world."""
    t0 = time.time()
    typed = False
    while time.time() - t0 < timeout:
        if is_inworld():
            return True
        h = V._find_hwnd()
        if not h:
            time.sleep(0.5); continue
        V._foreground(h)
        x, y, w, hgt = V._client_rect_screen(h)
        if w < 640:
            # LAUNCHER (small window): click the START button (left-center).
            for fx, fy in ((0.14, 0.33), (0.14, 0.40), (0.5, 0.85)):
                _click_screen(x + w * fx, y + hgt * fy)
                if is_inworld():
                    return True
            time.sleep(1.0)
        else:
            # LOGIN / CHARACTER-SELECT (full window). Fill creds once, then
            # Enter (login) and double-click the first char + Enter (select).
            if not typed:
                _click_screen(x + w * 0.62, y + hgt * 0.30)        # ID field
                for c in 'test':
                    V.key_tap(V._vk_of(c))
                V.key_tap(V._vk_of('tab'))
                for c in 'test':
                    V.key_tap(V._vk_of(c))
                typed = True
            V.key_tap(V._vk_of('enter')); time.sleep(0.8)
            if is_inworld():
                return True
            # char-select: double-click first char slot then confirm
            _click_screen(x + w * 0.20, y + hgt * 0.55)
            _click_screen(x + w * 0.20, y + hgt * 0.55)
            V.key_tap(V._vk_of('enter')); time.sleep(1.0)
    return is_inworld()


# -------------------------------------------------------------------- status ---
def status():
    print(f'server (7011/7022 listening): {server_up()}')
    pids = V._pids()
    print(f'client process: {pids or "NOT running"}')
    proc = _proc()
    if not proc:
        print('  (cannot open client for memory read — launched? non-elevated?)'); return
    inw = _u32(proc, GS_FLAG)
    print(f'  in-world flag (0x70EE08): {inw}  ({"IN-WORLD" if inw else "launcher/login/loading"})')
    p = _player(proc)
    if p:
        lvl = _u8(proc, p + 0x99); hp = _u32(proc, p + 0x9c); mhp = _u32(proc, p + 0x110c)
        mp = _u32(proc, p + 0xa0); mmp = _u32(proc, p + 0x1110)
        x = struct.unpack('<d', _rd(proc, p + 0x11f8, 8))[0] if len(_rd(proc, p + 0x11f8, 8)) == 8 else 0
        y = struct.unpack('<d', _rd(proc, p + 0x1288, 8))[0] if len(_rd(proc, p + 0x1288, 8)) == 8 else 0
        print(f'  player @0x{p:08X}  Lv {lvl}  HP {hp}/{mhp}  MP {mp}/{mmp}  pos ({x:.0f},{y:.0f})')
        # count nearby monsters in the scene list
        scene = _u32(proc, SCENE_PTR); node = _u32(proc, scene + 0xc); mob = 0; n = 0
        while node and 0x400000 <= node < 0x7FFF0000 and n < 80:
            e = _u32(proc, node + 8)
            if e and _u32(proc, e + 0x84) >= 0x000F0000:
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
def cmd_up(args):
    print('stop-all...'); stop_all()
    print('server:', start_server())
    print('client:', launch_client())
    print('auto-login...', flush=True)
    ok = auto_login()
    print('IN-WORLD' if ok else 'NOT in-world (try: wsdev login, or log in manually)')
    status()


def cmd_down(args):
    stop_all(); print('stopped server + client')


def cmd_restart(args):
    stop_all(); cmd_up(args)


def cmd_login(args):
    print('IN-WORLD' if auto_login() else 'still not in-world'); status()


def cmd_hit(args):
    n = int(args[0]) if args else 1
    if not is_inworld():
        print('not in-world — nothing to hit'); return
    for i in range(n):
        open(SENTINEL, 'w').close()
        # wait for the driver to consume it (it polls ~20Hz)
        for _ in range(20):
            time.sleep(0.05)
            if not os.path.exists(SENTINEL):
                break
        time.sleep(0.15)
    print(f'forced {n} hit(s) — check server_live.log [ATK]/[KILL]/[LEVEL] and wsdev status')


def main():
    if len(sys.argv) < 2:
        print(__doc__); return
    cmd, rest = sys.argv[1], sys.argv[2:]
    table = {'up': cmd_up, 'down': cmd_down, 'restart': cmd_restart, 'login': cmd_login,
             'status': lambda a: status(), 'hit': cmd_hit}
    if cmd in table:
        table[cmd](rest)
    elif cmd in ('shot', 'state', 'key', 'hold', 'click', 'watch', 'win'):
        # delegate to wsview
        subprocess.run([sys.executable or 'python', os.path.join(HERE, 'wsview.py'), cmd, *rest])
    else:
        print(__doc__)


if __name__ == '__main__':
    main()
