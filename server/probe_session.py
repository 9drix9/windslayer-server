"""
Probe candidate session/socket objects. If a pointer to the char struct
(0x1719D208) is found at some address Y, then the session object might be
at Y - 0x4CC. Check which candidate actually has the expected layout
(string fields, more pointers, etc. of a session struct).
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

pid = find_pid('WindSlayer_patched.exe')
if not pid:
    print('Game not running'); sys.exit(1)
proc = kernel32.OpenProcess(0x10|0x0400, False, pid)

# Re-find the active char. May have moved since last scan, re-verify.
# Look for the string "drix" prefixed by an 8-byte magic, then use the last such match.
# For now, trust the previously-found address.
DRIX_CHAR = 0x1719D208

# Candidate session bases: each reference address minus 0x4CC
# (offset used by handlers: `mov eax, [esi+0x4CC]`)
REFS = [0x033876A8, 0x03387C20, 0x0673E808, 0x06AFEA4C, 0x16FFB730]

print(f'Looking for session object where [session+0x4CC] == 0x{DRIX_CHAR:08X}\n')

for ref in REFS:
    session = ref - 0x4CC
    if session < 0x10000:
        print(f'  ref 0x{ref:08X} -> session 0x{session:08X} (too low, skip)')
        continue
    # Read at session
    d = rd(proc, session, 0x40)
    if not d or len(d) < 4:
        print(f'  ref 0x{ref:08X} -> session 0x{session:08X} UNREADABLE')
        continue
    # Check first dword — if it's a vtable or valid-looking pointer
    first = struct.unpack('<I', d[0:4])[0]
    # Also check: does [session+0x4CC] actually contain the char_ptr?
    at_4cc = u32(proc, session + 0x4CC)
    match = '**MATCH**' if at_4cc == DRIX_CHAR else ''
    print(f'  ref 0x{ref:08X} -> candidate session 0x{session:08X}: [+0x000]=0x{first:08X}, [+0x4CC]=0x{at_4cc:08X} {match}')
    # Dump first 64 bytes
    for i in range(0, 0x40, 16):
        row = d[i:i+16]
        h = ' '.join(f'{b:02X}' for b in row)
        a = ''.join(chr(b) if 32<=b<127 else '.' for b in row)
        print(f'    0x{session+i:08X}: {h}  {a}')

# If we found a match, dump the full session structure
print('\n\nDetailed dump of best candidate session(s):')
for ref in REFS:
    session = ref - 0x4CC
    if u32(proc, session + 0x4CC) == DRIX_CHAR:
        print(f'\n===== SESSION at 0x{session:08X} =====')
        # Dump 0x1000 bytes
        d = rd(proc, session, 0x600)
        for i in range(0, len(d), 16):
            row = d[i:i+16]
            h = ' '.join(f'{b:02X}' for b in row)
            a = ''.join(chr(b) if 32<=b<127 else '.' for b in row)
            print(f'  +{i:04X}: {h}  {a}')
        # Also try looking at offsets mentioned in our handler analysis:
        # - [session+0x4CC] = char_ptr (verified)
        # - [session+0x970] = map_state (from 0x4305B0 analysis)
        # - [session+0x4D8] = some status object
        print('\n  Known offsets referenced by handlers:')
        for off, name in [(0x4CC, 'char_ptr'), (0x4D8, 'status_obj'), (0x970, 'map_state'),
                          (0x5C0, 'list_head'), (0x5F0, 'last_entry'), (0x408, 'busy_flag'),
                          (0x368, 'buffer')]:
            v = u32(proc, session + off)
            print(f'    [+0x{off:03X}] {name:15s} = 0x{v:08X}')

kernel32.CloseHandle(proc)
