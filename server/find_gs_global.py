"""Find the game_state singleton via static-data globals. Scan the module's
data range for a dword that points to a heap object X where:
  [X+0x4cc] is a valid pointer (scene)
  [X+0x4e0] is a valid pointer (roster list)
  [X+0x408] == 0 (reentrancy flag, normally 0)
  scene[0] (vtable) is a valid code/rdata pointer
Report the global address and the resolved scene + gate fields.
"""
import ctypes, ctypes.wintypes as wt, struct, sys, bisect

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
    k32.ReadProcessMemory(proc, ctypes.c_void_p(a), ctypes.byref(buf), sz, ctypes.byref(r))
    return bytes(buf[:r.value]) if r.value else None


def main():
    entity = int(sys.argv[1], 16) if len(sys.argv) > 1 else None
    pid = find_pid('WindSlayer_patched.exe')
    proc = k32.OpenProcess(0x10 | 0x0400, False, pid)
    print(f'PID {pid}' + (f' entity=0x{entity:08X}' if entity else ''))

    # snapshot heap for pointer validation
    bufs = []
    addr = 0x10000
    mbi = MBI()
    while addr < 0x7FFF0000:
        if not k32.VirtualQueryEx(proc, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        b = mbi.BaseAddress or addr
        size = mbi.RegionSize or 0x1000
        if mbi.State == 0x1000 and mbi.Protect in (0x04, 0x40, 0x02, 0x20):
            data = rpm(proc, b, min(size, 0x4000000))
            if data:
                bufs.append((b, data))
        addr = b + size
    bufs.sort()
    starts = [b for b, _ in bufs]

    def mem(a, sz):
        i = bisect.bisect_right(starts, a) - 1
        if i < 0: return None
        b, d = bufs[i]
        off = a - b
        return d[off:off+sz] if 0 <= off and off+sz <= len(d) else None

    def u32(a):
        d = mem(a, 4); return struct.unpack('<I', d)[0] if d else None

    HEAP_LO, HEAP_HI = 0x00800000, 0x40000000
    CODE_LO, CODE_HI = 0x00400000, 0x00800000

    def heap_ok(v):
        return v is not None and HEAP_LO <= v < HEAP_HI

    # scan the module's data range (widened) for globals pointing to game_state
    found = []
    for b, data in bufs:
        if not (0x00600000 <= b <= 0x00790000):
            continue
        for off in range(0, len(data) - 4, 4):
            gs = struct.unpack_from('<I', data, off)[0]
            if not heap_ok(gs):
                continue
            scene = u32(gs + 0x4cc)
            roster = u32(gs + 0x4e0)
            if not (heap_ok(scene) and heap_ok(roster)):
                continue
            # roster must be a small list: count @ +0x18 in [1..8], head @ +0x10 heap ptr
            cnt = u32(roster + 0x18)
            head = u32(roster + 0x10)
            if cnt is not None and 1 <= cnt <= 8 and heap_ok(head):
                found.append((b + off, gs, scene, cnt))

    print(f'{len(found)} global(s) -> game_state-like object:')
    for g, gs, scene, cnt in found:
        s220 = u32(scene + 0x220)
        s970 = u32(scene + 0x970)
        sf24 = u32(scene + 0xf24)
        mark = '  <<< +0x970 == our entity' if entity and s970 == entity else ''
        print(f'  global@0x{g:08X} -> gs=0x{gs:08X} scene=0x{scene:08X} rostercnt={cnt} '
              f'+0x220(uid)={s220} +0x970(local)=0x{(s970 or 0):08X} +0xf24={sf24}{mark}')
    k32.CloseHandle(proc)


if __name__ == '__main__':
    main()
