"""find_roster.py — locate the EN client's game_state object at runtime by its
structural signature, and dump the character roster list at [game_state+0x4e0].

The opcode-0x1A handler (VA 0x451D8D) creates the local player by:
  index = GetU32K()                       # 1-based index into roster
  record = list_get([game_state+0x4e0], index)   # VA 0x409770
  entity[+0x11dc] = record                 # name-string ptr (NULL -> nameplate crash)
  copy appearance/stats from record
  call 0x422120(entity, [game_state+0x4cc])  # register local player -> scene+0x970

list_get (0x409770) treats the list as: count @ +0x18, head node @ +0x10,
node.next @ [node+0], payload @ [node+8]. We replicate that walk here.

We don't know game_state's address statically (it's a dispatcher arg), so we
scan all module .data globals AND a heap window for a pointer whose target has:
  - [+0x4cc] = a valid pointer  (scene)
  - [+0x4e0 +0x18] = small count (1..16)
  - [+0x4e0 +0x10] = a valid pointer (list head)
"""
import ctypes, ctypes.wintypes as wt, struct, sys

k32 = ctypes.windll.kernel32


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [('BaseAddress', ctypes.c_void_p), ('AllocationBase', ctypes.c_void_p),
                ('AllocationProtect', wt.DWORD), ('__align', wt.DWORD),
                ('RegionSize', ctypes.c_size_t),
                ('State', wt.DWORD), ('Protect', wt.DWORD), ('Type', wt.DWORD)]


# Proper 64-bit-safe prototypes (otherwise ctypes truncates HANDLE/pointers).
k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.ReadProcessMemory.restype = wt.BOOL
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p,
                               ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
k32.CloseHandle.argtypes = [wt.HANDLE]


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


def rd(proc, a, sz):
    if not a or sz <= 0:
        return None
    buf = (ctypes.c_ubyte * sz)()
    r = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(proc, ctypes.c_void_p(a), ctypes.byref(buf), sz, ctypes.byref(r))
    return bytes(buf) if ok and r.value == sz else None


def u32(proc, a):
    d = rd(proc, a, 4)
    return struct.unpack('<I', d)[0] if d else None


def u8(proc, a):
    d = rd(proc, a, 1)
    return d[0] if d else None


def ptr_ok(v):
    return v is not None and 0x00400000 <= v < 0x7FFF0000


def cstr(proc, a, maxlen=32):
    d = rd(proc, a, maxlen)
    if not d:
        return None
    z = d.find(b'\x00')
    raw = d[:z if z >= 0 else maxlen]
    try:
        return raw.decode('latin-1')
    except Exception:
        return repr(raw)


def list_get(proc, list_addr, index):
    """Replicate VA 0x409770: 1-based index into the list."""
    count = u32(proc, list_addr + 0x18)
    if count is None or index < 1 or index > count:
        return None, count
    node = u32(proc, list_addr + 0x10)   # head
    steps = index - 1
    cur = node
    while steps > 0:
        if not ptr_ok(cur):
            return None, count
        cur = u32(proc, cur)             # next ptr at [node+0]
        steps -= 1
    if not ptr_ok(cur):
        return None, count
    payload = u32(proc, cur + 8)
    return payload, count


def main():
    pid = find_pid('WindSlayer_patched.exe')
    if not pid:
        print('Game not running'); return
    proc = k32.OpenProcess(0x10 | 0x0400, False, pid)
    print(f'PID {pid}', flush=True)

    # gather scannable regions: module .data-ish + committed private/heap pages
    regions = []
    addr = 0x10000
    mbi = MEMORY_BASIC_INFORMATION()
    while addr < 0x7FFF0000:
        res = k32.VirtualQueryEx(proc, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not res:
            break
        base = mbi.BaseAddress or addr
        size = mbi.RegionSize or 0x1000
        # MEM_COMMIT=0x1000, readable protections
        if mbi.State == 0x1000 and mbi.Protect in (0x04, 0x02, 0x20, 0x40):
            regions.append((base, size))
        addr = base + size

    print(f'scanning {len(regions)} committed regions...', flush=True)
    candidates = []
    for base, size in regions:
        # cap region read size to avoid huge reads
        chunk = min(size, 0x400000)
        data = rd(proc, base, chunk)
        if not data:
            continue
        # scan dword-aligned values as candidate game_state pointers
        for off in range(0, len(data) - 4, 4):
            gs = struct.unpack_from('<I', data, off)[0]
            if not ptr_ok(gs):
                continue
            scene = u32(proc, gs + 0x4cc)
            if not ptr_ok(scene):
                continue
            cnt = u32(proc, gs + 0x4e0 + 0x18)
            head = u32(proc, gs + 0x4e0 + 0x10)
            if cnt is not None and 1 <= cnt <= 16 and ptr_ok(head):
                candidates.append((gs, scene, cnt, head, base + off))
        if len(candidates) > 40:
            break

    # dedup by gs
    seen = set()
    uniq = []
    for c in candidates:
        if c[0] in seen:
            continue
        seen.add(c[0]); uniq.append(c)

    print(f'found {len(uniq)} candidate game_state objects', flush=True)
    for gs, scene, cnt, head, foundat in uniq[:12]:
        print(f'\n=== game_state 0x{gs:08X} (pointer found at 0x{foundat:08X}) ===', flush=True)
        print(f'    +0x4cc scene   = 0x{scene:08X}', flush=True)
        print(f'    +0x4e0 roster  : count={cnt} head=0x{head:08X}', flush=True)
        for idx in range(1, cnt + 1):
            rec, _ = list_get(proc, gs + 0x4e0, idx)
            if not ptr_ok(rec):
                print(f'      [{idx}] -> (bad record ptr {rec})', flush=True)
                continue
            cls = u32(proc, rec + 0x644)
            name = cstr(proc, rec)        # record starts with name string (per 0x451E60 copy loop)
            print(f'      [{idx}] record=0x{rec:08X} class={cls} name="{name}"', flush=True)

    k32.CloseHandle(proc)


if __name__ == '__main__':
    main()
