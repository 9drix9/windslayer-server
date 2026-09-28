"""Directly write to the client to force our 0x07 entity into local-player state:
  +0x11dc = entity_base   (point name-ptr at our own name; [+0x11]=0 -> safe)
  +0x98   = 4             (alive = local-player-eligible -> nameplate + register path)
Optionally also set valid f64 positions. Then read back to confirm.

Usage: force_local.py <entity_hex> [posX posY]
"""
import ctypes, ctypes.wintypes as wt, struct, sys

k32 = ctypes.windll.kernel32
k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.ReadProcessMemory.restype = wt.BOOL
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.WriteProcessMemory.restype = wt.BOOL
k32.WriteProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
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
    posX = float(sys.argv[2]) if len(sys.argv) > 2 else None
    posY = float(sys.argv[3]) if len(sys.argv) > 3 else None
    pid = find_pid('WindSlayer_patched.exe')
    # VM_READ|VM_WRITE|VM_OPERATION|QUERY
    proc = k32.OpenProcess(0x10 | 0x20 | 0x08 | 0x0400, False, pid)
    if not proc:
        print('OpenProcess failed', k32.GetLastError()); return
    print(f'PID {pid} entity 0x{base:08X}')

    def rd(a, sz):
        buf = (ctypes.c_ubyte * sz)()
        r = ctypes.c_size_t(0)
        k32.ReadProcessMemory(proc, ctypes.c_void_p(a), ctypes.byref(buf), sz, ctypes.byref(r))
        return bytes(buf[:r.value]) if r.value else None

    def wr(a, payload):
        r = ctypes.c_size_t(0)
        buf = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
        ok = k32.WriteProcessMemory(proc, ctypes.c_void_p(a), ctypes.byref(buf), len(payload), ctypes.byref(r))
        return ok and r.value == len(payload)

    def u32(a):
        d = rd(a, 4); return struct.unpack('<I', d)[0] if d else None

    def u8(a):
        d = rd(a, 1); return d[0] if d else None

    print('BEFORE: alive=%s  +0x11dc=0x%08X' % (u8(base+0x98), u32(base+0x11dc) or 0))
    ok1 = wr(base + 0x11dc, struct.pack('<I', base))   # name ptr -> self
    ok2 = wr(base + 0x98, b'\x04')                       # alive = 4
    if posX is not None:
        wr(base + 0x11f8, struct.pack('<d', posX))
        wr(base + 0x1288, struct.pack('<d', posY))
        print(f'  wrote posX={posX} posY={posY}')
    print(f'  wrote +0x11dc={ok1}  +0x98={ok2}')
    print('AFTER : alive=%s  +0x11dc=0x%08X' % (u8(base+0x98), u32(base+0x11dc) or 0))
    k32.CloseHandle(proc)


if __name__ == '__main__':
    main()
