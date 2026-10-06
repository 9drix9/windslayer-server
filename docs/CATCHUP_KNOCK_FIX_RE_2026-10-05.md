# Catch-up knock fix F1 (EN 2009 client): RE, design and verification, 2026-10-05

**Question.** `live_harness/p8p13_live_triage.md` finding 1 says the field catch-up pass runs pass 2 of `FUN_00412c60` for every entity, so it consumes the local player's fresh knock (`+0x9DC = 6`) before the 0x0D builder can send it (4 of 162 knocks lost in D1). Is that right? Where can pass 2 be restricted to the catch-up's copy, and is the triage's proposed cave safe?

**Method.** Static RE and emulation only. The game was not started, and the server and the live exes were not touched.
- Ghidra corpus `re_tools/corpus_2009` (decomp and asm), plus the exe bytes.
- Capstone: a recursive CFG of `FUN_00412c60` with its three jump tables, the ESP depth of every instruction, every access to a frame slot, and an abstract interpretation of EBX/EDI/EBP.
- Unicorn 2.1.4: the function run from the live exes and from the test exes on synthetic entity lists.
- Scripts are in `re_tools/f1_knock_fix/`:
  - `re_f1.py`: the RE facts below, from the pristine exe.
  - `verify_f1.py`: static proof on the test exes.
  - `emu_f1.py`: the emulation.
  - `flag_check.py`: an in-memory build check that writes no file.

VAs are for Build 14. "Depth" is ESP relative to the function entry, where `[esp]` is the return address. The body runs at depth **-0x3C**: `sub esp,2Ch` plus four pushes. There `[esp+40h]` is `param_1` (the scene) and `[esp+44h]` is `param_2` (the entity).

## 0. Bottom line

1. **The triage's mechanism is confirmed.**
   - `FUN_00412c60` has two callers: 0x42CB56 in the main tick, with param 0, and 0x42CC94 in the catch-up, with param = the entity.
   - Pass 1 honours `param_2`. Pass 2, from 0x4138C6 to 0x41419C, reloads the scene list head and walks every entity. Its filter (0x4138F9..0x413939) admits the local player: `scene+0x224 == +0x88` at 0x41390C.
   - In emulation, the live exe's catch-up call for copy B runs the reaction function for P, B, C and M, so the local player's knock is consumed. The F1 exe runs it for B only.
2. **The triage underestimated one risk, so F1 is gated to the field.**
   - When `room+0x7C != 0`, the frame function skips the main tick entirely (0x42CAF0: `cmp [eax+7Ch],0; jnz 0x42CBE0`). In PvP rooms (mode 1), the catch-up's `+0x163C` branch is then the **only** caller of `FUN_00412c60`.
   - With the ungated proposal, an entity there that is not itself a catch-up subject would never get its pass-2 reaction. One example is a type-4 entity in a room, since `+0x163C` is set only for room-player entities (`FUN_00447f40`, `FUN_00448350`, `FUN_00461ad0`). The reaction order of the room lockstep would also change.
   - So the cave applies the one-node list only when `[[scene+0xFB4]+0x7C] == 0`. That is the same field test that the consumer (`FUN_004129f0`) and the catch-up condition use. PvP rooms keep the original walk, and emulation shows they match the live exe byte for byte.
3. **The main tick is unchanged.** `param_2 == 0` runs the two original instructions, `mov eax,[edi+0Ch]; cmp eax,ebx`, and then rejoins at 0x4138CB. Emulation shows identical heap, stack, call log and callee-saved registers.
4. **The patch is small.**
   - The 5-byte hook at 0x4138C6 becomes `jmp 0x4C6F00`.
   - The cave is 60 bytes at 0x4C6F00..0x4C6F3B in the zero `.text` tail.
   - `.text` VirtualSize grows from 0xC58A0 to 0xC5F3C.
   - Nothing else changes. The `--no-knock-fix` build reproduces the pre-F1 `WindSlayer_patched.exe` and `WindSlayer_p2.exe` byte for byte.

## 1. Who calls `FUN_00412c60`, and when

The whole-image scan finds only two E8 calls to 0x412C60, and no absolute reference to it.

