# Pets: server design (EN 2009 client, Outspark v1.04 Build 14)

Group: `pet`. Written 2026-09-27 for the P8+ roadmap addendum (pets have no roadmap phase yet).

**Sources, most authoritative first**
1. 2009 Ghidra corpus `re_tools/corpus_2009/` (`decomp/*.c`, `asm/*.asm`, `protocol_spec_2009.json`, `dispatch_2009.json`, `client_map_2009.json`). The exe `corpus_2009/WindSlayer_2009.exe` is byte-identical to the installed `WindSlayer2009\WindSlayer.exe` (sha256 `46bc54042b105d68...`, checked in this pass).
2. Client data, decoded as COPIES into `docs/systems_2009/_work/pet/`: `windslayer_2009_hii.txt` (item table, 4322 rows), `windslayer_2009_hni.txt` (NPC/monster templates, 204 rows), plus the plaintext `hs/ITMLngKo.lng`, `hs/UILngKo.lng` and `hs/NPCLngKo.lng`.
3. PySlayer `gamedef.sqlite3` (KR item and NPC rows). PySlayer has **no** pet code: `server_packets/__init__.py` only try-imports `opcode_0xAB..0xB2` and `opcode_0xC0`, and those files do not exist.
4. `RETAIL_VIDEO_SURVEY_2026-09-24.md` and `RETAIL_VIDEO_CATALOG_2026-09-24.json`.
5. The server, read-only: `records.py` (has_pet placeholders, `look_ext`), `inventory.py` (`KIND_TO_CASH_SLOT_2009`), `ground.py` (pet pickup key), `cash.py` (P8 stage 1).

The 2008 client has no pet system at all: no handler for 0xAB..0xB2 and no "pet" strings (per the corpus spec).

**Tags.** Every claim carries one tag:
- **[V]** VERIFIED: read in this pass in the 2009 decomp, asm or exe bytes, or in the decoded client data.
- **[S]** from `protocol_spec_2009.json`, which was verified in the corpus pass. I did not re-read it here.
- **[I]** INFERRED: design reasoning, KR analogy, or the only reading consistent with the code, but not proven.

**Scratch outputs** (all under `_work/pet/`, nothing written to the install): `decode_hs.py`, `dump_pet_items.py`, `pet_items.txt`, `align_kr_en.py` / `align_kr_en.txt` (KR↔EN item-id alignment), `peread.py` / `bytescan.py` (exe byte reads), `patch_hii_cardnpc.py`, and **`windslayer_2009_petfix.hii`**, a candidate patched item table for Stage 0a (not installed).

Address conventions (2009): `gs` = game_state 0x54EBD0. `scene` = [gs+0x4F0]. Local player entity = [scene+0x988]. Local uid = scene+0x224. Cash-shop context `ctx` = gs+0x754 (0x54F324). Its "owned" vector is at ctx+0x04 (begin ptr +0x08) and its "equipped" vector at ctx+0x38 (begin ptr +0x3C). Character info `ci` = scene+0x48, so equipment-bag count = ci+0x373 = scene+0x3BB.

---

## 0. Opcode summary

| Opcode | Dir | Name | Grammar (bytes after the opcode) | Client site | Server today |
|---|---|---|---|---|---|
| 0xAB | S2C | PetEquip | `u32 uid, u16 pet_item_id` [remote only: `bool has_info` [`u8 level, str[13] name`]] | case 0x4576FE | missing |
| 0xAC | S2C | PetUnequip | `u32 uid, u16 pet_item_id` | 0x4579A2 | missing |
| 0xAD | S2C | PetAwakeState | `u32 uid, bool awake` [remote && awake: `u8 level, str[13] name`] | 0x457B52 | missing |
| 0xAE | S2C | PetStatus (local pet) | `u8 gauge, u16 exp` (nothing is read if the local pet_info is NULL) | 0x457D00 | missing |
| 0xAF | S2C | PetLevelUp | `u32 uid` [`u8 level`] | 0x457E71 | missing |
| 0xB0 | S2C | PetRename (broadcast) | `u32 uid` [`str[13] name`] | 0x45D423 | missing |
| 0xB1 | S2C | PetFeedResult (local pet) | `u8 gauge, u32 food_serial` | 0x457DDC | missing |
| 0xB2 | S2C | PetAction | `u32 uid, u8 action` | 0x457FAA | missing |
| 0xC0 | S2C | PetRenameResult (SubHandler4) | `u32 ticket_serial, str[13] name` | 0x46BCC7 | missing |
| 0x82 | C2S | PetEquip | `u16 pet_item_id` | send 0x450019 (FUN_0044fe20) | not dispatched |
| 0x83 | C2S | PetUnequip | `u16 pet_item_id` | send 0x47739D (FUN_00477000) | not dispatched |
| 0x85 | C2S | PetFeed | `u16 food_item_id` | send 0x46D6E1 (FUN_0046d640) | not dispatched |
| 0x86 | C2S | PetEmote | `u8 action` (0x12/0x14/0x15) | send 0x44780D (FUN_004462e0) | not dispatched |
| 0x4D | C2S | PetRename | `u32 pet_serial, str[13] new_name` | send 0x469072 (FUN_00465890) | not dispatched |
| 0x1F | C2S | GroundItemPickup (pet auto-loot) | `u16 ground_id` | send 0x42EA76 (FUN_0042e790) | **handled** (`ground.py`, key `pet_pickup`) |
| 0x15 | C2S | UseItem (Pet Bell, Type 0) | `u16 item_id` | FUN_0044f070 generic path | generic use exists; no bell logic |

Pet data also rides in these packets:
- S2C 0x04 / 0x05 / 0x07 player records: slot-15 id, appearance words 14..16, pet block.
- S2C 0x2B / 0x2C / 0x2E arena rosters (owner: pvp spec).
- S2C 0x6A / 0x6F cash records with limit_type 3 (owner: P8).
- S2C 0x1D / 0x1E pet gear.
- S2C 0x13 auto-loot result.
- S2C 0x25 bell consume.

---

## 1. Read first: blockers found in this pass

**B1. With the installed data, no pet can ever appear.** [V]
- Every builder of the pet sprite takes the NPC template from the pet item's hii **CardNpc** column (def+0x150):
  - FUN_00447f40 (0x447F40, used by 0x04/0x05/0x07/0x2C/0x2E/0xAB/0xAD and the mall preview)
  - FUN_00448350 (arena team changes 0x35/0xC3)
  - the 0x6F finalize in FUN_0046a2e0 (decomp :571)
- Each one returns silently when CardNpc == 0.
- The loader FUN_00403950 maps token 0x44 (CardNpc) to def+0x150, the same field whose non-zero count feeds the card-deck total at itemtable+0x24. That total was live-verified as 900 in 2009 (en_content).
- In the installed `WindSlayer2009\hs\windslayer.hii`, all four pets and all sixteen pet-gear rows have `CardNpc: 0`.
- In KR (`gamedef.sqlite3`) the same rows carry 182..185, which are the pet templates: hni rows 182..185 are `./hs/pet/pet001..004.hsi`, "Picky/Ulie/ChikaPuka/GuriGuri", AI flag [10] = 1, speed 40000, Cmt 300..303.
- **Fix (Stage 0a): a data patch.** Set CardNpc to the template index on the 20 rows, then re-seal the file:
  - cipher: `raw = plain - K[i%3]`, with K = E9 DE E0
  - footer = **SHA-1 of the ciphered body** [V: the footer of the installed file equals sha1(raw body), not sha1(plain)]
- A ready copy is `_work/pet/windslayer_2009_petfix.hii`. The round-trip was verified: 20 rows changed and the footer is valid.
- Side effect: the card-deck total label rises by 20 [I; no other reader of def+0x150 was found by grep].

