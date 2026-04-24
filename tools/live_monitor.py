"""
Live memory monitor - polls client state every second and shows changes.
No INT3 breakpoints - safe to run while game is active.
"""
import ctypes, ctypes.wintypes as wt, struct, time, sys

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
        if e.szExeFile.lower() == name.lower():
            kernel32.CloseHandle(snap); return e.th32ProcessID
        if not kernel32.Process32NextW(snap, ctypes.byref(e)):
            kernel32.CloseHandle(snap); return None

def u32(proc, addr):
    buf = (ctypes.c_ubyte * 4)(); r = ctypes.c_size_t(0)
    if kernel32.ReadProcessMemory(proc, ctypes.c_void_p(addr), ctypes.byref(buf), 4, ctypes.byref(r)):
        if r.value == 4:
            return struct.unpack('<I', bytes(buf))[0]
    return 0

def u8(proc, addr):
    buf = (ctypes.c_ubyte * 1)(); r = ctypes.c_size_t(0)
    if kernel32.ReadProcessMemory(proc, ctypes.c_void_p(addr), ctypes.byref(buf), 1, ctypes.byref(r)):
        if r.value == 1:
            return buf[0]
    return 0

def walk_list(proc, head):
    """Walk linked list at head, return set of (key, entry_ptr) tuples."""
    seen = set()
    items = []
    node = head
    while node and node not in seen and len(items) < 500:
        seen.add(node)
        entry = u32(proc, node + 8)
        key = u32(proc, entry + 0) if entry else 0
        items.append((key, entry, node))
        nxt = u32(proc, node + 0)
        if nxt == 0: break
        node = nxt
    return items

def snap_state(proc, gs):
    """Snapshot of key state fields."""
    return {
        'gs_5f0': u32(proc, gs + 0x5F0),      # last accessed entry
        'gs_970': u32(proc, gs + 0x970),
        'gs_4cc': u32(proc, gs + 0x4CC),
        'gs_59c': u32(proc, gs + 0x59C),
        'gs_5a4': u32(proc, gs + 0x5A4),
    }

def main():
    pid = find_pid('WindSlayer_patched.exe')
    if not pid:
        print('Game not running'); return
    proc = kernel32.OpenProcess(0x10 | 0x0400, False, pid)
    print(f'Attached to PID {pid}')

    gs = u32(proc, 0x70e710)
    print(f'game_state = 0x{gs:08X}')

    last_keys = set()
    last_state = {}
    iter_count = 0

    print('\nMonitoring... Ctrl+C to stop. Changes shown below:')
    print('-' * 60)

    try:
        while True:
            iter_count += 1

            # Walk list and compare keys
            head = u32(proc, gs + 0x5C0)
            items = walk_list(proc, head)
            keys = {k for (k, _, _) in items}

            added = keys - last_keys
            removed = last_keys - keys

            # Check state values
            state = snap_state(proc, gs)
            state_changes = {}
            for k, v in state.items():
                if last_state.get(k) != v:
                    state_changes[k] = (last_state.get(k, 0), v)

            # Print if anything changed
            if added or removed or state_changes or iter_count == 1:
                ts = time.strftime('%H:%M:%S')
                print(f'\n[{ts}] iter {iter_count}, {len(keys)} entries')
                if added:
                    print(f'  +added: {sorted(hex(k) for k in added)}')
                if removed:
                    print(f'  -removed: {sorted(hex(k) for k in removed)}')
                for k, (old, new) in state_changes.items():
                    print(f'  ~{k}: 0x{old:08X} -> 0x{new:08X}')

            last_keys = keys
            last_state = state
            time.sleep(1)
    except KeyboardInterrupt:
        print('\nStopped.')
    finally:
        kernel32.CloseHandle(proc)

if __name__ == '__main__':
    main()
