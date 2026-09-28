"""
snapshot_v2.py — targeted inspection of the EN client's activation gates.

Checks the SPECIFIC offsets the workflow identified:
- [+0x5A0] : input gate (1 = input accepted, 0 = input blocked)
- [+0x5A4], [+0x5A8], [+0x59C] : adjacent fields set by 0x45AF00
- [+0x970] : local-player pointer (NULL = renderer skips)
- [+0x98]  : entity alive state (3 = spawned, 4 = local-player-eligible)
- [+0x14F4]: misc "alive-related" bool

Also tries multiple bases for game_state because snapshot.py's 0x70E710 guess
may be wrong.
"""
import ctypes, ctypes.wintypes as wt, struct, sys

kernel32 = ctypes.windll.kernel32


class PE32(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ProcessID', wt.DWORD),
                ('th32DefaultHeapID', ctypes.c_void_p), ('th32ModuleID', wt.DWORD),
                ('cntThreads', wt.DWORD), ('th32ParentProcessID', wt.DWORD),
                ('pcPriClassBase', wt.LONG), ('dwFlags', wt.DWORD),
                ('szExeFile', wt.WCHAR * 260)]


def find_pid(name):
    snap = kernel32.CreateToolhelp32Snapshot(2, 0)
    e = PE32(); e.dwSize = ctypes.sizeof(e)
    if not kernel32.Process32FirstW(snap, ctypes.byref(e)):
        return None
    while True:
        if e.szExeFile.lower() == name.lower():
            return e.th32ProcessID
        if not kernel32.Process32NextW(snap, ctypes.byref(e)):
            return None


def rd(proc, a, sz):
    if a == 0 or sz == 0:
        return None
    buf = (ctypes.c_ubyte * sz)()
    r = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(proc, ctypes.c_void_p(a), ctypes.byref(buf), sz, ctypes.byref(r))
    return bytes(buf) if ok and r.value == sz else None


def u32(proc, a):
    d = rd(proc, a, 4)
    return struct.unpack('<I', d)[0] if d else 0


def u8(proc, a):
    d = rd(proc, a, 1)
    return d[0] if d else 0


def looks_pointer(v):
    return 0x00400000 <= v < 0x80000000


pid = find_pid('WindSlayer_patched.exe')
if not pid:
    print('Game not running'); sys.exit(1)
proc = kernel32.OpenProcess(0x10 | 0x0400, False, pid)
print(f'PID {pid}', flush=True)

# Try a sweep of candidate game_state globals (snapshot.py used 0x70E710)
# but the workflow mentioned 0x70EECC and others. Scan a window.
print()
print('=== Candidate game_state pointers (.data globals) ===', flush=True)
candidates = [0x70E710, 0x70EECC, 0x70E6F4, 0x70E6F8, 0x70E6FC, 0x70EAD0, 0x70EAD4]
for va in candidates:
    v = u32(proc, va)
    label = 'POINTER' if looks_pointer(v) else 'value' if v else 'NULL'
    print(f'  [0x{va:08X}] = 0x{v:08X}  ({label})', flush=True)

# For each pointer-shaped global, look at the activation-related offsets.
print()
print('=== Activation-gate offsets at each pointer base ===', flush=True)
ACTIVATION_OFFSETS = [
    (0x98,  'u8',  'alive_state (3=spawned, 4=local-player-eligible)'),
    (0x5A0, 'u32', 'input_gate (>0 = input accepted)'),
    (0x5A4, 'u32', 'activation_a (set to 1 by 0x45AF00)'),
    (0x5A8, 'u32', 'activation_b (set to 1 by 0x45AF00)'),
    (0x59C, 'u32', 'activation_c (set to 0x18 by 0x45AF00)'),
    (0x970, 'u32', 'local_player_ptr (NULL = renderer skips)'),
    (0x14F4,'u8',  'misc alive flag'),
    (0x84,  'u32', 'uid (should match our account_id hash)'),
]

for va in candidates:
    base = u32(proc, va)
    if not looks_pointer(base):
        continue
    print(f'  --- base 0x{base:08X} (from [0x{va:08X}]) ---', flush=True)
    for off, ty, label in ACTIVATION_OFFSETS:
        if ty == 'u8':
            v = u8(proc, base + off)
            print(f'    +0x{off:04X} ({ty}) = {v:3d} (0x{v:02X})  {label}', flush=True)
        else:
            v = u32(proc, base + off)
            print(f'    +0x{off:04X} ({ty}) = 0x{v:08X}  {label}', flush=True)

# Try dereferencing through +0x970 if any candidate has a non-null local-player ptr
print()
print('=== Following +0x970 chain (local player struct) ===', flush=True)
for va in candidates:
    base = u32(proc, va)
    if not looks_pointer(base):
        continue
    lp = u32(proc, base + 0x970)
    if looks_pointer(lp):
        alive = u8(proc, lp + 0x98)
        uid = u32(proc, lp + 0x84)
        print(f'  base 0x{base:08X}: local_player at 0x{lp:08X}', flush=True)
        print(f'    [lp+0x98] alive = {alive}', flush=True)
        print(f'    [lp+0x84] uid   = 0x{uid:08X}', flush=True)
        # apparences area
        for i in range(14):
            v = struct.unpack('<H', rd(proc, lp + 0x120 + i*2, 2) or b'\x00\x00')[0]
            print(f'    apparence[{i:2d}] @ +0x{0x120 + i*2:04X} = {v}', flush=True)
        break

kernel32.CloseHandle(proc)
