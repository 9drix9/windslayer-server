# Position sync between two clients: EN 2009 client, static RE, 2026-09-28

**Report.** "I also see a different position on the clients, out of sync after dashing and hitting." There were two EN 2009 clients: A (TestHero) and B (test).

**Method.** Static RE only:
- Ghidra corpus `re_tools/corpus_2009`, decomp and asm
- server code in `WindSlayer2Game/server_live` (read only)
- PySlayer

Nothing here was tested live. Unless a VA is marked otherwise, VAs are 2009 and entity offsets are 2009.

## 0. Bottom line

**Both players and monsters desync, for different reasons.**

### Players
- The observer's copy is driven purely by input replay:
  - one S2C 0x1B node per C2S 0x0D
  - the copy runs its own state machine and physics
- A dash, an attack and a skill cast are only input words. Motion 6, 1, 5, or 7 plus a variant all ride the wire, so the copy re-derives them with the same code.
- Replay is lockstep only while the sum of node holds equals the mover's simulated time.
- **The server breaks that.** `presence.relay_fields` (presence.py:252, `START_HOLD_MS`) clamps the hold to 30 ms for the first non-idle node after any idle-word node.
  - The client also sends idle-word packets while its entity is still busy: an attack or cast animation after the key is released, dash end, hurt slide, airborne. It sends them every 210 ms (builder `0x42E24F` / `0x42E25B`).
  - So each "release key, then press again within 210 ms" costs the copy up to 180 ms of simulation.
- Every later input then reaches the copy while it is still finishing the previous animation. The state gates drop or shorten it:
  - dash needs state 8/0xC (`0x414887`)
  - attack needs 8/0xC/6
  - skill cast needs 8/0xC/6, and is on the wire for only about 1-2 ticks
- Result per occurrence:
  - a dash up to about 120 px short
  - a walk up to about 45 px short
  - or a skill or lunge dropped entirely on B
- The error stays until the mover's next **landed** hit. That hit's interaction tail carries A's exact x,y and snaps B's copy.
- This matches "after dashing and hitting". Walking alone stays in sync.

### Monsters
- Each client simulates its own copy.
- Before aggro, positions differ by design: the client wander AI is random.
- After aggro, the server's command words carry no position for the attacker. The 16-byte self-form 0x2A skips the position block (`0x459E2C`).
- Hit knockback happens only on the attacker's client (0x0D ie tail → `0x412F57`, `0x413BAF`).
- The watchers get a flinch that is position-synced but has no slide, at the monster's position from before the knockback.
- So every surviving hit leaves A's monster displaced relative to B's. The chase words then move both copies in parallel from different points.

## 1. Dash (double tap, skill 80): local execution, wire, and the observer

### Mover side (A)
1. **Keyboard.** `FUN_0042c310` runs once per frame from the tick `0x42C920`.
   - It counts left/right press and release edges in `param_2[4]` / `[5]`, with a 150 ms window (`0x42C59E` / `0x42C5CF` / `0x42C60D` / `0x42C63D`, `CMP 0x96`).
   - Once the count is above 2, and no action key is held, it checks `FUN_004281b0(0x50)` against the local player's learned list at `+0x2AE` (`0x42C8B4`).
   - It then writes motion `scene+0x259 = 6` (`0x42C8C4`) and direction `scene+0x258 = 2 / 6` (`0x42C8D7` / `0x42C8E6`).
   - The counters reset when the render state `+0x166C == 7` (`0x42C903`). `FUN_004249e0` shows that the render state is the action state, so 7 is dash end. They also reset on any action key.
   - So motion 6 is held until the dash ends: quick release, count 4. It is held less if the second press is released more than 150 ms after its edge.
2. **UDP loopback.** `FUN_00424500` sends UDP 0x03 `{u32 uid, u8 motion*10+dir, u8 sub*10+vb}` to 127.0.0.1 every tick, then resets `+0x258/+0x259/+0x25A`.
   - `FUN_0045e230` (network thread, under CS `+0x368`) → `FUN_00424ae0` writes the entity's `+0x93F..+0x942` (`0x424BC2..`).
