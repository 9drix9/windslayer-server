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
  type [--enter] [--delay ms] <text...>
                         type text one character at a time as real key presses
                         (shift chords for capitals and symbols), e.g.
                         type /w Alice hi   |   type --enter /manner test -50
  click <x> <y>          left-click at CLIENT coords (relative to game window)
  dclick <x> <y>         double-click (WM_LBUTTONDBLCLK 0x203: bag/equip items, popup 0x50)
  rclick <x> <y>         right-click (WM_RBUTTONUP 0x205: player popup 0x50)
  drag <x1> <y1> <x2> <y2>
                         press at (x1,y1), move, release at (x2,y2) (drag-and-drop)
  watch [n] [delay]      take n shots delay sec apart -> _watch_000.png ...
  win                    print window handle / rect / foreground state

Key tokens: arrow names (left/right/up/down), space, enter, esc, tab, backspace,
slash (/), minus (-), period (.), comma, semicolon, quote, equals, lbracket,
rbracket, backslash, backtick, single letters/digits (a, z, 1 ...), a single
symbol character (/ - . ?), or fN. Case-insensitive. Chords: shift+a, ctrl+c.

--client 2 (any command) targets the second client WindSlayer_p2.exe instead of
WindSlayer_patched.exe (same as env WS_CLIENT=2).

--build 2009 (any command) drives the EN 2009 client (Outspark v1.04 Build 14,
WindSlayer2009) with its own memory layout (client_layout.py, client_map_2009.json); same as
env WS_BUILD=2009. Default: the build the server speaks (config.json CLIENT_BUILD), which is
2008 unless changed. Both builds' exes are named WindSlayer_patched.exe, so the tools only
pick processes whose image carries the selected build's PE timestamp.

Examples:
  python wsview.py shot
  python wsview.py state
  python wsview.py key right right
  python wsview.py hold right 800
  python wsview.py click 400 300
  python wsview.py type --enter hi
  python wsview.py key slash w space shift+a l i c e
