"""Complete runtime gate diagnostic for the EN client, using the verified static
global game_state @ 0x70ea00.

  scene          = *(0x70eecc)        (game_state+0x4cc)
  anim_container = *(0x70eee0)        (game_state+0x4e0)   <- body-render anim-set list

Body-sprite render gates (fn 0x431960):  scene+0xf00 in {4,5,6}; per entity in
the scene+0xc list: alive(+0x98)>=3; 0x4097b0(anim_container, entity+0xe60) != NULL
(list: count@+0x18, head@+0x10, node.next@[node], payload@[node+8]); entity+0x15b4 != 0x16.

Camera/control gate (fn 0x422120):  alive==4; scene+0xf24==0; entity+0x84==scene+0x220;
then scene+0x970 = scene+0xee8 = entity.
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


GS = 0x70EA00
SCENE_PTR = 0x70EECC
ANIM_PTR = 0x70EEE0
GS_FLAG = 0x70EE08


def main():
    pid = find_pid('WindSlayer_patched.exe')
    if not pid:
        print('Game not running'); return
    proc = k32.OpenProcess(0x10 | 0x0400, False, pid)
    print(f'PID {pid}')

    def rd(a, n):
        buf = (ctypes.c_ubyte * n)()
        r = ctypes.c_size_t(0)
        k32.ReadProcessMemory(proc, ctypes.c_void_p(a), ctypes.byref(buf), n, ctypes.byref(r))
        return bytes(buf[:r.value]) if r.value else None

    def u32(a):
        d = rd(a, 4); return struct.unpack('<I', d)[0] if d else None
    def u8(a):
        d = rd(a, 1); return d[0] if d else None
    def f64(a):
        d = rd(a, 8); return struct.unpack('<d', d)[0] if d else None
    def cstr(a, n=18):
        d = rd(a, n)
        if not d: return '?'
        z = d.find(b'\x00')
        return ''.join(chr(c) if 32 <= c < 127 else '.' for c in d[:z if z >= 0 else n])

    gsflag = u32(GS_FLAG)
    scene = u32(SCENE_PTR)
    anim = u32(ANIM_PTR)
    print(f'game_state @ 0x{GS:08X}  +0x408 flag={gsflag}')
    print(f'scene          = 0x{(scene or 0):08X}   (NULL => not in world)')
    print(f'anim_container = 0x{(anim or 0):08X}')
    if not scene:
        print('Not in world (scene NULL). Enter the world first.'); k32.CloseHandle(proc); return

    print(f'\n--- SCENE fields ---')
    print(f'  +0x0C entity-list head = 0x{(u32(scene+0xc) or 0):08X}')
    print(f'  +0x220 uid-gate value  = {u32(scene+0x220)}')
    print(f'  +0x970 local-player    = 0x{(u32(scene+0x970) or 0):08X}')
    print(f'  +0xEE8 camera-target   = 0x{(u32(scene+0xee8) or 0):08X}')
    print(f'  +0xF24 transition-flag = {u32(scene+0xf24)}   (must be 0 for registration)')
    print(f'  +0xF00 scene-state     = {u32(scene+0xf00)}   (must be 4/5/6 for body renderer)')

    if anim:
        print(f'\n--- ANIM container @0x{anim:08X} ---')
        print(f'  +0x18 count = {u32(anim+0x18)}   (0 => NO entity body renders)')
        print(f'  +0x10 head  = 0x{(u32(anim+0x10) or 0):08X}')

    print(f'\n--- ENTITY LIST (scene+0xC) ---')
    head = u32(scene + 0xc)
    node = head
    seen = 0
    while node and 0x400000 <= node < 0x7FFF0000 and seen < 40:
        ent = u32(node + 8)
        nxt = u32(node)
        if ent and 0x400000 <= ent < 0x7FFF0000:
            nm = cstr(ent)
            alive = u8(ent + 0x98)
            uid = u32(ent + 0x84)
            e60 = u32(ent + 0xe60)
            s15b4 = u32(ent + 0x15b4)
            px = f64(ent + 0x11f8); py = f64(ent + 0x1288)
            # would the anim lookup succeed? replicate 0x4097b0
            animhit = '?'
            if anim:
                cnt = u32(anim + 0x18)
                animhit = 'YES' if (cnt and e60 is not None and 1 <= e60 <= cnt) else 'NO(idx not in list)'
            print(f'  ent=0x{ent:08X} name="{nm}" alive={alive} uid=0x{(uid or 0):08X} '
                  f'+0xe60(animidx)={e60} +0x15b4={s15b4} pos=({px:.0f},{py:.0f}) animlookup={animhit}')
        node = nxt
        seen += 1
    if seen == 0:
        print('  (entity list empty or unreadable)')
    print(f'\n(walked {seen} list nodes)')
    k32.CloseHandle(proc)


if __name__ == '__main__':
    main()