3. **State machine** `FUN_00414210`:
   - The motion switch, case 6 (`0x414887`): `FUN_004281b0(0x50, type)` against **the entity's own** `+0x2AE` list. If the state is 8 or 0xC, go to state 5 (wind-up). The duration is `FUN_00422710`, the anim length.
   - State 6 continues while `+0xE24 < 500` **and** `+0x940 == 6` (`0x4150F0` / `0x4150F6`). Otherwise it goes to state 7, the dash end.
4. **Movement** `FUN_00415f80`:
   - The base step is `tick / +0x1280` (`0x4163EE` / `0x4163FD`).
   - `+0x1280` is 4.0 for players (`FUN_0041b830`, `0x41B84F`), times item and buff speed options (`FUN_0041d640` case 4, `0x41D7B6`). That gives 7.5 px per 30 ms tick.
   - State 6 multiplies by 2.7 (`0x416403`), about 20 px per tick, 675 px/s. About 340 px over the 500 ms cap.
   - vb (`+0x942`) scales every mode: ×1.25 / ×1.15 / ×0.85 / ×0.5.
5. **C2S 0x0D** (builder `FUN_0042e1b0`, Send `0x42E704`):
   - It is sent immediately when the input bytes change: lo = dir | 6 << 2, which is 0x19 (left) or 0x1A (right).
   - hi = `+0x9E8 & 0xFFF`.
   - A dash carries no tails: ae and ie are 0. **No position.**

### Observer side (B, type-3 copy)
- **0x1B handler** `0x45712B`:
  - builds a 0x58-byte node: hold, direction, motion, sub, vb, action, reaction, hi12, facing2
  - sets `+0x963 = 1`
  - appends the node to `+0x13F0`
- **Consumer** `FUN_004129f0`, for a type-3 copy:
  - If hold is 0 and a node is queued, it **loads** that node's hold without applying it (`0x412A5E..0x412AA3`).
  - Later ticks count down (`0x412AA8`).
  - At 0 it applies the node (`0x412AFC..0x412C29`).
  - An entity that is fully idle (state 8, neutral inputs, `+0x9E0` and `+0x9DC` both 0) has its hold zeroed (`0x412ABC..0x412AF6`).
- **Tick gates.** Hit resolution, state machine, movement and hit detection all skip a remote type-3 copy while its hold is 0 (`0x412CB1`, `0x414278`, `0x415FEF`, `0x416B0D`). So:
  - node k's input is simulated for exactly hold(k+1)/30 ticks
  - the copy is frozen while its queue is empty
- **Catch-up.** A copy with more than one queued node gets one extra full tick per frame (`0x42CC1F..0x42CCBD`). This drains backlog faster. It does not change what is simulated.

### Answer
The dash is **not** on an unrelayed path. Motion 6 is on the wire, and the copy runs the same case-6 / state 5-6-7 code at the same speed.
- It uses its own `+0x1280`, computed from the record's equipment and buffs.
- It uses its own skill list: TestHero's record carries 80, from `accounts.json` skills [260, 94, 80, 194].

The copy dashes the same distance only if motion 6 reaches it in state 8/0xC and the next node, the one that clears motion 6, comes at the same simulated time. That is exactly what the hold clamp breaks (section 6, R1).

## 2. Attacks and moving skills

**Input words.** All of these go through the same UDP-loopback → builder → 0x1B relay path.

| Motion | Meaning | Skill check at `+0x2AE` |
|---|---|---|
| 1 | attack / combo | none |
| 2 | guard | 90, at `0x414EFB` |
| 3 | jump | double jump 94, at `0x414647` |
| 4 | drop | none |
| 5 | strong attack | 88, at `0x414A4A` |
| 6 | dash | 80 |
| 7 | skill cast | variant in `+0x941` |

For motion 7, `FUN_0044f070` sets `scene+0x259 = 7` and `+0x25A = variant` when it sends C2S 0x15 (`0x44F2C2..`). `FUN_004511b0` clears it as soon as the local render state is a skill pose, 0x18..0x22 (`0x4512F6`). So the cast words are on the wire for only about 1-2 ticks. The live capture showed idle words 60 ms later.

