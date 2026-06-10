#!/usr/bin/env python3
"""
wsview.py — WindSlayer live VIEW + DEBUG + CONTROL harness
==========================================================
The companion to wsre.py. Where wsre.py reverse-engineers the binary, wsview.py
drives the *running* client so the developer (or an AI agent) can see the screen,
inspect live game objects, and inject input — a full debug/remote-control loop.

Client must be running NON-ELEVATED (so ReadProcessMemory + SendInput work).

  shot [path]            screenshot the game window -> PNG (default _shot.png)
  state                  player + nearby entities (uid/alive/pos/anim/hp)
  key <tokens...>        tap keys into the game (e.g.  key right right space)
  hold <key> <ms>        hold one key for <ms> milliseconds (e.g. hold right 700)
  click <x> <y>          left-click at CLIENT coords (relative to game window)
  watch [n] [delay]      take n shots delay sec apart -> _watch_000.png ...
  win                    print window handle / rect / foreground state

Key tokens: arrow names (left/right/up/down), space, enter, esc, tab,
single letters/digits (a, z, 1 ...), or fN. Case-insensitive.

Examples:
  python wsview.py shot
  python wsview.py state
  python wsview.py key right right
  python wsview.py hold right 800
  python wsview.py click 400 300
"""
import os, sys, struct, time, ctypes
import ctypes.wintypes as wt

HERE = os.path.dirname(os.path.abspath(__file__))
SHOT_DEFAULT = os.path.join(HERE, '_shot.png')

# --- live-client constants (mirror wsre.py) ---------------------------------
GAME_STATE = 0x70EA00
SCENE_PTR  = 0x70EECC
ANIM_PTR   = 0x70EEE0
WINDOW_TITLE = 'WindSlayer'
PROC_NAME = 'WindSlayer_patched.exe'

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ============================================================ process / memory
def _pids(name=PROC_NAME):
    class PE32(ctypes.Structure):
        _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ProcessID', wt.DWORD),
                    ('th32DefaultHeapID', ctypes.c_void_p), ('th32ModuleID', wt.DWORD),
                    ('cntThreads', wt.DWORD), ('th32ParentProcessID', wt.DWORD),
                    ('pcPriClassBase', wt.LONG), ('dwFlags', wt.DWORD), ('szExeFile', wt.WCHAR * 260)]
    out = []
    snap = kernel32.CreateToolhelp32Snapshot(2, 0)
    e = PE32(); e.dwSize = ctypes.sizeof(e)
    if not kernel32.Process32FirstW(snap, ctypes.byref(e)):
        return out
    while True:
        if e.szExeFile.lower() == name.lower():
            out.append(e.th32ProcessID)
        if not kernel32.Process32NextW(snap, ctypes.byref(e)):
            return out


def _open_inworld():
    """(proc, pid) for first in-world client; falls back to any open."""
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    chosen = None
    for pid in _pids():
        proc = kernel32.OpenProcess(0x10 | 0x400, False, pid)
        if not proc:
            continue
        if chosen is None:
            chosen = (proc, pid)
        b = (ctypes.c_ubyte * 4)(); r = ctypes.c_size_t(0)
        if kernel32.ReadProcessMemory(proc, ctypes.c_void_p(SCENE_PTR), b, 4, ctypes.byref(r)) and r.value == 4:
            if struct.unpack('<I', bytes(b))[0]:
                return proc, pid
    return chosen or (None, None)


def _reader(proc):
    def rd(a, n):
        b = (ctypes.c_ubyte * n)(); r = ctypes.c_size_t(0)
        kernel32.ReadProcessMemory(proc, ctypes.c_void_p(a), b, n, ctypes.byref(r))
        return bytes(b[:r.value]) if r.value else None
    return rd


# ============================================================ window helpers
def _find_hwnd(pid=None):
    """Top-level visible window for the game (optionally a specific pid)."""
    result = []
    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        if not user32.IsWindowVisible(h):
            return True
        wpid = wt.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(wpid))
        if pid and wpid.value != pid:
            return True
        n = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(h, n, 256)
        if WINDOW_TITLE.lower() in n.value.lower():
            # prefer the real game window (has a client area)
            r = wt.RECT(); user32.GetClientRect(h, ctypes.byref(r))
            result.append((h, (r.right - r.left) * (r.bottom - r.top)))
        return True
    user32.EnumWindows(cb, 0)
    if not result:
        return None
    result.sort(key=lambda t: -t[1])      # biggest client area = game canvas
    return result[0][0]


