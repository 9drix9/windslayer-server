# Retail combat feel: EN 2009 client (Outspark v1.04 Build 14), static RE, 2026-09-23

**Scope.** The 2011 retail video (Novice Hunting Park, a level-1 Novice hitting Ssiyo) looks different from field combat on the private server. This document says why, and what the server or a client patch can change. It covers six things:
- the monster name-tag colour
- body-contact damage from monsters the player has not hit
- damage digits
- impact words and cry bubbles
- grade words
- the combo counter

**Method.** Static RE only, using:
- the Ghidra corpus of `WindSlayer_2009.exe`, plus the 2008 corpus
- the KR 2025 exes (`WSKRGame.exe` and the KR `WindSlayer.exe`)
- the game data

Nothing here has been tested live. Four findings were each checked by an adversarial verifier, and their corrections are applied. Two further points were settled by re-reading code for this document, and they are marked **[new]**:
- the colour of the damage digits (the verifiers disagreed)
- the KR 2025 name-tag rule

Offsets are for 2009 unless marked otherwise. For every game exe named here, file offset = VA - 0x400000; this was checked with pefile.

## 0. Bottom line

- **The video was recorded on a later client.** Build 14 is the January 2009 closed-beta client: `ui011_a1.hsc` carries a "Close Beta Events, Jan 7-14 2009" banner.
- **Two features already work in Build 14 and need nothing:**
  - damage digits (green at the monster, red at the player)
  - impact words and cry bubbles
- **Two features are rules the later official client added.** Both read one per-mob flag that the server already controls: entity `+0x971`, "server controlled" (KR 2025 `+0x9B9`).
  - The later client draws a monster's name box red while that flag is set (KR `0x4756FC`).
  - It lets a monster hurt the player by touch only while that flag is set (KR `0x43BAE0`).
  - Build 14 has neither rule. Two small client patches add them. The server's existing aggro packets then drive both: 0x2A on the first hit the monster survives, 0x9E when the chase is given up.
- **Two features have no code in Build 14: the "N Combo!" counter and the grade-word logic.**
  - The grade-word art (Good!/Great!/Wow!) ships in the data but nothing uses it.
  - The server can show a grade word through the stall-sign packet 0x85/0x86. This is experimental.
  - The combo counter at the right edge needs a larger client patch, or the later client.

## 1. Summary table

| Feature | How Build 14 does it | Why it differs from retail | Server fix | Client patch | Confidence |
|---|---|---|---|---|---|
| Monster name box turns red when hit, returns to normal after the chase | Box colour comes only from the level gap (`0x43C3CC..0x43C41C`): blue if the monster's level is L-1..L+3, red if above L+3, black if below. A Lv 1 Ssiyo seen by a Lv 1 player is always blue. `+0x971` is never read. | The later client (KR `0x4756FC`) draws the box red while `+0x971 != 0`, black otherwise. It moved the level gap to the name-text colour. | No new packet. Spawn with server_controlled 0 and keep `MOB_AGGRO` on. 0x2A on the first surviving hit turns the box red; 0x9E turns it back. Change the `True` fallback at `windslayer_server.py:5012` to `False`. | 15 bytes at `0x43C3CC` (KR rule). Alternative: 41 bytes that keep the level colour while idle. | High (code read in both clients); not tested live |
| Monsters the player has not hit are harmless to touch | Contact is pure geometry (`0x416BF2..0x416E18`). Any ticked monster with type >= 3 and AtkType bit 0x10 clear produces event 6, and a wandering monster is always ticked. The flinch, knockback, red digit and impact word are drawn locally, before any server reply. | The later client (KR `0x43BAE0`) skips contact unless `+0x971` is set, or the monster has an aggro target. | No packet can stop it for a monster that is still wandering. Keep `MOB_CONTACT_AGGRO_ONLY`; it removes only the HP loss. With the patch: send 0x2A only to aggroed monsters, keep them moving with command words, send 0x9E on give-up. | 52 bytes at `0x416BF2` | High |
| Damage digits | Drawn by the client: its own estimate goes into `+0x11B4`, and `FUN_00431830` pops it. Green over a monster, red over the player (checked). | Not missing. Only the number differs from the HP change the server applies. | Optional: port the client formula so the digit equals the HP loss. Optional: S2C 0x54 to keep the client's copy of the monster's HP current. | None | High |
| Impact words and cry bubbles | Drawn by the client when an entity enters its hurt state (`FUN_0042d110`): KRAKK/CHUD/THUD at the victim; AHRGH!/OUCH!/UGH... next to a hurt monster. | Not missing. "TWAK" is not in the 2009 data; it is later art. | None | None | High |
| Grade word over the player ("Good" green, "BAD" orange) | No code. The art exists (`basic.hsi` 332/333/334 = Good!/Great!/Wow!) but nothing references it. No "BAD" art exists anywhere. | Later client code. | Experimental: S2C 0x85 (stall sign) on the player's own uid with sprite 0x14C..0x14E, cleared by 0x86 after about 700 ms. | Design only | High that no code exists; medium for the workaround |
| "N Combo!" counter at the right edge | No code, no art, no string (exe, lng, hui, ips, every atlas; KR 2025 too) | Later client | Only an approximation: the 0x85 title text ("3 Combo!") drawn over the player | Design only (hooks `0x417D3A`/`0x41AC56`, draw after `0x43E565`); not ready | High that it is absent |

## 2. Per feature

### 2.1 Monster name-tag colour

**Verified facts (Build 14)**

