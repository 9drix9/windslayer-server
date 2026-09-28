# Client handler at VA 0x4503E1 — opcode 0x2B "enter world" response parser

## Source
Reverse-engineered from `WindSlayer_patched.exe` (image base 0x400000), function
spanning 0x4503E1..0x45079C. Function is a tail-call: ends with
`jmp 0x44d7ad` rather than `ret`. Uses the Fireway `CEncMsg::GetDataFromPacket`
reader table at 0x4A4090..0x4A40DC.

## Buffer layout
- Allocator `call 0x48B484` returns a 0x15F0-byte buffer. If non-NULL, `call
  0x444390` runs a constructor; else the pointer is zeroed. The buffer is stored
  at local `[esp+0x18]` throughout.
- `memset(buf, 0, 0x15F0)` at 0x450411.
- Constant write at 0x45042D: `buf[0x98] = 3` (status/type byte).

## Reader-vtable targets (observed)
| target        | signature                                  |
|---------------|--------------------------------------------|
| 0x4A4090      | `GetDataFromPacket(USHORT&)`               |
| 0x4A4094      | `GetDataFromPacket(UCHAR&)`                |
| 0x4A4098      | `GetDataFromPacket(char*, int maxlen)`     |
| 0x4A40C8      | `GetDataFromPacket(UINT&)`                 |
| 0x4A40CC      | `GetDataFromPacket(ULONG&)`                |
| 0x4A40D0      | unknown reader (variant) — treat as 4-byte |
| 0x4A40D4      | `GetDataFromPacket(int&)`                  |

## Ordered read table

All offsets are relative to the 0x15F0-byte buffer.

| seq | VA         | reader   | buf off | field name (guess) |
|-----|------------|----------|---------|--------------------|
|  1  | 0x00450434 | STRING   | +0x0000 | character name (maxlen 0x11 = 17, incl. NUL) |
|  2  | 0x00450452 | UINT     | +0x0084 | character ID / uid |
|  3  | 0x0045046F | INT      | +0x15D8 | money / gold (near end of buffer) |
|  4  | 0x0045048A | USHORT   | +0x0012 | level |
|  5  | 0x004504A8 | UCHAR    | +0x0110 | class / job |
|  6  | 0x004504C5 | UCHAR    | +0x0111 | gender (or skin) |
|  7  | 0x004504E3 | UCHAR    | +0x0099 | equip/cosmetic byte A (hair?) |
|  8  | 0x00450501 | UCHAR    | +0x009A | equip/cosmetic byte B (face?) |
|  9  | 0x0045051E | 0x4A40D0 | +0x0113 | 4-byte field (flags?) |

### Loop A — stats/attributes (14 × USHORT)
State at 0x450524: `ptr = buf+0x120`, counter=14 (0xE).
Each iter reads one USHORT via [0x4A4090] into `ptr`, then `ptr+=2; counter--`.

| seq 10 | 0x00450552 | USHORT (loop) | +0x0120..+0x013B | 14 USHORTs: stats (STR/DEX/INT/… x 14) |

### Discrete USHORTs (5 × USHORT)
| 11 | 0x0045057C | USHORT | +0x00E6 | unknown ushort E6 |
| 12 | 0x0045059A | USHORT | +0x00E8 | unknown ushort E8 |
| 13 | 0x004505B7 | USHORT | +0x00EA | unknown ushort EA |
| 14 | 0x004505D5 | USHORT | +0x00EC | unknown ushort EC |
(These four consecutive USHORTs at 0xE6..0xED could be HP/MP/max-HP/max-MP.)

### Loop B — outer/inner inventory or equipment (16 outer × (1 + 6) USHORTs)
State at 0x4505DB..0x4505F2:
- Outer dest: `p_outer = buf+0x13C`, outer counter = 0x10 (16)
- Inner dest starts at `buf+0x16E` (carried across outer iterations via [esp+0x14]→[esp+0x20])

Per outer iteration (loop head 0x450600, back-edge 0x450666):
- seq 15: read USHORT into `p_outer`  (call at 0x450612)
- Inner loop (head 0x450630, back-edge 0x450652): counter = 6
  - seq 16: read USHORT into `p_inner` (call at 0x450642); `p_inner += 2`
- `p_outer += 2`

Totals over Loop B:
- 16 outer USHORTs at buf+0x13C..buf+0x15B (0x13C + 2*i, i=0..15)
- 16 × 6 = 96 inner USHORTs at buf+0x16E..buf+0x22D (0x16E + 2*j, j=0..95)

This looks like an item/equip table: 16 slots each carrying 1 slot-id USHORT and 6
per-slot attribute USHORTs.