"""
import os, sys, struct, time, ctypes
import ctypes.wintypes as wt

HERE = os.path.dirname(os.path.abspath(__file__))
SHOT_DEFAULT = os.path.join(HERE, '_shot.png')

sys.path.insert(0, HERE)
import client_layout as CL  # noqa: E402  (per-build addresses, client-2009-tooling)

WINDOW_TITLE = 'WindSlayer'

# Which client to drive: `--client 2` (or env WS_CLIENT=2) targets WindSlayer_p2.exe,
# the copy patched to UDP P2P port 42908 so two clients can run at once.
CLIENT_EXES = CL.EXE_NAMES
if '--client' in sys.argv:
    _i = sys.argv.index('--client')
    os.environ['WS_CLIENT'] = sys.argv[_i + 1]
    del sys.argv[_i:_i + 2]
CLIENT = os.environ.get('WS_CLIENT', '1')
PROC_NAME = CLIENT_EXES.get(CLIENT, CLIENT_EXES['1'])


def pop_build_arg(argv):
    """Remove `--build <2008|2009>` from argv; returns the value or None. Raises ValueError
    when the value is missing."""
    if '--build' not in argv:
        return None
    i = argv.index('--build')
    if i + 1 >= len(argv) or argv[i + 1].startswith('--'):
        raise ValueError('--build needs a value: 2008 or 2009')
    value = argv[i + 1]
    del argv[i:i + 2]
    return value


# Which build to drive: `--build 2009` (or env WS_BUILD=2009); default: config.json
# CLIENT_BUILD (the build the server speaks), which defaults to 2008. A child wsview run by
# wsdev inherits WS_BUILD.
try:
    _b = pop_build_arg(sys.argv)
    if _b is not None:
        os.environ[CL.ENV_BUILD] = _b
    BUILD = CL.resolve_build(os.environ.get(CL.ENV_BUILD))
except ValueError as _e:
    raise SystemExit(str(_e)) from None
LAYOUT = CL.layout(BUILD)

# --- live-client constants of the selected build (client_layout.py; 2008: mirror wsre.py) ---
GAME_STATE = LAYOUT.game_state
SCENE_PTR = LAYOUT.scene_ptr
ANIM_PTR = LAYOUT.anim_ptr

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
# VkKeyScanW returns a SHORT; with the default int restype -1 would read as 0xFFFF.
user32.VkKeyScanW.restype = ctypes.c_short
user32.VkKeyScanW.argtypes = [ctypes.c_wchar]

# ============================================================ process / memory
def _pids(name=PROC_NAME):
    """Pids of the selected client of the selected build. Both builds' exes share the name,
    so a process whose PE timestamp belongs to the other build is skipped; one that cannot
    be read (elevated) is kept for the window/input commands (client_layout.select_pids)."""
    return CL.find_client_pids(name, LAYOUT)


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
    """Top-level visible window of the selected client (or of a specific pid)."""
    result = []
    wanted = {pid} if pid else set(_pids())
    if not wanted:
        return None                        # client not running (don't match e.g. Explorer windows)
    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        if not user32.IsWindowVisible(h):
            return True
        wpid = wt.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(wpid))
        if wpid.value not in wanted:
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
KEYEVENTF_UNICODE = 0x0004

VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12

# VK name -> VK code (we convert to scancode via MapVirtualKey)
_VK = {
    'left': 0x25, 'up': 0x26, 'right': 0x27, 'down': 0x28,
    'space': 0x20, 'enter': 0x0D, 'esc': 0x1B, 'escape': 0x1B, 'tab': 0x09,
    'backspace': 0x08, 'delete': 0x2E, 'home': 0x24, 'end': 0x23,
    'ctrl': VK_CONTROL, 'shift': VK_SHIFT, 'alt': VK_MENU, 'lalt': VK_MENU,
    # US-layout OEM keys. ord('/') is 0x2F = VK_HELP, which has no scancode, so the
    # old single-character fallback could not type slash commands (chat_mail_gm B14).
    'slash': 0xBF, 'minus': 0xBD, 'period': 0xBE, 'comma': 0xBC, 'semicolon': 0xBA,
    'quote': 0xDE, 'equals': 0xBB, 'lbracket': 0xDB, 'rbracket': 0xDD,
    'backslash': 0xDC, 'backtick': 0xC0,
    'q': 0x51,
}
_MODS = {'shift': VK_SHIFT, 'ctrl': VK_CONTROL, 'control': VK_CONTROL, 'alt': VK_MENU}
# arrows / nav keys need the extended flag
_EXTENDED = {0x25, 0x26, 0x27, 0x28, 0x2E, 0x24, 0x23}


def _vk_scan(ch):
    """(vk, [modifier vks]) that types `ch` on the active keyboard layout, or None.
    VkKeyScanW's high byte: 1 shift, 2 ctrl, 4 alt (AltGr = ctrl+alt)."""
    r = user32.VkKeyScanW(ch)
    if r == -1 or (r & 0xFF) == 0xFF:
        return None
    mods = []
    if r & 0x100:
        mods.append(VK_SHIFT)
    if r & 0x200:
        mods.append(VK_CONTROL)
    if r & 0x400:
        mods.append(VK_MENU)
    return r & 0xFF, mods


def _vk_of(token):
    t = token.lower()
    if t in _VK:
        return _VK[t]
    if len(t) == 1:
        if t.isalnum() and t.isascii():
            return ord(t.upper())
        hit = _vk_scan(t)                      # '/', '-', '.' ... (shift, if any, is ignored)
        if hit:
            return hit[0]
    if t.startswith('f') and t[1:].isdigit():
        return 0x70 + int(t[1:]) - 1
    raise ValueError(f'unknown key token: {token}')