Drawing and colours:
- There is one tag drawer, `FUN_0043c300`. Its only caller is `FUN_0043bcb0`, at `0x43BE44`. The frame root `FUN_0043e480` calls `FUN_0043bcb0` at `0x43E573` when `game+0x1700 == 0`.
- The tag pass runs only in scene mode 4/5/6, for entities of type >= 3. It hides type-4 corpses (`+0x166C == 0x16`, `0x43BE06`).
- The box is a 3-slice from `basic.hsi` (`DAT_0054e844`, loaded at `0x43F6A3`): sprites EDI, EDI+1 and EDI+2, drawn by DrawSpriteSlice at `0x43C483`.
- Colours, measured from `fx001_a8.hsc` (the KR data has the same pixels): 0x26 black (0,0,0), 0x2A red (255,0,0), 0x2D blue (0,53..56,255). All are about 44 % opaque (alpha 113). Black over grass looks dark green.

How the box sprite is chosen (`0x43C368..0x43C41C`):
- The default is EDI = 0x26.
- PvP room (`(game+0x4FC)+0x7C == 1`, `scene+0xF90 != 4`), type-3 player: team `+0x158B` 1 gives 0x2D, team 2 gives 0x2A.
- Otherwise, if the local player exists (`scene+0x988`) and the entity is type 4 with `+0xF00 != 0`:
  - L = the local player's `+0x9D`, or the party-average level `scene+0x985` when that is non-zero.
  - M = the monster's `+0x9D`.
  - L-1 <= M <= L+3 gives 0x2D; M > L+3 gives 0x2A; otherwise the box stays 0x26.

Other facts:
- The monster's level `+0x9D` comes from the hni template (`template+0x650`), copied at spawn (`0x456CB5`). Ssiyo is template 1, Lv 1.
- The tag code reads no combat field: not `+0x971`, `+0xE44`, `+0x96E`, `+0xE10`, nor the hate list.
- Monsters get no level box. Only players get the rank badge (sprite 0x11C, drawn when `+0x9E < 99`).
- `+0x971` has exactly three store sites in the exe:
  - `0x457081`: the 0x1A wire byte
  - `0x459B51`: 0x29 sets it to 0
  - `0x459C89`: the shared 0x2A/0x9E/0x9F handler sets it to (opcode != 0x9E)

**[new] The later official rule (KR 2025).** Name-tag function `0x475650` in `WSKRGame.exe` and in the KR `WindSlayer.exe` (identical bytes). The rule is at VA `0x4756FC`, file `0x74AFC`:
```
mov ecx,0x26                          ; default black box
...                                   ; PvP team rule unchanged (+0x160B)
cmp dword [scene+0x99C],0 / je        ; local player exists
cmp byte  [ebx+0x9C],4    / jne       ; monster
cmp dword [ebx+0xF48],0   / je        ; hni type (= 2009 +0xF00)
cmp byte  [ebx+0x9B9],0               ; server controlled (= 2009 +0x971)
mov eax,0x2A / cmovne ecx,eax         ; red while server controlled
```
- The level gap moved from the box to the monster's name-text colour (`0x475803..0x4758E3`):
  - L-1 <= M <= L+3: `0xFF46C8FF` (light blue)
  - M > L+3: `0xFFFF6482` (pink)
  - lower: `0xFFFFFF78` (light yellow)
- KR draws no level box for monsters either.
- This settles the finding's open question. "Red when hit, normal when the chase ends" is the later client's `+0x971` rule, not a level effect.

**Why it is missing.** Build 14 predates that rule. For a level-1 player every Ssiyo is blue, whatever the server sends.

**Trigger chain with either patch**
1. S2C 0x1A spawn with server_controlled 0 sets `+0x971 = 0` (`0x457081`). The box is black (patch A) or the level colour (patch B).
2. A hit the monster survives makes the server send the 16-byte 0x2A self-form. `0x459C89` sets `+0x971 = 1`, and the box is red on the next frame.
3. Chase commands on 0x2A or 0x9F keep `+0x971 = 1`. 0x1B does not touch it.
4. Give-up: 0x9E sets `+0x971 = 0`, and the box returns to normal.
5. A kill (0x29) clears it; corpse tags are hidden anyway. A respawn (0x1A with 0) clears it.
6. Several monsters can be red at once.

**Server change** (current code already behaves this way when `MOB_AGGRO` is on)
1. **Spawn with server_controlled 0.**
   - `config.json:39` and `config.py:135` are already `false`.
   - The code fallback `self.config.get('MOB_SERVER_CONTROLLED', True)` at `windslayer_server.py:5012` is `True`. Change it to `False` so that a missing key cannot make every monster red and frozen.
2. **`MOB_AGGRO` must be true** (`config.json:42` is true).
   - Every 0x9E sender runs only when `MOB_AGGRO` is on: `_mob_ai_step` give-up, `_run_monster_ai` "no target to chase", `_release_aggro` on player death.
   - `_release_hit_lock` sends 0x2A after every caught hit the monster survives, whatever `MOB_AGGRO` says.
   - So with `MOB_AGGRO` off, a hit monster stays red (and frozen) until it dies.
3. **Hit, turn red.** `_release_hit_lock` → `_mob_command(STOP, form='2A')` sends S2C 0x2A (16 bytes): `<u32 mob uid> 00 00 00 00 00 00 00 00 <u32 receiver uid>`.
   - Hits the server detects itself (skills, DoT, traps) go through `_mob_aggro` → `_mob_send_decision`. That sends a 0x2A takeover first, even when `MOB_AI_COMMAND` is "1B".
