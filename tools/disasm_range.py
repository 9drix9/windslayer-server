"""Linear disassembly of a VA range (single aligned pass from start)."""
import pefile, capstone, sys

pe = pefile.PE(r'C:\Users\ohdri\Desktop\WindSlayer2Game\WindSlayer_patched.exe')
base = pe.OPTIONAL_HEADER.ImageBase
data = pe.get_memory_mapped_image()
md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)

IAT = {
    0x4a4090: 'GetU16', 0x4a4094: 'GetU8', 0x4a4098: 'GetStr',
    0x4a40c8: 'GetU32', 0x4a40cc: 'GetU32K', 0x4a40d0: 'GetBool1',
    0x4a40d4: 'GetI32', 0x4a40b0: 'GetF64',
}

start = int(sys.argv[1], 16)
end = int(sys.argv[2], 16)
code = data[start - base: end - base]
for ins in md.disasm(code, start):
    note = ''
    if ins.mnemonic == 'call' and '0x4a4' in ins.op_str:
        try:
            a = int(ins.op_str.split('[')[1].rstrip(']'), 16)
            if a in IAT:
                note = f'   ; {IAT[a]}'
        except (IndexError, ValueError):
            pass
    print(f'0x{ins.address:08X}: {ins.mnemonic:9s} {ins.op_str}{note}')
