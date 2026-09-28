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
The launcher still needs the three SSO arguments: WindSlayer_patched.exe -<a> -<b> -<c>
(play_2009.bat passes them).

usage: python patch_2009.py [address] [--src WindSlayer.exe] [--out WindSlayer_patched.exe]
                            [--no-smooth] [--no-aggro-rules] [--name-rule level|kr]
                            [--no-combo-hud] [--p2]
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
    name_rule = opt('--name-rule', 'level')
    if name_rule not in NAME_RULES:
        raise SystemExit(f'--name-rule must be one of {sorted(NAME_RULES)}')
    if p2 and '--out' not in sys.argv:
        out = os.path.join(HERE, 'WindSlayer_p2.exe')
    args = [a for a in args if a not in ('--no-smooth', '--no-aggro-rules', '--p2', '--no-combo-hud')]
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

    if combo_hud:
        # after the smooth block: it writes .text VirtualSize from a stale pefile value
        sys.path.insert(0, HERE)
        import combo_hud_2009
        combo_hud_2009.apply(data, off, put, iat, pe)

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