def parse_chord(token):
    """'shift+a' -> ([VK_SHIFT], 0x41); 'slash' -> ([], 0xBF); '?' -> ([VK_SHIFT], 0xBF)."""
    parts = token.split('+') if len(token) > 1 else [token]
    mods = []
    for p in parts[:-1]:
        if p.lower() not in _MODS:
            raise ValueError(f'unknown modifier {p!r} in {token!r} (shift, ctrl, alt)')
        mods.append(_MODS[p.lower()])
    key = parts[-1]
    if len(key) == 1 and not key.isalnum():
        hit = _vk_scan(key)
        if hit:
            return mods + [m for m in hit[1] if m not in mods], hit[0]
    return mods, _vk_of(key)


def plan_text(text):
    """Key plan for typing `text`: [('key', vk, [mods], ch) | ('unicode', None, [], ch)].
    Characters the layout cannot produce (e.g. Hangul without an IME) fall back to a
    KEYEVENTF_UNICODE event, which only WM_CHAR readers see."""
    plan = []
    for ch in text:
        if ch == '\n':
            plan.append(('key', _VK['enter'], [], ch))
            continue
        if ch == '\t':
            plan.append(('key', _VK['tab'], [], ch))
            continue
        hit = _vk_scan(ch)
        if hit is None:
            plan.append(('unicode', None, [], ch))
        else:
            plan.append(('key', hit[0], hit[1], ch))
    return plan


def _send_scan(vk, up):
    scan = user32.MapVirtualKeyW(vk, 0)
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    if vk in _EXTENDED:
        flags |= KEYEVENTF_EXTENDED
    inp = _INPUT(type=1)
    inp.u.ki = _KBD(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=None)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def _send_unicode(ch, up):
    inp = _INPUT(type=1)
    inp.u.ki = _KBD(wVk=0, wScan=ord(ch), dwFlags=KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0),
                    time=0, dwExtraInfo=None)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


MODIFIER_VKS = (0x10, 0xA0, 0xA1, 0x11, 0xA2, 0xA3, 0x12, 0xA4, 0xA5, 0x5B, 0x5C)


def clear_modifiers():
    """Release every modifier before injecting input.

    An interrupted chord (shift+X) or a stray toggle leaves SHIFT/CTRL/ALT latched for
    the whole desktop. The game then ignores plain keys - movement, hotkeys, chat - while
    mouse clicks still work, which looks exactly like "input is broken" (observed
    2026-09-17 after a long `type` run left SHIFT toggled; cost ~20 min to diagnose)."""
    for vk in MODIFIER_VKS:
        _send_scan(vk, True)


def key_tap(vk, dur=0.05):
    _send_scan(vk, False); time.sleep(dur); _send_scan(vk, True); time.sleep(0.03)


def chord_tap(mods, vk, dur=0.05):
    """Hold modifiers, tap vk, release modifiers in reverse order."""
    for m in mods:
        _send_scan(m, False); time.sleep(0.02)
    key_tap(vk, dur)
    for m in reversed(mods):
        _send_scan(m, True); time.sleep(0.02)


def type_text(text, delay=0.03):
    """Type text as individual key presses (one char at a time, so the chat IME sees
    each WM_KEYDOWN/WM_CHAR). Returns the characters sent as unicode fallbacks."""
    fallbacks = []
    for kind, vk, mods, ch in plan_text(text):
        if kind == 'key':
            chord_tap(mods, vk)
        else:
            fallbacks.append(ch)
            _send_unicode(ch, False); time.sleep(0.05); _send_unicode(ch, True); time.sleep(0.03)
        time.sleep(delay)
    return fallbacks


MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010


def _mouse_event(flags):
    inp = _INPUT(type=0)
    inp.u.mi = _MOUSE(dx=0, dy=0, mouseData=0, dwFlags=flags, time=0, dwExtraInfo=None)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def mouse_click_screen(x, y):
    user32.SetCursorPos(int(x), int(y)); time.sleep(0.04)
    for f in (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP):
        _mouse_event(f); time.sleep(0.03)


