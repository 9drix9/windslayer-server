# instance_dungeon: party instance dungeons (indun01-03), dungeon room list / create / party window, stage loads (S2C 0xA5), map-object triggers (S2C 0xA7), dungeon result (S2C 0x36 kind 5)

Client: EN Outspark v1.04 Build 14 (2009-01-23), `WindSlayer.exe` in `Desktop\WindSlayer2009`.

Sources, in priority order:

1. The 2009 Ghidra corpus `re_tools/corpus_2009`: `decomp/`, `asm/`, `protocol_spec_2009.json` (entries 0x2C, 0x18, 0x1A, 0x1B, 0x1C, 0x1D, 0x2D, 0x95, 0x0A (dead) C2S; 0x08, 0x2B, 0x2C, 0x2D, 0x2E, 0x2F, 0x31, 0x34, 0x35, 0x36, 0x37, 0xA5, 0xA7, 0xC1, 0xC2, 0xC3 S2C), `strings.tsv`, `client_map_2009.json`, and the exe copy `WindSlayer_2009.exe` for jump tables.
2. 2009 client data, decoded to **copies** under `_work/instance_dungeon/` with `_work/instance_dungeon/dec_hmi.py --out <dir> <inputs>`:
   - `maps/indun*.hmi.xml` (all 18 stage files), `maps/stage98_01`, `stage99_02`, `stage01_01` for comparison;
   - `data/windslayer.hni.txt` (NPC/monster templates), `data/windslayer.hii.txt` (items), `data/windslayer.hui.txt` (UI windows), `data/windslayer.hqi.txt` (quests);
   - `scripts_summary.json`, produced by `summarize_scripts.py --maps maps --out scripts_summary.json` (every SCRIPT_INFOR row, with its tile, position and the client-side monster uid).
   - `hs/NPCLngKo.lng`, `UILngKo.lng`, `MapLngKo.lng`, `ITMLngKo.lng` are plain text and were read in place.
3. The 2008 PvP spec `docs/systems/pvp_arena.md` (room machinery; the 2009 client keeps its shape).
4. PySlayer. It has **no** dungeon implementation: only a stub `opcode_0xA5.py` = `u16 201 + u32 1212` (a KR layout, 6 bytes; the EN 2009 handler reads 2) and a `0x2C` list handler for types 1 and 4. It is not ground truth for anything in this document.
5. The retail video survey: KR footage `8Ol-DThGCZ8` 1:07 shows the bubble "All party members must approach the portal and press the [Down] button to move". That is a later KR build.
6. The live server `WindSlayer2Game/server`, read only.

**Tags.** Every claim carries one of these tags:

- **[V]**: VERIFIED. I read it in the 2009 decomp or asm at the VA given, in a decoded data file, or in a `protocol_spec_2009.json` entry whose `verification` says it was read in asm (VA cited).
- **[V-2008]**: the 2009 handler has the same fingerprint as the 2008 one, and the meaning comes from the 2008 analysis (`pvp_arena.md`).
- **[I]**: INFERRED. A design proposal or a reasoned guess. It was not read in code.

**Notation:** `gs` = game_state 0x54EBD0. `scene` = `[gs+0x4F0]`. `map` = `[gs+0x4FC]` (= `[scene+0xFB4]`). `tpl(n)` = the n-th record (1-based) of the NPC template list `[gs+0x504]` (FUN_00409ef0). Strings are quoted with their exe VA and, where the exe has one, the string-pointer slot (`slot 0x54Axxx`). UI captions are `UILngKo #id`.

---

## 0. Key findings (read first)

