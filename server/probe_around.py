"""
Probe memory around known pointer locations. Called with a list of addresses
to dump 256 bytes around each.
"""
import ctypes, ctypes.wintypes as wt, struct, sys

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

pid = find_pid('WindSlayer_patched.exe')
if not pid:
    print('Game not running'); sys.exit(1)
proc = kernel32.OpenProcess(0x10|0x0400, False, pid)
print(f'PID {pid}', flush=True)

# Addresses to probe (from previous find_char.py output)
# 0x1719D208 = active drix struct (referenced from 6 places)
# References come from 0x033876A8, 0x03387C20 (promising — session/socket?)
#                   0x0673E808, 0x06AFEA4C (other heap regions)
#                   0x16FFB730 (char list table)
refs_to_probe = [
    ('drix_struct_start', 0x1719D200),  # just before the struct
    ('ref_at_033876A8', 0x033876A8),     # session candidate 1
    ('ref_at_03387C20', 0x03387C20),     # session candidate 2
    ('ref_at_0673E808', 0x0673E808),
    ('ref_at_06AFEA4C', 0x06AFEA4C),
    ('ref_at_16FFB730', 0x16FFB730),
]

for name, addr in refs_to_probe:
    # Dump 256 bytes centered on the address: -128 to +128
    start = addr - 0x80
    d = rd(proc, start, 0x100)
    if not d:
        print(f'\n{name} @ 0x{addr:08X}: read failed'); continue
    print(f'\n=== {name} @ 0x{addr:08X} (marker at offset +0x80) ===')
    for i in range(0, 0x100, 16):
        row = d[i:i+16]
        h = ' '.join(f'{b:02X}' for b in row)
        a = ''.join(chr(b) if 32<=b<127 else '.' for b in row)
        marker = '   <-- HERE' if start+i <= addr < start+i+16 else ''
        print(f'  0x{start+i:08X}: {h}  {a}{marker}')

# For the drix char struct itself, dump a LOT more to see field structure
print(f'\n=== drix char struct content (0x1719D200..0x1719D600) ===')
d = rd(proc, 0x1719D200, 0x400)
for i in range(0, 0x400, 16):
    row = d[i:i+16]
    h = ' '.join(f'{b:02X}' for b in row)
    a = ''.join(chr(b) if 32<=b<127 else '.' for b in row)
    print(f'  0x{0x1719D200+i:08X}: {h}  {a}')

kernel32.CloseHandle(proc)
