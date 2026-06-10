#!/usr/bin/env python3
"""
wsre.py — WindSlayer Reverse-Engineering toolkit (one tool, start to finished)
=============================================================================
Consolidates the project's scattered RE scripts into a single CLI that takes the
EN client from raw binary -> full annotated protocol map, and inspects the live
client at runtime.

STATIC (pefile + capstone, against the .exe):
  opcodes                 list every opcode -> handler VA (+ known name/dir)
  spec <op>               trace ONE opcode handler's Get* wire-field sequence
  spec --all [-o f.json]  trace ALL opcode handlers -> protocol_map.json (the
                          "RE the whole protocol" command)
  disasm <start> <end>    linear disassembly of a VA range (annotates Get* calls)
  callers <va>            find E8 call sites + immediate refs to a VA
  writers <off>           find readers/writers of an entity offset (e.g. 0x84)
  status                  RE/implementation coverage vs the server's _dispatch

RUNTIME (ctypes ReadProcessMemory; client must be NON-ELEVATED):
  scan                    game_state/scene/anim-container + entity-list gate dump
  entity <name|0xADDR>    dump an entity struct's key fields
  container               enumerate the anim/record container (npccodes -> names)
  find <text>             search client heap for a string (+ who references it)
  inworld                 list all client PIDs and whether each is in-world

Examples:
  python wsre.py opcodes
  python wsre.py spec 0x07
  python wsre.py spec --all -o protocol_map.json
  python wsre.py scan
  python wsre.py entity TestHero
"""
import argparse, os, struct, sys, json

# ------------------------------------------------------------------ config ---
HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.environ.get('WS_BIN') or next(
    (p for p in (os.path.join(HERE, '..', 'WindSlayer_patched.exe'),
                 r'C:\Users\ohdri\Desktop\WindSlayer2Game\WindSlayer_patched.exe') if os.path.exists(p)),
    os.path.join(HERE, '..', 'WindSlayer_patched.exe'))
IMAGE_BASE = 0x400000
INDEX_TABLE, HANDLER_TABLE = 0x4584B0, 0x458368
N_HANDLERS, N_OPCODES = 82, 167
DISPATCHER = 0x44D640

# game_state is a STATIC global; scene/anim are pointer fields off it (no ASLR).
GAME_STATE = 0x70EA00
SCENE_PTR = 0x70EECC        # game_state+0x4cc
ANIM_PTR = 0x70EEE0         # game_state+0x4e0
GS_FLAG = 0x70EE08          # game_state+0x408

IAT = {
    0x4a4090: 'GetU16', 0x4a4094: 'GetU8', 0x4a4098: 'GetStr',
    0x4a40c8: 'GetU32', 0x4a40cc: 'GetU32K', 0x4a40d0: 'GetBool1',
    0x4a40d4: 'GetI32', 0x4a40b0: 'GetF64',
}