4. **Chase ends, turn back.** `_mob_hand_back` sends S2C 0x9E (12 bytes): `<u32 mob uid> 00 00 00 00 00 00 00 00`.
5. **Expected side effects** (decide whether to act on them):
   - Stationary templates (AI[9]) are not aggroed. After a caught hit they stay red for about `HOLD_2A_SECS` = 0.96 s, until `_run_monster_ai` sends 0x9E ("no target to chase"). This is harmless, so leave it.
   - AI[5] proximity monsters turn red with no hit when the server takes them over. This mirrors the client's own AI[5] rule, so leave it.
   - `+0x971` is per client. With per-session monsters, which is how it works today, only the owner sees it. Once monsters are shared, every client that received the 0x2A sees the monster red.

**Client patch.** Choose one. Both start at `0x43C3CC`, so they cannot be combined.

*Option A, recommended: the KR rule (red while server controlled, black otherwise), 15 bytes. Checked with capstone.*

2009, VA `0x43C3CC`, file `0x3C3CC`:
```
orig 8B C2 80 B8 85 09 00 00 00 8B 88 88 09 00 00
new  80 BB 71 09 00 00 00 75 47 EB 4A 90 90 90 90
```
- This decodes as `cmp byte [ebx+0x971],0 / jne 0x43C41C (mov edi,0x2A) / jmp 0x43C421 (EDI stays 0x26) / nop x4`.
- `0x43C3DB..0x43C41B` becomes dead code. The only branches into that range come from inside it (`0x43C3E5`, `0x43C400`, `0x43C407`). The PvP path still jumps to `0x43C41C`, which is unchanged.
- The original bytes match in the corpus `WindSlayer_2009.exe` and in `WindSlayer`, `_p2`, `_patched`, `_patched_7011` and `_v7111` in `WindSlayer2009`.

2008, VA `0x43C390`, file `0x3C390`:
```
orig 8B C2 80 B8 6D 09 00 00 00 8B 88 70 09 00 00
new  80 BB E4 08 00 00 00 75 47 EB 4A 90 90 90 90
```
- The `jne` goes to `0x43C3E0` (`mov edi,0x2a`) and the `jmp` to `0x43C3E5`.
- The original bytes match in `WindSlayer`, `_hires`, `_p2`, `_patched`, `_patched.beforealive4` and `WSGame` in `WindSlayer2Game`.

The KR level colours for the name text are not ported. That would need a code cave, and it is optional.

*Option B: keep Build 14's level colour while idle, red while server controlled, 41 bytes.* Verified by the verifier: every exe has the original bytes; no stack read is left behind.

2009, VA `0x43C3CC`, 37 bytes:
```
orig 8B C2 80 B8 85 09 00 00 00 8B 88 88 09 00 00 8A 89 9D 00 00 00 88 4C 24 1B 74 0A 8A 8A 85 09 00 00 88 4C 24 1B
new  80 BB 71 09 00 00 00 75 47 8B 8A 88 09 00 00 8A 89 9D 00 00 00 80 BA 85 09 00 00 00 74 06 8A 8A 85 09 00 00 90
```
2009, VA `0x43C416`, 4 bytes: `38 44 24 1B` → `38 C1 90 90`.

2008, VA `0x43C390`, 37 bytes:
```
orig 8B C2 80 B8 6D 09 00 00 00 8B 88 70 09 00 00 8A 89 99 00 00 00 88 4C 24 1B 74 0A 8A 8A 6D 09 00 00 88 4C 24 1B
new  80 BB E4 08 00 00 00 75 47 8B 8A 70 09 00 00 8A 89 99 00 00 00 80 BA 6D 09 00 00 00 74 06 8A 8A 6D 09 00 00 90
```
2008, VA `0x43C3DA`: `38 44 24 1B` → `38 C1 90 90`.

With option B, an idle Ssiyo seen by a Lv 1 player gets a blue box. Whether A (black) or B (blue) matches the video's "dark teal" box needs a frame comparison (§4).

*Not recommended:* S2C 0x22 SetLevel on a monster (`u32 uid, u8 level`, level >= 100 to turn red).
- Every send plays the level-up pillar and `level_up.wav` on the monster.
- Reverting to a level of 1..99 recomputes the monster's HP/MP with the player formula.

**Corrections applied**
- The finding's list of writers of `+0x9D` was incomplete: summon/pet spawns `FUN_00447f40`/`FUN_00448350`, `FUN_00461ad0`, and `FUN_0041b830` (level 49 in some rooms). None of them changes a field monster.
- `FUN_0043bcb0` also draws an emote icon (`+0x98`, for 3 s). It is unrelated to the box.
- The premise that "the server already keeps `+0x971` in step with aggro" holds only with `MOB_AGGRO` on.

### 2.2 Body-contact gate (monsters the player has not hit are harmless)

**Verified facts**

When a monster is ticked:
- The tick gate for a type-4 entity (`0x416B50..0x416B69`) is `(+0xEE4 && +0x971) || +0x96D`. `+0x96D` is cleared at `0x416B79`.
- The client AI sets `+0x96D = 1` at `0x418BBF`. Its gate (`0x418B22..0x418B66`) needs `+0x971 == 0`, an empty command queue and `+0xEE4 == 0`.
- So a wandering monster is always ticked. A server-controlled monster is ticked only while a command hold runs.