| Call site | Context | param_2 | Runs when |
|---|---|---|---|
| 0x42CB56 | main tick loop 0x42CB04..0x42CB88: `FUN_004185b0`, builder `FUN_0042e1b0` (if scene mode 6), consumer `FUN_004129f0(scene,0)`, **`FUN_00412c60(scene,0)`**, `FUN_00414210`, `FUN_00415f80`, detection `FUN_00416ab0(scene,0)` | 0 | `[game+0x4FC]+0x7C == 0` (field) only: 0x42CAEA..0x42CAF4 |
| 0x42CC94 | catch-up 0x42CBFB..0x42CCC4, after the tick loop, for each list entity that matches (a) or (b) below | entity (ESI) | every frame, in field and room |

The catch-up conditions (0x42CC1F..0x42CC50):
- **(a)** type 3, `+0x13FC > 1` (two or more queued 0x1B nodes), and room mode 0.
- **(b)** `+0x163C != 0` and room mode 1. For (b), `FUN_004185b0(scene,30,entity)` runs first.

When room mode is not 0, 0x42CAF4 goes to 0x42CBE0, which calls only `FUN_0042a300` (room bookkeeping, none of the tick functions). **A room has no main tick.**

`scene+0xFB4` is `game+0x4FC`. Its only writer is 0x42B4AE in `FUN_0042b2b0`. Both pointers are therefore the same room object, and its `+0x7C` is the mode: 0 field, 1 PvP.

## 2. `FUN_00412c60` structure

### Frame

Prologue at 0x412C60: `sub esp,2Ch; push ebx; push ebp; mov ebp,[esp+3Ch]` (param_2), then `push esi; push edi; mov edi,[esp+40h]` (scene), `mov eax,[edi+0Ch]; xor ebx,ebx; cmp eax,ebx; mov [esp+14h],eax`.

The only exit is at 0x4141A2: `pop edi; pop esi; pop ebp; pop ebx; add esp,2Ch; ret 8` (stdcall). The CFG walk over all 1332 reachable instructions finds a consistent depth at every merge point, and the single RET runs at depth 0.

### Pass 1 (0x412C82..0x4138C0), the attackers' `+0x9E0`

- The cursor is `[esp+14h]`. With param != 0, the loop runs once for `param_2` and leaves at 0x4138B6: `cmp [esp+44h],ebx; jnz 0x4138C6`.
- With param 0, it loops `0x4138BC cmp [esp+14h],ebx; jnz 0x412C82` and falls through to 0x4138C6.
- An empty list skips pass 1: 0x412C7A `jz 0x4138C6`.

### Pass 2 (0x4138C6..0x41419C), the victims' `+0x9DC`

```
4138C6  mov eax,[edi+0Ch]        ; list head            <- hooked (5 bytes: 8B 47 0C 3B C3)
4138C9  cmp eax,ebx              ; ebx == 0
4138CB  mov [esp+14h],eax        ; cursor = head        <- the cave rejoins here
4138CF  jz  4141A2               ; empty list -> epilogue
4138D5  mov edi,[esp+40h]        ; scene
4138D9  xor ebp,ebp              ; ebp = 0 for the whole pass
4138DB  jmp 4138E0
4138E0  mov eax,[esp+14h]        ; LOOP HEAD: node
4138E4  mov ebx,[eax+8]          ;   entity = node+8
4138E7  cmp ebx,ebp
4138E9  mov ecx,[eax]            ;   next = node+0
4138EB  mov [esp+14h],ecx        ;   cursor = next
4138EF  mov [esp+20h],ebx        ;   saved entity (read only by the PvP 0xD branch)
4138F3  jz  414198               ;   null entity -> next
4138F9..413939                   ;   filter: type 3 with (+0xEE4 | local player | scene mode 4/5 | PvP room), or type 4
41393F..41397D                   ;   +0x9DC != 0: clear +0xF18, retime buffs 0x1A8..0x1B2
41397F..413998                   ;   switch (+0x9DC - 1), table 0x4141E8 via the byte index 0x4141FC
   ...                           ;   cases 1-5 0x41399F, 9 0x413A3D, 6/7/8/10 0x413A73, 0xD 0x413C75, others 0x41415A
41415A..414192                   ;   +0xE04 follow-up (walks the real list from [edi+0Ch])
414198  cmp [esp+14h],ebp        ; LOOP TAIL: cursor != NULL -> head
41419C  jnz 4138E0
4141A2  epilogue
```

