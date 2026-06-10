"""Runtime PROOF: make TestHero visible + controllable by satisfying both gates:
  (1) body-sprite: set entity+0xe60 to a valid anim index present in the anim
      container ([game_state+0x4e0], count 180). Use 75 (same as the NPC that
      already passes animlookup) to prove the sprite draws.
  (2) registration/camera: set scene+0x970 = scene+0xee8 = entity, and make the
      uid gate consistent (entity+0x84 = scene+0x220). Mirror the screen-coord
      init that 0x422120 normally does.
Usage: force_visible.py [anim_index]   (default 75)
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


SCENE_PTR = 0x70EECC


def main():
    anim_idx = int(sys.argv[1]) if len(sys.argv) > 1 else 75
    pid = find_pid('WindSlayer_patched.exe')
    proc = k32.OpenProcess(0x10 | 0x20 | 0x08 | 0x0400, False, pid)
    print(f'PID {pid}  anim_idx={anim_idx}')

    def rd(a, n):
        b = (ctypes.c_ubyte * n)(); r = ctypes.c_size_t(0)
        k32.ReadProcessMemory(proc, ctypes.c_void_p(a), ctypes.byref(b), n, ctypes.byref(r))
        return bytes(b[:r.value]) if r.value else None
    def u32(a):
        d = rd(a, 4); return struct.unpack('<I', d)[0] if d else None
    def u8(a):
        d = rd(a, 1); return d[0] if d else None
    def f64(a):
        d = rd(a, 8); return struct.unpack('<d', d)[0] if d else 0.0
    def cstr(a, n=18):
        d = rd(a, n); z = d.find(b'\x00') if d else -1
        return (d[:z if z >= 0 else n].decode('latin-1', 'replace')) if d else '?'
    def wr(a, payload):
        r = ctypes.c_size_t(0)
        buf = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
        return k32.WriteProcessMemory(proc, ctypes.c_void_p(a), ctypes.byref(buf), len(payload), ctypes.byref(r)) and r.value == len(payload)
    def w32(a, v): return wr(a, struct.pack('<I', v & 0xFFFFFFFF))
    def w8(a, v): return wr(a, bytes([v & 0xFF]))

    scene = u32(SCENE_PTR)
    if not scene:
        print('scene NULL - not in world'); return
    s220 = u32(scene + 0x220)
    print(f'scene=0x{scene:08X} scene+0x220(uid)={s220}')

    # find TestHero in scene+0xc list
    E = None
    node = u32(scene + 0xc)
    seen = 0
    while node and 0x400000 <= node < 0x7FFF0000 and seen < 40:
        ent = u32(node + 8)
        if ent and 0x400000 <= ent < 0x7FFF0000 and cstr(ent).startswith('TestHero'):
            E = ent; break
        node = u32(node); seen += 1
    if not E:
        print('TestHero entity not found in scene list'); return
    px, py = f64(E + 0x11f8), f64(E + 0x1288)
    print(f'entity E=0x{E:08X}  pos=({px:.0f},{py:.0f})  before: alive={u8(E+0x98)} +0xe60={u32(E+0xe60)} +0x84={u32(E+0x84)} +0x15b4={u32(E+0x15b4)}')

    # (1) body sprite: valid anim index
    w32(E + 0xe60, anim_idx)
    # alive + safe nameplate
    w8(E + 0x98, 4)
    w32(E + 0x11dc, E)
    # uid gate consistency
    w32(E + 0x84, s220 if s220 is not None else 1)
    # (2) registration: local-player ptr + camera target
    w32(scene + 0x970, E)
    w32(scene + 0xee8, E)
    # mirror screen-coord init that 0x422120 does
    ix, iy = int(round(px)), int(round(py))
    w32(E + 0x15a4, ix); w32(E + 0x1598, ix)
    w32(E + 0x15a8, iy); w32(E + 0x159c, iy)
    w8(E + 0x15b0, 2); w32(E + 0x15b4, 8)

    print(f'AFTER: alive={u8(E+0x98)} +0xe60={u32(E+0xe60)} +0x84=0x{u32(E+0x84):08X} '
          f'scene+0x970=0x{u32(scene+0x970):08X} scene+0xee8=0x{u32(scene+0xee8):08X} +0x15b4={u32(E+0x15b4)}')
    print('Done. Check the game window.')
    k32.CloseHandle(proc)


if __name__ == '__main__':
    main()