**B2. The exe's hard-coded pet item ids are KR-numbered and point at the wrong EN rows.** [V]
- The EN 2009 hii inserts four EN event items at ids 4249..4252. Proof: `align_kr_en.txt`, where KR == EN for ids ≤ 4248 and KR id + 4 == EN id for everything after.
- The client indexes items purely by line order. FUN_00403950 discards the id token (case 0) and FUN_00404750 returns entry `id-1` [V]. So every exe constant ≥ 4249 is off by four.

| Meaning | Exe constant (KR id) | Row at that id in the EN hii | Real EN id | Sites |
|---|---|---|---|---|
| Pet Bell (Jingle Bell), Type 0 | 0x10B9 (4281) | Toy Pipe (Type 1) | 0x10BD (4285) | 0x44FD72 `CMP DI,imm16` (imm @+3) |
| Pet food 250/100/50/20 | 0x10BA..0x10BD | Cruiser Sword / Premium Guild Billboard / Guild Billboard / Pet Bell | 0x10BE..0x10C1 | auto-feed search FUN_0046d640: PUSH/MOV at 0x46D64B, 0x46D659, 0x46D660, 0x46D66E, 0x46D675, 0x46D683, 0x46D68A, 0x46D698 (imm @+1); manual range `LEA EAX,[EDI-0x10BA]` 0x46D69F (disp @+2, bytes `46 EF FF FF`) |
| Pet name ticket | 0x10DE (4318) | Rare-DEX-61-99 (Type 2) | 0x10E2 (4322) | cash-use switch FUN_0046d6f0 `SUB EAX,0x10B7` 0x46DB2B (imm @+1; the same switch holds the premium billboard and the foods) |
| Guild billboards (not pets; tell the guild owner) | 0x10B7 / 0x10B8 | bows | 0x10BB / 0x10BC | 0x44FCB8, 0x45DBC3, 0x45DBD2, 0x45DD3B, 0x45DD4A, 0x47E846 (imm @+5) |

- Consequences on the unpatched exe:
  - The bell gate never runs, so the bell is sent as a plain C2S 0x15 of 0x10BD.
  - Using EN food or the rename ticket from the bag falls to the generic cash-use dialog 0x3F4. That dialog sends **C2S 0x48** `{u16 id}` and shows a wait box, so the server must answer 0x72 [V: FUN_00465890 case 0x3f4 sends 'H'].
  - Auto-feed never fires.
  - The rename dialog 0x4CB can never open.
- **Fix (Stage 0b): an exe patch that adds 4 to each immediate above.** The pet part is 11 immediates. Pet items (Type 6) and pet gear have no hard-coded ids [V: byte scan of .text for 0x10B7..0x10E2].
- **After cp-2** (the G-CP exe patch: `patch_2009.py`, default ON, built and not installed yet; CLIENT_PATCH_SET_RE_2026-10-06.md 8):
  - EN 4285 takes the bell gate.
  - Auto-feed and bag feeding send **C2S 0x85** {4286..4289}, not C2S 0x48.
  - EN 4322 opens window 0x4CB, which sends C2S 0x4D.
  - The consequences listed above then apply only to a server talking to the stock exe. The server's pet ids are already the EN ids (`cash.py`). Only the entry path changes (0x85 / 0x4D instead of 0x48).

**B3. Several pet handlers dereference NULL.** [V] Each one is a client crash, so the server must keep a per-viewer model. See §7.

**B4. No EN NPC sells the Pet Bell.** [V]
- KR grocers 19 and 23 list item 4281 (the bell). In the EN hni no NPC lists any id in 4249..4322, except NPC 181 "Moiba", which lists 4283.
- The client help text still says "(You can buy it from grocer in the village.)" (exe 0x529328).
- Shop lists are client data (hni), so the bell must come from loot, GM or quest grants, or from a hni patch [I].

---

## 2. How the client implements pets

### 2.1 Item-table rows (EN 2009 hii ids) [V]

| EN id (hex) | Name (ITMLngKo) | Type | Kind | Spr_Num | Cash / Cash_T / Cash_V / Cash_P | Pet link (needed CardNpc) |
|---|---|---|---|---|---|---|
| 4294 (0x10C6) | Picky, "Cute baby bird Picky... It gathers droped items for you." | 6 | 14 | 1 | 1 / 0 / 0 / 4900 | 182 |
| 4299 (0x10CB) | Ulie (lamb) | 6 | 14 | 2 | 1 / 0 / 0 / 4900 | 183 |
| 4304 (0x10D0) | ChikaPuka (devil) | 6 | 14 | 3 | 1 / 0 / 0 / 4900 | 184 |
| 4309 (0x10D5) | GuriGuri (frog) | 6 | 14 | 4 | 1 / 0 / 0 / 4900 | 185 |
| 4290..4293 | [Picky] Red Hood, Red Glass (Kind 16); Aviation Gear, Police Hat (Kind 15) | 1 | 15/16 | 101/102 | 1 / 0 / 0 / 1700-2300 | 182 |
| 4295..4298 | [Ulie] Spiral Glass, Pacifier (16); Skull Hood, Felt Hat (15) | 1 | 15/16 | 201/202 | cash | 183 |
| 4300..4303 | [ChikaPuka] Water Goggle, Dark Sunglass (16); Punk Headphone, Ribbon Hairband (15) | 1 | 15/16 | 301/302 | cash | 184 |
| 4305..4308 | [GuriGuri] Lotus Hat, Firework Hat (15); Gentle Bow tie, Red Nerd Glass (16) | 1 | 15/16 | 401/402 | cash | 185 |
| 4285 (0x10BD) | Pet Bell, "You can wake your up with this." | 0 | - | - | Cash 0, Buy 500, Cash_Cls 17 | - |
| 4286..4289 (0x10BE..0x10C1) | Pet Food 250/100/50/20 ea., "stamina will be recovered up to 90%" | 5 | 0 | 0 | 1 / 1 / 250,100,50,20 / 4700,2000,1100,500 | - |
| 4322 (0x10E2) | Pet name making | 5 | 0 | 0 | 1 / 1 / 1 / 1100 | - |

- Cash_Cls: 17 = pets (and the bell), 18 = pet gear, 19 = pet consumables. UILngKo 3300 "Pet|" is the mall tab label [V data].
- **P8 note:** hii `Cash_T` is 0 for pets, but on the wire a pet record's limit_type must be **3**. A cash model that sets `kind = Cash_T` gets this wrong.

### 2.2 Client data structures

**Owner entity (0x16B8 bytes)** [V]
- +0x150 u16[25] equip grid:
  - slot 15 (+0x16E) = pet item (Kind 14)
  - slot 23 (+0x17E) = pet headgear (Kind 15)
  - slot 24 (+0x180) = pet apparel (Kind 16)
  - FUN_00427af0 writes these three kinds only when the item is cash, and writes no option block for them.
- +0x12E u16[17] appearance words. [14] pet, [15] pet_helm, [16] pet_wear = the Spr_Num of the item. The compose routine FUN_004282c0 writes `look[kind] = spr` for Kinds 14..16.
- +0x1628 `pet_info` pointer.
- +0x1638 pet sprite entity pointer.
- +0x127C pet NPC template pointer, used by the gear gate.
- +0x1630/+0x1634 pet tick accumulators and +0x162C/+0x162D dirty flags. These are host-only; see 2.7.

**pet_info (0x1C bytes)** [V]
- Local player: it is the cash record itself, bound from the equipped list ctx+0x38. Bound by FUN_00462b70 in 0xAB, by FUN_00462c80 in the own 0x07/0x2B record, and by the 0x6F equipped kind-3 record.
- Remote player: a 0x1C-byte block allocated by the appear handler, 0xAB or 0xAD, and freed on despawn (FUN_004241c0 :103, which never frees the local one).