### Registers and slots at the hook, from the CFG abstract interpretation

- **Predecessors of 0x4138C6:** 0x412C7A (`jz`), 0x4138BA (`jnz`) and the fall-through of 0x4138C0. All three are at depth -0x3C.
- **At 0x4138B6 and 0x4138C6:** EBX = 0 and EDI = scene on every path. Pass 1 rewrites both in places, for example `mov edi,0Ah` at 0x4130F2 and `mov ebx,[ecx+8]` at 0x413092, but restores them before 0x4138B6. EBP is the pass-1 entity, and 0x4138D9 zeroes it.
- **No branch lands inside the hook.**
  - The in-function CFG has no edge into 0x4138C7..0x4138CA.
  - A byte scan of all of `.text` for E8/E9/0F8x/7x/EB patterns finds only the two known jumps to 0x4138C6.
  - No absolute 32-bit value 0x4138C6..0x4138CA appears anywhere in the file.
  - In the corpus, only 0x412C7A and 0x4138BA reference it.
- **Pass 2 is closed.** Every instruction reachable from 0x4138C6 lies in 0x4138C6..0x4141A9, and nothing branches back into pass 1.
- **Pass 2 has two exits**, and both lead to the epilogue 0x4141A2:
  - the empty-list `jz` at 0x4138CF
  - the loop tail 0x41419C, when the cursor is NULL (fall-through)

  The loop head 0x4138E0 is reached only from 0x4138DB and 0x41419C. There is no other RET and no tail jump.

Frame-slot accesses, from every ESP-relative operand normalised by depth. No `lea reg,[esp+x]` exists, so no frame pointer escapes.

| Slot | Pass 1 | Pass 2 |
|---|---|---|
| `[esp+14h]` cursor | W 0x412C76/0x412C95, R 0x412C86/0x4138BC | **W 0x4138CB, R 0x4138E0, W 0x4138EB, R 0x414198 only.** The body never touches it, so a cursor of NULL ends the loop after one iteration. |
| `[esp+18h]` | none | W 0x413D9F/0x413DBB/0x413F99 (case 0xD temporaries) before any read |
| `[esp+1Ch]` | none | W 0x413D34/0x413D4B/0x413FFF/0x4140C8 before any read |
| `[esp+20h]` | W 0x413309, R 0x413368 (pass-1 local) | **W 0x4138EF at the loop head**, then R 0x413D70/0x413EBE, W/R 0x413F65.. (0xD index) |
| `[esp+40h]` param_1 | R only | R only (never written anywhere) |
| `[esp+44h]` param_2 | **R only** (0x412C65, 0x412C82, 0x4138B6) | **first write 0x413D0D** (case 0xD: a value read through `[scene+0xFB0]`), R 0x413E61/0x413E98/0x4140D2/0x41410D |

What this means for the hook:
- `[esp+44h]` still holds param_2 at 0x4138C6.
- It must not be re-read inside the loop: case 0xD reuses it as a local, as the triage said.
- `[esp+18h]` and `[esp+20h]` are free at the hook. The loop head reads node+0 and node+8 exactly once, then rewrites `[esp+20h]` with the same value.

### Pass-2 body for a single entity

When the entity is the last list element, the original loop runs the body once with `[esp+14h] = NULL` and leaves at 0x414198. That is exactly the state the one-node list creates.

The searches inside the body all walk the real scene list from `[edi+0Ch]`:
- the attacker lookup by `+0xE10` (0x4139A9, 0x413AAE)
- the PvP team walk (0x413D2F)
- the party walk (0x413F80, 0x414020)
- the `+0xE04` follow-up (0x414164)

So a reaction still finds its source entity.

## 3. Why the triage's cave needed a gate

The triage's cave (0x4138C6, a one-node list whenever param != 0) is correct for the **field** catch-up. Its "Risk" paragraph assumed that the reactions skipped in an arena catch-up "get their reaction on the next main tick". In a room there is no main tick (section 1), so with the ungated cave:
- In room mode 1, every `FUN_00412c60` call has param != 0, and pass 2 would only ever see the call's own entity.
- An entity with a pending `+0x9DC` that is not a catch-up subject (`+0x163C == 0`) would keep it forever.
- The victim of a room player's hit would be processed in its own call, which may come later in the list or in the next frame, instead of right after the attacker. All room clients' lockstep order would change.

