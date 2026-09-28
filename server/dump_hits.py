"""Dump candidate struct bases (addresses where 'TestHero' starts) interpreting
each as both an ENTITY (0x15f0) and a ROSTER RECORD, to classify them."""
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
    pid = find_pid('WindSlayer_patched.exe')
    proc = k32.OpenProcess(0x10 | 0x0400, False, pid)
    print(f'PID {pid}')

    def rd(a, sz):
        buf = (ctypes.c_ubyte * sz)()
        r = ctypes.c_size_t(0)
        k32.ReadProcessMemory(proc, ctypes.c_void_p(a), ctypes.byref(buf), sz, ctypes.byref(r))
        return bytes(buf[:r.value]) if r.value else None

    def u32(a):
        d = rd(a, 4); return struct.unpack('<I', d)[0] if d else None

    def u8(a):
        d = rd(a, 1); return d[0] if d else None

    def cstr(a, n=20):
        d = rd(a, n)
        if not d: return None
        z = d.find(b'\x00'); raw = d[:z if z >= 0 else n]
        return ''.join(chr(c) if 32 <= c < 127 else '.' for c in raw)

    addrs = [int(x, 16) for x in sys.argv[1:]]
    for a in addrs:
        print(f'\n=== base 0x{a:08X}  name="{cstr(a)}" ===')
        print(f'  ENTITY view : +0x84 uid=0x{(u32(a+0x84) or 0):08X}  '
              f'+0x98 alive={u8(a+0x98)}  +0x11dc nameptr=0x{(u32(a+0x11dc) or 0):08X}  '
              f'+0x8e1={u8(a+0x8e1)}')
        print(f'  RECORD view : +0x644 class={u32(a+0x644)}  +0x648={u32(a+0x648)}  '
              f'+0x650={u32(a+0x650)}  +0x640 b={u8(a+0x640)}')
    k32.CloseHandle(proc)


if __name__ == '__main__':
    main()