The contact block, `0x416BF2..0x416E18`, is in `FUN_00416ab0`, the last call of each 30 ms substep (`0x42CB80`). It checks:
- the monster:
  - type 4 (`0x416BF2`)
  - `+0xF00 >= 3` (`0x416BFF`)
  - `(+0xF04 & 0x10) == 0` (`0x416C0C`)
  - `+0x9E0 == 0` (`0x416C19`)
  - its state is not 0x17, 3, 0x10 or 0x16
- the victim:
  - it is the local player (unless in room mode 1)
  - type 3
  - not the monster's `+0xE18` (a puppet caster)
  - its state is not 0x14, 3, 0x17, 0x10 or 0x16
  - its i-frames `+0xE8C <= 0`
- that the two body rects overlap strictly.

It then writes player `+0x9DC = 6` (`0x416DD6`), or 1 or 6 when guarding, player `+0xE10` = the monster's uid, and `+0x9E8 = 250`. It reads neither `+0x971` nor `+0xE44`.

Template fields:
- `+0xF00` and `+0xF04` are the hni columns `type:` and `AtkType:` (`template+0x654` / `+0x670`), copied by 0x1A (`0x456BA4` / `0x456BB0`).
- Ssiyo has type 3 and AtkType 0. Only template 190 (indun_stone) has AtkType 0x10.
- No S2C packet changes them on a live monster.

The player's reaction is local and happens before any server reply:
- On the next tick the C2S 0x0D goes out, with action 6 and `event_source_uid` = the monster (`FUN_0042e1b0`, `0x42CB2A`).
- The hit resolution (`0x412D89` / `0x413B16`) adds the estimate to `+0x11B4` and sets hurt state 3 (`0x413C15`).
- Then come 270 ms of i-frames (`0x41531A`).
- S2C 0x28 only moves the HP globe.

Aggro target and the later client:
- `+0xE44` (aggro target) is written only in host mode, behind `scene+0xF40`, and `scene+0xF40` is only ever stored as 0 (`0x4128B3`). So it is always 0 in the field.
- The later client's gate, in both KR exes at VA `0x43BAE0` (file `0x3AEE0`): `cmp byte [ebx+0x9B9],0 / jne / cmp dword [ebx+0xE8C],0 / je skip`, placed in front of the same checks. The 2009 and 2008 clients lack it.

**Why it is missing.** Build 14 predates the gate.
- `MOB_CONTACT_AGGRO_ONLY` (on, `config.json:45`) already drops the HP loss from touches by monsters that are not after the player.
- But the flinch, knockback, red digit and impact word are drawn before the server sees the report.
- No S2C can stop this for a monster that is still wandering:
  - The only wire-reachable per-monster switch is `+0x971`. Setting it without a hold also freezes the monster.
  - The other gates are template constants or short-lived client values.
  - The one remaining lever is the Puppet buff (`+0xE18`, buff ids 0xA3C..0xA46). It makes the monster copy the caster's input and draws an effect, so it is not usable.

**Trigger chain with the patch**
1. The monster spawns with server_controlled 0 and wanders. The player touches it. The new check at `0x416C16` sees `+0x971 == 0` and skips. No flinch, no digit, no C2S 0x0D action 6.
2. On the first hit the monster survives, 0x2A sets `+0x971 = 1`. From then on contact works while the monster is ticked, that is, while a command hold runs.
3. The server's chase words (0x2A at least every 960 ms, direction 1 or 2) keep it ticked. A touch then gives event 6, a flinch, a C2S 0x0D action 6, `damage_player` and S2C 0x28.
4. 0x9E sets `+0x971 = 0`, and the monster is harmless again.

**Server change** (the wire format is unchanged; most of this is already in place)
1. **0x1A server_controlled = 0 for every field monster.**
   - Correction to the finding: with server_controlled 1 and hold 0 the monster is not ticked, so it is frozen *and harmless* on every client. It is not "harmful from spawn". It just never wanders.
2. **No 0x2A or 0x9F to a monster that is not aggroed**, and no server-driven wandering. Any 0x2A/0x9F makes that client's copy able to hurt.
3. **The keep-alive must be a moving word** (direction 1 or 2).
   - A neutral word (lo 0, for example the 16-byte release) has its hold zeroed by the consumer at `0x412AF6` as soon as the monster stands in state 8 with neutral inputs. The monster then stops ticking, and under the patch it cannot touch anyone. This is fine for a stunned monster.
   - `_mob_send_decision` already never sends 00 to a chasing monster; its keep-alive period is `MOB_CMD_KEEPALIVE_SECS`.
4. **0x9E on give-up** (`MOB_LEASH_PX`, or `MOB_AGGRO_TIMEOUT_SECS` = 15 s).
   - 0x9E does not flush the command queue or zero `+0xEE4`, so the monster may stand still for up to about 1 s before it wanders again. It is harmless during that time.
5. **Keep the unpatched-client safeguards:** `MOB_CONTACT_AGGRO_ONLY = true`, the `MOB_SWING_EVENTS` filter (`windslayer_server.py:3732`) and `CONTACT_MIN_SECS = 0.5` (`:3345`).
6. **Not covered by the patch or by KR:** swing events 7-10 from AI[0]/AI[8] monsters (Rynx, for example) that roll an attack while wandering (`0x418C31..0x419274`).
   - The server filter drops the damage, but the local flinch still shows.
   - Ssiyo has no attack rect, so it is not affected.

**Client patch**

