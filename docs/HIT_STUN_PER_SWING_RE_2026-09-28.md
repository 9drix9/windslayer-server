# Monster hit stun and slide per hit kind (EN 2009 client), static RE, 2026-09-28

**Question.** When a player's own client catches a hit on a monster, how does that client set the monster's stun (`+0x9E4`) and slide? What must the server send to the other clients (watchers) so their copies do the same? This matters for the three hit kinds that live session 2 tested: a basic swing, a dash attack and Ice Spear.

**Method.** Static RE only.
- Ghidra corpus `re_tools/corpus_2009`, decomp and asm. The jump tables of `FUN_00422710` were read from the exe bytes.
- The motion table `hs/body001.hsi` and the effect table `hs/basic.hsi`, decoded in memory into the scratchpad. The game installs were not written.
- The 2009 `hs/windslayer.hii`, read through `server_livefix/en_content.py`.
- The live session 2 data in `scratchpad/livetest/desync2/s2`.

VAs are for the 2009 client and entity offsets are 2009. Scratchpad scripts: `scratchpad/hitstun/{dec_hsi.py, motions.py, skilltable.py}`.

## 0. Bottom line

1. **What `FUN_00422710(attacker)` returns.** It returns the **total** length in ms of the attacker's current action. The caller subtracts the attacker's elapsed time `+0xE9C`. So the monster's `+0x9E4` is **what is left of the attacker's action at the hit tick**:
   - `hurt = L − E`, where L is the action length and E is the attacker's elapsed time at the hit.
   - If E > L (the unsigned result is larger than L), the client uses L instead (`0x412F34..0x412F4A`).
2. **Where L comes from.**
   - The player motion table `hs/body001.hsi`. Sprite n is state n, and its length is the sum of its frame `delay:` values.
   - Some values are fixed in the code: combo cut-offs 610 and 1250, and special cases for some skill variants.
3. **All three live values are reproduced exactly:**

   | Hit | L | E | hurt |
   |---|---|---|---|
   | Basic swing | 610 (fixed combo stage-0 end) | 120 | **490** |
   | Dash attack | 2140 (sprite 4 total) | 1380 (the dash attack sets E = 0x564) | **760** |
   | Ice Spear | 1290 (sprite 1 total) | 270 | **1020** |

