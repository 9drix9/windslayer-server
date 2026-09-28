"""Enumerate the anim/record container at [game_state+0x4e0] (0x70eee0) to find a
valid +0xe60 index for a warrior player. Each list entry: node.next@[node],
payload@[node+8]. Payload is a record with name@+0, class@+0x644 (per 0x1A's copy).
"""
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
    show_all = '--all' in sys.argv
    pid = find_pid('WindSlayer_patched.exe')
    proc = k32.OpenProcess(0x10 | 0x0400, False, pid)
    print(f'PID {pid}')

    def rd(a, n):
        b = (ctypes.c_ubyte * n)(); r = ctypes.c_size_t(0)
        k32.ReadProcessMemory(proc, ctypes.c_void_p(a), ctypes.byref(b), n, ctypes.byref(r))
        return bytes(b[:r.value]) if r.value else None
    def u32(a):
        d = rd(a, 4); return struct.unpack('<I', d)[0] if d else None
    def cstr(a, n=18):
        d = rd(a, n)
        if not d: return ''
        z = d.find(b'\x00')
        return ''.join(chr(c) if 32 <= c < 127 else '.' for c in d[:z if z >= 0 else n])

    container = u32(0x70EEE0)
    if not container:
        print('container NULL - not in world'); return
    cnt = u32(container + 0x18)
    head = u32(container + 0x10)
    print(f'container=0x{container:08X} count={cnt} head=0x{(head or 0):08X}')

    node = head
    idx = 0
    warriorish = []
    while node and 0x400000 <= node < 0x7FFF0000 and idx < (cnt or 0) + 2:
        idx += 1
        payload = u32(node + 8)
        if payload and 0x400000 <= payload < 0x7FFF0000:
            nm = cstr(payload)
            cls = u32(payload + 0x644)
            sub = u32(payload + 0x648)
            # entries with a printable name and small class are character/NPC anim-sets
            printable = nm and all(32 <= ord(c) < 127 for c in nm)
            if show_all or (printable and len(nm) >= 1):
                print(f'  [{idx}] payload=0x{payload:08X} class={cls} +648={sub} name="{nm}"')
            if cls in (1, 2, 3, 4, 5, 6) and printable:
                warriorish.append((idx, cls, nm))
        node = u32(node)
    print(f'\nentries with class in 1..6 (player-class candidates):')
    for i, c, n in warriorish[:40]:
        print(f'  index {i}: class={c} name="{n}"')
    k32.CloseHandle(proc)


if __name__ == '__main__':
    main()
