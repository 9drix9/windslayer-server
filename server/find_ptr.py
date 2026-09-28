"""Find all 4-byte pointers in heap that equal a target address, and show the
offset within the containing allocation region + surrounding context. Used to
locate the scene (which holds our entity at +0x970) and the entity list."""
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
    target = int(sys.argv[1], 16)
    pid = find_pid('WindSlayer_patched.exe')
    proc = k32.OpenProcess(0x10 | 0x0400, False, pid)
    print(f'PID {pid}  searching pointers to 0x{target:08X}')

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

    needle = struct.pack('<I', target)
    refs = []
    for b, data in bufs:
        s = 0
        while True:
            j = data.find(needle, s)
            if j < 0: break
            if j % 4 == 0:  # aligned pointer
                refs.append(b + j)
            s = j + 1
    print(f'{len(refs)} aligned pointer(s)')
    for r in refs[:60]:
        # is this the local-player slot? then container = r - 0x970 should have a +0x220 etc.
        # is this an entity-list node? show a few dwords around
        around = mem(r - 0x10, 0x20)
        hexs = around.hex() if around else '??'
        # if r corresponds to scene+0x970, container base = r-0x970
        cont = r - 0x970
        c220 = u32(cont + 0x220)
        c4cc_owner = ''
        print(f'  ptr@0x{r:08X}  [as scene+0x970 -> scene=0x{cont:08X}, scene+0x220(map?)=0x{(c220 or 0):08X}]')
    k32.CloseHandle(proc)


if __name__ == '__main__':
    main()