def _client_rect_screen(hwnd):
    r = wt.RECT(); user32.GetClientRect(hwnd, ctypes.byref(r))
    pt = wt.POINT(r.left, r.top); user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, r.right - r.left, r.bottom - r.top


def _foreground(hwnd):
    user32.ShowWindow(hwnd, 9)            # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.12)


# ============================================================ input injection
# SendInput with SCANCODE so DirectInput / GetAsyncKeyState games respond.
class _KBD(ctypes.Structure):
    _fields_ = [('wVk', wt.WORD), ('wScan', wt.WORD), ('dwFlags', wt.DWORD),
                ('time', wt.DWORD), ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong))]
class _MOUSE(ctypes.Structure):
    _fields_ = [('dx', wt.LONG), ('dy', wt.LONG), ('mouseData', wt.DWORD),
                ('dwFlags', wt.DWORD), ('time', wt.DWORD), ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong))]
class _IU(ctypes.Union):
    _fields_ = [('ki', _KBD), ('mi', _MOUSE)]
class _INPUT(ctypes.Structure):
    _fields_ = [('type', wt.DWORD), ('u', _IU)]

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_EXTENDED = 0x0001

# VK name -> VK code (we convert to scancode via MapVirtualKey)
_VK = {
    'left': 0x25, 'up': 0x26, 'right': 0x27, 'down': 0x28,
    'space': 0x20, 'enter': 0x0D, 'esc': 0x1B, 'escape': 0x1B, 'tab': 0x09,
    'ctrl': 0x11, 'shift': 0x10, 'alt': 0x12, 'lalt': 0x12,
    'q': 0x51,
}
_EXTENDED = {0x25, 0x26, 0x27, 0x28}      # arrows need the extended flag


def _vk_of(token):
    t = token.lower()
    if t in _VK:
        return _VK[t]
    if len(t) == 1:
        c = t.upper()
        return ord(c)
    if t.startswith('f') and t[1:].isdigit():
        return 0x70 + int(t[1:]) - 1
    raise ValueError(f'unknown key token: {token}')


def _send_scan(vk, up):
    scan = user32.MapVirtualKeyW(vk, 0)
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    if vk in _EXTENDED:
        flags |= KEYEVENTF_EXTENDED
    inp = _INPUT(type=1)
    inp.u.ki = _KBD(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=None)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def key_tap(vk, dur=0.05):
    _send_scan(vk, False); time.sleep(dur); _send_scan(vk, True); time.sleep(0.03)


def mouse_click_screen(x, y):
    user32.SetCursorPos(int(x), int(y)); time.sleep(0.04)
    for f in (0x0002, 0x0004):            # LEFTDOWN, LEFTUP
        inp = _INPUT(type=0)
        inp.u.mi = _MOUSE(dx=0, dy=0, mouseData=0, dwFlags=f, time=0, dwExtraInfo=None)
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp)); time.sleep(0.03)


# ============================================================ commands
def cmd_shot(args):
    from PIL import ImageGrab
    path = args[0] if args else SHOT_DEFAULT
    hwnd = _find_hwnd()
    if not hwnd:
        print('game window not found'); return
    _foreground(hwnd)
    x, y, w, h = _client_rect_screen(hwnd)
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    img.save(path)
    print(f'saved {path}  ({w}x{h})  hwnd=0x{hwnd:X}')


def cmd_watch(args):
    from PIL import ImageGrab
    n = int(args[0]) if args else 5
    delay = float(args[1]) if len(args) > 1 else 1.0
    hwnd = _find_hwnd()
    if not hwnd:
        print('game window not found'); return
    _foreground(hwnd)
    x, y, w, h = _client_rect_screen(hwnd)
    for i in range(n):
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(os.path.join(HERE, f'_watch_{i:03d}.png'))
        print(f'_watch_{i:03d}.png')
        if i < n - 1:
            time.sleep(delay)


