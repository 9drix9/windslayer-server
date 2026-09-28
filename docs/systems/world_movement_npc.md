# World, Movement & NPC Spawns — system design (`world_movement_npc`)

Scope: the 16 specs in `re_tools/corpus/systems/world_movement_npc.json`.

| Key | Dir | Name | Spec status |
|---|---|---|---|
| `0x42CE94/0x0D` | C2S | PlayerMoveState | partial |
| `0x42F76B/0x7E` | C2S | PortalEnterRequest | implemented |
| `0x44ADE3/0x5D` | C2S | VillageTransfer | missing |
| `0x03` | S2C | EnterWorldState | partial |
| `0x04` | S2C | PlayerAppear (count-prefixed list, no UI refresh) | missing |
| `0x05` | S2C | RemotePlayerAppear (single record) | missing |
| `0x06` | S2C | PlayerDespawn (remove entity by uid) | missing |
| `0x07` | S2C | EnterWorldPlayerList (list + enter-world UI refresh) | partial |
| `0x08` | S2C | ChangeMap | implemented |
| `0x1A` | S2C | NpcMonsterSpawn | implemented |
| `0x1B` | S2C | EntityActionCommand (queued command, mirrors C2S 0x0D) | missing |
| `0x2A` | S2C | RemoteEntityMoveStateSync (flush queue + target + position) | missing |
| `0x9E` | S2C | RemoteEntityMoveStateQueued (30 ms, clears +0x8E4) | missing |
| `0x9F` | S2C | RemoteEntityMoveStateQueued (990 ms, sets +0x8E4) | missing |
| `0x81` | S2C | VillageTransferResult | missing |
| `0xA5` | S2C | InstanceDungeonCreateResult | missing |

**Sources, in order of authority:**
1. Client binary evidence in the spec, plus `corpus/decomp` and `corpus/asm`.
2. `re_tools/LIVE_TEST_LOG.md`.
3. `server/windslayer_server.py`. Line numbers are for the 2026-09-17 file (2793 lines).
4. Memory notes.
5. PySlayer.

**New evidence gathered for this doc (all reproducible, read-only):**
- `re_tools/WindSlayer.exe` data tables at `0x70D07C`, `0x70D098`, `0x70D04C` and `0x70B8B4`, read through the PE section table.
- Decomp of `FUN_00422050` (fee), `FUN_0043dc40` (village dialog), `FUN_00412100` (move-queue consumer), `FUN_00413920` case 0x16 (template-entity self-revive), `FUN_00406170` / `FUN_00406df0` (portal line list) and `FUN_0042c450` (map reset).
- **EN** map files `WindSlayer2Game/hs/*.hmi`, decrypted with the mod-3 cipher (`+0xE9/+0xDE/+0xE0`). They are XML: 255 files, 251 of them `stageAA_BB`. The KR copies in `Desktop/hs_decrypted` were used only for comparison.
- `gamedef.sqlite3` `npcs.UI` and `maps`, plus `server/portals.json`.
- `server/wsproto.py`, run against the specs in a scratch script (it was not modified).

Cross-group work this design depends on. Each item is implemented once, in the group that owns it:
- `login_character` **lc-uid-online**: per-account uid.
- `combat_skill` **cs-monster-lifecycle**: 0x29/0x06/0x1A respawn.
- `item_inventory`: 0x03 item lists.
- `chat_mail_gm` **chat_mail_gm-system-notice**: 0x15 replaces the 0x0A welcome.
- `party` M1: uid.

---

## 1. How the client implements the system

### 1.1 Scene modes and what destroys what
`scene = *0x70EECC`. `scene+0xF00` holds the mode:

| Mode | Meaning |
|---|---|
| 5 | Character select |
| 7 | Map loaded, no local player yet |
| 6 | In world. Only S2C 0x07's post-loop path sets it (`FUN_004400c0(EDI=6)`). |

**S2C 0x03 and S2C 0x08 (and 0x5E/0xA3/0xA4/0x2F) each do a full map load.** In order:
1. Fade-out: 16 x `Sleep(40)` on the network thread, about 640 ms.
2. Load `./hs/stage%02u_%02u.hmi` from `map_code/100` and `map_code%100`. A missing file only logs `Could not load MAP.`.
3. `FUN_0042c450` resets the scene. It seeds the scene clock `scene+0xF1C` from the packet's `game_clock_ms` / `game_time_ms` and places a "you are here" marker on world-map window 10 and village window 600.
4. `FUN_00422f40` **destroys every entity, including the local player** (`scene+0x970 = 0`).
5. `FUN_004271e0` frees ground items.
6. `FUN_00444100` re-creates the map file's static NPC tiles (`event="2" NpcId=...`) client-side, with uids **33000000+**.
7. Mode is set to 7.

What each packet adds:
- **0x03** also loads the whole character sheet: gold, cumulative exp, fame, winnie, battle W/L/KO/Down, mentor, 3 active quest slots and progress, completed quests, 3 inventory tabs. It memsets the 4th (cash) tab and re-arms timer 6 (C2S 0x05 keepalive, 240 s). It then sends C2S 0x2F and C2S 0x63, both 0 bytes.
- **0x08** closes window 0x79 (death dialog), 0x236, any modal, and several PvP/stall windows. EN has no reason byte; the KR reasons became 0x5E/0xA3/0xA4.

**Nothing exists after a load until the server spawns it.** The following S2C 0x07 must carry a record with `uid == scene+0x220`, the `account_id` from S2C 0x02. `RegisterLocalPlayer` (0x422120) makes that record the local player when `scene+0xF24 == 0 && uid == scene+0x220`. The gate is **uid-only**, so any 0x04/0x05/0x07/0x1A record carrying the receiver's own uid re-points the local player.

### 1.2 Entity kinds

| Spawned by | `entity+0x98` | What | Notes |
|---|---|---|---|
| S2C 0x07 / 0x04 (u8 count + records) / 0x05 (one record) | 3 | players: local if uid matches, else remote | 0x07/0x04 record: 368 B + optionals. 0x05: 367 B, no shop tail, buff entries always 6 B. |
| S2C 0x1A | 4 | template entities (monsters, server NPCs) | `template_index` (1-based) binds name, stats and sprite from list `game_state+0x4E0`. `server_controlled` (+0x8E4) switches off client wander AI. |
| map file (`FUN_00444100`) | — | town NPCs, uids 33000000+ | Talking and quest offers are client-local. `gamedef.npcs.UI` is the client window the NPC opens (§1.6). |

**No handler de-duplicates uids.** Every 0x04/0x05/0x07/0x1A allocates a new 0x15F0-byte entity. Removal happens through S2C 0x06 (`FUN_00422b90`: any entity with `+0x84 == uid`) or a map load.

**Template entities revive themselves.** In `FUN_00413920` case 0x16 (decomp :962-983), a `+0x98 == 4` entity whose `+0xE5C - scene_clock < 0` is moved to `(+0x1318, +0x1320)` with full HP and MP. S2C 0x29 writes those three values. The local player (type 3) is never revived this way in field mode.

### 1.3 uid space (must not collide)

| Range | Owner |
|---|---|
| 0 | invalid (lookups fail silently) |
| 1 .. 0x000EFFFF | player characters. Must equal the 0x02 `account_id` of that session (see lc-uid-online). |
| 0x000F0000 .. 0x001FFFFF | server template entities (`MOB_UID_BASE`, :138) |
| 0x00200000 .. | ground items (`GROUND_ITEM_UID_BASE`, :140) |
| 33000000 (0x01F78A40) .. | client-local map NPCs. Never assign. |

### 1.4 Movement model
The client is authoritative for its own physics. The server only relays **input state**.

**C2S 0x0D** (18..83 B) is evaluated every 30 ms logic tick. Gates: mode 6, not a P2P room (`[0x70EED8]+0x70 == 0`), local player present. Send rules:
1. Send immediately if any of `+0x8B3..+0x8B6` changed, or `+0x950 != 0`, or `+0x94C != 0`.
2. Otherwise send nothing if less than 210 ms have passed since the last send.
3. Not idle (idle = `+0x904 == 8 && +0x8B3 == 8 && +0x8B4..+0x8B6 == 0`): send about every 210 ms.
4. On becoming idle: one final packet.
5. Idle: a keepalive every 15 000 ms.

Fields:
- `realtime_delta_ms`: on every 10th send, the real time spanned by the last 10 sends (a speed-hack check); otherwise 0.
- `map_code`.
- `logic_elapsed_ms`: a multiple of 30, measured since the last send.
- The 8-byte state blob.
- Tails: `ae != 0` adds `u32 event_source_uid`, plus `f64,f64` when `vb == 4` and `ae` is 7 or 9. `ie != 0` adds a 43/45-byte interaction tail. **Position is only in the `ie` tail.**

The scene clock can be tracked exactly per client: `clock = last 0x03/0x08 game_time + sum(logic_elapsed_ms since)`.

Shared 8-byte state blob (C2S 0x0D, S2C 0x1B/0x2A/0x9E/0x9F). Little-endian, `lo = u32(bytes 0-3)`, `hi = u32(bytes 4-7)`:

| Bits | Entity field | Meaning / values |
|---|---|---|
| lo 0-1 | +0x8B3 | horizontal input. Wire 1 = 2 (left), 2 = 6 (right), 0/3 = 8 (neutral). |
| lo 2-4 | +0x8B4 | vertical/stance. 3 = jump (live capture `0x7240000C`); 4 = the value the portal gate requires. |
| lo 5-8 | +0x8B5 | sub-state (applied to players only) |
| lo 9-11 (`vb`) | +0x8B6 | sub-state. 4 with action 7/9 adds the target-point tail. |
| lo 12-15 (`ae`) | +0x94C | action event: 0 none, 1-5 attacks, 6-10 grab/throw, 0xD death |
| lo 16-19 (`ie`) | +0x950 / +0x8EA | interaction/reaction: 1, 9, 0xB, 0xC seen |
| lo 20-21 | +0x8D0 | action facing: 1 = 2, 2 = 6, else 8 |
| lo 22-31, hi 12-31 | — | stale garbage from the shared scratch buffer `scene+0xEF8`. **Mask it off.** |
| hi 0-11 | +0x958 | action timer/param (50/250/600 seen) |

