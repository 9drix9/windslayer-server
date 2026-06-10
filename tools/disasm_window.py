"""Disassemble a window of code around given VAs in WindSlayer_patched.exe."""
import pefile, capstone, sys

pe = pefile.PE(r'C:\Users\ohdri\Desktop\WindSlayer2Game\WindSlayer_patched.exe')
base = pe.OPTIONAL_HEADER.ImageBase
data = pe.get_memory_mapped_image()
md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)

def window(va, before=0x80, after=0x60):
    start = va - before
    code = data[start - base: va + after - base]
    # find a clean decode alignment: try start offsets until target decodes on boundary
    for adj in range(16):
        ok = False
        for ins in md.disasm(code[adj:], start + adj):
            if ins.address == va:
                ok = True
                break
            if ins.address > va:
                break
        if ok:
            for ins in md.disasm(code[adj:], start + adj):
                mark = '  <<<<' if ins.address == va else ''
                print(f'0x{ins.address:08X}: {ins.mnemonic:10s} {ins.op_str}{mark}')
                if ins.address >= va + after - 16:
                    break
            return
    print(f'could not align decode for 0x{va:08X}')

for va_s in sys.argv[1:]:
    va = int(va_s, 16)
    print(f'===== window around 0x{va:08X} =====')
    window(va)
    print()