def cmd_win(args):
    hwnd = _find_hwnd()
    if not hwnd:
        print('game window not found'); return
    x, y, w, h = _client_rect_screen(hwnd)
    fg = user32.GetForegroundWindow()
    print(f'hwnd=0x{hwnd:X} client@screen=({x},{y}) size={w}x{h} foreground={fg == hwnd}')


def cmd_key(args):
    if not args:
        print('usage: key <token> [token...]'); return
    hwnd = _find_hwnd()
    if hwnd:
        _foreground(hwnd)
    for tok in args:
        key_tap(_vk_of(tok))
        print(f'tapped {tok}')


def cmd_hold(args):
    if len(args) < 2:
        print('usage: hold <key> <ms>'); return
    vk = _vk_of(args[0]); ms = int(args[1])
    hwnd = _find_hwnd()
    if hwnd:
        _foreground(hwnd)
    _send_scan(vk, False); time.sleep(ms / 1000.0); _send_scan(vk, True)
    print(f'held {args[0]} for {ms}ms')


def cmd_click(args):
    if len(args) < 2:
        print('usage: click <x> <y>  (client coords)'); return
    cx, cy = int(args[0]), int(args[1])
    hwnd = _find_hwnd()
    if not hwnd:
        print('game window not found'); return
    _foreground(hwnd)
    x, y, w, h = _client_rect_screen(hwnd)
    mouse_click_screen(x + cx, y + cy)
    print(f'clicked client ({cx},{cy}) -> screen ({x+cx},{y+cy})')


def cmd_borderless(args):
    """Strip the window caption/border and resize to cover the monitor.
    The D3D9 backbuffer stays 800x600, so windowed Present STRETCHES it to the
    new client area => stable borderless fullscreen, HUD preserved, GPU-scaled.
    Pass 'reset' to restore a normal 800x600 titled window."""
    GWL_STYLE = -16
    WS_CAPTION = 0x00C00000; WS_THICKFRAME = 0x00040000
    WS_MINIMIZEBOX = 0x00020000; WS_MAXIMIZEBOX = 0x00010000
    WS_SYSMENU = 0x00080000; WS_BORDER = 0x00800000; WS_DLGFRAME = 0x00400000
    WS_POPUP = 0x80000000; WS_VISIBLE = 0x10000000
    SWP_FRAMECHANGED = 0x0020; SWP_SHOWWINDOW = 0x0040; SWP_NOZORDER = 0x0004
    hwnd = _find_hwnd()
    if not hwnd:
        print('game window not found'); return
    st = user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
    if args and args[0] == 'reset':
        new = (st | WS_CAPTION | WS_THICKFRAME | WS_SYSMENU) & ~WS_POPUP
        user32.SetWindowLongW(hwnd, GWL_STYLE, new)
        user32.SetWindowPos(hwnd, 0, 200, 100, 820, 660, SWP_FRAMECHANGED | SWP_SHOWWINDOW | SWP_NOZORDER)
        print('restored windowed'); return
    sw = user32.GetSystemMetrics(0); sh = user32.GetSystemMetrics(1)
    new = (st & ~(WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX |
                  WS_SYSMENU | WS_BORDER | WS_DLGFRAME)) | WS_POPUP | WS_VISIBLE
    user32.SetWindowLongW(hwnd, GWL_STYLE, new & 0xFFFFFFFF)
    user32.SetWindowPos(hwnd, 0, 0, 0, sw, sh, SWP_FRAMECHANGED | SWP_SHOWWINDOW | SWP_NOZORDER)
    _foreground(hwnd)
    print(f'borderless: hwnd=0x{hwnd:X} -> {sw}x{sh} (style 0x{st:08X} -> 0x{new & 0xFFFFFFFF:08X})')


