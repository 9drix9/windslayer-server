"""Hexdump an entity struct and decode key fields."""
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
    print(f'PID {pid}  entity 0x{base:08X}')

    def rd(a, sz):
        buf = (ctypes.c_ubyte * sz)()
        r = ctypes.c_size_t(0)
        k32.ReadProcessMemory(proc, ctypes.c_void_p(a), ctypes.byref(buf), sz, ctypes.byref(r))
        return bytes(buf[:r.value]) if r.value else None

    def u32(a):
        d = rd(a, 4); return struct.unpack('<I', d)[0] if d else None

    def u8(a):
        d = rd(a, 1); return d[0] if d else None

    def f64(a):
        d = rd(a, 8); return struct.unpack('<d', d)[0] if d else None

    # hexdump first 0x40
    head = rd(base, 0x40)
    print('first 0x40 bytes:')
    for i in range(0, 0x40, 16):
        row = head[i:i+16]
        h = ' '.join(f'{c:02x}' for c in row)
        a = ''.join(chr(c) if 32 <= c < 127 else '.' for c in row)
        print(f'  +0x{i:03x}: {h}  {a}')

    print(f'  byte@+0x11 = {u8(base+0x11)}  (nameplate reads [+0x11dc]+0x11)')
    print(f'  +0x84 uid    = 0x{(u32(base+0x84) or 0):08X}')
    print(f'  +0x98 alive  = {u8(base+0x98)}')
    print(f'  +0x9c        = 0x{(u32(base+0x9c) or 0):08X}')
    print(f'  +0x11dc nptr = 0x{(u32(base+0x11dc) or 0):08X}')
    print(f'  +0x11f8 posX = {f64(base+0x11f8)}')
    print(f'  +0x1288 posY = {f64(base+0x1288)}')
    print(f'  +0x644 class = {u32(base+0x644)}')
    print(f'  +0x8e1       = {u8(base+0x8e1)}')
    k32.CloseHandle(proc)


if __name__ == '__main__':
    main()
