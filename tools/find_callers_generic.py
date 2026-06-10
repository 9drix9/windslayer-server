"""Find E8 call sites targeting given VAs."""
import pefile, capstone, struct, sys

pe = pefile.PE(r'C:\Users\ohdri\Desktop\WindSlayer2Game\WindSlayer_patched.exe')
base = pe.OPTIONAL_HEADER.ImageBase
data = pe.get_memory_mapped_image()
txt = next(s for s in pe.sections if s.Name.startswith(b'.text'))
tstart = txt.VirtualAddress
tcode = data[tstart: tstart + txt.Misc_VirtualSize]
md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)

targets = [int(x, 16) for x in sys.argv[1:]] or [0x422120]
for TARGET in targets:
    print(f'=== callers of 0x{TARGET:08X} ===')
    # E8 rel32 calls
    for off in range(len(tcode) - 5):
        if tcode[off] == 0xE8:
            rel = struct.unpack_from('<i', tcode, off + 1)[0]
            callva = base + tstart + off
            if callva + 5 + rel == TARGET:
                # which function? find nearest preceding int3-padding gap
                print(f'  E8 call at 0x{callva:08X}')
    # immediate refs (indirect)
    needle = struct.pack('<I', TARGET)
    i = 0
    while True:
        i = data.find(needle, i)
        if i < 0:
            break
        print(f'  imm ref at VA 0x{base + i:08X}')
        i += 1
    print()