### Loop C — skills (9 × USHORT)
State at 0x450668..0x450676: `ptr = buf+0x15C`, counter = 9.
Loop head 0x450680, back-edge 0x4506A2.
- seq 17: USHORT at `ptr` via [0x4A4090]; `ptr += 2`
- Total: 9 USHORTs at buf+0x15C..buf+0x16D. (Sits in the hole between Loop A end
  at 0x13B and Loop B inner start at 0x16E.)

### Dynamic array count + payload
| 18 | 0x004506B6 | UCHAR → stack [esp+0x58] | (not a buf field) | array count N (byte) |

Code: `cmp byte ptr [esp+0x58], 0; jbe end_of_loop`. If N > 0, loop:
- State at 0x4506CB..0x4506D5: `ptr = buf+0x29A`
- Loop head 0x4506E0, back-edge 0x45070F (signed compare `jl`, counter in [esp+0x14])
- seq 19: USHORT at `ptr` via [0x4A4090]; `ptr += 2`; loop i < N

Produces N USHORTs at buf+0x29A..buf+0x29A+2N-1.

### Non-reader helper call
0x0045071B: `push [esi+0x4CC]; call 0x422120` — not a packet read. Likely post-
processing (e.g., update UI or list node from a field loaded earlier). Safe to
ignore for protocol byte layout.

### Two trailing UCHARs
| 20 | 0x00450738 | UCHAR  | +0x14EB | single-byte flag (non-zero toggles derived byte at buf+0x98 via `buf[0x98] = (flag!=0) ? 3 : 1` — see below) |
| 21 | 0x0045076E | UCHAR  | +0x14EC | last byte |

The snippet at 0x4503F7..0x450767 includes:
```
setne dl
lea edx, [edx + edx + 1]        ; dl = 1 or 3
mov byte ptr [eax - 0x1454], dl  ; buf + 0x14EC - 0x1454 = buf + 0x98
```
i.e. `buf[0x98] = (buf[0x14EB] != 0) ? 3 : 1`. That field set earlier to `3` at
0x45042D is re-derived here.

### Post-processing after all reads
```
mov edx, [buf + 0x110C]
mov [buf + 0x9C],  edx
mov ecx, [buf + 0x1110]
mov [buf + 0xA0],  ecx
push esi
call 0x442EE0     ; e.g., character list insert?
push esi
call 0x443280     ; e.g., transition to world scene
jmp  0x44D7AD     ; tail call out of dispatcher
```
This copies two 4-byte fields from 0x110C/0x1110 to 0x9C/0xA0 (character-object
pointer fields being relocated within the structure). Then hands off.

## Fixed-part wire layout (minimum payload the server must send)

Based on the first 17 reads (before any loops), plus loops A/B/C, plus dynamic
array, plus final 2 bytes. The UCHARs/USHORTs are little-endian. Strings are
fixed-length (0x11 bytes = name with trailing NULs).

```
offset  size   field                         → buf off
------  ----   --------------------------    ---------
  0     17     char[17] name (NUL-padded)    0x0000
 17      4     uint32  char_id               0x0084
 21      4     int32   money                 0x15D8
 25      2     uint16  level                 0x0012
 27      1     uint8   class                  0x0110
 28      1     uint8   gender                 0x0111
 29      1     uint8   hair / cosmetic A      0x0099
 30      1     uint8   face / cosmetic B      0x009A
 31      4     uint32  flags (reader 0x4A40D0; 4 bytes)  0x0113
 35     28     uint16[14] stats              0x0120..0x013B
 63      2     uint16  hp?                    0x00E6
 65      2     uint16  mp?                    0x00E8
 67      2     uint16  max_hp?                0x00EA
 69      2     uint16  max_mp?                0x00EC
 71     32     uint16[16] slot_ids            0x013C..0x015B
103    192     uint16[16][6] slot_attrs       0x016E..0x022D
295     18     uint16[9] skill_slots          0x015C..0x016D
313      1     uint8  extra_count N           (stack; gates loop D)
314    2*N     uint16[N] extras               0x029A..0x029A+2N
...      1     uint8  flag                    0x14EB
...      1     uint8  last_byte               0x14EC
```

Note that `Loop A/B/C` are written in the order the reader sees them on the wire,
but the target offsets jump around (0x120 → 0xE6/E8/EA/EC → 0x13C → 0x16E → 0x15C
→ 0x29A). The wire order is what matters; buffer offsets are where the handler
stores each field.

## Loops summary
- Loop A: 14 × u16 at buf+0x120 stride 2
- Loop B outer: 16 × u16 at buf+0x13C stride 2; each outer iter also runs
  Loop B inner: 6 × u16 starting from buf+0x16E, persistent pointer across outer
  iters (so global stride 2, total 96 × u16)
- Loop C: 9 × u16 at buf+0x15C stride 2
- Loop D: N × u16 at buf+0x29A stride 2, where N is the preceding u8