**Remote-entity command queue** (verified in the `FUN_00412100` decomp). Each entity has a FIFO of 0x58-byte nodes at `entity+0x1350`. Each tick it is consumed for entities with `+0x98 > 2` and `uid != scene+0x220`, and only while `[scene+0xF88]+0x70 == 0`.
- **Players (type 3):**
  - If `+0xE48 == 0` and a node is queued: `+0xE48 = node[0]` (or 30 if 0).
  - On later ticks `+0xE48 -= 30`; an idle entity drops it straight to 0.
  - At 0 the head node is applied and popped. So `node[0]` is a **delay counted from the previous command**, cut short when the entity is idle.
  - Applied fields: `8B3/8B4/8B5/8B6`, `+0x8EA` (or `+0x950` in a room), `D78`, `1328/1330`, `8DB`, `8E8`, `1338/1340`, `8D4`, `8E7`. If `node.ie != 0` the position also comes from `node+0x10/+0x18`.
- **Monsters (type 4):** a node is applied as soon as `+0xE48` is 0; then `+0xE48 = node[0]` (the action duration). Only `8B3/8B4` and the common fields are applied. **Position is never taken from a node.**
- **Common fields, all types:** `+0x94C = action`, `+0xD80 = target`, `+0x958`, `+0x8FF`, `+0x8D0`.

| Opcode | Size | Queue | `node[0]` | Target | Position | +0x8E4 |
|---|---|---|---|---|---|---|
| **0x1B** | 16..52 | append | `hold_ms` from the wire | `u32` if `ae != 0`; `f64 x,y` if `vb == 4` and `ae` is 7/9 | `f64 x,y` if `ie != 0`, applied on dequeue (players only) | unchanged |
| **0x2A** | 16/17/33/34 | **flush**, then append | 960 (adjusted when the target is the receiver) | always `u32`, plus `u8` if `ae` is 9/10 | `f64 x,y,bool airborne` written **immediately** to any entity type. Read only when the mover is not dead (state 0x10) and target != receiver. | set 1 |
| **0x9E** | 12 | append | 30 | none | none | **cleared** |
| **0x9F** | 12 | append | 990 | none | none | set 1 |

For monsters, `+0x8E4` is the `server_controlled` flag, and the client wander AI (`FUN_00417e10`) only runs while it is 0. So **0x2A/0x9F switch a monster's client wander off, and 0x9E switches it back on** (inferred; T-2A-3).

**Relay-opcode decision.** The specs disagree: the 0x0D and 0x1B specs name 0x1B as the relay, while the 0x2A spec calls 0x2A "the correct relay". The field mapping settles it for plain relaying. 0x1B's optional tails are exactly 0x0D's, under the same bit conditions, and land on the same entity fields:

