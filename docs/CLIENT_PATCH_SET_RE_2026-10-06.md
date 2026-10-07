# Client patch set G-CP: data patches cp-1 and cp-4, exe patch cp-2 (EN 2009, Build 14), 2026-10-06

Scope: three patches of gate G-CP (approved by the user). All of them are built as copies. Nothing in `C:\Users\ohdri\Desktop\WindSlayer2009` was written, except the two exe build scripts that cp-2 was allowed to edit (`patch_2009.py`, `combo_hud_2009.py`; the originals are kept next to them as `*.pre-cp2.py`).
- **cp-1**: pet sprite templates in `hs\windslayer.hii` (sections 0-7).
- **cp-4**: the "join guild" entry in the player right-click menu, in `hs\windslayer.hui` and `hs\UILngKo.lng` (sections 0-7).
- **cp-2**: the exe +4 item-id shift, a new default-ON patch in `patch_2009.py` (section 8, at the end).

cp-3 (the Pet Bell source) is not part of this work.

Specs: ROADMAP_2009_ADDENDUM 3.0 `arch09-client-patch-set` and P15; `systems_2009/pet.md` 1 B1/B2/B4 and 9 stage 0; `systems_2009/guild.md` 1.5, 1.6, 12 Q1/Q11; `live_harness/p12p14_live_triage.md` finding 2.

Tags: [V] verified (in the corpus, in the data or by running code). [I] inferred.

---

## 0. Result

| Patch | File | Change | Pristine SHA-1 / size | Patched SHA-1 / size |
|---|---|---|---|---|
| cp-1 | `hs\windslayer.hii` | `CardNpc` 0 → 182..185 on 20 rows (EN ids 4290..4309) | `82615c9f…62be` / 2,199,660 | `e759b301ebabb2eb0d005856897062c894abb7a0` / 2,199,700 |
| cp-4 | `hs\windslayer.hui` | Window 80 (0x50): `Size 137 220 → 137 240`, `Bt_No 11 → 12`, and control 12 inserted | `47fa6b7a…8c9d` / 1,800,149 | `634e97c029924906d19f65c15a888de895762004` / 1,800,412 |
| cp-4 | `hs\UILngKo.lng` | Inserted `8038 Apply#to#Guild` between 8034 and 8039 | `070195b7…14c5` / 252,759 | `c6cea897f7ec0bac414c861cedd5eb1457267be0` / 252,780 |

- Every patched file decodes. Both ciphered files pass the footer check, both in the Python re-implementation and in **the client's own `ValidationCheck.dll` code run under unicorn**, where a copy with one byte flipped is rejected.
- A structured diff of every file equals the intended change set and nothing else.
- The cp-1 output is **byte-identical** to the candidate `_work/pet/windslayer_2009_petfix.hii`.
- Clicking the new control sends **C2S 0x89 with a 4-byte (u32) guild id**, the id of the target's guild [V asm].

Staged files:
- `Windslayer 2\client_patches\patch_data_2009.py`: build, verify, status, install and uninstall.
- `Windslayer 2\client_patches\repo_stage\client_2009\patch_data_2009.py`: an identical copy for the repo's `client_2009\`. It has not been committed.
- `Windslayer 2\client_patches\data_2009\hs\{windslayer.hii, windslayer.hui, UILngKo.lng}`: the built copies.
- `Windslayer 2\client_patches\vcheck_emu_2009.py`: a read-only DLL-emulation checker (needs `unicorn` and `pefile`).
- cp-2: `Windslayer 2\client_patches\WindSlayer_patched_cp.exe`, `WindSlayer_p2_cp.exe` and `verify_cp2.py` (section 8.6).

---

## 1. Sources and safety

**Pristine sources.**
- The installer `installers_2009\WindSlayer-01_04_0000.exe` was unpacked into the scratchpad:
  - first its overlay, whose entries are `name\0path\0ver\0size\0` + bytes (`data1.hdr`, `data1.cab`, `data2.cab`);
  - then only the needed files, through `iscab.Cab`.
- The installer's `windslayer.hii`, `windslayer.hui`, `UILngKo.lng`, `ITMLngKo.lng`, `windslayer.hni`, `ValidationCheck.dll` and `WindSlayer.exe` all have the **same SHA-1** as the files in `WindSlayer2009`. So the install is pristine for these files [V].
- The SHA-1s are pinned in the script (`PRISTINE`).

