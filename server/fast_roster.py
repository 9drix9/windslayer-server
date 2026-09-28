"""fast_roster.py — locate game_state and dump the char roster, fast.

Strategy: snapshot all committed private RW regions into local buffers ONCE,
then resolve everything in-process (no per-candidate syscalls). game_state is a
heap object with this signature:
   [gs+0x4cc]            -> valid pointer (scene)            (from 0x451FF7 / gate arg)
   [gs+0x4e0 + 0x10]     -> valid pointer (roster list head) (list_get 0x409770)
   [gs+0x4e0 + 0x18]     -> small count 1..16                (list_get bound check)
The roster record list is walked exactly like VA 0x409770 (1-based index).
Record starts with the name string; class is at record+0x644.
"""
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
k32.CloseHandle.argtypes = [wt.HANDLE]


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
    return bytes(buf[:r.value]) if ok else (bytes(buf[:r.value]) if r.value else None)


def main():
    pid = find_pid('WindSlayer_patched.exe')
    if not pid:
        print('Game not running'); return
    proc = k32.OpenProcess(0x10 | 0x0400, False, pid)
    if not proc:
        print('OpenProcess failed', k32.GetLastError()); return
    print(f'PID {pid}', flush=True)

    # enumerate committed private RW regions
    regions = []
    addr = 0x10000
    mbi = MBI()
    total = 0
    while addr < 0x7FFF0000:
        if not k32.VirtualQueryEx(proc, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        b = mbi.BaseAddress or addr
        size = mbi.RegionSize or 0x1000
        if (mbi.State == 0x1000 and mbi.Type == 0x20000
                and mbi.Protect in (0x04, 0x40)):
            regions.append((b, size))
            total += size
        addr = b + size
    print(f'{len(regions)} private RW regions, {total/1e6:.1f} MB', flush=True)

    # snapshot into buffers
    bufs = []  # (base, bytes)
    for b, size in regions:
        chunk = rpm(proc, b, min(size, 0x2000000))
        if chunk:
            bufs.append((b, chunk))
    bufs.sort()
    print(f'snapshotted {len(bufs)} buffers', flush=True)

    import bisect
    starts = [b for b, _ in bufs]

    def mem(a, sz):
        i = bisect.bisect_right(starts, a) - 1
        if i < 0:
            return None
        b, data = bufs[i]
        off = a - b
        if 0 <= off and off + sz <= len(data):
            return data[off:off + sz]
        return None

    def mu32(a):
        d = mem(a, 4)
        return struct.unpack('<I', d)[0] if d else None

    def ok(v):
        return v is not None and 0x00400000 <= v < 0x7FFF0000

    def cstr(a, n=32):
        d = mem(a, n)
        if not d:
            return None
        z = d.find(b'\x00')
        raw = d[:z if z >= 0 else n]
        return ''.join(chr(c) if 32 <= c < 127 else '.' for c in raw)

    def list_get(list_ptr, index):
        # list_ptr is the ALREADY-dereferenced list object ([game_state+0x4e0]).
        cnt = mu32(list_ptr + 0x18)
        if cnt is None or index < 1 or index > cnt:
            return None
        cur = mu32(list_ptr + 0x10)
        for _ in range(index - 1):
            if not ok(cur):
                return None
            cur = mu32(cur)
        if not ok(cur):
            return None
        return mu32(cur + 8)

    # scan candidate game_state bases. [gs+0x4e0] is a POINTER to the list obj;
    # list.count @ +0x18, list.head @ +0x10 (per VA 0x409770). Read gs-local
    # fields straight from the buffer; deref the list pointer via mem().
    LO, HI = 0x00400000, 0x7FFF0000
    cands = []
    for b, data in bufs:
        n = len(data)
        end = n - 0x4e8
        unpack = struct.unpack_from
        off = 0
        while off < end:
            scene = unpack('<I', data, off + 0x4cc)[0]
            if LO <= scene < HI:
                lp = unpack('<I', data, off + 0x4e0)[0]
                if LO <= lp < HI:
                    cnt = mu32(lp + 0x18)
                    if cnt is not None and 1 <= cnt <= 16:
                        head = mu32(lp + 0x10)
                        if head is not None and LO <= head < HI:
                            cands.append((b + off, scene, lp, cnt, head))
            off += 4
    print(f'{len(cands)} candidate game_state objects', flush=True)

    shown = 0
    for gs, scene, lp, cnt, head in cands:
        rec = list_get(lp, 1)
        if not ok(rec):
            continue
        name = cstr(rec)
        cls = mu32(rec + 0x644)
        printable = bool(name) and all(c != '.' for c in name)
        tag = '' if printable else '  (name not printable)'
        print(f'\n=== game_state 0x{gs:08X}  list=0x{lp:08X} ===', flush=True)
        print(f'    +0x4cc scene  = 0x{scene:08X}', flush=True)
        print(f'    roster        : count={cnt} head=0x{head:08X}', flush=True)
        for idx in range(1, cnt + 1):
            r = list_get(lp, idx)
            if not ok(r):
                print(f'      [{idx}] bad ptr', flush=True); continue
            print(f'      [{idx}] rec=0x{r:08X} class={mu32(r+0x644)} '
                  f'name="{cstr(r)}"{tag}', flush=True)
        shown += 1
        if shown >= 12:
            break
    if shown == 0:
        print('No plausible roster found.', flush=True)

    k32.CloseHandle(proc)


if __name__ == '__main__':
    main()