The field-mode gate removes all of that, and room behaviour stays byte-for-byte the original. The knock-report race does not exist in rooms anyway: the 0x0D builder runs only in the main tick (`FUN_0042e1b0` at 0x42CB2A, scene mode 6).

**Field only, and why the copy alone is enough.**
- In the field, the consumer copies a node's ae into the copy's `+0x9DC` and src into `+0xE10`, and with `[scene+0xF40] == 0` it zeroes the copy's `+0x9E0` (0x412BA6). So a copy is never an attacker in pass 1, and pass 2 has nothing to do for any other entity in the catch-up.
- With client-owned HP (`+0xF40 != 0`, not used by this server), a copy's pass-1 hit leaves its victims' `+0x9DC` pending. The next main tick consumes them after its builder runs. For the local player that is the wanted order: report, then react, each once. The attacker's `+0x9E0` is still cleared in pass 1 (0x413831).
- A copy that fails the field filter (type 3, `+0xEE4 == 0`, not local, scene mode 6, not PvP) is skipped by both passes, before and after the fix.

## 4. The patch

Hook at 0x4138C6: `8B 47 0C 3B C3` becomes `E9 35 36 0B 00` (`jmp 0x4C6F00`). The cave runs from 0x4C6F00 to 0x4C6F3B (60 bytes):

```
4C6F00  837C244400        cmp  dword [esp+44h],0     ; param_2 (only read so far)
4C6F05  742B              je   4C6F32                ; main tick -> original pair
4C6F07  8B442440          mov  eax,[esp+40h]         ; scene (param_1, never written)
4C6F0B  8B80B40F0000      mov  eax,[eax+0FB4h]       ; room
4C6F11  83787C00          cmp  dword [eax+7Ch],0     ; field?
4C6F15  751B              jne  4C6F32                ; PvP room -> original pair (full walk)
4C6F17  8B442444          mov  eax,[esp+44h]
4C6F1B  89442420          mov  [esp+20h],eax         ; node+8 = the copy
4C6F1F  C744241800000000  mov  dword [esp+18h],0     ; node+0 = next = NULL
4C6F27  8D442418          lea  eax,[esp+18h]         ; head = &node (never 0)
4C6F2B  85C0              test eax,eax               ; ZF = 0 -> 0x4138CF does not exit
4C6F2D  E999C9F4FF        jmp  4138CB
4C6F32  8B470C            mov  eax,[edi+0Ch]         ; the original two instructions
4C6F35  3BC3              cmp  eax,ebx
4C6F37  E98FC9F4FF        jmp  4138CB
```

**What the cave touches.**
- It writes only EAX and EFLAGS: no push, pop or call, and no ESP change.
- On the one-node path it also writes `[esp+18h]` and `[esp+20h]`.
- It uses `test eax,eax`, so it does not rely on EBX = 0, even though that is proven.
- It reads the scene from `[esp+40h]`, which is never written, rather than from EDI, even though EDI = scene is also proven.

