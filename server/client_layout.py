#!/usr/bin/env python3
"""
client_layout.py - per-build client memory layout and launch facts (client-2009-tooling)
=======================================================================================
The live dev tooling (wsview.py, wsdev.py) and the server's local-memory combat driver
(GameServer._combat_driver) read the running client's memory. The EN 2008 exe
(WindSlayer2Game) and the EN 2009 exe (Outspark v1.04 Build 14, WindSlayer2009) share the
process name WindSlayer_patched.exe but not their layout, so every address and offset
lives here, per build:

    import client_layout as CL
    L = CL.layout('2009')            # CL.LAYOUT_2008 / CL.LAYOUT_2009
    L.scene_ptr, L.scene_local_uid   # 0x54F0C0, 0x224
    CL.find_client_pids('WindSlayer_patched.exe', L)   # only processes of that build

Sources: the 2008 column is the live-proven set the tools used before (wsview/wsdev,
windslayer_server ENTITY_*); the 2009 column is client_map_2009.json. SOURCES names the
client_map_2009.json entry of every field; test_tooling2009.py checks both columns
against that file, so a correction there shows up as a failing test here.

A running client's build is told apart by its PE header: TimeDateStamp of the image at
0x400000 (neither build uses ASLR, client_map_2009.json meta.image_base) is 0x48240544 for
every 2008 exe (WindSlayer.exe, _patched, _p2) and 0x49797E29 for every 2009 exe
(WindSlayer.exe, _patched; patch_2009.py keeps the header).

Launch: the 2008 exe starts without arguments (its login screen takes ID/password). The
2009 exe needs three launcher (SSO) arguments - FUN_00440e00 returns 0 (the exe opens the
Outspark web page and exits) unless all three are non-empty - and sends the first one in
its C2S 0x01 (spec_2009 0x451CE5/0x01 sso_account). The server reads "<account> <password>"
from it (client-2009-login), so the command line is
    "<dir>\\WindSlayer_patched.exe" -<account> <password> -x -x
(launch_command; play_2009.bat does the same).
"""
import os
import re
import struct
from dataclasses import dataclass, fields

BUILD_2008, BUILD_2009 = '2008', '2009'
BUILDS = (BUILD_2008, BUILD_2009)
DEFAULT_BUILD = BUILD_2008
ENV_BUILD = 'WS_BUILD'

# `--client 1|2`: the same file names in both builds' install dirs. WindSlayer_p2.exe is the
# copy with the P2P UDP port 42907 -> 42908: 2008 project_multiclient, 2009 `patch_2009.py
# --p2` (its port immediate is at 0x4405C9, MOV [EAX+0x248],0xa79b, client_map scene +0x248).
# The server tells the two apart by that port in C2S 0x2B (world.client_number, `--to c:2`).
EXE_NAMES = {'1': 'WindSlayer_patched.exe', '2': 'WindSlayer_p2.exe'}

IMAGE_BASE = 0x400000


@dataclass(frozen=True)
class Layout:
    build: str
    pe_timestamp: int          # IMAGE_FILE_HEADER.TimeDateStamp of the exe
    # --- globals (VA) ---
    game_state: int            # static game object
    scene_ptr: int             # -> scene/session object
    anim_ptr: int              # -> anim/template container
    receive_busy_flag: int     # game_state+0x408/+0x418: 0 while OnReceive may run (NOT in-world)
    # --- scene offsets ---
    scene_entity_list: int     # singly linked: node.next = [node], entity = [node+8]
    scene_local_uid: int       # this client's uid (S2C 0x02 account id)
    scene_mode: int            # 5 character select, 6 field, 4 other in-game
    scene_local_player: int    # local player entity pointer (RegisterLocalPlayer)
    # --- entity offsets (name is at +0 in both) ---
    ent_uid: int
    ent_type: int              # u8: 4 spawned by 0x1A (local/monster), 3 remote player
    ent_level: int             # u8
    ent_hp: int                # u32
    ent_mp: int                # u32
    ent_max_hp: int            # u32
    ent_max_mp: int            # u32
    ent_anim: int              # u32 1-based anim container index
    ent_pos_x: int             # f64 logic position
    ent_pos_y: int             # f64
    ent_action_state: int      # 8 idle, 0x16 dead, 3/0x17 attack
    ent_swing: int             # u8 1 while an action key is active
    ent_render_state: int      # u32 4 attack swing, 8 idle, 0x16 dead
    ent_facing: int            # u8 2 left, 6 right (kept after release)


