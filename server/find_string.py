"""Search the client's heap for a byte string (e.g. the character name) and
report every address, plus a hexdump of surrounding bytes, so we can identify
the structure that actually holds the in-world character data."""
import ctypes, ctypes.wintypes as wt, struct, sys

k32 = ctypes.windll.kernel32


class MBI(ctypes.Structure):
    _fields_ = [('BaseAddress', ctypes.c_void_p), ('AllocationBase', ctypes.c_void_p),
                ('AllocationProtect', wt.DWORD), ('__a', wt.DWORD),
                ('RegionSize', ctypes.c_size_t),
                ('State', wt.DWORD), ('Protect', wt.DWORD), ('Type', wt.DWORD)]


k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.ReadProcessMemory.restype = wt.BOOL
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]


class PE32(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ProcessID', wt.DWORD),
                ('th32DefaultHeapID', ctypes.c_void_p), ('th32ModuleID', wt.DWORD),
                ('cntThreads', wt.DWORD), ('th32ParentProcessID', wt.DWORD),
                ('pcPriClassBase', wt.LONG), ('dwFlags', wt.DWORD),
                ('szExeFile', wt.WCHAR * 260)]


def find_pid(name):
    snap = k32.CreateToolhelp32Snapshot(2, 0)
    e = PE32(); e.dwSize = ctypes.sizeof(e)
    if not k32.Process32FirstW(snap, ctypes.byref(e)):
        return None
    while True:
        if e.szExeFile.lower() == name.lower():
            return e.th32ProcessID
        if not k32.Process32NextW(snap, ctypes.byref(e)):
            return None


def rpm(proc, a, sz):
    buf = (ctypes.c_ubyte * sz)()
    r = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(proc, ctypes.c_void_p(a), ctypes.byref(buf), sz, ctypes.byref(r))
    return bytes(buf[:r.value]) if r.value else None


def main():
    needle = (sys.argv[1] if len(sys.argv) > 1 else 'TestHero').encode('latin-1')
    pid = find_pid('WindSlayer_patched.exe')
    if not pid:
        print('Game not running'); return
    proc = k32.OpenProcess(0x10 | 0x0400, False, pid)
    print(f'PID {pid}  searching for {needle!r}', flush=True)

    addr = 0x10000
    mbi = MBI()
    hits = []
    bufs = []
    while addr < 0x7FFF0000:
        if not k32.VirtualQueryEx(proc, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        b = mbi.BaseAddress or addr
        size = mbi.RegionSize or 0x1000
        if mbi.State == 0x1000 and mbi.Type == 0x20000 and mbi.Protect in (0x04, 0x40):
            data = rpm(proc, b, min(size, 0x4000000))
            if data:
                bufs.append((b, data))
                start = 0
                while True:
                    i = data.find(needle, start)
                    if i < 0:
                        break
                    hits.append(b + i)
                    start = i + 1
        addr = b + size

    bufs.sort()
    print(f'{len(hits)} hits', flush=True)

    def mem(a, sz):
        import bisect
        starts = [x[0] for x in bufs]
        idx = bisect.bisect_right(starts, a) - 1
        if idx < 0:
            return None
        bb, dd = bufs[idx]
        off = a - bb
        if 0 <= off and off + sz <= len(dd):
            return dd[off:off + sz]
        return None

    # For each hit, find pointers elsewhere in heap that point AT this address
    # (i.e., who references the name string) -> that's the record/struct.
    for h in hits[:20]:
        print(f'\n=== name @ 0x{h:08X} ===', flush=True)
        ctx = mem(h - 16, 64)
        if ctx:
            print('   ctx: ' + ctx.hex(), flush=True)
        # find references to h
        target = struct.pack('<I', h)
        refs = []
        for bb, dd in bufs:
            s = 0
            while True:
                j = dd.find(target, s)
                if j < 0:
                    break
                refs.append(bb + j)
                s = j + 1
                if len(refs) > 8:
                    break
            if len(refs) > 8:
                break
        print(f'   {len(refs)} pointer(s) -> this string: ' +
              ', '.join(f'0x{r:08X}' for r in refs[:8]), flush=True)

    k32.CloseHandle(proc)


if __name__ == '__main__':
    main()