2009, VA `0x416BF2`, file `0x16BF2`, 52 bytes:
```
orig 80 BD 9C 00 00 00 04 0F 85 1F 02 00 00 83 BD 00 0F 00 00 03 0F 82 12 02 00 00 F6 85 04 0F 00 00 10 0F 85 05 02 00 00 83 BD E0 09 00 00 00 0F 85 F8 01 00 00
new  80 BD 9C 00 00 00 04 75 24 83 BD 00 0F 00 00 03 72 1B F6 85 04 0F 00 00 10 75 12 83 BD E0 09 00 00 00 75 09 80 BD 71 09 00 00 00 75 05 E9 FA 01 00 00 90 90
```
- The four original checks are kept. Their 32-bit exit jumps become short jumps to one shared `jmp 0x416E1E` at `0x416C1F`.
- The new `cmp byte [ebp+0x971],0 / jne 0x416C24` falls into the original code at `0x416C26`.
- Checked:
  - all five 2009 game exes have the original bytes
  - no branch enters `0x416BF3..0x416C5F`
  - no absolute pointer points into that range
- The KR aggro-target term (`+0xE44`) is left out because it is always 0 in 2009 field mode. The finding also gives a 110-byte KR-exact variant that includes it.
- **Do not use** the 13-byte alternative in the digits finding (file `0x16C0C`, which replaces the AtkType test). It overlaps this patch and drops the AtkType 0x10 guard.

2008, VA `0x4164C5`, file `0x164C5`, 92 bytes. This one is KR-exact: server controlled `+0x8E4` OR aggro target `+0xDB4`. The original bytes match in six 2008 exes, and the only branch into the range goes to the loop head `0x416521`.
```
orig 80 BD 98 00 00 00 04 0F 85 0D 02 00 00 83 BD 64 0E 00 00 03 0F 82 00 02 00 00 83 BD 50 09 00 00 00 0F 85 F3 01 00 00 8B 85 04 09 00 00 83 F8 17 0F 84 E4 01 00 00 83 F8 03 0F 84 DB 01 00 00 83 F8 10 0F 84 D2 01 00 00 83 F8 16 0F 84 C9 01 00 00 8B 7B 0C 85 FF 0F 84 BE 01 00 00
new  80 BD 98 00 00 00 04 75 42 83 BD 64 0E 00 00 03 72 39 83 BD 50 09 00 00 00 75 30 0F B6 85 E4 08 00 00 0B 85 B4 0D 00 00 74 21 8B 85 04 09 00 00 83 F8 17 74 16 83 F8 03 74 11 83 F8 10 74 0C 83 F8 16 74 07 8B 7B 0C 85 FF 75 11 E9 CA 01 00 00 90 90 90 90 90 90 90 90 90 90 90 90
```

**Corrections applied**
- The finding's rule D.1 was wrong about server_controlled 1 (see server change 1 above).
- The neutral-word hold is zeroed as soon as the monster idles, not after 960 ms.
- A 0x9E hand-back can leave the monster standing still for up to about 1 s.
- The guard path only tests "facing differs" (`0x416D60..0x416D6C`), not strictly "facing the monster".
- The contact verifier's claim about digit colour is itself wrong (see §2.3).

### 2.3 Damage digits, impact words, cry bubbles

**Verified facts**

Tick order:
- Main tick `FUN_0042c920`, each 30 ms substep:
  - AI `0x42CB14`
  - C2S builder `0x42CB2A`
  - command queue `0x42CB48`
  - hit resolution `FUN_00412c60` at `0x42CB56`
  - state machine `0x42CB64`
  - movement `0x42CB72`
  - hit detection `FUN_00416ab0` at `0x42CB80`
- After the substep loop, in room mode 0: the digit emitters `FUN_00431830` (`0x42CCD6`) and `FUN_004319e0` (`0x42CCDB`), then the render-state pass `FUN_004510a0` (`0x42CD4B`).
- Correction: the finding cited `0x42CC94` and `0x42CCBD`. Those belong to a per-entity catch-up pass that runs only for players.

The damage estimate, `FUN_004194f0` (no randomness):
```
t   = A / (A + D)
t   = t * 2.0            ; constant at 0x52EEE8
t   = t * La
t   = t / (La + Lv)
dmg = trunc(A * t)
```
- A depends on the event:
  - events 1 and 6: the attacker's Body_Atk `+0x123C`
  - events 7 and 8: Weak_Atk `+0x11D4`
  - events 4, 5, 9 and 10: Strong_Atk `+0x11D0`. Events 4 and 9 also add a skill-table term that is not decoded yet (`0x4197DA`).
  - events 2 and 3: nothing
  - Events 5, 8 and 10 are remapped to 4, 7 and 9 at `0x419549..0x419569`.
- D = the victim's Def `+0x11E0`, plus `+0x122C` for events 1 and 4.
- La and Lv are the attacker's and victim's levels (`+0x9D`).
- Element and buff terms are added after that.
- The result is clamped to at least 1 (`0x41A55A`) and at most the victim's `+0xA0` (`0x41A689`).
- No popup if the victim's HP is below 1 (`0x419525`) or the victim is a hidden GM.
- In the field (room mode != 1 and `scene+0xF40 == 0`) the function skips the HP subtraction and adds dmg to the victim's `+0x11B4` (`0x41A936`). Delayed slots use `+0x11BC`/`+0x11C4`; mana shield uses `+0x11B8`.

**[new, settles the disagreement] Digit colour.** Re-read at `0x431885..0x4318D4`:
- The emitter tests room mode first: `CMP [EDX+0x7C],EBX(=1) / JNZ 0x4318BF`.
- In the field (room mode != 1) it tests the victim's type:
  - type 3 (player): kind 0, red digits (`basic.hsi` 6..15)
  - anything else: kind 1, green digits (0x5F..0x68)
