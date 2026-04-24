# Client Dispatch Table (English 2008 WindSlayer)

The client's packet dispatcher lives at VA `0x44D5D0`. When a packet arrives it:

1. Reads the opcode byte from the header
2. Looks up an index in a byte-table at VA `0x4584B0` (`movzx edx, byte [eax + 0x004584B0]`)
3. Jumps to the handler via a VA table at `0x00458368` (`jmp [edx*4 + 0x00458368]`)

The handler table has **82 entries**. Multiple opcodes can map to the same handler (for example, opcode 0x2A and 0x2D both go to handler index 28 at VA `0x44F762`).

## Dispatcher prologue (reverse-engineered)

The dispatcher is a `__thiscall` function. `ecx` (→ `esi`) is the session/socket object; `arg1` is the packet buffer.

```
mov eax, opcode      ; byte from packet header
add eax, -1          ; eax = opcode - 1
cmp eax, 0xA6        ; max valid opcode is 0xA7
ja  default_handler
movzx edx, byte [eax + 0x4584B0]  ; lookup index
jmp [edx*4 + 0x00458368]           ; jump to handler
```

## Full opcode → handler mapping

| Opcode | Idx | Handler VA  | Notes                       |
|--------|-----|-------------|-----------------------------|
| 0x00   |  0  | 0x0044D663  |                             |
| 0x01   |  1  | 0x0044D8CA  | **login response**          |
| 0x02   |  2  | 0x0044E38F  | login response step 2       |
| 0x03   |  3  | 0x0044EF72  | timer kill                  |
| 0x04   |  4  | 0x0044FD08  |                             |
| 0x05   |  5  | 0x004507A1  |                             |
| 0x06   |  3  | 0x0044EF72  | shared with 0x03            |
| 0x07   |  6  | 0x00450867  |                             |
| 0x08   |  7  | 0x004511C7  |                             |
| 0x09   |  8  | 0x00451383  |                             |
| 0x0A–0x0F | 81 | 0x004582B3 | default/unknown handler    |
| 0x10   |  9  | 0x00451516  | **"first purchase of wind cash" dialog** — don't trigger |
| 0x11   |  9  | 0x00451516  | shared with 0x10            |
| 0x12   | 10  | 0x004518D0  |                             |
| 0x13   | 11  | 0x0045478A  |                             |
| 0x14   | 12  | 0x004519C9  |                             |
| 0x15   | 13  | 0x00451AF0  |                             |
| 0x16   | 14  | 0x00451D7E  |                             |
| 0x17,0x18 | 81 | 0x004582B3 | default                    |
| 0x19   | 15  | 0x00451D8D  |                             |
| 0x1A   | 16  | 0x004523F9  |                             |
| 0x1B   | 17  | 0x00452648  |                             |
| 0x1C   | 18  | 0x0045298C  |                             |
| 0x1D   | 19  | 0x00454090  |                             |
| 0x1E   | 20  | 0x0045285A  |                             |
| 0x1F   | 21  | 0x00457852  |                             |
| 0x20   | 22  | 0x004542C4  |                             |
| 0x21   | 23  | 0x00454582  |                             |
| 0x22   | 81  | 0x004582B3  | default                     |
| 0x23   | 19  | 0x00454090  | shared with 0x1D            |
| 0x24   | 24  | 0x00452D69  |                             |
| 0x25,0x26 | 81 | 0x004582B3 | default                    |
| 0x27   | 25  | 0x00454963  |                             |
| 0x28   | 26  | 0x00454D46  |                             |
| 0x29   | 27  | 0x00454E6D  |                             |
| 0x2A   | 28  | 0x0044F762  | shared with 0x2D — checks `cmp byte [esp+0x43], 0x2E` |
| 0x2B   | 29  | 0x004503E1  | **enter-world** (calls scene_A + scene_B) |
| 0x2C   | 30  | 0x004507EE  | **DANGER: freezes client** (calls 0x4305B0(session, 0x27)) |
| 0x2D   | 28  | 0x0044F762  | shared with 0x2A            |
| 0x2E   | 31  | 0x00450B6D  | world data (list entry setup) |
| 0x2F   | 32  | 0x004551BE  | **DANGER: always shows dialog** |
| 0x30   | 33  | 0x004551F8  |                             |
| 0x31   | 34  | 0x0045565E  |                             |
| 0x32   | 35  | 0x004555E6  |                             |
| 0x33   | 36  | 0x00455C21  |                             |
| 0x34   | 37  | 0x00455E94  |                             |
| 0x35   | 38  | 0x00456015  |                             |
| 0x36   | 39  | 0x00456431  |                             |
| 0x37   | 81  | 0x004582B3  | default                     |
| 0x38   | 40  | 0x0045662F  |                             |
| 0x39   | 41  | 0x00451942  |                             |
| 0x3A   | 24  | 0x00452D69  | shared with 0x24            |
| 0x3B   | 42  | 0x00456649  |                             |
| 0x3C   | 43  | 0x004549E9  |                             |
| 0x3D   | 44  | 0x00454E41  |                             |
| 0x3E   | 81  | 0x004582B3  | default                     |
| 0x3F   | 45  | 0x00454AF2  |                             |
| 0x40   | 46  | 0x00453F66  |                             |
| 0x41   | 46  | 0x00453F66  | shared with 0x40            |
| 0x42   | 42  | 0x00456649  | shared with 0x3B            |
| 0x43   | 47  | 0x004549A6  |                             |
| 0x44–0x4C | 81 | 0x004582B3 | default                    |
| 0x4D   | 48  | 0x0045683F  |                             |
| 0x4E   | 49  | 0x00456889  |                             |
| 0x4F   | 50  | 0x00456B0F  |                             |
| 0x50   | 51  | 0x00456B44  |                             |
| 0x51   | 52  | 0x00456DC7  |                             |
| 0x52   | 53  | 0x004572E0  |                             |
| 0x53   | 54  | 0x00457341  |                             |
| 0x54   | 55  | 0x004573F0  |                             |
| 0x55   | 56  | 0x00456B29  |                             |
| 0x56   | 57  | 0x00457536  |                             |
| 0x57   | 58  | 0x004575FA  |                             |
| 0x58   | 81  | 0x004582B3  | default                     |
| 0x59   | 59  | 0x0044D7CF  |                             |
| 0x5A   | 60  | 0x004576C3  | **key exchange**            |
| 0x5B   | 61  | 0x00457750  |                             |
| 0x5C   | 62  | 0x004523CB  |                             |
| 0x5D   |  6  | 0x00450867  | shared with 0x07            |
| 0x5E   | 63  | 0x00457794  |                             |
| 0x5F–0x61 | 81 | 0x004582B3 | default                    |
| 0x62   | 64  | 0x004577A8  |                             |
| 0x63   | 65  | 0x0044D7AD  | **no-op** (just ret, safe to echo) |
| 0x64   | 66  | 0x0044E017  |                             |
| 0x65–0x72 | 81 | 0x004582B3 | default                    |
| 0x73   | 67  | 0x00457EF6  |                             |
| 0x74   | 68  | 0x00457F91  |                             |
| 0x75–0x7D | 81 | 0x004582B3 | default                    |
| 0x7E   | 22  | 0x004542C4  | shared with 0x20            |
| 0x7F   | 69  | 0x0044E2FE  |                             |
| 0x80   | 70  | 0x004528F3  |                             |
| 0x81   | 81  | 0x004582B3  | default                     |

## Getting this table yourself

```python
import struct
with open('WindSlayer.exe', 'rb') as f:
    data = f.read()

def foff(va): return 0x1000 + (va - 0x401000)

# Handler array
handlers = []
f = foff(0x00458368)
for i in range(82):
    handlers.append(struct.unpack('<I', data[f + i*4 : f + i*4 + 4])[0])

# Opcode → index map
m = foff(0x004584B0)
for op in range(0x82):
    idx = data[m + op]
    print(f'  0x{op:02X} -> idx {idx} = 0x{handlers[idx]:08X}')
```

## What we don't know

We have the **handler VAs** but have only reverse-engineered a handful of them:
- `0x2B` (enter-world) fully mapped — see `enter_world_fields.md`
- `0x2C`, `0x2F` identified as dangerous (freeze / always-dialog)
- `0x63` identified as a no-op
- The rest are unknown — contributions welcome
