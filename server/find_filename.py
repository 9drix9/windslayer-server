"""
Scan game heap for the formatted string "stage12_85" or related variants.
If found, dump the surrounding struct — the two integers 12 and 85 should
be nearby (maybe in a fixed struct alongside the formatted filename).
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

# Search for formatted filenames the client might have built
patterns = [b'stage12_85', b'stage00_00', b'stage01_01', b'./hs/stage']
addr = 0
mbi = MBI()
hits = {p: [] for p in patterns}
scanned_mb = 0

while addr < 0x80000000:
    if not kernel32.VirtualQueryEx(proc, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
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
                for p in patterns:
                    base_idx = 0
                    while True:
                        idx = chunk.find(p, base_idx)
                        if idx == -1: break
                        hits[p].append(pos + idx)
                        base_idx = idx + 1
            pos += chunk_size
            scanned_mb += chunk_size / (1024*1024)
    addr = base + size
    if addr == 0: break

print(f'Scanned ~{scanned_mb:.0f} MB', flush=True)
for p, hs in hits.items():
    pretty = p.decode('ascii', errors='replace')
    print(f'\n"{pretty}": {len(hs)} occurrences')
    for h in hs[:5]:
        # Dump 64 bytes BEFORE and 32 AFTER the hit — integers near the buffer
        ctx = rd(proc, max(0, h - 64), 96 + len(p))
        print(f'  at 0x{h:08X}:')
        for i in range(0, len(ctx), 16):
            row = ctx[i:i+16]
            h_str = ' '.join(f'{b:02X}' for b in row)
            a_str = ''.join(chr(b) if 32<=b<127 else '.' for b in row)
            offset = (max(0, h - 64) + i) - h
            mark = ' <-- HIT HERE' if -16 < offset < len(p) else ''
            print(f'    off{offset:+4d}: {h_str}  {a_str}{mark}')
kernel32.CloseHandle(proc)
