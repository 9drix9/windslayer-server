#!/usr/bin/env python3
"""
patch_2009.py - build WindSlayer_patched.exe from the EN 2009 client (Outspark v1.04, Build 14)
============================================================================================
Applies, to a copy of the pristine WindSlayer.exe (every original byte is checked first):
  - X-Trap: the four API entry points 0x49F420 (start) / 0x49F440 (keepalive) /
    0x49F460 / 0x49F480 (steps) become RET. XTrapVa.dll is not shipped; the server must
    never send S2C 0xC5 (the X-Trap challenge) to a patched client.
  - Version server address: the 16-byte slot 0x52DD54 (pushed at 0x4409C8); 16..52-char
    host names go into the dead Yahoo URL slot 0x52DFD7 and the push is repointed. The
    P2P loopback '127.0.0.1' at 0x527E30 is never touched.
  - No-response timer (WM_TIMER 2 -> 'No response from the server.' -> back to select):
    jump-table entry 0x43F0FC 0x43EB99 -> 0x43ED9C (the default case), as in the 2008 exe.
  - Post-login UDP LAN probe (broadcast to 255.255.255.255:42907): 15 bytes NOPed at
    0x452610 inside the S2C 0x02 handler, as in the 2008 exe.
  - Smooth: the frame function's LeaveCriticalSection exits 0x40E7A0 (after Present),
    0x40E732, 0x40E76B go through a cave at 0x4C6200 (LeaveCS; Sleep(1); ret 4) so
    Fireway's network thread is not starved (see WindSlayer2Game/patch_smooth.py).
  - Aggro rules of the later official client (RETAIL_COMBAT_FEEL_RE_2026-09-23 2.1/2.2; KR
    2025 0x4756FC / 0x43BAE0), both keyed on monster +0x971 "server controlled", which the
    server sets with S2C 0x2A on the first surviving hit and clears with 0x9E on give-up:
      * contact gate 0x416BF2 (52 B): a monster touches the player only while +0x971 is set,
        so un-provoked wandering mobs are harmless (no flinch, no digit, no C2S 0x0D 6);
      * no wander swing 0x419272 (1 B): an un-hit monster never rolls a swing or dash while it
        wanders (the contact gate alone left AI[0] mobs swinging at passers-by);
      * name box 0x43C3CC: the tag box is red while +0x971 is set. --name-rule picks the idle
        colour: 'level' (default) keeps Build 14's own level-gap rule - blue for L-1..L+3,
        red above, black below (the Outspark 2009 look: RETAIL_VIDEO_SURVEY_2026-09-24, e.g.
        a Lv1 Ssiyo is blue) - 41 B at 0x43C3CC + 0x43C416; 'kr' is the later KR/WS2 rule,
        black whenever idle (15 B).
  - Combo HUD v2 (--no-combo-hud to skip), rebuilt to the retail rules measured from footage
    (re_tools/docs/GRADE_WORDS_COMBO_RULES_2026-09-24.md): per-hit damage roll with the GOOD /
    BAD / CRITICAL! words, "N Combo!" per damaging hit with a 2.0 s window and a hard cut,
    orange from 20. Live-verified 2026-09-24 (GOOD, rolled digits, 2-3 Combo). See
    combo_hud_2009.py; its art is 4 extra hs\ files (combo_hud_2009.py --install-assets;
    without them a built-in fallback is drawn). v1 is kept in _backup_combo_v1\.
  - Catch-up knock fix F1 (--no-knock-fix to skip; live_harness/p8p13_live_triage.md finding 1,
    RE in re_tools/docs/CATCHUP_KNOCK_FIX_RE_2026-10-05.md). The field catch-up pass of the frame
    function (0x42CC94) calls FUN_00412c60(scene, copy) for every player copy with more than one
    queued 0x1B node. That function honours its entity parameter only in pass 1; pass 2 (the
    victims' +0x9DC reactions) walked every entity, so it consumed the local player's fresh knock
    (+0x9DC = 6 from the frame's last hit detection) before the next tick's 0x0D builder could
    report it: about 2.5 % of knocks were never sent. The 5 bytes 0x4138C6 (pass-2 list head
    load) jump to a 60-byte cave at 0x4C6F00: when param_2 != 0 and the room is the field
    (room +0x7C == 0), pass 2 walks a one-node list built in the function's own frame
    ([esp+0x18] next = NULL, [esp+0x20] = param_2), so it handles only that copy. param_2 == 0
    (the main tick) and PvP rooms (room mode 1, where the catch-up is the only tick and has no
    main tick to fall back on) run the original instructions unchanged.
  - cp-2 KR->EN item-id shift (--no-id-shift to skip; re_tools/docs/systems_2009/pet.md 1 B2,
    guild.md 1.6, CLIENT_PATCH_SET_RE_2026-10-06.md cp-2). Build 14's exe hard-codes KR item ids,
    but the EN hii inserts four event items at 4249..4252 and the client indexes items by line
    order, so every id >= 4249 is 4 lower in the exe than in the EN data. +4 on the id field of
    17 instructions (ID_SHIFT_SITES): 11 pet (Pet Bell gate 4281 -> 4285; auto-feed bag search
    and manual range, Pet Food 250/100/50/20 4282..4285 -> 4286..4289; base of the cash-use
    switch, which moves the premium billboard 4279 -> 4283, the four foods and the rename ticket
    4318 -> 4322 together) and 6 guild billboard (use 4280 -> 4284; S2C 0xBA / 0xBB sprite pick
    and 0xB3 sub 185, 4280 / 4279 -> 4284 / 4283). Server consequence: with this patch, board
    item ids in 0xBA / 0xBB / sub 185 and the pet item ids are the EN ids; without it, the KR ids.
The launcher still needs the three SSO arguments: WindSlayer_patched.exe -<a> -<b> -<c>
(play_2009.bat passes them).

usage: python patch_2009.py [address] [--src WindSlayer.exe] [--out WindSlayer_patched.exe]
                            [--no-smooth] [--no-aggro-rules] [--name-rule level|kr]
                            [--no-combo-hud] [--no-knock-fix] [--no-id-shift] [--p2]
  --p2  second client for multiplayer tests: the client's own UDP P2P port 42907 -> 42908
        (4 immediates; only one process can bind 42907), written to WindSlayer_p2.exe
        unless --out is given.
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
B = 0x400000
SLOT_VA, SLOT_SIZE = 0x52DD54, 16
LONG_VA, LONG_SIZE = 0x52DFD7, 53
PUSH_VA = 0x4409C8
LOOPBACK_VA = 0x527E30
SMOOTH_SITES = (0x40E7A0, 0x40E732, 0x40E76B)
CAVE_VA = 0x4C6200
# (va, original, new, what) - see the docstring; checked with capstone, no branch enters the
# rewritten ranges except at their starts, and nothing reads the skipped [esp+0x1b] byte.
AGGRO_RULES = (
    (0x416BF2,
     '80BD9C000000040F851F020000' '83BD000F0000030F8212020000' 'F685040F0000100F8505020000'
     '83BDE0090000000F85F8010000',
     '80BD9C000000047524' '83BD000F000003721B' 'F685040F0000107512' '83BDE0090000007509'
     '80BD71090000007505' 'E9FA010000' '9090',
     'contact gate (+0x971)'),
    # The wander branch of FUN_004185b0 copies its rolled motion +0x947 (attack A 1 / attack B 5
    # / dash 6, AI[0]/AI[8]/AI[3]) into the input +0x940 for the whole 3-6 s wander period
    # (0x41926A..0x419274), so an un-hit Monkey Soldier swings at a passer-by and his own client
    # draws the hit, digit and flinch (live 2026-09-25, G4(c3)). The contact gate above covers
    # only body contact. je -> jmp: the wander never takes the rolled motion; server commands
    # reach +0x940 through the queue consumer 0x4129F0 and are unaffected.
    (0x419272, '7406', 'EB06', 'no wander swing'),
)
# The name box: red while +0x971 is set; the idle colour per --name-rule.
NAME_RULES = {
    # KR 2025 0x4756FC: black whenever idle.
    'kr': ((0x43C3CC, '8BC280B885090000008B8888090000', '80BB71090000007547EB4A90909090',
            'red name box, kr idle'),),
    # Build 14's level colour while idle (RETAIL_COMBAT_FEEL 2.1 option B): the +0x971 test is
    # put in front of the level compare; the local level byte stays in CL instead of
    # [esp+0x1b] (read only at 0x43C416).
    'level': ((0x43C3CC,
               '8BC280B885090000008B88880900008A899D000000884C241B740A8A8A85090000884C241B',
               '80BB71090000007547' '8B8A88090000' '8A899D000000' '80BA8509000000' '7406'
               '8A8A85090000' '90',
               'red name box, level idle'),
              (0x43C416, '3844241B', '38C19090', 'name box level compare')),
}


# F1 catch-up knock fix (see the docstring and re_tools/docs/CATCHUP_KNOCK_FIX_RE_2026-10-05.md).
# Hook: FUN_00412c60 0x4138C6 'mov eax,[edi+0Ch]; cmp eax,ebx' (pass-2 list head; reached from
# 0x412C7A, 0x4138BA and the fall-through of 0x4138C0, always at body depth with EBX = 0 and
# EDI = scene; nothing branches into 0x4138C7..0x4138CA). Both cave paths rejoin at 0x4138CB
# 'mov [esp+14h],eax; jz 0x4141A2; mov edi,[esp+40h]; xor ebp,ebp; jmp 0x4138E0'.
# The cave touches only EAX, the flags and, on the one-node path, [esp+18h] / [esp+20h]: pass-2
# locals that the loop head 0x4138E0 reads once (node+0 next, node+8 entity) and writes before any
# other read. [esp+44h] (param_2) is only read before pass 2 (first write 0x413D0D), so it is intact
# at the hook. Cave in the zero .text tail past the combo HUD cave (0x4C6220..0x4C68A0), leaving that
# cave 0x660 bytes to grow; both knock ranges are in combo_hud_2009.RESERVED.
KNOCK_HOOK, KNOCK_ORIG, KNOCK_RESUME = 0x4138C6, bytes.fromhex('8B470C3BC3'), 0x4138CB
KNOCK_CAVE, KNOCK_CAVE_LEN = 0x4C6F00, 0x3C


def knock_cave_bytes():
    rel32 = lambda src, dst: struct.pack('<i', dst - (src + 5))
    one = bytes.fromhex(
        '837C244400'          # +00 cmp  dword [esp+44h],0   param_2 (the catch-up's copy) or 0 (main tick)
        '742B'                # +05 jz   +32                 param_2 == 0: the original instructions
        '8B442440'            # +07 mov  eax,[esp+40h]       scene (param_1, never written in the function)
        '8B80B40F0000'        # +0B mov  eax,[eax+0FB4h]     room (== game+0x4FC)
        '83787C00'            # +11 cmp  dword [eax+7Ch],0   field? (the +0x13FC queue catch-up)
        '751B'                # +15 jnz  +32                 PvP room: keep the all-entity walk
        '8B442444'            # +17 mov  eax,[esp+44h]
        '89442420'            # +1B mov  [esp+20h],eax       node+8 = the copy
        'C744241800000000'    # +1F mov  dword [esp+18h],0   node+0 = next = NULL
        '8D442418'            # +27 lea  eax,[esp+18h]       list head = &node
        '85C0')               # +2B test eax,eax             ZF = 0: 0x4138CF does not exit
    cave = one + b'\xE9' + rel32(KNOCK_CAVE + len(one), KNOCK_RESUME)          # +2D jmp 0x4138CB
    assert len(cave) == 0x32
    cave += KNOCK_ORIG + b'\xE9' + rel32(KNOCK_CAVE + len(cave) + len(KNOCK_ORIG), KNOCK_RESUME)
    assert len(cave) == KNOCK_CAVE_LEN   # +32 mov eax,[edi+0Ch]; +35 cmp eax,ebx; +37 jmp 0x4138CB
    return cave


def apply_knock_fix(data, off, put, pe, combo=None):
    """combo = (entries, cave_end) returned by combo_hud_2009.apply(), or None when it was skipped."""
    sys.path.insert(0, HERE)
    import combo_hud_2009
    cave = knock_cave_bytes()
    assert len(cave) == KNOCK_CAVE_LEN
    hook = b'\xE9' + struct.pack('<i', KNOCK_CAVE - (KNOCK_HOOK + 5))
    mine = ((KNOCK_HOOK, len(KNOCK_ORIG)), (KNOCK_CAVE, len(cave)))
    # every other patch site: combo_hud's RESERVED list (all other patch_2009 edits) plus, when it
    # was applied, the combo cave and its four hooks
    others = [r for r in combo_hud_2009.RESERVED if r not in mine]
    if combo is not None:
        entries, combo_end = combo
        others.append((combo_hud_2009.CAVE_VA, combo_end - combo_hud_2009.CAVE_VA))
        others += [(va, len(orig)) for va, orig, _n, _w in combo_hud_2009.hooks(entries)]
    missing = [r for r in mine if r not in combo_hud_2009.RESERVED]
    if missing:
        raise SystemExit(f'knock fix: combo_hud_2009.RESERVED lacks {[(hex(a), n) for a, n in missing]}')
    for a, al in mine:
        for b, bl in others:
            if a < b + bl and b < a + al:
                raise SystemExit(f'knock fix: 0x{a:X}+{al} overlaps another patch at 0x{b:X}+{bl}')
    text = next(s for s in pe.sections if s.Name.rstrip(b'\0') == b'.text')
    if not (text.VirtualAddress + B <= KNOCK_CAVE and KNOCK_CAVE + len(cave) <= text.VirtualAddress + B + text.SizeOfRawData):
        raise SystemExit('knock fix: cave is outside the raw .text data')
    # verify everything before writing anything (put() re-checks the hook)
    ho = off(KNOCK_HOOK)
    if bytes(data[ho:ho + len(KNOCK_ORIG)]) != KNOCK_ORIG:
        raise SystemExit(f'catch-up knock fix: unexpected bytes at 0x{KNOCK_HOOK:X}: '
                         f'{bytes(data[ho:ho + len(KNOCK_ORIG)]).hex()} (want {KNOCK_ORIG.hex()}) - not the pristine 2009 exe?')
    co = off(KNOCK_CAVE)
    if bytes(data[co:co + len(cave)]) != b'\0' * len(cave):
        raise SystemExit(f'knock cave: 0x{KNOCK_CAVE:X}..0x{KNOCK_CAVE + len(cave) - 1:X} is not all zero - already patched?')
    data[co:co + len(cave)] = cave
    print(f'  {"knock fix cave":22} 0x{KNOCK_CAVE:X}  {len(cave)} zero bytes -> {cave.hex()}')
    put(KNOCK_HOOK, KNOCK_ORIG, hook, 'catch-up knock fix')
    # .text VirtualSize: max() so the patch order does not matter (after the smooth block, which
    # writes it from a stale pefile value)
    fo = text.get_file_offset() + 8
    cur = struct.unpack_from('<I', data, fo)[0]
    new = max(cur, KNOCK_CAVE + len(cave) - B - text.VirtualAddress)
    if new != cur:
        struct.pack_into('<I', data, fo, new)
    print(f'  {"knock .text VSize":22} 0x{cur:X} -> 0x{new:X}')


# cp-2: KR->EN +4 item-id shift (see the docstring). EN id = KR id + 4 for every id >= 4249
# (_work/pet/align_kr_en.txt; EN hii 4322 rows, id == line order). Each entry is one whole
# instruction (checked byte for byte); only its id field (offset, size; sign -1 = negative
# displacement) changes. Verified with capstone 2026-10-06: a raw scan of .text for imm == id or
# disp == -id, id in 4249..4322, finds exactly these 17 (plus misaligned decodes and the CRT
# exponent check 0x4C237B 'cmp ax,0x10C5'); no u16 item-id table in .rdata/.data holds 4249..4322.
# All ranges are in combo_hud_2009.RESERVED.
ID_SHIFT = 4
ID_SHIFT_SITES = (
    # (instruction va, original bytes, field offset, field size, sign, KR id, what)
    # --- pet (pet.md 1 B2): 11
    (0x44FD72, '6681FFB910',     3, 2, +1, 0x10B9, 'id+4 pet bell gate'),    # FUN_0044f070 cmp di,0x10B9   Pet Bell
    (0x46D64B, '68BA100000',     1, 4, +1, 0x10BA, 'id+4 feed find 250'),    # FUN_0046d640 push 0x10BA     bag search
    (0x46D659, 'BFBA100000',     1, 4, +1, 0x10BA, 'id+4 feed use 250'),     #              mov edi,0x10BA  id fed
    (0x46D660, '68BB100000',     1, 4, +1, 0x10BB, 'id+4 feed find 100'),
    (0x46D66E, 'BFBB100000',     1, 4, +1, 0x10BB, 'id+4 feed use 100'),
    (0x46D675, '68BC100000',     1, 4, +1, 0x10BC, 'id+4 feed find 50'),
    (0x46D683, 'BFBC100000',     1, 4, +1, 0x10BC, 'id+4 feed use 50'),
    (0x46D68A, '68BD100000',     1, 4, +1, 0x10BD, 'id+4 feed find 20'),
    (0x46D698, 'BFBD100000',     1, 4, +1, 0x10BD, 'id+4 feed use 20'),
    (0x46D69F, '8D8746EFFFFF',   2, 4, -1, 0x10BA, 'id+4 feed range'),       # lea eax,[edi-0x10BA]; cmp ax,3
    (0x46DB2B, '2DB7100000',     1, 4, +1, 0x10B7, 'id+4 cash-use switch'),  # FUN_0046d6f0 sub eax,0x10B7; cmp eax,27h
    # --- guild billboards (guild.md 1.6): 6
    (0x44FCB8, '6681FFB810',     3, 2, +1, 0x10B8, 'id+4 board use'),        # FUN_0044f070 cmp di,0x10B8 -> dialog 0x4B7
    (0x45DBC3, '663DB810',       2, 2, +1, 0x10B8, 'id+4 0xBA board spr'),   # S2C 0xBA cmp ax,0x10B8 -> sprite 0x145
    (0x45DBD2, '663DB710',       2, 2, +1, 0x10B7, 'id+4 0xBA prem spr'),    #          cmp ax,0x10B7 -> sprite 0x146
    (0x45DD3B, '663DB810',       2, 2, +1, 0x10B8, 'id+4 0xBB board spr'),   # S2C 0xBB, same pair
    (0x45DD4A, '663DB710',       2, 2, +1, 0x10B7, 'id+4 0xBB prem spr'),
    (0x47E846, '66817C2414B810', 5, 2, +1, 0x10B8, 'id+4 B3 sub185'),        # cmp word [esp+14h],0x10B8 -> C2S 0x15
)


def id_shift_edits():
    """[(va, orig, new, what)] for ID_SHIFT_SITES: whole instructions, the id field + ID_SHIFT."""
    out = []
    for va, ins, fo, fs, sign, kr, what in ID_SHIFT_SITES:
        orig = bytes.fromhex(ins)
        fmt = {2: '<h', 4: '<i'}[fs]
        if fo + fs > len(orig) or struct.unpack_from(fmt, orig, fo)[0] != sign * kr:
            raise SystemExit(f'id shift table: 0x{va:X} field +{fo}/{fs} is not {sign * kr:#x}')
        new = bytearray(orig)
        struct.pack_into(fmt, new, fo, sign * (kr + ID_SHIFT))
        out.append((va, orig, bytes(new), what))
    return out


def apply_id_shift(data, off, put, combo=None):
    """cp-2. combo = (entries, cave_end) returned by combo_hud_2009.apply(), or None when it was skipped."""
    sys.path.insert(0, HERE)
    import combo_hud_2009
    edits = id_shift_edits()
    mine = [(va, len(orig)) for va, orig, _n, _w in edits]
    missing = [r for r in mine if r not in combo_hud_2009.RESERVED]
    if missing:
        raise SystemExit(f'id shift: combo_hud_2009.RESERVED lacks {[(hex(a), n) for a, n in missing]}')
    # every other patch site: RESERVED (all other patch_2009 edits, incl. the knock hook + cave) plus,
    # when it was applied, the combo cave and its four hooks
    others = [r for r in combo_hud_2009.RESERVED if r not in mine]
    if combo is not None:
        entries, combo_end = combo
        others.append((combo_hud_2009.CAVE_VA, combo_end - combo_hud_2009.CAVE_VA))
        others += [(va, len(orig)) for va, orig, _n, _w in combo_hud_2009.hooks(entries)]
    for i, (a, al) in enumerate(mine):
        for b, bl in others + mine[i + 1:]:
            if a < b + bl and b < a + al:
                raise SystemExit(f'id shift: 0x{a:X}+{al} overlaps another patch at 0x{b:X}+{bl}')
    # verify every site before writing anything (put() re-checks each one)
    for va, orig, _new, what in edits:
        o = off(va)
        if bytes(data[o:o + len(orig)]) != orig:
            raise SystemExit(f'{what}: unexpected bytes at 0x{va:X}: {bytes(data[o:o + len(orig)]).hex()} '
                             f'(want {orig.hex()}) - not the pristine 2009 exe, or already patched?')
    for va, orig, new, what in edits:
        put(va, orig, new, what)


def pe_of(data):
    import pefile
    return pefile.PE(data=bytes(data), fast_load=True)


def main():
    args = sys.argv[1:]
    def opt(name, default):
        if name in args:
            i = args.index(name); v = args[i + 1]; del args[i:i + 2]; return v
        return default
    src = os.path.join(HERE, opt('--src', 'WindSlayer.exe'))
    out = os.path.join(HERE, opt('--out', 'WindSlayer_patched.exe'))
    smooth = '--no-smooth' not in args
    aggro_rules = '--no-aggro-rules' not in args
    p2 = '--p2' in args
    combo_hud = '--no-combo-hud' not in args
    knock_fix = '--no-knock-fix' not in args
    id_shift = '--no-id-shift' not in args
    name_rule = opt('--name-rule', 'level')
    if name_rule not in NAME_RULES:
        raise SystemExit(f'--name-rule must be one of {sorted(NAME_RULES)}')
    if p2 and '--out' not in sys.argv:
        out = os.path.join(HERE, 'WindSlayer_p2.exe')
    args = [a for a in args if a not in ('--no-smooth', '--no-aggro-rules', '--p2', '--no-combo-hud', '--no-knock-fix',
                                         '--no-id-shift')]
    if any(a.startswith('--') for a in args):
        raise SystemExit(f'unknown option(s): {[a for a in args if a.startswith("--")]}')
    address = args[0] if args else '127.0.0.1'

    data = bytearray(open(src, 'rb').read())
    pe = pe_of(data)
    pe.parse_data_directories(directories=[1])     # imports
    iat = {i.name.decode(): i.address for e in pe.DIRECTORY_ENTRY_IMPORT for i in e.imports if i.name}
    off = lambda va: pe.get_offset_from_rva(va - B)

    def put(va, orig, new, what):
        o = off(va)
        cur = bytes(data[o:o + len(orig)])
        if cur != orig:
            raise SystemExit(f'{what}: unexpected bytes at 0x{va:X}: {cur.hex()} (want {orig.hex()}) - not the pristine 2009 exe?')
        data[o:o + len(new)] = new
        print(f'  {what:22} 0x{va:X}  {orig.hex()} -> {new.hex()}')

    print(f'{src} -> {out}')
    for va, first in ((0x49F420, 0x8B), (0x49F440, 0x68), (0x49F460, 0x8B), (0x49F480, 0x8B)):
        put(va, bytes([first]), b'\xC3', 'x-trap stub')

    # version server address
    addr = address.strip()
    if not addr or not addr.isascii() or any(c.isspace() for c in addr) or len(addr) > LONG_SIZE - 1:
        raise SystemExit('address must be ASCII, no spaces, at most 52 characters')
    o = off(SLOT_VA)
    if bytes(data[o:o + 14]) != b'207.211.84.46\0':
        raise SystemExit('version-server slot is not the original Outspark address')
    if len(addr) <= SLOT_SIZE - 1:
        data[o:o + SLOT_SIZE] = addr.encode().ljust(SLOT_SIZE, b'\0')
        print(f'  {"version server":22} 0x{SLOT_VA:X}  -> {addr!r}')
    else:
        lo = off(LONG_VA)
        data[lo:lo + LONG_SIZE] = addr.encode().ljust(LONG_SIZE, b'\0')
        put(PUSH_VA, b'\x68' + struct.pack('<I', SLOT_VA), b'\x68' + struct.pack('<I', LONG_VA), 'version push (long)')
        print(f'  {"version server":22} 0x{LONG_VA:X}  -> {addr!r}')
    assert bytes(data[off(LOOPBACK_VA):off(LOOPBACK_VA) + 10]) == b'127.0.0.1\0', 'P2P loopback moved?'

    put(0x43F0FC, struct.pack('<I', 0x43EB99), struct.pack('<I', 0x43ED9C), 'no-response timer')
    put(0x452610, bytes.fromhex('6A01689BA700006AFFFF15') + struct.pack('<I', iat['?SendTo@CSNSocket@@QAE_NKIH@Z'] if '?SendTo@CSNSocket@@QAE_NKIH@Z' in iat else 0x4C70D4),
        b'\x90' * 15, 'LAN probe NOP')

    if smooth:
        leave, sleep = iat['LeaveCriticalSection'], iat['Sleep']
        call_leave = b'\xFF\x15' + struct.pack('<I', leave)
        cave = (bytes.fromhex('FF742404') + call_leave + b'\x6A\x01' + b'\xFF\x15' + struct.pack('<I', sleep)
                + bytes.fromhex('C20400'))
        text = next(s for s in pe.sections if s.Name.rstrip(b'\0') == b'.text')
        co = off(CAVE_VA)
        if any(data[co:co + len(cave)]) or CAVE_VA - B + len(cave) > text.VirtualAddress + text.SizeOfRawData:
            raise SystemExit('cave area is not free')
        data[co:co + len(cave)] = cave
        struct.pack_into('<I', data, text.get_file_offset() + 8, max(text.Misc_VirtualSize, CAVE_VA - B + len(cave) - text.VirtualAddress))
        for va in SMOOTH_SITES:
            put(va, call_leave, b'\xE8' + struct.pack('<i', CAVE_VA - (va + 5)) + b'\x90', 'smooth (Sleep 1)')

    if aggro_rules:
        for va, orig, new, what in AGGRO_RULES + NAME_RULES[name_rule]:
            put(va, bytes.fromhex(orig), bytes.fromhex(new), what)

    combo = None
    if combo_hud:
        # after the smooth block: it writes .text VirtualSize from a stale pefile value
        sys.path.insert(0, HERE)
        import combo_hud_2009
        combo = combo_hud_2009.apply(data, off, put, iat, pe)

    if knock_fix:
        # after the smooth block (stale VirtualSize write) and the combo HUD (its cave extent)
        apply_knock_fix(data, off, put, pe, combo)

    if id_shift:
        # byte-disjoint from every other edit (checked), so the order does not matter
        apply_id_shift(data, off, put, combo)

    if p2:
        # scene+0x248 own port (0x4405C9), CreateSockets UDP bind (0x4407F9), the UDP handler
        # (0x45F194) and the launcher dialog (0x488C3C) - client_map_2009 second_client_udp_port
        for va in (0x4405CF, 0x4407FA, 0x45F19A, 0x488C42):
            put(va, struct.pack('<I', 42907), struct.pack('<I', 42908), 'p2 UDP port')

    tmp = out + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    if os.path.exists(out):
        aside = out + '.replaced'
        if os.path.exists(aside):
            try:
                os.remove(aside)
            except OSError:
                aside = f'{out}.replaced{os.getpid()}'
        os.replace(out, aside)
        try:
            os.remove(aside)
        except OSError:
            pass
    os.replace(tmp, out)
    print('done')


if __name__ == '__main__':
    main()