| Off | Type | Meaning |
|---|---|---|
| +0x00 | u32 | cash serial (local only) |
| +0x04 | u16 | pet item id (local) |
| +0x06 | u8 | limit_type 3 (local) |
| +0x08 | u16 | EXP (cap 0x7788 = 30600) |
| +0x0A | u8 | awake / summoned |
| +0x0B | u8 | level 1..9 (0 = unbound, see 2.3) |
| +0x0C | u8 | gauge % (the client calls it pet "HP"; the food text calls it "stamina") |
| +0x0D | char[13] (15 in the cash record) | name |

**Pet sprite entity** (a normal 0x16B8 entity; FUN_00447f40 / FUN_00448350 / 0x6F finalize) [V]
- name = pet_info+0xD
- position = owner position; +50 px x in scene mode 4
- +0x88 uid = client counter gs+0x3FC, seeded to **0x1F78A40 (33,000,000)** by FUN_00445970 in the S2C 0x03 / 0x08 map loads (calls at 0x45336D / 0x455613). Server uids must stay below this.
- +0x9C state: 4 = shown, 2 = hidden (asleep, or arena spectator)
- +0x9D = level; +0x9E growth phase = 1 (level < 5), 2 (5..8), 3 (≥ 9)
- +0xF00 = template type (0 for pets)
- AI flags copied from template+0x254 (13 dwords)
- +0x163C = owner
- It is registered in the scene list (FUN_00422fc0).
- Hit detection only targets state-4 entities with +0xF00 > 2 (FUN_00416ab0 :103/:266), so pets can never be hit.

**Other data** [V]
- EXP thresholds: u16 table at 0x5259C4 = `0, 120, 360, 840, 1800, 3720, 7560, 15240, 30600` (a second copy is at 0x525494). The pet window EXP gauge is (exp - T[lvl]) / (T[lvl+1] - T[lvl]), and exp 0x7788 shows 100%.
- Speech cooldowns: u32 table at 0x52541C, per emote 0..12 = 8000, 0, 0, 8000, 0, 0, 15000, 15000, 0, 0, 8000, 15000, 8000 ms.

### 2.3 How a pet item becomes a pet [V unless tagged]
1. **Bought.** The pet lands in the account's Spark-Shop box (ctx+0x28; S2C 0x6A) as a limit_type-3 record [S].
2. **Box → character.** This move is client-side inside the mall and is reported by C2S 0x42 on mall close (P8).
   - FUN_00465890 @0x466537 refuses a Type-6 box record whose +0x0B (level) != 0: "You can't move your current pet\r\nto the bag." (exe 0x52B540).
   - Otherwise, for action 0xCA, it asks "When you put it r\nin the bag, it can't be moved\r\nto other characters.\r\nDo you want to proceed?" (0x52B4E0). UI text 9253/9339 repeats the rule.
   - So level 0 in a box record means "not yet bound to a character" [I].
3. **In the bag.** A not-equipped kind-3 record in S2C 0x6F goes into the **equipment tab**, count 1 (FUN_00464e00 case 6 → ci+0x3F2, gate ci+0x373 < 46).
4. **Equip.** Double-click → FUN_00477000 → FUN_0044fe20 → **C2S 0x82**.
   - The server answers **S2C 0xAB**. The client then:
     - moves the item from the bag into grid slot 15
     - sets look[14] = Spr_Num
     - moves the cash record from the owned list to the equipped list and binds +0x1628
     - builds the sprite (FUN_00447f40)
5. **Unequip.** Equipment window, action 7 on the Pet control → **C2S 0x83** → **S2C 0xAC**.
6. **Relog / map load.** The own 0x07 record carries slot 15 and look[14..16]. The following 0x6F carries the record with is_equipped = 1, which re-binds +0x1628 and builds the sprite in its finalize.

### 2.4 Pet equipment slots [V]
- Equipment window 7 iterates grid +0x150..+0x180 (FUN_004423e0).
  - Controls 3..27 map to grid slots 0..24 (FUN_00477000: `param_1 - 3 < 0x19`). So Pet = control 18, Pet's Headgear = control 26, Pet's Apparel = control 27 [I: control = slot + 3].
  - Labels: UILngKo 142 "Pet", 150 "Pet's#Headgear", 151 "Pet's#Apparel".
  - The Pet slot is greyed while the pet sleeps (FUN_004423e0 sets the disabled flag when pet_info+0xA == 0).
- Gear (Type 1, Kind 15/16, cash) is equipped with the normal **C2S 0x0F** and unequipped with **C2S 0x11**. The client gates first (FUN_0044fe20):
  - "Register the pet first." (0x52B5C4) when grid slot 15 == 0 **or** +0x127C (the pet template) == 0. +0x127C is zeroed whenever the sprite is released, so gear cannot be worn onto a sleeping pet.
  - "This is not the equipment of your pet." (0x52B59C) when template+0x154 != gear CardNpc. Template+0x154 is the template's own 1-based index (FUN_00409dd0 `puVar6[0x55] = count`), so gear and pet must share the same template (182..185).
- A pet can be neither equipped over nor unequipped while gear is on: "Take off the equipment of your pet first." (0x52B570, FUN_0044fe20 and FUN_00477000).
- The server replies to gear with **S2C 0x1D / 0x1E** (item_inventory / P8). Compose then sets look[15] / look[16]. The gear is drawn as part of the pet frame, through the owner's draw call (2.6).

### 2.5 Windows [V]
- **Equipment window 7.**
  - Control 0x1C = pet gauge bar; 0x1D = "%u%%" (FUN_00442670, redrawn by 0xAE and 0xB1).
  - UILngKo 9039 "pet_hungry" and 9040 "000%" are the template captions [I].
  - Control **0x1E = "Pet info." button** (FUN_00448730 case 7): it calls FUN_00442710, which returns 0 when there is no pet_info (nothing happens), else opens **window 0x4CE**.
- **Pet info window 0x4CE** (FUN_00442710). UILngKo 9211 "Pet#info", 9212 "Type", 9213 "Exp", 9214 "Detail", 9219 "Lv00", 9216 "00%".
  - ctrl 9 = name
  - ctrl 7 = EXP gauge
  - ctrl 8 = "%u%%" EXP
  - ctrl 0xB = "Level %d"
  - ctrl 0xA = type: "Offense" if template AI[11], "Riding" if AI[12], else "Support". The EN pets are all "Support" [V data].
  - It is closed by 0xAC (FUN_00497c70(0x4CE)).
  - Retail shows "Pet info. (Name / Type / Level Lv00 / Exp 00%)" (5FvE81G417w 2:16, fRrgsphUl8w 0:32).
- **Rename dialogs.**
  - 0x4CB (name entry; UILngKo 9208 "Pop-Up (Change pet name", 9209 "Please enter a Pet name.") → validator FUN_0043e1b0(name, 1), the same character-name rules → 0x4CC (confirm, 9220 "Check pet name") → C2S 0x4D.
  - 0x4CB opens only from bag use of the rename ticket (FUN_0046d6f0 case 0x10DE) and only when FUN_00462c80(+0x16E) finds the equipped pet record. Otherwise: "Register the pet first."

### 2.6 Spawn, despawn and follow: all client-local [V]
- **Spawn** comes only from server packets: 0x04 / 0x05 / 0x07 / 0x2C / 0x2E (remote pet block, or local bind), 0xAB, 0xAD(1) and the 0x6F finalize.
  - Mall and preview avatars (FUN_00461ad0, FUN_00462f20 → FUN_00463440) build local-only preview pets.
  - The sprite is built in state 4, or state 2 when pet_info+0xA == 0. In arena map mode 1 it is also state 2 when the owner's team +0x158B == 0.