# Accumulated opcode knowledge (name | direction). dir: in=client->server,
# out=server->client, both. Extend as RE proceeds.
OPCODES = {
    0x01: ('Login', 'both'), 0x02: ('LoginResult/CharList', 'out'),
    0x03: ('InGameState / Chat(in)', 'both'), 0x04: ('SetStats(in)/Spawn', 'both'),
    0x05: ('Heartbeat(in)/InitAlt', 'in'), 0x07: ('PlayerSpawn', 'out'),
    0x08: ('ChangeMap', 'out'), 0x0A: ('Chat/Welcome', 'out'),
    0x0B: ('BuyItem', 'in'), 0x0C: ('SellItem', 'in'), 0x0D: ('Move', 'in'),
    0x0E: ('CreateCharacter', 'in'), 0x0F: ('EquipItem', 'in'),
    0x14: ('UpdateStats(entity HP)', 'out'), 0x15: ('UseItemOrSkill', 'in'),
    0x16: ('AcceptQuest(in)/ChatBroadcast(out)', 'both'), 0x18: ('GetItem/Drop', 'out'),
    0x19: ('LostItem/Sell', 'out'), 0x1A: ('MobQuery(in)/Spawn(out)', 'both'),
    0x21: ('ExpDelta', 'out'), 0x25: ('UseSkill/Attack', 'in'),
    0x26: ('QuestGrant', 'out'), 0x27: ('QuestComplete', 'out'),
    0x28: ('SetHP(local)', 'out'), 0x29: ('Death', 'out'),
    0x2B: ('EnterWorld', 'in'), 0x2C: ('RoomQuery', 'in'), 0x2D: ('SceneFinalize', 'in'),
    0x2F: ('ArenaQuery', 'in'), 0x38: ('QuestAbandon', 'out'),
    0x44: ('SetMP(local)', 'out'), 0x55: ('QuestProgressDisplay', 'out'),
    0x57: ('QuestLogAdd/Reward', 'out'), 0x59: ('QuestProgress', 'out'),
    0x63: ('PostSpawnAck', 'both'), 0x7E: ('ChangeMapPortal', 'in'),
    0x80: ('CashBalance', 'out'),
}

# Entity struct offsets (0x15F0 bytes).
ENT = {0x00: 'name[17]', 0x84: 'uid', 0x98: 'alive', 0x9c: 'field9c', 0x110: 'class',
       0xe60: 'animIdx', 0x11dc: 'nameptr', 0x11f8: 'posX(f64)', 0x1288: 'posY(f64)',
       0x15b4: 'state15b4'}


# ----------------------------------------------------------------- static ---
def _load_pe():
    import pefile
    pe = pefile.PE(BIN)
    return pe, pe.get_memory_mapped_image()


def _u32(data, va):
    return struct.unpack_from('<I', data, va - IMAGE_BASE)[0]


def _u8(data, va):
    return data[va - IMAGE_BASE]


def opcode_handler_map(data):
    """opcode (1..0xA7) -> handler VA, via dispatch tables."""
    handlers = [_u32(data, HANDLER_TABLE + 4 * i) for i in range(N_HANDLERS)]
    m = {}
    for i in range(N_OPCODES):
        slot = _u8(data, INDEX_TABLE + i)
        if slot < N_HANDLERS:
            m[i + 1] = handlers[slot]
    return m


def _cs():
    import capstone
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    return md


def cmd_opcodes(args):
    pe, data = _load_pe()
    m = opcode_handler_map(data)
    print(f'{"op":>4} {"handler":>10}  dir   name')
    for op in sorted(m):
        name, d = OPCODES.get(op, ('?', '?'))
        print(f'0x{op:02X} 0x{m[op]:08X}  {d:<4}  {name}')
    print(f'\n{len(m)} opcodes mapped (dispatcher 0x{DISPATCHER:08X}).')


def _trace_handler(data, start, end):
    """Return ordered [(va, get_name, dest_offset_str)] for a handler range."""
    md = _cs()
    md.detail = True
    code = data[start - IMAGE_BASE: end - IMAGE_BASE]
    last = {}
    seq = []
    strlen = None
    for ins in md.disasm(code, start):
        m, op = ins.mnemonic, ins.op_str
        if m == 'lea' and ', ' in op:
            reg, src = op.split(', ', 1)
            last[reg] = src
        elif m == 'push' and (op.startswith('0x') or op.isdigit()):
            try: strlen = int(op, 0)
            except ValueError: strlen = None
        elif m == 'call' and op.startswith('dword ptr [0x4a4'):
            try: a = int(op.split('[')[1].rstrip(']'), 16)
            except (IndexError, ValueError): a = 0
            if a in IAT:
                dest = next((s for s in last.values() if 'esi' in s), None) or \
                       next((s for s in last.values() if 'esp' in s), None)
                extra = f' len={strlen}' if 'Str' in IAT[a] else ''
                seq.append((ins.address, IAT[a], (dest or '?') + extra))
                last.clear()
        if m == 'ret':
            break
    return seq