# The 2008 column: the addresses the tools and the driver read before this module existed
# (wsview GAME_STATE/SCENE_PTR/ANIM_PTR, wsdev GS_FLAG/_player/status, windslayer_server
# CLIENT_SCENE_PTR/SCENE_LOCAL_UID/ENTITY_*, driver literals +0x8b8/+0x15b4).
LAYOUT_2008 = Layout(
    build=BUILD_2008, pe_timestamp=0x48240544,
    game_state=0x70EA00, scene_ptr=0x70EECC, anim_ptr=0x70EEE0, receive_busy_flag=0x70EE08,
    scene_entity_list=0x0C, scene_local_uid=0x220, scene_mode=0xF00, scene_local_player=0x970,
    ent_uid=0x84, ent_type=0x98, ent_level=0x99, ent_hp=0x9C, ent_mp=0xA0,
    ent_max_hp=0x110C, ent_max_mp=0x1110, ent_anim=0xE60,
    ent_pos_x=0x11F8, ent_pos_y=0x1288, ent_action_state=0x904,
    ent_swing=0x8B8, ent_render_state=0x15B4, ent_facing=0x8BD,
)

# The 2009 column (client_map_2009.json; the VA of the instruction that proves each value is
# in that file's evidence text).
LAYOUT_2009 = Layout(
    build=BUILD_2009, pe_timestamp=0x49797E29,
    game_state=0x54EBD0,       # WinMain 0x487D74 MOV EDI,0x54ebd0
    scene_ptr=0x54F0C0,        # game_state+0x4F0; 0x487D96 MOV ECX,[0x54f0c0]
    anim_ptr=0x54F0D4,         # game_state+0x504
    receive_busy_flag=0x54EFE8,  # game_state+0x418
    scene_entity_list=0x0C,    # per-tick 0x42C920 walks *(scene+0xc)
    scene_local_uid=0x224,     # S2C 0x02 0x452217 ADD ECX,0x224; RLP gate 0x423043
    scene_mode=0xF18,          # setter 0x4405B0; 0x02 handler sets 5 at 0x4521E4
    scene_local_player=0x988,  # RLP 0x42304F MOV [EAX+0x988],EDI
    ent_uid=0x88,              # 0x1A 0x456B45; RLP gate 0x42303B
    ent_type=0x9C,             # 0x1A 0x456B56 MOV byte [EDI+0x9c],0x4
    ent_level=0x9D,
    ent_hp=0xA0,               # S2C 0x28 0x459768
    ent_mp=0xA4,               # S2C 0x44 0x4597AF
    ent_max_hp=0x11AC,         # 0x1A 0x456BC8
    ent_max_mp=0x11B0,
    ent_anim=0xEFC,            # 0x1A 0x456B2A
    ent_pos_x=0x1298,          # 0x1A 0x456D65 (f64)
    ent_pos_y=0x1328,          # 0x1A 0x456D7F (f64)
    ent_action_state=0x994,    # 0x1A 0x456EE7
    ent_swing=0x944,           # input handler 0x42E1D7 / 0x42E72F, RLP 0x423275
    ent_render_state=0x166C,   # RLP 0x4233A0; render 0x433BD6 CMP [EDI+0x166c],0x16
    ent_facing=0x949,          # 2008 +0x8BD, region shift +0x8C (0x8B3..0x8DF); 0x1A read order
)

LAYOUTS = {BUILD_2008: LAYOUT_2008, BUILD_2009: LAYOUT_2009}