def cmd_autoborderless(args):
    """Background watcher: once the game window exists, force it borderless and
    keep it that way (re-applies after map changes / device resets that resize
    the window). Exits when the game closes. Aspect: pass 'fit' to pillarbox
    (preserve 4:3), default 'fill' stretches edge-to-edge."""
    GWL_STYLE = -16
    WS_CAPTION = 0x00C00000; WS_THICKFRAME = 0x00040000
    WS_MINIMIZEBOX = 0x00020000; WS_MAXIMIZEBOX = 0x00010000
    WS_SYSMENU = 0x00080000; WS_BORDER = 0x00800000; WS_DLGFRAME = 0x00400000
    WS_POPUP = 0x80000000; WS_VISIBLE = 0x10000000
    SWP_FRAMECHANGED = 0x0020; SWP_SHOWWINDOW = 0x0040; SWP_NOZORDER = 0x0004
    mode = (args[0] if args else 'fill').lower()
    sw = user32.GetSystemMetrics(0); sh = user32.GetSystemMetrics(1)
    if mode == 'fit':
        tw = min(sw, int(sh * 4 / 3)); th = min(sh, int(sw * 3 / 4))
    else:
        tw, th = sw, sh
    tx, ty = (sw - tw) // 2, (sh - th) // 2
    print(f'auto-borderless ({mode}) -> {tw}x{th}@({tx},{ty}); watching... (Ctrl-C to stop)')
    misses = 0
    while True:
        hwnd = _find_hwnd()
        if not hwnd:
            misses += 1
            if misses > 20:          # game gone for ~10s
                print('game closed; exiting watcher'); return
            time.sleep(0.5); continue
        misses = 0
        r = wt.RECT(); user32.GetWindowRect(hwnd, ctypes.byref(r))
        cr = wt.RECT(); user32.GetClientRect(hwnd, ctypes.byref(cr))
        # only transform once the real game canvas exists (client >= 640px) and
        # it's not already at our target rect
        if cr.right >= 640 and not (r.left == tx and r.top == ty and
                                    (r.right - r.left) == tw and (r.bottom - r.top) == th):
            st = user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
            new = (st & ~(WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX |
                          WS_SYSMENU | WS_BORDER | WS_DLGFRAME)) | WS_POPUP | WS_VISIBLE
            user32.SetWindowLongW(hwnd, GWL_STYLE, new & 0xFFFFFFFF)
            user32.SetWindowPos(hwnd, 0, tx, ty, tw, th,
                                SWP_FRAMECHANGED | SWP_SHOWWINDOW | SWP_NOZORDER)
            print(f'applied borderless to hwnd=0x{hwnd:X}')
        time.sleep(0.7)


def cmd_state(args):
    proc, pid = _open_inworld()
    if not proc:
        print('no readable client (run it NON-elevated)'); return
    rd = _reader(proc)
    def u32(a): d = rd(a, 4); return struct.unpack('<I', d)[0] if d else None
    def u8(a):  d = rd(a, 1); return d[0] if d else None
    def f64(a): d = rd(a, 8); return struct.unpack('<d', d)[0] if d else 0.0
    def cstr(a, n=18):
        d = rd(a, n); z = d.find(b'\x00') if d else -1
        return ''.join(chr(c) if 32 <= c < 127 else '.' for c in (d[:z if z >= 0 else n] if d else b''))
    scene = u32(SCENE_PTR); anim = u32(ANIM_PTR)
    print(f'PID {pid}  scene=0x{(scene or 0):08X}  anim=0x{(anim or 0):08X}')
    if not scene:
        print('  (not in world)'); return
    node = u32(scene + 0xc); n = 0
    print(f'  {"addr":>10} {"name":<16} {"alive":>5} {"uid":>10} {"anim":>5}  pos')
    while node and 0x400000 <= node < 0x7FFF0000 and n < 80:
        ent = u32(node + 8)
        if ent and 0x400000 <= ent < 0x7FFF0000:
            nm = cstr(ent)
            if nm and any(ch.isalnum() for ch in nm):
                print(f'  0x{ent:08X} {nm:<16} {u8(ent+0x98)!s:>5} '
                      f'0x{(u32(ent+0x84) or 0):08X} {u32(ent+0xe60)!s:>5}  '
                      f'({f64(ent+0x11f8):.0f},{f64(ent+0x1288):.0f})')
        node = u32(node); n += 1
    print(f'  ({n} list nodes walked)')


def main():
    if len(sys.argv) < 2:
        print(__doc__); return
    cmd, rest = sys.argv[1], sys.argv[2:]
    table = {'shot': cmd_shot, 'watch': cmd_watch, 'win': cmd_win, 'key': cmd_key,
             'hold': cmd_hold, 'click': cmd_click, 'state': cmd_state,
             'borderless': cmd_borderless, 'autoborderless': cmd_autoborderless}
    fn = table.get(cmd)
    if not fn:
        print(__doc__); return
    fn(rest)


if __name__ == '__main__':
    main()
