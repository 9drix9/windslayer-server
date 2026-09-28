"""
Attach to WindSlayer_patched.exe and inspect its memory.

Dump the linked list at [[0x70e710] + 0x5C0] to see which "key" entries exist.
The 0x2B handler's helpers look for key 0x47E; if we see it, our 0x2E packet
worked and the problem is elsewhere. If we don't see it, 0x2E never completed.
"""
import ctypes
import ctypes.wintypes as wt
import struct
import sys
import time

PROCESS_ALL_ACCESS = 0x1F0FFF
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
TH32CS_SNAPPROCESS = 0x00000002

kernel32 = ctypes.windll.kernel32

class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', wt.DWORD),
        ('cntUsage', wt.DWORD),
        ('th32ProcessID', wt.DWORD),
        ('th32DefaultHeapID', ctypes.c_void_p),
        ('th32ModuleID', wt.DWORD),
        ('cntThreads', wt.DWORD),
        ('th32ParentProcessID', wt.DWORD),
        ('pcPriClassBase', wt.LONG),
        ('dwFlags', wt.DWORD),
        ('szExeFile', wt.WCHAR * 260),
    ]

def find_process(name):
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return None
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    if not kernel32.Process32FirstW(snap, ctypes.byref(entry)):
        kernel32.CloseHandle(snap)
        return None
    while True:
        if entry.szExeFile.lower() == name.lower():
            kernel32.CloseHandle(snap)
            return entry.th32ProcessID
        if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
            break
    kernel32.CloseHandle(snap)
    return None

def read_mem(proc, addr, size):
    buf = (ctypes.c_ubyte * size)()
    read = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(proc, ctypes.c_void_p(addr),
                                     ctypes.byref(buf), size,
                                     ctypes.byref(read))
    if ok and read.value == size:
        return bytes(buf)
    return None

def read_dword(proc, addr):
    data = read_mem(proc, addr, 4)
    if data is None:
        return None
    return struct.unpack('<I', data)[0]

def main():
    pid = find_process('WindSlayer_patched.exe')
    if not pid:
        print('WindSlayer_patched.exe not running - launch the game first')
        return
    print(f'Found PID {pid}')

    proc = kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION,
                                 False, pid)
    if not proc:
        print(f'OpenProcess failed: {kernel32.GetLastError()}')
        return

    # The game state is referenced as [0x70e710]. That's a static global holding
    # a pointer to the game-state struct. Read it:
    game_state_ptr_addr = 0x70e710
    game_state_ptr = read_dword(proc, game_state_ptr_addr)
    print(f'[0x{game_state_ptr_addr:08X}] (game state ptr) = 0x{game_state_ptr or 0:08X}')
    if not game_state_ptr:
        print('Game state is NULL. Client may not have initialized yet.')
        kernel32.CloseHandle(proc)
        return

    # The list head is at [game_state + 0x5C0]
    list_head_ptr_addr = game_state_ptr + 0x5C0
    list_head = read_dword(proc, list_head_ptr_addr)
    print(f'[game_state+0x5C0] (list head) = 0x{list_head or 0:08X}')

    if not list_head:
        print('List is empty - no entries created yet.')
        kernel32.CloseHandle(proc)
        return

    # Walk the linked list. Each node: [0]=next, [4]=?, [8]=entry_with_key
    # entry has key at [entry+0]
    print('\nWalking list...')
    node = list_head
    count = 0
    seen = set()
    while node and count < 200 and node not in seen:
        seen.add(node)
        nxt = read_dword(proc, node + 0)
        entry = read_dword(proc, node + 8)
        key = None
        if entry:
            key = read_dword(proc, entry + 0)
        key_str = f'0x{key:04X}' if key is not None else 'NULL'
        print(f'  [{count:3d}] node=0x{node:08X}  entry=0x{entry or 0:08X}  key={key_str}')
        node = nxt
        count += 1

    print(f'\nTotal entries: {count}')
    # Check for specific keys
    keys_we_care = {0x47E, 0x480, 0x4B, 0x16, 0x2A, 0x14, 0x478, 0x17B, 0x4D}
    found_keys = set()
    node = list_head
    seen2 = set()
    while node and node not in seen2:
        seen2.add(node)
        entry = read_dword(proc, node + 8)
        if entry:
            key = read_dword(proc, entry + 0)
            if key is not None:
                found_keys.add(key)
        node = read_dword(proc, node + 0)
        if node == 0:
            break
    print(f'\nKeys we care about:')
    for k in sorted(keys_we_care):
        status = 'PRESENT' if k in found_keys else 'MISSING'
        print(f'  0x{k:04X}: {status}')

    # Also read session pointer (player + 0x4CC) if we can find the player
    # The player object pointer is often at [0x70e6f4 + some offset] or stored as a global
    # Try [0x70e6f4]
    sock_ptr = read_dword(proc, 0x70e6f4)
    print(f'\n[0x70e6f4] (primary socket) = 0x{sock_ptr or 0:08X}')

    kernel32.CloseHandle(proc)

if __name__ == '__main__':
    main()
