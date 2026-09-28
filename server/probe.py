"""
Read-only memory probe. Dumps game state to a timestamped file so runs can
be diffed. Usage: python probe.py <label>
  label: short name for this capture (e.g. "hud_stuck", "crash_dialog", "frozen")
"""
import ctypes, ctypes.wintypes as wt, struct, sys, os, time

kernel32 = ctypes.windll.kernel32

class PE32(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD),('cntUsage', wt.DWORD),('th32ProcessID', wt.DWORD),
                ('th32DefaultHeapID', ctypes.c_void_p),('th32ModuleID', wt.DWORD),
                ('cntThreads', wt.DWORD),('th32ParentProcessID', wt.DWORD),
                ('pcPriClassBase', wt.LONG),('dwFlags', wt.DWORD),('szExeFile', wt.WCHAR * 260)]

def find_pid(name):
    snap = kernel32.CreateToolhelp32Snapshot(2, 0)
    e = PE32(); e.dwSize = ctypes.sizeof(e)
    kernel32.Process32FirstW(snap, ctypes.byref(e))
    while True:
        if e.szExeFile.lower() == name.lower(): return e.th32ProcessID
        if not kernel32.Process32NextW(snap, ctypes.byref(e)): return None

def rd(proc, a, sz):
    buf = (ctypes.c_ubyte*sz)(); r = ctypes.c_size_t(0)
    kernel32.ReadProcessMemory(proc, ctypes.c_void_p(a), ctypes.byref(buf), sz, ctypes.byref(r))
    return bytes(buf[:r.value])

def u32(proc, a):
    d = rd(proc, a, 4)
    return struct.unpack('<I', d)[0] if len(d) == 4 else 0

label = sys.argv[1] if len(sys.argv) > 1 else 'run'
ts = time.strftime('%H%M%S')
outfile = f'probe_{ts}_{label}.txt'

pid = find_pid('WindSlayer_patched.exe')
if not pid:
    print('Game not running'); sys.exit(1)
proc = kernel32.OpenProcess(0x10|0x0400, False, pid)

lines = []
def p(s): lines.append(s); print(s, flush=True)

p(f'=== probe "{label}" at {time.strftime("%H:%M:%S")} PID {pid} ===')

# Dump a row of 64 bytes at 0x70E700 (globals area)
p('\n[globals 0x70E6F0..0x70E720]')
d = rd(proc, 0x70E6F0, 0x40)
for i in range(0, 0x40, 16):
    row = d[i:i+16]
    h = ' '.join(f'{b:02X}' for b in row)
    a = ''.join(chr(b) if 32<=b<127 else '.' for b in row)
    p(f'  0x{0x70E6F0+i:08X}: {h}  {a}')

gs = u32(proc, 0x70E710)
p(f'\ngame_state = 0x{gs:08X}')

if gs:
    # Dump first 0x100 bytes of game_state
    p('\n[game_state first 0x100 bytes]')
    d = rd(proc, gs, 0x100)
    for i in range(0, 0x100, 16):
        row = d[i:i+16]
        h = ' '.join(f'{b:02X}' for b in row)
        a = ''.join(chr(b) if 32<=b<127 else '.' for b in row)
        p(f'  +{i:03X}: {h}  {a}')

    # Dump 0x4C0..0x600 (where char/socket fields should be)
    p('\n[game_state +0x4C0..+0x600]')
    d = rd(proc, gs + 0x4C0, 0x140)
    for i in range(0, 0x140, 16):
        row = d[i:i+16]
        h = ' '.join(f'{b:02X}' for b in row)
        a = ''.join(chr(b) if 32<=b<127 else '.' for b in row)
        p(f'  +{0x4C0+i:03X}: {h}  {a}')

    # Dump 0x900..0x1000 (char+0x970 area)
    p('\n[game_state +0x900..+0xA00]')
    d = rd(proc, gs + 0x900, 0x100)
    for i in range(0, 0x100, 16):
        row = d[i:i+16]
        h = ' '.join(f'{b:02X}' for b in row)
        a = ''.join(chr(b) if 32<=b<127 else '.' for b in row)
        p(f'  +{0x900+i:03X}: {h}  {a}')

    # List walk [gs+0x5C0] — count + first few + LAST entry
    p('\n[list walk at gs+0x5C0]')
    head = u32(proc, gs + 0x5C0)
    seen = set()
    keys = []
    node = head
    while node and node not in seen and len(keys) < 1000:
        seen.add(node)
        entry = u32(proc, node + 8)
        key = u32(proc, entry) if entry else 0
        keys.append((key, entry, node))
        nxt = u32(proc, node + 0)
        if nxt == 0: break
        node = nxt
    p(f'  total entries: {len(keys)}')
    if keys:
        p(f'  first key: 0x{keys[0][0]:X} at node 0x{keys[0][2]:08X}')
        p(f'  last key:  0x{keys[-1][0]:X} at node 0x{keys[-1][2]:08X}')
    # Count keys by range
    lo = sum(1 for k,_,_ in keys if k < 0x100)
    hi = sum(1 for k,_,_ in keys if 0x100 <= k < 0x300)
    xhi = sum(1 for k,_,_ in keys if k >= 0x300)
    p(f'  keys < 0x100: {lo}  |  0x100-0x2FF: {hi}  |  >= 0x300: {xhi}')
    # High-value keys (potentially interesting entries)
    big_keys = sorted(set(k for k,_,_ in keys if k >= 0x200))[:30]
    p(f'  keys >= 0x200: {[hex(k) for k in big_keys]}')

    # Dump the last-accessed entry (gs+0x5F0)
    last_entry = u32(proc, gs + 0x5F0)
    p(f'\n[last_accessed entry 0x{last_entry:08X}]')
    if last_entry:
        d = rd(proc, last_entry, 128)
        for i in range(0, 128, 16):
            row = d[i:i+16]
            h = ' '.join(f'{b:02X}' for b in row)
            a = ''.join(chr(b) if 32<=b<127 else '.' for b in row)
            p(f'  +{i:03X}: {h}  {a}')

kernel32.CloseHandle(proc)

# Write out
outpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), outfile)
with open(outpath, 'w') as f:
    f.write('\n'.join(lines))
print(f'\nWrote {outpath}', flush=True)
