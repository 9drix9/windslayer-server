"""
Scan heap for any structure referencing loading-screen resource strings:
  "loading001.dds"   VA 0x006F1E65
  "loading001.bmp"   VA 0x004DD22B
  "loading.hui"      VA 0x004AEFCB
  "loading_a0.bmp"   VA 0x006F7889
These are pushed into UI dialog/sprite structs. Finding the structs
lets us zero their 'active' flag.
"""
import ctypes, ctypes.wintypes as wt, struct, sys

kernel32 = ctypes.windll.kernel32

class PE32(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD),('cntUsage', wt.DWORD),('th32ProcessID', wt.DWORD),
                ('th32DefaultHeapID', ctypes.c_void_p),('th32ModuleID', wt.DWORD),
                ('cntThreads', wt.DWORD),('th32ParentProcessID', wt.DWORD),
                ('pcPriClassBase', wt.LONG),('dwFlags', wt.DWORD),('szExeFile', wt.WCHAR * 260)]

class MBI(ctypes.Structure):
    _fields_ = [('BaseAddress', ctypes.c_void_p), ('AllocationBase', ctypes.c_void_p),
                ('AllocationProtect', wt.DWORD), ('RegionSize', ctypes.c_size_t),
                ('State', wt.DWORD), ('Protect', wt.DWORD), ('Type', wt.DWORD)]

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

pid = find_pid('WindSlayer_patched.exe')
if not pid:
    print('Game not running'); sys.exit(1)
proc = kernel32.OpenProcess(0x10|0x0400, False, pid)
print(f'PID {pid}', flush=True)

# Also search for the dynamic string refs — "loading" text appearing as zero-terminated strings
# anywhere in heap (could be allocated UI strings not in .rdata)
dynamic_patterns = [b'loading001', b'loading.hui', b'loading_a0',
                    b'WaitingMap', b'Loading', b'LOADING']

# Static .rdata VAs to search for as 4-byte pointer refs
static_vas = [0x006F1E65, 0x004DD22B, 0x004AEFCB, 0x006F7889,
              0x006F7871, 0x006F839C, 0x006F1EAD]

# Scan heap
addr = 0
mbi = MBI()
static_refs = {v: [] for v in static_vas}
dyn_refs = {p: [] for p in dynamic_patterns}

while addr < 0x80000000:
    ok = kernel32.VirtualQueryEx(proc, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
    if not ok:
        addr += 0x1000; continue
    base = mbi.BaseAddress or 0
    size = mbi.RegionSize
    if mbi.State == 0x1000 and mbi.Type in (0x20000, 0x40000) and (mbi.Protect & 0x74):
        chunk_size = 0x10000
        pos = base
        end = base + size
        while pos < end:
            chunk = rd(proc, pos, min(chunk_size, end - pos))
            if chunk:
                # Static pointer refs
                for v in static_vas:
                    needle = struct.pack('<I', v)
                    base_idx = 0
                    while True:
                        idx = chunk.find(needle, base_idx)
                        if idx == -1: break
                        static_refs[v].append(pos + idx)
                        base_idx = idx + 4
                # Dynamic string hits
                for p in dynamic_patterns:
                    base_idx = 0
                    while True:
                        idx = chunk.find(p, base_idx)
                        if idx == -1: break
                        dyn_refs[p].append(pos + idx)
                        base_idx = idx + 1
            pos += chunk_size
    addr = base + size
    if addr == 0: break

print('\n=== Static .rdata pointer refs (heap stores of string VA) ===')
for va, refs in static_refs.items():
    if refs:
        print(f'  VA 0x{va:08X}: {len(refs)} refs')
        for r in refs[:5]:
            ctx = rd(proc, r - 8, 24)
            print(f'    at 0x{r:08X}  ctx={ctx.hex()}')

print('\n=== Dynamic string occurrences (heap-allocated strings) ===')
for p, refs in dyn_refs.items():
    if refs:
        print(f'  {p}: {len(refs)} occurrences')
        for r in refs[:8]:
            # Show the full string
            s = rd(proc, r, 64).split(b'\x00')[0]
            print(f'    at 0x{r:08X}: "{s.decode("ascii", errors="replace")}"')
            # Look for who points to this string
            needle = struct.pack('<I', r)
            # (skip re-scan for performance, just note addr)

kernel32.CloseHandle(proc)