- Only in PvP room mode 1 does it compare the team byte `+0x158B`.
- So Build 14 already draws green at the monster and red at the player, as in the video. The contact verifier's "team byte in the field" reading was wrong.
- Digits live 1000 ms (`0x42E839`) and start fading after 822 ms.

Words and cries:
- `FUN_004511b0` calls `FUN_0042d110` at `0x4516AE` when an entity's render state changes.
- Entering state 3 or 0x17 from a state that is not already a hurt state spawns, in order:
  - a spark and `Play_Sound(1)`
  - `gs+0x470 = 50`, for the local player only
  - for type 4 only: a cry `0x9F + rand%3` (AHRGH!/OUCH!/UGH...) at x±40 depending on facing, lasting 500 ms (`0x42D2CE`)
  - an impact word `0x9C + rand%3` (KRAKK/CHUD/THUD), lasting 270 ms (`0x42D300`)
- The art comes from `efftext001_a1.hsc`. "TWAK" is not in the 2009, 2008 or KR data.
- Two cases produce no word or cry:
  - Guarded hits (events 1-5) show a digit but never enter state 3.
  - A non-zero `+0x98B` (skill render state) overrides the render state.

What packets do and do not do:
- S2C 0x28 (`0x459731`) only stores the HP and refreshes the globe.
- None of the 20 popup calls in the S2C dispatcher creates a digit.
- The only packet writers of `+0x11B4` are the arena UDP opcodes 0x06/0x0C/0x0E, gated to room mode 1 (`0x45EC28`).

**Why it looks different.** It works. Two differences remain:
1. Touches from monsters the player has not hit also produce the red digit and impact word. The contact patch (§2.2) fixes that.
2. The digit is the client's estimate. The server applies `combat.body_damage = max(1, Body_Atk - def)` (`combat.py:181`, used at `windslayer_server.py:3770`). The two disagree.

**Server change (optional, so the numbers are truthful)**
- **Port the estimate** for monster-to-player contact (`_monster_contact`) and for player-to-monster hits. Keep the operation order shown above.
  - Ssiyo touching a player: trunc(18 / ((3 + Def) × (1 + Lp))). Correction to the finding: this equals floor(9/(3+Def)) only when Lp = 1.
  - A player with Weak_Atk W hitting Ssiyo (Def 2, Lv 1): trunc(W·W/(W+2) · 2·Lp/(Lp+1)).
  - Take the player's Weak_Atk, Strong_Atk and Def the way `FUN_0041b830` computes them. That function recomputes any entity, monsters included (correction).
  - The client does this in x87 float math with A as float32. Exact-integer boundaries (for example A=6, D=3, La=Lv=1) may truncate differently, so verify live.
- **Keep the green digits' HP cap current (optional).** In the field the client's copy of the monster's `+0xA0` stays at the spawn value.
  - Send S2C 0x54 `{u32 uid, u16 max_hp, u16 cur_hp}` (8 bytes). The handler at `0x45C897` writes `+0x11AC` and `+0xA0` for any entity. The party-frame refresh it also calls finds no widget for a monster.
  - Never send cur_hp 0 for a live monster: `0x419525` would then hide all its digits.
  - **Do not use S2C 0x40** (correction). Its HP write applies only to the local player (`0x459974..0x45998E`). On a monster it only plays heal sparkle 0x37 and a sound.
- **Other players watching (multiplayer, inferred, only once monsters are shared).** To show someone else's hit, send the watcher (never the attacker) one of these:
  - the full 0x2A `{u32 mob, u32 lo with bits 12-15 = 6 or 7, u32 hi 0, u32 target = attacker uid, f64 x, f64 y, u8 airborne}`
  - 0x1B `{u32 mob, u32 hold, u32 lo, u32 hi, u32 target}`

  Two limits:
  - 0x9F cannot carry a target: node+8 is forced to 0 at `0x459DB1`. It gives the flinch, word and cry but no digit (correction).
  - A watcher's 0x2A also sets that watcher's `+0x971 = 1`, which means a red tag and enabled contact under the patches.

### 2.4 Grade word ("Good"/"BAD") and "N Combo!" counter

**Verified facts**

No combo counter anywhere:
- No "Combo" string (ASCII or UTF-16) in the exe, `UILngKo.lng`, `windslayer.hui` or `StringTable-0009-English.ips`.
- No "Combo!" image in any atlas the client loads (all were rendered and checked).
- None in the KR 2025 data or exes either.
- The only big-number HUD is the PvP scoreboard `FUN_00432760`, which runs in room mode 1 only.

The grade art exists but no code uses it:
- `basic.hsi` sprites 332/333/334 (0x14C/0x14D/0x14E) are Good! (green), Great! (orange) and Wow! (red-orange), on `fx001_a8.hsc` region 320..740 × 665..731.
- The anchor is -50,-163, which is above the head. Each is 3 frames of 80/80/500 ms at scale 1.5/0.9/1.0, with no repeat.
- The 2008 `basic.hsi` does not have them (324 sprites). 2009 has them (341 sprites), and so does KR 2025 (450).
- No instruction or data table in Build 14 references these ids as constants, and KR 2025 has no immediate reference either.
- There is no BAD, Miss, Cool or Perfect art. The orange word in the video is most likely Great!.

The attack chain:
- `+0x979` is the stage (0/1/2) of the basic 3-hit chain. It is set only from key timing in `FUN_00414210`:
  - the input windows are 410-610 ms and 1050-1250 ms
  - the stage lengths are 610 ms, 1250 ms, and the animation length