4. **What the monster does with it.** The monster's action byte `+0x9DC` is the attacker's tail byte `+0x975` (the tail's `u8 target_action_event`).
   - **7 / 8 (swing):** `+0xE9C` starts at 0x3C. The monster slides **2 ticks** and stays stunned for `hurt − 60`.
   - **9 / 10 (skill or strong attack):** `+0xE9C` starts at 0. The monster slides **4 ticks** and stays stunned for `hurt`.
   - **Element of the skill:** `FUN_004194f0` sets `+0x95D` to the Attribute of the skill the attacker has learned:
     - **2 (ice):** 1000 ms more stun, and the frozen render.
     - **4 (wind):** the slide lasts until `+0xE9C` reaches 600, which is 19 ticks, at walk step × `+0x13E8`.
5. **Ice Spear's 2 s is the hurt plus the ice element, not a debuff.** It is 1020 + 1000. Ice Spear has Con 0, no debuff slot is inserted, and `FUN_00426840` does nothing for a Warrior with variant 1.
   - Today the watcher gets nothing for the freeze. The relay is action 7 with hi 0: a one-tick flinch, and `+0x95D` is cleared.
6. **The watcher packet.** The 0x2A handler reads `u8 action_flag` for action 9 or 10 into node+0x52. The consumer copies node+0x52 to the monster's `+0x98C`, and that byte is exactly the attacker's `+0x98B` (the cast variant), which the attacker's own client uses.
   - So a **34-byte** 0x2A with action 9 and flag = variant replays on the watcher everything the attacker's client runs in pass 2:
     - the 4-tick slide
     - the stun from hi
     - the element (`+0x95D`), taken from the watcher's copy of the attacker's learned skill list (0x04/0x05 carry it into entity `+0x2AE`)
     - the local debuff from `FUN_00426840`
   - This unblocks M3. The "variant → `+0x98B`" blocker in `desync_fix_plan.md` §7 is solved:
     - `+0x98B` = `+0x941` at state-1 entry (FUN_00414210 case 7, line 450).
     - That is lo bits 5-8 of the attacker's motion-7 cast words.
7. **Neither live failure is a relay-format problem. Both come from the chase gate being shorter than the attacker's stun.**
   - **Dash attack:** the stun is 700 ms, but the gate was 450 ms. The attacker's copy therefore started 250 ms late, which at 125 px/s is about 30 px.
   - **Ice Spear:** the stun is 2020 ms, and there was no gate. The chase started 222 ms after the report, so 1.8 s × 125 px/s ≈ 225 px, plus 15 px of slide, which is +245 px.

## 1. `FUN_00422710(entity, scene)` (`0x422710`, fastcall ECX = entity, EDX = scene)

### 1.1 What it is
- **Monsters** (type 4): the length of the monster's current action, taken from its own `hni`/`hsi` (not needed here).
- **Players** (type 3):
  - It switches on the state `+0x994`.
  - Inside that, it switches on the weapon type `+0x11A`.
  - Some cases also depend on the variant `+0x98B`, the tier `+0x119`, the combo stage `+0x979` and the hit-landed flag `+0x965`.
- **Table lookups** use `scene+0xFA4`, a `CCharControl` object (`FUN_004016a0`). Its `CPList` at `+0x1B8` (head `+0x1BC`, count `+0x1C4`) holds one motion per sprite of **`./hs/body001.hsi`**. The loader is `FUN_00402a10` and each frame is added by `FUN_00403280`:
  - motion `+0x44` = Σ frame `+0x48`, and frame `+0x48` is the `delay:` token (0x3EB)
  - `FUN_004033a0` with EAX = k returns list entry k (1-based)
  - the fixed-index paths return entry k+1 for the index k they start from
- **Weapon type** `+0x11A` is the index of the last non-zero Job flag of the equipped weapon (Kind 0xB, `FUN_0041b830` 0x41B9xx). So:
  - 1 Warrior, 2 Monk, 3 Archer, 4 Rogue, 5 Mage, 6 Priest weapons
  - 0 means no weapon (Job[0] only)

### 1.2 body001.hsi motion totals (2009)

T[n] is sprite n, which is state n. Frame starts are in ms.

| n | T[n] | Frames (start) | Used for |
|---|---|---|---|
| 1 | **1290** | 250 · **250** · 330 (600) · 930 · 1010 · 1090 | state 1 skill (wt 0/1) |
| 4 | **2140** | 0 · **100** · 180 · 360 · 440 · 640 · **740** · 820 · 1000 · 1080 · 1280 · **1380** · 1460 · 1660 · 1740 | state 4 combo (wt 0/1). The swing frames are bold. |
| 5 | 50 | – | state 5, dash wind-up |
| 7 | 150 | – | state 7, dash end |
| 14 | 640 | – | state 0xE, air attack (wt 0/1) |
| 15 | 1290 | – | state 0xF, air skill (wt 0/1) |
| 23 | 1040 | – | state 1, wt 1 variant 5 |
| 24 / 28 / 32 / 36 | 1390 / 1240 / 1240 / 1490 | – | state 0xF, wt 3 / 4 / 2 / 5-6 |
| 25 / 29 / 33 / 37 | 720 / 640 / 540 / 740 | – | state 0xE, wt 3 / 4 / 2 / 5-6 |
| 26 / 30 / 34 / 38 | 1240 / 1240 / 1490 / 1390 | – | state 1, wt 4 / 2 / 5-6 / 3 |
| 27 / 31 | 2140 / 2140 | – | state 4, wt 4 / 2 |
| 35 / 39 | 740 / 720 | – | state 4, wt 5-6 / 3 (a single shot, no stage) |

### 1.3 The length L for each attacker state (from the jump tables in the exe)

The case tables are at `0x422E78` (by state), `0x422E9C` (state 4), `0x422EB0` (state 1), `0x422EC8` (0xE) and `0x422EDC` (0xF).

**State 4** (ground combo):

| Weapon type | Stage 0 | Stage 1 | Stage 2 |
|---|---|---|---|
| 0 / 1 | 610 | 1250 | T[4] = 2140 |
| 2 | 610 | 1250 | T[31] = 2140 |
| 4 | 610 | 1250 | T[27] = 2140 |
| 3 | T[39] = 720 | – | – |
| 5 / 6 | T[35] = 740 | – | – |

**State 1** (skill cast, strong attack):

| Weapon type | Length |
|---|---|
| 0 / 1 | T[1] = 1290. With variant 5 and weapon 1: T[23] = 1040. |
| 2 | variant 5: FX 0xA4 (§1.5)<br>variant 6: tier 1 → 250 before the hit, **1640** at the hit; other tiers → 1200<br>variant 8, tier 1: 250 / **3300**<br>variant 9, tier 2: 600<br>otherwise T[30] = 1240 |
| 3 | variant 8, tier 1: T[38] + 350 = 1740<br>variant 10, tier 1: T[38] + 700 = 2090<br>otherwise T[38] = 1390 |
| 4 | variant 5, tier 1: 700<br>otherwise T[26] = 1240 |
| 5 | variant 7, tier 1: FX 0xFF (§1.5)<br>otherwise T[34] = 1490 |
| 6 | T[34] = 1490 |

The "at the hit" values apply because pass 1 sets the attacker's `+0x965 = 1` at `0x41385D` before it calls `FUN_00422710`.

**State 0xE** (air attack) and **state 0xF** (air skill):

| Weapon type | 0xE | 0xF |
|---|---|---|
| 2 | 540 | 1240 |
| 3 | 720 | 1390 |
| 4 | 640 | 1240 |
| 5 / 6 | 740 | 1490 |
| other | T[14] = 640 | T[15] = 1290 |

**Any other state s:** T[s].

### 1.4 E: the attacker's `+0xE9C` at the hit

**Tick order** (`FUN_0042c920`, `0x42CAB2..0x42CB80`):
1. keyboard
2. **builder `FUN_0042e1b0` (sends C2S 0x0D)**
3. consumer
4. hit resolution `FUN_00412c60`
5. state machine `FUN_00414210` (`+0xE9C += 30` first)
6. movement
7. hit detection `FUN_00416ab0`

**What follows from that order:**
- A hit caught on tick N with `+0xE9C` = E is reported by the builder on tick N+1. Pass 1 on tick N+1 then computes the hurt, still with E, because the state machine for N+1 runs after it.
- The input that starts the action is sent by the builder on the tick the state machine enters the action, with E = 0 on that tick.

So, from the attacker's C2S 0x0D stream:

> **E = E0 + Σ logic_elapsed_ms(packets after the action-start packet P, up to and including the hit report H) − 30**

- E0 = 0 for a swing or skill from standing.
- E0 = 1380 (0x564) for a dash attack (§3).

**Check against the live data:**
- c01: the swing words `lo 0x200004` came at 54.861 and the report at 55.011. The logic time was idle 60 + report 90 = 150, so E = 120.
- s02 Ice Spear: the cast words `lo 0x20003c` came at 15.800 and the report at 16.099, so E = 300 − 30 = 270.
- Dash attack: the report comes one tick after the attack words, so E = 1380.

**Where the swing frames fall.** Hit detection uses the attack rectangle of the current frame. `FUN_0041dc70` (and `FUN_00402400`) look it up from the sprite's `col_region` at time E. So E also depends on the distance to the target. For the measured monkeys, every hit landed on the first tick of the second frame of the swing: sprite 4 `no 7`@100 → tick 120, `no 3`@740 → 750, `no 10`@1380 → 1380, and sprite 1 `no 3`@250 → 270. Those give the fallback defaults:

| Kind | E | hurt |
|---|---|---|
| Stage 0 | 120 | 490 |
| Stage 1 | 750 | 500 |
| Stage 2 or dash attack | 1380 | 760 |
| Skill or strong attack on T[1] | 270 | 1020 |

For other sprites, use the timeline value of E.

### 1.5 FX lengths (Monk Weakness Detection, Mage Nova)

These call `CSpriteControl::GetTotalFrameTime(scene+0xFAC, 0xA4 / 0xFF)`. `scene+0xFAC` is the global effect sprite control (`DAT_0054e844`, `FUN_0042b2b0`), and `./hs/basic.hsi` is loaded by `FUN_0043f140`.

If the index is 0-based and basic.hsi is the file loaded there, 0xA4 is sprite 165 = 710 ms and 0xFF is sprite 256 = 1490 ms. **Not verified**: my parse of basic.hsi read 239 of its 341 sprite headers. Use the timeline, or measure `+0x9E4` live.

## 2. Hit resolution per kind (`FUN_00412c60`) and what the watcher needs

### 2.1 The attacker's own client

**Pass 1** is keyed on the attacker's `+0x9E0`, the interact event of the catch.
- **Case 7, a swing** (`0x412F12..0x412F9x`):
  - mob `+0x9E4 = L − E`, clamped as in §0
  - mob `+0x95B` = attacker `+0x949`, the attacker's facing: 2 = left, 6 = right
  - mob `+0x9DC` = attacker `+0x975`, which is 7, or 8 when the monster was airborne (`FUN_00416ab0` 0x417Bxx: victim state 9/0x13/0xE/0xF → 8)
  - mob `+0x98C = 0`
  - mob `+0x964 = 1`
- **Case 9, a skill or strong attack** (`0x413431..0x413542`):
  - the attacker's `+0x965 = 1`, then the same `+0x9E4`
  - mob `+0x95B` = attacker `+0x949`, with two exceptions:
    - Rogue tier 1, variant 5: the direction is reversed
    - Mage tier 1, variant 7: away from the attacker by x
  - mob `+0x9DC` = `+0x975`, which is 9, or 10 when airborne (`FUN_004127d0`)
  - mob **`+0x98C` = attacker `+0x98B`**

**Pass 2** is keyed on mob `+0x9DC` (`0x413998..0x413BAF`).
- 9 / 10:
  - if `+0x98C` ≠ 0, expire the mob's status slots 0x1B3..0x1BD
  - then `FUN_00426840(scene, attacker, mob)` with AL = `+0x98C`
- 6..10:
  - expire the status slots 0xA3C..0xA46
  - `FUN_004194f0(mob, attacker, +0x9DC, +0x98C, 0…)`
- `LAB_00413baf`: if `+0x963 && !+0x964`, copy `+0x95C` → `+0x95B` and `+0x9E8` → `+0x9E4`
  - on the attacker this is skipped, because `+0x964 = 1`
- The start of `+0xE9C` and the next state:
  - 7 / 8: `+0xE9C = 0x3C`
  - 9 / 10: `+0xE9C = 0`
  - 8 / 10: state 0x17 (airborne, `+0x96B = 1`, `+0xEEC = 0`)
  - otherwise: state 3

**State 3** (`0x41524D..0x415444`) runs each tick after `+0xE9C += 30`.
- **The stun:**
  - It holds while the hit-lock `+0x96E` is set.
  - Otherwise it exits once `+0xE9C (+500 if vb 4 and 13D8/13E0 are set) ≥ +0x9E4`.
  - With **`+0x95D == 2` the limit is `+0x9E4 + 1000`**.
  - On exit, if the mob holds a status in 0x1B3..0x1BD, it goes to **state 0x15**, which is the stun control state (Rogue "Stun" family 435..444). Otherwise it goes to state 8.
- **The slide:**
  - It slides while `+0xE9C < 0x96`.
  - That gives **2 ticks from 0x3C** (90 and 120) and **4 ticks from 0** (30..120).
  - With `+0x95D == 4` it slides while `+0xE9C < 600` (19 ticks from 0). The speed is multiplied by `+0x13E8` (`FUN_00415f80` 0x4161xx).
- **State 0x17** has the same stun rule (`0x415449..`) and then lands.

**The stun in ms** is therefore:
- 7 / 8: `hurt − 60`
- 9 / 10: `hurt + 1000·[+0x95D == 2]`

This checks against the live values: 430 (412-425 measured), 700 (685-694) and 2020 (2006-2010).

### 2.2 `FUN_004194f0`: the element byte `+0x95D` (thiscall scene; victim, attacker, action, variant)

1. It returns early if the victim's HP (`+0xA0`) is below 1.
2. It normalizes the action: 5 → 4, 8 → 7, 10 → 9.
3. **It always clears `+0x95D` and `+0x13E8` first.** So a basic swing (action 7) removes a pending ice.
4. **For actions 4 and 9 only** it switches on the variant, the attacker's class (`+0x118`) and tier (`+0x119`) to get a family base id. The full map is in the scratch script `eff_base`, for example:
   - variant 1: Warrior 0x104 (Ice Spear), Monk 0x22C, Archer 0x150, Mage 0x200
   - variant 2: Warrior 0x10F, Archer 0x15B, Mage 0x20B; Monk and Rogue return early
5. `FUN_004281b0(base, 0)` finds the learned level in the attacker's list at `+0x2AE`. The family size is EDI = 10.
6. `FUN_00404750` then loads that hii record.
7. For action 9: **`+0x95D` = record `+0x190` (the hii `Attribute`)** when it is 1..4.
8. If `+0x95D == 4`, then `+0x13E8` = 1.0, or 2.5 for Mage tier 1 variant 7 (Nova).
9. **Deferred cases:** Monk tier 2 variant 9 (Cartilage Smash) and Rogue tier 2 variant 6 (Time Bomb) move the effect to `+0x95E`, set `+0x95D = 0` and store the damage in `+0xE20`. The effect is applied when the debuff detonates.

**Element meanings.**
- 1 fire: render flag 1 in state 3 (`FUN_004249e0`). It also sets the attacker's burn tracker `+0xE14`, which only works for the local player.
- 2 ice: 1000 ms more stun and render flag 2.
- 3: no effect in state 3.
- 4 wind: the long slide.

**HP.** HP is not changed in the field (scene `+0xF40 == 0` and `fb4+0x7C != 1`), so running this on a watcher is harmless. One exception: a tier-2 attacker with variant 9 who knows Vampiric Attack (0xB25) gets the HP drain applied to the attacker entity, even on a watcher's copy.

### 2.3 `FUN_00426840`: the local debuff (AL = `+0x98C`)

For some (variant, class, tier) combinations, this inserts a status slot on the mob through `FUN_004262b0(scene, mob, record Con, …, attacker uid)`. Examples:
- Rogue variant 2 → 0x1B3 Stun (Con 3000) → state 0x15 after the hurt
- Warrior tier 1 variant 7 → 0x8BB Weapon Shackle (Con 8010)
- Rogue variant 1 → 0x187 Poison

Ice Spear gets nothing. The full list is the "debuff" column in §5.

The server already sends S2C 0x41 for these (`_skill_debuff`) to the caster and to the watchers. The attacker's client thus also gets both the local insert and the 0x41, so the watcher ends up in the same situation.

### 2.4 The S2C 0x2A the watcher needs (handler `0x459C3D..0x459F91`)

**Wire order:**

| Field | Size | Note |
|---|---|---|
| `mover` | u32 | the monster (0x4C7108) |
| `lo` | u32 | the first 4 of 8 bytes read into `scene+0xF10` (0x4C70EC) |
| `hi` | u32 | the next 4 bytes |
| `target` | u32 | node+0x08 → `+0xE10`. The attacker. |
| `action_flag` | u8 | node+0x52 → `+0x98C`. **Only when (lo>>12)&0xF is 9 or 10** (0x459DF1..0x459E10, read through 0x4C70F0). Present in the self-form too. |
| `x` | f64 | only when target ≠ receiver and the mover's state ≠ 0x10 |
| `y` | f64 | same condition |
| `airborne` | u8 | same condition (0x4C70FC) |

**The `lo` bits:**

| Bits | Field | Node byte | Relay value |
|---|---|---|---|
| 0-1 | dir | node+0x4A | 0 → 8 (none) |
| 2-4 | motion | node+0x4B | 0 |
| 5-8 | variant of the monster itself | node+0x4C | 0 |
| 9-11 | vb | node+0x4D | 0 (vb 3 would stop the attacker from updating `+0x9E4`) |
| 12-15 | **action** | node+0x4F → `+0x9DC` | the tail byte |
| 16-19 | reaction | node+0x4E | 0; not used for type 4 |
| 20-21 | **facing2** | node+0x50 → `+0x95C` | 1 → 2 (left), 2 → 6 (right), else 8 |

**`hi` & 0xFFF** → node+0x44 → `+0x9E8`.

**What the watcher's copy then does:**
- The consumer (`FUN_004129f0`, type 4) applies the node at once.
- Pass 2 on the watcher runs exactly the attacker's pass 2:
  - `+0x963` is set by the handler at 0x459CB0.
  - `+0x964` is cleared at 0x412CA8, and only the attacker's pass 1 sets it.
  - So `LAB_00413baf` copies facing2 → `+0x95B` and hi → `+0x9E4`.
- Result:
  - **action 9 / 10:** `+0xE9C = 0`, a 4-tick slide and a stun of hi. `FUN_004194f0` sets `+0x95D` from `target`'s learned list, giving +1000 (ice) or the 19-tick wind slide. `FUN_00426840` adds the local debuff.
  - **action 7 / 8:** `+0xE9C = 0x3C`, a 2-tick slide, a stun of hi − 60, and `+0x95D` cleared.
  - `target` must be an entity on the watcher. Otherwise `FUN_004194f0` and `FUN_00426840` are skipped and `+0x95D` keeps its old value.

**The flag.** It is the attacker's `+0x98B`, which is the variant of its current skill:
- the lo bits 5-8 of its last motion-7 cast words (`lo 0x20003C` → 1 for Ice Spear)
- or `FUN_0044f070`'s range table applied to the last C2S 0x15 skill id (§5)
- 0 for a strong attack (motion 5 does not set `+0x98B`), for a swing that `FUN_0041aa80` promotes to an event 9, or when the variant is not known. With 0 there is no element and no debuff, but the slide and stun still apply.

## 3. Telling a dash attack from a basic swing

**What the client does** (`FUN_00414210` motion case 1, lines 192-219):
- Motion 1 while the attacker is in **state 6 (dash)** enters state 4 with `+0xE9C` preset:
  - **0x564 (1380) and stage `+0x979 = 2`** for weapon types 0, 1, 2 and 4
  - 0xB4 (180) for weapon type 3
  - 200 for weapon types 5 and 6
- Motion 5 in state 6 enters state 1 with `+0xE9C` preset to 150 (wt 0/1), 120 (wt 2/4), 210 (wt 3) or 270 (wt 5/6). That is the dash strong attack, event 9.
- State 6 lasts at most the wind-up T[5] = 50 ms plus 500 ms (`+0xE24 < 500`). The keyboard handler holds motion 6 until the dash ends, or until an action key resets its counters (`FUN_0042c310`).

**On the wire:**
- The attacker's C2S 0x0D words go **motion 6 (`lo & 0x1C == 0x18`) → motion 1 or 5**, with no idle words in between. The builder sends immediately when the input changes.
- The logic time from the first motion-6 packet to the motion-1 packet is ≤ 50 + 500 + 30 ms.
- Live d-series: `lo 0x1a` then the attack words, and the report 120-150 ms after `0x1a`. That matches A going 5 → 6 → 4.
- A dash attack's report usually comes **one tick (30 ms of logic time) after the motion-1 packet**. A swing from standing comes at 150 ms.

**The hit report cannot tell them apart.** Both have event 7 and `target_action_event` 7, `flag_8e7` (`+0x974`) is 1 for both, and the lo input bits are the attack key held in both cases.

**Server rule.**
- dash attack ⇔ the motion-1 (or 5) packet directly follows motion-6 words, and the logic time since the motion-6 start is ≤ 580 ms
- Then E = 1380 + Σ(P→H) − 30 and hurt = 2140 − E, normally 760.
- The third swing of a held combo (E ≥ 1280) gives the same hurt.

## 4. Ice Spear: where the 2 s comes from, and what the watcher gets today

**On A's client:**
- The report tail carries `+0x975 = 9`, so the mob's `+0x9DC` is 9.
- `+0x98C` = 1, because Ice Spear is variant 1: `FUN_0044f070` maps 0x104..0x10E → variant 1 → `+0x98B`.
- `FUN_004194f0`, variant 1, Warrior → family 0x104 → the learned 260 → hii Attribute **2** → `+0x95D = 2`.
- State 3 then holds for `+0x9E4 + 1000` = 1020 + 1000 = **2020 ms** (0x4152AC).
- Ice Spear has Con 0, so no debuff slot, and `FUN_00426840` returns for a Warrior with variant 1.
- **So the 2 s is the hurt plus the ice element.**

**The watcher today** (livefix 9ee51aa, log s02 16.099-16.322):
1. The 0x1B cast pose of A (motion 7, variant 1): an animation only. Remote copies never detect hits.
2. The 33-byte 0x2A `{lo 0x00007000, hi 0, target 1, the point}`:
   - action 7, facing2 0 → `+0x95B = 8` and `+0x9E4 = 0`
   - `FUN_004194f0`(action 7) clears `+0x95D`
   - the result is a one-tick flinch in place: no slide, no freeze
3. The chase word, twice:
   - at cast time, 15.749, from `_skill_attack` → `_damage_monster` → aggro
   - 222 ms after the report, because event 9 sets no gate

Nothing carries the freeze today: no 0x41 or 0x3B, and no element.

## 5. Skill table (the class's own weapon; the length L at the hit; the element on event 9)

Rules for reading the table:
- The variant is from `FUN_0044f070` and is the first id of a 10-level family.
- L is from §1.3.
- hurt = L − E. Use E from the timeline, or 270 on T[1] as the default.
- The ice rows add 1000 ms to the stun. The wind rows slide 19 ticks.
- Strong attack (motion 5): variant 0, L = the class's state-1 length (1290 for wt 0/1), no element.

| class | tier | var | first id | skill | L (own weapon) | effect +0x95D (attr) | FUN_00426840 debuff |
|---|---|---|---|---|---|---|---|
| 1 Warrior | 0 | 1 | 260 (0x104) | Ice Spear | 1290 | 2 ICE +1000 ms | - |
| 1 Warrior | 0 | 2 | 271 (0x10f) | Fire Beat | 1290 | 1 fire | - |
| 1 Warrior | 0 | 3 | 314 (0x13a) | Wind Cutter | 1290 | 4 WIND 19-tick slide | - |
| 1 Warrior | 0 | 4 | 1898 (0x76a) | Double Attack | 1290 | 0 none | - |
| 1 Warrior | 0 | 5 | 1909 (0x775) | Battle Charge | 1040 | 4 WIND 19-tick slide | - |
| 1 Warrior | 1 | 6 | 2224 (0x8b0) | Crescent Slash | 1290 | 4 WIND 19-tick slide | - |
| 1 Warrior | 1 | 7 | 2235 (0x8bb) | Weapon Shackle | 1290 | none | 0x8bb Weapon Shackle Con 8010 |
| 1 Warrior | 1 | 8 | 2257 (0x8d1) | Sonic Slash | 1290 | 4 WIND 19-tick slide | - |
| 1 Warrior | 1 | 9 | 2268 (0x8dc) | Heaven Strike | 1290 | 1 fire | - |
| 2 Monk | 0 | 1 | 556 (0x22c) | Blazing Kick | 1240 | 1 fire | - |
| 2 Monk | 0 | 2 | 578 (0x242) | Taunt | 1240 | none (early return) | 0x242 Taunt Con 5010 |
| 2 Monk | 0 | 3 | 567 (0x237) | Double Kick | 1240 | none | - |
| 2 Monk | 0 | 4 | 2008 (0x7d8) | Flying Kick | 1240 | 4 WIND 19-tick slide | - |
| 2 Monk | 0 | 5 | 2019 (0x7e3) | Weakness Detection | FX 0xA4 (basic.hsi, 710?) | none (early return) | 0x7e3 Weakness Detection Con 15000 |
| 2 Monk | 1 | 6 | 2334 (0x91e) | Triple Kick | 1640 | 3 lightning (no change) | - |
| 2 Monk | 1 | 7 | 2345 (0x929) | Flying Kick | 1240 | 4 WIND 19-tick slide | - |
| 2 Monk | 1 | 8 | 2367 (0x93f) | Merciless Strike | 3300 | 3 lightning (no change) | - |
| 2 Monk | 1 | 9 | 2378 (0x94a) | Super Flying Kick | 1240 | 4 WIND 19-tick slide | - |
| 2 Monk | 2 | 6 | 2389 (0x955) | Counterblow | 1200 | 4 WIND 19-tick slide | - |
| 2 Monk | 2 | 7 | 2400 (0x960) | Dragon Kick | 1240 | 1 fire | - |
| 2 Monk | 2 | 8 | 2422 (0x976) | Evasive Counterkick | 1240 | 4 WIND 19-tick slide | - |
| 2 Monk | 2 | 9 | 2433 (0x981) | Cartilage Smash | 600 | deferred: +0x95E = 2, +0x95D = 0 | 0x981 Cartilage Smash Con 5010 |
| 3 Archer | 0 | 1 | 336 (0x150) | Ice Arrow | 1390 | 2 ICE +1000 ms | - |
| 3 Archer | 0 | 2 | 347 (0x15b) | Fire Arrow | 1390 | 1 fire | - |
| 3 Archer | 0 | 3 | 358 (0x166) | Wind Arrow | 1390 | 4 WIND 19-tick slide | - |
| 3 Archer | 0 | 4 | 1920 (0x780) | Double Arrow | 1390 | none | - |
| 3 Archer | 0 | 5 | 1931 (0x78b) | Arrow Grapple | 1390 | none (early return) | 0x78b Arrow Grapple Con 2520 |
| 3 Archer | 1 | 6 | 2444 (0x98c) | Piercing Arrow | 1390 | 0 none | - |
| 3 Archer | 1 | 7 | 2455 (0x997) | Poison Arrow | 1390 | none | 0x997 Poison Arrow Con 8010 |
| 3 Archer | 1 | 8 | 2466 (0x9a2) | Sniping Shot | 1740 | 0 none | - |
| 3 Archer | 1 | 9 | 2477 (0x9ad) | Arrow Shower | 1390 | 0 none | - |
| 3 Archer | 1 | 10 | 2488 (0x9b8) | Finishing Blow | 2090 | 1 fire | - |
| 4 Rogue | 0 | 1 | 391 (0x187) | Poison | 1240 | none | 0x187 Poison Con 9000 |
| 4 Rogue | 0 | 2 | 435 (0x1b3) | Stun | 1240 | none (early return) | 0x1b3 Stun Con 3000 -> state 0x15 after hurt |
| 4 Rogue | 0 | 3 | 1953 (0x7a1) | Blazing Blade | 1240 | 1 fire | - |
| 4 Rogue | 0 | 4 | 1942 (0x796) | Shuriken | 1240 | 3 lightning (no change) | - |
| 4 Rogue | 1 | 5 | 2554 (0x9fa) | Assassination | 700 | 4 WIND 19-tick slide | - |
| 4 Rogue | 1 | 6 | 2565 (0xa05) | Blind | 1240 | none | 0xa05 Blind Con 5520 |
| 4 Rogue | 1 | 7 | 2587 (0xa1b) | Throw Knife | 1240 | 4 WIND 19-tick slide | - |
| 4 Rogue | 1 | 8 | 2598 (0xa26) | Extreme Thrust | 1240 | 1 fire | - |
| 4 Rogue | 2 | 5 | 2620 (0xa3c) | Puppet | 1240 | none | 0xa3c Puppet Con 7020 |
| 4 Rogue | 2 | 6 | 2631 (0xa47) | Time Bomb | 1240 | deferred: +0x95E = 1, +0x95D = 0 | 0xa47 Time Bomb Con 5010 |
| 4 Rogue | 2 | 7 | 2642 (0xa52) | Spider Web | 1240 | none | 0xa52 Spider Web Con 3000 |
| 4 Rogue | 2 | 8 | 2653 (0xa5d) | Poison Cloud | 1240 | none | 0xa5d Poison Cloud Con 9000 |
| 5 Mage | 0 | 1 | 512 (0x200) | Ice Bolt | 1490 | 2 ICE +1000 ms | - |
| 5 Mage | 0 | 2 | 523 (0x20b) | Fire Bolt | 1490 | 1 fire | - |
| 5 Mage | 0 | 3 | 534 (0x216) | Wind Bolt | 1490 | 4 WIND 19-tick slide | - |
| 5 Mage | 0 | 4 | 1986 (0x7c2) | Stone Guard | 1490 | 3 lightning (no change) | - |
| 5 Mage | 0 | 5 | 1997 (0x7cd) | Teleport | 1490 | none | - |
| 5 Mage | 1 | 6 | 2664 (0xa68) | Stone Wave | 1490 | 3 lightning (no change) | - |
| 5 Mage | 1 | 7 | 2675 (0xa73) | Nova | FX 0xFF (basic.hsi, 1490?) | 4 WIND 19-tick slide x2.5 | - |
| 5 Mage | 1 | 8 | 2686 (0xa7e) | Fire Wall | 1490 | 1 fire | - |
| 5 Mage | 1 | 9 | 2697 (0xa89) | Earthquake | 1490 | 3 lightning (no change) | - |
| 5 Mage | 1 | 10 | 2708 (0xa94) | Blizzard | 1490 | 2 ICE +1000 ms | - |
| 5 Mage | 2 | 7 | 2763 (0xacb) | Meteor | 1490 | 3 lightning (no change) | - |
| 6 Priest | 0 | 1 | 446 (0x1be) | Heal | 1490 | none | - |
| 6 Priest | 0 | 2 | 457 (0x1c9) | Mana Transfer | 1490 | none | - |
| 6 Priest | 0 | 3 | 479 (0x1df) | Holy Strike | 1490 | 1 fire | - |
| 6 Priest | 0 | 4 | 589 (0x24d) | Remote Heal | 1490 | none | - |
| 6 Priest | 0 | 5 | 600 (0x258) | Remote Mana Transfer | 1490 | none | - |
| 6 Priest | 0 | 6 | 1964 (0x7ac) | Earth Strike | 1490 | 3 lightning (no change) | - |
| 6 Priest | 0 | 7 | 1975 (0x7b7) | Thornbush | 1490 | 3 lightning (no change) | 0x7b7 Thornbush Con 2010 |
| 6 Priest | 1 | 8 | 2820 (0xb04) | Holy Lightning | 1490 | 4 WIND 19-tick slide | - |
| 6 Priest | 1 | 9 | 2809 (0xaf9) | Breath of Life | 1490 | none (early return) | - |
| 6 Priest | 2 | 8 | 2831 (0xb0f) | Strike of Darkness | 1490 | none | 0xb0f Strike of Darkness Con 8010 |
| 6 Priest | 2 | 9 | 2853 (0xb25) | Vampiric Attack | 1490 | none | - |
| 6 Priest | 2 | 10 | 2864 (0xb30) | Frenzy | 1490 | none (early return) | 0xb30 Frenzy Con 8010 |
| 6 Priest | 2 | 11 | 2875 (0xb3b) | Mutation | 1490 | none (early return) | 0xb3b Mutation Con 8010 |

"none (early return)" means `FUN_004194f0` returns before it computes damage or an element. The debuff still comes from `FUN_00426840`.

## 6. Spec for the implementer

### 6.1 Inputs at a client-caught hit report H on monster m by attacker X (m survives)

| Input | Source |
|---|---|
| `ie` | H's reaction nibble: 7 for a swing, 9 for a skill or strong attack |
| `tae` | H's tail `target_action_event` (`+0x975`): 7, 8, 9 or 10 |
| `f` | X's facing (`+0x949`). Take it from the tail's sign of dx, or else the held key, as in M1. Overrides: Rogue tier 1 variant 5 → reversed; Mage tier 1 variant 7 → the side of m relative to X. |
| `v` | the variant of X's last motion-7 cast words (lo bits 5-8), if that cast is still inside its L. Otherwise 0. |
| wt, class, tier | from X's record. wt is the last non-zero Job index of the equipped weapon, 0 when there is none. |
| P, E | P is the 0x0D that started X's current action, and **E = E0 + Σ logic_elapsed_ms(after P … H) − 30** |

### 6.2 hurt (12 bits) by kind

| Kind | Detected by | E0 | L | Default hurt |
|---|---|---|---|---|
| swing (combo) | ie 7, P = the motion-1 start | 0 | wt 0/1/2/4: 610 if E < 610, 1250 if E < 1250, else 2140; wt 3: 720; wt 5/6: 740 | 490 (stage 0), 500, 760 |
| dash attack | ie 7, and P is motion 1 directly after motion-6 words (≤ 580 ms of logic time into the dash) | 1380 (wt 0/1/2/4), 180 (wt 3), 200 (wt 5/6) | as above | **760** |
| skill | ie 9, P = the motion-7 words | 0 | §1.3 state 1 by (wt, v, tier); §5 column L | Warrior: **1020** |
| strong attack | ie 9, motion 5 (v = 0) | 0, or the dash preset (150 / 120 / 210 / 270) | the state-1 length with variant 0 (wt 0/1: 1290) | 1020 |

- hurt = L − E. If E > L or E < 0, use L.
- The air attack and air skill use the 0xE / 0xF rows of §1.3.
- Log E and hurt next to A's `+0x9E4` (`mana.py`) to calibrate.

### 6.3 Relay bytes (to every holder of m except X; X still gets only the 16-byte release first)

| Kind | Size | Layout |
|---|---|---|
| ie 7 (tae 7/8) | 33 | `u32 m, u32 lo = tae<<12 \| f<<20, u32 hi = hurt, u32 X, f64 x, f64 y, u8 0` |
| ie 9 (tae 9/10) | **34** | `u32 m, u32 lo = tae<<12 \| f<<20, u32 hi = hurt, u32 X, u8 v, f64 x, f64 y, u8 0` |

Here f is 1 for left and 2 for right. x, y is the point before the slide (the tail victim point).

Examples, with X uid 1:
- Basic, left, m 0xF0001, (1446.25, 1887):
  `01 00 0F 00 00 70 10 00 EA 01 00 00 01 00 00 00 00 00 00 00 00 99 96 40 00 00 00 00 00 7C 9D 40 00`
- Dash attack, right, (1075, 1887):
  `01 00 0F 00 00 70 20 00 F8 02 00 00 01 00 00 00 00 00 00 00 00 CC 90 40 00 00 00 00 00 7C 9D 40 00`
- Ice Spear, left, m 0xF0000, (861, 1920), v 1:
  `00 00 0F 00 00 90 10 00 FC 03 00 00 01 00 00 00 01 00 00 00 00 00 E8 8A 40 00 00 00 00 00 00 9E 40 00`

**Constraints:**
- Never put action 9 or 10 in a self-form unless the flag byte is included.
- Never send a positioned 0x2A to X before its release.
- Keep vb at 0.

### 6.4 Server estimate of the knockback

`dx = sign(f) × ticks × walk_px_s × 0.03`, where ticks is:
- 2 for tae 7/8
- 4 for tae 9/10
- 19 × `+0x13E8` (1.0, or 2.5 for Nova) for a wind element

tae 8/10 launch the monster (state 0x17). That is not modelled; keep the fix at the hit point.

### 6.5 Gate rule (no chase word to anyone before this)

- `stun` is:
  - tae 7/8: `hurt − 60`
  - tae 9/10: `hurt + 1000` for an ice element (§5), otherwise `hurt`
- For a wind element, use `max(stun, 600)`.
- Add the Con of a 0x1B3..0x1BD debuff (Rogue Stun, state 0x15), or leave it to the `debuffs.py` control window.
- `ai_recover_until = max(current, t_H + stun/1000 + MARGIN)`
  - MARGIN = 0.12 s: 60 ms of tick rounding plus relay latency. It replaces the flat `MOB_HIT_RECOVER_SECS`, which becomes a floor.
  - Live values:

    | Hit | Stun | Gate |
    |---|---|---|
    | Basic | 430 | 0.55 s |
    | Dash | 700 | 0.82 s |
    | Ice Spear | 2020 | 2.14 s |

- Apply the gate for ie 9 as well as ie 7. A combo re-arms it.
- **Arm it at cast time too.** When `_skill_attack` (the server's box) damages a monster that the client will also report, set `ai_recover_until = max(current, now + L_skill/1000 + MARGIN)` **before** `_damage_monster`. That stops the aggro path from sending a chase word at cast time, which is what happened at s02 15.749.
  - The report can only come while X is in state 1, so within L of the cast.
  - When the report arrives, recompute the gate from it. If no report comes, the provisional gate simply expires.

## 7. Open points

- **The FX lengths** for Monk variant 5 and Mage tier 1 variant 7 (basic.hsi 0-based index 0xA4 / 0xFF) are tentative (§1.5).
- **E depends on the hitbox reach.** A target at the edge of the swing is hit on a later frame. The timeline value of E covers this; the fallback defaults do not.
- **Combo-stage tracking with a held key.** A held attack key repeats swing, swing, swing without new words. The server sees only the 210 ms re-sends, so L must come from E alone (the stage windows are 0..610, 640..1250 and 1280..2140).
- **Rule to check live:** a new anchor P starts when E passes the current stage end without an advance (a new state 4 starts on the tick after state 8).