def mouse_rclick_screen(x, y):
    # popup 0x50 opens on WM_RBUTTONUP (0x205), so the release matters
    user32.SetCursorPos(int(x), int(y)); time.sleep(0.04)
    for f in (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP):
        _mouse_event(f); time.sleep(0.03)


def mouse_dclick_screen(x, y, gap=0.03):
    """Two left clicks at the same point well inside GetDoubleClickTime, so Windows
    turns the second press into WM_LBUTTONDBLCLK (two separate `click` processes are
    too slow for that)."""
    user32.SetCursorPos(int(x), int(y)); time.sleep(0.04)
    for f in (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP):
        _mouse_event(f); time.sleep(gap)


def drag_path(x1, y1, x2, y2, steps=12):
    """Intermediate cursor points from (x1,y1) to (x2,y2), ending exactly on (x2,y2)."""
    steps = max(1, int(steps))
    return [(round(x1 + (x2 - x1) * i / steps), round(y1 + (y2 - y1) * i / steps))
            for i in range(1, steps + 1)]


def mouse_drag_screen(x1, y1, x2, y2, steps=12, hold=0.15):
    """Press at (x1,y1), move in steps (WM_MOUSEMOVE with the button down), release at
    (x2,y2): trade 0x22/0x26 and drop-to-ground 0x13/0x14 act on WM_LBUTTONUP."""
    user32.SetCursorPos(int(x1), int(y1)); time.sleep(0.05)
    _mouse_event(MOUSEEVENTF_LEFTDOWN); time.sleep(hold)
    for px, py in drag_path(x1, y1, x2, y2, steps):
        user32.SetCursorPos(int(px), int(py)); time.sleep(0.02)
    time.sleep(hold)
    _mouse_event(MOUSEEVENTF_LEFTUP); time.sleep(0.05)


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
    clear_modifiers()
    if not args:
        print('usage: key <token> [token...]   (chords: shift+a)'); return
    try:
        plan = [(tok, *parse_chord(tok)) for tok in args]      # validate every token first
    except ValueError as e:
        print(e); return
    hwnd = _find_hwnd()
    if hwnd:
        _foreground(hwnd)
    for tok, mods, vk in plan:
        chord_tap(mods, vk)
        print(f'tapped {tok}')


def parse_type_args(args):
    """type [--enter] [--delay ms] <text...> -> (text, press_enter, delay_seconds).
    Words are re-joined with single spaces, so quote the text to keep repeated spaces."""
    enter, delay, words = False, 0.03, []
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--enter':
            enter = True; i += 1
        elif a == '--delay' and i + 1 < len(args):
            delay = int(args[i + 1]) / 1000.0; i += 2
        elif a == '--':
            words.extend(args[i + 1:]); break
        else:
            words.append(a); i += 1
    return ' '.join(words), enter, delay


def cmd_type(args):
    """type [--enter] [--delay ms] <text...>: per-character key presses for chat/IME input."""
    clear_modifiers()
    try:
        text, enter, delay = parse_type_args(args)
    except ValueError:
        print('--delay expects milliseconds, e.g. type --delay 80 hi'); return
    if not text and not enter:
        print('usage: type [--enter] [--delay ms] <text...>   e.g. type --enter /w Alice hi'); return
    hwnd = _find_hwnd()
    if not hwnd:
        print('game window not found'); return
    _foreground(hwnd)
    fallbacks = type_text(text, delay)
    if enter:
        key_tap(_VK['enter'])
    # ascii(): a cp1252 console cannot print Hangul, and a print error here would hide
    # that the keys were already sent.
    print(f'typed {ascii(text)}' + (' + enter' if enter else '')
          + (f'  (unicode fallback, invisible to scancode readers: {ascii("".join(fallbacks))})'
             if fallbacks else ''))