- **Draw.** The pet is not drawn in the monster loop, which skips state-4 entities with +0x163C != 0. It is drawn inside the owner's FUN_00401e90 call (FUN_004332f0 :394-427):
  - the frame comes from `template+0x148[(pet action*3 + phase)]`
  - layers 14..16 (pet body / helm / wear) come from the **owner's** look[14..16]
  - so look[14] must be non-zero, or no body layer is drawn [I: the draw loop skips word-0 layers]
- **Follow.** The pet runs the ordinary monster AI in FUN_004185b0. A state-4 entity is driven when +0xF00 > 2 **or** +0x163C != 0. When +0x163C is set, its chase target is the owner instead of +0xE44:
  - face or walk when |dx| > 65
  - jump when the owner is higher
  - drop when the owner is > 140 px lower with |dx| < 200, or > 20 px lower with |dx| < 40 (0x418EA0..0x41919A; constants per MONSTER_AGGRO_RE)
  - The pet templates' AI flags are all 0 except [10], so pets never attack.
- **Warp.** FUN_0042e790 @0x42EC25..0x42EC6A (`CMP EDX,0x258`): when |dx|+|dy| between pet and owner > 600, it plays effect 0x14F at the pet, teleports the pet onto the owner (action 8) and plays 0x14F again.
- **No pet position, movement or C2S report ever goes on the wire.** Every viewer simulates every pet from the owner's replicated position.
- **Despawn.**
  - Owner despawn (0x06 → FUN_00423fe0) frees the owner, frees the remote pet_info and removes the sprite at +0x1638 recursively.
  - 0xAC and 0xAD(0) only *release* the sprite (FUN_00448330: sprite state 2, +0x1638 = 0, +0x127C = 0).
  - No state-2 reaper was found, so each sleep/wake or unequip/equip cycle leaves one orphan sprite until the next map load [I: harmless leak].
  - Map load (0x03 / 0x08) destroys all entities.

### 2.7 Hunger, EXP and level: the client code is host-only (dead in EN), and it is the best design reference
FUN_004185b0 @0x4186D3 → FUN_00425d90 runs only inside `entity state 3 && map_mode == 0 && scene+0xF40 != 0`. scene+0xF40 (the 2008 +0xF24 room-host flag) is written in exactly one place, the constructor at 0x4128B3, and only with 0 [V: exhaustive asm grep]. So in EN the **server** owns the gauge, EXP and level, and pushes them with 0xAE / 0xAF / 0xAD.

