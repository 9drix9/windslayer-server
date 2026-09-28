"""
Scan game heap for references to the loading-screen string VAs, identify
the UI object(s) holding them. These are candidates for force-dismissal.
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

# Target string VAs (from PE analysis)
targets = {
    0x006F1E65: 'loading001.dds',
    0x006F7871: 'loading8_a0.bmp',
    0x006F7889: 'loading_a0.bmp',
    0x006F7859: 'loading16_a0.bmp',
    0x006F1EAD: 'WaitingMap',
    0x006F839C: 'loading %s',
    0x006F83AC: 'loading update list...',
    0x006F1E87: '\\x00Map',
    0x004AEFCB: 'loading.hui',
    0x004DD32F: 'loading001.dds_b',
}

# Walk virtual memory, find references to each target VA in committed private/heap regions
print('Scanning heap for references...', flush=True)
found = {va: [] for va in targets}

addr = 0
mbi = MBI()
scanned_mb = 0
while addr < 0x80000000:
    ok = kernel32.VirtualQueryEx(proc, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
    if not ok:
        addr += 0x1000; continue
    base = mbi.BaseAddress or 0
    size = mbi.RegionSize
    # MEM_COMMIT=0x1000, heap-ish: Private(0x20000) or Mapped(0x40000)
    if mbi.State == 0x1000 and mbi.Type in (0x20000, 0x40000) and (mbi.Protect & 0x74):
        # Scan region
        chunk_size = 0x10000
        pos = base
        end = base + size
        while pos < end:
            chunk = rd(proc, pos, min(chunk_size, end - pos))
            if chunk:
                for va, name in targets.items():
                    needle = struct.pack('<I', va)
                    base_idx = 0
                    while True:
                        idx = chunk.find(needle, base_idx)
                        if idx == -1: break
                        found[va].append(pos + idx)
                        base_idx = idx + 4
            pos += chunk_size
            scanned_mb += chunk_size / (1024*1024)
    addr = base + size
    if addr == 0: break

print(f'Scanned ~{scanned_mb:.0f} MB', flush=True)
for va, refs in found.items():
    if refs:
        print(f'\n"{targets[va]}" (VA 0x{va:08X}): {len(refs)} references', flush=True)
        for r in refs[:5]:
            # Read surrounding context
            ctx = rd(proc, r - 8, 32)
            h = ' '.join(f'{b:02X}' for b in ctx)
            print(f'  0x{r:08X}: {h}', flush=True)

kernel32.CloseHandle(proc)
