"""Hexdump memory around given addresses (read-only) to inspect container structs."""
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

    for arg in sys.argv[1:]:
        a = int(arg, 16)
        lo = a - 0x20
        d = rd(lo, 0x60)
        print(f'\n=== around 0x{a:08X} (from 0x{lo:08X}) ===')
        if not d:
            print('  unreadable'); continue
        for i in range(0, len(d), 16):
            row = d[i:i+16]
            addr = lo + i
            words = ' '.join(f'{struct.unpack_from("<I", row, j)[0]:08x}' for j in range(0, len(row), 4))
            mark = ' <<<' if lo + i <= a < lo + i + 16 else ''
            print(f'  0x{addr:08X}: {words}{mark}')
    k32.CloseHandle(proc)


if __name__ == '__main__':
    main()