The host rules, for an owner in a field map [V code; I = that retail's server matched them]. Every 60 000 ms of accumulated frame time:
- **Awake:**
  - gauge < 2 → gauge = 0, awake = 0 (falls asleep)
  - else gauge -= 1, exp = min(exp+1, 30600)
  - if level_for(exp) > level, level += 1 (+0x162D)
- **Asleep:** a second accumulator gives gauge += 1 every 300 000 ms. The client has no 100 cap; the server should cap at 100.

Level gates in the client [V]:
- /Pet warning needs level ≥ 5; /Pet trick needs level ≥ 9.
- Growth phase changes at 5 and 9.
- Auto-loot of equipment needs phase ≥ 2 (level ≥ 5).
- Speech lines 6..9 need level ≥ 4; lines 10..12 need level ≥ 8 (FUN_00447e10).
- The help text (0x5293F0) says "Level2 /Pet smile possible, Level5 Phase 2 grow up, Level6 /Pet warn possible, Level9 Phase 3 grow up". The code disagrees on smile (any level) and warning (≥ 5).

### 2.8 Auto-loot (FUN_0042e790, send 0x42EA76) [V]
Every logic tick, for each ground item (list scene+0x1C) the client checks:
1. The local player has a pet sprite, and `(pet.x-item.x)^2 + (pet.y-item.y)^2 ≤ 624`. The distance is measured from the **pet**.
2. item owner (+0x1C) == 0, **or** owner == own uid **and** drop_time (+0x24) != 0. So other players' drops are never auto-looted, even after 15 s, and an owned drop needs a non-zero drop_time. `ground.py` already floors drop_time at 1.
3. Item Type (+0x18 = def+0x154):
   - 0 → consume tab count < 46 and it fits
   - 1 → the pet phase must be ≥ 2 (else skipped silently), equipment tab < 46 and it fits
   - 2 → etc tab < 46 and it fits
   - any other Type, or a full bag → pet speech 3 ("My bag is full!"), no send
4. Then it sends **C2S 0x1F u16 ground_id** on every tick until S2C 0x13 arrives. The item is never marked as requested.

### 2.9 Emotes, speech and chat commands [V]
- **Chat commands** (FUN_004462e0):
  - "/Pet smile" → action 0x12 (any level)
  - "/Pet warning" → 0x14 (level ≥ 5, else "Insufficient pet level." 0x528068)
  - "/Pet trick" → 0x15 (level ≥ 9)
  - Only the first 8 characters are compared, case-insensitive. The pet must exist and be awake. The 700 ms anti-spam applies ("Do not Spam.").
  - The client plays nothing locally. It sends **C2S 0x86** and waits for **S2C 0xB2**, which writes the action into sprite +0x994.
  - With no awake pet, the text falls through as normal chat.
- **Speech bubbles** are client-local (FUN_00447e10 → template+0x6A8 index into NPCLngKo Cmt 300..303, drawn for 4 s by FUN_0043bcb0). Index = emote + 13*rand(0/1).

| Emote | Trigger (all client-side) | Picky line / variant |
|---|---|---|
| 0 | "You've received %s. (Count:%u)" (FUN_00441630, any bag gain) | "Yami~" / "I will get it." |
| 1 | S2C 0xAE with awake && gauge ≤ 10 (also triggers auto-feed) | "Feeling sleepy" / "I am so starving." |
| 2 | S2C 0xB1 (fed) | "I feel new power." / "Yami~delicious" |
| 3 | auto-loot blocked | "My bag is full!" / "Inventory is full." |
| 4 / 5 | S2C 0xAF (5 at level 5 or 9) | "Level UP!" / "I grow up, master!" |
| 6 | owner idle (action 8) > 5 s | "I think I can get good item." |
| 7 | owner HP < 20% (FUN_0042c920) | "You need to recover your HP soon." |
| 8 | end of S2C **0x22 SetLevel** | "My master died." |
| 9 | owner enters action 0x16 (FUN_0042d110) | "Congrats!" |
| 10 | owner attack actions (FUN_004511b0) | "Arrrgg!" |
| 11 | owner walking/running > 5 s | "Go with me, master." |
| 12 | owner hit (FUN_0042d110, screen shake) | "Don't hit master!" |

- Lines 8 and 9 look swapped: level-up plays "My master died.", and action 0x16 (dead, per the 2008 value) plays "Congrats!". KR text has the same order, so this is an original bug to reproduce, not fix [V code, I meaning of 0x16].

### 2.10 The retail "BeastMaster hawk" is a class skill, not a pet
- BeastMaster summons are **Type 3 skills** [V hii]. The families are 11 ids each, a Lv0 base plus Lv1..10:
  - Summon Roaring Dog 0x9C3..0x9CC (Lv30+, Con 8010)
  - **Summon Brutal Eagle 0x9CE..0x9D7** (Lv33+, Attr 1)
  - Summon Lazy Sloth 0x9D9..0x9E2
  - Summon Tough Bear 0x9E4..0x9ED
  - Summon Cutephant 0x9EF..0x9F8
  - Beast Attack 0xC2C: "Casting Beast Attack commands the dog to use its skill"
- Summons are **buff slots**: FUN_004262b0 inserts them into entity+0xF24 (21 × 0x18 slots). A self-cast summon's duration is forced to 60 000 ms, and a new summon replaces the old one [V].
- FUN_0044f070 maps the families to scene+0x259 = 7 / +0x25A = 6..10. Beast Attack reads the active family [V].
- None of this touches +0x1628 / +0x1638 or opcodes 0xAB..0xB2 [V: no summon id in the pet code].
- So the "brown hawk pet (BeastMaster companion)" in q2szDnD1o4o 2:00, with "3 beast/pet skill icons" on the quickbar, is **most likely Summon Brutal Eagle** [I: video not viewable here; the other bird candidate is the cash pet Picky, "Cute baby bird"].
- It belongs to combat_skill (their Q9 "summon skills"), not to this spec.
- How the summon creature is drawn is **not traced**.

---

## 3. Opcode reference

Wire notes:
- All S2C are primary-switch cases of OnReceive FUN_00451960, except 0xC0 (SubHandler4 FUN_0046a2e0, gated on ctx+0x14 / +0x18).
- "Local form" means uid == the receiver's scene+0x224.
- `str[13]` must be NUL-terminated inside 13 bytes: names are strcpy-ed into the sprite.

### S2C 0xAB PetEquip [V decomp :~4760-4815, asm 0x4576FE..0x45799D]
```
u32 uid              @0x457715 → entity lookup FUN_00419450
u16 pet_item_id      @0x457730 → def FUN_00404750; gate def+0x1F0 != 0 && def+0x154 == 6, else stop (6 B)
-- entity or def missing: stop.   uid == local: stop (6 B total)
bool has_pet_info    @0x457907 (remote only)
if has_pet_info: u8 level @0x45795E → pet_info+0xB ; str[13] name @0x45797D → pet_info+0xD ; pet_info+0xA = 1
```
- **Both forms:** FUN_00427af0 puts the id in grid slot 15 and returns the previous pet. FUN_004282c0(kind 0xE, cash 1, spr = def+0x1D0) sets look[14] = Spr_Num [V asm 0x4577A1..0x4577F9].
- **Local form**, only if equipment-tab count ≤ 45 **and** FUN_00425420(-1) removes the item from the bag:
  - returns the replaced pet to the bag
  - FUN_00462b70 moves the record owned → equipped and binds +0x1628 (kind 3)
  - FUN_00462c00 moves the replaced pet's record back
  - refreshes the inventory and equipment window
  - FUN_00447f40 builds the sprite
  - If the bag step fails, the grid and look are changed but nothing else happens.
- **Remote form:** has_pet_info = 0 only runs FUN_00447f40, which does nothing without an existing pet_info.
- **Server rules:**
  - The local form must be exactly 6 bytes.
  - The local form needs the kind-3 record in the owned list (last 0x6F) and the item in the equipment tab. Otherwise the sprite does not appear.
  - Remote form: send has_pet_info = 1 with level and name only when the pet is awake, and mark `viewer.pet_info[uid] = True`.
- Example, uid 1 equips Picky: local `AB 01 00 00 00 C6 10`; remote `AB 01 00 00 00 C6 10 01 01 'Picky' 00×8` (21 B).

### S2C 0xAC PetUnequip [V decomp :4819-4849, asm 0x4579A2..0x457B4D]
```
u32 uid, u16 pet_item_id     (same def gates as 0xAB)
```
- Both forms: FUN_00427f00 clears the id from the grid; FUN_004282c0(…, spr 0) sets look[14] = 0 [V asm 0x457A86..0x457AA0].
- **Local:** if the tab count ≤ 45, the item goes back to the bag; FUN_00462c00 moves the record back; +0x1628 = 0; the equipment window refreshes; **window 0x4CE closes**; FUN_00448330 releases the sprite.
- **Remote:** `MOV EAX,[EDI+0x1628]; MOV byte [EAX+0xA],0` with **no NULL check** (0x457B3C), then the sprite is released.
- **CRASH if the viewer never got pet_info for that uid.**

### S2C 0xAD PetAwakeState [V decomp :4850-4905, asm 0x457B52..0x457CFB]
```
u32 uid, bool awake
-- entity missing: stop.   local: no more bytes
remote && awake: u8 level, str[13] name   (pet_info allocated if NULL)
```
- **Local:** needs pet_info (else ignored). Sets +0xA = awake; if asleep, gauge +0xC = 0; refreshes the equipment window.
- **Remote:** `pet_info+0xA = awake` with **no NULL check** (0x457C37). So awake = 0 to a viewer without pet_info **crashes**.
- **Awake:** FUN_00447f40 (re)builds the sprite, then effect 0x14F at the pet. **Asleep:** effect 0x14F, then the sprite is released.
- Client strings for this state: 0x529328 "When pet fells asleep, feeding pet will wake it up. When you don't have food to feed, wait till pet's HP reaches 10%, Use 'Jingle Bell' to wake it up. (You can buy it from grocer in the village.)", and "Pet has to\r\nhave at least over 10% HP to wake up." (0x52B4AC).

### S2C 0xAE PetStatus (local only, no uid) [V decomp :4906-4925]
```
if local entity && local pet_info:  u8 gauge → +0xC ; u16 exp → +0x8
```
- If awake && gauge ≤ 10: FUN_0046d640(0) **auto-feeds**, sending C2S 0x85 for the first owned food, then plays emote 1. It always redraws the window-7 gauge, and redraws window 0x4CE if it is open (state 3).
- Every such packet at ≤ 10% re-sends 0x85 while food is owned. Feed promptly, or send the low value only once per tick.

### S2C 0xAF PetLevelUp [V decomp :4926-4960]
```
u32 uid ; if entity && pet_info: u8 level → +0xB
```
- Local with window 0x4CE open: redraw.
- If a sprite exists: +0x9D = level, +0x9E = phase, effect 0x24 on the pet, emote 5 at level 5 or 9, else emote 4.
- Safe to broadcast: all reads are gated.

### S2C 0xB0 PetRename (broadcast) [V decomp :4961-4975, asm 0x45D423..]
```
u32 uid ; if entity && entity+0x88 == uid && pet_info: str[13] name → +0xD, then strcpy into *(entity+0x1638)
```
- **No NULL check on +0x1638.** It crashes when the pet has pet_info but no sprite: asleep, or after 0xAC on the local player. Send it only while the pet is awake and shown.

### S2C 0xB1 PetFeedResult (local only) [V decomp :4976-4990]
```
if local pet_info: u8 gauge → +0xC ; u32 food_serial → FUN_0046df70(ctx, serial, 1) consumes one unit
```
- Then it redraws the gauge and plays emote 2.
- It does **not** change awake: send 0xAD(1) as well to wake the pet.
- serial 0 or an unknown serial consumes nothing, but the refresh still runs [S].

### S2C 0xB2 PetAction [V decomp :4991-5002]
```
u32 uid, u8 action   (both always read)
```
- If the entity, pet_info, awake and sprite all exist: sprite+0x994 = action, +0xE9C = 0.
- Always safe to send. Only use 0x12 / 0x14 / 0x15.

### S2C 0xC0 PetRenameResult (SubHandler4) [S + V strings]
```
(always) close box 0x16 ; if local pet_info: u32 ticket_serial ; str[13] name → pet_info+0xD, strcpy into *(local+0x1638)
```
- Then FUN_0046df70 consumes one ticket and shows "Pet name has been changed." (0x529480).
- There is **no failure path**.
- Same NULL hazard as 0xB0 (0x46BD38).

### Pet block in player records (S2C 0x04 / 0x07; 0x05) [V asm 0x453D80..0x453E58]
- Inside each record:
  - `repeat(17) appearance_part`: [14..16] = pet / pet_helm / pet_wear Spr_Num
  - `repeat(10) {cash id, attr0}` for slots 15..24: row 0 = pet id (slot 15), row 8 = pet headgear (slot 23), row 9 = pet apparel (slot 24)
- **0x04 / 0x07:** if `uid != receiver's uid`, then `bool has_pet` [if != 0: `u8 level, str[13] name`]; has_pet is also stored as awake. The receiver's **own** record has no byte at all. Instead it binds +0x1628 = FUN_00462c80(slot-15 id) from the equipped list.
- **0x05:** `bool has_pet` [...] is **always** present [S; `records.to_0x05` already does this].
- The record then calls FUN_00447f40 (param_3 = 0), which needs pet_info.
- **Server rule:** has_pet = (a pet is equipped && awake) for remote rows. When 1, record `viewer.pet_info[uid] = True`.
- The arena rosters 0x2B / 0x2E (receiver-gated) and 0x2C (always present) carry the same tail [S]. The pvp spec owns them.

### Pet record in cash lists (S2C 0x6A box, 0x6F owned/equipped) [S]
- A 28-byte record: `u32 serial, u16 item_id, u8 limit_type=3, u8 0, u16 exp, u8 awake, u8 level, u8 gauge, str[15] name`.
- In 0x6F each record is preceded by `bool is_equipped`. Equipped + kind 3 sets local +0x1628 [V decomp :542-546]. The final-page finalize builds the pet sprite from +0x16E's CardNpc [V :556-690].
- 0x6A overwrites level from exp when exp != 0; 0x6F keeps it verbatim [S].
- **Dangling-pointer hazard [V]:** 0x6F mode 0/1 `_free`s every record of both lists (decomp :410-430) but never clears local +0x1628. If the new owned set lacks the equipped pet record, +0x1628 points at freed memory, and later 0xAE / 0xB1 / window-7 refreshes use it [I: crash or garbage].

### C2S 0x82 PetEquip [V FUN_0044fe20, asm 0x44FFD6..0x450019]
- `u16 pet_item_id` (2 B).
- Client gates:
  - gender and level (def+0x1D4 / +0x1D8)
  - not attacking or casting: "You can't equip or unequip while attacking."
  - scene+0x259 != 7 (a summon cast is not pending)
  - ≥ 2400 ms since scene+0x25C
  - class flag
  - Type 6 needs grid slots 23 and 24 empty, else "Take off the equipment of your pet first."
- No local state change and **no lock**. An unanswered request just leaves the pet in the bag.

### C2S 0x83 PetUnequip [V FUN_00477000 :56-110, asm 0x47737F..0x47739D]
- `u16 pet_item_id`.
- Client gates: equipment window action 7 on controls 3..27; tab count < 46 and a free slot (else the bag-full box); not attacking; no pet gear on.
- No lock.

### C2S 0x85 PetFeed [V FUN_0046d640 whole]
- `u16 food_id`. The auto path takes the first owned food via FUN_00464c10, which scans the **owned** list for a record with qty != 0.
- The manual path (bag use) needs pet_info and gauge != 100 (`CMP [EAX+0xC],0x64`).
- It works while asleep: manual feeding is how you wake the pet.
- No lock. On the unpatched exe, see B2: auto-feed searches the wrong ids, and bag use goes to C2S 0x48. After cp-2 both paths send 0x85 with EN 4286..4289.

### C2S 0x86 PetEmote [V asm 0x447798..0x4478D0]
- `u8 action` ∈ {0x12, 0x14, 0x15}. No lock; nothing plays until 0xB2.

### C2S 0x4D PetRename [V asm 0x469139..0x4691CF, 0x469063..0x469089]
- `u32 pet_serial` (**the equipped pet record's serial**: `MOV EDX,[ESI]` with ESI = FUN_00462c80(+0x16E)) + `str[13] new_name` (NUL-padded; FUN_0043e1b0-valid: non-empty, no space, no quote or backslash, no reserved words such as "gamemaster" or "wind", passes FILTER.DLL CheckString).
- Then the client opens the box **"Waiting for the server to respond."** (0x52CF5C) → **MUST REPLY**.

### C2S 0x15 with the Pet Bell [V FUN_0044f070 @0x44FD72]
- Patched exe (cp-2, bell = 0x10BD): if pet_info exists and awake == 0 and gauge ≥ 10 → C2S 0x15 u16 id. If gauge < 10 → "Pet has to have at least over 10% HP to wake up." If awake → nothing.
- Unpatched exe: EN bell 0x10BD is sent with no pet gate.
- No lock. The consume and use effect happen on S2C 0x25.

### C2S 0x1F (pet auto-loot)
- Same wire format as the W key. See 2.8. It repeats every 30 ms tick until S2C 0x13, so it must be idempotent (already true in `ground.py`).

---

## 4. Server flows

State per character (see §6): `pet` = the equipped kind-3 cash record or None; grid[15/23/24]; `look_ext[0..2]`.

State per session (viewer): `pet_info_seen: set(uid)`. It is cleared on the viewer's map load and on the owner's despawn.

**F1 Equip (C2S 0x82 id).**
1. Drop unless:
   - the sender is in world on a field map
   - it owns a not-equipped kind-3 record with item_id == id and def Type 6
   - grid[23] == grid[24] == 0
   - the equipment tab has the item
2. If grid[15] != 0, the old pet record becomes not equipped (the client returns it to the bag itself).
3. Set grid[15] = id, look_ext[0] = def.Spr_Num, record.equipped = True, persist.
4. Send the local 0xAB (6 B) to the owner.
5. For each other session on the map, send the remote 0xAB (has_pet_info = record.awake, level, name) and add the uid to `pet_info_seen` when it is 1.
6. Start the pet tick (F4) if awake.

No reply on refusal: the client is not locked. A refusal may be logged or told with a 0x15 system line [I].

**F2 Unequip (C2S 0x83 id).**
1. Drop unless grid[15] == id, grid[23] == grid[24] == 0, and the equipment tab has room.
2. Set grid[15] = 0, look_ext[0] = 0, record.equipped = False, persist; stop the tick.
3. Send 0xAC to the owner.
4. Send 0xAC to every viewer whose `pet_info_seen` contains the uid, then remove it.
5. Viewers without pet_info get nothing: their stale grid slot is invisible and is rebuilt on the next appear [I].

**F3 Appear / enter world.**
- Remote rows carry grid slots 15/23/24, look[14..16] and `has_pet = equipped && awake` (+ level, name). The own 0x07 row carries grid and look only.
- After the own 0x07, 0x28 and 0x44, send **0x6F with the pet record `is_equipped = 1`** (P8 `_send_owned_cash` order is already correct). That builds the local pet.
- Then send 0xAE {gauge, exp} [I: harmless resync].

**F4 Tick** [I: rules from 2.7]
- One timer per equipped pet while the owner is in world on a field map (map_mode 0). Arena, room and mall-preview time does not count, matching the host gate.
- Every 60 s, **awake:**
  - if gauge < 2: set gauge = 0, awake = 0 → run F6-sleep
  - else gauge -= 1; exp = min(exp+1, 30600)
  - if level_for(exp) > level: level += 1 → send **0xAF {uid, level}** to the owner and all viewers
  - Send **0xAE {gauge, exp}** to the owner.
- Every 300 s, **asleep:** gauge = min(100, gauge+1); send 0xAE.
- Do **not** tick while offline [I].
- level_for(exp) = the first L in 1..8 with exp < T[L] (T = 120, 360, 840, 1800, 3720, 7560, 15240, 30600), else 9.

**F5 Feed (C2S 0x85 food_id).**
1. Find an owned kind-1 record with that item_id (EN 4286..4289 with cp-2; the stock exe's auto-feed asks for KR 4282..4285, which are EN non-food rows) and qty > 0. Otherwise drop. The client will re-ask on the next low 0xAE.
2. gauge = max(gauge, 90) [I: "recovered up to 90%"]. Decrement qty and persist.
3. Send **0xB1 {gauge, serial}**. The client consumes its copy by serial, so do not also send 0x72.
4. If the pet was asleep: awake = 1, send **0xAD {uid, 1}** (local 5 B) to the owner, and the remote 0xAD {uid, 1, level, name} to viewers (add to `pet_info_seen`).
5. Unpatched exe: bag use of EN food arrives as **C2S 0x48 {id}** behind a wait box. Reply 0x72 {uid, id, serial}, which consumes locally and closes the box, then send 0xB1 {gauge, serial 0} (no second consume) and 0xAD if waking [I].

**F6 Sleep / wake.**
- Sleep:
  1. Set awake = 0 and gauge = 0.
  2. Send **0xAD {uid, 0}** to the owner. The client zeroes the gauge, releases the sprite and plays effect 0x14F.
  3. Send the same only to viewers in `pet_info_seen`. Do not remove them: remote pet_info survives with awake = 0.
- Wake: use F5 step 4.

**F7 Pet Bell (C2S 0x15 id == bell).**
- The bell is EN 4285 (0x10BD) in both exe states.
- Refuse unless the pet is equipped, asleep and gauge ≥ 10 (the client only gates this when patched).
- Consume one from the bag, send **0x25 {id}** (plays the use effect), then wake as in F5 step 4.

**F8 Emote (C2S 0x86 a).**
- Refuse unless a ∈ {0x12, 0x14 with level ≥ 5, 0x15 with level ≥ 9} and the pet is awake, with a 700 ms server-side throttle [I].
- Send **0xB2 {uid, a}** to the owner **and** all viewers on the map. The sender plays nothing locally, so echo is required [I].

**F9 Rename (C2S 0x4D serial, name).** A reply is **mandatory**.
- Succeed only if all hold:
  - serial == the equipped pet's serial
  - the character owns a rename ticket (EN 4322) with qty > 0
  - the name is valid (≤ 12 bytes, mirrors FUN_0043e1b0 and the login_character name rules)
  - the pet is **awake** (crash guard for 0xC0)
- On success:
  - Set name, consume the ticket, persist.
  - Send **0xC0 {ticket_serial, name}** to the owner.
  - Send **0xB0 {uid, name}** to viewers in `pet_info_seen` whose pet is awake.
- Refusal: send **S2C 0x73 {result 0}** (1 B). It closes box 0x16 and shows the character-rename failure popup [I: 0x73 is the only known box-0x16 closer with a harmless failure form; its text is about a *name already used*].
- The rename dialog cannot be reached on the unpatched exe (B2). After cp-2 it can: EN 4322 → 0x4CB → C2S 0x4D.

**F10 Pet gear (C2S 0x0F / 0x11 on Kind 15/16 cash items).**
- Handled by the item_inventory / P8 equip path, plus:
  - require grid[15] != 0, the pet awake, and gear.CardNpc (patched data) == the pet template. Without the data patch use the EN table §2.1: Picky 4290-4293, Ulie 4295-4298, ChikaPuka 4300-4303, GuriGuri 4305-4308.
  - slot 23 (Kind 15) / 24 (Kind 16)
  - look_ext[1 or 2] = Spr_Num
  - broadcast **0x1D** / **0x1E**
- Refuse 0x83 while either gear slot is set (F2).

**F11 Map change / relog.**
- Map load: F3, and the owner's `pet_info_seen` for the new map is rebuilt from the new appear rows.
- Keep the 0x6F consistent with grid[15] **every time** (hazard §7-H5).

**F12 Auto-loot.**
- Handled today through 0x1F → 0x13. Keep drop_time ≥ 1 and the bag-room check server-side.

**F13 Box → character (P8 C2S 0x42 list B).**
- Bind the pet to the character: if level == 0, set level = 1, exp = 0, gauge = 100, awake = 1, name = the species name (default [I]).
- Refuse moving a bound pet (level ≥ 1) back to the box (list A): keep it on the character and log it. This mirrors the client rule in 2.3 [I].

---

## 5. Must-reply rules and soft-locks

| C2S | Client state if unanswered | Required reply | Refusal |
|---|---|---|---|
| 0x4D PetRename | "Waiting for the server to respond." box: **soft-lock** [V] | 0xC0 (+0xB0 to viewers) | 0x73 {0} [I] |
| 0x48 (unpatched exe food/ticket via dialog 0x3F4) | wait box [V send path; 2008 flow] | 0x72 | 0x72 {uid, 0, 0} (P8 fallback) |
| 0x82 / 0x83 / 0x85 / 0x86 / 0x15 (bell) / 0x1F | no lock [V] | 0xAB / 0xAC / 0xB1 / 0xB2 / 0x25 / 0x13 | silence is safe; 0x85 and 0x1F will repeat |

---

## 6. Persistence

Per pet (a cash record; lives in the P8 store):
- `serial`: u32, unique
- `item_id`: the species pet item
- `kind = 3`
- `exp`: 0..30600
- `level`: 0 = unbound box pet, 1..9
- `gauge`: 0..100
- `awake`: bool
- `name`: ≤ 12 bytes; the wire has 13 for pet packets and 15 in cash records
- `equipped`: bool
- owner character, once bound
- optionally `tick_ms` carry-over (partial minute) [I]

Per character:
- grid slots 15 / 23 / 24 (already in the 25-slot grid)
- `look_ext` = [pet, pet_helm, pet_wear] Spr_Num (already in the store)

Consumables:
- foods = kind-1 cash records (Cash_V count)
- rename ticket = kind-1 cash record
- Pet Bell = a normal Type-0 bag stack

Runtime only (not persisted): per-session `pet_info_seen`, pet tick timers.

---

## 7. Hazards and invariants checklist

- **H1.** Never send 0xAC, or 0xAD with awake = 0, for owner X to a viewer that has no pet_info for X. The client does a NULL write at 0x457B3C / 0x457C37 [V].
- **H2.** Never send 0xB0 or 0xC0 while the pet has pet_info but no sprite: asleep, or right after 0xAC. It writes through NULL at 0x45D49F / 0x46BD38 [V].
- **H3.** Local forms of 0xAB and 0xAD carry no tail. The own 0x07 / 0x04 record carries no has_pet byte. 0x05 always carries it [V/S].
- **H4.** Every name must be NUL-terminated inside its field (strcpy) [V].
- **H5.** 0x6F frees the record behind local +0x1628. The equipped pet record must be in every 0x6F while grid[15] != 0 [V free, I crash].
- **H6.** Server uids must stay below 33,000,000, the client-local pet uid base [V].
- **H7.** 0xAB local only works when the kind-3 record is in the owned list and the item is in the equipment tab. Send the 0x6F first [V].
- **H8.** 0xAE at gauge ≤ 10 while awake makes the client send 0x85 each time food is owned. Answer it, or you get a loop of requests [V].
- **H9.** An unpatched hii makes pets invisible (B1; fixed by G-CP cp-1). An unpatched exe breaks food, bell and rename (B2; fixed by G-CP cp-2). Each fix is a separate install, so the server must not assume either one.

---

## 8. Live test plan (two 2009 clients)

Client A = owner (`WindSlayer.exe` build), client B = viewer (`WindSlayer_p2.exe`, second UDP port). Both on the same field map.
- Inject with `wsdev sendspec <op> '{json}' --to c:N`.
- Check memory with a ReadProcessMemory probe of [[0x54F0C0]+0x988]+0x1628 / +0x1638 / +0x16E.
- Tests T0..T3 need only Stage 0; the rest need the server stages.

| # | Setup | A should see | B should see |
|---|---|---|---|
| T0 | **Unpatched hii.** Grant Picky (0x6F owned kind 3), double-click it, server 0xAB local | Picky leaves the bag and appears in the Pet slot; "Pet info." opens (name/Support/Level/Exp); **no sprite**, +0x1638 == 0 | nothing |
| T1 | Patched hii copy installed (user action), repeat T0 | A baby bird next to A, name tag "Picky"; it follows walking, jumps up ledges, drops down; warps to A with effect 0x14F when > 600 px away | the same pet following A (B's client runs the AI) |
| T2 | B enters the map after A's pet is out (0x07 / 0x04 with has_pet 1) | - | A plus the pet on arrival |
| T3 | Inject 0xAF {A, 5} | effect 0x24, bubble "I grow up, master!", sprite switches to phase 2; pet window "Level 5" | the same animation |
| T4 | A types "/Pet smile" (Stage 3 server) | A's pet smiles (after 0xB2) | same; the chat line is not shown |
| T5 | A types "/Pet warning" at level 1 | "Insufficient pet level." and no packet | nothing |
| T6 | GM sets gauge 11 + tick; A owns Pet Food 20 (patched exe) | gauge 10% → bubble "Feeling sleepy"/"I am so starving." → auto C2S 0x85 → 0xB1: gauge 90%, food count −1, bubble "I feel new power." | nothing (local-only packets) |
| T7 | No food, gauge reaches 0 | 0xAD 0: pet vanishes with effect 0x14F; Pet slot greyed; "Pet info." still opens | the pet vanishes with the effect |
| T8 | Wait or GM gauge 10, use Pet Bell | bell consumed (0x25), pet reappears (0xAD 1) | the pet reappears |
| T9 | Pet Bell at gauge 5 (patched exe) | "Pet has to have at least over 10% HP to wake up." and no packet | - |
| T10 | Equip Red Hood on Picky | hood drawn on the pet; Pet's Headgear slot filled | the hooded pet |
| T11 | Equip Skull Hood (Ulie gear) on Picky | "This is not the equipment of your pet." and no packet | - |
| T12 | Unequip Picky while it wears the hood | "Take off the equipment of your pet first." | - |
| T13 | Remove the hood, then unequip Picky (0x83 → 0xAC) | the pet vanishes and returns to the bag; window 0x4CE closes | the pet vanishes (sent only because B has pet_info) |
| T14 | Kill a mob; a consumable drop lands near the pet | auto-pickup within ~25 px of the **pet**, "You've received ...", bubble "Yami~"; equipment drops only once the pet is level ≥ 5 | 0x13 removes the item |
| T15 | Rename ticket (patched exe), name "Tweety" | wait box → "Pet name has been changed.", name tag updates | name tag updates (0xB0) |
| T16 | Rename while asleep (server refuses with 0x73) | wait box closes with the failure popup; **no crash** | - |
| T17 | A changes map / relogs | pet reappears after load (0x07 + 0x6F) | B, on the new map, sees it on arrival |
| T18 | A logs out | - | A and the pet vanish (0x06) |
| T19 | Retail check: BeastMaster casts Summon Brutal Eagle (combat_skill) | a 60 s summon in a buff slot, unrelated to the Pet slot | - |

---

## 9. Implementation plan (each stage testable)

**Stage 0: client prerequisites (no server code).**
- 0a: install the CardNpc-patched hii. The candidate is in `_work/pet/`; the user decides. Test T0 → T1 with injected 0xAB, 0xAD and 0xAF.
- 0b: exe patch for the id shift (11 pet immediates, plus 6 guild ones). Add it to `patch_2009.py` [I]. Test: using Pet Food 20 sends C2S 0x85 0x10C1 (sniffer); the rename ticket opens 0x4CB. **Done as G-CP cp-2** (CLIENT_PATCH_SET_RE_2026-10-06.md 8): default ON, `--no-id-shift` skips it, test exes in `client_patches\`, not installed yet.
- 0a is also built: G-CP cp-1, `client_patches\patch_data_2009.py`, byte-identical to the `_work/pet` candidate.
- 0c (optional): hni patch so a grocer sells the Pet Bell, or a GM/loot source.

**Stage 1: model and visibility.**
- Pet fields on kind-3 cash records (P8 hook: limit_type 3 encoding in 0x6A / 0x6F, species default name), and grid 15/23/24 + look_ext in records.
- The has_pet block in 0x04 / 0x05 / 0x07 per receiver, the `pet_info_seen` tracker, and GM `!pet give|set` for testing.
- Test T2, T17, T18 with a GM-equipped pet.

**Stage 2: equip / unequip.** 0x82 → 0xAB, 0x83 → 0xAC, broadcast, gear-slot gate. Test T0/T1 via the UI, T12, T13.

**Stage 3: status, level, emotes, sleep.** The F4 tick (0xAE / 0xAF / 0xAD), 0x86 → 0xB2. Test T3..T5, T7.

**Stage 4: feeding and waking.** 0x85 → 0xB1 (+0xAD), bell through 0x15 → 0x25 + 0xAD, and the unpatched 0x48 fallback. Test T6, T8, T9.

**Stage 5: pet gear.** Kind 15/16 through 0x0F / 0x11 with the species check. Test T10, T11.

**Stage 6: rename.** 0x4D → 0xC0 + 0xB0, 0x73 refusal. Test T15, T16.

**Stage 7: mall integration (with P8).** Sell pets, gear, food and tickets; 0x6A box records with level 0; 0x42 binding rules (F13). Test: buy Picky, move it to the bag (confirm text), equip.

**Stage 8: arena and rooms (with P9).** Pet tails in 0x2B / 0x2C / 0x2E; no pet tick in rooms. Test: the pet is hidden for spectators and shown for team members.

---

## 10. Open questions

1. Did retail EN ship a later data patch? That would explain the Build-14 data with CardNpc 0 and the +4 id shift. Or does our install mix an exe and hs from different builds? Check the installer (`installers_2009/WindSlayer-01_04_0000.exe`, extract with `iscab.py`) against the installed hii.
2. Were retail server tick rates the same as the host code (1%/min, +1 exp/min, +1%/5 min asleep)? Were any other EXP sources used (kills)? None are visible client-side.
3. Food effect: set to 90%, or +N capped at 90? And the per-food amounts (250/100/50/20 are pack counts, not strength).
4. Did retail echo 0xB2 to the sender? The sender plays nothing locally, so probably yes.
5. The best refusal for C2S 0x4D. 0x73 {0} shows a character-name message. Is a better box-0x16 closer free of side effects?
6. Is box level 0 really the "unbound" marker? And do bound pets ever go back to the box?
7. Emote lines 8 and 9 appear swapped (level-up says "My master died."). Confirm what action 0x16 is in 2009.
8. Is the retail "hawk" Summon Brutal Eagle or the Picky pet? Look at q2szDnD1o4o 2:00-2:24 for a Pet-slot icon or a pet name tag. Summon rendering is untraced (combat_skill Q9).
9. The FUN_0043e1b0 failure box text (not recovered), and the exact 0x4CB edit-length limit.
10. The orphaned state-2 sprites after sleep/unequip cycles: any visible side effect in long sessions?
11. Should pets tick while the owner sits in a stall or in the mall preview map? The host gate says field map only.
