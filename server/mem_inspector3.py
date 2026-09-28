"""
Read [socket+0x4CC] which is what helpers actually check.
"""
import ctypes, ctypes.wintypes as wt, struct

kernel32 = ctypes.windll.kernel32

class PE32(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD),('cntUsage', wt.DWORD),('th32ProcessID', wt.DWORD),
                ('th32DefaultHeapID', ctypes.c_void_p),('th32ModuleID', wt.DWORD),
                ('cntThreads', wt.DWORD),('th32ParentProcessID', wt.DWORD),
                ('pcPriClassBase', wt.LONG),('dwFlags', wt.DWORD),('szExeFile', wt.WCHAR * 260)]

def find_pid(name):
    snap = kernel32.CreateToolhelp32Snapshot(2, 0)
    e = PE32(); e.dwSize = ctypes.sizeof(e)
    if not kernel32.Process32FirstW(snap, ctypes.byref(e)): return None
    while True:
        if e.szExeFile.lower() == name.lower(): return e.th32ProcessID
        if not kernel32.Process32NextW(snap, ctypes.byref(e)): return None

def rd(proc, addr, size):
    buf = (ctypes.c_ubyte * size)(); read = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(proc, ctypes.c_void_p(addr), ctypes.byref(buf), size, ctypes.byref(read))
    return bytes(buf) if ok and read.value == size else None

def u32(proc, a): d = rd(proc, a, 4); return struct.unpack('<I', d)[0] if d else 0
def u8(proc, a): d = rd(proc, a, 1); return d[0] if d else 0

def main():
    pid = find_pid('WindSlayer_patched.exe')
    if not pid: print('Game not running'); return
    p = kernel32.OpenProcess(0x10 | 0x0400, False, pid)

    sock = u32(p, 0x70e6f4)
    sock2 = u32(p, 0x70e6f8)
    sock3 = u32(p, 0x70e6fc)
    gs = u32(p, 0x70e710)
    print(f'primary_socket [0x70e6f4] = 0x{sock:08X}')
    print(f'secondary_socket [0x70e6f8] = 0x{sock2:08X}')
    print(f'tertiary_socket [0x70e6fc] = 0x{sock3:08X}')
    print(f'game_state [0x70e710] = 0x{gs:08X}')

    # Helper 0x443280 does: eax = [esp+0x68] (esi from caller = socket)
    # Then: ecx = [socket+0x4CC]
    # Then: eax = [ecx+0x970]
    # Check: eax != 0 AND [eax+0x9c] > 0 AND [eax+0x14eb] == 0
    print('\n=== Helper 0x443280 state check path ===')

    # Try each socket as "esi"
    for name, s in [('primary', sock), ('secondary', sock2), ('tertiary', sock3)]:
        if not s: continue
        print(f'\n-- if esi = {name}_socket (0x{s:08X}):')
        ptr = u32(p, s + 0x4CC)
        print(f'   [socket+0x4CC] = 0x{ptr:08X}')
        if ptr:
            # Try reading [ptr+0x970]
            ptr2 = u32(p, ptr + 0x970)
            print(f'   [[socket+0x4CC]+0x970] = 0x{ptr2:08X}')
            if ptr2:
                v9c = u32(p, ptr2 + 0x9C)
                v14eb = u8(p, ptr2 + 0x14EB)
                v98 = u8(p, ptr2 + 0x98)
                print(f'     [+0x9C] = 0x{v9c:08X}')
                print(f'     [+0x98] = 0x{v98:02X}')
                print(f'     [+0x14EB] = 0x{v14eb:02X}')

    # The 0x2B handler uses [eax+0x110c] and [eax+0x1110] where eax = the 5616-byte
    # buf we sent. That buf is short-lived (allocated per call, freed soon).
    # But the 0x9C copy goes to THAT temp buffer, not a persistent object.
    # So helper checks [socket+0x4CC] which points elsewhere.

    # Let's also look at [0x70e710 + 0x5F0] (the 0x47E entry) contents
    gs_5f0 = u32(p, gs + 0x5F0)
    print(f'\n[game_state+0x5F0] (last-found-entry) = 0x{gs_5f0:08X}')
    if gs_5f0:
        # This is the 0x47E entry that helper 0x442EE0 stored
        print('   Entry contents:')
        for off in [0, 4, 8, 0xAC, 0xBC]:
            v = u32(p, gs_5f0 + off)
            print(f'     [+0x{off:02X}] = 0x{v:08X}')

    kernel32.CloseHandle(p)

if __name__ == '__main__':
    main()