def cmd_spec(args):
    pe, data = _load_pe()
    m = opcode_handler_map(data)
    if args.all:
        out = {}
        for op in sorted(m):
            start = m[op]
            seq = _trace_handler(data, start, start + 0xA00)
            name, d = OPCODES.get(op, ('?', '?'))
            out[f'0x{op:02X}'] = {
                'handler': f'0x{start:08X}', 'name': name, 'dir': d,
                'fields': [{'va': f'0x{a:08X}', 'get': g, 'dest': dst} for a, g, dst in seq],
            }
        path = args.o or 'protocol_map.json'
        json.dump(out, open(path, 'w'), indent=2)
        print(f'wrote {path}: {len(out)} opcode handlers traced.')
        return
    op = int(args.opcode, 16) if isinstance(args.opcode, str) else args.opcode
    if op not in m:
        print(f'opcode 0x{op:02X} has no handler'); return
    start = m[op]
    name, d = OPCODES.get(op, ('?', '?'))
    print(f'opcode 0x{op:02X} ({name}, {d}) -> handler 0x{start:08X}')
    for a, g, dst in _trace_handler(data, start, start + 0xA00):
        print(f'  0x{a:08X}  {g:10s} -> {dst}')


def cmd_disasm(args):
    pe, data = _load_pe()
    md = _cs()
    start, end = int(args.start, 16), int(args.end, 16)
    for ins in md.disasm(data[start - IMAGE_BASE: end - IMAGE_BASE], start):
        note = ''
        if 'call' in ins.mnemonic and '0x4a4' in ins.op_str:
            try:
                a = int(ins.op_str.split('[')[1].rstrip(']'), 16)
                if a in IAT: note = f'   ; {IAT[a]}'
            except (IndexError, ValueError): pass
        print(f'0x{ins.address:08X}: {ins.mnemonic:9s} {ins.op_str}{note}')


def cmd_callers(args):
    pe, data = _load_pe()
    txt = next(s for s in pe.sections if s.Name.startswith(b'.text'))
    tcode = data[txt.VirtualAddress: txt.VirtualAddress + txt.Misc_VirtualSize]
    target = int(args.va, 16)
    print(f'=== E8 call sites to 0x{target:08X} ===')
    for off in range(len(tcode) - 5):
        if tcode[off] == 0xE8:
            rel = struct.unpack_from('<i', tcode, off + 1)[0]
            cva = IMAGE_BASE + txt.VirtualAddress + off
            if cva + 5 + rel == target:
                print(f'  call @ 0x{cva:08X}')
    needle = struct.pack('<I', target)
    i = 0
    while True:
        i = data.find(needle, i)
        if i < 0: break
        print(f'  imm ref @ 0x{IMAGE_BASE + i:08X}')
        i += 1


def cmd_writers(args):
    pe, data = _load_pe()
    txt = next(s for s in pe.sections if s.Name.startswith(b'.text'))
    start = IMAGE_BASE + txt.VirtualAddress
    code = data[txt.VirtualAddress: txt.VirtualAddress + txt.Misc_VirtualSize]
    md = _cs()
    off = int(args.offset, 16)
    pat = struct.pack('<i', off)
    hits, i = [], 0
    while True:
        i = code.find(pat, i)
        if i < 0: break
        hits.append(i); i += 1
    print(f'offset 0x{off:X}: {len(hits)} disp hits')
    for h in hits:
        for back in range(1, 10):
            o = h - back
            if o < 0: continue
            ins = next(md.disasm(code[o:o + 16], start + o), None)
            if ins and f'0x{off:x}' in ins.op_str and ins.size > back:
                first = ins.op_str.split(',')[0].strip()
                kind = 'WRITE' if ('ptr [' in first and f'0x{off:x}]' in first) else 'read'
                print(f'  0x{ins.address:08X} [{kind}] {ins.mnemonic} {ins.op_str}')
                break