- It never checks whether a swing landed, and it is not in C2S 0x0D.
- `+0x95F` counts the victims of one swing (`0x417D3A`, `0x41AC56`).

**Correction: the server can show a grade word.** The finding said no packet could; this was verified again for this document.
- **The renderer.** `FUN_00432e10` (frame root, `0x43E54F`) runs for every type-3 entity with `+0x1594 != 0` (`0x432E72`), and does not exclude the local player. It:
  - draws the `basic.hsi` sprite whose id is the u16 at `+0x15AE` (`0x432EB0`, DrawSprite at `0x432EC0`), at the entity's drawn position `+0x165C`/`+0x1660`, timed by the render-state clock `+0x1664`
  - then draws the 25-byte title at `+0x1595` as text at (x+5, y-140) (`0x432EF9..0x432F11`)
- **S2C 0x85 StallOpenedBroadcast** (handler `0x47751C`): `{u32 owner_uid, u16 sign_sprite, char[25] title}`, 31 bytes. It is ignored when the uid is unknown or `+0x1594` is already set (`0x47754B`). Otherwise it sets `+0x1594 = 1` and reads the sprite and the title.
- **S2C 0x86 PlayerStallSignRemove** (`0x4775AC`): `{u32 owner_uid}`. It clears `+0x1594` and `+0x15AE`. It shows "The shop is closed or adjusting." only if that uid is the stall this client is browsing (`game+0xDBC+4`, which is 0 after login).
- **Every other reader of `+0x1594`** (full grep):
  - the sign click hit-test (`0x450301`, box x±0x67, y-0x6E..-0x50). Hovering changes the cursor, and a click sends C2S 0x61 ItemStallVisitRequest (`0x450791`).
  - the 0x07/0x04 player-record parser
  - the stall-placement proximity check (`0x470CF7`)

  None of them affects movement or attacks.

**Server action (experimental; behind a new flag, off by default)**
1. **Track per player {count, last_ms}** from accepted hit reports: C2S 0x0D event 7, source = the local player, target = a monster. If now - last_ms < WINDOW, count++; otherwise count = 1. The retail WINDOW is unknown; try 2000-3000 ms.
2. **Choose a grade.** The retail rule is unknown (inferred). For example, map the chain stage or the count to 0x14C/0x14D/0x14E.
3. **Show it.**
   - If a word is already up, send 0x86 `{uid}` first.
   - Then send 0x85 `{uid, 0x014C|0x014D|0x014E, 25 zero bytes}`. Example, uid 1 and Good!: `01 00 00 00 4C 01` followed by 25 × `00`.
   - Instead of zeros, the title can carry text such as "3 Combo!". It is drawn over the head, not as the retail HUD at the right edge.
   - About 700 ms later send 0x86 `{uid}`. Otherwise the last frame stays on screen.
4. **Timing caveat.** The pop animation is timed from `+0x1664`, which resets when the player's render state changes (`0x451316`), not when the packet arrives.
   - If 0x85 arrives more than about 160 ms after the swing's state change, the word appears at its final frame with no pop.
   - While the word is shown, the pop replays at every later render-state change.
5. **Guards.**
   - Never do this while the player has a real stall open.
   - Reject C2S 0x61 for a uid that carries a fake sign.
   - Other players cannot place a stall next to a flagged player while the word shows (`0x470CF7`).

**Combo counter.** It needs a client patch or the later client. Only a design exists; no bytes are written.
- Hook the victim counter at `0x417D3A` (attacker in EDI) and `0x41AC56` (attacker in ESI), for the local player's first victim of each swing.
- Keep {count, last_ms} in a spare `.data` slot.
- After `0x43E565`, draw green digits 0x5F+d on `DAT_0054e844` in screen space (clip -0x80000000, -0x80000000, 0x640, 0x4B0), and the word with `CIME::Draw_Text` (`DAT_0054e840`).
- A grade word by patch: `FUN_0042cd90(0x54EBD0, 0x14C+min(+0x979,2), 0, 0, +0x165C, +0x1660, 1, 5, 0)`, which is stdcall with 9 arguments (`RET 0x24`).
- Low priority.

## 3. Live-test plan

**Rules**
- Run tests only when the user is not playing.
- Patch a copy of the exe that is normally launched (for example `WindSlayer_patched.exe` copied to `WindSlayer_feel.exe`), and keep the original.
- Make one change per run and restart the client between runs.
- Use a level-1 Novice in Novice Hunting Park against Ssiyo.
- Use the server in `server_aggro` with `config.json` as it is: `MOB_AGGRO` true, `MOB_AI_COMMAND` "2A", `MOB_CONTACT_DAMAGE` true, `MOB_CONTACT_AGGRO_ONLY` true, `MOB_SERVER_CONTROLLED` false.
- Set logging to DEBUG so that ignored touches are logged.

**Patch helper** (it refuses to write unless the original bytes match):
```python
# usage: python apply_patch.py <exe> <va_hex> <orig_hex> <new_hex>
import sys
exe, va = sys.argv[1], int(sys.argv[2], 16)
orig, new = bytes.fromhex(sys.argv[3]), bytes.fromhex(sys.argv[4])
off = va - 0x400000
d = bytearray(open(exe, 'rb').read())
assert len(orig) == len(new) and d[off:off+len(orig)] == orig, 'original bytes differ: stop'
d[off:off+len(new)] = new
open(exe, 'wb').write(d)
```

