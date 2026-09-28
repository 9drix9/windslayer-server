"""Find the in-world scene and report the registration-gate fields:
  scene = [game_state+0x4cc]
  scene+0x220 = expected local-player uid (gate: entity+0x84 must equal this)
  scene+0x970 = local player ptr (NULL until registered)
  scene+0xf24 = transition flag (must be 0)
Scan candidate game_state objects (valid +0x4cc and +0x4e0 pointers).
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
    print(f'PID {pid}' + (f'  entity=0x{entity:08X}' if entity else ''))

    bufs = []
    addr = 0x10000
    mbi = MBI()
    while addr < 0x7FFF0000:
        if not k32.VirtualQueryEx(proc, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        b = mbi.BaseAddress or addr
        size = mbi.RegionSize or 0x1000
        if mbi.State == 0x1000 and mbi.Type == 0x20000 and mbi.Protect in (0x04, 0x40):
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

    LO, HI = 0x00400000, 0x7FFF0000

    def ok(v):
        return v is not None and LO <= v < HI

    seen = set()
    found = []
    for b, data in bufs:
        n = len(data) - 0x4e8
        off = 0
        while off < n:
            scene = struct.unpack_from('<I', data, off + 0x4cc)[0]
            if LO <= scene < HI:
                roster = struct.unpack_from('<I', data, off + 0x4e0)[0]
                if LO <= roster < HI:
                    f24 = u32(scene + 0xf24)
                    s220 = u32(scene + 0x220)
                    s970 = u32(scene + 0x970)
                    if f24 == 0 and s220 is not None and s220 < 0x100000:
                        if scene not in seen:
                            seen.add(scene)
                            found.append((b + off, scene, s220, s970, u32(scene + 0xee8)))
            off += 4

    print(f'{len(found)} scene candidates (f24==0, +0x220 small):')
    for gs, scene, s220, s970, ee8 in found[:40]:
        star = ''
        if entity and (s970 == entity or ee8 == entity):
            star = '  <<< local player == our entity!'
        print(f'  gs=0x{gs:08X} scene=0x{scene:08X}  +0x220(uid)={s220}  '
              f'+0x970(local)=0x{(s970 or 0):08X}  +0xee8=0x{(ee8 or 0):08X}{star}')
    k32.CloseHandle(proc)


if __name__ == '__main__':
    main()