def cmd_status(args):
    pe, data = _load_pe()
    m = opcode_handler_map(data)
    srv = os.path.join(HERE, 'windslayer_server.py')
    handled = set()
    if os.path.exists(srv):
        import re
        for mm in re.finditer(r'opcode == (0x[0-9A-Fa-f]+)', open(srv, encoding='utf-8').read()):
            handled.add(int(mm.group(1), 16))
    named = sum(1 for op in m if op in OPCODES)
    print(f'opcodes mapped: {len(m)} | named: {named} | server inbound-handled: {len(handled)}')
    print('inbound handled:', ', '.join(f'0x{o:02X}' for o in sorted(handled)))
    print('named but NOT inbound-handled:',
          ', '.join(f'0x{o:02X}' for o in sorted(set(OPCODES) - handled)))


# ---------------------------------------------------------------- runtime ---
def _rt():
    import ctypes, ctypes.wintypes as wt
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = wt.HANDLE
    k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k.ReadProcessMemory.restype = wt.BOOL
    k.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]

    class PE32(ctypes.Structure):
        _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ProcessID', wt.DWORD),
                    ('th32DefaultHeapID', ctypes.c_void_p), ('th32ModuleID', wt.DWORD),
                    ('cntThreads', wt.DWORD), ('th32ParentProcessID', wt.DWORD),
                    ('pcPriClassBase', wt.LONG), ('dwFlags', wt.DWORD), ('szExeFile', wt.WCHAR * 260)]

    def pids(name='WindSlayer_patched.exe'):
        out = []
        snap = k.CreateToolhelp32Snapshot(2, 0)
        e = PE32(); e.dwSize = ctypes.sizeof(e)
        if not k.Process32FirstW(snap, ctypes.byref(e)):
            return out
        while True:
            if e.szExeFile.lower() == name.lower():
                out.append(e.th32ProcessID)
            if not k.Process32NextW(snap, ctypes.byref(e)):
                return out

    return ctypes, k, pids


def _open_inworld(ctypes, k, pids):
    """Return (proc, pid) for the first in-world client (scene != NULL)."""
    for pid in pids():
        proc = k.OpenProcess(0x10 | 0x400, False, pid)
        if not proc:
            continue
        buf = (ctypes.c_ubyte * 4)(); r = ctypes.c_size_t(0)
        if k.ReadProcessMemory(proc, ctypes.c_void_p(SCENE_PTR), buf, 4, ctypes.byref(r)) and r.value == 4:
            if struct.unpack('<I', bytes(buf))[0]:
                return proc, pid
    # fall back to any openable
    for pid in pids():
        proc = k.OpenProcess(0x10 | 0x400, False, pid)
        if proc:
            return proc, pid
    return None, None


def _reader(ctypes, k, proc):
    def rd(a, n):
        b = (ctypes.c_ubyte * n)(); r = ctypes.c_size_t(0)
        ok = k.ReadProcessMemory(proc, ctypes.c_void_p(a), b, n, ctypes.byref(r))
        return bytes(b[:r.value]) if r.value else None
    return rd


def cmd_inworld(args):
    ctypes, k, pids = _rt()
    ps = pids()
    if not ps:
        print('no WindSlayer_patched.exe running'); return
    for pid in ps:
        proc = k.OpenProcess(0x10 | 0x400, False, pid)
        if not proc:
            print(f'PID {pid}: open failed (elevated? run client NON-elevated)'); continue
        rd = _reader(ctypes, k, proc)
        d = rd(SCENE_PTR, 4)
        scene = struct.unpack('<I', d)[0] if d else 0
        print(f'PID {pid}: scene=0x{scene:08X}  {"IN-WORLD" if scene else "(login/not in world)"}')