# field -> (client_map_2009.json section, entry key, index of the hex value in the column
# text). Index 1..8 of 'other entity offsets (for tools)' walk its comma list in order: both
# columns list the same fields in the same order ('+0 name, +0x99 level, +0xA0 MP, ...').
_OTHER = 'other entity offsets (for tools)'
SOURCES = {
    'game_state': ('globals', 'game_state', 0),
    'scene_ptr': ('globals', 'scene_ptr', 0),
    'anim_ptr': ('globals', 'anim_container_ptr', 0),
    'receive_busy_flag': ('globals', 'receive_busy_flag', 0),
    'scene_entity_list': ('scene_fields', '+0x0C entity_list_head', 0),
    'scene_local_uid': ('scene_fields', '+0x220 local_uid', 0),
    'scene_mode': ('scene_fields', '+0xF00 scene_mode', 0),
    'scene_local_player': ('scene_fields', '+0x970 local_player', 0),
    'ent_uid': ('entity_fields', '+0x84 uid', 0),
    'ent_type': ('entity_fields', '+0x98 type', 0),
    'ent_level': ('entity_fields', _OTHER, 1),
    'ent_hp': ('entity_fields', '+0x9C hp', 0),
    'ent_mp': ('entity_fields', _OTHER, 2),
    'ent_max_hp': ('entity_fields', '+0x110C max_hp', 0),
    'ent_max_mp': ('entity_fields', _OTHER, 3),
    'ent_anim': ('entity_fields', '+0xE60 anim_index', 0),
    'ent_pos_x': ('entity_fields', '+0x11F8/+0x1288 pos_f64', 0),
    'ent_pos_y': ('entity_fields', '+0x11F8/+0x1288 pos_f64', 1),
    'ent_action_state': ('entity_fields', '+0x904 action_state', 0),
    'ent_swing': ('entity_fields', '+0x8B8 swing_flag', 0),
    'ent_render_state': ('entity_fields', '+0x15B4 render_state', 0),
    'ent_facing': ('entity_fields', _OTHER, 8),
}

_HEX_TOKEN = re.compile(r'(?<![0-9A-Za-z])\+?(0x[0-9A-Fa-f]+|0(?![0-9A-Za-z]))')


def column_values(text):
    """Every offset/address in one client_map column text, in order:
    '+0x1298 (x f64) / +0x1328 (y f64)' -> [0x1298, 0x1328]; '+0 name, ...' -> [0, ...]."""
    return [int(t, 16) if t.lower().startswith('0x') else 0 for t in _HEX_TOKEN.findall(str(text))]


def layout_fields():
    return [f.name for f in fields(Layout) if f.name not in ('build', 'pe_timestamp')]


def layout(build=None):
    """The Layout of `build` ('2008' default)."""
    build = str(build or DEFAULT_BUILD)
    if build not in LAYOUTS:
        raise ValueError(f'client build must be one of {BUILDS}, not {build!r}')
    return LAYOUTS[build]


def build_of_timestamp(stamp):
    """'2008' / '2009' for a PE TimeDateStamp, None for anything else."""
    for b, lay in LAYOUTS.items():
        if lay.pe_timestamp == stamp:
            return b
    return None


def resolve_build(explicit=None, config_build=None):
    """The build a dev tool drives: --build / $WS_BUILD (`explicit`), else the build the server
    speaks (config.json CLIENT_BUILD, read here when `config_build` is None), else '2008'.
    Raises ValueError for anything but '2008' / '2009'."""
    value = explicit if explicit not in (None, '') else config_build
    if value in (None, ''):
        value = _config_build()
    value = str(value or DEFAULT_BUILD).strip()
    if value not in BUILDS:
        raise ValueError(f'--build / {ENV_BUILD} must be one of {", ".join(BUILDS)}, not {value!r}')
    return value


def _config_build():
    try:
        import config as cfgmod
        return cfgmod.load().CLIENT_BUILD
    except Exception:                                   # noqa: BLE001 - a broken config.json: default
        return DEFAULT_BUILD


# ------------------------------------------------------------------ launch ---
SSO_FILLER = 'x'          # launcher arguments 2 and 3 only have to be non-empty (FUN_00440e00)


def launch_command(build, exe, user='test', password='test'):
    """What subprocess.Popen starts. 2008: [exe] (unchanged). 2009: ONE command-line string,
    because the SSO parser FUN_00440e00 reads GetCommandLineA raw: it starts two characters
    after the first '"' (so the exe path must be quoted) and splits at every ' -'; a
    list2cmdline-quoted "-user pass" would put the quote into argument 1."""
    if build != BUILD_2009:
        return [exe]
    user, password = str(user), str(password)
    for what, text in (('account', user), ('password', password)):
        if not text or any(c.isspace() for c in text) or '"' in text or text.startswith('-'):
            raise ValueError(f'2009 launch: the {what} must be non-empty, without spaces, quotes '
                             f'or a leading "-" (the SSO field is split at " -"), got {text!r}')
    return f'"{exe}" -{user} {password} -{SSO_FILLER} -{SSO_FILLER}'