def _client_point(args, n=2):
    """(hwnd, [screen x, y, ...]) for n client-coordinate args, or (None, None) after printing why."""
    if len(args) < n:
        return None, None
    try:
        vals = [int(a) for a in args[:n]]
    except ValueError:
        print(f'coordinates must be integers: {args[:n]}'); return None, None
    hwnd = _find_hwnd()
    if not hwnd:
        print('game window not found'); return None, None
    _foreground(hwnd)
    x, y, w, h = _client_rect_screen(hwnd)
    return hwnd, [v + (x if i % 2 == 0 else y) for i, v in enumerate(vals)]


def cmd_dclick(args):
    clear_modifiers()
    hwnd, pt = _client_point(args)
    if hwnd is None:
        if len(args) < 2:
            print('usage: dclick <x> <y>  (client coords)')
        return
    GCL_STYLE, CS_DBLCLKS = -26, 0x0008
    has_dbl = bool(user32.GetClassLongW(hwnd, GCL_STYLE) & CS_DBLCLKS)
    mouse_dclick_screen(*pt)
    print(f'double-clicked client ({args[0]},{args[1]}) -> screen ({pt[0]},{pt[1]})  '
          f'[dblclick time {user32.GetDoubleClickTime()}ms, window class CS_DBLCLKS={has_dbl}]')


def cmd_rclick(args):
    clear_modifiers()
    hwnd, pt = _client_point(args)
    if hwnd is None:
        if len(args) < 2:
            print('usage: rclick <x> <y>  (client coords)')
        return
    mouse_rclick_screen(*pt)
    print(f'right-clicked client ({args[0]},{args[1]}) -> screen ({pt[0]},{pt[1]})')


def cmd_drag(args):
    clear_modifiers()
    hwnd, pt = _client_point(args, 4)
    if hwnd is None:
        if len(args) < 4:
            print('usage: drag <x1> <y1> <x2> <y2>  (client coords)')
        return
    mouse_drag_screen(*pt)
    print(f'dragged client ({args[0]},{args[1]}) -> ({args[2]},{args[3]})')


def cmd_hold(args):
    clear_modifiers()
    if len(args) < 2:
        print('usage: hold <key> <ms>'); return
    vk = _vk_of(args[0]); ms = int(args[1])
    hwnd = _find_hwnd()
    if hwnd:
        _foreground(hwnd)
    _send_scan(vk, False); time.sleep(ms / 1000.0); _send_scan(vk, True)
    print(f'held {args[0]} for {ms}ms')