**Server log lines to watch** (from `windslayer_server.py`):
- `[AGGRO] <name> uid=0x... turns on uid 0x...` (aggro starts)
- `[AGGRO] <name> uid=0x... -> <word> (0x2A lo NN)` (a new command word)
- `[AGGRO] <name> uid=0x... handed back to the client wander (0x9E): <why>`
- `[DAMAGE] '<char>': <name> uid=0x... <kind> (C2S 0x0D action 6): -N HP a->b`
- DEBUG: `[DAMAGE] <name> uid=0x... is not after this player: touch (C2S 0x0D action N) ignored`

**Run 0: baseline (no patch, no server change)**
1. Walk through a Ssiyo that has never been hit.
   - Expect on screen: flinch and knockback, a red digit over the player (trunc(18/((3+Def)·2)) at Lv 1), an impact word, no cry, and the HP globe unchanged.
   - Expect in the log: at DEBUG, the "not after this player" line; no `-N HP` line.
   - If the HP globe drops, the running server is not using `server_aggro`'s config. Fix that before going on.
2. Hit a Ssiyo once without killing it.
   - Expect on screen: a green digit at the monster, a word and a cry. The box stays blue.
   - Expect in the log: `turns on uid`, then `(0x2A lo ..)` lines.
3. Record the digit colours. This confirms red at the player and green at the monster.

**Run 1: contact patch only (§2.2)**
1. Walk through a Ssiyo that has not been hit, several times and from both sides.
   - Expect: no flinch, digit or word, and no C2S 0x0D action 6. At DEBUG, no "touch ... ignored" line from that monster.
2. Hit it once.
   - Expect: it chases, with moving `0x2A` words at least every 0.96 s.
   - When it touches the player: flinch, red digit, word, a `-N HP` line, and the HP globe drops.
3. Break the chase: run past `MOB_LEASH_PX`, or wait 15 s without hitting.
   - Expect: a `handed back ... (0x9E)` line. The monster may stand still for up to about 1 s, then it wanders.
   - Walk through it: nothing happens.
4. Failure signs:
   - A chasing monster never hurts: check that the command words move (direction 1 or 2) and are refreshed within 0.96 s.
   - A monster stays frozen after the hand-back: check for a leftover hold.

**Run 2: tag patch A (§2.1)**, applied to the run-1 exe or alone
1. After spawn, every monster box should be black and translucent. NPC tags are unchanged (black box, green name, title).
2. Hit a Ssiyo: its box turns red on the first hit it survives, at the same moment as the `0x2A` line. Hit a second one: both are red.
3. At the `0x9E` line the box turns black again.
4. A one-hit kill shows no red, and the corpse tag disappears.
5. A stationary template goes red for about 1 s, then back. An AI[5] monster turns red when the server takes it over.

**Run 3: tag patch B instead of A** (optional). The idle Ssiyo box should be blue, and red while the monster chases. Compare A and B against a frame of the video.

**Run 4: grade-word experiment** (needs new server code behind a flag)
1. First send by a dev command: 0x85 `{own uid, 0x014C, 25×00}`, then 0x86 after 700 ms.
   - Expect: "Good!" above the head.
   - Note whether the pop plays or the word appears at its final frame.
   - No "shop is closed" box should appear.
   - Click above the head: a C2S 0x61 must be logged and ignored.
2. Only then wire it to the hit reports.

**Run 5: damage formula port** (needs server code)
1. For 10 touches, compare each red digit with the `-N HP` line.
2. Check that the kill comes on the hit whose green digits add up to the template HP.

## 4. Open questions

1. **Which client the 2011 video used.** Many things point to a build later than Build 14: the combo counter, grade words, "TWAK", a small level box next to monster names, red-on-combat tags, and the contact gate. Neither Build 14 nor KR 2025 draws a level box for monsters (in KR the level gap shows as name-text colour). The later EN client is the only way to settle the grade rule, the combo window and hold time, and the level box.
2. **Idle box colour.** KR draws black (patch A); Build 14 draws the level colour, blue for Ssiyo at Lv 1 (patch B). Compare a video frame of an idle Ssiyo; "dark teal" fits both. If unsure, use A, because it is the later client's code.
3. **The "BAD" grade word.** No BAD art exists; Great! (orange) is the likely match. Re-check the frame.
4. **Multiplayer.** `+0x971` is per client. Once monsters are shared, decide:
   - whether every watcher gets the 0x2A takeover (the monster is red and able to hurt on every client), or only the attacker
   - whether to relax the server's hate-list filter (`MOB_CONTACT_AGGRO_ONLY`) to match
5. **Monsters that swing while wandering** (AI[0]/AI[8], for example Rynx). Neither the patch nor KR gates their swing events 7-10. How retail handled these is not known.
6. **AI[5] proximity monsters.** They turn red and harmful without being hit. This is consistent with the client's own AI[5] rule, but it has not been checked against retail.
7. **Standing still after a hand-back.** Up to about 1 s, because 0x9E does not flush the queue. This is cosmetic; a fix would need a different hand-back order, which has not been designed.
8. **Parts of `FUN_004194f0` not decoded yet:** the element and buff terms after `0x419FD6`, and the skill-table term for events 4/9 (`0x4197DA`). The x87 precision mode may also matter. All of this is needed only for an exact server port with gear or skills.
9. **The stall-sign grade word is static-only.** The pop timing against `+0x1664`, and whether retail showed the word to other players, are not verified.
10. **Porting the KR name-text level colours** (`0xFF46C8FF`/`0xFFFF6482`/`0xFFFFFF78`) to Build 14 would need a code cave. It is optional and not designed.