"""
Deep state inspector. Reads the exact variables the 0x2B helpers check.
"""
import ctypes
import ctypes.wintypes as wt
import struct

kernel32 = ctypes.windll.kernel32

class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', wt.DWORD), ('cntUsage', wt.DWORD),
        ('th32ProcessID', wt.DWORD), ('th32DefaultHeapID', ctypes.c_void_p),
        ('th32ModuleID', wt.DWORD), ('cntThreads', wt.DWORD),
        ('th32ParentProcessID', wt.DWORD), ('pcPriClassBase', wt.LONG),
        ('dwFlags', wt.DWORD), ('szExeFile', wt.WCHAR * 260),
    ]

def find_pid(name):
    snap = kernel32.CreateToolhelp32Snapshot(2, 0)
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    if not kernel32.Process32FirstW(snap, ctypes.byref(entry)):
        kernel32.CloseHandle(snap); return None
    while True:
        if entry.szExeFile.lower() == name.lower():
            kernel32.CloseHandle(snap); return entry.th32ProcessID
        if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
            break
    kernel32.CloseHandle(snap); return None

def rd(proc, addr, size):
    buf = (ctypes.c_ubyte * size)()
    read = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(proc, ctypes.c_void_p(addr),
                                     ctypes.byref(buf), size, ctypes.byref(read))
    return bytes(buf) if ok and read.value == size else None

def rd_u32(proc, addr):
    d = rd(proc, addr, 4)
    return struct.unpack('<I', d)[0] if d else None

def rd_u8(proc, addr):
    d = rd(proc, addr, 1)
    return d[0] if d else None

def main():
    pid = find_pid('WindSlayer_patched.exe')
    if not pid:
        print('Game not running'); return
    proc = kernel32.OpenProcess(0x10 | 0x0400, False, pid)

    # [0x70e710] = game state
    gs = rd_u32(proc, 0x70e710)
    print(f'game_state = 0x{gs:08X}')

    # [0x70e6f4] = primary socket (CSNSocket for game server)
    sock = rd_u32(proc, 0x70e6f4)
    print(f'primary_socket = 0x{sock:08X}')

    # [[esp+0x68]+0x4CC] in helper 0x443280 - this is where the "player" object is
    # esp+0x68 in the helper is the arg (esi from 0x2B handler) which is passed to it.
    # In the 0x2B handler, esi seems to be set from somewhere.
    # Let me try reading [game_state + various offsets] to find the player.

    # From 0x44D7CF (0x5A handler): `lea edi, [esi + 0x700]` suggests esi+0x700 is session
    # And esi seems to come from a global. Let me try [game_state + X] for likely offsets.

    # Try [game_state + 0x5F0] - this is where the found 0x47E entry gets stored
    entry_47e_at = rd_u32(proc, gs + 0x5F0)
    print(f'[game_state+0x5F0] (found 0x47E entry) = 0x{entry_47e_at or 0:08X}')

    # Read some key fields from game_state
    print('\n=== Key fields in game_state ===')
    for off in [0x0, 0x4, 0xC, 0x4CC, 0x5C0, 0x5F0, 0x970, 0xD0A, 0x3f8, 0x500, 0xE0]:
        val = rd_u32(proc, gs + off)
        if val is not None:
            print(f'  [gs+0x{off:04X}] = 0x{val:08X}')

    # Try reading [gs+0x4CC] as a session pointer
    session = rd_u32(proc, gs + 0x4CC)
    print(f'\nsession (gs+0x4CC) = 0x{session or 0:08X}')
    if session:
        print('  Session fields:')
        for off in [0x0, 0x4, 0x48, 0x131, 0x218, 0x220, 0x700, 0x970, 0xF70, 0xF61, 0x3F8]:
            val = rd_u32(proc, session + off)
            b = rd_u8(proc, session + off)
            print(f'    [session+0x{off:04X}] = 0x{val:08X} (byte: 0x{b:02X})')

        # Check the critical path [session+0x970][0x9c] and [+0x14EB]
        sess970 = rd_u32(proc, session + 0x970)
        print(f'  [session+0x970] = 0x{sess970 or 0:08X}')
        if sess970:
            sess970_9c = rd_u32(proc, sess970 + 0x9C)
            sess970_14eb = rd_u8(proc, sess970 + 0x14EB)
            sess970_98 = rd_u8(proc, sess970 + 0x98)
            print(f'    [+0x9c] = 0x{sess970_9c:08X} (the "is in world" value checked by helpers)')
            print(f'    [+0x98] = 0x{sess970_98:02X} (status byte: 3=alive)')
            print(f'    [+0x14EB] = 0x{sess970_14eb:02X} (flag byte)')

    # Also check what current scene / state is. There's usually a global
    # that indicates "current scene index". Search near game_state for common scene IDs.
    # "Waiting" dialog is entry 0x16 in the list, stored with its sub-dialog list.
    # Let me look at entry 0x16's sub-dialog list to see if it has anything in it.
    print('\n=== Entry 0x16 (dialog manager) sub-list ===')
    # Walk main list to find entry 0x16
    node = rd_u32(proc, gs + 0x5C0)
    while node:
        entry = rd_u32(proc, node + 8)
        if entry:
            key = rd_u32(proc, entry + 0)
            if key == 0x16:
                print(f'  Found entry 0x16 at 0x{entry:08X}')
                # Its sub-list probably at entry+0xac or similar
                # Check some offsets
                for off in [0x04, 0x08, 0x0C, 0x10, 0x1C, 0xA8, 0xAC, 0xB0, 0xBC]:
                    v = rd_u32(proc, entry + off)
                    print(f'    [+0x{off:02X}] = 0x{v or 0:08X}')
                break
        node = rd_u32(proc, node + 0)
        if node == 0: break

    kernel32.CloseHandle(proc)

if __name__ == '__main__':
    main()
