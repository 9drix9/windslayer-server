"""Full linear trace of an opcode handler: every Get* IAT call in execution
order with the destination offset it writes (from the preceding lea reg,[esi+off]
or lea reg,[esp+off]). Gives the exact wire format the client expects.

Usage: trace_handler.py <start_hex> <end_hex>
"""
import pefile, capstone, sys

pe = pefile.PE(r'C:\Users\ohdri\Desktop\WindSlayer2Game\WindSlayer_patched.exe')
base = pe.OPTIONAL_HEADER.ImageBase
data = pe.get_memory_mapped_image()
md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
md.detail = True

IAT = {
    0x4a4090: 'GetU16', 0x4a4094: 'GetU8', 0x4a4098: 'GetStr(len)',
    0x4a40c8: 'GetU32', 0x4a40cc: 'GetU32K', 0x4a40d0: 'GetBool1',
    0x4a40d4: 'GetI32', 0x4a40b0: 'GetF64',
}

start = int(sys.argv[1], 16)
end = int(sys.argv[2], 16)
code = data[start - base: end - base]

# track last lea dest into esi/edx/eax/ecx (the arg that becomes the write dest)
last_dest = {}   # reg -> textual dest
seq = []
strlen_push = None
for ins in md.disasm(code, start):
    m = ins.mnemonic
    op = ins.op_str
    if m == 'lea' and ',' in op:
        reg, src = op.split(', ', 1)
        last_dest[reg] = src
    elif m == 'push':
        # GetStr pushes the max length right before; remember last imm push
        if op.startswith('0x') or op.isdigit():
            try:
                strlen_push = int(op, 0)
            except ValueError:
                strlen_push = None
    elif m == 'call' and op.startswith('dword ptr [0x4a4'):
        addr = int(op.split('[')[1].rstrip(']'), 16)
        name = IAT.get(addr)
        if name:
            # the destination is whichever reg most recently had a lea to [esi+..]/[esp+..]
            # the handler pushes the dest pointer just before the call; find the [esi+..] dest
            dest = None
            # prefer an esi-relative dest (entity field)
            for reg, src in last_dest.items():
                if 'esi' in src:
                    dest = src
            if dest is None:
                # fall back to most recent stack dest
                for reg, src in last_dest.items():
                    if 'esp' in src:
                        dest = src
            extra = f' len={strlen_push}' if 'Str' in name else ''
            seq.append((ins.address, name, dest, extra))
            last_dest.clear()

print(f'{len(seq)} field reads in 0x{start:08X}..0x{end:08X}\n')
for addr, name, dest, extra in seq:
    print(f'  0x{addr:08X}  {name:12s} -> {dest}{extra}')