**Which attacks move the body.** The movement gate `FUN_00415f80` covers:
- states 6, 0xC, 9, 0x13, 0x17, 0xE, 0xF, 0x11
- any state with `+0x95B != 8`
- a list of `(+0x98B variant, +0x118 class, +0x119 tier, +0x11A weapon type, +0xE9C timer)` windows

The cases are:
- **Plain combo swings** (state 4 from standing) do **not** move.
- **Dash attack / dash strong attack.**
  - Motion 1 or 5 while in state 6 sets `+0x95B = facing`.
  - For motion 1, `+0xE9C = 0x564` and `+0x979 = 2`, the final combo stage.
  - The slide runs at walk speed for about 150 ms, until `0x4158A3` / `0x415987` clears `+0x95B`.
- **Skill lunges** (state 1):
  - Weapon type 2: 2.7× (variants 6/8), 2× (7), or ×2 forward until the hit lands and then ×-2 recoil.
  - Weapon type 1, the Warrior / Berserker line: variant 5, 2.7× between 250 and 500 ms until the hit lands; variant 8, 2.7× below 250 ms.
  - Variant 5 with weapon type 5: 12.5×.
  - "Until the hit lands" is `+0x965`. Hit-stop in states 0xE/0xF is `+0x96B = 1`.
- **Combo advance.** Motion 1 held in the E9C windows 410..610 and 1050..1250 advances the combo (`0x41588x..0x41592D`).

**Replay on the copy.** Every input is carried:
- class and tier come from the 0x04/0x05 record
- weapon type is recomputed from the record's equipment grid (`FUN_0041b830`)
- the variant is in lo bits 5-8
- the timers are simulated

The "hit landed" flag is replayed too. The mover's hit report (ie 7) → node reaction nibble → `+0x977` (`0x412BA0`) → `+0x965 = 1` on the copy's next simulated tick (`0x41385D`). That is the same logical tick as on the mover, so lunge stop, recoil and hit-stop match.

**What does not replay.** Each accepting state is checked only on simulated ticks:
- case 1: state 8/0xC/6, or 9/0x13 in the air
- case 5: 8/0xC/6
- case 7: 8/0xC/6 with `+0x941 != 0`

If the copy is still in the previous animation because it lost time, the input is ignored:
- A 1-2 tick cast or a quick attack tap is **dropped entirely** on B.
- A dash starts late and is then cut short by the node that clears motion 6.

Skill-specific hit effects are also not replayed. `+0x95D` (effect 4 = long knockback ×`+0x13E8` for 600 ms) and `+0x98C` come from `FUN_004194f0` with the attacker's variant. The 0x1B relay does not carry that variant: node+0x52 is 0.

## 3. Knockback: who moves whom, and what is on the wire

**Detection is local to the victim's client** (`FUN_00416ab0`):
- Monster contact (`0x416BF2..0x416E18`) and monster attacks (`0x4172ED..`) are checked only against the local player (`0x416EFF..`).
- The local player's swings only hit when the attacker is local (case 4/0xE path).

**A local player hit by a monster:**
- Local effects:
  - player `+0x9DC = 6/7/8`
  - `+0xE10 = mob`
  - `+0x95C = mob facing`
  - `+0x9E8 = 250` for contact
  - The mob's `+0x9E0` case in `FUN_00412c60` (`0x412DE4`) sets player `+0x95B = mob facing` and `+0x9E4`.
  - Then state 3, or 0x17 airborne for 8/10.
  - Movement slides the player at walk speed in the `+0x95B` direction for about 90-150 ms (`0x414FF7` clears it), about 22-37 px. There are 270 ms of i-frames (`0x41531A`).
- **On the wire:**
  - ae (lo 12-15) and event_source_uid in the next 0x0D
  - facing2 (lo 20-21) = `+0x95C`
  - hi12 = `+0x9E8`
  - **no position**
- On B: the copy's hit resolution copies `+0x95C → +0x95B` and `+0x9E8 → +0x9E4`, because `+0x963` was set by 0x1B (`0x413BAF..0x413BCD`). **So the player knockback does replay**: same direction, same duration, same speed.

