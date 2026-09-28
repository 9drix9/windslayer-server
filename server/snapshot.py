"""One-shot: dump critical state fields right now."""
import ctypes, ctypes.wintypes as wt, struct, sys

kernel32 = ctypes.windll.kernel32

class PE32(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD),('cntUsage', wt.DWORD),('th32ProcessID', wt.DWORD),
                ('th32DefaultHeapID', ctypes.c_void_p),('th32ModuleID', wt.DWORD),
                ('cntThreads', wt.DWORD),('th32ParentProcessID', wt.DWORD),
                ('pcPriClassBase', wt.LONG),('dwFlags', wt.DWORD),('szExeFile', wt.WCHAR * 260)]

def find_pid(name):
    snap = kernel32.CreateToolhelp32Snapshot(2, 0)
    e = PE32(); e.dwSize = ctypes.sizeof(e)
    if not kernel32.Process32FirstW(snap, ctypes.byref(e)): return None
    while True:
        if e.szExeFile.lower() == name.lower(): return e.th32ProcessID
        if not kernel32.Process32NextW(snap, ctypes.byref(e)): return None

def rd(proc, a, sz):
    buf = (ctypes.c_ubyte * sz)(); r = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(proc, ctypes.c_void_p(a), ctypes.byref(buf), sz, ctypes.byref(r))
    return bytes(buf) if ok and r.value == sz else None

def u32(proc, a):
    d = rd(proc, a, 4)
    return struct.unpack('<I', d)[0] if d else 0

def u8(proc, a):
    d = rd(proc, a, 1)
    return d[0] if d else 0

pid = find_pid('WindSlayer_patched.exe')
if not pid:
    print('Game not running'); sys.exit(1)
proc = kernel32.OpenProcess(0x10 | 0x0400, False, pid)
print(f'PID {pid}', flush=True)

gs = u32(proc, 0x70E710)
print(f'game_state = 0x{gs:08X}', flush=True)

print(f'\n[game_state fields]', flush=True)
fields_gs = [
    (0x4CC, 'char_ptr'),
    (0x4D8, 'socket_like'),
    (0x5C0, 'list_head'),
    (0x5F0, 'last_accessed_entry'),
    (0x59C, '???'),
    (0x5A4, '???'),
    (0x970, '???'),
]
for off, name in fields_gs:
    v = u32(proc, gs + off)
    print(f'  [gs+0x{off:03X}] {name:20s} = 0x{v:08X}', flush=True)

char = u32(proc, gs + 0x4CC)
if char:
    print(f'\n[char fields] char=0x{char:08X}', flush=True)
    for off, name in [
        (0x0C, 'ptr_A'),
        (0xEE8, 'map_ctx'),
        (0x970, 'map_state'),
        (0xF70, 'map_enter_byte'),
        (0xF5B, '?'),
    ]:
        v = u32(proc, char + off)
        print(f'  [char+0x{off:03X}] {name:20s} = 0x{v:08X}', flush=True)

    # Follow char->[0x0C]->[0x08]->[0x84]
    p1 = u32(proc, char + 0x0C)
    if p1:
        p2 = u32(proc, p1 + 0x08)
        if p2:
            val = u32(proc, p2 + 0x84)
            print(f'  [char->0x0C->0x08->0x84] = 0x{val:08X}  (opcode 0x2C validation target)', flush=True)
        else:
            print(f'  char->0x0C->0x08 == 0 (validation fails)', flush=True)
    else:
        print(f'  char->0x0C == 0 (validation fails)', flush=True)

    # Follow char->[0x970]->[0x15B4]
    ms = u32(proc, char + 0x970)
    if ms:
        b = u8(proc, ms + 0x15B4)
        print(f'  [char->0x970+0x15B4] byte = 0x{b:02X}  (0x4305B0 checks for 0x16)', flush=True)
        b2 = u8(proc, ms + 0x14EB)
        print(f'  [char->0x970+0x14EB] byte = 0x{b2:02X}', flush=True)
    else:
        print(f'  char->0x970 == 0 (loading screen state not allocated)', flush=True)

# Walk the list at [gs+0x5C0] to count entries and look for type 0x16
print(f'\n[list at gs+0x5C0]', flush=True)
head = u32(proc, gs + 0x5C0)
seen = set()
count = 0
keys = []
node = head
while node and node not in seen and count < 200:
    seen.add(node)
    entry = u32(proc, node + 8)
    key = u32(proc, entry) if entry else 0
    keys.append(key)
    nxt = u32(proc, node + 0)
    if nxt == 0: break
    node = nxt
    count += 1
print(f'  {count} entries, keys: {[hex(k) for k in keys]}', flush=True)

kernel32.CloseHandle(proc)