def cmd_scan(args):
    ctypes, k, pids = _rt()
    proc, pid = _open_inworld(ctypes, k, pids)
    if not proc:
        print('no readable client (run it NON-elevated)'); return
    rd = _reader(ctypes, k, proc)
    def u32(a): d = rd(a, 4); return struct.unpack('<I', d)[0] if d else None
    def u8(a): d = rd(a, 1); return d[0] if d else None
    def f64(a): d = rd(a, 8); return struct.unpack('<d', d)[0] if d else 0.0
    def cstr(a, n=18):
        d = rd(a, n); z = d.find(b'\x00') if d else -1
        return ''.join(chr(c) if 32 <= c < 127 else '.' for c in (d[:z if z >= 0 else n] if d else b''))
    scene = u32(SCENE_PTR); anim = u32(ANIM_PTR)
    print(f'PID {pid}  game_state@0x{GAME_STATE:08X}')
    print(f'  scene          = 0x{(scene or 0):08X}')
    print(f'  anim_container = 0x{(anim or 0):08X}  count={u32(anim+0x18) if anim else None}')
    if not scene:
        print('  (not in world)'); return
    print(f'  scene +0x220 uid-gate = {u32(scene+0x220)}  +0x970 local=0x{(u32(scene+0x970) or 0):08X} '
          f'+0xf00 state={u32(scene+0xf00)} +0xf24 trans={u32(scene+0xf24)}')
    node = u32(scene + 0xc); n = 0
    print('  --- entity list (scene+0xc) ---')
    while node and 0x400000 <= node < 0x7FFF0000 and n < 40:
        ent = u32(node + 8)
        if ent and 0x400000 <= ent < 0x7FFF0000:
            print(f'    0x{ent:08X} "{cstr(ent)}" alive={u8(ent+0x98)} uid=0x{(u32(ent+0x84) or 0):08X} '
                  f'animIdx={u32(ent+0xe60)} pos=({f64(ent+0x11f8):.0f},{f64(ent+0x1288):.0f})')
        node = u32(node); n += 1


def cmd_entity(args):
    ctypes, k, pids = _rt()
    proc, pid = _open_inworld(ctypes, k, pids)
    if not proc:
        print('no readable client (run NON-elevated)'); return
    rd = _reader(ctypes, k, proc)
    def u32(a): d = rd(a, 4); return struct.unpack('<I', d)[0] if d else None
    def u8(a): d = rd(a, 1); return d[0] if d else None
    def f64(a): d = rd(a, 8); return struct.unpack('<d', d)[0] if d else 0.0
    def cstr(a, n=18):
        d = rd(a, n); z = d.find(b'\x00') if d else -1
        return ''.join(chr(c) if 32 <= c < 127 else '.' for c in (d[:z if z >= 0 else n] if d else b''))
    target = args.who
    base = None
    if target.startswith('0x'):
        base = int(target, 16)
    else:
        scene = u32(SCENE_PTR); node = u32(scene + 0xc) if scene else 0; n = 0
        while node and 0x400000 <= node < 0x7FFF0000 and n < 60:
            ent = u32(node + 8)
            if ent and cstr(ent).startswith(target):
                base = ent; break
            node = u32(node); n += 1
    if not base:
        print(f'entity "{target}" not found'); return
    print(f'entity 0x{base:08X}')
    for off, label in sorted(ENT.items()):
        if 'f64' in label:
            print(f'  +0x{off:04X} {label:14s} = {f64(base+off):.2f}')
        elif 'name' in label:
            print(f'  +0x{off:04X} {label:14s} = "{cstr(base+off)}"')
        else:
            v = u8(base+off) if off in (0x98,) else u32(base+off)
            print(f'  +0x{off:04X} {label:14s} = {v} (0x{(v or 0):X})')