1. **A dungeon is a room game with `room_mode 5`, built on the PvP room machinery.** [V] The client uses the same opcodes as arena and play rooms: list open C2S 0x2C, create C2S 0x18, join C2S 0x1A/0x1B/0x1C, join result S2C 0x34, enter S2C 0x2F, roster S2C 0x2E/0x2C/0x2D, team/ready S2C 0x35, exit C2S 0x1D. Dungeon-only additions: the list rows S2C 0xC2/0xC1, kick C2S 0x2D, start C2S 0x95, stage load S2C 0xA5, map-object trigger S2C 0xA7 and result S2C 0x36 with `result_kind 5`. §5 lists what is shared with P9.
2. **Every dungeon stage is a mode-1 ("room") map, so everything that moves is driven by the UDP room host.** [V] Every `indunAA_BB.hmi` with `InDun="1"` also has `mode="1"` (map+0x7C). On such maps the 30 ms tick FUN_0042c920 runs **no** local monster/player simulation (mode compare at 0x42CAF0). Only pets (`entity+0x163C != 0`) and the dungeon-script checker FUN_0042a300 run (InDun test 0x42CBE0, call 0x42CBF6). The client sends **no** C2S 0x0D (movement/hit report; FUN_0042e1b0 is called only in the mode-0 block, 0x42CB2A) and **no** C2S 0x7E (portal; gate `CMP [map+0x7C],0` at 0x4311C6). Its only per-tick output is the UDP opcode 3 input datagram `{u32 uid, u8 keyA, u8 keyB}` to `host_ip:room_no+10000` (FUN_00424500, SendTo at 0x4249A2). Positions, monster AI, hits, HP and deaths arrive as UDP host snapshots and events (FUN_0045e230). **The server must be that host.** This is the same blocker as P10 (decision gate G2). The 2009 patched exe does **not** NOP the room SendTo (`patch_2009.py` has no patch at FUN_00424500) [V], so no special exe is needed, unlike 2008 S1-17.
3. **The client creates every stage monster itself, hidden, with deterministic uids.** [V] On an InDun map load (the S2C 0x2F handler at 0x455E05, and S2C 0xA5 at 0x45D7EE), FUN_00423970 walks the map's `SCRIPT_LISTS`. For every `Action="13"` (monster appear) row whose tile resolves, in file order, it creates a type-4 entity with template `ActMonsAprMonsIndx`, the position of the first floor line of tile `ActMonsAprTileIndx` in layer `ActMonsAprLayerIndx` (FUN_00407530), uid **33,000,000 + n** (n counts successful creations and restarts at 0 on every load), and action/render state **0x16 = hidden**. The host makes a monster "appear" by putting it into its snapshots with another state. The server's monster table per room and stage must reproduce this numbering exactly (§1.5). The uids fit the 25-bit uid field of the UDP snapshot (`& 0x1FFFFFF`, 0x45E95E), because 33,000,000 < 2^25.
4. **Stages are advanced by the server, not the client.** [V] The client contains no stage-transition logic. S2C 0xA5 `{u16 stage_code}` fades, loads `./hs/indun%02u_%02u.hmi` in place, deletes the old stage's monsters (type 4 without pet owner, FUN_00424420), creates the new stage's hidden monsters and clears the wait flag `gs+0x16F8` (0x45D72F..0x45D7FD). Portals inside a stage are `event="1"` floor tiles whose `value_num` is the next stage code. The client's own portal code does nothing on mode-1 maps: C2S 0x7E is gated off, and the InDun branch of FUN_00406840 needs a line flag `[9]` that nothing ever writes. So the "all members press Down at the portal" rule has to be evaluated by the server from UDP input (`keyA/10 == 4` is the same input the field portal sender tests at 0x4311DE) [I for the rule, V for the gates].
5. **Waiting room = the first stage map itself.** [V] The Dungeon Party window 0x4C6 (roster, Ready, Kick, Start) is filled by FUN_004446c0, and only when `map+0x2E0` (the `InDun` attribute) is non-zero. The lobby map `indun01_00.hmi` has `InDunWaitingMap="1"` but no `InDun`, so the party window would stay empty there. Its `InDunWaitingMap` / `ConnInDunMapName` fields are parsed into map+0x2DC/+0x2E4/+0x3E8 (0x407E02..0x407E82) and **never read** by any other code (asm scan). So S2C 0x2F must name the first stage (101 / 201 / 301) [I, forced by the V gate]. The run starts on the UDP phase change: UDP opcode 8 (round running) closes 0x4C6 and opens HUD 0x47A for mode 5 (0x45E525..0x45E54B). **No TCP packet can start a run.**
6. **Dungeons are NPC templates.** [V] The create dialog's dungeon list `[gs+0x504]+0x128` holds the `windslayer.hni` records with `Quest: 2` (FUN_00409150 @0x409B.. / FUN_00409dd0: `+0x648 == 2`). The value that C2S 0x18 carries in its `map_id` field, and that S2C 0x2F carries as `dungeon_ref` and 0xC1/0xC2 as `npc_index`, is the record's 1-based position, **not a map code**. In EN 2009 these are 187 "Gang of forest" (Lv 8), 197 "Monkey Ghost King" (Lv 15) and 204 "Weapon Destroy" (Lv 22). Their entrance NPCs (hni `UI: 1220` = window 0x4C4) are 186 "Hidden forest", 196 "Hades Range Door" and 203 "Crater machine". **None of the three entrance NPCs is placed in any EN 2009 map file** (all 288 `.hmi` scanned for `NpcId`) [V]. The server has to spawn them (S2C 0x1A, gate G-ID1) or the dungeon list is unreachable.
7. **Party size is at most 5.** [V] FUN_004446c0 has 5 roster slots. The create dialog's capacity buttons are captioned "1".."5". **Master = the first type-3 entity in the scene list** = the first entry of the S2C 0x2E roster. On mode-1 maps FUN_00422fc0 always appends (bVar5 stays true), so later joiners (S2C 0x2C) queue behind. Only the master gets Kick and Start, and Start needs every other member's `entity+0x158C` (= `next_team` from S2C 0x35) to be non-zero ("All have to be ready.").
8. **In the dungeon, the only exit is "Leave Now" (C2S 0x1D u8 4).** [V] For mode 5, the 0x2F handler disables "Reserve Quit" (0x4B ctrl 2) and enables "Leave Now" (ctrl 3), with the text "\r\nWould you exit from the dungeon?" (0x527FFC, slot 0x54A7D8) (0x455B38..0x455B80). Kicked players are thrown out with S2C 0x08 `reason 2` "You were kicked out." (0x527E70, slot 0x54A804) [V string/reason; I that retail used it for dungeon kicks].
9. **Result.** [V] S2C 0x36 is read only on a mode-1 map. `result_kind 5, round_result 1` gives the item reward: it is added to the bag **by the client**, "You've received %s. (Count:%u)" (0x5298B0) is shown, reward window 0x4A5 ("Rewards Dungeon", UILngKo #8060) opens, effect 0x144 plays, and HP/MP are refilled. Any other `round_result` shows "Failed to conquer the instant dungeon." (0x528040, slot 0x54A7CC) and sets HP/MP from the packet. The dead room-host builder FUN_004296a0 revives dead players with HP 10 (0x4298AC). Wire bytes and hazards are in §3.13.
10. **Server today.** [V] (read only) `routes_2009` answers C2S 0x2C `list_type 5` with an empty 0xC2 (`test_world2009.test_room_list_windows`). It consumes C2S 0x2D and 0x95 ("no dungeon rooms (pvp owns rooms)", `windslayer_server.py` `routes_2009`; line numbers move while other workflows edit the file). The must-reply registry refuses C2S 0x18 with S2C 0x30 (the arena "too many channels" text, even for `room_type 5`) and 0x1A/0x1B/0x1C with `0x34 {2, type}` (`registry.py:271-274`). `en_maps.load_indun()` parses indun files but only extracts event-2 spawns, and indun maps have none (their monsters live in `SCRIPT_LISTS`).

---

## 1. Content (from the client data)

### 1.1 The three dungeons

| Dungeon template (hni idx = `dungeon_ref`) | Name (NPCLngKo) | Lv (`+0x650`) | `Weak_Atk_AI_ChkTime` (`+0x68C`) | `Strong_Atk_AI_ChkTime` (`+0x690`) | Description (`Infor` → `+0x6AC`) | Entrance NPC (UI 1220) | Stages | Time limit |
|---|---|---|---|---|---|---|---|---|
| 187 | #305 "Gang of forest" | 8 | 2 | **101** | #318 "A unknown monster has come to the forest that was the worst fear of village. What is this villein monster?" | 186 "Hidden forest" (#307, Infor #308 "Popola Village Find gang of forest, and let them pay for deeds.") | 101-106 (+ lobby 100) | 12 min |
| 197 | #326 "Monkey Ghost King" | 15 | 4 | **201** | #318 (same text) | 196 "Hades Range Door" (#325) | 201-206 | 20 min |
| 204 | #333 "Weapon Destroy" | 22 | 7 | **301** | #318 (same text) | 203 "Crater machine" (#332) | 301-305 | 20 min |

- [V] The records are hni `Quest: 2`, type 0, sprite `./hs/indun_button.hsi` (sprites 1/2/3). The hni loader FUN_00409dd0 counts them into `[gs+0x504]+0x124` and stores their positions in the array `+0x128` (0x409150 tail). `record+0x154` = the 1-based list position (`puVar6[0x55] = count` in FUN_00409dd0).
- [V] Field offsets come from the loader token switch (buffer base `local_12d0`): Quest +0x648, UI +0x64C, Lv +0x650, type +0x654, Weak_Atk_AI_ChkTime +0x68C, Strong_Atk_AI_ChkTime +0x690, Infor text +0x6AC, name +0x000.
- [V] The first-stage codes 101/201/301 sit in `Strong_Atk_AI_ChkTime`. [I] It is the dungeon's first stage (the client never reads +0x690 of a dungeon record). The meaning of 2/4/7 is unknown (open question 3).
- [V] Reward pool candidates: each dungeon record's `item:` column lists the same 16 items (`Drop: 5000` each). They are 1343-1346 "Old Gold Ring - Fire/Water/Earth/Wind" (Kind 17), 1395-1398 "Old Gold Necklace -…" (16), 1443-1446 "Old Belt -…" (14) and 1491-1494 "Old Earrings -…" (15). The column is loaded to record+0x288 (`local_1048`). [I] It is the clear-reward table. The dead host's reward picker FUN_00424e30 walks a *different* array `record+0xA98` (count +0xAA0, round-robin index +0xA9C), which **no loader fills**: only the destructor FUN_00408cc0 touches it. So the real reward list was room-server data.
- [V] Time limit: stage 1 of each dungeon has a `Condition 2 / Action 16` row (`Time` 720000 / 1200000 / 1200000 ms).
- [V] Map name ids (MapInfo `name` → MapLngKo): 299 "Unknown Forest", 300 "Unknown Cave", 301 "Dungeon Lobby", 303 "Hades Range Entrance", 304 "Hades Range halfway1", 305 "Hades Range halfway2", 306 "Hades Range valley", 307 "Hades Range cliff", 308 "Hades Range top", 281 "Balderan Dungeon". They are shown on the HUD (map+0x184, cut to 20 chars + "…" by the 0x2F handler).

### 1.2 Stage files

[V] Stage code = `AA*100 + BB` for `indunAA_BB.hmi`. The same number is `map+0x1C4` (computed from the file name, stored at 0x407BA3 in FUN_00407800), and it must be the map code in every UDP snapshot header (see §3.16). Stage codes are a **separate namespace** from field map codes: indun 101 is not stage01_01, which also has `map+0x1C4 = 101`, but the two are never loaded together. The exe also carries a file list `hs/indun01_00..05, 02_01..05, 03_01/02/03/05` (0x4CC6CB.., no xrefs; probably a patcher list). `indun01_06`, `indun02_06` and `indun03_04` exist on disk but are not in it. That is harmless.

| Stage | MapInfo | Scripts | Monsters (template × count; client uid range) | Forward portal (ScrIdx: condition → tile, dest) | Other |
|---|---|---|---|---|---|
| 100 | name 301, **no mode**, `InDunWaitingMap=1`, `ConnInDunMapName=indun01_01.hmi` | 0 | – | – | event-3 tile L5T5 (1100,1400). Unused by the client (§0.5) |
| 101 | 299 mode 1 InDun | 3 | 190 Statue ×1 at 3 s (33000000) | #1: time+cleared 10 s → L3T8 (1455,802) → 102 | #3 time limit 720000 ms. Start tile (event 3, value 1) L4T4 (200,900) |
| 102 | 299 | 9 | 4 Pponyang ×8 (2 immediate, 6 at 3..13 s) | #3: time+cleared 13.001 s → L3T23 (2255,802) → 103 | back tile L4T4 → 101 |
| 103 | 299 | 23 | 188 Sutah ×18 (1..32 s), 189 Ledulgi ×4 (40..52 s) | #23: time+cleared 52.001 s → L6T46 (7700,900) → 104 | back L4T42 → 102 |
| 104 | 299 | 17 | 3 Ororing ×10, 5 Poco ×5, 190 Statue (time+cleared 10 s) | #1: **Statue dead** → L7T7 (7600,1700) → 105 | back L7T33 → 103 |
| 105 | 300 | 6 | 190 Statue at 0.5 s; on Statue death 189 Ledulgi ×4 | #6: time+cleared 20 s → L3T6 (1350,650) → 106 | back L3T1 → 104 |
| 106 | 299 | 31 | 189 ×10 (5..32 s), 5 Poco ×6 (180..195 s), 6 Kamikaze Rat ×11 (243..253 s), **12 Rynx** at 256 s (33000027) | none | #1 reward NPC when **Rynx dead**. #18/#30 banner (indun_title sprite 1 "warning" + `warning_alarm.wav`) at 240 s / 253 s. back L5T2 → 105 |
| 201 | 303 | 13 | 193 Slow Peach ×11 at 3 s | #13: time+cleared 10 s → L7T42 (4900,500) → 202 | #1 time limit 1200000 ms. Start L7T2 (100,500) |
| 202 | 304 | 13 | 193 ×6, 195 Quick Peach ×6 at 3 s | #13: time+cleared 10 s → L2T20 (1900,415) → 203 | back L7T8 → 201 |
| 203 | 305 | 17 | 15 Monkey Soldier, 140 Monkey Lord at 3 s; on their deaths 194 ×6 / 192 ×8 | #1: time+cleared 60 s → L2T23 → 204 | back L7T9 → 202 |
| 204 | 306 | 30 | 15/194/192/140 ×28 (3..12 s), 190 Statue (time+cleared 60 s) | #1: **Statue dead** → L5T30 (1900,779) → 205 | back L8T2 → 203 |
| 205 | 307 | 18 | 17 Monkey General ×6, 191 Bogy Monkey No.1 ×11 | #18: time+cleared 60 s → L6T156 (900,600) → 206 | back L6T94 → 204 |
| 206 | 308 | 40 | 194 ×21 (3..40 s), 190 Statue (time+cleared 50 s); on Statue death **81 Monkey King** + 192 ×8 + 191 ×7 | none | #24 banner on Statue death. #40 reward NPC when **Monkey King dead**. back L3T1 → 205 |
| 301 | 281 | 30 | 26 Iron Ball / 198 Iron Ball MK2 / 199 Ball Kuu ×26 (3..60 s), 190 Statue at 60 s | #28: **Statue dead** → L3T9 (2850,401) → 302 | #1 time limit 1200000 ms. #29/#30 banners (sprites 11/12) at 60 s. Start L5T6 (0,1307) |
| 302 | 281 | 28 | 199/27/200/201 ×24, Statue at 60 s | #26: Statue dead → L2T16 → 303 | banners at 60 s. back L3T1 → 301 |
| 303 | 281 | 31 | 142/201/199 ×27, Statue at 60 s | #29: Statue dead → L3T1 (0,350) → 304 | banners at 60 s. back L4T59 → 302 |
| 304 | 281 | 23 | 200/28 ×19, Statue (time+cleared 13 s) | #21: Statue dead → L3T53 (2850,350) → **304 (data bug; must be 305)** | banners time+cleared 13 s. back L4T65 → 303 |
| 305 | 281 | 24 | 202 King Golem ×16, 200/199/198/201, **94 Atomic Ball** (time+cleared 9 s) | none | #16 reward NPC when **Atomic Ball dead**. #8 banner time+cleared 9 s. back L5T11 → 304 |

All [V] from `scripts_summary.json` (ScrIdx = file order, which equals XML `ScrIdx` in all 18 files; FUN_0040b790 overwrites the field with the running count). Positions are **tile** positions. The client adds the sprite's first floor-line offset (FUN_00407530), so exact coordinates need the .hsi line data (en_maps already reads it for field maps) [I].

Monster templates (hni idx; Lv / HP / Def / Exp) [V]: 3 Ororing 3/15/8/22; 4 Pponyang 5/30/13/34; 5 Poco 6/50/18/40; 6 Kamikaze Rat 5/30/13/30; 12 Rynx 8/174/31/100; 15 Monkey Soldier 9/104/30/96; 17 Monkey General 13/138/48/146; 26 Iron Ball 15/144/57/176; 27 Bryan Ball 17/168/66/206; 28 Radiant Ball 19/192/75/236; 81 Monkey King 15/288/57/282; 94 Atomic Ball 23/524/100/608; 140 Monkey Lord 12/132/44/132; 142 Screwroid 18/180/71/222; 188 Sutah 4/20/10/26; 189 Ledulgi 6/50/18/40; **190 Statue 8/5/9999/2** (`indun_stone.hsi`, the breakable stone that gates most portals); 191 Bogy Monkey No.1 15/138/48/112; 192 No.2 15/132/44/100; 193 Slow Peach 15/5/9999/48; 194 No.3 15/104/30/66; 195 Quick Peach 15/69/22/48; 198 Iron Ball MK2 22/144/57/140; 199 Ball Kuu 22/168/66/170; 200 Quaker Ball 22/192/75/202; 201 Elecroid 22/180/71/186; 202 King Golem 22/259/88/238.

### 1.3 The script language (`SCRIPT_LISTS`)

[V] Parser: FUN_00407800 0x4085F6..0x408886. Each row becomes a 0x578-byte object appended to the list `map+0x40` by FUN_0040b790. That function sets `+0x000 = running index (1-based)`, and it also increments `[list]+0x18` for Action 0x11 rows (see renderer gate below). Field layout:

| Offset | XML attribute | Read when |
|---|---|---|
| +0x000 | ScrIdx (overwritten with the list position) | always |
| +0x004 | PlayerIndex | Condition 1, 4, 5 |
| +0x008 | Condition | always (switch table 0x4089F0) |
| +0x00C | Action | always (switch table 0x408A08) |
| +0x010 / +0x014 | LayerIndex / TileIndex | Condition 1 |
| +0x018 | Time | Condition 2, 6 |
| +0x560 | CondItem | Condition 4 |
| +0x534 | the map code `map+0x1C4` at load | always |
| +0x530 / +0x540 | activation tick / active flag | runtime |
| +0x548 | watched entity (Condition 5) | bound by FUN_00423e30 |
| +0x54C / +0x550 | ActAprLocX / ActAprLocY, or a computed anchor | Actions 12, 17 / runtime |
| +0x554 / +0x558 / +0x55C | ActMons/ActPrtl LayerIndx, TileIndx, MonsIndx; or InDunRwdNpcIndx (+0x55C); or ActLayerIndx (resolved to a sprite handle) / ActTileIndx | Actions 13, 14, 15, 17 |
| +0x564 | ActTime | Action 17 |
| +0x568 | ActItmAprItmIndx | Action 12 |

| Condition | Meaning | Used in EN data | Evaluated where |
|---|---|---|---|
| 1 | player on tile (PlayerIndex, LayerIndex, TileIndex) | no | nowhere in the client [V] |
| 2 | Time ms elapsed | yes (monster spawns, time limit, banners) | nowhere in the client except the time-limit read (below) [V]. Server [I] |
| 3 | immediately | yes | client FUN_0042a300 (sets +0x540) [V]. Server |
| 4 | player has item (CondItem) | no | nowhere [V] |
| 5 | monster PlayerIndex (template) dead | yes (Statue, bosses) | client FUN_0042a300: the watched entity's render state == 0x10 → active, anchor = death position, **and scene+0xF3C = scene+0xF38 (slow-motion)** [V]. Server |
| 6 | Time elapsed **and** all monsters cleared (`scene+0xF9C`, set when no type-4 entity outside state 0x16 remains, FUN_00414210 store at 0x415615) | yes (most portals) | client FUN_0042a300 [V]. Server |

| Action | Meaning | Client reaction |
|---|---|---|
| 12 (0xC) | item appears at (X,Y) | none found [V] |
| 13 (0xD) | monster appears | pre-created hidden at load (FUN_00423970) [V]. Appears when the host's snapshot changes its state [I] |
| 14 (0xE) | portal appears | S2C 0xA7 → FUN_00406550: anchor from the tile's first floor line, `+0x540 = 1`, `[list]+0x18 += 1`. The renderer FUN_004063b0 then draws sprite **0x14A** at the anchor and the screen banner sprite **0x153** at (220,100) [V] |
| 15 (0xF) | reward NPC (InDunRwdNpcIndx; absent in EN data = 0) | renderer draws sprite **0x154** at +0x54C/+0x550 when Condition == 5 (the boss death spot) [V]. Nothing on the wire |
| 16 (0x10) | time limit | only with Condition 2: `scene+0xF53 (round_time_min) = Time / 60000` in FUN_00423e30 (store at 0x423EC6), **only on the S2C 0x2F load**, not on 0xA5 [V] |
| 17 (0x11) | timed banner/animation: sprite (ActLayerIndx→hsi, ActTileIndx) at screen (X,Y) for ActTime ms | S2C 0xA7 → FUN_00406550 sets `+0x530 = tick`, `+0x540 = 1`. Also set locally for Conditions 3/5/6. Drawn until tick > +0x530 + +0x564, then the row is freed and `[list]+0x18 -= 1` [V] |

Renderer gate [V]: FUN_004063b0 draws nothing while `[map+0x40]+0x18 < 1`. That counter starts at the number of Action-17 rows and is raised by every portal activation through 0xA7. So a stage with neither (101, 102, 103, 105, 201-205) shows no portal marker until the first 0xA7.

Client-local script checks [V]: FUN_0042a300 runs only when `scene+0xF18 == 6` **and** `scene+0xF1C == 1` (round running, set by UDP opcode 8). FUN_00423e30 (Condition-5 binding and the Action-16 timer) runs only from the 0x2F handler, **never from 0xA5**. So on stages 2+ the client cannot evaluate Condition 5 by itself: no slow-motion and no reward-NPC marker unless the server enters that stage with a fresh 0x2F (open question Q7). Everything that matters (spawns, portals, results) must come from the server anyway.

### 1.4 Per-stage monster uid rule (for the server's monster table)

[V] FUN_00423970 (0x423970..0x423CED):

```
uid = 33000000
for row in SCRIPT_LISTS (file order):
    if row.+0x540 != 0 or row.map != map+0x1C4 or row.Action != 13: continue
    if tile (ActMonsAprLayerIndx, ActMonsAprTileIndx) has no floor line: continue      # no uid consumed
    if template ActMonsAprMonsIndx missing: continue                                  # no uid consumed
    create type-4 entity: uid, anim index +0xEFC = MonsIndx, +0xF0C = row index,
        level/HP/MP/atk/def/exp/AI from the template, state +0x994 = +0x166C = 0x16 (hidden),
        ai-check-time *1000 (+0xEF4), RegisterLocalPlayer
    uid += 1
```

It runs on every 0x2F (InDun map) and every 0xA5. **Every room of the same stage uses the same uids.** Isolation comes only from each client receiving just its own room's traffic [I].

---

## 2. Client model

### 2.1 Windows

| Window | UI title (UILngKo) | Opened by | Controls the code handles (FUN_00448730) |
|---|---|---|---|
| 0x4C4 | #9152 "Dungeon list" | NPC click with hni `UI: 1220` [V-2008 NPC→UI mechanism; the proximity/click path is at 0x42E790 lines 1090-1144] | Opening (state 3/4) sends **C2S 0x2C {5}** and closing sends **{0}** (0x448B59 / 0x448E9B) [V]. ctrl 7 "Create" (hui Link 1222, see Q9). ctrl 8 "Join" → number dialog 0x49 with `scene+0xF90 = 5` and prompt "Please enter the number\r\nof the Dungeon room number\r\nto join." (0x527ED0) [V]. ctrl 9..13 = rows (FUN_00445c00(0) gate, level check `entity+0x9D >= row+0x3E` else "You need higher level" (0x52D3E0), `scene+0xF64 = room_no`, `scene+0xF90 = type`; a locked row opens 0x4A, else sends C2S 0x1A) [V]. Counter label "%u/%u" (FUN_00445750 mode 5) [V] |
| 0x4C5 | #9169 "Create dungeon" | ctrl 7 of 0x4C4 via UI link [I] | ctrl 7/0x11 focus the title/password edits (8/0x12). ctrl 0xB..0x10 capacity; captions "1".."5" go to ctrl 0xA. ctrl 0x16/0x17 cycle the dungeon (`[win+0x134]` = id from `[gs+0x504]+0x128`) and show "Lv" = record+0x650 in ctrl 9. **ctrl 0x13 Create** → gate FUN_00445c00(0), CheckString(title) else "No cursing \r\nplease" (0x52A2A4), template lookup else "Data error occurred.\r\nPlease, consult helpdesk." (0x527F90), `scene+0x991 >= record+0x650` else "You need higher level", then **C2S 0x18** and the modal "Waiting for the server to respond." (0x52CF5C) [V] |
| 0x4C6 | #9180 "Dungeon Party" | S2C 0x2F with mode 5 (state 3, 0x455B53) [V]. Closed by UDP opcode 8 (0x45E532) and S2C 0x08 [V] | Filled by FUN_004446c0 on 0x2C/0x2E/0x35/0x2D (InDun map only): slot i (0..4) = name (index 4+i), "Lv.%d" (9+i), status "Ready"/"Waiting" (0xE+i; 0x527EC0/0x527EC8; not shown for the master slot), class icon (0x1E+i). The master sees per-member buttons 0x12+i enabled. `gs+0x4E0` = local is master, `gs+0x4E1` = all others ready [V]. Code event ids: **0x13 / 0x17** kick (0x13 arms, 0x17 confirms; **only slot 1 is wired**, jump table 0x44ED04 = `[0,4,4,4,0,4,4,4,1,2,3]` for 0x13..0x1D) → **C2S 0x2D {0, uid}**. **0x1B** Ready (only when own `+0x158C == 0`) → **C2S 0x1D {1}**. **0x1C** Leave → exit dialog 0x4B. **0x1D** Start (master; `gs+0x4E1` else chat "All have to be ready." 0x527EA8) → **C2S 0x95** [V]. Captions ↔ ids are unresolved (Q9) |
| 0x4B | "Leave Arena" (ctrls Question / Reserve Quit / Leave Now / Cancel) | 0x4C6 event 0x1C; 0x47A ctrl 1 [V-2008] | ctrl 2 → C2S 0x1D {3} (disabled for mode 5), ctrl 3 → C2S 0x1D {4} (enabled for mode 5) [V 0x44C32A/0x44C336, 0x455B63..0x455B80] |
| 0x47A | play-room HUD reused as the in-dungeon HUD | UDP opcode 8 in mode 5 (0x45E546, state 3) [V] | exit → 0x4B [V-2008] |
| 0x4A5 | #8060 "Rewards Dungeon" (slot01 icon, OK) | S2C 0x36 kind 5 clear [V] | OK closes it. No packet found [I] |
| 0x49 | number dialog (shared) | 0x4C4 ctrl 8 | OK → C2S 0x1B {n, 5}, n in 1..196 [V] |
| 0x4A | password dialog (shared) | locked row / 0x34 result 3 | OK → C2S 0x1C {F64, 5, pw} [V] |
| 0x133 | world "balloon" join confirm | click a player whose room balloon kind is 5: "Will you\r\nenter the dungeon?" (0x527E88, FUN_00450130) [V] | Yes → C2S 0x1B {room_no, 5} [V] |

### 2.2 Scene/map fields used by the dungeon

| Field | Meaning | Written by |
|---|---|---|
| scene+0xF52 / +0xF53 | round_count / round_time_min (the HUD countdown `F53*60 - header_seconds`, "%d:%02d", 0x45E810) | 0x2F; +0xF53 also by Action 16 (0x2F load only) [V] |
| scene+0xF54 str[16] / +0xF64 u16 | host_ip / room_no (UDP target `F54:F64+10000`) | 0x2F (and the join paths for F64) [V] |
| scene+0xF66 str[17] | room title | 0x2F [V] |
| scene+0xF90 | room mode (5) | 0x2F; the join paths set 5 for dungeon rows/number [V] |
| scene+0xF1C | round phase 0 waiting / 1 running / 2 ended | UDP 7/8/9 via FUN_004296a0(scene, n) (0x42A0xx) [V] |
| scene+0xF3C | slow-motion start (Condition 5 fired); suppresses "Timer Error." | FUN_0042a300; cleared by FUN_0042dcf0 [V] |
| scene+0xF9C | "all monsters cleared" | FUN_00414210 case 0x10 on InDun maps [V] |
| map+0x7C | MapInfo `mode` (1 for every indun stage) | loader [V] |
| map+0x2DC / +0x2E4 / +0x3E8 | InDunWaitingMap / ConnInDunMapName / its code | loader only, never read [V] |
| map+0x2E0 | InDun | loader. Gates FUN_004446c0, FUN_00423970, FUN_0042a300, scoreboard skip (FUN_00444220 / FUN_00443bc0 return early) [V] |
| map+0x40 | script list | loader [V] |
| entity+0x158B / +0x158C | team / next_team (= **ready** in a dungeon room) | 0x2E/0x2C tail, 0x35, 0xC3 [V] |
| entity+0x1590 | score (the host adds each kill's party-share EXP here, FUN_0041b160) | 0x37, host [V] |

### 2.3 What the client blocks inside a dungeon

[V] FUN_00445e30(param) on a mode-1 map with `scene+0xF90 == 5`:
- It returns 0 (allowed) for callers that pass 0. The quickslot/item path FUN_0044f070 does this first, so **skills and consumables still go out as C2S 0x15 on TCP** in a dungeon (arena rooms block them all with "Not allowed in arena.").
- With param 1 it shows "You can't do that in the dungeon." (0x527FC0) and refuses. Callers: item drop, the ids in table 0x54A398 inside FUN_0044f070, server-select, stall and others (FUN_00472f40, FUN_0046fcc0, FUN_00470ae0, FUN_00489aa0, FUN_0046d6f0, 4 sites in FUN_00448730).

The room-list open gate (FUN_00448730 0x448AC2: `map+0x7C != 1`) keeps every room list closed inside a dungeon [V].

---

## 3. Opcode reference

Each entry: direction, wire grammar with the client VA that reads/writes it, client effect, the server's duty, errors. Wire formats are those of `protocol_spec_2009.json` unless noted.

### 3.1 C2S 0x2C RoomListWindowOpenClose — `u8 list_type`
- [V] Builder FUN_004455b0 (Add 0x4455CC, Send 0x4455DD). `5` when 0x4C4 opens (0x448B59), `0` when any list closes (0x448E9B), including the forced close caused by S2C 0x2F.
- Server: on `5` → subscribe the session to the dungeon list and send **S2C 0xC2** with all joinable dungeon rooms. On `0` → unsubscribe, **no reply**. [V-2008] a reply to a close froze the 2008 client.
- No modal. If `5` is unanswered the list just stays empty.

### 3.2 S2C 0xC2 InstanceDungeonRoomListReset / 0xC1 …Append
```
u8  total_rooms                  @0x45A8E1 -> "%u/%u" denominator (0x4C4)
u8  room_count                   @0x45A8F5
repeat room_count (26 B each):
  str[17] room_name              @0x45A939 -> row+0x00
  u16 room_no                    @0x45A950 -> row+0x3A   ("%03d")
  u8  max_players                @0x45A967 -> row+0x3C
  u8  cur_players                @0x45A97E -> row+0x3D
  u8  room_level                 @0x45A995 -> row+0x3E   (min level; row disabled if > own level)
  u8  room_type = 5              @0x45A9AC -> row+0x3F
  u16 npc_index = dungeon_ref    @0x45A9C3 -> row+0x44   (tpl lookup -> row+0x50)
  bool has_password              @0x45A9DA -> row+0x40
```
- [V] 0xC2 clears window 0x4C4, sets the label to "0/0", **closes message box 0x16** (0x45A8BE), then falls through into the 0xC1 body. 0xC1 appends without dedupe.
- Row text: "%03d %s(%02d/%02d) Lv.%d" (FUN_004455f0) [V].
- Hazard [V]: rows go to window 0x4C4 through FUN_0049e830 only while it is the active list. Send only to subscribers, in world.
- Server [I]:
  - `room_level` = the dungeon's Lv (8/15/22), because the client greys rows above the viewer's level.
  - `total_rooms` = the number of rows sent.
  - List only rooms in the waiting phase (a started room answers joins with 0x34 {9}).
  - On any change (create/join/leave/start/delete), re-send the full 0xC2 to subscribers (same rule as 2008 F1.3).

### 3.3 S2C 0x31 RoomListAddEntry (type 5 form)
- [V] `str[17], u16, u8 max, u8 cur, u8 level, u8 type, [u16 dungeon_ref if type==5], bool`. A type-5 row is added to 0x4C4 only when that window is active. It is not stored on any list (0x45A2A4).
- [I] Not needed. Use 0xC2 re-sends. **Never** put type-5 rows into 0x32/0x33, which have no `dungeon_ref`.

### 3.4 C2S 0x18 InstanceDungeonCreateRoom — 27 B
```
str[17] room_title   @0x44DEE0  edit ctrl 8 (raw buffer, NUL-terminated text)
u8  max_users        @0x44DEED  atol(caption ctrl 0xA) -> 1..5
u8  round_time_min   @0x44DEF7  constant 1
str[5] password      @0x44DF14  edit ctrl 0x12 ("" = none)
u16 map_id           @0x44DF21  = record+0x154 = dungeon template index (187/197/204), NOT a map code
u8  room_type        @0x44DF2F  constant 5
```
- **MUST-REPLY** [V]: modal "Waiting for the server to respond." (0x44B959..0x44B97A). Only a server packet closes it.
- Server validation [I]:
  1. Parse strings up to the first NUL.
  2. The dungeon must be one of the Quest-2 templates, else `0x34 {2,5}`.
  3. Level >= template Lv, else `0x34 {6,5}` "Your level is not appropriate." (0x52D370).
  4. Not already in a room or queue.
  5. manner >= -19.
  6. Room pool limit, else S2C 0x30 (arena "too many channels" text; there is no dungeon-specific text) [V 0x30 text].
- Success [I, 2008 order]: `0x34 {5, 5}` (closes the box, clears 0x4C4 rows, 0x45AA..) → S2C 0x2F (mode 5) → S2C 0x2E (roster = creator). Then re-send 0xC2 to subscribers.

### 3.5 C2S 0x1A RoomJoin (row) / 0x1B RoomJoinByNumber / 0x1C JoinRoomWithPassword
- [V] 0x1A `u16 room_no, u8 room_type=5` (0x44B939..0x44B961). 0x1B `u16 room_no (1..196; 1..197 from dialog 0x133), u8 5`. 0x1C `u16 room_no, u8 5, str[5] password`. All three show the **MUST-REPLY** modal.
- Gates [V]: FUN_00445c00. Arg 0 for rows, arg 1 for the number dialog, which adds level >= 5. It refuses on manner < -19 (0x52A360 "You can't use play room,\r\nbattlefield,or arena due to your\r\nlow manner point."), on a room map, when a party-play window is not in state 1, when not standing still ("Only able during stop motion."), when a monster targets the player ("Not during hunting/battle."), or during a stall/shop.
- Server replies, S2C 0x34 `{u8 result, u8 room_type}` [V strings, 0x34 handler 0x45AA69..]:

| result | Client effect | Server use |
|---|---|---|
| 1 | "Incorrect password." (0x52D7A8) | wrong password |
| 2 | "Invalid room #." (0x52D778) | no such dungeon room / kind mismatch |
| 3 | closes the box, opens password dialog 0x4A | locked room joined without a password |
| 4 | "The room is full\r\n(Please try again in a few minutes.)" (0x52D740); sets the matching row cur = max in the active list | full |
| 5 | closes the box. `room_type 5` branch clears 0x4C4 rows + label (no list free) | accepted → continue with 0x2F + 0x2E |
| 6 | "Your level is not appropriate." | level < room_level |
| 9 | "Already started." (0x52B324); removes that row from the active list | run already started (**new in 2009**) |

- Success sequence [I]:
  1. `0x34 {5,5}` → `0x2F` → `0x2E` to the joiner, listing **every** member in join order so the master stays first.
  2. `0x2C` (single spawn, team 1, next_team 0) to each existing member.
  3. Re-send 0xC2.

### 3.6 S2C 0x2F RoomGameEnter (mode 5) — 49 B
```
u8  round_count      @0x455708 -> scene+0xF52   (1)
u8  round_time_min   @0x455728 -> scene+0xF53   (overwritten by Action 16: 12 / 20 / 20)
u16 map_id           @0x455743   loads ./hs/indun%02u_%02u.hmi when room_mode == 5  -> 101 / 201 / 301
str[16] host_ip      @0x455764 -> scene+0xF54   (UDP host address the client can reach)
u16 room_no          @0x455784 -> scene+0xF64   (UDP port = room_no + 10000)
u8  current_round    @0x4557A4 -> scene+0xF77   (1)
u8  blue_round_wins, blue_round_losses, red_round_wins, red_round_losses   (0)
u8  red_score, blue_score                                                   (0)
u8  room_mode = 5    @0x455881 -> scene+0xF90
str[17] room_title   @0x4558DB -> scene+0xF66
u16 dungeon_ref      @0x455905   (only if room_mode == 5) -> tpl(dungeon_ref)
```
- [V] Before reading: `gs+0x478 = 0xA5A`, `scene+0xF20 = 0`, `scene+0xF24 = tick`, alive counters 0. It closes UI 0x16, 0x2A, 0x14, 0x478, 0x17B, **0x4C4**, 0x71. If a list was open the client then emits C2S 0x2C {0}: consume it.
- [V] Labels on 0x4C6: ctrl 2 = `"%s %s [%s]"` of (" Room<no>", title, **template name** record+0) (0x4559C1). If the template is missing it uses `"%s %s"`. Ctrl 3 = the template's Infor description (record+0x6AC). Exit dialog text/buttons as in §2.1. Window **0x4C6 opened**.
- [V] Then:
  1. fade, unload, load the stage (on failure only "Could not load MAP. (%s)");
  2. FUN_0042dcf0 (HUD reset; clears scene+0xF3C/+0xF1C);
  3. map name to the HUD;
  4. **FUN_00424130 frees every entity including the local player (scene+0x988 = 0)**;
  5. FUN_00428750, FUN_00444ca0;
  6. with InDun: FUN_00423970 (hidden stage monsters) and FUN_00423e30 (Condition-5 bindings + Action-16 timer);
  7. `scene+0xF18 = 7` (loading).
- **MUST follow with S2C 0x2E** that includes the receiver [V-2008 + V 0x2E tail sets scene+0xF18 = 6]. Otherwise the client stays in the loading state with no local player.
- **Do not** send 0x03/0x07/0x08 here.

### 3.7 S2C 0x2E ArenaEnterRoster / 0x2C ArenaPlayerSpawn / 0x2B ArenaPlayerRoster / 0x2D ArenaPlayerDespawn
- Grammars as in the 2009 spec [V]. 0x2E/0x2B: 338 B base per player, a per-receiver pet block, hp/mp words, tail `u8 arena_team, u8 arena_next_team, u8 kills, u8 deaths, u32 score`. 0x2C: 328 B base, **unconditional** pet block, no hp/mp, tail `team, next_team`.
- Dungeon rules:
  - `arena_team` **must be 1** for every member [V]. Team 0 makes the entity a spectator (`+0x9C = 1`), and FUN_004446c0 lists only type-3 entities, so a team-0 member disappears from the party window. It also takes the member out of the dead host's alive count.
  - `arena_next_team` = 1 when ready, 0 when waiting.
  - The master's own entry is always shown as master. Send its `next_team` as 0 [I].
  - Order = join order [V, see §0.7].
- 0x2D `{u32 uid}` removes a member and refreshes 0x4C6 (FUN_004446c0) [V]. Never send it for the receiver itself [V-2008].
- 0xC3 RoomTeamUpdateBatch `{u8 n; n × (u32 uid, u8 team, u8 next_team)}` [V] could set several ready flags at once. [I] Not needed.

### 3.8 C2S 0x1D ArenaTeamSubscribe — `u8 code` (dungeon use)
- [V] `1` = Ready (0x4C6 event 0x1B → 0x44A94D; only when own +0x158C == 0). `4` = Leave Now (0x4B ctrl 3). `3` (Reserve Quit) is disabled for mode 5 but can still be crafted. No modal for any code.
- Server [I]:
  - `1` → `member.ready = True`; broadcast **S2C 0x35 {uid, team=1, next_team=1}** to the room. The client stores next_team in +0x158C and redraws "Ready" (0x45AE1F → FUN_004446c0). **Never send next_team 3** (leave flag).
  - `4` (and `3`, treated as 4) → leave (F7).
  - `0`/`2` → ignore.

### 3.9 C2S 0x2D InstanceDungeonRoomKick — `u8 0, u32 target_uid`
- [V] 0x44E2BA..0x44E2D3. The target is found **by name** in the local scene. Only the button pair of roster slot 1 (the second member) is wired. There is no modal.
- Server [I]:
  - Accept only from the room master, only in the waiting phase, and only for a member of the same room.
  - The target gets **S2C 0x08 {reason 2, return_map, time}** "You were kicked out." plus the field re-entry tail (0x03, 0x07, …; MapTransfer).
  - The others get **S2C 0x2D {uid}**. Re-send 0xC2 to subscribers.

### 3.10 C2S 0x95 InstanceDungeonStart — no body
- [V] Master only (0x4C6 event 0x1D), after the client-side "all ready" check. No modal.
- Server [I]:
  - Re-check: sender is master, phase is waiting, every non-master member is ready, 1 <= members <= max.
  - Then set `phase = running`, run clock t0, and **start the UDP phase stream with opcode 8**. That is what closes 0x4C6 and opens HUD 0x47A [V].
  - Remove the room from the list (re-send 0xC2); further joins → 0x34 {9}.
- If the server ignores 0x95 nothing happens and the master can press again (no lock).

### 3.11 S2C 0xA5 InstanceDungeonStageLoad — `u16 stage_code`
- [V] Read at 0x45D746. Fade (FUN_0043d810), `sprintf("./hs/indun%02u_%02u.hmi", code/100, code%100)`, free map objects (FUN_00407640), load (FUN_00407800; on failure only "Could not load MAP. (%s)", 0x52E0A0). Then:
  - FUN_00424420 deletes type-4 entities without a pet owner, i.e. the old monsters (players are type 3 and stay);
  - with InDun, FUN_00423970 creates the new stage's hidden monsters (uids restart at 33000000);
  - `gs+0x16F8 = 0`.
  - It does **not** call FUN_00423e30. The time limit and Condition-5 bindings are not refreshed.
- No gate: outside an instance it replaces whatever map is loaded. No reply.
- Server [I]:
  - Send it to every member at the same moment, when the portal rule is met (F9).
  - Then reset the room's monster table for the new stage (§1.4).
  - Switch the snapshot header map code to the new stage code (else the client drops the snapshots [V header compare, SHR 0x12 at 0x45E37E]).
  - Place the players at the arrival point in the next snapshots. The arrival point is the tile whose `event 1` portal leads back to the previous stage [I], or the event-3 start tile on stage 1.

### 3.12 S2C 0xA7 InstanceDungeonObjectTrigger — `i32 object_index`
- [V] Read at 0x45D819, then FUN_00406550(map, tick).
  - `object_index` = the 1-based script row (= ScrIdx).
  - Action 14 → the portal becomes visible (sprite 0x14A at the tile's floor-line centre, banner 0x153).
  - Action 17 → the banner/animation starts.
  - Anything else, 0 or out of range → ignored.
- Server [I]: send it to the room when a portal row's condition is met, and when an animation row fires. It is idempotent for portals (re-activation only re-increments the draw counter).

### 3.13 S2C 0x36 RoomRoundResult, dungeon form
```
[read only if map+0x7C == 1 (0x45AF3C)]
u8  result_kind = 5                 @0x45AF63
u8  round_result                    @0x45AF82 -> scene+0xF7C
if round_result == 1:                            # cleared
    u32 dungeon_unused              @0x45AFBE   (read, ignored; the dead host puts the member's score entity+0x1590 here)
    u16 reward_item_id              @0x45AFFF   (0 = none)
    if reward_item_id != 0:
        u16 reward_item_tail        @0x45B021   (option word 5 of the item's 12-byte option block)
else:                                            # failed
    if local player exists:
        u16 hp                      @0x45B331 -> local +0xA0
        u16 mp                      @0x45B348 -> local +0xA4
```
- [V] Clear:
  - closes 0x4A5 if open;
  - looks up the item (FUN_00404750) and adds it to the bag by the item def kind +0x154: consumable (FUN_00424fb0), equip (FUN_00425420, **only if the equip bag holds < 46**), other (FUN_004252a0);
  - prints "You've received %s. (Count:%u)" with count 1 (0x5298B0);
  - puts a reward record into 0x4A5, opens 0x4A5, plays effect 0x144 (FUN_0042cd90), refills local HP/MP.
- [V] Fail: message box "Failed to conquer the instant dungeon." (0x528040), then HP/MP from the packet.
- Lengths [V]: 8 (clear, no item), 10 (clear with item), 6 (fail), 2 (fail with no local player).
- Hazards [V]:
  - Ignored entirely on a mode-0 map.
  - The reward is additive; sending twice grants twice on the client.
  - The encoder needs `assume` for the map-mode and local-player conditions, as the 2008 codec notes say (`pvp_arena.md` B17).
- Server [I]:
  - Add the same item to the server inventory with the same slot rule the client uses (item_inventory owner). If the equip bag is full the client silently drops the item, so the server must check `equip_count < 46` first, or pick another item.
  - Grant EXP/gold with the normal packets (S2C 0x21 exp delta / 0x18); 0x36 carries none.
  - Send per member: each member can get a different item.

### 3.14 S2C 0x37 ArenaScoreUpdate (dungeon)
[V] It updates entity scores (mode 5 < 6 → **added**) and prints nothing (the "[Blue%d:%dRed]" line needs mode <= 3). The scoreboard windows skip InDun maps (FUN_00444220 / FUN_00443bc0 early return). [I] Not needed.

### 3.15 S2C 0x08 ChangeMap (return to field) — `u8 reason, u16 map_code, u32 game_time_ms`
- [V] reason 0 = plain; 1 "Connection to PVP server was lost…"; **2 "You were kicked out."**; 3/4 moderation popups.
- [V] The handler closes 0x2A, **0x4C4**, 0x480, 0x4D, **0x47A**, 0x47E, **0x4B**, **0x4C6**, clears the room state because the old map was mode 1, destroys all entities and loads `stageAA_BB`.
- Must be followed by the field re-entry tail (0x03, 0x07, …) [V-2008].

### 3.16 UDP room host (shared with P9/P10) `[UDP]`
[V] Client side, FUN_00424500 / FUN_0045e230. The Fireway UDP framing is owned by the P9/P10 spec.
- **Client → host**, every 30 ms tick while the local player is type 3 or before the first snapshot (`scene+0xF20 == 0`), else every 9 s: opcode 3 `{u32 uid (scene+0x224), u8 keyA = scene+0x259*10 + scene+0x258, u8 keyB = scene+0x25A*10 + scene+0x25B}` to `scene+0xF54 : scene+0xF64 + 10000`. `keyA/10 == 4` is the portal/Down input (compare 0x4311DE) [I].
- **Host → client**, snapshot opcodes 7 (waiting), 8 (running), 9 (ended). Header `u32` in scene+0xF04:
  - bits 0-9 seconds (**10 bits: wraps at 1023 s = 17 min 3 s**, so a 20-minute countdown shows wrong values after 17 min [V arithmetic 0x45E810]);
  - bits 10-17 entity count;
  - bits 18-31 map code, which must equal `map+0x1C4` = the stage code.
- Then per entity 8 bytes (only type > 2 entities matched by uid are applied):
  - w0: x/2 (12 bits) | y/2 << 12 (11 bits) | facing bit 23 | (action-1) << 24 (6 bits) | hit flags << 30;
  - w1: flag bit 0 | uid << 1 (25 bits).
- Other host events: HP 6/0x0C/0x0E `{u32 uid, u16 hp, u16 x}`; MP 0x0B/0x0D/0x0F; hit/heal 0x0A `{u32 uid, bool, bool, [u16 hp], [u16 mp]}`; 0x10 …
- Dungeon-specific client behaviour on these packets [V]:
  - Opcode 8 on the first transition: `FUN_004296a0(scene,1)`, **closes 0x4C6, opens 0x47A** (mode 5), no HP refill in mode 5 (0x45E67F), no "The round will start…" texts on InDun maps (gate `map+0x2E0 == 0` at 0x45E709).
  - Opcode 9 → `FUN_004296a0(scene,2)` (phase ended).
  - Entities in state 0x16 are hidden. A monster "appears" when a snapshot gives it another action. Death = action 0x10, then 0x16.

### 3.17 Dead code in the client (never received)
[V] C2S 0x0A `RoomHost_DungeonResult` (FUN_004296a0 @0x429919) is the room host's per-player result broadcast. Its record mirrors S2C 0x36 kind 5 (cleared: `u32 score, u16 item, [u16 tail]`; failed: `u16 hp (10 if <= 0), u16 mp`). The socket `scene+0xFC4` is only ever NULL and the host flag `scene+0xF40` is only ever 0. It is useful as the reference for what the server's host sends.

---

## 4. Flows

Notation as in `pvp_arena.md`. "Modal" = message box 0x16 "Waiting for the server to respond."

**F1. Reach the list.** [I] The server spawns entrance NPC 186 (and optionally 196/203) with S2C 0x1A in a field map (Popola, per its Infor text). The player clicks it → window 0x4C4 opens → C2S 0x2C {5} → S2C 0xC2. Gate G-ID1: confirm that a server-spawned type-0 template with `UI: 1220` is clickable. The client's NPC-interaction check needs type 4, `+0xF00 < 3`, a template and no pet owner (0x42E790 lines 1090-1094) [V], and 0x1A spawns are type 4 [V].

**F2. Create.** 0x4C4 ctrl 7 → 0x4C5 → pick the dungeon (ctrl 0x16/0x17), capacity 1..5, title, optional password → Create → C2S 0x18 (modal) → server validates → `0x34 {5,5}`, `0x2F` (mode 5, map 101/201/301, dungeon_ref), `0x2E` [creator, team 1, next 0]. The creator is on stage 1 and window 0x4C6 shows itself as master. The master's Start button is always enabled (FUN_004446c0 enables it for `gs+0x4E0`); pressing it while any other member is not ready only prints "All have to be ready." (0x527EA8). With one member `gs+0x4E1` stays 1, so Start works at once [V].

**F3. Join.** A row click (C2S 0x1A), the number dialog (0x1B), the password dialog (0x1C) or a world balloon (0x1B, kind 5) → modal → server `_join_room(kind='dungeon')` → error `0x34 {1|2|3|4|6|9, 5}`, or `0x34 {5,5}` + `0x2F` + `0x2E` (all members, join order) to the joiner and `0x2C` (joiner) to the others. Re-send 0xC2.

**F4. Ready.** Non-master presses Ready → C2S 0x1D {1} → broadcast `0x35 {uid, 1, 1}` → "Ready" in slot; the master's Start is enabled when all others are ready.

**F5. Kick.** Master: slot-1 button → confirm → C2S 0x2D {0, uid} → target: `0x08 {2, return_map, t}` + field tail; others: `0x2D {uid}`; the room keeps its master.

**F6. Leave (waiting phase).** Leave → 0x4B → Leave Now → C2S 0x1D {4} → leaver: `0x08 {0, return_map, t}` + field tail; others: `0x2D {uid}`. If the master left, the next member in join order becomes master: the client promotes the first remaining type-3 entity automatically [V FUN_004446c0], so the server must use the same rule. An empty room is deleted. Re-send 0xC2.

**F7. Start.** Master → C2S 0x95 → server checks → UDP phase 8 stream to all members (0x4C6 closes, HUD 0x47A, countdown from `scene+0xF53`) → the script engine starts at t = 0 for stage 1.

**F8. Stage run** (server = host) [I]. Each tick:
- (a) apply inputs;
- (b) simulate players and monsters, including hits and HP;
- (c) evaluate script rows of the current stage, and fire each row once:
  - Condition 3 at stage start;
  - Condition 2 when `t_stage >= Time`;
  - Condition 5 when the watched template's (first) monster dies;
  - Condition 6 when `t_stage >= Time` and no appeared monster is alive.
- (d) Actions:
  - 13 → the monster becomes visible (state from 0x16 to its spawn/idle action in the next snapshots);
  - 14 → `S2C 0xA7 {row}` and the portal becomes usable;
  - 17 → `S2C 0xA7 {row}`;
  - 15 → reward NPC (§F10);
  - 16 → set the run time limit (stage 1 only).
- (e) Send snapshots/events.
- Kills add party-share EXP. The dead host computes `share = level_i / Σlevels × (2n+10)/10 × monster.Exp` per member (FUN_0041b160) [V formula shape; exact rounding FUN_004127a0 not traced]. The server grants it with S2C 0x21.

**F9. Portal** [I; retail KR evidence]. When a portal row has fired, every **living** member stands on that portal's floor line (tile event 1) with `keyA/10 == 4` (Down) in the same ~1 s window → `S2C 0xA5 {value_num}` to all members, then the new stage starts at t = 0 with fresh monsters. Dead members travel along. Suggested behaviour: revive them with 10 HP, as on failure.
- Data fix: stage 304's forward portal says 304, so map it to 305.
- The back-portals (event 1 to the previous stage) are never activated by any script [V], so ignore them.

**F10. Clear** [I]. On the final stage (106/206/305), the reward-NPC row fires (boss death: Rynx 12 / Monkey King 81 / Atomic Ball 94):
1. send per member `S2C 0x36 {5, 1, score, item, tail}`;
2. add the item and EXP server-side;
3. switch the UDP phase to 9;
4. after ~10 s (or on each member's Leave Now) return everyone with `0x08 {0, return_map, t}` + field tail;
5. delete the room.

**F11. Fail** [I]. Every member dead (the dead host fails a room only when no team-1 player with HP > 0 is left: FUN_0041b160 then calls FUN_004296a0(scene,2) at 0x41B697 [V]), or the time limit expires → per member `S2C 0x36 {5, 0, hp=max(hp,10), mp}` → phase 9 → return to field as in F10.

**F12. Disconnect** [I]. Remove the member as in F6 without the 0x08. If the room becomes empty, delete it. If a running room drops to zero living members, fail it as in F11.

---

## 5. What is shared with the PvP room machinery (P9/P10)

| Piece | Shared P9/P10 item (`pvp_arena.md`) | Dungeon difference |
|---|---|---|
| Room number pool, `Room` model, session `room`, `return_map/xy` | pvp-room-model, pvp-session-registry | `kind='dungeon'`, `mode=5`, adds `dungeon_ref`, `stage`, `run_t0`, per-stage monster table, script state. The pool widens to **1..196** in 2009 (C2S 0x1B range) [V] |
| List subscription | pvp-room-lists (2009: one C2S 0x2C with list_type) | type 5 → S2C 0xC2/0xC1 (extra `total_rooms`, `npc_index`) |
| Create / join / password / balloon | pvp-room-create, pvp-room-join, pvp-room-balloon | map_id field = template index; results add 9 "Already started." |
| Enter room (0x2F + 0x2E + 0x2C), leave (0x2D + 0x08) | pvp-room-enter-leave, pvp-warp-refactor (2009: 0x08 reason byte replaces 0x5E/0xA3/0xA4) | 0x2F adds `dungeon_ref`; team always 1; kick uses reason 2 |
| Team select → ready | pvp-team-select (0x1D → 0x35) | code 1 = ready (next_team 1); codes 0/2 ignored; 3 disabled |
| UDP host (input, snapshots, HP/MP/hit events, phases) | pvp-udp-host (P10), gate G2 | adds monsters (AI, spawn-by-script), stage map code switching, script timers, portal-Down detection; no teams/rounds |
| Result 0x36 | pvp-round-scoring | kind 5 branch (item reward / fail HP) instead of round counters |
| Codec issues | pvp-wsproto-fixes (assume flags) | 0x36 kind-5 conditions, 0x2E per-receiver pet block |
| Dungeon-only | none | 0xC1/0xC2, C2S 0x2D kick, C2S 0x95 start, S2C 0xA5, S2C 0xA7, script engine, entrance NPC spawn, reward pool |

---

## 6. Server state and persistence

**Room (in memory; never persisted)** [I]:

```
DungeonRoom(Room):
    dungeon_ref (187|197|204), title, password, max_players (1..5), room_level (tpl Lv),
    members [uid...] in join order (index 0 = master), ready {uid: bool},
    phase waiting|running|ended, stage (101..), run_t0, stage_t0, time_limit_s,
    scripts [ {row, fired} ] for the current stage,
    monsters {uid(33000000+n): {tpl, hp, state, x, y, script_row, appeared}},
    portal_open {row: (line, dest)}, at_portal {uid: t_last_down},
    udp: socket (server_ip, 10000+room_no), peers {uid: addr}
```

**Session**: `room`, `return_map`, `return_xy`, `list_sub` (add 'dungeon') [I].

**Persistence** [I]:
- The reward item goes through the normal inventory store; EXP/level through the normal character store.
- The client has no per-character dungeon counters: no clear count, cooldown or entry ticket strings or fields were found [V negative search of strings.tsv]. Entry limits are server policy (config).

**Content data** [I]:
- A generated `dungeons_2009.json` (or `en_content` extension) from the hni Quest-2 records and the 18 stage files: stages, script rows, monster rows with uid n, portal lines, event-3 start tiles, back-portal arrival tiles, time limit, reward pool.
- `en_maps.load_indun()` already parses the files, but it needs a SCRIPT_LISTS extractor.

---

## 7. Current server status (read only, 2026-09-27)

| Opcode | Today | Correct? |
|---|---|---|
| C2S 0x2C {5}/{0} | 0xC2 `{0,0}` empty / no reply (`_handle_room_list`) | yes (placeholder) |
| C2S 0x18 type 5 | registry refusal S2C 0x30 (arena text) | closes the modal; the text is wrong for dungeons (use `0x34 {2,5}` or `{6,5}`) |
| C2S 0x1A/0x1B/0x1C type 5 | `0x34 {2, 5}` "Invalid room #." | closes the modal (ok placeholder) |
| C2S 0x2D | consumed ("instance dungeon room kick") | fine until rooms exist |
| C2S 0x95 | consumed | fine until rooms exist |
| C2S 0x1D | no route in the 2008 or 2009 table (falls to the unhandled log) | must learn code 1 = ready and 4 = leave in dungeon rooms |
| C2S 0x15 in a room | `_cast_refusal` refuses casts on a map that "is a PvP room (room casts belong to the pvp group)" | must allow in dungeon rooms (the client allows skills there) |
| S2C 0xA5/0xA7/0xC1/0xC2 rows/0x36 kind 5 | not built (0xA5: `test_no_server_module_sends ... the old 0xA5`) | – |
| Entrance NPCs | not spawned | – |

---

## 8. Live test plan (two 2009 clients)

Setup [I]:
- Client A: `WindSlayer2009\play_2009.bat test test` (patched exe).
- Client B: the p2 exe (second UDP port, `patch_2009.py` option) with `admin admin`.
- Both need unique uids and levels >= 8: inject S2C 0x22 SetLevel or use a level-10+ character.
- `python wsdev.py --build 2009 …` for injections. Confirm the flag spelling with the tooling doc. Injections reach every live session, so stop B for single-client tests.
- Encode 0x36/0x2E with the per-receiver/assume rules.
- Take `wsview.py shot` after each step.
- After any 0x2F / 0xA5 / 0x08 injection that the server does not follow up, recover with `wsdev restart`.

### 8.1 Injection tests (single client, TCP only; runnable before any server code)

| # | Step | Expected [basis] |
|---|---|---|
| T1 | `sendspec C2 '{"total_rooms":2,"room_count":2,"repeat[room_count]":[{"room_name":"Forest run","room_no":1,"max_players":5,"cur_players":1,"room_level":8,"room_type":5,"npc_index":187,"has_password":0},{"room_name":"Locked","room_no":2,"max_players":3,"cur_players":1,"room_level":22,"room_type":5,"npc_index":204,"has_password":1}]}'` while 0x4C4 is open (needs G-ID1 or a debug way to open it) | rows "001 Forest run(01/05) Lv.8" enabled and "002 Locked(01/03) Lv.22" greyed for a Lv<22 char; counter "1/2" [V 0xC1 handler] |
| T2 | click row 1 | C2S 0x1A `01 00 05`, modal; today the server answers `0x34 {2,5}` → "Invalid room #." [V registry] |
| T3 | Create in 0x4C5 (Gang of forest, capacity 5, title "Run") | C2S 0x18 27 B ending `bb 00 05` (187 LE, type 5); byte 18 = 5; byte 19 = 1 [V builder] |
| T4 | `sendspec 2F '{"round_count":1,"round_time_min":12,"map_id":101,"host_ip":"127.0.0.1","room_no":1,"current_round":1,"room_mode":5,"room_title":"Run","dungeon_ref":187}'` then immediately `sendspec 2E` with A (team 1, next 0) | stage "Unknown Forest" loads (fade), window "Dungeon Party" shows " Room1 Run [Gang of forest]" and the description; A in slot 1 without a status; Start (master) and Leave enabled, Ready disabled for the master; the Statue is **not visible** (hidden 0x16); `wsview state` lists uid 33000000 (type 4). A stands at (0,0) (no snapshot) [V] |
| T5 | (after T4) `sendspec 2C` with "Rival" uid 2 team 1 next 0 | slot 2 "Rival Lv.N Waiting"; the master's kick buttons for slot 2 enabled; pressing Start prints "All have to be ready." and sends nothing [V FUN_004446c0, 0x44E1D6] |
| T6 | `sendspec 35 '{"uid":2,"team":1,"next_team":1}'` | slot 2 "Ready"; Start now sends C2S 0x95 [V] |
| T7 | click Start | C2S 0x95 (0 B), no modal [V] |
| T8 | click slot-2 kick → confirm | C2S 0x2D `00 02 00 00 00` [V] |
| T9 | Leave → Leave Now (Reserve Quit greyed) | C2S 0x1D `04` [V] |
| T10 | `udpsend 42907 8 <header: sec 0 | count 0<<10 | 101<<18>` (arch-udp-inject) | 0x4C6 closes, 0x47A opens, timer "12:00" [V 0x45E525; needs UDP framing] |
| T11 | `sendspec A7 '{"object_index":1}'` on stage 101 | portal marker sprite at (≈1455,≈802) + banner [V FUN_00406550/004063b0] |
| T12 | `sendspec A5 '{"stage_code":102}'` | fade, "Unknown Forest" stage 2 geometry; `wsview state` shows 8 hidden type-4 entities 33000000..33000007; players keep their entities [V] |
| T13 | `sendspec 36 '{"result_kind":5,"round_result":1,"dungeon_unused":0,"reward_item_id":1343,"reward_item_tail":0}'` | "You've received Old Gold Ring - Fire. (Count:1)", window "Rewards Dungeon" with the ring, effect, HP/MP full; the ring appears in the equip bag **on the client only** [V] |
| T14 | `sendspec 36 '{"result_kind":5,"round_result":0,"hp":10,"mp":0}'` | box "Failed to conquer the instant dungeon.", HP 10 [V] |
| T15 | `sendspec 08 '{"reason":2,"map_code":102,"game_time_ms":0}'` + restart | popup "You were kicked out.", then map change [V] |
| T16 | stage 106: `sendspec A7 '{"object_index":18}'` | the warning banner with alarm sound for 3 s [V renderer; sprite from indun_title.hsi #1] |

### 8.2 End-to-end (after the stages below)
1. **Stage D1-D2 (TCP rooms).** A clicks the entrance NPC, creates "Gang of forest". B sees the row, joins, presses Ready. A sees "Ready" and presses Start. The server logs 0x95 accepted. Kick and Leave Now return the player to the saved field position. The list updates for a third observer.
2. **Stage D3.** After Start, both HUDs switch (0x47A) and the timer counts. The server injects 0xA5 102/103 by GM command; both clients switch stage together. A GM `/indun clear` → both get 0x36 clear with (possibly different) items; the inventories persist after relog.
3. **Stage D4-D5 (simulation).** Both players move and see each other. The Statue appears at 3 s and breaks after hits. 10 s later the portal marker shows. Both press Down on it → stage 102. The party runs to 106, Rynx dies, the reward shows, and 10 s later both are back in Popola. A wipe run shows the failure box and returns both. A run left idle past 12 min fails on the timer.

---

## 9. Implementation plan (each stage testable)

| Stage | id | Depends on | Work | Exit test |
|---|---|---|---|---|
| D0 | indun-content | arch-content-loader (en_content/en_maps) | Extract the dungeon catalog (hni Quest 2: idx, name, Lv, first stage from +0x690, reward pool from `item:`), the stage tables (script rows, action-13 rows → uid n with the FUN_00423970 skip rules, portal lines via hsi floor lines, event-3 start tiles, back-portal arrival points, time limit). Data fix 304 → 305. Unit tests pinned to §1.2/§1.4 (e.g. stage 106 Rynx uid 33000027; stage 206 Monkey King 33000022) | `python -m unittest test_indun_content` |
| D1 | indun-entry | D0, world registry | Config-placed entrance NPCs spawned with S2C 0x1A (tpl 186/196/203). Decision gate **G-ID1**: does the click open 0x4C4 and send C2S 0x2C {5}? If not, fall back to a GM/chat command that opens the list through a world balloon (0x5B kind 5) join only, and record the finding | live: click NPC → C2S 0x2C 05 in `wsdev cap` |
| D2 | indun-rooms | P9 pvp-room-model, pvp-session-registry, pvp-room-lists, pvp-warp-refactor (MapTransfer with 0x08 reason) | DungeonRoom on the shared RoomManager. 0x2C{5} → 0xC2 + pushes. 0x18 → validation, 0x34{5,5} + 0x2F + 0x2E. 0x1A/0x1B/0x1C → 0x34 codes incl. 9. 0x2C to others. 0x1D{1} → 0x35. 0x2D kick → 0x08{2} + 0x2D. 0x1D{4}/disconnect → leave, master handoff. 0x95 validated and logged. Allow C2S 0x15 in dungeon rooms | §8.2 step 1 with two clients |
| D3 | indun-stage-tcp | D2, arch-udp-inject (P9 framing) | Minimal UDP phase driver per room (header-only opcode 7/8/9 datagrams at 1 Hz with the stage map code and the run seconds). 0x95 → phase 8. GM `/indun stage <code>` → 0xA5 to the room + monster table reset. `/indun trigger <row>` → 0xA7. `/indun clear|fail` → 0x36 kind 5 per member + inventory/EXP + return after 10 s. Timer with the 1023 s wrap handled (cap the display) | §8.2 step 2 |
| D4 | indun-sim | **G2 (shared with P10)** | The in-stage simulation. Option A: the faithful UDP host (pvp-udp-host) extended with monsters (AI from hni `AI:` flags, the ported client tick FUN_004185b0/FUN_00412c60/FUN_00414210) and player movement/collision on the stage's floor lines. Option B: a client patch that lets `InDun` maps run the mode-0 local simulation (the per-tick `map+0x7C` compares at 0x42CAF0/0x42CC37/0x42CCD0, the 0x0D gate, the portal gate 0x4311C6) and reuses the field combat model (61 B hit reports, S2C 0x2A/0x29). Under B the server spawns monsters with 0x1A instead of relying on the hidden pre-created ones. Spike both on stage 101 (one Statue) | both clients see each other move and can break the Statue |
| D5 | indun-script-engine | D3, D4 | Conditions 2/3/5/6, actions 13/14/15/16/17, the portal all-Down rule, stage advance, wipe/timeout fail, clear reward (boss death), party-share EXP, auto return | §8.2 step 3 full run of dungeon 1, then 2 and 3 |
| D6 | indun-hardening | D5 | Mid-run leaver/reconnect (no rejoin; 0x34 {9}), empty-room teardown, per-account cooldown (config), GM `/indun list|kick|close`, logging, the 17-minute wrap on 20-minute dungeons (e.g. shorten to 17 min or reset the header seconds per stage and accept a wrong HUD), and the Condition-5 visual gap on stages 2+ (Q7) | regression suite + a two-client soak |

---

## 10. Open questions

1. **G-ID1: entrance.** No EN 2009 map places NPC 186/196/203. Does a server 0x1A spawn of a type-0 `UI: 1220` template open 0x4C4? Where did retail put them (Popola for 186 per its Infor text; unknown for 196/203)?
2. **G2 (shared with P10): simulation.** Faithful UDP host (monster AI and collision written server-side) versus the mode-1/InDun local-simulation patch. For PvE the patch path reuses the field combat model and is much smaller. The cost: an exe patch set, and the hidden script-created monsters must be ignored or removed.
3. Exact meaning of `Weak_Atk_AI_ChkTime` 2/4/7 on the dungeon templates (minimum party size? stage count? difficulty?). The client never reads it for dungeon records.
4. Reward rule: one item per member from the template's 16-item `item:` pool (5000 = 0.5 %?), or always one? What does `reward_item_tail` (option word 5) mean for "Old …" items? The dead picker FUN_00424e30 sets it from table 0x525FCC `+0x45` on InDun maps on a 1-in-5000 counter.
5. What does retail put in the 0x36 `dungeon_unused` u32? The dead host puts the member's score = accumulated party-share EXP. Is EXP granted per kill (S2C 0x21) or at the end?
6. Time base of Condition 2/6: stage-relative (assumed) or run-relative? The client's own check compares against `gs+0xE40`, a session-relative clock, which looks wrong. Only the server's choice matters.
7. On stages entered by 0xA5 the client never binds Condition-5 rows (FUN_00423e30 runs only from 0x2F). Did retail enter every stage with 0x2F + 0x2E (a full reload), or did the boss slow-motion and reward-NPC marker simply not show after stage 1? Test by injecting 0x2F for stage 106.
8. Does anything need to happen when the reward window's OK is pressed (no C2S found)? Does retail return the party automatically, and after how long?
9. Window 0x4C6 control mapping. The code's event ids (0x13/0x17 kick, 0x1B Ready, 0x1C Leave, 0x1D Start) do not line up with the hui caption list (0x1D "Ready", 0x1E "Leave", 0x22 "Start"). Also, 0x4C4 ctrl 7 "Create" links to UI 1222 in the hui, not 1221. Capture screenshots and C2S traces to map buttons to ids.
10. Only roster slot 1 has a working kick path in the code. Is that a client bug or intended (2-player rooms)?
11. Stage 304's forward portal `value_num = 304`. Did the retail server hard-code the order? This design maps it to 305.
12. Arrival position after 0xA5: the back-portal tile (assumed), or the stage's first floor line?
13. The 20-minute dungeons and the 10-bit seconds field (1023 s). How did retail show 20:00? It may have reset the header seconds per stage.
14. Ground drops inside dungeons (the dead host's FUN_00424cb0 path, 1-in-5000 on InDun maps). Are S2C 0x11 ground items and C2S 0x1F pickups valid on mode-1 maps?
15. The lobby map `indun01_00` ("Dungeon Lobby", mode 0) and its `ConnInDunMapName`. Was it an alternative entry flow (a field-like lobby loaded by 0xA5 code 100) that EN never used?
