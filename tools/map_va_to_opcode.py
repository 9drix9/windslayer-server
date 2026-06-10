"""Map arbitrary code VAs to the opcode whose handler contains them, using the
dispatch tables (index 0x4584B0 -> handler-slot, handler table 0x458368)."""
import pefile, struct, sys

pe = pefile.PE(r'C:\Users\ohdri\Desktop\WindSlayer2Game\WindSlayer_patched.exe')
base = pe.OPTIONAL_HEADER.ImageBase
data = pe.get_memory_mapped_image()

def u32(va):
    return struct.unpack_from('<I', data, va - base)[0]

def u8(va):
    return data[va - base]

HANDLER_TABLE = 0x458368
INDEX_TABLE = 0x4584B0
N_HANDLERS = 82
N_OPCODES = 167  # 0x01..0xA7

handlers = [u32(HANDLER_TABLE + 4 * i) for i in range(N_HANDLERS)]
index = [u8(INDEX_TABLE + i) for i in range(N_OPCODES)]
op2h = {}
for i, slot in enumerate(index):
    if slot < N_HANDLERS:
        op2h[i + 1] = handlers[slot]
h2ops = {}
for op, h in op2h.items():
    h2ops.setdefault(h, []).append(op)

starts = sorted(set(handlers))

def containing(va):
    prev = None
    for h in starts:
        if h <= va:
            prev = h
        else:
            break
    return prev

targets = [int(x, 16) for x in sys.argv[1:]]
for t in targets:
    h = containing(t)
    ops = h2ops.get(h, []) if h else []
    opss = ', '.join(f'0x{o:02X}' for o in ops) if ops else '(no opcode -> sub-handler/helper)'
    hstr = f'0x{h:08X}' if h else 'None'
    print(f'VA 0x{t:08X} -> handler {hstr}  opcodes: {opss}')