| C2S 0x0D field | S2C 0x1B field | Entity |
|---|---|---|
| `logic_elapsed_ms` | `hold_ms` | queue delay since the previous command (reproduces the sender's timeline) |
| `event_source_uid` | `target_uid` | +0xD80 |
| `f64_1338/f64_1340` | `target_x/target_y` | +0x1338/+0x1340 |
| `ie` tail `pos_x/pos_y` | `pos_x/pos_y` | +0x11F8/+0x1288 |

The other `ie`-tail fields (`target_uid +0xD78`, `target_dx/dy`, `flag_8db`, `flag_8e7`, `timer_dac`, `target_action_event`, `item_id`) have **no carrier in any EN TCP S2C**. The queue node has slots for them, but they are only filled on the P2P path.

0x9E/0x9F carry no position, and a nonzero `ie` nibble snaps the entity to (0,0) (node x/y stay 0). **Design: relay 0x0D as 0x1B. Use 0x2A for server-authoritative keyframes, 0x9E for "stop now".** Confirm live with T-1B-1 and T-1B-3.

### 1.5 Portals (C2S 0x7E) and the EN map data

**Client algorithm.**
1. At map load, `FUN_00406170` builds `[map]+0x58`, a list of collision lines. It walks layers in XML order, then each layer's tiles in XML order, then every line of the tile's sprite (`CSpriteControl::GetLineCount/GetLineInfor` on the `.hsi`).
2. Each entry stores the line type and absolute `x1,y1,x2,y2` (sprite line + tile `pos_x/pos_y`), plus the tile's `event` and `value_num` XML attributes.
3. Every tick `FUN_0042cf20` checks the gates:
   - mode 6
   - not a P2P room
   - `player+0x8B4 == 4`
   - `player+0x904` is 8 or 0xC
   - `now > app+0x470 + 2500`; the cooldown is stamped whenever these gates pass
4. It then calls `FUN_00406df0`, which returns the 0-based index of the first entry with `type == 0 && event != 0 && y1 == trunc(y) && x1 <= trunc(x) <= x2`.
5. If that entry's `event == 1`, the client sends **`u32 index`**. `value_num` is read but **not sent**.
6. Latent client bug: when no line matches, it tests an uninitialised stack slot and can send index **0**.

**Map files carry the destination.** Portal tiles are `<Tile ... event="1" value_num="<dest map code>"/>`; NPC tiles are `event="2" NpcId=".."`. EN `stage01_01`: one portal tile at (1355,802) with `value_num=102`, and the live index is 23, so every line before it counts. **Consequence:** the server can compute `(map, index) -> destination` from the EN `.hmi` plus `.hsi` line counts, instead of the KR `gamedef.maps` table.

**The KR portal table does not match the EN world** (scratch analysis of all 251 EN stage maps against `portals.json`):
- The EN maps have **594 portal (map, destination) pairs**. Only **357** appear in `portals.json`, so **237 EN portals are silently ignored** by `_handle_change_map` :1263-1270.
  - Examples: every EN town has a portal to 9701 (flea market), none in the table; `208->418`; `201->108`.
- **228** table pairs lead to destinations the EN map does not have.
- Of the 580 well-formed rows, only **222** arrival points sit next to the EN reverse portal. For example, `202_6 -> 201 @ (895,1406)`, but EN 201's portal to 202 is at tile (1100,1200). **132** rows are inconsistent and **226** have no reverse portal in EN.
- **20 keys are malformed**: `None_None`, `_`, `1002_None`, and `1003_28`/`1008_1`/... whose destination is `null`. That leaves **569** usable keys, not 589.

**Arrival rule** (derived, no client code involved). The KR `gamedef.maps` arrival point for A->B sits next to B's reverse portal tile (the one with `value_num == A`): 450 of 580 KR rows match.
- `dy` is -73..-100 from the tile `pos_y`: -80 (130 rows), -85 (88), -91 (55), -88 (52).
- `dx` is +46..+65: mostly 50-55.
- The offset depends on the portal sprite (hsi, sprite).
- Checks: 101<-102 = tile (1355,802) -> (1411,714); 102<-101 = tile (0,800) -> (48,713).
- The local player currently spawns at `(100,100)` from `accounts.json` on map 101 and is still visible and controllable (0x07 at :750 uses `char['x'/'y']`). Spawning above the floor therefore settles by gravity, at least for the local player (T-05-3 checks remote records).

### 1.6 NPCs and the village transfer (C2S 0x5D / S2C 0x81)

**NPC windows.** `gamedef.npcs.UI` is the client window an NPC opens when talked to. The cross-checks agree:

| UI | Window | NPCs |
|---|---|---|
| 13 | shop | 57 NPCs |
| 42 | 0x2A arena list | idx 146 Arena Keeper Yeoppo |
| 1144 | 0x478 play-room list | idx 171 Playroom Keeper Nori |
| 600 | 0x258 village transfer | **idx 117 Garan Maria** |
| 662 / 663 | gathering | ore / herb nodes |
| 664-666 | refine / alchemy / reinforce | crafting stations |
| 1220 | ? | idx 186 Hidden Forest Clearing, 196 Mt. Hwangcheon Entrance, 203 Crater Transport Machine, 286 Dungeon Ana Johnson, 317 Hidden Forest (probable map-transport / instance-dungeon NPCs; Q10) |

**Garan Maria (NpcId 117) positions in the EN map files** (tile `pos_x, pos_y`):

| Map | Position |
|---|---|
| 501 | (2115,645) |
| 601 | (1800,900) |
| 701 | (3785,433) |
| 801 | (500,2200) |
| 901 | (3700,654) |
| 1001 | (1000,300) |
| 1101 | (2100,1100) |

She is **not** in EN 101/201/401; the KR maps also place her in 201 and 401.

**Client flow.**
1. Window 600 lists destinations; controls 3..15 give `village_index = control - 2`. `FUN_0043dc40` checks:
   - index > 10: "You are unable to transfer to the area."
   - `fee = FUN_00422050(index)`; fee 0: "You can't move to where you already are." and no dialog.
   - `scene+0xEE0` (manner) >= 500: `fee = (u16)(fee * 0.9)`. Every table value is a multiple of 10, so rounding mode does not matter.
   - It opens confirm window 0x237 with the name and fee.
2. Confirm (control 7) checks `u64 gold >= fee` ("You are short of gold."), then sends **`u8 village_index, u16 fee`** (the discounted fee). There is no wait dialog.
3. S2C 0x81 `u8 result [u64 gold]`:
   - `result == 1`: sets **absolute** gold (`scene+0x270`), updates window 9 control 0xB and plays sound 0x29.
   - Anything else: "Village transfer failed. Please try again in a few minutes." (1 byte).
   - **It does not move the player.**

**Tables** (read from `re_tools/WindSlayer.exe`, u16 arrays):

| VA | Content |
|---|---|
| `0x70D07C` fee by distance | `[0, 1000, 1940, 2840, 3700, 4520, 5300, 6040, 6740, 7400, 8020, 8600, 9140]` |
| `0x70D098` travel order | `[0, 1, 2, 3, 4, 5, 5, 6, 7, 8, 9]` |
| `0x70D04C` village -> map group (`map_code // 100`) | `[0, 1, 2, 4, 7, 5, 8, 6, 9, 10, 11]` (same as `0x70B9AE`, the region warp stone table) |
| `0x70B8B4` names | 0 "Nearest Village", 1 "The Beginning of the Adventure", 2 "Popola Village", 3 "Ozzi Village", 4 "Balderan", 5 "Amakusa", 6 "Mining Settlement", 7 "Atajokuna", 8 "Serien, the City of Water", 9 "Serien Underworld", 10 "Underwater City RA" |

**`FUN_00422050(dest)`** (asm 0x422050-0x422111):
1. `g = map_code // 100`. If `GROUP[dest] == g`, return 0.
2. `cur` = the first i in 1..10 with `GROUP[i] == g`. None found: return 0. So **only maps in groups 1, 2, 4, 5, 6, 7, 8, 9, 10, 11 can use the transfer.**
3. `d = |ORDER[cur] - ORDER[dest]|`. If `d == 0` (Amakusa <-> Mining Settlement), return `FEE[1]` = 1000, with **no** crossing bonus.
4. If `(cur == 5 && dest > 5) || (dest == 5 && cur > 5)`, then `d += 1`.
5. Return `FEE[d]`.

The fee depends only on the map **group**, so it applies from field maps too (102 -> Popola = 1000). Base fees, before the manner discount:

| cur \ dest | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 (grp 1) | 0 | 1000 | 1940 | 2840 | 3700 | 3700 | 4520 | 5300 | 6040 | 6740 |
| 2 (grp 2) | 1000 | 0 | 1000 | 1940 | 2840 | 2840 | 3700 | 4520 | 5300 | 6040 |
| 3 (grp 4) | 1940 | 1000 | 0 | 1000 | 1940 | 1940 | 2840 | 3700 | 4520 | 5300 |
| 4 (grp 7) | 2840 | 1940 | 1000 | 0 | 1000 | 1000 | 1940 | 2840 | 3700 | 4520 |
| 5 (grp 5) | 3700 | 2840 | 1940 | 1000 | 0 | 1000 | 1940 | 2840 | 3700 | 4520 |
| 6 (grp 8) | 3700 | 2840 | 1940 | 1000 | 1000 | 0 | 1000 | 1940 | 2840 | 3700 |
| 7 (grp 6) | 4520 | 3700 | 2840 | 1940 | 1940 | 1000 | 0 | 1000 | 1940 | 2840 |
| 8 (grp 9) | 5300 | 4520 | 3700 | 2840 | 2840 | 1940 | 1000 | 0 | 1000 | 1940 |
| 9 (grp 10) | 6040 | 5300 | 4520 | 3700 | 3700 | 2840 | 1940 | 1000 | 0 | 1000 |
| 10 (grp 11) | 6740 | 6040 | 5300 | 4520 | 4520 | 3700 | 2840 | 1940 | 1000 | 0 |

With manner >= 500 the fees become 900, 1746, 2556, 3330, 4068, 4770, 5436, 6066, 6660 and 7218. The maximum is 8020 (unreachable with d <= 10), so the fee fits in a u16.

Destination town per village: `TOWN = {1:101, 2:201, 3:401, 4:701, 5:501, 6:801, 7:601, 8:901, 9:1001, 10:1101}`. Arrival points are not in the client. Use the §1.5 arrival rule next to the town's Garan Maria or main entrance, and verify live (T-5D-3).

### 1.7 Instance dungeon result (S2C 0xA5)
`u8 result, u32 unused`:
- 1: modal "Moving into Instance Dungeon".
- 2: "Failed to create an Instance Dungeon."
- Other values: nothing.

It performs no map change. The C2S request is unidentified. The EN map loader (`FUN_00407190`) parses `MapInfo` attributes `InDunWaitingMap` (+0x2D0), `ConnInDunMapName` (+0x2D8, converted to a map code at +0x3DC) and `InDun` (+0x2D4), and the tile attributes `ActPrtlAprTileIndx`, `ActPrtlAprConnMap` and `InDunRwdNpcIndx`. **None of the 255 EN `.hmi` files use them**, so EN instance dungeons are probably dormant content.

---

## 2. Request/response flows

Notation: `->` client to server; `<=` server to that same client; `<~` server to the *other* in-world sessions on the same map, encoded in **each target's** mode. Live clients send everything with `no_enc=True`, so `use_by_array=True` today.

### F1. Enter world (C2S 0x2B -> 0x03, 0x07, ...)
1. `-> 0x2B` = `u8 x5 refuse flags, str[16] p2p_ip, u32 p2p_port, str[17] char_name` (login group). Client gate: mode 5.
2. Server: find the character by name. If it is not found, log and stop, as :715-717 does; the client's 5.5 s timer then fires.
   - Load persisted `map, x, y, hp, mp, gold, exp, level, fame, winnie, manner, stats, inventory, quests`.
   - `session.uid = session.account_id` (lc-uid-online).
   - `session.current_map = char.map` (default 101), `session.pos = (char.x, char.y)`, `session.in_world = False`, `session.no_enc` = mode of this packet.
3. `<= 0x03` (64 B + lists):

   | Field | Value |
   |---|---|
   | `channel_id` | 1 |
   | `map_code` | **`session.current_map`** (B3) |
   | `game_clock_ms` | 1000 (remember as `session.clock0`) |
   | `gold` | `session.gold` |
   | `exp_total` | `session.exp` (cumulative) |
   | `fame_points`, `winnie_points`, `battle_*` | from the character |
   | `mentor_id` | 0 |
   | `unk_e78` | 0 |
   | `active_quest_id[3]` / `active_quest_progress[3]` | from `session.quest_slot` / progress |
   | tab capacities | 35/35/35 (range 1..45) |
   | completed quests | at most 85 |
   | equip/consume/etc lists | item_inventory model; at most 45 each, contiguous ids, `option_count <= 5` |

4. `<= 0x07` = count 1, own record (§3.4): idle defaults, real stats, level, `cur_hp/cur_mp` and equip grid. The client registers the local player, sets mode 6 and refreshes the UI.
5. `<= 0x04`: other in-world players on this map, **at most 5 records per packet** (payload limit 2038 B = 0x7FF - 8 header - 1 opcode; 368 B each). Never include the receiver's uid.
6. `<= 0x59` for each active quest that is ready (0x03 zeroes the ready flags; quest group).
7. `<= 0x15` welcome (first entry only; chat group), `<= 0x28 hp`, `<= 0x44 mp`. These are kept even though 0x07 now carries HP/MP, because this is the proven path.
8. `<= 0x1A`: every live template entity of the map instance, with **current** position and HP. At most `floor(2037 / (82 + 6*effects + 4*server_controlled))` per packet: 24 plain, 23 with one effect.
9. Set `in_world = True`. Then `<~ 0x05` (own record) to peers. Use `0x04` with count 1 instead when a shop sign or a buff id 0xA31..0xA3B must be carried.
10. Client: `-> 0x2F` and `-> 0x63` (0 B). Do **not** answer 0x63 with an empty S2C 0x63 (B21).

### F2. Local movement (C2S 0x0D -> relay 0x1B)
1. `-> 0x0D`. Client gates are in §1.4.
2. Server drops the packet silently when:
   - `not in_world`
   - `Grammar(0x0D).decode()` fails (requires the B1 fix)
   - `map_code != session.current_map` (a stale packet from before a transfer)
3. Server state:
   - `lo &= 0x003FFFFF`, `hi &= 0xFFF`
   - `session.move = (lo, hi, now)`
   - `session.clock += logic_elapsed_ms`
   - if `ie`: `session.pos = (pos_x, pos_y)`
   - Optional speed check: when `realtime_delta_ms != 0`, compare it with the sum of the last 10 `logic_elapsed_ms`; flag when logic > 1.25 x real (P3).
4. **Never reply to the sender.** The old echo desynced the cipher.
5. `<~ 0x1B` = `{uid: session.uid, hold_ms: min(logic_elapsed_ms, 990), state_blob: pack('<II', lo, hi), [target_uid: event_source_uid], [target_x/y: f64_1338/1340], [pos_x/y]}`.
   - Tails follow `ae`/`vb`/`ie` exactly (16/20/36/32/52 B).
   - Recipients: `in_world`, same map, `uid != session.uid`.
   - Optional: skip pure idle keepalives (same blob, no tails, `logic_elapsed_ms >= 15000`).
6. A recipient that has not yet created the entity (0x05 still in flight) consumes 4 bytes and ignores the rest. This is safe.

### F3. Portal (C2S 0x7E)
1. `-> 0x7E u32 portal_line_index`. Client gates are in §1.5.
2. Refusals. The server sends nothing and the player stays put:
   - `not in_world` (a transfer is in progress)
   - less than 2.0 s since the last accepted transfer
   - `(current_map, index)` not in the **EN** portal table (`portals_en.json`, §3.6). Until that table exists, fall back to `portals.json` with malformed keys dropped.
   - destination == current map (none exist; keep the guard, :1272)
   - index 0 on a map whose table has no genuine index-0 portal (client bug, §1.5)
   - *(P2)* last known position more than ~200 px from that portal's line, using the parsed `.hsi` lines
3. Accept: `MapTransfer(session, dest, arrival_x, arrival_y, reason='portal')` (F7).

### F4. Leave map / disconnect
1. Triggers: socket closed (`_handle_fireway` finally, :578-581), logout, or the start of any `MapTransfer`.
2. Remove the session from `MapInstance.sessions`; set `in_world = False`.
3. `<~ 0x06 u32 uid` to the remaining in-world sessions of that map. **Never** send it to the leaving client: its own uid leaves `scene+0x970`/`+0xEE8` dangling and crashes it.
4. Persist `map, pos, hp, mp, gold, exp, level, inventory, quests`.

### F5. Village transfer (C2S 0x5D -> 0x81 + MapTransfer)
1. The player talks to Garan Maria (hold Space next to her), which opens window 600. Picking a destination opens 0x237; OK sends `-> 0x5D u8 village_index, u16 fee`.
   - Client gates: index <= 10, fee != 0 (so the current map group is a village group and dest != cur), gold >= fee.
2. Server validation. Each failure answers `<= 0x81 u8 0` (1 B):
   - `in_world`
   - `1 <= village_index <= 10`
   - `fee_srv = village_fee(current_map, village_index, char.manner)` (the §1.6 algorithm) is not 0
   - `session.gold >= fee_srv`
   - not within 10 s of the last transfer (matches the "try again in a few minutes" text)
   - If `fee != fee_srv`, log a warning and **charge `fee_srv`**.
3. Accept:
   - `session.gold -= fee_srv`
   - `<= 0x81 u8 1, u64 session.gold` (9 B; absolute)
   - `MapTransfer(session, TOWN[idx], *TOWN_ARRIVAL[idx], reason='village')`

### F6. Template entities (monsters / server NPCs)
1. The map instance is created when its first player enters. `MapInstance.entities` is built from `map_spawns.json`: `uid = 0x000F0000 + map-unique counter`, `template_index`, `npcs` row stats, home x/y.
2. Enter: F1 step 8.
3. Death (combat group, cs-monster-lifecycle):
   - `<= / <~ 0x29 {uid, respawn_tick: 0x7FFFFFFF, respawn_x/y: home}`. This stops the §1.2 self-revive.
   - After about 3 s, `0x06 uid` to all in-world sessions on the map.
4. Respawn timer fires, from a periodic ticker (not only from 0x38/0x25, see B11): `0x1A` with the same uid, `cur_hp = max`, `pos = home`, sent to every in-world session on the map.
5. A map instance with no players keeps entity state for N minutes before being discarded. This prevents the kill -> leave -> return free-respawn exploit.

### F7. `MapTransfer(session, map_code, x, y, reason)` (shared primitive)
Used by: portal (F3), village (F5), death revive C2S 0x2E (combat), warp stones C2S 0x70/0x71 (premium_cash), GM `/go` C2S 0x06 (chat_mail_gm), instance dungeons (F10), PvP return 0x5E/0xA3/0xA4 (pvp_arena).
1. F4 steps 2-3 on the old map.
2. `session.current_map = map_code`, `session.pos = (x, y)`, persist.
3. `<= 0x08 {map_code, game_time_ms: 1000}` (**6 B**). It closes the death dialog, modals and the stall. For PvP returns use 0x5E/0xA3/0xA4 with the same body. Omit it only for `reason == 'enter_world'` (0x08 during enter-world caused a reload loop, :754-760).
4. F1 steps 3-9 on the new map, using the **current** `hp/mp` and no welcome line.
5. Enable `in_world` only after 0x07 has been sent.

### F8. Server-originated remote-entity state (0x2A / 0x9E / 0x9F)
- **Keyframe** (a GM moves a player, the server corrects a position, a monster AI drift fix):
  - `<~ 0x2A {mover_uid, move_bits: pack(lo, hi), target_uid: 0, [action_flag], pos_x, pos_y, airborne: 0}`
  - Always append the 17-byte position block (33/34 B); nothing after it is read.
  - Never send it to the mover itself (teleport plus a node leak). Keep `ie = 0`.
- **Stop now:** `<~ 0x9E {mover_uid, move_bits: pack(0, 0)}`. Neutral input applies within one tick. For a monster it also re-enables client wander (T-2A-3).
- **Buffered state:** `<~ 0x9F {mover_uid, move_bits}`. Applies when the entity is idle, or within 990 ms. Unused in phase 1.

### F9. Server-driven monster movement (P2)
1. Spawn with `server_controlled = 1, cmd_hold_ms = 0`. The client wander AI stays off for that entity.
2. AI tick: pick an action and send `<~ 0x1B {uid, hold_ms: duration, state_blob: pack(facing_wire | motion << 2, 0)}`.
   - Monsters ignore `pos` in 0x1B, so movement comes from motion + facing + client physics.
   - Walk = facing wire 1/2 with motion 1 (to be confirmed by T-1B-2).
3. Integrate `x += dir * speed * dt` on the server, with speed measured in T-1B-2 and clamped to the 600 px leash (`FUN_00417e10`). Every ~5 s, or on drift, send a 0x2A keyframe with the server position.

### F10. Instance dungeon (P3; request unknown)
1. `-> ???` (not identified; Q10).
2. Server: create the instance, then `<= 0xA5 {result: 1, unk_instance: map_code}` (the u32 is ignored by EN), then `MapTransfer(..., reason='instance')`.
3. Failure: `<= 0xA5 {result: 2, unk_instance: 0}`.

---

## 3. Server state and data model

### 3.1 World registry (new module `server/world.py`)
```python
class MapInstance:
    map_code: int
    sessions: dict[int, dict]      # uid -> session (in_world, current_map == map_code)
    entities: dict[int, Monster]   # template entities shared by all players on the map
    next_uid: int                  # 0x000F0000-based, unique per server
    empty_since: float | None

class World:
    maps: dict[int, MapInstance]
    by_uid: dict[int, dict]
    lock: threading.RLock                        # every registry mutation and broadcast iteration
    def map(self, code) -> MapInstance
    def peers(self, s) -> list[dict]             # in_world, same map, uid != s['uid']
    def send(self, s, opcode, payload)           # _send_encrypted(s['sock'], s, op, payload, use_by_array=s['no_enc'])
    def broadcast(self, s, opcode, payload)      # send() to peers(s); never to s
```
`_send_encrypted` (:583-599) already serialises the cipher per session with `send_lock`, so cross-thread sends are safe. A slow receiver blocks the sender's receive thread in `sendall`, so add a per-session outbound queue and writer thread (P2). Remove the `time.sleep(0.05)` calls from the transfer path (:749, :764, :777, :786, :1297-1317). Each one blocks that session's receive loop.

### 3.2 Session fields (added or changed)

| Field | Type | Use |
|---|---|---|
| `uid` | int | = `account_id` (lc-uid-online), replacing the hardcoded 1 (:2656) |
| `no_enc` | bool | encoding mode of the last packet received. Used for every send **to** this session, including broadcasts from other threads. |
| `current_map` | int | single source of truth: 0x03 `map_code`, portal table, 0x0D `map_code` check, MapInstance membership |
| `in_world` | bool | False from the start of a transfer until 0x07 is sent. Gates relays to and from the session. |
| `pos` | (float, float) | last known: arrival point, 0x0D `ie` tail, *(dev)* memory reader |
| `move` | (lo, hi, t) | last relayed blob: idle detection, keepalive suppression, 0x04/0x05 input bytes |
| `clock` | int | estimated `scene+0xF1C` = seed + sum(`logic_elapsed_ms`) |
| `last_transfer_t` | float | portal/village cooldown |
| `hp, mp, max_hp, max_mp` | int | current values go into 0x07 and 0x28/0x44 |
| `gold, exp, level, fame, winnie, manner, battle{w,l,ko,down}` | int | 0x03 / 0x07 / 0x05; one wallet |
| `inventory, equip_grid, quests, quest_slot, quest_progress, quests_done` | existing | 0x03 lists (item_inventory, quest groups) |

### 3.3 Persistence (`accounts.json`)
- Character fields: `map, x, y, hp, mp, gold, exp, level, fame, winnie, manner, str/dex/int/spr, inventory, equip_grid, quests, quest_slot, quest_progress, quests_done`.
- Save on MapTransfer, disconnect, level-up and every 60 s. Today `_save_accounts` runs only for character creation (:2612) and the default file (:402).
- New characters get `map 101, x 1411, y 714` (a verified 101 arrival point) instead of `map 0, x/y 100` (:2605-2606, :392-393).

### 3.4 Player record (0x07 / 0x04 / 0x05): one builder, three grammars
`player_record(session, char, remote: bool)` returns a dict encoded with the 0x07/0x04 grammar (inside `repeat[player_count]`) or the 0x05 grammar. Key names differ per grammar (e.g. `karma` vs `manner_points`, `action_state_904` vs `motion_id`), so keep a key map.

| Field (0x07 name) | Value | Sent today by `_build_en_opcode_07` |
|---|---|---|
| `karma` | char.manner (0) | `i32(1)` :2308 |
| `room_id` / `gm_level` | 0 / 0 (GM: 1 + `gm_hidden`) | 0 / 0 |
| `job`, `job2`, `level`, `rank_icon`, `gender` | class, job2, level, 99 (no icon) or fame rank, account gender | class, 0, level, **0**, 0 (:2325-2329) |
| `appearance_part[14]` | char look (hair 1, face 2, top 4, bottom 5, shoes 6, weapon 11) | same (:2332-2346) |
| `stat_str/dex/int/tol` (+0xE6..+0xEC) | char str/dex/int/spr | **0, 0, 0, 0** (:2349) |
| equip 16 x (id + 6 attr), cash 9 | `session.equip_grid` | same (:2354-2362) |
| `buff_count`, `skill_count` | active buffs; learned skills (<= 30) | 0, 0 (:2364-2365, labelled "inventory/skill") |
| `+0x954, +0x8D9, +0x8CF, +0x904, +0xE00, +0x8BD` | **0, 0, 8, 8, 0, 2** | 32, 0, 1, 0, 501, 0 (:2367-2372) |
| `pos_x, pos_y` | `session.pos` | `char['x'/'y']` (:2374-2375) |
| `+0x8B3, +0x8B4, +0x8B5, +0x8B6` | **8, 0, 0, 0**; for a remote record, the decoded `session.move` | **127, 0, 0, 1** (IP octets, :2378-2387) |
| 5 bools, `+0xD94`, `+0x8EC` | 0 | 0 |
| `cur_hp`, `cur_mp` | `session.hp`, `session.mp` (never 0 for a live player) | **0, 0** (:2391-2392) |
| `shop_open` | 0; or 1 + sprite + title for an open stall (shop_storage) | 0 |

### 3.5 Template entity (0x1A) defaults

| Field | Value | Sent today by `_build_opcode_1A` |
|---|---|---|
| `template_index` | from `npc_templates.json` (§3.6) | npccode (:1839, no `anim` key in MONSTER_DB) |
| `effect_count` | 0 (pending T-1A-1), otherwise real status effects | 1 x effect **0x0B3B** (:1574-1576) |
| `home_x/home_y` | spawn point (u32) | spawn |
| `respawn_tick` | 0 | 0 |
| `unk_954, unk_8d9, unk_8cf, action_state, action_elapsed_ms, unk_8bd` | **0, 0, 8, 8, 0, 2** | 32, 0, 1, **0**, 501, 0 (:1584-1589) |
| `facing, motion, motion_8b5, motion_8b6` | **8, 0, 0, 0** | **127, 0, 0, 1** (:1593) |
| `cur_hp` | current HP (0 = corpse) | `mob.hp` |
| `server_controlled` (+ `cmd_hold_ms`) | **1 + 0** while server melee relies on static server x/y (a wandering client mob drifts away from `mob.x/y`); 0 once positions are tracked | 0 (:1599) |

### 3.6 Content tables
- **`server/portals_en.json` (new).** Key `"{map}_{line_index}"`, value `{dest, x, y, tile}`. It is generated by `tools/build_portals_en.py`:
  1. Decrypt `hs/stageAA_BB.hmi` (mod-3 cipher).
  2. Walk `LayerLists` in order, each layer's `Tile`s in XML order, and each tile's sprite lines from its `.hsi`: `SpriteLists` maps `hsi=` to `./hs/*.hsi`, and the `.hsi` holds the line list; format per `reference_asset_formats.md`. Assign a running line index.
  3. For tiles with `event="1"`, record each `type == 0` line index -> `value_num`.
  4. Arrival in `dest` = the reverse portal tile in `dest` (`value_num == map`) + (dx, dy). Use the offsets observed in `gamedef.maps` for that portal (hsi, sprite); default (+55, -85).
  5. Assert that `101_23 -> 102` (live capture).

  Until the tool exists, keep `portals.json`, but drop the 20 malformed keys and log unknown keys with the EN `value_num` candidates for that map.
- **`gamedef.sqlite3 npcs`** (319 rows): `idx, Title_Str, hsifile, Lv, type (3 = AI monster, 0 = NPC), HP, MP, Body_Atk, Def, Exp, speed, Drop, AI, UI` (UI = window, §1.6). EN names are in `server/data/en/npcs_en.json`.
- **`server/data/map_spawns.json` (new):** `{map_code: [{"template": int, "npc": int, "x": f, "y": f, "respawn_s": int}]}`. It replaces `MAP_SPAWNS` (:131-137) and `MONSTER_DB` (:113-128). Seed it with the eight map-102 points. The source of spawn data is open (Q7): `SCRIPT_LISTS` is empty in the EN `.hmi` files.
- **`server/data/npc_templates.json` (new):** EN `template_index -> {name, hsi}`, dumped live with `enum_container.py` on maps 101, 102 and 501.
  - Hypothesis: `template_index == npcs.idx`. For: index 1 Pupu = KR idx 1, index 6 Kamikaze Rat = KR idx 6.
  - Against: the container has 180 entries vs 319 rows. T-1A-2 decides it.
- **`VILLAGES` constants:** the §1.6 tables, plus `TOWN`, `TOWN_ARRIVAL`.
- **Map names:** `data/en/map_codes_en.json`. `data/map_codes.json` is keyed by the KR file code.

### 3.7 Codec rules (`wsproto.Grammar`, specs from `protocol_spec.json`)
- **Fix B1 first.** Otherwise every condition containing a hex literal evaluates to false.
- 0x1B: pass the helper field `state_blob_u32_0 = lo` in the record so the tail conditions evaluate.
- 0x2A: pass `move_bits_lo = lo` and `assume={'mover.state_904 != 0x10 && target_uid != local_player_uid': True}` so the position block is emitted.
- `bytes[8]` fields take Python `bytes`; `str[N]` takes `str`.
- Every builder asserts `len(payload) <= 2038`. `make_raw_packet` (:209-215) silently masks the size to 11 bits.

---

## 4. Current implementation status and proven bugs

### 4.1 Status by opcode

| Opcode | Server today | Verdict |
|---|---|---|
| C2S 0x0D | `_dispatch` :606-607 -> `_handle_world_sync` :669-684: consumed, not parsed, not relayed | partial (single-player correct) |
| C2S 0x7E | `_handle_change_map` :1244-1320: `portals.json` lookup; unknown or same-map ignored; replays 0x08+0x03+0x07+0x0A+0x28+0x44+0x1A. Live PASS on 101<->102. | implemented (B2, B7, B14-B17) |
| C2S 0x5D | not in `_dispatch` ("Unhandled", :659-660) | missing (B19) |
| S2C 0x03 | `_build_pyslayer_opcode_03` :2035-2081. Layout correct (64 B, live decode OK); values hardcoded. | partial (B2, B3) |
| S2C 0x04 / 0x05 / 0x06 | no builders (`_build_pyslayer_opcode_07` :2083-2190 is an unused KR layout) | missing |
| S2C 0x07 | `_build_en_opcode_07` :2268-2397 + `_packet` :2399-2408. Layout correct (live 369 B decodes); values wrong. | partial (B4-B6, B8) |
| S2C 0x08 | :1282-1283 `u16 map, u32 uid, u32 0`; the first 6 B are read | implemented (B18) |
| S2C 0x1A | `_build_opcode_1A` :1543-1600. Layout correct (live 89 B decodes). | implemented (B11-B13) |
| S2C 0x1B / 0x2A / 0x9E / 0x9F | none | missing |
| S2C 0x81 / 0xA5 | none | missing |
| codec `wsproto.py` | hex literals in conditions break encode and decode | bug B1 |

### 4.2 Bugs

**B1 (crash_or_desync). `wsproto._eval` treats hex literals as unknown field names.** Any condition containing one is classed as client state and evaluates to **false** on both encode and decode.
- Evidence: `wsproto.py:183-184`. `re.findall(r'[A-Za-z_]\w*', ...)` pulls `xF` out of `0xF`, and the filter at :184 only removes names matching `0x...`, so `xF` is reported "missing".
- Reproduced in scratch:
  - `_eval('((state_lo >> 12) & 0xF) != 0', {'state_lo': 0x5000})` raises ClientStateCondition, while the same expression with `15` returns True.
  - A 22-byte C2S 0x0D with `ae=5` fails with "4 trailing byte(s)".
  - 0x1B with `ae` and `ie` set encodes 16 B instead of 36.
  - 0x07 with buff 0xA31 encodes 375 B instead of 379.
- Affected: the 0x0D/0x1B/0x2A tails and the 0x04/0x07/0x2E buff params. `LIVE_TEST_LOG`'s PASS only covered tail-less samples.
- Fix: strip `\b0[xX][0-9a-fA-F]+\b` before collecting names. Scratch-verified: the 0x0D `ae`/`ie=12` tails decode, 0x1B encodes 36 B, 0x07 A31 encodes 379 B and round-trips.

**B2 (crash_or_desync). Every portal wipes the client's character sheet.** `_build_pyslayer_opcode_03` hardcodes:
- clock 1000 (:2043, labelled "x ?")
- gold 100000 (:2044)
- `exp_total` 30000 (:2045, labelled "fame related")
- empty active quests (:2064-2067)
- zero completed quests and items (:2076-2079)

It is sent at enter-world (:733) **and on every map change** (:1298). The session meanwhile holds `_wallet` gold 999999 (:994-1000), `session['exp']`, `['inventory']` and `['quests']`. After one portal:
- the bag and quest log are empty client-side while the server model keeps them (quest re-accept becomes possible; 0x59 targets empty slots);
- gold jumps back at the next 0x18;
- the exp bar is computed from 30000.

Fix: F1 step 3. Cross-reference item_inventory B5/B6.

**B3 (wrong_behavior). Enter-world ignores the saved map.** :722 sets `session['current_map'] = char.map or 101`, but :733 calls `_build_pyslayer_opcode_03(session, char)` without `current_map`, so the client always loads 101 (default arg :2035). Monsters (:787) and portal keys (:1260) use `session['current_map']`. A character saved on any other map stands in 101 while the server uses another map's portals and spawns. Fix: pass `current_map=session['current_map']`.

**B4 (wrong_behavior). 0x07 seeds a non-idle movement state.**
- Evidence: :2378-2387 writes IP octets `127,0,0,1` into `+0x8B3..+0x8B6` (`session['client_ip']` is never set, so it is always 127.0.0.1). :2367-2372 sends `+0x954 = 32, +0x8CF = 1, +0x904 = 0, +0xE00 = 501, +0x8BD = 0`.
- Spec 0x07 references (3)(4) give the idle defaults `8,0,0,0` and `0/8/8/0/2`.
- Consistent with that, the first live 0x0D (`00 02 40 53`) has `+0x8B6 = 1` (the last octet).
- With `+0x8B3 = 127` or `+0x904 = 0` the player fails the idle test (§1.4 rule 3) and streams 0x0D every ~210 ms. A remote record built this way would walk.
- Fix: §3.4. Test: T-0D-2.

**B5 (wrong_behavior). 0x07 sends the four base stats as 0** (:2348-2349, labelled "equip-appearance/dye ids"). Spec: `+0xE6..+0xEC` are STR/DEX/INT/TOL, used by `FUN_0041ACE0` for max HP/MP and by the stat window. Fix: send `char.str/dex/int/spr`.

**B6 (wrong_behavior). 0x07 `cur_hp/cur_mp = 0`** (:2391-2392). The player spawns at HP 0 and relies on the follow-up 0x28/0x44 (:781-782, :1314-1315). A remote record with HP 0 is a corpse (untargetable). Fix: real values.

**B7 (wrong_behavior). A portal is a free full heal.** `_handle_change_map` sends `session['max_hp']/['max_mp']` (:1312-1313) instead of the current values tracked by item use (:1216). Fix: current values in 0x07 and 0x28/0x44.

**B8 (crash_or_desync, multiplayer blocker). Every login is uid 1.**
- `session['account_id'] = 1` (:2656); 0x07 uses `char.get('uid', 1)` (:2307); change-map overrides it with account_id (:1296); `_send_level` defaults `uid=1` (:1623); the combat driver finds "the player" by `uid == 1` (:1774, :1781).
- Once presence exists, a second client receiving 0x05/0x04 with uid 1 registers it as **its own local player** (spec 0x05 hazard 2), and 0x1B/0x2A for uid 1 are queued for, or teleport, the receiver.
- Fix: lc-uid-online. Keep test = 1 and admin = 2 so the harness still works.

**B9 (wrong_behavior). C2S 0x0D is dropped unparsed** (:669-684). The comment at :679-680 says the payload "begins with two f64 world coords", but positions exist only in the `ie` tail. No relay exists. `session['x']/['y']` are never written anywhere, so `_handle_melee` (:1640-1641) always uses the saved character x/y. Fix: F2.

**B10 (wrong_behavior). Monsters are per session, not per map.**
- `_spawn_map_monsters` replaces `session['monsters']` on every entry (:1823) with uids `MOB_UID_BASE + i` (:1832).
- Leaving and returning re-creates every mob at full HP (kill, portal, return exploit).
- Two players on a map get private monster sets with **identical uids**.
- Fix: `MapInstance.entities` (§3.1, F6).

**B11 (crash_or_desync). Monster death and respawn corrupt client entities.**
- `_send_death` sends 0x29 with `respawn_tick = 0, x = 0, y = 0` (:1986-1992 defaults). Per decomp `FUN_00413920` case 0x16 (:962-983), a type-4 entity with `+0xE5C - clock < 0` is revived **by the client** at `(+0x1318, +0x1320) = (0, 0)` with full HP right after the death animation. That leaves a live ghost at the map origin that the server considers dead.
- `_tick_respawns` (:2023-2033) then re-sends 0x1A for the same uid, and 0x1A has no de-duplication, so a second entity appears (`FUN_004189f0` returns the first match).
- `_tick_respawns` is only called from `_handle_melee` :1638 and `_handle_attack` :1847, so real kills made through `_combat_driver` never respawn.
- Fix: F6 (cs-monster-lifecycle). Tests: T-1A-3, T-06-2.

**B12 (wrong_behavior). 0x1A seeds a non-idle state and a stray status effect** (:1574-1593):
- `action_state (+0x904) = 0` (idle is 8)
- facing/motion `127,0,0,1` (IP octets)
- `+0x8CF = 1`, `+0x954 = 32`, `+0xE00 = 501`
- The inner-loop entry `u16 0xB3B, i32 0` is a **status effect** (`FUN_00424e20`, skill table), not an "idle action".

The live result "`effect_count = 0` hides the body" predates these value fixes; the non-idle state may be the real cause, and may also explain why mobs never wander. Fix: §3.5, then T-1A-1.

**B13 (wrong_behavior). Template index conflated with the KR npccode.** `_spawn_map_monsters` passes `anim_idx=stat.get('anim', npccode)` (:1839), but `MONSTER_DB` has no `anim` key, so the npccode is sent as `template_index`. It only works because Pupu is 1 == 1. An unbound index leaves `+0x11DC` NULL; memory notes a nameplate NULL-deref crash at 0x43C4C2 for type-4 entities. Fix: `npc_templates.json`.

**B14 (wrong_behavior). The portal table is KR, not EN** (evidence §1.5, from the decrypted EN `.hmi` files):
- 237 of 594 EN portals have no key, so pressing the key does nothing;
- 228 table pairs do not exist in EN;
- 132 arrival points disagree with the EN layout (e.g. `202_6 -> 201 @ (895,1406)` vs the EN tile at (1100,1200));
- 20 malformed keys (`None_None`, `_`, `*_None`, null destinations).

`portals.json` is only trustworthy for the live-tested 101/102. Fix: `portals_en.json` (§3.6).

**B15 (wrong_behavior). No server portal guard.** `_handle_change_map` has no `in_world` check and no cooldown. `portals.json` has **21** `{map}_0` keys (103_0, 216_0, 402_0, 408_0, 423_0, 505_0, 510_0, 705_0, 806_0, 811_0, 901_0, 906_0, 908_0, 919_0, 1006_0, 1104_0, 1105_0, 1107_0, 1110_0, 1113_0, 1119_0), and the spurious-index-0 client bug (§1.5) can hit them. Fix: F3 step 2.

**B16 (wrong_behavior, edge). Map change commits before it can complete.** `_handle_change_map` sets `current_map` (:1276) and sends 0x08 (:1283, which destroys every client entity) **before** resolving the character (:1291). The no-character branch (:1319-1320) leaves the client in mode 7 with no player, and the server on the new map. Fix: resolve and build everything first, then send.

**B17 (wrong_behavior). No persistence.** Map, position, HP/MP, gold, exp, inventory and quests live only in the session. `char['level']` is changed in memory (:2003) but never saved. `_save_accounts` is called only at :402 and :2612. Every stored character has `map 0, x/y 100`. Fix: §3.3.

**B18 (cosmetic). The 0x08 body carries the uid in the clock slot.** The body is `u16 map, u32 uid, u32 0` (:1282, 10 B). Per spec, the u32 is `game_time_ms` (the scene clock) and the last 4 B are ignored. The docstring at :1254 is wrong. Fix: `{map_code, game_time_ms: 1000}`, 6 B.

**B19 (missing). No C2S 0x5D handler.** Confirming a village transfer does nothing and shows no feedback (no 0x81).

**B20 (cosmetic, performance). Double fade on every portal.** Map change sends 0x08 **and** 0x03; each fades (~640 ms `Sleep` on the client network thread) and loads the map, so the client stalls about 1.3 s and loads twice. The server also sleeps 50 ms six times in the session's receive thread (:1297-1317). 0x08 is still needed to close window 0x79 and modals. The comment at :1285-1290 (0x08+0x07 alone left the body invisible) predates the 0x07 fixes. Re-test with T-08-2 before removing either packet.

**B21 (cosmetic, cross-group). Wrong replies and labels for C2S 0x63/0x2F.** `_dispatch` answers C2S 0x63 with an **empty S2C 0x63** (:610-611) on every map load. S2C 0x63 is BattlefieldQueueCounts (`u16, u8`), so two Fireway short reads leave garbage Battlefield labels. :630-631 labels C2S 0x2F "arena query"; it is MessengerFriendListRequest. Fix: no 0x63 reply; relabel 0x2F.

**B22 (cosmetic, cross-group). Welcome whisper on every map change.** Each map change sends the whisper-style 0x0A "Welcome!" (:1306-1309), which adds "Server" to the whisper dropdown. Fix: chat_mail_gm-system-notice (0x15, first entry only).

**B23 (cosmetic). Dead code and stale comments.**
- Unused: `_build_pyslayer_opcode_07` (KR layout, :2083), `_build_pyslayer_enter_world` (:2410), `_build_enter_world_response` (:2472), `_build_map_enter_packet` (:900), `_fake_map_server` (:862), `_spawn_test_monster` (:1602; its uids start at 0x1000, inside the player uid range).
- Stale comments: docstring :686-699 (0x2E/316 B flow); :737-748 ("positions are i32"; they are f64 and encoded correctly); :789-839 (disabled 0x6F/0x19 theories).

**B24 (wrong_behavior, multiplayer). No presence infrastructure.**
- The disconnect `finally` (:578-581) only deletes the session: no despawn broadcast, no persistence.
- The session does not store its encoding mode, so a broadcast cannot pick the target's mode.
- `_admin_listener` (:459-461) forces `use_by_array=True` for all sessions.
- Fix: §3.1/§3.2, F4.

**B25 (cosmetic). Wrong monster name.** `MONSTER_DB[1]` is named "Seeyo" (:114); the EN client template shows "Pupu" (LIVE_TEST_LOG bug 7). This affects server logs only.

---

## 5. Implementation plan

| ID | Title | Opcodes | Pri | Effort | Depends on | MP |
|---|---|---|---|---|---|---|
| `world-codec-hexfix` | Fix wsproto hex-literal conditions; unit tests for the 0x0D/0x1B/0x2A tails and 0x07 A31 buffs | 0x0D, 0x1B, 0x2A, 0x04, 0x07 | P0 | S | — | no |
| `world-03-real-state` | Build 0x03 from the session (map, gold, exp, fame, quests, completed quests, item lists); pass `current_map` at enter-world; re-send 0x59 for ready quests | 0x03 | P0 | M | codec-hexfix; item_inventory list model | no |
| `world-entity-lifecycle` | 0x29 hold tick + home, 0x06 corpse despawn, periodic respawn ticker, no duplicate 0x1A (implemented once with combat `cs-monster-lifecycle`) | 0x06, 0x1A | P0 | S | T-06-2, T-1A-3 | no |
| `world-player-record` | Grammar-based `player_record()` with idle defaults and real stats/HP/MP/level; use it for 0x07 | 0x07 | P1 | S | codec-hexfix | no |
| `world-1a-defaults` | 0x1A through the grammar with idle defaults; `effect_count` per T-1A-1; `server_controlled` per §3.5 | 0x1A | P1 | S | codec-hexfix, T-1A-1 | no |
| `world-maptransfer` | `MapTransfer()` (F7): build everything first, then 0x08 (6 B) + 0x03 + 0x07 + current HP/MP + batched 0x1A; no sleeps; one welcome | 0x08, 0x03, 0x07, 0x7E | P1 | M | 03-real-state, player-record | no |
| `world-portal-guards` | `in_world` gate, 2 s cooldown, index-0 guard, drop malformed `portals.json` keys, log EN `value_num` candidates for unknown keys | 0x7E | P1 | S | maptransfer | no |
| `world-portal-table-en` | Tool that generates `portals_en.json` from EN `.hmi` + `.hsi` line lists (line index -> `value_num`, arrival = reverse tile + offset); asserts `101_23 -> 102`; live survey for gaps | 0x7E | P1 | M | — | no |
| `world-persistence` | Persist map/pos/hp/mp/gold/exp/level/stats/inventory/quests; save on transfer, disconnect and every 60 s; sane new-character spawn | — | P1 | M | lc-uid-online | no |
| `world-registry` | `World`/`MapInstance`, `in_world` flag, per-session `no_enc`, thread-safe `send`/`broadcast`, disconnect cleanup | — | P1 | M | lc-uid-online | yes |
| `world-presence` | Enter/leave presence: 0x04 (<= 5 per packet) to the entrant, 0x05 to peers, 0x06 on leave/transfer/disconnect | 0x04, 0x05, 0x06 | P1 | M | registry, player-record | yes |
| `world-move-relay` | Parse 0x0D (validate map/in_world), mask garbage, relay as 0x1B with tails, track pos and clock | 0x0D, 0x1B | P1 | M | presence, codec-hexfix | yes |
| `world-shared-monsters` | `MapInstance.entities`, `map_spawns.json`, uid allocator, empty-map retention, per-map combat routing | 0x1A, 0x06 | P1 | M | registry, entity-lifecycle | yes |
| `world-village-transfer` | C2S 0x5D: server fee formula + gold + cooldown, 0x81 result, MapTransfer to `TOWN`/`TOWN_ARRIVAL` | 0x5D, 0x81 | P2 | M | maptransfer, 03-real-state | no |
| `world-template-map` | Dump EN template lists (`enum_container.py`), build `npc_templates.json`, join `gamedef.npcs` for stats; replace MONSTER_DB/anim | 0x1A | P2 | M | 1a-defaults | no |
| `world-keyframe-2a` | 0x2A keyframe helper (always append the pos block, never to the mover) and 0x9E stop helper | 0x2A, 0x9E | P2 | S | move-relay | yes |
| `world-position-estimate` | Remote-player positions for 0x04/0x05/0x2A: `ie`-tail updates + horizontal dead reckoning at the measured walk speed; spawn slightly above the floor (gravity settles, T-05-3); dev memory reader per pid | 0x0D, 0x04, 0x05, 0x2A | P2 | L | move-relay, T-0D-4, T-05-3 | yes |
| `world-monster-ai-1b` | Server-controlled monsters: `server_controlled=1`, AI tick sends 0x1B motion commands, integrates position, 0x2A drift correction | 0x1A, 0x1B, 0x2A | P2 | L | shared-monsters, T-1B-2 | no |
| `world-portal-proximity` | Reject 0x7E whose portal line is far from the last known position (needs the parsed `.hsi` lines) | 0x7E | P3 | M | portal-table-en, position-estimate | no |
| `world-queued-sync` | 0x9F buffered-state helper (for example GM or AI state that should not interrupt the current action) | 0x9F | P3 | S | move-relay | yes |
| `world-speedhack` | `realtime_delta_ms` vs sum(`logic_elapsed_ms`) check on 0x0D; log or kick | 0x0D | P3 | S | move-relay | no |
| `world-instance-dungeon` | 0xA5 result + MapTransfer once the C2S request and the UI 1220 NPC flow are identified | 0xA5 | P3 | M | maptransfer | no |
| `world-single-fade` | Drop 0x08 or 0x03 on portal transfers if T-08-2 shows visibility is kept | 0x08, 0x03 | P3 | S | maptransfer, T-08-2 | no |
| `world-cleanup` | Remove dead builders and stale comments; stop the empty S2C 0x63; relabel 0x2F; fix the 0x08 body | 0x63, 0x2F, 0x08 | P3 | S | — | no |

- **UDP:** none of this needs UDP. Field movement is TCP 0x0D. The P2P UDP path runs only in rooms (`[map]+0x70 == 1`).
- **Order:** codec-hexfix -> entity-lifecycle -> player-record + 1a-defaults -> 03-real-state -> maptransfer -> portal-guards -> portal-table-en -> (lc-uid-online) -> persistence -> registry -> presence -> move-relay -> shared-monsters -> village-transfer -> P2/P3.

Relay sketch (F2):
```python
G0D = Grammar(SPEC['0x42CE94/0x0D']['grammar']); G1B = Grammar(SPEC['0x1B']['grammar'])
def _handle_move_state(self, sock, s, payload, no_enc):
    s['no_enc'] = no_enc
    if not s.get('in_world'): return
    try: r = G0D.decode(payload)
    except GrammarError: return
    if r['map_code'] != s['current_map']: return
    lo, hi = r['state_lo'] & 0x003FFFFF, r['state_hi'] & 0xFFF
    ie = (lo >> 16) & 0xF
    s['clock'] = s.get('clock', 1000) + r['logic_elapsed_ms']
    if ie: s['pos'] = (r['pos_x'], r['pos_y'])
    s['move'] = (lo, hi, time.monotonic())
    body = G1B.encode(dict(uid=s['uid'], hold_ms=min(r['logic_elapsed_ms'], 990),
        state_blob=struct.pack('<II', lo, hi), state_blob_u32_0=lo,
        target_uid=r.get('event_source_uid', 0), target_x=r.get('f64_1338', 0.0),
        target_y=r.get('f64_1340', 0.0), pos_x=r.get('pos_x', 0.0), pos_y=r.get('pos_y', 0.0)))
    self.world.broadcast(s, 0x1B, body)      # peers only, never the sender
```

---

## 6. Live test plan

**Harness** (`server/wsdev.py`):
- Commands: `up`, `restart`, `cap [--all] <secs> <wsview action>`, `send <op> <hex>`, `sendspec <op> '<json>'`, `state`, `shot`.
- `hold` for movement and actions: `hold right 1000`, `hold s 450`, `hold space 350`.
- One experiment per restart (`feedback_experiments.md`). Record results in `LIVE_TEST_LOG.md`.

**Conventions:**
- Until B1 is fixed, `sendspec` drops hex-literal conditions. Use raw `send` for 0x1B/0x2A tails; hex is given below.
- Test ghost player uid **2** (local is 1). Test monster uid **0x000F1000** (`00 10 0F 00`).
- `<px> <py>` = the player position from `python wsdev.py state`.

### C2S 0x0D — PlayerMoveState
- **T-0D-1 (safe).** Run each with `python wsdev.py cap --all 3 ...`: `hold right 1000`, `hold left 1000`, `hold up 300`, `hold down 300`, and `hold s 450` next to a Pupu on 102.
  - Expected: 18 B while walking; `lo` bits 0-1 = 2 (right) / 1 (left); jump `+0x8B4 = 3`; an attack sets `ae != 0`, giving a 22 B packet with `event_source_uid`.
  - Record every distinct masked `lo`; T-1B-* replays these blobs.
- **T-0D-2 (safe).** Release all keys after spawning, then `python wsdev.py cap --all 6 shot`.
  - Before the B4 fix: a stream of ~28 packets (every 210 ms).
  - After: at most 2 packets, then silence until the 15 s keepalive.
- **T-0D-3 (safe).** Walk into a monster or get hit. Look for a 0x0D with `ie != 0` (>= 61 B). `pos_x/pos_y` must match `wsview state`.
- **T-0D-4 (safe).** `state`, then `hold right 2000`, then `state`. Record px/s and the sum of `logic_elapsed_ms` from `cap`. This gives the walk speed and clock rate for world-position-estimate.

### C2S 0x7E — PortalEnterRequest
- **T-7E-1 (state_change).** On map 101, stand on the portal at tile (1355,802) (ledge x ~1404). Run `python wsdev.py cap --all 4 hold up 400`, then after a restart `... hold down 400`.
  - Expected: exactly one of them produces `0x7E 17 00 00 00`, followed by S2C 0x08, 0x03 (64 B), 0x07 (369 B), 0x0A, 0x28, 0x44, 0x1A x8.
  - Resolves which key gives `+0x8B4 = 4` (spec says Up, live log says Down); check bits 2-4 of the preceding 0x0D.
- **T-7E-2 (state_change).** On 102, at the return portal (`102_26`), press the key twice within 1 s. Expected: one 0x7E only (2.5 s client cooldown), arrival at 101 (1411,714).
- **T-7E-3 (state_change, needs world-03-real-state).** Before portalling record gold, bag and quest log (`key i`, `key q`, `shot`); portal and re-check. Expected: identical. Before the fix: empty bag and log, gold 100000 (B2).
- **T-7E-4 (state_change, B14 survey).** On 102, walk to the portal toward 103 (EN tile (2306,664)) and `cap --all 4 hold <portal key> 400`. Record the index and compare it with `portals.json` `102_14`. Repeat once per reachable map to seed `portals_en.json` checks.

### C2S 0x5D — VillageTransfer
- **T-5D-1 (safe, capture only).** Prerequisite: the character is in a village town with Garan Maria.
  - After world-03-real-state, set TestHero `map: 501, x: 2170, y: 560` in accounts.json and relog. Alternatively reach 501 by portals.
  - Stand next to Garan Maria (EN tile (2115,645)) and run `cap --all 3 hold space 350`. Expected: window 600 opens with no packet.
  - Click a destination, then OK: `cap --all 3 click <x> <y>`. Expected: `0x5D <idx> <fee u16 LE>`; the server logs "Unhandled opcode 0x5D".
  - From 501 (village 5), check: idx 2 Popola -> **2840** (`18 0B`), idx 6 Mining -> **1000** (`E8 03`), idx 10 RA -> **4520** (`A8 11`). With manner >= 500 expect 90 %.
- **T-5D-2 (safe).** In village 5, pick idx 5 (current) -> "You can't move to where you already are.", no packet. On field map 102 (group 1), window 600 is not reachable (Garan Maria is absent).
- **T-5D-3 (state_change, after world-village-transfer).** Confirm with enough gold. Expected: coin sound 0x29, gold label drops by the fee, fade, the town loads and the player is visible at `TOWN_ARRIVAL`. Record `state` pos to fix the arrival table. With too little gold (`sendspec 81 '{"result":1,"gold":10}'` first), the client refuses locally ("You are short of gold."), no packet.

### S2C 0x03 — EnterWorldState
- **T-03-1 (disruptive).** Covered by T-7E-3. Injecting it directly destroys every entity; it can only be tested through the server flow, because 0x07 must follow.
- **T-03-2 (state_change, after 03-real-state + persistence).** Log out and back in with 2 bag items and 1 active quest. Expected: both restored; quest counter correct after 0x59.

### S2C 0x04 — PlayerAppear
- **T-04-1 (state_change).** In town:
  ```
  python wsdev.py sendspec 04 '{"player_count":1,"repeat[player_count]":[{"name":"Shop","uid":3,"job":1,"level":5,"rank_icon":99,"repeat[14]":[{"appearance_part":0},{"appearance_part":1},{"appearance_part":1},{"appearance_part":0},{"appearance_part":2},{"appearance_part":2},{"appearance_part":2},{"appearance_part":0},{"appearance_part":0},{"appearance_part":0},{"appearance_part":0},{"appearance_part":179},{"appearance_part":0},{"appearance_part":0}],"stat_str":5,"stat_dex":5,"stat_int":5,"stat_tol":5,"anim_substate_8cf":8,"action_state_904":8,"direction_8bd":2,"pos_x":<px+100>,"pos_y":<py>,"input_state_8b3":8,"cur_hp":100,"cur_mp":50,"shop_open":1,"shop_sign_sprite":1,"shop_title":"Ghost Shop"}]}'
  ```
  - Expected: a clothed character "Shop" stands idle with a stall sign "Ghost Shop". Clicking it sends C2S 0x61 (`cap --all 3 click <x> <y>`).
  - Cleanup: `send 06 03 00 00 00`.

### S2C 0x05 — RemotePlayerAppear
- **T-05-1 (state_change).** `python wsdev.py sendspec 05 '{"name":"Ghost","uid":2,"job1":1,"level":5,"rank_emblem":99,"repeat[14]":[...same 14 as T-04-1 with key "appearance"...],"stat_str":5,"stat_dex":5,"stat_int":5,"stat_tol":5,"attack_dir_8cf":8,"motion_id":8,"facing_dir":2,"pos_x":<px+60>,"pos_y":<py>,"input_dir_8b3":8,"cur_hp":100,"cur_mp":50}'` (367 B).
  - Expected: "Ghost" appears idle; `state` lists alive=3 uid 2; the local player stays controllable.
- **T-05-2 (crash_risk, run last).** The same with `"uid":1`. Expected per spec: the local-player pointer moves to the ghost, so control and camera break (confirms B8). Restart the client afterwards.
- **T-05-3 (state_change).** T-05-1 with `pos_y: <py-200>` over flat ground. Expected: Ghost falls onto the floor within ~1 s. This validates spawning remote records above the floor (world-position-estimate).

### S2C 0x06 — PlayerDespawn
- **T-06-1 (state_change).** After T-05-1: `python wsdev.py send 06 02 00 00 00`. Expected: Ghost vanishes, `state` no longer lists uid 2, no crash.
- **T-06-2 (state_change).** On 102: `send 06 00 00 0F 00` (mob uid 0x000F0000). Expected: that Pupu disappears. Then re-spawn it with the T-1A-1 packet using `uid: 983040`. Expected: exactly one entity with that uid. This validates entity-lifecycle.
- **T-06-3 (crash_risk, do not run casually).** `send 06 01 00 00 00` (own uid). The spec predicts a dangling `scene+0x970` and likely a crash. Only run it to confirm the server guard is needed.

### S2C 0x07 — EnterWorldPlayerList
- **T-07-1 (safe, after world-player-record).** Relog. Expected:
  - visible and controllable;
  - the status sheet (`key c` + `shot`) shows real STR/DEX/INT/TOL and HP/MP before 0x28/0x44 arrive;
  - T-0D-2 shows an idle client.
- **T-07-2 (state_change).** Right after login run T-04-1 twice with different uids. Expected: the local UI is unaffected and both ghosts render (0x07 followed by 0x04 is safe).

### S2C 0x08 — ChangeMap
- **T-08-1 (disruptive).** `python wsdev.py sendspec 08 '{"map_code":102,"game_time_ms":1000}'` (6 B). Expected: fade, map 102 loads with no player and no monsters (all entities destroyed, mode 7). Recovery: `wsdev.py restart`.
- **T-08-2 (state_change, world-single-fade experiment).** Route portal transfers through (a) 0x08 + 0x07 + 0x28/0x44 without 0x03, or (b) 0x03 + 0x07 without 0x08. One variant per restart. Record visibility, control and fade time; with (b), also check whether window 0x79 closes after death.

### S2C 0x1A — NpcMonsterSpawn
- **T-1A-1 (state_change).** In town 101, idle defaults with **no** effect entry (83 B):
  ```
  python wsdev.py sendspec 1A '{"count":1,"repeat[count]":[{"template_index":1,"uid":987136,"effect_count":0,"home_x":<px>,"home_y":<py>,"unk_8cf":8,"action_state":8,"unk_8bd":2,"pos_x":<px+80>,"pos_y":<py>,"facing":8,"cur_hp":24,"server_controlled":0}]}'
  ```
  - Expected: a Pupu with a **visible body**. If the client wander AI now works, `state` pos changes within ~6 s.
  - If the body is invisible, retry with `"effect_count":1,"repeat[effect_count]":[{"effect_id":2875,"effect_duration_ms":0}]` and record which field mattered.
  - Then repeat with `"server_controlled":1,"cmd_hold_ms":0`. Expected: the body stays static.
- **T-1A-2 (state_change).** Templates 2, 3, 4 and 6 on maps 101, 102 and one town. Expected names: Blue Pupu / Well-done Pupu / Blood Pupu / Kamikaze Rat everywhere. The same name on every map means the list is global (supports `template_index == npcs.idx`).
- **T-1A-3 (state_change).** Kill a Pupu on 102 (`hold s 450` x3), then `state` every second for 5 s. Expected with the current 0x29 (tick 0, x/y 0): uid 0xF000x reappears alive near (0,0) (confirms B11). After the fix (tick 0x7FFFFFFF, home x/y): it stays dead until 0x06 + 0x1A.

### S2C 0x1B — EntityActionCommand
- **T-1B-1 (state_change, ghost relay).** After T-05-1, walk right with hold 210 ms: `python wsdev.py send 1B 02 00 00 00 D2 00 00 00 02 00 00 00 00 00 00 00`. Or use a right-walk `lo` captured in T-0D-1 in bytes 8-11.
  - Stop after 1 s: `send 1B 02 00 00 00 E8 03 00 00 00 00 00 00 00 00 00 00` (hold 1000).
  - Expected: Ghost walks right about 1 s, then stops. The second hold delays the stop, which confirms that hold is a delay counted from the previous command.
- **T-1B-2 (state_change, monster command).** Spawn T-1A-1 with `server_controlled: 1, cmd_hold_ms: 0`, then `send 1B 00 10 0F 00 D0 07 00 00 06 00 00 00 00 00 00 00` (facing wire 2 = right, motion 1, hold 2000).
  - Repeat with `lo` byte `0E` (motion 3), `16` (motion 5) and `1A` (motion 6).
  - Expected: the Pupu faces or moves right for 2 s. Record `state` x before and after (walk speed for F9).
- **T-1B-3 (state_change, two clients, after move-relay + lc-uid-online).** Start `WindSlayer_p2.exe` as admin/test on the same map. Walk, jump and attack on client A.
  - Expected: B sees A move with < 300 ms lag and stop on the same spot; no cipher desync on either client.

### S2C 0x2A — RemoteEntityMoveStateSync
- **T-2A-1 (state_change).** After T-05-1, build the payload with `python -c "import struct;print(struct.pack('<IQIddB',2,0,0,<px+200>,<py>,0).hex(' '))"`, then `python wsdev.py send 2A <hex>` (33 B).
  - Expected: Ghost snaps 200 px right immediately and stays idle.
- **T-2A-2 (crash_risk).** `send 2A` with mover uid 1 (own). Expected per spec: the local player teleports and a node leaks, but no crash. Only run it to justify the server guard.
- **T-2A-3 (state_change).** On a wandering T-1A-1 Pupu (`server_controlled: 0`), send `2A` for uid 987136 (`00 10 0F 00`) with its current x/y.
  - Expected: wandering stops (+0x8E4 set).
  - Then `send 9E 00 10 0F 00 00 00 00 00 00 00 00 00`. Expected: wandering resumes. This confirms the §1.4 +0x8E4 inference.

### S2C 0x9E / 0x9F — RemoteEntityMoveStateQueued
- **T-9E-1 (state_change).** With Ghost walking (T-1B-1 without the stop): `python wsdev.py send 9E 02 00 00 00 00 00 00 00 00 00 00 00` (12 B). Expected: Ghost stops within about one tick.
- **T-9F-1 (state_change).** The same with `send 9F ...`. Expected: the stop applies within <= 990 ms. Record the 0x9E/0x9F/0x1B latencies.

### S2C 0x81 — VillageTransferResult
- **T-81-1 (state_change).** `python wsdev.py sendspec 81 '{"result":1,"gold":12345}'` (9 B). Expected: coin sound 0x29, the inventory gold label shows 12345, no map change. The server wallet stays out of sync until the next 0x18.
- **T-81-2 (safe).** `python wsdev.py send 81 00`. Expected: "Village transfer failed. Please try again in a few minutes."; gold unchanged.

### S2C 0xA5 — InstanceDungeonCreateResult
- **T-A5-1 (safe).** `python wsdev.py send A5 01 00 00 00 00`. Expected: "Moving into Instance Dungeon" dialog; no map change.
- **T-A5-2 (safe).** `send A5 02 00 00 00 00`. Expected: "Failed to create an Instance Dungeon."
- **T-A5-3 (safe).** `send A5 03 00 00 00 00`. Expected: nothing.
- **T-A5-4 (safe).** Talk to an NPC with UI=1220 (for example "Hidden Forest Clearing") with `cap --all 3 hold space 350`, then click its buttons. Record the window and any C2S packet (Q10).

---

## 7. Open questions

1. **Relay opcode.** Did the original server relay 0x0D as 0x1B with `hold_ms = logic_elapsed_ms`? What were 0x2A/0x9E/0x9F used for (knockback, idle transitions)? T-1B-1 and T-1B-3 decide the practical answer.
2. **Positions of players already on the map.** 0x0D carries position only in the `ie` tail. How did the original server fill `pos_x/pos_y` in 0x04/0x05/0x2A? Server physics (walk speed, gravity and `.hsi` collision lines, `FUN_00413920`/`FUN_004158d0`) or tolerated desync? T-0D-4 and T-05-3 bound the dead-reckoning approach.
3. **Portal key.** Is the `+0x8B4 == 4` gate Up or Down? T-7E-1.
4. **Monster body and wander.** Was the invisible body at `effect_count = 0` caused by the non-idle seed (`+0x904 = 0`, facing 127) rather than the missing 0xB3B effect? Does client wander run once idle defaults are sent? T-1A-1.
5. **EN portal indices.** Can the `.hsi` line lists be parsed well enough to reproduce `101_23`, including line type and per-tile line order in `FUN_00406170`? Otherwise a live portal survey is needed for about 594 portals. Do any EN portals rely on a nonzero line type or several type-0 lines per tile, giving several indices per tile?
6. **Village transfer pairing and arrival points.** Is 0x81 really the reply to 0x5D (inferred from strings)? Which arrival coordinates did the original server use per town? Is there an EN village-transfer NPC in 101/201/401 (Garan Maria is absent there in the EN maps), or is the feature only reachable from Amakusa onward?
7. **Monster spawn data.** Spawn points, counts and respawn times are not in the EN or KR `.hmi` (`SCRIPT_LISTS` is empty). `gamedef.npcs.AiSpawn` is a candidate; otherwise they must be authored.
8. **Template list scope.** Is the `game_state+0x4E0` list global (`template_index == npcs.idx`) or loaded per map? T-1A-2 and `enum_container.py` on several maps.
9. **0x06 on monsters.** Does it free a type-4 entity cleanly, with no aura or party side effects? Is there any other same-uid de-duplication on the client? T-06-2.
10. **Instance dungeons.** Which C2S requests creation? What do UI-1220 NPCs (286 "Dungeon Ana Johnson", 186/196/203/317) open and send? What did the discarded u32 in 0xA5 hold? No EN map uses the `InDun*` attributes.
11. **Scene clock.** Does re-seeding `scene+0xF1C` to 1000 on every 0x03/0x08 affect buff timers or respawn ticks? Is per-client clock tracking (`seed + sum(logic_elapsed_ms)`) worth using for timed client-side monster revives instead of 0x06 + 0x1A?
12. **Double fade.** Can transfers use only 0x08 or only 0x03 without losing visibility? T-08-2.
13. **`+0x904` values 8 vs 0xC.** Both allow portal use. Must the server also reject portals in any state the client allows?
14. **9701/9702 portals.** Every EN town has a portal to 9701 (flea market) and some to 9702. Are these normal field maps, or do they need room-style handling (S2C 0x2F) like the PvP lobbies (pvp_arena group)?
