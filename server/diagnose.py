"""
Comprehensive diagnostic: find the socket 'this' pointer and follow the
validation chain used by opcode 0x2C / 0x05:
  esi (socket) -> +0x4CC = char_ptr
  char_ptr     -> +0x0C  = ptr_A
  ptr_A        -> +0x08  = ptr_B
  ptr_B        -> +0x84  = validation_value (map_id we need to send)

Also dumps entries from the linked list at [game_state+0x5C0] to identify
the 'loading' dialog for possible force-dismiss.
"""
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
    kernel32.Process32FirstW(snap, ctypes.byref(e))
    while True:
        if e.szExeFile.lower() == name.lower(): return e.th32ProcessID
        if not kernel32.Process32NextW(snap, ctypes.byref(e)): return None

def rd(proc, a, sz):
    buf = (ctypes.c_ubyte*sz)(); r = ctypes.c_size_t(0)
    kernel32.ReadProcessMemory(proc, ctypes.c_void_p(a), ctypes.byref(buf), sz, ctypes.byref(r))
    return bytes(buf[:r.value])

def u32(proc, a):
    d = rd(proc, a, 4)
    return struct.unpack('<I', d)[0] if len(d) == 4 else 0

def valid_ptr(v):
    return 0x10000 < v < 0x80000000

pid = find_pid('WindSlayer_patched.exe')
if not pid:
    print('Game not running'); sys.exit(1)
proc = kernel32.OpenProcess(0x10|0x0400, False, pid)
print(f'PID {pid}', flush=True)

# Try multiple known globals as esi candidates
print('\n=== Socket/session candidates ===', flush=True)
candidates = []
for addr, label in [(0x70E6F4, 'packet_global'), (0x70E6FC, 'udp_sock_global'),
                    (0x70E700, 'unk_700'), (0x70E710, 'game_state')]:
    v = u32(proc, addr)
    if not valid_ptr(v):
        print(f'  [0x{addr:08X}] {label} = 0x{v:08X} (invalid)', flush=True); continue
    char_ptr = u32(proc, v + 0x4CC)
    note = ''
    if valid_ptr(char_ptr):
        note = ' <- char_ptr looks valid'
        candidates.append((addr, label, v, char_ptr))
    print(f'  [0x{addr:08X}] {label} = 0x{v:08X}, +0x4CC = 0x{char_ptr:08X}{note}', flush=True)

# For each good candidate, follow validation chain
print('\n=== Validation chain (for opcode 0x2C/0x05 map_id) ===', flush=True)
for addr, label, sock, char_ptr in candidates:
    print(f'\nFor {label}:', flush=True)
    print(f'  char_ptr = 0x{char_ptr:08X}', flush=True)
    ptr_A = u32(proc, char_ptr + 0x0C)
    print(f'  char_ptr+0x0C = 0x{ptr_A:08X}', flush=True)
    if valid_ptr(ptr_A):
        ptr_B = u32(proc, ptr_A + 0x08)
        print(f'    +0x08 = 0x{ptr_B:08X}', flush=True)
        if valid_ptr(ptr_B):
            val = u32(proc, ptr_B + 0x84)
            print(f'      +0x84 = 0x{val:08X}  <<< this is the map_id to send', flush=True)
    # Also check char+0x970
    map_state = u32(proc, char_ptr + 0x970)
    print(f'  char_ptr+0x970 (map_state) = 0x{map_state:08X}', flush=True)
    if valid_ptr(map_state):
        b_15b4 = rd(proc, map_state + 0x15B4, 1)
        b_14EB = rd(proc, map_state + 0x14EB, 1)
        print(f'    +0x15B4 byte = 0x{b_15b4[0]:02X}  (0x4305B0 wants 0x16)', flush=True)
        print(f'    +0x14EB byte = 0x{b_14EB[0]:02X}', flush=True)

# Dump list entries — focus on likely loading dialog entries
print('\n=== List walk [game_state+0x5C0] ===', flush=True)
gs = u32(proc, 0x70E710)
if valid_ptr(gs):
    head = u32(proc, gs + 0x5C0)
    seen = set()
    count = 0
    high_type_entries = []  # entries with key >= 0x100 (potential dialogs)
    node = head
    while node and node not in seen and count < 600:
        seen.add(node)
        entry = u32(proc, node + 8)
        key = u32(proc, entry) if valid_ptr(entry) else 0
        if key >= 0x100:
            high_type_entries.append((key, entry, node))
        nxt = u32(proc, node + 0)
        if nxt == 0 or not valid_ptr(nxt): break
        node = nxt
        count += 1
    print(f'  Total entries: {count}', flush=True)
    print(f'  Entries with key >= 0x100: {len(high_type_entries)}', flush=True)
    for key, entry, node in high_type_entries[:30]:
        # Dump first 64 bytes of entry
        d = rd(proc, entry, 64)
        # Look for .rdata string pointers
        str_refs = []
        for i in range(0, len(d)-3, 4):
            v = struct.unpack('<I', d[i:i+4])[0]
            if 0x006F0000 <= v < 0x00700000 or 0x004AE000 <= v < 0x00500000:
                s = rd(proc, v, 32).split(b'\x00')[0]
                try: txt = s.decode('ascii')
                except: txt = repr(s)
                if txt and any(c.isalpha() for c in txt):
                    str_refs.append(f'+{i:02X}:"{txt}"')
        refs_str = ' '.join(str_refs[:3]) if str_refs else ''
        print(f'  key=0x{key:X}  entry=0x{entry:08X}  {refs_str}', flush=True)

kernel32.CloseHandle(proc)
