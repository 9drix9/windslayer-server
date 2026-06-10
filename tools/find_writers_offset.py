"""Find all instructions referencing a given entity offset, classified read/write.
Usage: find_writers_offset.py 0xe60"""
import pefile, capstone, struct, sys

pe = pefile.PE(r'C:\Users\ohdri\Desktop\WindSlayer2Game\WindSlayer_patched.exe')
base = pe.OPTIONAL_HEADER.ImageBase
data = pe.get_memory_mapped_image()
txt = next(s for s in pe.sections if s.Name.startswith(b'.text'))
start = base + txt.VirtualAddress
code = data[txt.VirtualAddress: txt.VirtualAddress + txt.Misc_VirtualSize]
md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)

off = int(sys.argv[1], 16)
pat = struct.pack('<i', off)
hits = []
i = 0
while True:
    i = code.find(pat, i)
    if i < 0:
        break
    hits.append(i)
    i += 1
print(f'offset 0x{off:X}: {len(hits)} raw disp hits')

for h in hits:
    best = None
    for back in range(1, 10):
        o = h - back
        if o < 0:
            continue
        ins = next(md.disasm(code[o:o + 16], start + o), None)
        if ins and f'0x{off:x}' in ins.op_str and ins.size > back:
            best = ins
            break
    if best:
        ops = best.op_str
        # crude write detection: dest operand (before comma) is the memory ref
        memfirst = ops.split(',')[0].strip()
        is_write = ('ptr [' in memfirst) and (f'0x{off:x}]' in memfirst)
        kind = 'WRITE' if is_write else 'read'
        print(f'  0x{best.address:08X} [{kind}] {best.mnemonic} {ops}')
    else:
        print(f'  .text+0x{h:x} (unframed)')
