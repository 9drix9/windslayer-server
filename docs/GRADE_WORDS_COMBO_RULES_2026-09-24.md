# Retail rules for grade words and the combo counter, and the combo_hud_2009.py redesign

## Summary

- **Grade word:** it is a per-hit damage-roll label. It has nothing to do with the combo count or with chain timing.
- **Combo counter:** it counts damaging hits with a 2.0 s window and hides with a hard cut. The digits are green up to 19 and orange from 20. Footage from 2011–12 (KR/WS2) also shows red from about 50 (inferred).
- **Consequence for the patch:** the grade and the count have to move from the hit event at 0x412D70 to the damage function `FUN_004194f0`. That is the only place where Build 14 knows the damage.

## New measurements in this pass

Work files are in `<local scratch>\grades\synth\`.

1. **Orange threshold, measured at 30 fps** (`r6\hi_sheet.png`, `r6\s518.png`).
   - r6TUkXgLgEk (EN WindSlayer 2, 2012), one chain from 2 to 32: "19 Combo!" is green until 530.167 s. "20 Combo!" appears orange at 530.200 s. 21 through 32 are all orange.
   - The "Combo!" word changes colour together with the digits.
   - 3Ia9cd9Lfg8: 18 and 19 are green, 21 is orange (98.75 s). This corrects the catalogue's "21 green".
2. **A red tier** (`8ol\zoom_tiers.png`, `8ol\hi_sheet.png`).
   - 8Ol-DThGCZ8 (KR test footage, 2011) shows 23, 29, 30, 35, 43 and 44 in orange: yellow-centre gradient, G/R about 0.66–0.81.
   - It shows 51, 54, 58, 63 and everything up to 194 in red-orange, G/R about 0.37–0.41.
   - The last orange frame is "4x" (44–49) and the first red one is 51. **The boundary at 50 is inferred.**
   - Measured profiles:

     | Tier | Top | Middle | Bottom |
     |---|---|---|---|
     | Orange | (230,113,14) | (236,224,9) | (214,120,28) |
     | Red | (221,23,14) | (244,147,19) | (214,39,8) |

3. **Two survey videos play fast, so their absolute timings are wrong.**
   - 2b70F9hk_Lc: damage digits last 0.2–0.3 s of video time (`2b\sheet41.png`). ws4 shows them for about 0.8–1.0 s (`ws4d\sheet2.png`), and the code gives them 1000 ms. So 2b70 is compressed about 3×.
   - That explains its 0.7 s "combo window": 0.7 s × 3 ≈ 2.1 s. The counter-lifetime to word-lifetime ratio is 2.3 there and 2.4 in y-ONQ.
   - yal_mcf74Bg: damage-over-time ticks are every 990 ms in code (`% 0x3DE` in FUN_004185b0 and FUN_0042e790). In the video they appear every 0.52 s, so the video runs about 1.9× fast.
   - Inferred from this: the Nov 2009 build had grade words but no counter. Its four basic hits fell within about 2 s of real time, and no counter was drawn.
4. **Retail word sizes and colours** (ws4 at 720p divided by 1.2, so 800×600 terms; `words\crops.png`):
   - GOOD: about 75×23 px, squared "techno" letters, mint-to-dark-teal gradient.
   - BAD: about 63×23 px, rounded bold italic. Fill runs (194,100,10) → highlight (243,201,77) → (198,102,11).
   - CRITICAL!: about 133×27 px, same squared font as GOOD, yellow → orange → red.
   - All three have a dark outline and a white outer rim.
5. **Code facts in `FUN_004194f0`** (corpus_2009 asm; bytes checked against the pristine exe, sha256 46bc5404…, the same as the installed `WindSlayer.exe`):
   - Build 14's damage has no randomness.
   - 0x41A55A clamps the damage to at least 1: `83 FB 01 7D 05 BB 01 00 00 00`.
   - 0x41A689 caps it at the victim's +0xA0.
   - 0x41A6AA is `3B DA 0F 84 22 F6 FF FF`: if the damage is 0 it returns, and no digit is drawn.
   - So a damage roll applied after the clamp can reach 0, and a 0 produces no digit and no count, exactly as in retail.

## (a) Which grade word appears

Tallies below cover the six frame logs plus this pass. The rates use 356 landed hits from y-ONQ, ws4, HOZq and 2b70.

| Hypothesis | Consistent | Inconsistent | Verdict |
|---|---|---|---|
| By combo count (our patch: Good!/Great!/Wow! at 2-4 / 5-9 / ≥10, nothing on hit 1) | 0 distinctive | 57 words on hit 1 or with no counter (y-ONQ 19, A4tc 16, ws4 11, HOZq 7, 2b70 4). About 80 hits at count ≥5 and not one Great!/Wow!. About 70% of hits at count ≥2 get no word | Rejected |
| By chain stage or key timing (+0x979, the windows in FUN_00414210) | – | y-ONQ has every word on every stage (BAD 13/4/3, GOOD 10/8/4 by stage). The 57 first-hit words have no timing reference. Four graded damage-over-time ticks involve no input, one while the player stood about 150 px away | Rejected |
| By time since the previous hit | – | The ranges overlap completely (BAD 0.48–29 s, GOOD 0.56–22 s, none from 0.4 s) | Rejected |
| First hit of a chain gets no word (yal) | 8 (from a compressed video) | 57 | Rejected |
| Only from Strong Attack (HOZq alternative) | – | y-ONQ Lv3 has 64 graded hits before Strong Attack is learned; A4tc Lv1–2 has 23 | Rejected |
| Hit landed while the player was in hit-stun | – | 7 BADs with no recent damage, and GOODs 0.08 s after damage | Rejected |
| **Per-hit damage roll: BAD = low, GOOD = high, CRITICAL! = crit** | 13 target groups; 1505 grade-vs-normal pairs in the right order, 1154 ties (integer rounding) | **0 out of order.** Permutation test on the 8 groups with large damage values: p < 5e-5 (0 of 20000). Soft outliers: y-ONQ ungraded 5 (possibly a Ssiyo), 1 of 17 GOODs showing 4, ws4 404.30 (merged popups), HOZq 14 = 14 across different attack kinds | **Accepted** |

**Rates:**
- BAD 48/356 = 13.5% (95% CI about 10–17)
- GOOD 45/356 = 12.6% (about 9–16)
- CRITICAL! 19/356 = 5.3% (about 3–8)
- no word about 68.5%

**Damage model (inferred, from `model\fit.py`):** damage = trunc(base × M × U(0.9, 1.1)), with M = 0.75 for BAD, 1.0 for no word, 1.25 for GOOD and 1.5 for CRITICAL!.
- It fits 13 of 14 groups. The exception is yal "basic 58", which fits with ±15% jitter.
- It explains that Oraring at Lv3 shows BAD = 0 (no digit, but the word still shows), no word = 0 or 1, and CRITICAL! = 1. Koring gives BAD 2, GOOD 3, CRITICAL! 4.
- Measured multipliers: BAD 0.64–0.83, GOOD 1.12–1.34, CRITICAL! 1.44–1.68.
- Damage digits stay green on critical hits.

**How the word is shown:**
- Above the attacker, about 125 px above the name tag, following the player.
- It lasts 0.8–0.97 s, pops in at about 1.5× then 0.9× then 1.0× over roughly 160–270 ms, and rises slightly.
- Each graded hit spawns a new word. The old one runs out underneath and is not removed (5 overlap cases). A BAD on a 0-damage hit still shows.

## (b) Combo counter

| Rule | Support | Against |
|---|---|---|
| Hidden at 1; "2 Combo!" from the 2nd damaging hit | Every chain in all videos, about 70 | 0 |
| +1 per damaging hit per victim (a multi-hit or multi-target swing adds 2 or more) | ≥10 clear two-digit cases (ws4 5, 2b70 5); the WS2 and KR videos jump by +4 to +7 | 0 clear; 4 ambiguous one-digit cases (A4tc, y-ONQ) |
| Our patch's "once per swing" (first victim only, [EBP+0x965]) | – | ≥10 |
| A 0-damage hit neither increments, refreshes nor resets | ≥15 (y-ONQ ×5 timed, A4tc 6, ws4 3+, HOZq 1) | 0 |
| Window 2.0 s from the last damaging hit | Continued at 1.5–2.006 s: 27 cases; reset at 2.065–2.5 s: 11 | 0 (2b70 is explained by the time compression) |
| Our 1500 ms gap | – | the same 27 cases |
| Display lasts as long as the window, then a hard cut (last visible frame 1.8–1.95 s after the hit; ws4 1.93, HOZq 1.9, r6 1.83–1.9, A4tc 1.8) | 5 videos | our 300 ms fade (ws4 shows at most a 1–2 frame fade) |
| Taking damage, target switches and kills do not reset it | dozens / ≥8 | 0 |
| Colour: green 2–19, orange 20–49, red ≥50 | 19→20 measured exactly in r6; 3Ia9; 8Ol; Outspark-era maximum is 17, still green | 0 |
| "Combo!" and the number pop on **every** increment (about 1.3–1.5× for 70–130 ms, then about 0.9×, then 1.0×) | r6 at 30 fps, y-ONQ, HOZq, A4tc | our "word pops only at count 2" |

The 20 and 50 thresholds come from 2011–12 clients. For the Outspark era they are inferred.

## Patch design for combo_hud_2009.py

**Signals Build 14 exposes at the existing hit hook (0x412D70).**
- It runs in pass 1 of `FUN_00412c60`.
- It has the attacker in EBP, the event in ESI (7/9/2/4/0xC), the victim uid at +0xE08, the chain stage at +0x979 (set only by the key timing in `FUN_00414210`, windows 410–610 and 1050–1250 ms), the victim count at +0x95F and the first-victim flag at +0x965.
- It has **no damage and no crit flag**: damage is computed later, per victim, in pass 2 by `FUN_004194f0`. Since the retail grade follows the damage and not the stage or timing, drop this hook.

**New hook A at 0x41A55A (10 bytes).**
- Replace `83FB017D05BB01000000` with `E9 <caveA> 90×5`. The cave returns to 0x41A564.
- The only jump into this range is the internal `JGE` to 0x41A564.
- State at this point: EBX = damage, EBP = attacker, [ESP+0x20] = victim, [ESP+0x28] = scene, [ESP+0x74] = event (already remapped 5→4, 8→7, 10→9), [ESP+0x7C] = param_6. Add 0x20 to these offsets after `pushad`.
- The cave:
  1. Re-execute the clamp.
  2. Always write PEND = 0.
  3. Apply the filters: [scene+0x988] == EBP, victim byte +0x9C == 4, room +0x7C != 1, and [scene+0xF40] == 0. The last one keeps the roll off paths where the client owns HP, so PvP and arena clients cannot desync (inferred safety rule; check live that it is 0 in the field).
  4. If the event is 4, 7 or 9 and param_6 == 0:
     - Roll r = xorshift % 1000. CRITICAL! if r < 50, BAD if r < 180, GOOD if r < 310, otherwise no word. These are knobs.
     - Multiply EBX by M × (900 + rng % 201), using a 64-bit multiply, and divide by 1,000,000. Write the result back into the saved EBX.
     - Queue the word in ST_WORD, priority CRITICAL! > GOOD > BAD. For CRITICAL!, also store the victim uid.
     - Set PEND = 1.
  5. If the event is 9 and param_6 != 0 (the delayed hit from `FUN_00426b60`): PEND = 1, no roll.
  6. If the event is 0 and param_6 != 0 (a damage-over-time tick): roll and grade when GRADE_DOT is on, no count (inferred from yal).

**New hook B at 0x41A6AA (8 bytes).**
- Replace `3BDA0F8422F6FFFF` with `E9 <caveB> 90×3`.
- If PEND is set and EBX != 0: clear PEND, then:
  - if now − last < 2000: count++
  - otherwise: count = 1
  - last = now, show_until = now + 2000

  Then `cmp ebx,edx / jz 0x419CD4 / jmp 0x41A6B2`.
- No call happens between A and B. The one early exit at 0x41A5C3 (a deferred store) skips B, and the next A overwrites PEND.

**HUD hook (0x43E57D, kept).**
- If ST_WORD is set, call `FUN_0042cd90(0x54EBD0, word, [scene+0x224], 0,0,0, 1, 10, 0)` and clear it. Spawning from the HUD hook avoids appending to the effect list while `FUN_0042e790` is iterating it.
- Optionally spawn 0x14B, the existing critical burst, on the victim (inferred from y-ONQ 2:08).
- Drop the logic that retires the previous grade word; retail lets them overlap.
- FADE_MS = 0.
- Use the "Combo!" pop sprite (+21) on every increment.
- Choose the colour set: orange when count ≥ 20, red when count ≥ 50. Both are knobs, and each needs its art probe to pass.

**Init hook (kept).**
- Seed the RNG with `rdtsc | 1`.
- Probe GetExtent for the new grade word at base+32, orange digit 0 at base+35 and red digit 0 at base+57, and set flag bits from the results.
- Fallback when a probe fails:
  - GOOD → 0x14C Good!
  - CRITICAL! → 0x14E Wow! plus 0x14B
  - BAD → no word
  - orange/red tier → the red damage digits 0x06+d with orange CIME text

**State block (0x551680..0x55169F, still inside STATE_END).**

| Offset | Contents |
|---|---|
| +0x00 | count |
| +0x04 | last hit time |
| +0x08 | show_until |
| +0x0C | art_base |
| +0x10 | RNG state |
| +0x14 | u16 ST_WORD, u8 priority, u8 PEND |
| +0x18 | art flags |
| +0x1C | critical-hit victim uid |

**Other changes.**
- CHAIN_MS = SHOW_MS = 2000.
- The new hooks do not overlap RESERVED.
- Server: nothing required. Digits and kill HP already disagree. The mean multiplier is about 1.025.

## Art needed

The new sprites are appended to combo001.hsi after the existing 32, drawn in the combo001 style.

- **+32 GOOD (~75×23), +33 BAD (~63×23), +34 CRITICAL! (~133×27).**
  - One cell each, 3 frames, copying Good!'s frame table: 80 ms at 1.5×, 80 ms at 0.9×, then about 700 ms at 1.0× (about 860 ms in total).
  - Optionally split the last frame to add a rise of about 1 px per 30 ms.
  - Anchor like 0x14C (centre about 130 px above the feet).
  - Styles as measured above. Reference crops: `words\crops.png`.
- **+35..+56 orange set:** digits 0–9, word, digit pops, word pop, with the same 40×52 and 76×26 cells. Uses the orange gradient above.
- **+57..+78 red set:** same layout, red gradient.

The atlas grows to about 512×256. Check that map sprite ids stay relative to their base; the "first_map_sprite 374" note in the manifest shifts.

## Live-test plan

Test with a patched copy, non-elevated, only while the user is not playing, one change per run, Lv1 Novice against Ssiyo.

0. **Static:**
   - `--check`.
   - Unicorn emulation of cave A over 100k rolls: expect rates of 13/68/13/5 ±0.5%. With EBX = 3 expect BAD 2 and CRITICAL! 4. With EBX = 1 expect BAD 0 and no-word 0 or 1 at about 50%.
   - Wrong attacker, PvP room or param_6 set: EBX unchanged.
   - Cave B: EBX = 0 means no count and a jump to 0x419CD4.
1. **Record 60 swings at 30 fps.** Expect:
   - words on about 30% of hits, first hits included
   - BAD shows no digit at Lv1 and does not count
   - CRITICAL! shows the largest digit
   - "2 Combo!" on the 2nd damaging hit
   - a hard cut 2.0 s ±1 frame after the last damaging hit
2. **Window:** swings every 1.8 s continue the chain; every 2.3 s restart it.
3. **Multiple victims:** stack two Ssiyos; one swing that damages both adds 2.
4. **Resets:** being hit changes nothing. Kill one Ssiyo and hit another within 2 s: the chain continues.
5. **Colour tiers:** a dev build with ORANGE_AT = 3 and RED_AT = 5, then the real values, using a pack or a multi-target skill to reach 20 and above.
6. **Regressions:**
   - Red digits for monster-on-player hits keep their Run 0 values.
   - PvP room: no word, no counter, damage unchanged.
   - Server [DAMAGE] lines and kills unchanged.
   - Read 0x551680 through ReadProcessMemory.
7. **Fallbacks:** with only the old combo001 art, and with no art at all.
8. **Damage over time:** ticks vary and get words, but do not count.

## Still open

- Whether the 20/50 thresholds and a 2.0 s window (A4tc and r6 suggest about 1.85 s in WS2) were the same in the Outspark era.
- Whether the crit rate depends on a stat.
- Whether guarded hits (event 4) and damage-over-time ticks count.

Everything in "Patch design" is untested static design.**Verdict:** the colleague's two central rules hold up under recounting. The patch design is placed correctly, but it has one real bug and two gaps to fix before it is built.

**Rule (a): the grade word is a per-hit damage label. Confirmed.** Words appear on first hits, at every count and on damage-over-time ticks, and within a group of comparable hits BAD ≤ no word ≤ GOOD < CRITICAL!. The ordering is the robust part. The multipliers (0.75/1.25/1.5) and the ±10% jitter are a hand-tuned fit, not a measurement.

**Rule (b): the counter (Outspark era). Confirmed.**
- Hidden at 1; "2 Combo!" on the second damaging hit.
- +1 for each damage number. A 0-damage hit neither adds, refreshes nor resets.
- The window is about 2.0 s and equals the display time, ending with a hard cut. The pop plays on every increment.
- Chains survive taking damage, kills and target switches.

**Colours.**
- Orange from 20 is exact in the 2012 English WindSlayer 2 client (r6: green 19 at 530.167 s, orange 20 at 530.200 s; 3Ia9: 18 and 19 green, 21 orange). I checked the frame sheets. For the Outspark era it is inferred, since no Outspark video goes past 17.
- Red from 50 comes only from the 2011 Korean test footage. The boundary lies somewhere between 47 and 51 and was never seen.

**Code facts I confirmed** in the pristine exe (sha256 46bc5404…):
- The hook bytes are as stated: `83FB017D05BB01000000` at 0x41A55A and `3BDA0F8422F6FFFF` at 0x41A6AA.
- No jump lands inside either range except on its first byte.
- There is no call between 0x41A564 and 0x41A6AA, and EDX is 0 at hook B on every path.
- The stack offsets are right: +0x20 victim, +0x28 scene, +0x74 event, +0x7C param_6.
- "Server: nothing required" holds. On the path where [scene+0xF40] is 0 and the room is not PvP, the damage only feeds the cap at +0xA0 and the digit accumulators at +0x11B4 to +0x11C8. There is no HP or aggro write, and the client's hit report sends no damage.

**Corrections to the evidence**

1. **"Per victim" is overstated; count per damage number instead.**
   - ws4 582.32 was misread. The 4 came from a swing at 581.75 and the 3 from a separate swing at about 582.35: an ordinary two-swing chain on one Ssiyo.
   - ws4 340.74, 555.22 and 558.99 add 2 within one frame with only one visible victim.
   - Only 563.16 shows two victims side by side.
   - The 2b70 pairs are 0.3–0.6 s apart in real time, so they could be separate swings.
   - Counting every damage call with damage above 0, as the design does, is still the right behaviour.
2. **ws4 166.47 "BAD re-pop, damage 2(?)" is not a new word.** It is the tail of the BAD at 165.77, which dealt 1; the hit at 166.43 was ungraded with 2. Counting it would have broken the ordering, so the ordering survives, and the ws4 BAD count is 12, not 13.
3. **The rates quietly leave out A4tc.**
   - A4tc (2011) has BAD against GOOD at 17:4, versus 51:48 in the other five videos (Fisher two-sided p≈0.015).
   - I confirmed that the mob-fight BADs at 191.84, 192.44 and 192.88 are separate words.
   - 13/13/5% is an estimate for the Outspark era only (BAD CI 10–17%, GOOD 10–16%, CRITICAL! 3.4–8.2%). Keep the rates as knobs.
4. **The time compression is real, but "about 3×" is not established.**
   - Map-change fade-outs take 0.53–0.6 s in ws4 against 0.3–0.5 s in 2b70, which is only about 1.2–2× at those moments. The damage-digit lifetimes imply about 3×, so the capture rate varied with load.
   - 2b70 and yal are usable only for ratios, not absolute timings.
   - yal's "no counter" fits a Nov 2009 build either without a counter or with a window of about 1.1 s or less.
5. **The fit tolerance is chosen after the fact.**
   - fit.py builds two versions of the HOZq Poco group: one drops the none-10, the other drops the none-14. Keeping both makes the ±10% fit impossible.
   - yal's basic hits need ±15%.

**Fixes to the patch design**

- **A. Bug: the delayed hit is flagged by param_7, not param_6.**
  - FUN_00426b60 (pushes at 0x426BB8–0x426BC7) passes 0 in arg5 ([ESP+0x7C], param_6) and the stored damage in arg6 ([ESP+0x80], param_7).
  - The damage function reads param_7 as precomputed damage at 0x4195A4. param_6 is the damage-over-time effect id (pushed from SI at 0x41889E and 0x430D66).
  - As designed, the delayed hit is therefore rolled a second time and gets a second word.
  - Fix: if [ESP+0xA0] (param_7 after pushad) is not 0, do no roll and no word, and only set PEND=1. This also covers the recursive secondary hits at 0x41A313, which pass computed damage as arg6.
- **B. The event whitelist {4,7,9} rests on the wrong field.**
  - The damage function's event argument is the victim's +0x9DC reaction code, set in FUN_00416ab0 (the hit-detection function). It is not the attacker event that the old hook at 0x412D70 reads.
  - 7/8 is an ordinary hit. 2/3 and 4/5 are both set in the branches where the victim is guarding (state 2 or 0x11, facing the attacker). 1/6 is monster contact.
  - Accepting 4 but not 2 is inconsistent.
  - Safest: drop the whitelist and rely on the other filters (local attacker, monster victim, not a PvP room, [scene+0xF40]=0, param_6=0, param_7=0), or log the argument live first.
- **C. Rolling the truncated integer distorts low damage.**
  - With damage 1, a no-word hit comes out 0 half the time. ws4 at Lv1 against Ssiyo showed 0 of 13 silent normal hits, while A4tc showed about 27% and y-ONQ Oraring Lv3 45%.
  - Safest: allow 0 only for BAD and keep no-word, GOOD and CRITICAL! at 1 or more. Alternatively, let a no-word roll reach 0 only when the damage before the clamp was 0 or less.
- **D. Minor points.**
  - Six branches land on 0x41A55A. The life-drain path (0x41A512, where damage becomes twice the heal) will also be rolled; this is only cosmetic.
  - Grading damage-over-time ticks and not counting them is fine.
  - Make the red tier opt-in, since the evidence is Korean only.

**Weakest claims, weakest first:**
1. Per-victim counting (one clean case).
2. Red from 50.
3. The multiplier and jitter values.
4. Fixed rates across builds.
5. Orange from 20 in the Outspark era.
6. The compression factors and "no counter in Nov 2009".
7. [scene+0xF40] being 0 in the field, which still needs a live read.

My frame sheets are in `<local scratch>\grades\adv\`:
- `ws4\zoom_555.10.png`, `zoom_558.87.png`, `zoom_563.03.png`, `seq_581.png`, `seq_340.png`, `r165_1.png`, `r165_2.png`
- `a4\mob_big.png`
- `2b\ends.png`