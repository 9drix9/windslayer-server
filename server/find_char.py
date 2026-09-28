"""
Find the real character struct by scanning heap for character name strings.
Whichever struct contains "drix\x00" IS the char struct — from there we can
find the containing session/socket object.
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

# Scan committed private heap regions for character name strings
# Try multiple — whichever character the user selected
targets = [b'drix\x00', b'TestHero\x00', b'testestst\x00']
hits = {t: [] for t in targets}

addr = 0
mbi = MBI()
scanned_mb = 0
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
                for t in targets:
                    base_idx = 0
                    while True:
                        idx = chunk.find(t, base_idx)
                        if idx == -1: break
                        hits[t].append(pos + idx)
                        base_idx = idx + 1
            pos += chunk_size
            scanned_mb += chunk_size / (1024*1024)
    addr = base + size
    if addr == 0: break

print(f'Scanned ~{scanned_mb:.0f} MB', flush=True)
for t, hs in hits.items():
    if hs:
        print(f'\n"{t.rstrip(chr(0).encode()).decode()}": {len(hs)} occurrences')
        for h in hs[:10]:
            # Dump surrounding bytes to see struct context
            ctx = rd(proc, max(0, h - 8), 32)
            cxh = ' '.join(f'{b:02X}' for b in ctx)
            print(f'  0x{h:08X}: ctx={cxh}')

# For each hit, try to find where this address is REFERENCED (pointer to it)
# That might be part of the character struct / session struct
print('\n\nSearching for POINTERS TO any of these string addresses:')
all_hit_addrs = []
for hs in hits.values():
    all_hit_addrs.extend(hs)

if all_hit_addrs:
    # Just scan heap for 4-byte LE of each address
    addr = 0
    refs = {h: [] for h in all_hit_addrs}
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
                    for h in all_hit_addrs:
                        needle = struct.pack('<I', h)
                        base_idx = 0
                        while True:
                            idx = chunk.find(needle, base_idx)
                            if idx == -1: break
                            refs[h].append(pos + idx)
                            base_idx = idx + 4
                pos += chunk_size
        addr = base + size
        if addr == 0: break
    for h, rs in refs.items():
        if rs:
            print(f'\n  0x{h:08X} referenced from {len(rs)} locations:')
            for r in rs[:5]:
                print(f'    at 0x{r:08X}')
else:
    print('  no hits to search for')

kernel32.CloseHandle(proc)