**A local player hitting a monster:**
- Attacker side: `+0x9E0 = 7`, `+0xE08 = mob`, `+0x13C8/+0x13D0 = mob − attacker`.
- Mob side: `+0x9DC = 7/8`, hit-lock `+0x96E = 1`.
- Resolution:
  - mob `+0x95B` = attacker facing (`0x412F57`)
  - state 3 slide at the **mob's** speed, about 90-150 ms
  - it stays hurt until the server's 16-byte 0x2A clears the lock (`0x459F04`)
- The attacker's body does not move, except through the lunge-stop, recoil and hit-stop flags above.
- **On the wire:** the 61-byte 0x0D ie tail. It carries the attacker's exact x,y and the mob delta, but no knockback.
- Server (MOB_AGGRO on):
  - to the attacker: a 16-byte 0x2A (no position)
  - to watchers: `_relay_hit` 0x2A `{lo 7<<12, target = attacker, pos = the server's point}` (windslayer_server.py:5472)
- Watchers:
  - facing2 is 0 → node+0x50 = 8 → `+0x95B = 8`
  - hi is 0 → `+0x9E4 = 0`
  - so they get a one-tick flinch with **no slide**, at the position from before the knockback
  - `+0x9E0` is zeroed on copies in the field (`0x412BA6`), so B's copy of A never hits B's monster itself

## 4. Which C2S packets carry the mover's real x,y

- **C2S 0x0D interaction tail only** (bits 16-19 not 0). Builder `FUN_0042e1b0`:
  - u32 `+0xE08`
  - f64 `+0x1298` / `+0x1328` (the logic x,y)
  - f64 `+0x13C8` / `+0x13D0`
  - bool `+0x967`, bool `+0x974`
  - u32 `+0xE3C`
  - u8 `+0x975`
  - plus u16 `+0x960` if ie is 0xC
  - That is 43 or 45 bytes. The 61-byte hit report is 18 + 43.
- **For the local player, ie is set only when an interaction lands:**
  - 7: swing hit
  - 9: skill or grab hit
  - 2 / 4: hit on a guarding target
  - 0xC: trap
  - That is once per tick with a hit, **never periodically**.
  - Whiffs, walking, dashing, jumping and being hit (ae only) carry no position.
- **C2S 0x15**, ground-point skills 0x0A31..0x0A3B only: u16 of the **drawn** position `+0x165C` / `+0x1660` (`0x44FC2D` / `0x44FC49`).
- C2S 0x88 guild board placement.
- Nothing else. UDP 0x03 goes to loopback only and has no position. The UDP snapshots with positions (`FUN_00424500`, types 7/8/9) are room-host only.

## 5. S2C packets that can correct an observer's copy

**0x2A** (handler `0x459C3D`, shared with 0x9E/0x9F):
- **When mover state != 0x10 and target != receiver**, it writes `+0x1298/+0x1328/+0x966` **immediately** for any entity type (`0x459E42..0x459E89`).
- It also:
  - flushes the queue (`0x459E93..0x459EBF`)
  - sets `+0xEE4 = 0` (`0x459ECB`)
  - sets the node hold to 960 (`0x459ED5..0x459EE7`)
  - sets `+0x971 = 1` (`0x459C89`) and `+0x963 = 1`
- **For a type-3 copy**, the flush throws away pending relay nodes. The copy then simulates its **current** input for up to 960 ms before the 0x2A words apply, unless the idle check zeroes the hold.
- So 0x2A is clean only for an idle copy. On a moving one it costs up to about 1 s of timeline.
- `+0x971` has no field effect on players. The name-box patch path is type 4 only (`0x43C3BA`); the tick gates test it only for type 4.

**0x1B with a reaction nibble and a position:**
- The position is applied **in-stream at dequeue**, for types other than 4 (`0x412B36..0x412B50`). The hold and the timeline are kept.
- Side effects:
  - `+0x977 = nibble` (`0x412BA0`). Only 2/4/7/9 do anything: `+0x965 = 1`, plus hit-stop in 0xE/0xF (`0x41384D`).
  - `+0x9E0 = 0` in the field
  - `+0xE3C` and `+0xE08`, `+0x13C8/D0`, `+0x967`, `+0x975`, `+0x960`, `+0x974` all become 0
