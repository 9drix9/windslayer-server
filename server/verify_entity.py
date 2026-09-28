"""Verify entity fields against the values our 0x07 builder sent, to locate any
remaining wire misalignment."""
import ctypes, ctypes.wintypes as wt, struct, sys

k32 = ctypes.windll.kernel32
k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.ReadProcessMemory.restype = wt.BOOL
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]


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


def main():
    base = int(sys.argv[1], 16)
    pid = find_pid('WindSlayer_patched.exe')
    proc = k32.OpenProcess(0x10 | 0x0400, False, pid)
    print(f'PID {pid} entity 0x{base:08X}')

    def rd(a, sz):
        buf = (ctypes.c_ubyte * sz)()
        r = ctypes.c_size_t(0)
        k32.ReadProcessMemory(proc, ctypes.c_void_p(a), ctypes.byref(buf), sz, ctypes.byref(r))
        return bytes(buf[:r.value]) if r.value else None

    def u8(a):  d = rd(a, 1); return d[0] if d else None
    def u16(a): d = rd(a, 2); return struct.unpack('<H', d)[0] if d else None
    def u32(a): d = rd(a, 4); return struct.unpack('<I', d)[0] if d else None
    def f64(a): d = rd(a, 8); return struct.unpack('<d', d)[0] if d else None

    # (offset, type, expected)
    checks = [
        (0x84, 'u32', 1), (0x15d8, 'u32', 1), (0xe2, 'u16', 0), (0xe1, 'u8', 0),
        (0x12, 'u16', 0), (0x14, 'u8', 0),
        (0x110, 'u8', 1), (0x111, 'u8', 0), (0x99, 'u8', 1), (0x9a, 'u8', 0),
        (0x113, 'u8', 0),
        (0x120, 'u16', 0), (0x122, 'u16', 1), (0x124, 'u16', 1),  # apparence head/hair/face
        (0xe6, 'u16', 0),
        (0x954, 'u32', 32), (0x8d9, 'u8', 0), (0x8cf, 'u8', 1),
        (0x904, 'u32', 0), (0xe00, 'u32', 501), (0x8bd, 'u8', 0),
        (0x11f8, 'f64', 2000.0), (0x1288, 'f64', 2000.0), (0xe50, 'u32', 0),
        (0x8b3, 'u8', 127), (0x8b4, 'u8', 0), (0x8b5, 'u8', 0), (0x8b6, 'u8', 1),
        (0xd94, 'u32', 0), (0x8ec, 'u8', 0), (0x14f4, 'u8', 0),
    ]
    fn = {'u8': u8, 'u16': u16, 'u32': u32, 'f64': f64}
    for off, ty, exp in checks:
        got = fn[ty](base + off)
        ok = '  OK' if got == exp else '  <<< MISMATCH'
        gs = f'{got:.3f}' if ty == 'f64' and got is not None else (f'0x{got:X}' if got is not None else 'None')
        print(f'  +0x{off:04X} {ty:3s} expect={exp} got={gs}{ok}')
    k32.CloseHandle(proc)


if __name__ == '__main__':
    main()
