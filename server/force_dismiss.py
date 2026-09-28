"""
Force-dismiss the "Waiting for server response" dialog by directly manipulating
the client's dialog manager (entry 0x16) sub-list.

Approach: scan entry 0x16's memory for the string pointer 0x006F6C34 ("Waiting...")
and neutralize that dialog instance.
"""
import ctypes, ctypes.wintypes as wt, struct, time, sys

kernel32 = ctypes.windll.kernel32
PROCESS_ALL_ACCESS = 0x1F0FFF

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

def wr(proc, addr, data):
    buf = (ctypes.c_ubyte * len(data))(*data)
    written = ctypes.c_size_t(0)
    return kernel32.WriteProcessMemory(proc, ctypes.c_void_p(addr), ctypes.byref(buf), len(data), ctypes.byref(written))

def u32(proc, a):
    d = rd(proc, a, 4); return struct.unpack('<I', d)[0] if d else 0

def search_memory(proc, start, size, needle):
    """Search for needle in memory range."""
    chunk_size = 0x10000
    addrs = []
    pos = start
    end = start + size
    while pos < end:
        chunk = rd(proc, pos, min(chunk_size, end - pos))
        if chunk:
            base = 0
            while True:
                idx = chunk.find(needle, base)
                if idx == -1: break
                addrs.append(pos + idx)
                base = idx + 1
        pos += chunk_size - 3  # overlap for multi-byte needle
    return addrs

def main():
    pid = find_pid('WindSlayer_patched.exe')
    if not pid:
        print('Game not running'); return
    proc = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not proc:
        print(f'OpenProcess failed: {ctypes.GetLastError()}'); return

    # Find entry 0x16 in the list
    gs = u32(proc, 0x70e710)
    node = u32(proc, gs + 0x5C0)
    entry_16 = None
    while node:
        entry = u32(proc, node + 8)
        if entry and u32(proc, entry + 0) == 0x16:
            entry_16 = entry
            print(f'Entry 0x16 at 0x{entry:08X}')
            break
        nxt = u32(proc, node + 0)
        if nxt == 0 or nxt == node: break
        node = nxt

    if not entry_16:
        print('Entry 0x16 not found'); return

    # Dump entry 0x16 struct (256 bytes)
    dump = rd(proc, entry_16, 256)
    print(f'\nEntry 0x16 dump:')
    for i in range(0, 256, 16):
        row = dump[i:i+16]
        h = ' '.join(f'{b:02X}' for b in row)
        print(f'  +{i:04X}: {h}')

    # Search entire heap for references to string VA 0x006F6C34 (Waiting message)
    # Typical heap is 0x00800000 - 0x20000000
    print('\nSearching heap for "Waiting" string refs...')
    needle = struct.pack('<I', 0x006F6C34)
    refs = []
    for base in range(0x00800000, 0x20000000, 0x100000):
        hits = search_memory(proc, base, 0x100000, needle)
        refs.extend(hits)
        if refs and len(refs) > 5: break
    print(f'Found {len(refs)} references to "Waiting" string')
    for r in refs[:20]:
        print(f'  0x{r:08X}')

    # For each ref, try to find the dialog structure containing it
    # Dialogs typically have: [base]=vtable, [base+0x04..0x20]=fields, [+X]=string_ptr
    # If we can find the dialog's "active" flag, we can zero it.

    # Simpler approach: scan for the dialog queue itself - look for back-to-back entries
    # with string pointer as first DWORD.
    print('\nRefs near entry 0x16 address:')
    for r in refs:
        if abs(r - entry_16) < 0x100000:  # within 1MB of entry 0x16
            # Read surrounding bytes
            ctx = rd(proc, r - 16, 48)
            if ctx:
                print(f'  0x{r:08X}: ctx={ctx.hex()}')

    kernel32.CloseHandle(proc)

if __name__ == '__main__':
    main()