def parse_sso_command_line(cmdline):
    """Port of the 2009 SSO parser FUN_00440e00 (0x440E00): the three launcher arguments it
    copies to scene+0x48 / +0xCE / +0x101 (before FUN_0048b3d0's marker strip, which the
    live test showed keeps the trailing space). Argument 1 is what C2S 0x01 carries."""
    q = cmdline.find('"')
    if q < 0:
        return None
    text = cmdline[q + 2:]
    out, field = ['', '', '', ''], 0
    for i in range(len(text) - 1):
        if text[i] == ' ' and text[i + 1] == '-':
            field += 1
            continue
        if 1 <= field <= 3:
            out[field] += text[i + 1]
    return out[1], out[2], out[3]


# ---------------------------------------------------------------- processes ---
def process_ids(exe_name):
    """Pids of every process whose image file name is `exe_name` (case-insensitive)."""
    import ctypes
    from ctypes import wintypes as wt

    class PE32(ctypes.Structure):
        _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ProcessID', wt.DWORD),
                    ('th32DefaultHeapID', ctypes.c_void_p), ('th32ModuleID', wt.DWORD),
                    ('cntThreads', wt.DWORD), ('th32ParentProcessID', wt.DWORD),
                    ('pcPriClassBase', wt.LONG), ('dwFlags', wt.DWORD), ('szExeFile', wt.WCHAR * 260)]
    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.restype = wt.HANDLE
    snap = k.CreateToolhelp32Snapshot(2, 0)
    out = []
    try:
        e = PE32(); e.dwSize = ctypes.sizeof(e)
        ok = k.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.lower() == exe_name.lower():
                out.append(e.th32ProcessID)
            ok = k.Process32NextW(snap, ctypes.byref(e))
    finally:
        k.CloseHandle(snap)
    return out


def image_timestamp(pid):
    """PE TimeDateStamp of the process image at 0x400000, or None when the process cannot be
    read (gone, or elevated while the tool is not: OpenProcess error 5)."""
    import ctypes
    from ctypes import wintypes as wt
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = wt.HANDLE
    k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    proc = k.OpenProcess(0x10 | 0x1000, False, pid)      # VM_READ | QUERY_LIMITED_INFORMATION
    if not proc:
        return None
    try:
        def rd(a, n):
            b = (ctypes.c_ubyte * n)(); r = ctypes.c_size_t(0)
            if not k.ReadProcessMemory(proc, ctypes.c_void_p(a), b, n, ctypes.byref(r)) or r.value != n:
                return None
            return bytes(b)
        mz = rd(IMAGE_BASE, 0x40)
        if not mz or mz[:2] != b'MZ':
            return None
        pe = struct.unpack_from('<I', mz, 0x3C)[0]
        hdr = rd(IMAGE_BASE + pe, 12)
        if not hdr or hdr[:4] != b'PE\0\0':
            return None
        return struct.unpack_from('<I', hdr, 8)[0]
    finally:
        k.CloseHandle(proc)


def select_pids(pids, stamp_of, want_stamp, strict=False):
    """Keep the pids whose image stamp is `want_stamp`. An unreadable stamp (None) is kept
    unless `strict` (window-only tools can still drive an elevated client; the memory driver
    needs the read anyway). A readable stamp of the other build is always dropped, so a 2008
    tool never grabs a running 2009 client of the same name and vice versa."""
    out = []
    for pid in pids:
        stamp = stamp_of(pid)
        if stamp == want_stamp or (stamp is None and not strict):
            out.append(pid)
    return out


def find_client_pids(exe_name, lay, strict=False):
    return select_pids(process_ids(exe_name), image_timestamp, lay.pe_timestamp, strict)


def install_exe(client_dir, client='1'):
    return os.path.join(client_dir, EXE_NAMES.get(str(client), EXE_NAMES['1']))