**Choice of cave.**
- The live exes use 0x4C6200..0x4C6214 (smooth) and 0x4C6220..0x4C689F (combo HUD). The bytes 0x4C68A0..0x4C6FFF are zero in the pristine and both live exes, inside `.text` raw data, which ends at 0x4C7000.
- 0x4C6F00 leaves the combo HUD 0x660 bytes to grow.
- `combo_hud_2009.RESERVED` now lists `(0x4138C6, 5)` and `(0x4C6F00, 0x3C)`, so `combo_hud_2009.apply()` refuses any growth into them.
- `patch_2009.apply_knock_fix()` refuses to run unless:
  - both ranges are in RESERVED
  - neither overlaps any other RESERVED range, the combo cave extent (from `apply()`'s return) or the combo hooks
  - the hook holds its original bytes
  - the cave is all zero and inside `.text` raw data
- Only then does it write.
- `.text` VirtualSize is raised with `max()` after the smooth and combo blocks, because the smooth block writes it from a stale pefile value.

**Options.** The fix is on by default. `--no-knock-fix` skips it. `patch_2009.py` now also rejects unknown `--options`: before this change, a mistyped flag such as `--no-knockfix` would silently have become the server address.

## 5. Verification (2026-10-05)

Test exes, built with explicit `--out` and default address 127.0.0.1 / name rule `level`, as the live builds use:
- `WindSlayer2009\WindSlayer_patched_f1test.exe`
- `WindSlayer2009\WindSlayer_p2_f1test.exe` (`--p2`)

**`verify_f1.py`: all PASS for both exes**
- **Diff against the live exe:** 59 bytes in 7 runs, all inside the allowed regions:
  - the hook (5 B)
  - the cave (52 of its 60 bytes are non-zero)
  - `.text` VirtualSize at file 0x218..0x219 (2 B), 0xC58A0 → 0xC5F3C
- **Everything else is unchanged:** all other section header fields, the 0x4C68A0..0x4C6EFF gap and the tail after the cave.
- **Hook:** decodes as `jmp 0x4C6F00`, exactly 5 bytes.
- **Cave:** decodes to exactly the 15 intended instructions (60 B).
- **Branch targets:** `je`/`jne` go to 0x4C6F32, and both `jmp`s go to 0x4138CB. 0x4138CB..0x4138DF is the original code.
- **Stack:** the CFG of the function plus the cave (1346 instructions) has a consistent depth at every merge point. All 15 cave instructions run at -0x3C, the same as the hook and 0x4138CB. The single RET 8 is at depth 0. 0x4138CB is reached only from the two cave `jmp`s, and no instruction starts inside the hook.
- **Callers:** still only 0x42CB56 and 0x42CC94.

**`emu_f1.py` (unicorn): all PASS, both exes, 4 list orders**

Setup:
- The entities are:
  - P: local player, contact knock 6 from M
  - B: copy, holding a node, replayed knock 6
  - C: copy, holding, hurt 7 from P
  - D: idle copy that fails the field filter
  - M: monster, hurt 7 from P
- `FUN_004194f0` is stubbed and logged (RET 0x20). Any other escape from the function is a failure.

| Run | Live exe | F1 exe |
|---|---|---|
| param 0, field (main tick) | reactions P, B, C, M | identical heap, stack, calls, registers; path `cmp/je → original pair → jmp` |
| param B, field (catch-up) | P, B, C, M: **P's knock consumed (the bug)** | **B only**; P keeps `+0x9DC = 6` for the builder, M keeps 7 |
| param C, field | P, B, C, M | C only |
| param D, field (filtered copy) | not run | nothing |
| param B, PvP room | P, B, C, D, M | identical to live (heap, stack, calls, registers) |

In every run: ESP balanced on return, EBX/ESI/EDI/EBP preserved, and the single return reached.

**`flag_check.py`: all PASS.** `patch_2009.main()` runs in memory with every write intercepted:
- the default build equals `_f1test.exe`
- the `--p2` build equals `_p2_f1test.exe`
- `--no-knock-fix` equals the live `WindSlayer_patched.exe`
- `--p2 --no-knock-fix` equals the live `WindSlayer_p2.exe`

## 6. Live test and what is not covered

- **Live-tested 2026-10-05** with both 2009 exes rebuilt with F1, against the live server:
  - targeted repro: A idle in Monkey Soldiers while B taps left and right every 60-210 ms near A (A's copy of B queued 2+ nodes deep in 21 % of samples): A 255/255 knocks reported, 0 lost; B 262/262;
  - two-player free fight (D1 re-run): 0 of 234 knocks lost (before F1: 4 of 162); every settled player rest 0.0 px; every monster rest within 7.5 px; no client error in 40 minutes;
  - combo HUD, name colours and the no-wander-swing rule unchanged.
- **A PvP room smoke test is advised**, even though room behaviour is proven unchanged.
- **Possible side effect on monsters.** A monster's fresh `+0x9DC` (a hit by the local player in the frame's last tick) is no longer consumed one tick early by a catch-up. This is the triage's "doubled hurt slide" note. Watch the monster-rest numbers.
- **The 2008 exe was not checked.** It probably has the same pass.
- **Shipped.** `client_2009/patch_2009.py` and `combo_hud_2009.py` in the public repo carry F1 (on by default, `--no-knock-fix` to skip).