- So nibble 1, 3, 5, 6 or 8 on the mover's real words is a **silent, timeline-safe teleport**. This is the best correction carrier.

**Other carriers:**
- 0x9E/0x9F: no position.
- 0x06 + a fresh 0x05: a respawn at the server's estimate, and the entity is recreated.

**No smooth correction exists.**
- `FUN_004510a0` → `FUN_004511b0` queues one drawn-position lerp segment per frame at `+0x1640`, capped at 100 ms.
- `FUN_0042c150` plays it back into `+0x165C/+0x1660`.
- So any logic snap renders as a one-frame glide.

**Retail KR (PySlayer) evidence:** none.
- PySlayer is single-player. `parse_0D` ignores the move.
- The imports for opcodes 0x1B and 0x2A are wrapped in try/except and the files are absent.
- It never sends 0x04/0x05 to peers.
- The only retail hint is the client grammar itself: the 0x1B position rides only with a reaction nibble. That suggests retail also relayed positions only on interactions and relied on lockstep replay.

## 6. Root causes, and what to do

**R1 (server, players, primary): the `START_HOLD_MS` clamp, presence.py:252.**
- Problem: it drops real simulated time whenever the previous node was idle words sent while the entity was busy.
- Proof that the fix is safe:
  - A busy entity sends a packet at least every 210 ms (`0x42E24F`, `0x42E25B`).
  - Only the client's *final* idle packet (state 8, `0x42E288`) can be followed by a gap longer than 210 ms.
- Fix: apply the clamp only when `logic_elapsed_ms > 210`, and relay the real elapsed otherwise.
  - The client's idle check already zeroes holds for a truly idle copy, so the clamp only saves one tick.
  - Then re-check the "walk starts about 1 s late" case that motivated it.
- Test: on A, tap attack, release, then double-tap dash within about 150 ms, and repeat. Compare A's x (local) with A's copy x on B, using wsview `state` on both.
  - Expected now: 20-120 px drift per repeat.
  - Expected after the fix: under 10 px.
- `test_presence.py:458` and `test_combatsync.py:238` pin the old holds.

**R2 (players, by design): nothing periodic carries position.**
- After R1, lockstep should hold.
- For robustness, the server could append a neutral nibble (for example 1) plus a position to the *next* relayed 0x1B. It needs an exact source, though:
  - the dev memory driver
  - or a client "position beacon" patch that makes the builder append the ie tail every N sends without setting `+0x9E0`
- The server's own estimate is not good enough; see R4.

**R3 (server, monsters): the attacker's copy is never position-corrected, and knockback is local to the attacker.**
- Options:
  1. Send `_relay_hit` with facing2 = the attacker's facing (lo bits 20-21) and hi12 = the hurt ms. The watchers' `0x413BAF` block then copies them into `+0x95B` / `+0x9E4`, so the slide happens there too. Add the slide to the server's estimate.
  2. Send periodic 33-byte 0x2A with target 0 and the server's point to every client that is not the authority, at the keep-alive rate.
- A single 0x2A cannot both clear the attacker's hit-lock (it needs target == receiver) and carry a position.

**R4 (server estimate): `presence.advance` is dash-blind.**
- `MOVING_MOTIONS = (walk, jump)` (presence.py:161). It ignores dash, lunges, knockback, vb and item speed.
- Effects:
  - `mobai.decide` chases a stale point after a dash
  - spawn records (0x04/0x05) and keyframes put copies at that stale point
- Fix: dead-reckon motion 6 at 2.7× walk for 500 ms after the wind-up, or accept the error until the next ie fix.

## 7. Open questions (need a live check)

1. Did live data ever show the "1 s late start" with real elapsed holds? The idle zeroing at `0x412ABC` suggests it should not happen. A stuck non-zero `+0x941`, `+0x942`, `+0x9DC` or `+0x9E0` on the copy would explain it.
2. What are the real wind-up length (state 5 anim) and dash distance at TestHero's speed? The static model gives about 340 px plus the wind-up.
3. How large are the per-hit monster knockback offsets? Check with wsview on both clients while A hits a chased Ssiyo.