def cmd_click(args):
    clear_modifiers()
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
    lock = 'lock' in [a.lower() for a in args]   # cursor confinement is opt-in
    sw = user32.GetSystemMetrics(0); sh = user32.GetSystemMetrics(1)
    if mode == 'fit':
        tw = min(sw, int(sh * 4 / 3)); th = min(sh, int(sw * 3 / 4))
    else:
        tw, th = sw, sh
    tx, ty = (sw - tw) // 2, (sh - th) // 2
    lockmsg = ('cursor FULLY LOCKED to game while focused (alt-tab to free for 2nd monitor)'
               if lock else 'cursor free')
    print(f'auto-borderless ({mode}) -> {tw}x{th}@({tx},{ty}); {lockmsg}; watching... (Ctrl-C to stop)')
    misses = 0
    tick = 0
    hwnd = None
    clipped = False
    pt = wt.POINT()
    try:
        while True:
            # --- window: keep it borderless (cheap check every ~0.3s) ---
            if tick % 20 == 0:
                hwnd = _find_hwnd()
                if not hwnd:
                    misses += 1
                    if clipped:
                        user32.ClipCursor(None); clipped = False
                    if misses > 30:          # game gone for ~7.5s
                        print('game closed; exiting watcher'); return
                    time.sleep(0.25); tick += 1; continue
                misses = 0
                r = wt.RECT(); user32.GetWindowRect(hwnd, ctypes.byref(r))
                cr = wt.RECT(); user32.GetClientRect(hwnd, ctypes.byref(cr))
                if cr.right >= 640 and not (r.left == tx and r.top == ty and
                                            (r.right - r.left) == tw and (r.bottom - r.top) == th):
                    st = user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
                    new = (st & ~(WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX |
                                  WS_SYSMENU | WS_BORDER | WS_DLGFRAME)) | WS_POPUP | WS_VISIBLE
                    user32.SetWindowLongW(hwnd, GWL_STYLE, new & 0xFFFFFFFF)
                    user32.SetWindowPos(hwnd, 0, tx, ty, tw, th,
                                        SWP_FRAMECHANGED | SWP_SHOWWINDOW | SWP_NOZORDER)
                    print(f'applied borderless to hwnd=0x{hwnd:X}')
            # --- cursor: FULL containment on all 4 edges while the game is the
            # foreground window, so it can never escape and get lost. ClipCursor is
            # the primary confinement; SetCursorPos is a backup if the game releases
            # the clip between ticks. Alt-tab away (game loses foreground) frees it. ---
            if lock and hwnd:
                if user32.GetForegroundWindow() == hwnd:
                    box = wt.RECT(tx, ty, tx + tw, ty + th)
                    user32.ClipCursor(ctypes.byref(box)); clipped = True
                    user32.GetCursorPos(ctypes.byref(pt))
                    nx = min(max(pt.x, tx), tx + tw - 1)
                    ny = min(max(pt.y, ty), ty + th - 1)
                    if nx != pt.x or ny != pt.y:
                        user32.SetCursorPos(nx, ny)
                elif clipped:
                    user32.ClipCursor(None); clipped = False
            time.sleep(0.015)
            tick += 1
    finally:
        user32.ClipCursor(None)


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
    L = LAYOUT
    print(f'PID {pid}  build {L.build}  scene=0x{(scene or 0):08X}  anim=0x{(anim or 0):08X}')
    if not scene:
        print('  (not in world)'); return
    # scene+0x220 (2009: +0x224) = the S2C 0x02 account_id: this client's own uid
    # (lc-uid-online: test 1, admin 2)
    print(f'  local uid (scene+0x{L.scene_local_uid:X}) = {u32(scene + L.scene_local_uid)}  '
          f'scene mode (+0x{L.scene_mode:X}) = {u32(scene + L.scene_mode)}')
    node = u32(scene + L.scene_entity_list); n = 0
    # lv = entity+0x99 (2009 +0x9D): what S2C 0x22 sets on a remote player (lc-level-broadcast,
    # P5 stage 4) and what the popup's Char. Info shows.
    print(f'  {"addr":>10} {"name":<16} {"alive":>5} {"uid":>10} {"lv":>3} {"anim":>5}  pos')
    while node and 0x400000 <= node < 0x7FFF0000 and n < 80:
        ent = u32(node + 8)
        if ent and 0x400000 <= ent < 0x7FFF0000:
            nm = cstr(ent)
            if nm and any(ch.isalnum() for ch in nm):
                print(f'  0x{ent:08X} {nm:<16} {u8(ent + L.ent_type)!s:>5} '
                      f'0x{(u32(ent + L.ent_uid) or 0):08X} {u8(ent + L.ent_level)!s:>3} '
                      f'{u32(ent + L.ent_anim)!s:>5}  '
                      f'({f64(ent + L.ent_pos_x):.0f},{f64(ent + L.ent_pos_y):.0f})')
        node = u32(node); n += 1
    print(f'  ({n} list nodes walked)')


def main():
    if len(sys.argv) < 2:
        print(__doc__); return
    cmd, rest = sys.argv[1], sys.argv[2:]
    table = {'shot': cmd_shot, 'watch': cmd_watch, 'win': cmd_win, 'key': cmd_key,
             'hold': cmd_hold, 'click': cmd_click, 'state': cmd_state,
             'type': cmd_type, 'dclick': cmd_dclick, 'rclick': cmd_rclick, 'drag': cmd_drag,
             'borderless': cmd_borderless, 'autoborderless': cmd_autoborderless}
    fn = table.get(cmd)
    if not fn:
        print(__doc__); return
    fn(rest)


if __name__ == '__main__':
    main()