def cmd_container(args):
    ctypes, k, pids = _rt()
    proc, pid = _open_inworld(ctypes, k, pids)
    if not proc:
        print('no readable client (run NON-elevated)'); return
    rd = _reader(ctypes, k, proc)
    def u32(a): d = rd(a, 4); return struct.unpack('<I', d)[0] if d else None
    def cstr(a, n=20):
        d = rd(a, n); z = d.find(b'\x00') if d else -1
        return ''.join(chr(c) if 32 <= c < 127 else '.' for c in (d[:z if z >= 0 else n] if d else b''))
    cont = u32(ANIM_PTR)
    if not cont:
        print('container NULL (not in world)'); return
    cnt = u32(cont + 0x18); node = u32(cont + 0x10); i = 0
    print(f'container 0x{cont:08X} count={cnt}')
    while node and 0x400000 <= node < 0x7FFF0000 and i < (cnt or 0) + 2:
        i += 1
        rec = u32(node + 8)
        if rec and 0x400000 <= rec < 0x7FFF0000:
            nm = cstr(rec)
            if nm and any(c.isalpha() for c in nm):
                print(f'  [{i}] rec=0x{rec:08X} class={u32(rec+0x644)} name="{nm}"')
        node = u32(node)


def cmd_find(args):
    ctypes, k, pids = _rt()
    import ctypes.wintypes as wt

    class MBI(ctypes.Structure):
        _fields_ = [('BaseAddress', ctypes.c_void_p), ('AllocationBase', ctypes.c_void_p),
                    ('AllocationProtect', wt.DWORD), ('__a', wt.DWORD), ('RegionSize', ctypes.c_size_t),
                    ('State', wt.DWORD), ('Protect', wt.DWORD), ('Type', wt.DWORD)]
    k.VirtualQueryEx.restype = ctypes.c_size_t
    k.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
    proc, pid = _open_inworld(ctypes, k, pids)
    if not proc:
        print('no readable client (run NON-elevated)'); return
    rd = _reader(ctypes, k, proc)
    needle = args.text.encode('latin-1')
    addr, mbi, hits = 0x10000, MBI(), []
    while addr < 0x7FFF0000:
        if not k.VirtualQueryEx(proc, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        b = mbi.BaseAddress or addr; sz = mbi.RegionSize or 0x1000
        if mbi.State == 0x1000 and mbi.Type == 0x20000 and mbi.Protect in (0x04, 0x40):
            data = rd(b, min(sz, 0x2000000))
            if data:
                s = 0
                while True:
                    j = data.find(needle, s)
                    if j < 0: break
                    hits.append(b + j); s = j + 1
        addr = b + sz
    print(f'PID {pid}: {len(hits)} hits for {needle!r}')
    for h in hits[:30]:
        print(f'  0x{h:08X}')


# -------------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser(description='WindSlayer RE toolkit')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('opcodes')
    sp = sub.add_parser('spec'); sp.add_argument('opcode', nargs='?'); sp.add_argument('--all', action='store_true'); sp.add_argument('-o')
    sp = sub.add_parser('disasm'); sp.add_argument('start'); sp.add_argument('end')
    sp = sub.add_parser('callers'); sp.add_argument('va')
    sp = sub.add_parser('writers'); sp.add_argument('offset')
    sub.add_parser('status')
    sub.add_parser('scan')
    sp = sub.add_parser('entity'); sp.add_argument('who')
    sub.add_parser('container')
    sp = sub.add_parser('find'); sp.add_argument('text')
    sub.add_parser('inworld')
    args = ap.parse_args()
    {
        'opcodes': cmd_opcodes, 'spec': cmd_spec, 'disasm': cmd_disasm,
        'callers': cmd_callers, 'writers': cmd_writers, 'status': cmd_status,
        'scan': cmd_scan, 'entity': cmd_entity, 'container': cmd_container,
        'find': cmd_find, 'inworld': cmd_inworld,
    }[args.cmd](args)


if __name__ == '__main__':
    main()