**Writes.** Writes went only to the scratchpad and to `Windslayer 2\client_patches\`. The `WindSlayer2009` and `WindSlayerKR` folders were only read. For example, `--status` on the real install reports all three files `pristine` with no backups.

**Hazard found while working: Desktop folder sync.**
- `sed -i` (write a temp file, then rename it over the original) on `client_patches\patch_data_2009.py` raced a folder-sync client (it left a "Name clash" copy of the script).
- The sync client put the **previous** content back under the real name and moved the new content into the clash copy. This was found, fixed and the clash file deleted.
- `--install` also replaces files by rename (`.gcp-tmp` + `os.replace`). It therefore re-reads every file after `--settle` seconds (default 10) and lists stray `* Name clash *` / `*.gcp-tmp` files. `--status` lists them too.
- The lead should pause the sync client (or exclude `WindSlayer2009` from it) before `--install`, and run `--status` again a little after installing.
- It happened again during the review fixes: an in-place edit of `patch_data_2009.py` came back one edit behind, with the newest text in a `(# Name clash ... #)` copy. The newest text was restored and the clash file deleted; later pushes were written in place (open, write, truncate, no rename) and re-checked after 12 s.
- Every revert combination is benign, though (checked by the review): stock hii = pets invisible; stock hui = the stock menu; stock lng with the patched hui = an empty label, because the hui loader clears the control struct per line before the text lookup.

---

## 2. File formats and the client's checks (RE)

### 2.1 Cipher and footer: `ValidationCheck.dll` [V disassembly + emulation]

Exports (image base 0x10000000):

| Export | VA | Behaviour |
|---|---|---|
| `CheckValid(this, const char* buf, int len, uchar* hash)` | 0x10001050 | Loads the standard SHA-1 IV (67452301 EFCDAB89 98BADCFE 10325476 C3D2E1F0) into `this`. Calls update at 0x100027D0 over `buf[0:len]` and final at 0x100028A0 into a local digest. Compares 20 bytes with `hash`. Returns `cmp == 0`. |
| `MakeHashCode` | 0x10001170 | The same SHA-1, with no compare. |
| `Decode(buf, len)` | 0x10001220 | `buf[i] += {E9, DE, E0}[i % 3]` |
| `Encode(buf, len)` | 0x100011D0 | `buf[i] += {17, 22, 20}[i % 3]` |

How the loaders use it:
- The hui loader FUN_00495ED0 and the hii loader FUN_00403950 copy the last 0x14 bytes and call `CheckValid(buf, size - 0x14, footer)`. They call `Decode` only if that returns 1; otherwise they abort.
- So the **footer = SHA-1 of the ciphered body**. pet.md B1 found the same.

Emulation proof (`vcheck_emu_2009.py`):
- It maps the real DLL in unicorn, with stubs for `memcpy`/`memset`.
- It calls the exported `CheckValid` and `Decode` exactly as the loaders do.
- Result:

| File | CheckValid | DLL Decode == Python decoder | One byte flipped |
|---|---|---|---|
| `data_2009\hs\windslayer.hui` (patched) | 1 | yes | 0 |
| `data_2009\hs\windslayer.hii` (patched) | 1 | yes | 0 |
| `WindSlayer2009\hs\windslayer.hui` (stock, control) | 1 | yes | 0 |

`list.hcd`, the other "hash-looking" file, is the curse-word filter: a zip with one entry `abcd`, ciphered text "Parse Text Curse FIle ...". There is **no** other integrity list over `hs\` [V].

### 2.2 `UILngKo.lng` is plain text [V]

- FUN_0048F590 builds the path `GetCurrentDirectory + "\hs\" + "UILngKo.lng"` (DAT_005269EC = `\hs\`).
- FUN_00405020 then does `fopen(path, "r")`, and `fscanf("%d %s")` until EOF:
  - `#` becomes a space;
  - each record is a 0x408-byte node {id, text[1024]}, appended to a linked list.
- There is **no cipher and no footer**: the stock file ends in `9352 Item#Name100\r\n`, and SHA-1 checks of either form fail.
- Lookup FUN_004051F0 walks the list linearly by id, so the first match wins.
  - A missing id logs `Error(Can't Find) %d` and leaves the destination buffer as it was.
  - That is why stock EN cannot show a control with text 8038.
- EN file: 3,600 records, ids unique and sorted, CRLF. It is ASCII except for **212 bytes**: 106 CP949 double-byte characters, mostly markers (♡ ×41, □ ×23, ※ ×6, e.g. id 8029 `※Individual#item#price.`) plus one untranslated Korean cash-shop notice (id 7978). The patch adds only ASCII, and the script's ASCII check exempts this file. It has no 8038 (nor 8036/8037, see 4.6).
- KR 2025 file: 3,762 records in CP949, and `8038 길드가입신청` ("guild join request").

### 2.3 `windslayer.hui` grammar (FUN_00495ED0) [V]

**Tokens.**
- Tokens are split on `' '` and `'\r'`; `'\n'` is skipped.
- Line 1 `Number_of_UI: N` only arms the parser. N is not used as a loop bound.
- A **window header** line has these tokens:
  - 0 window id;
  - 1 title text id (lookup, 0x41 chars);
  - 3 sprites;
  - 5/6 pos, 8/9 size, 0xB type, 0xD stretch, 0xF Check_UI, 0x11 Not_Close, 0x13 First;
  - **0x15 Bt_No**: the number of control lines that follow;
  - 0x17 Data.
- A **control** line has these tokens:
  - 2 text id (lookup, 0x81 chars);
  - 4 sprites;
  - 6/7 pos, 9/10 size;
  - 0xC type. A type other than 7 skips `Mul_Txt:`. Type 7 has `Mul_Txt: id` (lookup, 0x3E9 chars).
  - 0x10/0x11 Text_Pos, 0x13 Flag, 0x15 Color, 0x17 Type_Color, 0x19 Stretch, 0x1B Button, 0x1D Title, 0x1F Event, 0x21 Link, 0x23 Return, 0x25..0x2B Up/Down/Left/Right, 0x2D Data, 0x2F Cmt (`#` = none), 0x31 Enable, 0x33 Multi, 0x35 LSize.
- After a header with Bt_No = n, exactly n lines are parsed as controls (the counter `local_e80`).

**Control objects.**
- FUN_004970C0 copies the 0x904-byte parsed struct into the control object.
- It numbers the control **window+0x94 + 1**, the running count. The leading number on the line is not used.
- It appends the control at the **tail** of the list at window+0x8C/+0x90.
- So the N-th control line of a window becomes node N with id N.

**Struct offsets.** These are the fields FUN_00450130 writes:

| Offset | Field |
|---|---|
| +0x1C | Enable |
| +0x4C4 | Color |
| +0x50 | text, 0x81 bytes (FUN_00498120 writes it) |

**Grammar check.** The Python walk in `hui_windows()` checks the stock EN file (1,245 windows), the KR file (1,290 windows) and the patched EN file (1,245 windows). In each, every header and every control matches the grammar, and the window count equals `Number_of_UI`.

---

## 3. cp-1: pet CardNpc in `windslayer.hii`

**Why.** pet.md B1 [V]: every pet-sprite builder reads the NPC template from the pet item's `CardNpc` (def+0x150), and returns silently when it is 0:
- FUN_00447F40;
- FUN_00448350;
- the 0x6F finalize in FUN_0046A2E0.

All 20 pet and pet-gear rows in EN Build 14 have `CardNpc: 0`.

**Values (three independent sources agree):**
1. pet.md 1 B1 / 2.1: 182..185 per species.
2. The **KR 2025** `windslayer.hii` (footer valid). KR ids 4286..4305 (= EN − 4, the +4 shift above 4248) carry CardNpc 182 ×5, 183 ×5, 184 ×5, 185 ×5. Type, Kind, Spr_Num and Sprite are identical to EN row by row.
3. EN `windslayer.hni` rows 182..185 are `./hs/pet/pet001..004.hsi` with AI flag [10] = 1. All 4 pet `.hsi` files and all 18 `pet_helm*` / `pet_wear*` `.hsi` files in the EN install pass their footers.

**Change.** Exactly one token per row, `CardNpc: 0` → `CardNpc: N`, on these rows. Plain offset = the line's offset in the decoded body; the cipher has no header, so plain offset = raw offset.

| EN id | Line | Plain offset | Type | Kind | Spr_Num | CardNpc |
|---|---|---|---|---|---|---|
| 4290 | 4291 | 0x214FC5 | 1 | 16 | 101 | 182 |
| 4291 | 4292 | 0x2151C7 | 1 | 16 | 102 | 182 |
| 4292 | 4293 | 0x2153C9 | 1 | 15 | 101 | 182 |
| 4293 | 4294 | 0x2155CB | 1 | 15 | 102 | 182 |
| **4294 Picky** | 4295 | 0x2157CD | 6 | 14 | 1 | 182 |
| 4295 | 4296 | 0x2159CD | 1 | 16 | 201 | 183 |
| 4296 | 4297 | 0x215BCF | 1 | 16 | 202 | 183 |
| 4297 | 4298 | 0x215DD1 | 1 | 15 | 202 | 183 |
| 4298 | 4299 | 0x215FD3 | 1 | 15 | 201 | 183 |
| **4299 Ulie** | 4300 | 0x2161D5 | 6 | 14 | 2 | 183 |
| 4300 | 4301 | 0x2163D5 | 1 | 16 | 302 | 184 |
| 4301 | 4302 | 0x2165D7 | 1 | 16 | 301 | 184 |
| 4302 | 4303 | 0x2167D9 | 1 | 15 | 301 | 184 |
| 4303 | 4304 | 0x2169DB | 1 | 15 | 302 | 184 |
| **4304 ChikaPuka** | 4305 | 0x216BDD | 6 | 14 | 3 | 184 |
| 4305 | 4306 | 0x216DDD | 1 | 15 | 401 | 185 |
| 4306 | 4307 | 0x216FDF | 1 | 15 | 402 | 185 |
| 4307 | 4308 | 0x2171E1 | 1 | 16 | 402 | 185 |
| 4308 | 4309 | 0x2173E3 | 1 | 16 | 401 | 185 |
| **4309 GuriGuri** | 4310 | 0x2175E5 | 6 | 14 | 4 | 185 |

**What the script asserts before editing each row:**
- the line index equals the item id;
- Type, Kind and Spr_Num equal the table values;
- CardNpc is 0;
- the species matches: a pet's Spr_Num equals the species, and a gear item's Spr_Num // 100 equals the species.

**Size and offsets.**
- Each value grows from 1 to 3 characters, so the plain body grows by 40 bytes and the file goes from 2,199,660 to 2,199,700 bytes.
- The row count (4,322) and the line order are unchanged. The client indexes items by line (FUN_00404750 returns entry id−1), so no id moves.
- In raw form, every byte from 0x214FC5 to the end changes, because the content shifts by 2 bytes per edited row and the mod-3 key phase follows. The footer is new.
- **Side effect:** the count of non-zero CardNpc goes from **900 to 920**. That count is the card-deck total at itemtable+0x24, so its label rises by 20 (pet.md B1 [I]: no other reader of def+0x150 was found).

**Candidate comparison.** `_work/pet/windslayer_2009_petfix.hii` has SHA-1 `e759b301…b7a0`, the same as this build. Re-running its generator `patch_hii_cardnpc.py` on the installer copy also reproduces it byte for byte.
- So there is **no difference to explain**: an independent implementation, with row asserts the PoC did not have, produced identical bytes.
- The only additions over the PoC are the asserts: pinned pristine/patched hashes, the Type/Kind/Spr_Num/species check per row, and the 900 → 920 count.

---

## 4. cp-4: "Apply to Guild" in the player right-click menu

### 4.1 KR 2025 definition [V]

KR `hs\windslayer.hui`, window 80, decoded:
```
80 883 Sprite: 142000000143000000144000000 Pos: 0 0 Size: 137 240 Type: 0 Stretch: 1 Check_UI: 1 Not_Close: 0 First: 1 Bt_No: 12 Data: 0
...
12 Text: 8038 Sprite: 102106059000000000000000000 Pos: 20 210 Size: 90 19 Type: 18 Text_Pos: 10 4 Flag: 0 Color: -1 Type_Color: -1 Stretch: 0 Button: 1 Title: 0 Event: 1 Link: 0 Return: 0 Up: 0 Down: 0 Left: 0 Right: 0 Data: 0 Cmt: # Enable: 1 Multi: 0 LSize: 0 
```
- KR `UILngKo.lng` gives `8038 길드가입신청` ("guild join request").
- 8038 is referenced only by this control. EN references it nowhere, in the hui or in the exe (no 0x1F66 immediate).

### 4.2 EN Build 14 stock [V]

The header is `... Size: 137 220 ... Bt_No: 11 Data: 0`, and controls 1..11 are:

| Control | Text id → label |
|---|---|
| 1 | 491 End (X) |
| 2 | 885 Character name (the target-name label) |
| 3 | 8967 Char. Info |
| 4 | 8048 Trade |
| 5 | 888 Make Party |
| 6 | 889 Whisper |
| 7 | 8968 Add as Friend |
| 8 | 891 Converse |
| 9 | 892 Copy Name |
| 10 | 893 Praise |
| 11 | 894 Report |

- The EN localisers moved three labels to new EN ids (8967, 8048, 8968) and set Text_Pos `5 4` on all nine buttons; KR uses `15 4` / `20 4`.
- The KR Up/Down keyboard-navigation values were zeroed in EN.

### 4.3 The patch

`hs\windslayer.hui`:
- **Line 787** (plain offset 0x02FF34), window 80 header: `Size: 137 220` → `Size: 137 240` and `Bt_No: 11` → `Bt_No: 12`. Every other token is unchanged.
- **New line 799**, after control 11 and before the window-81 header (plain offset 0x030AF0):
  ```
  12 Text: 8038 Sprite: 102106059000000000000000000 Pos: 20 210 Size: 90 19 Type: 18 Text_Pos: 5 4 Flag: 0 Color: -1 Type_Color: -1 Stretch: 0 Button: 1 Title: 0 Event: 1 Link: 0 Return: 0 Up: 0 Down: 0 Left: 0 Right: 0 Data: 0 Cmt: # Enable: 1 Multi: 0 LSize: 0 
  ```
  (It ends with a trailing space and CRLF, like every control line.)
- The **geometry and logic are identical to KR**:
  - the button sprite 102/106/059 is the one controls 3..11 use;
  - Pos 20,210 is the next 20 px step after control 11 at 20,190;
  - Size 90×19, Type 18, Button 1, Event 1, Enable 1;
  - the bottom edge, 229, fits the new height of 240.
- The **only** token that differs from the KR line is `Text_Pos 10 4 → 5 4`, the EN window's own label inset, which all nine EN buttons use. `--verify --kr` checks this automatically.
- The window background is a vertical 3-slice with Stretch 1, as in KR, so 240 px renders the same way KR does.
- FUN_00450130's screen clamp (`if 600 < y + height: y -= height`) reads the height from the window object, so the taller menu is clamped correctly.
- Size: plain body +263 bytes (the new line + CRLF). The file goes from 1,800,149 to 1,800,412 bytes. Raw bytes change from 0x030AF0 to the end, and the footer is resealed.
- The other 1,244 windows are byte-identical, and controls 1..11 of window 80 are byte-identical (checked).

`hs\UILngKo.lng`:
- **New line 2371** (offset 0x0103D3) between `8034 Bank#Extension` and `8039 To#Buddy`: `8038 Apply#to#Guild` → "Apply to Guild".
- Sorted order is kept; the lookup does not need it.
- The other 3,600 records are unchanged. The file is plain text with no footer.

### 4.4 Wording and fit

**"Apply to Guild".**
- KR means "guild join request".
- The retail EN exe already calls this flow *applying*:
  - `"You have applied for this guild."` (sub 2 result 1);
  - `"applied for this\r\nguild."` (the master's application window 0x4BC);
  - `"Guild admission ..."`.
- So the button, the confirmation and the result now use one verb.
- No retail EN label exists for this button, because Build 14 shipped without it.
- The live triage suggested "Join Guild". That is shorter but inconsistent with the follow-up texts. To change the label, edit `LNG_TEXT` and re-pin `PATCHED['UILngKo.lng']`. The hui does not depend on the label.

**Fit on the 90-px button.**
- The game's font face could not be resolved: D3DXCreateFontA is reached indirectly, and `FontData.ini` is only `FontRegistration=No`.
- So the label was measured against existing EN 90-px labels at 12 px:

| Font | "Apply to Guild" | "Add as Friend" (same menu) | "Beginners-Only" (longest existing 90-px label) | "Assuming a GM" |
|---|---|---|---|---|
| Tahoma | 77 | 73 | 80 | 82 |
| Arial | 76 | 76 | 85 | 86 |
| Malgun | 78 | 74 | 82 | 86 |

- With the inset of 5, the label ends by about 83 of 90 px. It is narrower than two retail labels that already fit.
- This still needs a live look ([I]).

### 4.5 How the client uses control 12 (2009 corpus) [V]

**Right-click on another player** (FUN_00450130; entity type 3, messages 0x203/0x205):
1. 0x4503F1..0x450419: popup+0x134 = target uid. Open window 0x50 (`FUN_00448730(0x50,0,3,0,1)`). FUN_00498120(2) writes the target name into control 2.
2. 0x450436 `CMP word [target+0x12],1 / JBE` and 0x45044D `CMP word [local+0x988 → +0x12],0 / JNZ`. The **enabled** branch is taken only if the target is in a guild (+0x12 ≥ 2; 0 = none, 1 = GM tag) **and** the local player's +0x12 is 0.
3. Enabled branch:
   - 0x450458..0x4504B3 finds window 0x50 in the window list (+0x5C0) and loads its control list head (window+0x8C).
   - With `EDX = 0xB` (0x450486) it steps 11 times, so it lands on node 12. It sets +0x1C (Enable) = 1 and +0x4C4 (Color) = 0xFFFFFFFF.
   - 0x4504BD..0x450517: it allocates a u16 = target+0x12 (the guild id) and a 17-byte copy of target+0x16A1 (the guild name). It pushes the name and then the id onto popup+0xCC. The push FUN_0045F360/FUN_0045F600 inserts at the head, so the id is on top.
4. Greyed branch (0x450524..0x450583): the same 11-step walk; Enable = 0 and Color = 0xFF999999. Nothing is pushed.
5. With 11 controls the step loop runs off the end of the list (`TEST EAX,EAX / JNZ` at 0x45049D falls through) and changes nothing. This is the stock EN bug.

**Click on control 12** (FUN_00448730, `case 0x50` → `case 0xC`, asm 0x44CBF5..0x44CC93):
- It requires local +0x12 == 0 and that window 0x50 is open.
- It pops the guild id (u16) and the name from popup+0xCC (FUN_0045F430 pops from the head).
- It opens join dialog 0x4B6 (`FUN_00497C70(0x4B6,3,1)`) and calls FUN_00498120(4).
- `FUN_00497AF0(movzx id)` stores dialog+0x134 = guild id, and `FUN_00497B50(2)` stores dialog+0x40 = **mode 2**.

**OK on 0x4B6** (FUN_00480430 `case 0x4B6`, control 2 = the "OK" button of EN window 1206):
- Gates (guild.md 1.5): M+0x44 ≥ 2 → "Already in the other guild."; local +0x12 == 1 → refusal box 3.
- Mode 2 (0x482D20..0x482D64):
  - `Add(u8 0x89)` through IAT 0x4C7120 `?Add@CSNSocket@@QAE_NE@Z`;
  - `Add(FUN_00497B20())` through IAT **0x4C7114 `?Add@CSNSocket@@QAE_NI@Z` (unsigned int)**;
  - `Send`.
  - The result is **C2S 0x89 {u32 guild_id}**: the target's guild id, zero-extended from entity+0x12. It is not the target's uid.
- Mode 0, the plaza-board click (0x482D86..0x482DCD), uses IAT 0x4C7118 `?Add@CSNSocket@@QAE_NG@Z` (unsigned short), which gives C2S 0x89 {u16 guild_id}.
- The server (`server\guild.py` `Guilds.apply`) already accepts both forms by payload length. **No exe patch is needed for cp-4.**

### 4.6 Side notes

- **Another opener of window 0x50.** The party-HUD slots 0x75..0x78 (FUN_00448730) open window 0x50 for a party member and relabel control 5 "Break Party", but they do not touch control 12. Control 12 then keeps its last enabled or greyed state, and popup+0xCC may still hold a pair pushed by an earlier right-click. KR behaves the same. At worst it sends an application for a stale guild id, which the server validates and the master can deny [I].
- **Popup data is never cleared.** popup+0xCC is not cleared when the menu closes, so a menu opened enabled and then closed leaves the pair on the stack. The next push goes on top, so the next enabled click still takes the newest id.
- **Missing KR ids.** EN also lacks KR 8036/8037 ("input background" / "guild ad title input"). EN's board dialog 0x4B7 uses its own ids (9079/9080/9062/403), so no patch is needed.
- **GM accounts.** A test account that shows the GM tag (entity+0x12 = 1 through sub 37) sees the entry **greyed**, and could not send 0x89 anyway. For the live test, use a non-GM applicant, or a GM hidden so that +0x12 = 0.

---

## 5. Verification run (2026-10-06)

`python patch_data_2009.py --src <installer extraction> --out client_patches\data_2009` passed, then:

`python patch_data_2009.py --verify client_patches\data_2009 --src C:\Users\ohdri\Desktop\WindSlayer2009 --kr C:\Users\ohdri\Desktop\WindSlayerKR` passed. The install is only read.

| Check | hii | hui | lng |
|---|---|---|---|
| Source == pinned pristine SHA-1 | yes | yes | yes |
| Output == pinned patched SHA-1 | yes | yes | yes |
| Footer: SHA-1(ciphered body) == last 20 bytes | OK | OK | n/a (plain text) |
| Footer check by the DLL's own `CheckValid` (unicorn), with the flipped-byte control rejected | 1 / 0 | 1 / 0 | n/a |
| Decode round trip (Python, and DLL `Decode` == Python) | OK | OK | n/a |
| Loader-grammar walk | 4,322 rows, ids in order | 1,245 windows = Number_of_UI, all controls parse | `fscanf "%d %s"` walk 3,600 → 3,601 records, ids unique |
| Structured diff == intended change set | 20 single-token replacements (CardNpc) | 1 header line (2 tokens) + 1 inserted line | 1 inserted line |
| Extra | non-zero CardNpc 900 → 920 | windows other than 80 byte-identical; control 12 vs KR differs only in Text_Pos | window 80 text ids all resolve |

Install logic was tested on a **scratchpad copy** of the game folder, never the real one:
- install cp-1 only, then everything;
- re-install (a no-op);
- a tampered backup refused for both install and uninstall, with no file changed;
- unknown content refused, with no file changed;
- uninstall restores the pristine hashes and removes the backups;
- a patched file with no backup refused;
- a pre-existing identical backup accepted;
- a missing file restored on uninstall;
- the output-path guard refuses a folder (or its `hs\`) that holds `WindSlayer.exe`, and positional arguments are rejected.

### 5.1 Review fixes and re-run (2026-10-06, second pass)

The review found no defect in the patched bytes. It found five weak spots in the script's guards and error handling. All are fixed in `patch_data_2009.py` (and in its identical repo copy):

| Review issue | Fix |
|---|---|
| The same-file guard compared `abspath` strings case-sensitively. `--src srccopy --out SRCCOPY` overwrote the sources in place. | Paths are compared as `normcase(realpath(...))`, plus `os.path.samefile` when the target exists. A build is also refused when `--out\hs` is the folder a source is read from. |
| The game guard checked only `--out` and its parent, and `os.makedirs` ran before any check. `--out <game>\hs\x` created folders in the game tree. | Every ancestor of `--out` is walked: any folder holding `WindSlayer.exe` (a game install or a pristine extraction) refuses the build. All outputs are now built and verified in memory first, and `--out` is created only after every check has passed. |
| An OS error (a read-only or locked file) escaped as a traceback and left a `.gcp-tmp` behind. | `write_atomic` removes its temp file and raises a named error. On install and uninstall, `main` prints the file, the `--status` command and "rerun `--install` (idempotent) or `--uninstall`". `--uninstall` also removes the script's own `*.gcp-tmp` leftovers. |
| `--uninstall` restored from a second read of the backup, not the checked bytes. | The backup is read once in the preflight, checked against the pinned SHA-1, and exactly those bytes are written. |
| `--settle` defaulted to 3 s, while this doc said about 10 s. | The default is now 10 s. The docstring and section 6 tell the lead to pause the sync client. |

Re-run, all from a scratchpad, with nothing written outside it except the rebuilt `client_patches\data_2009\hs\` copies:
- **Rebuild:** `--src <installer extraction> --out client_patches\data_2009` gives `build OK`, with the same three pinned SHA-1s as before (`e759b301…`, `634e97c0…`, `c6cea897…`).
- **Verify:** `--verify client_patches\data_2009 --src WindSlayer2009 --kr WindSlayerKR` gives `verify OK`, with every row of the table above unchanged.
- **DLL emulation:** `vcheck_emu_2009.py` with the game's `ValidationCheck.dll` gives CheckValid 1 / Decode == Python / flipped byte 0 for both patched files and the stock hii. Result: ALL OK.
- **Test harness:** 38 checks on a scratchpad game copy (dummy `WindSlayer.exe`), all OK. Besides the regression set above, it covers:
  - case variants of the source folder (`SRCCOPY`, `SrcCopy`, `--src srccopy\HS`) are refused and leave the sources unchanged;
  - `--out` equal to the game, `game\hs`, `game\hs\x`, `GAME\HS\X\Y` or `game\sub\deep` is refused, and no folder is created;
  - `--out` below the pristine extraction is refused, and a non-pristine source refuses before `--out` exists;
  - with a read-only `windslayer.hui`, install fails with a clean error naming the file, leaves no `.gcp-tmp`, and stops at the state it reports (hii and lng installed, hui stock with its backup). After the flag is cleared, `--install` completes. The same holds for uninstall;
  - `.gcp-tmp` leftovers block `--install`, and `--uninstall` removes them and restores;
  - a backup that changes after the state check is refused before any write, and later re-reads of the backup are never what gets restored.

---

## 6. For the lead: install and live check

Close the game first, and pause the Desktop sync client (or exclude `WindSlayer2009` from it). `--game` must be the folder with `WindSlayer.exe` and `hs\`.
```
cd "C:\Users\ohdri\Desktop\Windslayer 2\client_patches"
python patch_data_2009.py --status  --game C:\Users\ohdri\Desktop\WindSlayer2009
python patch_data_2009.py --install --game C:\Users\ohdri\Desktop\WindSlayer2009        (or --only cp-1 / --only cp-4; re-checks after 10 s)
python patch_data_2009.py --status  --game C:\Users\ohdri\Desktop\WindSlayer2009        (again a minute later: Desktop sync)
python patch_data_2009.py --uninstall --game C:\Users\ohdri\Desktop\WindSlayer2009      (restore)
```
- If a write fails (the game is still running, a file is read-only, or the sync client holds a lock), the script names the file and stops; nothing is half-written. Fix the cause and rerun `--install` (it skips what is already installed), or run `--uninstall`.
- Backups are kept as `hs\<file>.orig-pre-gcp`. Uninstall deletes them only after the restored files have re-verified.
- Both patches work with any exe (`WindSlayer.exe`, `_patched`, `_p2`), because neither touches code.
- Live checks:
  - **cp-1:** pet.md T0 → T1. A GM-granted, equipped Picky now draws (inject 0xAB / 0xAD / 0xAF until pet-s2 exists).
  - **cp-4:** B, not in a guild and not a GM, right-clicks A, who is in a guild.
    - The menu is 240 px tall, with a white "Apply to Guild" button below "Report".
    - Click → dialog 0x4B6 → OK → the sniffer shows C2S 0x89 with a 4-byte body = A's guild id → "You have applied for this guild." on B, and A's HUD notification icon.
    - When B is in a guild, the entry is grey.

---

## 7. Open questions

1. **Label.** Is "Apply to Guild" the wording the user wants, or the triage's "Join Guild"? There is no retail EN source for this button. Check live that the label is not clipped (the font face is unresolved).
2. **Disabled-control clicks [I].** Does the input dispatcher skip controls with +0x1C = 0? If not, a greyed entry could still pop a stale {id, name} pair (4.6). The server-side validation makes this harmless.
3. **Desktop sync.** Is `WindSlayer2009` inside the synced Desktop tree? If yes, the sync client could revert installed files later, as it did here with a script (twice). The `--settle` re-check catches only the first 10 seconds. Consider excluding the game folder from sync.
4. **Card-deck total 900 → 920 (cp-1).** Is it visible anywhere (a collection UI), and does any server data mirror it? pet.md marks it [I].
5. **cp-1 alone and cp-2.** cp-1 alone makes pets visible, but the bell, auto-feed and the rename ticket still need cp-2 (the exe +4 shift, section 8). cp-2 also fixes the guild billboard ids (guild.md 1.6).

---

## 8. cp-2: exe +4 item-id shift (`patch_2009.py`, default ON, `--no-id-shift` to skip)

*This is the cp-2 (exe) part of G-CP. It was first appended at about 14:30. The rewrite of sections 0-7 at 14:42 dropped it, so it was added back here with every fact re-checked (2026-10-06, second pass). The Korean item names in 8.1 are now quoted exactly, from two independent KR sources.*

**Question.** pet.md 1 B2 and guild.md 1.6 / 12 Q1 say that the Build 14 exe hard-codes **KR** item ids for the Pet Bell, the pet foods, the rename ticket and the guild billboards, each one 4 below the EN id. Are the 17 listed sites right, and are they all of them? Can +4 be applied safely as a `patch_2009.py` patch?

**Method.** Static only; the game was not started.
- Capstone 5.0.7 on the pristine `WindSlayer.exe` (sha256 `46bc54042b105d68…17dd5`, byte-identical to `corpus_2009/WindSlayer_2009.exe`).
- The corpus_2009 asm listings (226,199 instructions) and decomp, and `dispatch_2009.json`.
- The installed EN `hs/windslayer.hii` (decoded read-only, footer valid, sha256 `c9f987e6…0e71`) and `hs/ITMLngKo.lng`.
- KR rows from two sources: PySlayer `gamedef.sqlite3` (read-only) and the KR 2025 client's own `hs/windslayer.hii` + `hs/ITMLngKo.lng` (CP949).
- A pattern count in the KR 2025 exe.
- The repeatable verifier `client_patches/verify_cp2.py --work <dir>`. It rebuilds the `--no-id-shift` exes into `<dir>` and writes nothing else.

The only files written in `WindSlayer2009` are `patch_2009.py` and `combo_hud_2009.py`. Their pre-edit copies are `patch_2009.pre-cp2.py` and `combo_hud_2009.pre-cp2.py`.

### 8.0 Bottom line

1. **All 17 sites are confirmed [V]**: 11 pet and 6 guild, exactly the lists in pet.md 1 B2 and guild.md 1.6. Two independent sweeps of `.text` find no 18th id constant in 4249..4322 (8.4).
2. **The patch adds +4 to the id field of each instruction.** Each site changes in exactly one byte (the low byte of the id), so 17 bytes change in total. There is no cave, no hook and no header change.
3. **`--no-id-shift` reproduces the current live exes byte for byte**, including F1. The cp exes differ from the live exes only in those 17 bytes.
4. **Once cp-2 is installed, the server must use the EN ids** (8.5). This matters most for the guild board item field of S2C 0xBA / 0xBB and 0xB3 sub 185, where the exe picks the sprite by comparing with its constant.

### 8.1 Id mapping [V]

**EN data.**
- The EN hii has 4,322 rows, its footer is valid, and the id column equals the line order on every row.
- The client indexes items by line order: FUN_00403950 drops the id token, and FUN_00404750 returns entry `id-1`.
- EN 4249..4252 are four inserted Type-1 rows (sprites 90, 91, 154, 95).

**KR (PySlayer gamedef) against EN, by Type / Sprite / Cash_Cls / Cash_V:**

| KR ids | Compared with | Result |
|---|---|---|
| 1..4248 | EN, same id | 4,246 of 4,248 identical. The 2 others are EN edits of their own: 1542 Type/Sprite, 1543 Sprite. |
| 4249..4318 | EN id + 4 | **70 of 70 identical** |
| 4249..4318 | EN, same id | 62 of 70 differ |

**The constants.** The KR 2025 client's own hii rows and `ITMLngKo.lng` give the same names as gamedef (Title ids in brackets).

| KR id (exe constant) | KR row (gamedef = KR 2025 hii) | EN row at that id | EN id = KR + 4 | EN row (Title → ITMLngKo) |
|---|---|---|---|---|
| 4279 (0x10B7) | `고급길드광고판` "premium guild billboard" [8553]; T5, Sprite 2649, Cash_Cls 14 | Sky Bow (T1) | 4283 (0x10BB) | Premium Guild Billboard; T5, Sprite 2649 |
| 4280 (0x10B8) | `길드광고판` "guild billboard" [8551]; T0, Sprite 2650, Buy 1000 | Rider's Bow (T1) | 4284 (0x10BC) | Guild Billboard; T0, Sprite 2650, Buy 1000 |
| 4281 (0x10B9) | `펫전용 징글벨` "jingle bell for pets" [8619]; T0, Sprite 2652, Buy 500 | Toy Pipe (T1) | 4285 (0x10BD) | Pet Bell; T0, Sprite 2652, Buy 500 |
| 4282..4285 (0x10BA..0x10BD) | `펫먹이 250개` / `100개` / `50개` / `20개` "pet food ×250 / ×100 / ×50 / ×20" [8643 / 8617 / 8616 / 8614]; T5, Sprite 2651, Cash_Cls 19 | Cruiser Sword / Premium Guild Billboard / Guild Billboard / Pet Bell | 4286..4289 (0x10BE..0x10C1) | Pet Food 250 / 100 / 50 / 20 ea.; T5, Sprite 2651 |
| 4318 (0x10DE) | `펫이름 지어주기` "name your pet" [8644]; T5, Sprite 2676 | Rare-DEX-61-99 (T2) | 4322 (0x10E2) | Pet name making; T5, Sprite 2676 |

**These constants are KR-numbered, not an EN choice.** The KR 2025 exe (`WindSlayer.exe` = `WSKRGame.exe`) contains `sub eax,0x10B7`, `push 0x10BA` and `push 0x10BD` once each, the same constants. Its compiler encodes the other sites differently.

### 8.2 Site table (`patch_2009.ID_SHIFT_SITES`)

"Field" is the byte offset and size of the id inside the instruction. pet.md's guild row says "imm @+5"; that applies only to 0x47E846.

| # | VA | Function / role | Pristine instruction (bytes) | Field | Old → new |
|---|---|---|---|---|---|
| 1 | 0x44FD72 | FUN_0044f070, Type-0 item use: Pet Bell gate | `cmp di,0x10B9` (66 81 FF B9 10) | imm16 @+3 | 4281 → 4285 |
| 2 | 0x46D64B | FUN_0046d640 auto-feed (di == 0): bag search through FUN_00464c10 | `push 0x10BA` (68 BA 10 00 00) | imm32 @+1 | 4282 → 4286 |
| 3 | 0x46D659 | same: the id that is fed | `mov edi,0x10BA` (BF BA 10 00 00) | imm32 @+1 | 4282 → 4286 |
| 4 | 0x46D660 | same | `push 0x10BB` | imm32 @+1 | 4283 → 4287 |
| 5 | 0x46D66E | same | `mov edi,0x10BB` | imm32 @+1 | 4283 → 4287 |
| 6 | 0x46D675 | same | `push 0x10BC` | imm32 @+1 | 4284 → 4288 |
| 7 | 0x46D683 | same | `mov edi,0x10BC` | imm32 @+1 | 4284 → 4288 |
| 8 | 0x46D68A | same | `push 0x10BD` | imm32 @+1 | 4285 → 4289 |
| 9 | 0x46D698 | same | `mov edi,0x10BD` | imm32 @+1 | 4285 → 4289 |
| 10 | 0x46D69F | same, manual feed: the `cmp ax,3; ja` range check | `lea eax,[edi-0x10BA]` (8D 87 46 EF FF FF) | disp32 @+2 | −4282 → −4286 (`46 EF FF FF` → `42 EF FF FF`) |
| 11 | 0x46DB2B | FUN_0046d6f0, base of the cash-use switch | `sub eax,0x10B7` (2D B7 10 00 00) | imm32 @+1 | 4279 → 4283 |
| 12 | 0x44FCB8 | FUN_0044f070, Guild Billboard use → dialog 0x4B7 in map 9702 | `cmp di,0x10B8` (66 81 FF B8 10) | imm16 @+3 | 4280 → 4284 |
| 13 | 0x45DBC3 | S2C 0xBA (handler 0x45DABD in dispatch FUN_00451960): board item → sprite 0x145 | `cmp ax,0x10B8` (66 3D B8 10) | imm16 @+2 | 4280 → 4284 |
| 14 | 0x45DBD2 | same → sprite 0x146 | `cmp ax,0x10B7` (66 3D B7 10) | imm16 @+2 | 4279 → 4283 |
| 15 | 0x45DD3B | S2C 0xBB (handler 0x45DBFB), per board → 0x145 | `cmp ax,0x10B8` | imm16 @+2 | 4280 → 4284 |
| 16 | 0x45DD4A | same → 0x146 | `cmp ax,0x10B7` | imm16 @+2 | 4279 → 4283 |
| 17 | 0x47E846 | SubHandler3, S2C 0xB3 sub 185 (case target 0x47E805): flag 1 and item == board → C2S 0x15 | `cmp word [esp+14h],0x10B8` (66 81 7C 24 14 B8 10) | imm16 @+5 | 4280 → 4284 |

### 8.3 The cash-use switch, and why sub 185 has no 18th site [V]

**The switch (site 11).**
- FUN_0046d6f0 reaches 0x46DB2B only for ids above 0xF70 (0x46D7A2 `cmp eax,0F70h`).
- The switch is `sub eax,0x10B7; cmp eax,27h; ja 0x46DC98 (default); movzx eax,[eax+0x46DF40]; jmp [eax*4+0x46DF30]`.
- The 40-entry index table has exactly **six** non-default slots:

| Slot | KR id → EN id | Target | What it does |
|---|---|---|---|
| 0x00 | 4279 → 4283 | 0x46DBF7 | Premium billboard: in map 0x25E6 (9702) only, opens board dialog 0x4B7 (`push 0x4B7; call 0x448730`) |
| 0x03..0x06 | 4282..4285 → 4286..4289 | 0x46DB47 | The four foods: `call 0x46D640` (feed) |
| 0x27 | 4318 → 4322 | 0x46DB67 | Rename ticket: if FUN_00462c80 accepts the worn pet, opens window 0x4CB (`push 0x4CB; call 0x448730`); otherwise shows a message box |

- Moving the base therefore moves all six cases to the EN ids together. The bound `cmp eax,27h` is relative to the base, so it stays as it is.
- After the patch, ids 4279..4282 fall below the base and take the default case. In EN those are bows, the Toy Pipe and the Cruiser Sword, which are not cash items. EN 4319..4321 land on the default slots 0x24..0x26.

**Sub 185 (site 17).** The C2S 0x15 that follows the compare re-reads the packet's own item field. It sends no second constant:
```
0x47E846  cmp word [esp+14h],0x10B8 ; jne 0x47F957
0x47E859  push 15h ; call [0x4C7120]   Add(u8)  opcode 0x15
0x47E861  mov edx,[esp+14h] ; push edx ; call [0x4C7118]   Add(u16)  the same word that was compared
0x47E878  push 1 ; call [0x4C712C]     Send
```
The IAT names come from the pristine exe's import table. With cp-2 the client sends back whatever the server put in the field, which must be 4284 (8.5).

### 8.4 Verification [V, 2026-10-06, re-run in the second pass]

1. **Baseline.** These four builds were run into a scratchpad, with nothing written to the game folder (`PYTHONDONTWRITEBYTECODE=1`, so no `__pycache__`):
   - `patch_2009.pre-cp2.py` and `--p2`;
   - `verify_cp2.py`'s `patch_2009.py --no-id-shift` and `--no-id-shift --p2`.

   All of them reproduce the live exes byte for byte:
   - `WindSlayer_patched.exe`: sha256 `b4cd0638a398d4b476c97adb5ada46eaf3ce73ca1d634ac5e107a2a1325e86b8`;
   - `WindSlayer_p2.exe`: sha256 `a9d7c8be13eeabc0932a5a5635e878c585a98c6a6b03ceb254820821e83621c6`.

   The default and `--p2` builds reproduce the staged cp exes exactly (8.6).
2. **Diff against the live exes.**
   - `WindSlayer_patched_cp.exe` vs `WindSlayer_patched.exe`: 17 differing bytes, all inside the id fields, one per site.
   - `WindSlayer_p2_cp.exe` vs `WindSlayer_p2.exe`: the same.
   - Against the pristine exe, the cp exes carry every live edit (1,425 B, or 1,429 B with `--p2`) plus those 17 bytes.
3. **Capstone, per site, pristine vs cp.**
   - The mnemonic, size, registers and memory operand are the same.
   - Capstone's `imm_offset` / `disp_offset` equals the table's field offset.
   - The old value equals the KR id and the new value equals KR + 4. For the LEA, the displacement goes from −0x10BA to −0x10BE.
   - Every instruction byte outside the field is unchanged, and the table's original bytes equal the pristine instruction bytes.
4. **Instruction boundaries.**
   - 16 sites are instruction starts in the Ghidra corpus listing.
   - 0x47E846 lies in a part of SubHandler3 that Ghidra did not list. It is reached by a linear decode from the sub-185 case target, which the 0xB3 tables give: index table 0x47FBA8[184] → target table 0x47FB14 → 0x47E805.
   - The exe has no relocations (relocs stripped, relocation directory size 0).
5. **Overlap.**
   - The 17 ranges are whole instructions, and all are in `combo_hud_2009.RESERVED`.
   - None overlaps RESERVED, the combo cave 0x4C6220..0x4C689F, its four hooks, or the F1 hook 0x4138C6 and cave 0x4C6F00. The nearest other patched byte is 0x144D bytes from an id field: the `--p2` UDP port at 0x45F19A. Without `--p2` it is 0x289A.
   - `apply_id_shift()` enforces this and refuses to run if a range is missing from RESERVED.
   - Negative tests, none of which wrote anything (review): re-applying to a cp exe is refused at the first original-byte check; an injected overlapping range is refused; a range missing from RESERVED is refused.
   - Re-run in this pass: a pristine copy with site 1 already shifted is refused (`id+4 pet bell gate: unexpected bytes at 0x44FD72`), and no output file is written. The same copy builds with `--no-id-shift`.
   - `combo_hud_2009.py --check` still reports `matches PREBUILT: True`.
6. **Completeness.** A new sweep (`scratchpad/cpfix/sweep_cp2.py`) was written for this pass, independent of the first one:
   - It decodes every listed instruction (226,199), and linearly decodes the 1,481 gaps the listing does not cover (61,886 B, 28,579 instructions). The only instructions with an imm in 4249..4322, or a disp in −4322..−4249, are **the 17 sites and the CRT exponent check 0x4C237B** `cmp ax,0x10C5`.
   - Every raw byte pattern in `.text` is explained: 2,790 in all (2,783 u16 values in 4249..4322 and 7 negative u32 values). 17 are the site id fields, 1 is the CRT compare, and 2,772 are not an id field of the instruction that holds them.
   - **Relative-base range checks** (a `sub` / `add` / `lea` base ≤ 4322 whose following `cmp` reaches ≥ 4249): only 0x46D69F (4282..4285) and 0x46DB2B (4279..4318). Two more matches are unrelated: 0x40CEB1 compares a different register, and 0x4B8834 is the CRT debug-heap `0xDDDD` check.
   - **Data sections.** `.data` holds no aligned u16/u32 in the range. `.rdata` holds only two aligned u16 hits, 0x5302F0 and 0x5310B0, and both are inside CRT double tables (±1.2244, 0.9665, ...). FUN_0044f070's u16 id table at 0x54A398 holds 614, 0, 17, 22, 24, 35, 1305, 104, 3105, 3236, 3950: all below 4249.
   - The independent review sweep (all 226K instructions, 1,481 gaps, raw and scaled forms, the game DLLs) agrees.
   - Pet items (Type 6) and pet gear have no hard-coded ids, as pet.md says.
7. **`verify_cp2.py --work <scratch>`**, re-run in this pass: **47 checks, ALL OK.**
8. **Option matrix (review).** With and without the shift, each pair differs in exactly the 17 bytes, for: the default build, `--no-combo-hud`, `--no-knock-fix`, `--no-smooth`, `--no-aggro-rules`, `--name-rule kr`, `--p2`, and a long host name.

### 8.5 Server consequences (when the cp-2 exe is installed)

| Area | Stock exe (today) | cp-2 exe |
|---|---|---|
| S2C 0xBA / 0xBB board item field (`board+0x42`) | 4280 board (sprite 0x145), 4279 premium (0x146) | **4284** board (0x145), **4283** premium (0x146). Sending 4279/4280 matches neither compare, so no sprite is chosen and `+0x48` keeps its memset 0 [I]. |
| 0xB3 sub 185 `{1, item}` | `{1, 4280}` → C2S 0x15 `{u16 4280}` (`B8 10`), guild.md T-B3-185 | `{1, 4284}` → C2S 0x15 `{u16 4284}` (`BC 10`) |
| EN 4284 Guild Billboard (T0), used from the bag | plain C2S 0x15 {4284} [I, guild.md T-BOARD-ITEM] | dialog 0x4B7 in 9702 (→ C2S 0x88) |
| EN 4283 Premium Guild Billboard (cash) | hits the pet-food case (0x10BB) | hits the premium board case (slot 0). This fixes X8 on the client side; keep Moiba's 0x0B refusal until G-BOARD. |
| EN 4285 Pet Bell | plain C2S 0x15 {4285} | the bell gate in FUN_0044f070 |
| EN 4286..4289 Pet Food | the cash dialog 0x3F4 → C2S 0x48 (pets.py C6) | auto-feed and manual feed → **C2S 0x85** {4286..4289} (pet.md stage 0b test: Pet Food 20 → `0x10C1`) |
| EN 4322 rename ticket | 0x3F4 → C2S 0x48 (refused, kept) | window 0x4CB → C2S 0x4D (pets.py C5: refusal 0x73 until pet-s6) |

**Server state today** (read-only check of `WindSlayer2Game\server`):
- `cash.py` already holds the EN ids (`PET_FOOD` 4286..4289, `PET_BELL` 4285, `PET_NAME_TICKET` 4322).
- `pets.py` C6 owns the stock 0x48 path. On a cp-2 exe, C2S 0x85 reaches `consumed(0x85, 'pet feed', no_pets)`, which is safe: 0x85 needs no reply.
- `guild.py` `BOARD_ITEMS = (0x10B8, 0x10B7)` holds the stock-exe ids. Only the board builders (`board_add`, `board_list`, `sub185`) and their tests use it. No live path sends 0xBA / 0xBB / sub 185 yet: C2S 0x88 is `consumed`, because guild-g6 is not built.

**Still needed: one server setting that follows the installed exe.** It was not added in this pass, because it belongs to the guild-g6 and pet stages, which own those builders. The proposal:
- `config.py` key `CLIENT_ITEM_IDS` (implemented; proposed here as `CLIENT_ID_SHIFT`): `"en"` when the cp-2 exe is installed (the default), `"kr"` for the stock exe. It only matters for `CLIENT_BUILD '2009'`; the 2008 build has no such ids.
- `guild.board_items(shift)` returns `(0x10BC, 0x10BB)` when the shift is on, else `(0x10B8, 0x10B7)`. guild-g6 passes the session's value to `board_add` / `board_list` / `sub185` and accepts C2S 0x15 {4284} or {4280} to match.
- The pet stages need no id switch (they already use EN ids). Only the entry path changes: 0x85 instead of 0x48.
- Tests for both values: the 0xBA field, the 0xBB rows, the sub-185 item, and the C2S 0x15 echo acceptance.

**cp-2 is independent of cp-1.** cp-1 (the hii CardNpc values) makes pets visible; cp-2 makes the pet items and the boards work. The content-loader rule `arch09-id-shift` still applies to server data: the EN hii is authoritative, and KR-derived rows above 4248 are shifted by +4.

### 8.6 Test exes (built, NOT installed)

| File | sha256 | Built with |
|---|---|---|
| `client_patches\WindSlayer_patched_cp.exe` | `f28c92be5180f2776efe232bbac088fc3af6faf3659a37e8aa3162cf6e132813` | `python patch_2009.py --out "<...>\client_patches\WindSlayer_patched_cp.exe"` |
| `client_patches\WindSlayer_p2_cp.exe` | `6823af708fb6e675938af905b381e855d9d0e6ae6fb69adc26c766ef4b34f068` | `python patch_2009.py --p2 --out "<...>\client_patches\WindSlayer_p2_cp.exe"` |

Both were rebuilt in this pass into a scratchpad, and the result is byte-identical to the staged files above. The staged files were not rewritten.

- **Install (the lead).** Back up the live `WindSlayer_patched.exe` / `WindSlayer_p2.exe`, then copy the two cp exes over them. Set the server's board ids to the EN values at the same time (8.5).
- **Rollback.** Restore the backups, or rebuild with `--no-id-shift`.
- **cp-2 is now the default.** A plain `python patch_2009.py` (the command that `wsdev.py` and `play_2009.bat` print) now writes an id-shifted `WindSlayer_patched.exe`. Pass `--no-id-shift` to rebuild the current live exes. The `wsdev.py` hint says so now.
  - `play_2009.bat` is in the game folder, which this work may not write, so it is left to the lead. Its "build it with" line could read `python patch_2009.py  (add --no-id-shift for the stock-id exe)`.
  - Done: the public-repo copies of `patch_2009.py` and `combo_hud_2009.py` carry cp-2, together with the `CLIENT_ITEM_IDS` server setting.
- **Re-verify.** `python client_patches\verify_cp2.py --work <scratch dir>` (47 checks; exit 0 = all OK).

---

## 9. cp-3d: the Pet Bell at the potion grocers (`hs\windslayer.hni`), 2026-10-07

*Sections 9-12 were appended on 2026-10-07. They add two hni patches to G-CP; sections 0-8 are unchanged. cp-1 and cp-4 were installed on the live client on 2026-10-06 (hii, hui and lng patched, each with its `.orig-pre-gcp` backup). The hni is still pristine there.*

**Why.** pet.md B4 [V]: no EN NPC lists the Pet Bell, yet the client help text says "(You can buy it from grocer in the village.)" (exe 0x529328). The server already sells it through config `SHOP_EXTRA_ITEMS` (cp-3, the server half). But the client builds the shop window from its own hni, so the row was never shown. cp-3d is the client half.

### 9.1 The hni and its loader FUN_00409150 [V decomp + asm]

- **Path and checks.** FUN_00409150 opens `./hs/windslayer.hni` (it first loads `NPCLngKo.lng` through FUN_00405020).
  - It copies the last 0x14 bytes and calls `CheckValid(buf, size - 0x14, footer)`, then `Decode`. It aborts (FUN_00408E80) if the check fails.
  - So the cipher and footer are those of hii/hui (2.1). The client's own `ValidationCheck.dll` accepts every patched hni (9.5).
- **Tokens.** The tokenizer is the hui one:
  - `' '` and `'\r'` end a token, and `'\n'` is skipped;
  - line 1 `Number_of_NPC: 204` only arms the parser;
  - every later `'\r'` commits the record.
- **Record buffer.** Each token is copied into one reused 0x400-byte stack buffer, `local_810`, with no bound check. Each line fills a 0xAA4-byte record (`local_12d0`, memset per line). The token index selects the field:

| Token | Key | Template offset | Conversion |
|---|---|---|---|
| 1 | (title) | +0x000 | NPCLngKo lookup (0x11 chars) |
| 2 | (.hsi) | +0x052 | string |
| 5..0x11 | `AI:` | +0x254..+0x284 | 13 × atol |
| **0x13** | **`item:`** | **+0x288** | **`sscanf("%04d")` × 0x78, 4 chars → 4 bytes each (0x1E0 chars)** |
| 0x15 | `Drop:` | +0x468 | `sscanf("%06d")` × 0x78 |
| 0x17 / 0x19 / 0x1D / 0x1F / 0x21 | Quest / UI / Lv / type / HP | +0x648 / +0x64C / +0x650 / +0x654 / +0x658 | atol |
| 0x37..0x39 | `sprite:` ×3 | +0x158/+0x15C/+0x160, stride 12 | `sscanf("%03d")` × 0x15 |
| 0x3F / 0x41 / 0x43 | Infor / Cmt / ExtraName | +0x6AC / list / +0x011 | lng lookups unless `#` |

- **Committing a record.** FUN_00409DD0 refuses a record with an empty name or `.hsi`, and the whole load then aborts. Otherwise it copies 0xAA4 bytes, appends the record at the list tail (FUN_0045F3F0) and stores the list count in record+0x154.
- **The EN file.** All 204 EN records:
  - number 1..204 in file order, so idx = list position = line index;
  - have exactly 68 tokens;
  - have a 480-digit `item:`, a 720-digit `Drop:` and three 63-digit `sprite:` tokens.

  So no sscanf reads leftover bytes of an earlier token past a token's NUL. The emulation in `hni_client_parse()` models those leftovers anyway.

### 9.2 Who reads template+0x288 [V]

A sweep of every `+ 0x288]` operand in the 2009 asm listing finds only three readers that use a template pointer. The other hits are other structures: the record behind +0x4F0 / +0x278 (FUN_00443970, FUN_00498A40, FUN_00451960), stack slots, and a float store (FUN_0040E440).

1. **FUN_0046F6E0**, the 2009 counterpart of the 2008 **FUN_00465A80** (which reads +0x278; the server comments use the 2008 name). It builds window 0xD's rows (callers FUN_0046FCC0 case 0xD and FUN_00441070):
   - It finds the template by list position. 0x46F782 `LEA EBX,[EAX+0x288]`.
   - Per slot: 0x46F790 `CMP dword [EBX],0 / JZ` ends the list at the **first zero**. The item def is `items[id-1]`; a missing def adds no row, and the walk continues.
   - For a Type-3 skill book it shows the next level the player lacks. Each row is a 0x3C-byte node {u16 id, ...} passed to FUN_0049E830(0xD).
   - 0x46F8A8 `CMP EAX,0x78`: at most 120 slots.
   - **So the bell must sit exactly in the first empty slot. A hole before it would hide it.**
2. **FUN_0046FCC0 / FUN_004704B0** read slot 0 only. They use the first item's Type to set up the companion windows: a Type-3 branch, and for Type 0 they enable controls of window 9, the bag [I semantics]. Slot 0 of every grocer is 3 (a potion) and stays 3.
3. **0x482248** (FUN_00480430 case 0x4BB, Moiba): slot 0 only (section 10).

### 9.3 The price is the hii's, on both sides [V]

- The hni has no price column.
- **Client.** A grocer row is bought through the quantity dialog's OK (FUN_00472F40), **merchant branch 0x47438E..0x4745A4** (the 0x474252..0x474327 branch is Moiba's purpose 5, section 10.1). It takes the row from shop window 0xD (FUN_004708F0 at 0x4743B0), looks up the def and calls FUN_00471450 at 0x474560 with:
  - ECX = **def+0x1E0 (hii `Buy`)**, loaded at 0x474557;
  - Type (def+0x154), the id, the quantity, PMoney (def+0x1E8) and the discount flag 1.

  Only if that returns 1 does it send **C2S 0x0B {u16 id, u16 qty, u16 npc}** (0x474570..0x4745A4). The npc word is window 0xD's +0x11C (0x474592), the grocer's hni idx: that window stays open behind the dialog, so it is never 0 (the server registry's `0x4745A4/0x0B`).
  *(Corrected 2026-10-07: this bullet first cited the Moiba branch's VAs, 0x474252..0x474327 / 0x4742E1.)*
- **FUN_00471450.**
  - Cost = qty × Buy × (0.9 if manner > 199) × 0.95 for each of two equipped-item conditions, rounded.
  - If gold < cost, it shows message box (3,3) and returns 0.
  - A **0 price skips the gold check**.
  - Type 0/1/2 also check that the matching bag tab has room.
- **4285 Pet Bell** (EN hii): Type 0, **Buy 500**, Sell 0, PMoney 0, Cash 0, Cash_Cls 17. KR gamedef 4281 has the same values (Type 0, Buy 500).
- **Server.** `_handle_buy_item` prices with the same hii `Buy` (`en_item(4285).buy` = 500) and the same discount (`SH.discount` / `SH.cost`). Rule 2 also refuses a cash item, and 4285 has Cash 0.

### 9.4 Which grocers, which slot

**The server's list.** `config.py` `SHOP_EXTRA_ITEMS = {str(npc): [4285] for npc in (8, 48, 50, 86, 107, 138, 152, 172)}`, the potion grocers of the 2009 towns. `_handle_buy_item` checks a buy against `EC.shop_list(npc) + self._shop_extras(npc)`, so the server puts the bell **after** the hni stock. Since 2026-10-07 `_shop_extras` skips an id the NPC's hni row already lists: the server reads the same hni as the client, so with cp-3d installed the bell comes from the row itself and is listed once (12.2 item 2, done).

**The patch.** It writes `4285` into slot `len(stock)` of each grocer's `item:` token, the first zero. That is the same place, and it becomes the last row of the window. The C2S 0x0B carries no slot, so "slot" here means list position and visibility.

| hni idx | Grocer (title) | Line | Line at (plain) | Build 14 stock (= rows before) | Slot | +0x288 + | Digits at (plain = raw) | Raw bytes old → new |
|---|---|---|---|---|---|---|---|---|
| 8 | Misty (353) | 9 | 0x02ED1 | 3 5 6 7 51 17 614 1270 1340 | 9 | 0x24 | 0x02F34..37 | `47525047` → `4b54584c` |
| 48 | Eve (383) | 49 | 0x139EB | 3 5 6 7 51 141 1065 1270 1271 1340 1341 614 22 | 13 | 0x34 | 0x13A5F..62 | `52504752` → `56524f57` |
| 50 | Hikaru (385) | 51 | 0x1473B | 3 5 141 142 1065 1064 614 35 1269 1271 1341 1342 | 12 | 0x30 | 0x147AB..AE | `47525047` → `4b54584c` |
| 86 | Margaret (407) | 87 | 0x23725 | 3 5 614 1305 141 142 51 1065 1064 1271 1269 1341 1342 | 13 | 0x34 | 0x23799..9C | `47525047` → `4b54584c` |
| 107 | Sophia (423) | 108 | 0x2C371 | 3 5 614 104 141 142 3438 1065 1064 3440 1271 1269 1341 1342 | 14 | 0x38 | 0x2C3EA..ED | `50475250` → `54495a55` |
| 138 | Celine (430) | 139 | 0x392DD | 3 5 614 3105 142 3438 3439 1064 3440 3441 1271 1269 1341 1342 | 14 | 0x38 | 0x39356..59 | `50475250` → `54495a55` |
| 152 | Catherine (436) | 153 | 0x3F046 | 3 5 614 142 3438 3439 1064 3440 3441 3236 1271 1269 1341 1342 | 14 | 0x38 | 0x3F0BF..C2 | `50475250` → `54495a55` |
| 172 | Evan (444) | 173 | 0x476C1 | 3 5 614 142 3438 3439 1064 3440 3441 3950 1271 1269 1341 1342 | 14 | 0x38 | 0x4773A..3D | `52504752` → `56524f57` |

- Each row changes 4 digits, `0000` → `4285`, inside the fixed-width 480-digit token. So the file size (349,083 B), every line length and every other byte stay the same.
- The cipher is positional (key index = offset mod 3), so exactly these 32 raw body bytes change, plus the 20-byte footer.
- Each grocer now shows 10 / 14 / 13 / 14 / 15 / 15 / 15 / 15 rows. Merchants with more rows already exist: EN 19 Shaori lists 26, and the skill masters Grand Master K / M / J (102 / 105 / 106) list 86, all under the walk's 120-slot cap.
- **The script asserts before editing:**
  - the line is template idx with its title id;
  - `UI: 13`;
  - the `item:` token is exactly the Build 14 stock followed by zeros only.
- **KR reference [V].** The KR 2025 hni (footer valid) and KR gamedef both list the bell, KR 4281 = EN 4285 − 4, as the **last row of grocers 19 (Shaori) and 23 (Shaomei)**. EN has no bell in either row, so Outspark dropped those two rows. But only EN 23 is exactly the KR list minus the bell:
  - **23 Shaomei:** KR (hni and gamedef) `39 2 103 210 211 58 2215..2222 4281`, 15 rows; EN the same 14 without 4281.
  - **19 Shaori:** the two KR sources differ. KR gamedef lists 27 rows, `10 154 12 50 54 49 58 126 131 132 139 143 144 149 152 157 150 209 2215..2222 4281`. The KR 2025 hni lists 26: the same without **209** (EN 209 = Arrow). EN 19 lists 26: the gamedef row without the bell, so it **keeps 209**. EN 19 is therefore the KR gamedef row minus the bell, not the KR 2025 hni row minus the bell; it has the same length as the KR 2025 hni row (26) only by coincidence.
  - *(Corrected 2026-10-07: this note first said EN 19 and 23 both carry KR's lists minus the bell.)*
  - cp-3d keeps KR's "append last" convention.
  - It uses the eight potion grocers the server config already names, not KR's pair, so that client and server agree. To also restore 19/23, add them to both `CP3D_GROCERS` and `SHOP_EXTRA_ITEMS`.

---

## 10. cp-5: Moiba sells the Guild Billboard 4284, not the premium board 4283

**Stock data.** In the EN hni, template 181 "Moiba" (title 293, `UI: 1211` = menu 0x4BB, map 9702) has `item: 4283` in slot 0 and zeros in the other slots.

The EN hii rows:

| EN id | Name | Type | Buy (def+0x1E0) | Sell | Cash (def+0x1F0) | Cash_Cls / Cash_T / Cash_P |
|---|---|---|---|---|---|---|
| 4283 | Premium Guild Billboard | 5 | **0** | 0 | **1** | 14 / 1 / 500 |
| 4284 | Guild Billboard | 0 | **1000** | 200 | 0 | 14 / 2 / 10 |

### 10.1 How the client uses Moiba's row [V asm]

1. **Menu 0x4BB control 5**, "Purchase advertisement" (FUN_00480430, case 0x4BB):
   - 0x482243 FUN_00409EF0 finds the template of the clicked NPC (window+0x11C).
   - **0x482248 `MOVZX EAX, word [EAX+0x288]`** reads slot 0, and FUN_00404750 looks up the item def.
   - **0x4822A2 `MOV ECX,[ESI+0x1E0]`** loads the price, and the quantity dialog 0x10 is titled `"How many do you want to buy?(%uGold)"` (0x528B90).
   - The id goes to dialog+0x134 (0x482325), with dialog+0x40 = 5.
2. **OK** (FUN_00472F40): 0x474252 reads dialog+0x134 → FUN_00471450 (Buy at 0x4742E1) → C2S 0x0B {id, qty, **npc 0**} at 0x474327 (the send site the server registry calls `0x474327/0x0B`).
   - **The npc word is always 0**, not Moiba's 181. FUN_00480430 copies the NPC id into dialog+0x11C (0x482331), but the OK control (hui window 16 control 2) has Event 1 (close): FUN_00448730 closes dialog 0x10 before it dispatches the control, and FUN_00497E00 mode 1 zeroes +0x120 / +0x11C (0x497E9F / **0x497EA5**). FUN_00472F40 then reads the zeroed +0x11C at **0x474315** (Add at 0x474320). The item at +0x134 is not cleared, so it survives.
   - Live-verified (P15 G1a): raw `0B BB 10 01 00 00 00`, i.e. {4283, 1, 0}.
   - The server names Moiba itself (`GameServer._board_sale_npc`): npc 0 on map 9702 with a board id (this exe's, the EN 4284 / 4283, or Moiba's hni row) is Moiba's sale; anything else stays "not a merchant". The grocer site 0x4745A4 never sends 0 (9.3).
   - *(Corrected 2026-10-07: this step and 12.3 first said npc 181.)*
3. **With 4283.**
   - The dialog reads **"(0Gold)"**.
   - FUN_00471450 skips the gold check (price 0). Type 5 also skips the bag check, so the client sends 0x0B for any quantity of a "free" item.
   - The server (`_buy_guild_board`) deliberately sells 4284 for 1,000 each instead. The window promises 0 gold, and the server then charges 1,000 or refuses for lack of gold.
4. **With 4284.**
   - The dialog reads **"(1000Gold)"**.
   - The client applies the same gold check (1,000 × qty × discount) and consume-tab check as the server before it sends anything.
   - The server sells exactly the item that was asked for.

### 10.2 What the retail data intended [V data, I intent]

- **KR NPC 181 sells KR 4280.** Both the KR 2025 hni and PySlayer `gamedef.sqlite3` say so. KR 4280 is 길드광고판 "guild billboard": Type 0, Buy 1000, Sell 200, Cash 0, Sprite 2650.
- **By the +4 rule (8.1: 70 of 70 rows above 4248 match at EN = KR + 4), that is EN 4284.**
- The EN value 4283 is KR 4280 + 3, which is the premium board (KR 4279 + 4). It is the only id above 4248 in the whole EN hni [V, pet.md B4].
- No NPC can sell 4283 sensibly:
  - its row is a cash row (Cash 1, Buy 0), and the client prices it at 0;
  - an S2C 0x18 cannot grant a Type-5 cash record (FUN_00441070 has no case, per `_buy_guild_board`).
- So 4283 is an off-by-one in the Outspark data, and **4284 is the intended row** [I]. No retail EN capture of this menu exists to confirm it.

### 10.3 The change

- Line 182, the line at plain 0x4B2DD, item token at +65. Slot 0 (+0x288) changes from `4283` to `4284`.
- One digit changes at plain/raw offset **0x4B321** (`4283` sits at 0x4B31E..0x4B321; raw `4b54584a` → `4b54584b`), plus the footer. The size is unchanged.
- The script asserts before editing: the line is template 181 with title 293, `UI: 1211`, and the column is exactly `[4283]` followed by zeros only.
- **Exe interplay:**
  - On the **cp-2 exe** (installed 2026-10-06, `CLIENT_ITEM_IDS 'en'`), using 4284 in 9702 opens board dialog 0x4B7 (site 12 of 8.2, 0x44FCB8), and its OK sends C2S 0x88.
  - On the stock exe, 4284 is a plain use. The server refuses the sale there ("boards are GM-seeded"), whichever id the client asks for.

---

## 11. Script changes and verification (cp-3d / cp-5), 2026-10-07

### 11.1 `patch_data_2009.py` changes

- **New file and patches.** `windslayer.hni` joins `PRISTINE` (`fd9ea75fd25af0b354961f72544da2f6622f8b9f`, 349,083 B; the installer extraction, the same bytes as the install) and `CIPHERED`. `PATCHES` gains `cp-3d` and `cp-5`, which both edit the hni. `--only` accepts `cp-1 / cp-3d / cp-4 / cp-5` and defaults to all four.
- **One pinned output per patch subset.** `PATCHED` is now `{file: {frozenset(patches): (sha1, size)}}`. The cp-1 and cp-4 pins are unchanged. The hni has one pin per subset:

| hni variant | SHA-1 | Size |
|---|---|---|
| cp-3d | `b2e775ee9fa0ed813696db41533abd4185d658e0` | 349,083 |
| cp-5 | `024ee5e601bb82263008b774d168ecf161219634` | 349,083 |
| cp-3d+cp-5 | `b49219f403ac77ca7ac5679660cfa11b11b2e3f2` | 349,083 |

- **Order.** A file's patches are applied in sorted order. cp-3d and cp-5 commute, which was checked: both orders give identical bytes.
- **Additive install.** `--install` adds the selected patches to what each file already holds:
  - A file that already holds them is left alone ("already installed").
  - Otherwise the target variant is built from the **pristine** bytes: the file itself while it is pristine, else its checked `.orig-pre-gcp` backup, read once.
  - A backup is written only for a pristine file. A patched file without a backup is still refused.
  - On the live folder (hii/hui/lng installed, hni pristine), a plain `--install` prints three "already installed" lines, then backs up and installs the hni.
- **Partial uninstall.** `--uninstall --only cp-5` (or cp-3d) rebuilds the hni from its backup with the remaining patch and keeps the backup. A file left with no patch is restored and its backup deleted after the settle re-check, as before.
- **State and checks.**
  - `--status` names the variant, for example `windslayer.hni  patched cp-3d+cp-5`.
  - The settle re-check compares variants, not just patched/pristine.
  - `--verify --only ...` names the variant the copies were built with.
- **New hni verification.** `verify_hni()` checks:
  - the structured diff: one `item:` token per intended line, and only the 4 digits of the intended slot;
  - the raw diff: only the edited digits plus the footer;
  - `hni_client_parse()`, the FUN_00409150 emulation of both files: 204 records = Number_of_NPC, position = idx, and only the intended `+0x288` slots differ;
  - the FUN_0046F6E0 walk: the eight grocers show their Build 14 stock + 4285 last;
  - Moiba's slot-0 id at 0x482248;
  - the hii rows that set the prices (4285 Type 0/Buy 500/Cash 0, 4284 Type 0/Buy 1000/Cash 0, 4283 Type 5/Buy 0/Cash 1, and every id the grocers show has Cash 0).
- **The hii source for the price checks.** `hii_plain_from()` accepts either the pristine hii or the pinned cp-1 output, so `--verify --src <game> --only cp-3d --only cp-5` works on the installed folder. The game's hii/hui/lng are patched now, so they cannot serve as pristine sources.
- **KR cross-check.** `--kr` reads the KR 2025 hni **by key**: its records have a second `Drop:` column, so the EN token positions do not apply. Moiba must sell KR 4280 (= EN 4284), and KR grocers 19 and 23 must end with KR 4281 (= EN 4285).

### 11.2 Verification run (2026-10-07)

Everything was built and checked in the scratchpad, then the hni was built into `client_patches\data_2009\hs\`. Nothing in `WindSlayer2009` or `WindSlayerKR` was written; the game folder was only read (`--status`, `--verify --src`).

| Check | Result |
|---|---|
| Build, every variant (all; `--only cp-3d`; `--only cp-5`; `--only cp-1 --only cp-4`) from the installer extraction | build OK. cp-1/cp-4 outputs byte-identical to the 2026-10-06 pins and to the installed files |
| `--verify client_patches\data_2009 --src <installer extraction> --kr WindSlayerKR` (all four patches) | **verify OK** |
| `--verify ... --src C:\Users\ohdri\Desktop\WindSlayer2009 --only cp-3d --only cp-5 --kr ...` (the install's own pristine hni + its cp-1 hii) | verify OK |
| `--verify` of the cp-3d-only and cp-5-only copies, each with `--kr` | verify OK |
| Footer by the **client's own `ValidationCheck.dll`** (`vcheck_emu_2009.py`, the install's DLL, SHA-1 `65e7d109…680b` = installer) on the cp-3d+cp-5, cp-3d and cp-5 hni, the stock hni, and the staged hii/hui | CheckValid 1, DLL Decode == Python, flipped byte → 0. **ALL OK** |
| Raw diff (cp-3d+cp-5 vs stock) | 33 body bytes (8 × 4 digits + 1 digit), all inside the edited digits, + the footer |
| FUN_00409150 emulation | 204 records parse in both. The only changed fields are 8[9], 48[13], 50[12], 86[13], 107[14], 138[14], 152[14], 172[14] 0→4285 and 181[0] 4283→4284 |
| Negative tests (`verify_pair` / patch guards) | refused: bell after a hole, footer not resealed, Moiba → 4285, an extra Drop edit, a cp-3d copy checked as cp-5, cp-3d or cp-5 applied twice, a build from a patched source |
| Install harness on a **scratch copy** of the live hs\ state (hii/hui/lng installed + backups, hni pristine; dummy `WindSlayer.exe`) | **37 checks, all OK.** Coverage: the default install touches only the hni; re-install is a no-op; `--uninstall --only cp-5` → cp-3d (backup kept); `--install --only cp-5` over cp-3d; partial and full uninstall; `--uninstall --only cp-1` touches only the hii; a tampered hni backup refuses install and uninstall with no change; unknown hni content and a patched hni without backup are refused; a `.gcp-tmp` leftover blocks install and uninstall removes it; a read-only hni fails cleanly with no tmp, the rerun completes, and a read-only partial uninstall fails cleanly; a missing hni is restored; `--out` inside a game folder is refused |

**The server's own parser** (`en_content.NpcCatalog.load`, imported read-only) on the patched hni:
- each grocer's `shop_list` is its stock + `[4285]`;
- Moiba's `shop_items` is `[4284]` (`shop_list(181)` stays None: UI 1211).

**Server tests against the patched hni.** A scratch runner, `scratchpad\cp35\run_with_hni.py`, redirects only the 2009 `hs/windslayer.hni` lookup (`en_content.hs_path`) to a built copy. Each module was run in the foreground, one at a time. Nothing was bound, and `accounts.json` had the same SHA-1 before and after.

| Module | Stock hni | cp-3d+cp-5 | cp-3d only | cp-5 only |
|---|---|---|---|---|
| `test_shopbank` | (not re-run) | **50 OK** (every 2009 merchant row exists and is non-cash; the 0x474327 tests still pass with 4283) | | |
| `test_pets` | 25 OK | **1 FAIL**: `ShopExtrasEmpty2009.test_no_extras_no_bell` (the hni itself now sells the bell, so the buy succeeds with `SHOP_EXTRA_ITEMS {}`) | | 25 OK |
| `test_guild_boards` | 21 OK | **1 FAIL**: `Boards2009.test_moiba_sells_the_guild_billboard` asserts `EC.npcs().get(MOIBA).shop_items == [4283]` | 24 OK (the module grew during the run; it was being edited) | |

Both failures are assertions about the **stock** hni. Both server tests read the live `CLIENT_DIR_2009`, so they start failing the moment the lead installs cp-3d / cp-5 (12.2).

*Fixed 2026-10-07 (12.2 status; `_shop_extras` and the two tests): both tests now detect the installed row, and both pass with the hni lookup pointed at the stock and at the cp-3d+cp-5 file.*

---

## 12. For the lead: install, server follow-ups, live check (cp-3d / cp-5)

### 12.1 Install

Close the game first, and pause the Desktop sync client.
```
cd "C:\Users\ohdri\Desktop\Windslayer 2\client_patches"
python patch_data_2009.py --status  --game C:\Users\ohdri\Desktop\WindSlayer2009
python patch_data_2009.py --install --game C:\Users\ohdri\Desktop\WindSlayer2009        (all; hii/hui/lng report "already installed")
python patch_data_2009.py --install --game C:\Users\ohdri\Desktop\WindSlayer2009 --only cp-3d   (or only one of them)
python patch_data_2009.py --uninstall --game C:\Users\ohdri\Desktop\WindSlayer2009 --only cp-3d --only cp-5   (restore the hni only)
```
- `--status` today reads: hii `patched cp-1`, lng and hui `patched cp-4` (each with a pristine backup), hni `pristine` with no backup, and no stray files.
- Both patches are data only and work with every exe. cp-5's board dialog needs the cp-2 exe, which is installed.

### 12.2 Server follow-ups (not done here: server code is outside this data-patch work)

1. **Two test assertions follow the installed hni.** They should accept both states, for example by reading the row from `EC.npcs()` and branching on it:
   - `test_guild_boards.Boards2009.test_moiba_sells_the_guild_billboard`: `[4283]` (stock) or `[4284]` (cp-5).
   - `test_pets.ShopExtrasEmpty2009.test_no_extras_no_bell`: with cp-3d installed, Misty's hni stock sells the bell. Use a merchant whose hni row has no 4285, or skip when `4285 in EC.shop_list(MISTY)`.
2. **`_shop_extras` duplicates the bell once cp-3d is installed.** The server reads the same game folder's hni, so its list becomes `[..., 4285, 4285]`. This is harmless: `SH.offered` returns the first match, the price is the same, and the first copy sits in the client's slot. It is untidy in logs, though. The proposed one-line change: skip extras already in the hni stock. `SHOP_EXTRA_ITEMS` then stays useful as the fallback for a client without cp-3d.
3. **`_buy_guild_board`'s docstring** ("the client asks for 4283 ... prices it at 0 gold") describes the stock hni. With cp-5 the client asks for 4284 and shows 1,000. The handler already accepts both ids, so no code change is needed.
4. **Stale VA in the server comments.** `config.py` and `en_content.py` name the shop builder FUN_00465A80 (the 2008 VA). In 2009 it is **FUN_0046F6E0**, reading +0x288.

**Status (2026-10-07).**
- Item 1: done. `test_moiba_sells_the_guild_billboard` reads Moiba's row (must be `[4283]` or `[4284]`), buys the id the client would ask for, and expects the price line only for 4283. `test_no_extras_no_bell` expects the bell sold at Misty only when her hni row lists it, and refused at Murdock (no bell in his row) on either hni. `ShopExtras2009.test_the_pet_bell_sells_at_a_potion_grocer` got the same treatment (it asserted `_shop_extras(MISTY) == [4285]`).
- Item 2: done. `_shop_extras` skips ids the NPC's hni row already lists. A new test forces Misty's row both ways (`test_an_hni_row_that_lists_the_bell_gets_no_extra`).
- Item 3: no change needed; the docstring already describes both rows ("With the cp-5 hni patch ... no line is sent").
- Item 4: `config.py` and the `_shop_extras` docstring now name FUN_0046F6E0. `en_content.py` / `shop.py` keep FUN_00465A80 where they describe the 2008 build (en_content notes the 2009 +0x288).
- Also added (review): `Boards2009.test_npc_id_zero_is_refused_after_leaving_the_plaza`. After a warp 9702 → 801, `current_map` is 801 and npc 0 is refused again.

### 12.3 Live check

- **cp-3d.**
  - Talk to Misty (or any of the eight grocers). The last row is **Pet Bell, 500 Gold** (shown with the discount).
  - Buy 1. The sniffer shows C2S 0x0B `{4285, 1, 8}`, then S2C 0x18, gold −500 (after discount), and the bell in the consume tab.
  - With less than 500 gold the client itself refuses and sends nothing.
  - On a cp-2 exe, using the bell on a sleeping pet takes the bell gate (8.2 site 1).
- **cp-5.**
  - In 9702, click Moiba → "Purchase advertisement". The dialog reads **"How many do you want to buy?(1000Gold)"**.
  - OK with qty 2 sends C2S 0x0B `{4284, 2, 0}` (npc 0, not 181: 10.1 step 2, 0x474315 / 0x497EA5), then 0x18 with 2 Guild Billboards and gold −2,000, and no price line (the dialog named the row sold).
  - With less than 1,000 gold, the client refuses before sending.
  - Using the board in 9702 opens dialog 0x4B7.

### 12.4 Open questions

1. **Grocer choice.** KR sold the bell at grocers 19/23 (Shaori, Shaomei), and EN dropped those rows. cp-3d follows the server's eight potion grocers. Should 19/23 get it too, in both the hni and `SHOP_EXTRA_ITEMS`?
2. **cp-5 intent [I].** 4284 rests on the KR rows and the +4 rule. No retail EN capture of Moiba's dialog exists. If one shows "(0Gold)" with a working premium board, revisit.
3. **Desktop sync** (7, Q3) applies to the hni too. Run `--status` again a minute after installing.

Staged:
- `client_patches\patch_data_2009.py`, SHA-1 `9c396b2d7aab100f4a5630846e2184bbe14fcce2`.
- `client_patches\repo_stage\client_2009\patch_data_2009.py`, an identical copy, not committed.
- `client_patches\data_2009\hs\windslayer.hni`, cp-3d+cp-5, `b49219f4…e3f2`, next to the unchanged hii/hui/lng copies.

Both script copies were written in place (no rename) and re-checked after 12 s, with no name-clash file.
