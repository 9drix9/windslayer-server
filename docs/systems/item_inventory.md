# item_inventory: system design (EN 2008 client)

Group spec: `re_tools/corpus/systems/item_inventory.json` (25 specs: C2S 0x0F 0x11 0x13 0x14 0x15 0x1F 0x67 0x68 0x69 0x72; S2C 0x11 0x12 0x13 0x18 0x1D 0x1E 0x23 0x24 0x3F 0x41 0x42 0x8D 0x8E 0x8F 0x9C).
Server: `WindSlayer2Game/server/windslayer_server.py` (line numbers below are from the current 2793-line file; several spec `references` cite older numbers).
Order of authority: client binary evidence in the spec, then LIVE_TEST_LOG.md, then server code, then memory notes, then PySlayer.

New evidence gathered for this doc (read-only, nothing modified):
- **EN item catalog = `WindSlayer2Game/hs/windslayer.hii`**. It decodes with the asset cipher (`plain[i] = raw[i] + [0xE9,0xDE,0xE0][i%3]`, drop the 20-byte SHA-1 footer) to plain text: `Number_of_ITEM: 4248`, then one line per item (`<idx> Title: .. Type: .. Sprite: .. HP: .. MP: .. Job: 7 flags .. Kind/Spr_Num/Gender (type 1) or Con[/Skill_Lv] (types 0/3) .. Lv Buy Sell PMoney CT CardSpr CardNpc Cash Cash_Cls Cash_T Cash_P Union Union_Cnt Union_Kind Job2 Cash_ST Cash_V Cash_B mHP mMP`).
- EN ids 1..4248 line up with `gamedef.sqlite3 items` by idx: Type matches for 4247/4248 (only 1542 differs), Lv matches for all type-1 items, Buy price differs for 3. **But `Kind` differs for 289 type-1 items** (e.g. 1343..1364). Every difference has the same shape: KR Kind 17/18/19/20 is EN Kind 14/15/16/17 (72-73 items each), and the KR DB has 361 ids above 4248 that the EN client does not have.
- `windslayer.hqi` decodes the same way (`Number_of_Quest: 291`). EN quest 26 has `Send: 0179...` / `Send_Num: 001...`.
- Item-id lists in both files (`Send`, `Reward`, `Demand`, `Union`, and `npcs.item`) are **4-digit DECIMAL** fields (counts are 3-digit decimal). Proof (re-checked against the live-dumped `server/en_item_catalog.json` names):
  - EN hqi has 556 non-zero item references in Send/Reward/Demand. Read as decimal, all 556 are in the EN catalog; read as hex, 197 fall outside it.
  - `Send 0179` is 179 Wooden Stick (Type 1, Kind 11, Lv 1), while hex 0x179 = 377 is a Lv-53 skill.
  - EN hii 2975 "Chipped Elementirium" has `Union 0281` / `Union_Cnt 0010`, which reads as 10 x Eledust (281). In hex it would be 16 x 641 "Vicar Shoes (M)".
  - gamedef `npcs.item` for npc 1 (Pupu) is `0003 0004 0005 0018 0019 0020 0021 0047 0048 2030 4356`. `MONSTER_DB` (113-128) already reads that list as decimal.

---

## 1. Overview: how the client implements items

### 1.1 Catalog and item types
The client item table lives at `[scene+0xF84]` (loader FUN_004034C0, 0x228-byte records, 1-based id). FUN_00404210 returns NULL for id 0 or an id past the end, and **every item handler silently ignores such ids**. That is why drop item 4356 never appears (live injection test: 0x18 item 4356 did nothing, item 179 worked).

Record fields the protocol depends on:

| def offset | meaning | hii column (EN) |
|---|---|---|
| +0x154 | Type / category: 0 consumable, 1 equipment, 2 etc/material, 3 skill book (0x18 learns the skill), 4 class-change, 5 cash-use item (own tab, see 0x6F) | `Type` |
| +0x158 / +0x15C | HP / MP delta applied by S2C 0x25 | `HP` / `MP` |
| +0x168 + class*4 | Job flags (Job[0] = any class) | `Job` |
| +0x1B8..+0x1C4 | stat modifiers (buff recalc) | `Skill_P_A..Skill_A_D` (inferred) |
| +0x1C8 | Duration used by 0x41/0x42 | most likely `Con` (items 148/153: Con 10000, CT 5000) |
| +0x1CC | Kind, which picks the equip slot in FUN_00426680 | `Kind` (EN values 0,3,5,6,7,8,9,10,11,12,14,15,16,17) |
| +0x1D4 | Gender (1/2) | `Gender` |
| +0x1D8 | required level | `Lv` |
| +0x1EC | cooldown ms | `CT` |
| +0x1F0 | "cash" flag: no drop/trade/sell, alternate equip grid | most likely `Cash` (474 EN items; +0x1FC = `Cash_P` price fits the same layout) |
| +0x202 / +0x212 | 8 recipe material ids / counts | `Union` / `Union_Cnt` (4-digit decimal x8) |
| +0x224 | craft kind: 2 = "Mineral Refining", anything else = "Concoction" | `Union_Kind` (EN: 342 x kind 2 = crafted gear such as 1828 "Crude Wooden Club (s)"; 19 x kind 1 = Elestones 282-284, Elementirium 2975-2979, crafted potions/food 3057-3067) |

### 1.2 Bag tabs (scene = `*0x70EECC`; `player_info = scene+0x48`)
| tab | item Type | capacity byte | ids | per-slot data | stacking |
|---|---|---|---|---|---|
| equipment | 1 | scene+0x3A9 | u16[45] scene+0x3C8 | 12-byte option block scene+0x422+12*i | none, 1 per slot |
| consumable | 0 | scene+0x3AA | u16[45] scene+0x63E | u16 qty scene+0x698, u32 cooldown stamp scene+0x6F4 | merge into one same-id stack while count+qty <= 999, else first empty slot (FUN_00423b70) |
| etc | 2 | scene+0x3AB | u16[45] scene+0x7A8 | u8 qty scene+0x802 | same rule with <= 99 (FUN_00423e70) |
| cash (4th) | 5 | n/a | u16[45] scene+0x830 | u8 scene+0x88A | filled by S2C 0x6F (premium_cash group); 0x03 memsets it |

Capacities must be 1..45. Every add or remove path refuses when the byte is > 45, and 0 gives "There isn't empty space in the inventory". The server currently sends 35/35/35.

### 1.3 The item instance and its wire reference
An item instance = `u16 id` + a 12-byte option block `w[0..5]`. `w0..w4` are socket/option words (elemental stones written by reinforcement, 0x8E). `w5` ("extra/tail") has unknown meaning. Equipment is matched **by id AND a byte-exact 12-byte block** everywhere (0x19/0x23 remove, 0x1D bag remove, 0x1E/0x24 grid remove, 0x8E/0x9C slot lookup).

Wire encoding, used by C2S 0x0F/0x11/0x13/0x14 (and 0x0C/0x3C in other groups) and S2C 0x1D/0x1E/0x23/0x24/0x19/0x03:
```
u16 item_id  [u16 count where present]  u8 n  repeat(n){u16 w[i]}  u16 w5
```
The client computes `n` = number of **leading non-zero** words among w0..w4 and stops at the first zero. A word after a zero gap can never be referenced by the client, so **the server must keep sockets packed from w0** (FUN_004241d0 inserts into the first zero word, and 0x9C extraction shifts the rest down, so the client keeps them packed too). S2C readers zero the block first and do not bound `n`. Never send n > 5: n = 6 is silently lost, n >= 7 corrupts the stack, and 0x19/0x23 with n >= 63 kills the /GS cookie.

S2C 0x11/0x12 ground records use the same `u8 n + n*u16 + u16` block, but the buffer is zeroed **once per packet**. **Always send n = 5.**

Items granted by S2C 0x18 / 0x99-sub9 / 0x26 / 0x27 / 0x8D / 0x8F always get an all-zero block. Only 0x03, 0x13 pickup (copies the ground record's words), 0x1D/0x1E (returned item) and 0x8E create non-zero blocks.

### 1.4 Equipment grid (per entity)
`entity+0x13C` holds u16[25] ids: slots 0..15 regular, 16..24 cash/costume. `entity+0x16E` holds 25 x 12-byte blocks. Only slots 0..15 get blocks from S2C 0x07. FUN_00426680 picks the slot from def+0x1CC Kind and def+0x1F0; **that table is not yet reversed**. The server's `_KIND_TO_GRID` (line 73) was copied from PySlayer's KR slot order and covers only Kinds 5,6,9,10,11,15,16.

### 1.5 Ground items
The scene list is at scene+0x18/0x1C, with 0x40-byte records built by FUN_00423a10 (from S2C 0x11/0x12):
- +0 id, +2..+0xD block, +0x0E/+0x10 u16 x/y, **+0x12 u16 ground_id**, +0x14 qty, +0x16 state
- state values: 0 resting, 1 pop-in, which becomes 3 after 800 ms, 3 falling then 0, 2 picked, flies to the picker and is freed after 1 s
- +0x18 category, +0x1C owner uid, +0x20 source uid, +0x24 drop_time

Pickup is client-gated (key "Key3", default `W`): distance^2 < 625, owner in {0, self} or `drop_time + 15000 < scene+0xF1C`, and bag space. Both S2C 0x03 and 0x08 free the scene item lists, so ground items vanish client-side on every map load.

**Map clock scene+0xF1C** is set by S2C 0x03 `game_clock_ms` and 0x08 `game_time_ms`, and grows 30 per logic frame. Every C2S 0x0D carries `logic_elapsed_ms` (always a multiple of 30), so the server can track each client's clock exactly: `clock = value sent in last 0x03/0x08 + sum(logic_elapsed_ms since)`.

Today the server resets that clock on every map entry:
- `_build_pyslayer_opcode_03` always sends `game_clock_ms = 1000` (line 2043, labelled "x ?").
- `_handle_change_map` packs the uid (1) into the 0x08 `game_time_ms` slot (line 1282). The 0x03 that follows overwrites it.

Any server-chosen `drop_time` must be expressed in the receiving client's clock.

### 1.6 Timed actions
Crafting, reinforcement and gathering are client-timed (5000 ms progress windows 0x293 / 0x295). The client sends only the **completion** packet (0x67/0x68/0x69), with no start packet, and it sets a busy flag (`[app+0xD5C+0x2C]` craft/reinforce, `+0x28` gather) that the result packet clears (0x8D/0x8E clear +0x2C, 0x8F clears +0x28). According to the 0x68 / 0x8D / 0x8E specs, leaving one unanswered locks the feature with "Not allowed during concoction, mineral refining, or reinforcement."

**Spec conflict (Q16):** the C2S 0x67 spec says the programmatic close of progress window 0x293 also clears +0x2C through the window event, and the C2S 0x69 spec says the same for +0x28 via FUN_00446300 (0x446A26). The S2C 0x8F spec lists this as unconfirmed. Design for the pessimistic reading (always answer exactly once); T-C67 / T-C69 settle it.

In every result handler the **client applies the inventory mutation locally from its own catalog data**. The server must mirror it exactly and must not send extra add/remove packets.

### 1.7 Opcode map and status
| key | dir | name | server status | server code |
|---|---|---|---|---|
| 0x0F | C2S | EquipItem | partial (double bag removal, ignores sockets) | `_handle_equip_item` 1467-1527, dispatch 636 |
| 0x11 | C2S | UnequipItem | missing | falls into unhandled branch 659 |
| 0x13 | C2S | DropInventoryItem | missing | - |
| 0x14 | C2S | DropEquippedItem | missing | - |
| 0x15 | C2S | UseItem (type-0 branch) | partial (no 0x25) | `_handle_use_item` 1160-1231, dispatch 624 |
| 0x1F | C2S | GroundItemPickupRequest | missing | - |
| 0x67 | C2S | RefiningConcoctionComplete | missing | - |
| 0x68 | C2S | ReinforcementComplete | missing | - |
| 0x69 | C2S | GatheringComplete | missing | - |
| 0x72 | C2S | ElementalStoneExtract | missing | - |
| 0x11 | S2C | GroundItemSpawn | missing | - |
| 0x12 | S2C | GroundItemDrop | partial (builder wrong, unused) | `_build_opcode_12_drop` 1700-1722 |
| 0x13 | S2C | GroundItemPickedUp | partial (builder mislabelled, unused) | `_build_opcode_13` 1724-1730 |
| 0x18 | S2C | GetItem (gold+victy+grant) | implemented | `_build_opcode_18` 1055, `_send_drop` 2015 |
| 0x1D | S2C | EquipItem | partial (always zero block) | `_build_en_opcode_1D_equip` 2220-2236 |
| 0x1E | S2C | UnequipItemToBag | missing (wrong dead builder) | `_build_en_opcode_1E` 2238-2266 |
| 0x23 | S2C | InventoryItemRemove (silent) | implemented (misused on equip) | `_build_opcode_23` 1073-1105 |
| 0x24 | S2C | UnequipItemRemove | missing | - |
| 0x3F | S2C | GoldUpdate | missing | - |
| 0x41 | S2C | ItemBuffApply | missing | - |
| 0x42 | S2C | ItemBuffApplySelfRefresh | missing | - |
| 0x8D | S2C | CraftResult | missing | - |
| 0x8E | S2C | ReinforceResult | missing | - |
| 0x8F | S2C | GatherResult | missing | - |
| 0x9C | S2C | ElementalStoneExtractResult | missing | - |

Cross-group packets this system cannot work without: S2C 0x03 (bag lists, gold, capacities) and 0x07 (equipment grid) from world/login; 0x25 (item-use confirm) from skill; 0x19/0x0B/0x0C from shop_storage; 0x26/0x27 (client-side quest item grants) from quest; 0x6F (cash tab / tool serials) from premium_cash.

---

## 2. Request/response flows

Conventions: "self" = the requesting session; "observers" = other sessions on the same map (needs per-session uids, see bug B19). "Refuse" = send nothing unless stated. None of these C2S requests leave client-side pending state, except the busy flags in flows F11-F14.

### F1. Inventory seeding on world entry and on every map change (0x2B / 0x7E -> 0x03 + 0x07)
1. C2S 0x2B (enter world) or 0x7E (portal). The server loads the character's persistent inventory (section 3).
2. S2C **0x03** EnterWorldState:
   - `gold` = model gold (u64), `winnie_points` = victy (u32), capacities `equip_tab_slots/consume_tab_slots/etc_tab_slots` = model caps (1..45).
   - `equip_item_count` + list `{item_id, option_count = leading-nonzero(w0..w4), option_value[], item_extra = w5}`.
   - `consume_item_count` + `{item_id, u16 quantity}`, `etc_item_count` + `{item_id, u8 quantity}`.
   - Lists contiguous, <= 45 each. The handler memsets all four tabs before reading, so **an empty list wipes the client bag**.
3. S2C **0x07** for the local player: `repeat(16){equip_item_id, 6 x equip_item_attr}` = equipped slots 0..15 with their **real** blocks, and `repeat(9) cash_equip_item_id` = slots 16..24.
4. After the map is loaded, re-send ground items that exist on this map (F6 step 5) and remember `clock_base = game_clock_ms` for F6/F7.

Gate/hazard: 0x03 destroys every entity, so 0x07 must follow (the current code already does this at 733-752 / 1298-1304).

### F2. Grant item / change currency (server-initiated: shop buy, loot fallback, GM, quest money)
1. Server decides the grant: `item_id` must exist in the **EN catalog** (<= 4248 and present in hii), and there must be room by the tab rules in 1.2.
2. Server applies it to the model: type 0/2 stack rules, type 1 = one instance with a zero block (even if count > 1, the client adds exactly one), type 3 = learned skill (skill group), type 4 = class change.
3. S2C **0x18** `{gold: model gold (absolute), victy: model victy (absolute), item_id, count}` shows "you've received %s. (Count:%u)". Use `item_id = 0` for a currency-only update.
4. Gold-only change: S2C **0x3F** `{gold}` updates the label only, with no message and no inventory refresh.
- Refusal: bag full means do not send the item (the client would drop it silently and diverge); send a chat line instead.
- Do **not** send 0x18 for quest Send items (0x26) or quest Reward items (0x27), because the client grants those itself from hqi (bugs B2/B3).

### F3. Equip (C2S 0x0F EquipItem -> S2C 0x1D)
Client gates before sending (with a message and no packet on failure): local entity exists; gender def+0x1D4 vs scene+0x130; level scene+0x979 >= def+0x1D8 ("You need higher level"); not attacking or casting, i.e. entity+0x904 and +0x15B4 not in {1,4,0xE,0xF} and < 0x18, and scene+0x258 == 0 ("You can't equip or unequip while attacking."; a skill cast left unanswered blocks this forever, bug B17); Job[class] or Job[0] set ("You can't equip other class items."). The quick-slot path is throttled to 1000 ms per slot.
1. C2S **0x0F** `u16 item_id, u8 stone_count, stone_id[stone_count], u16 extra_option`. Live capture: `B3 00 00 00 00` = item 179, no stones.
2. Server: decode with the spec grammar and build the block `w = stones + zeros, w5 = extra_option`.
3. Validate: EN hii Type == 1; the bag equip tab holds an instance with id AND exact block; char level >= Lv; Job flag; Gender; not a duplicate request (the instance is still in the bag).
   - Refuse (no packet) if not owned. If the client plainly shows it but the server does not have it, send S2C 0x23 `{item_id, count 1, block}` to remove the phantom.
4. Resolve the slot = kind_slot(EN Kind, Cash flag) (needs item I-06). If the slot is occupied, the old instance goes back to the bag model. Room is guaranteed because the new instance just left the bag.
5. Model: remove the instance from the bag, set `equipped[slot] = instance`, append the old instance to the bag if there was one.
6. S2C **0x1D** to self: `{uid, item_id, stone_count = n, stone_id[] = w0..w(n-1), block_tail = w5}`, echoing the client's words. For the local player the client itself removes the matching bag entry, **puts the previously equipped item into the first empty bag slot**, refreshes windows 7/9 and swaps quick slots.
7. S2C 0x1D with the same body to observers (sprite and stat update only).
8. **Never send 0x23 after 0x1D** (live bug B1).

### F4. Unequip (C2S 0x11 UnequipItem -> S2C 0x1E)
Client gates: source slot index 3..27; equip-tab capacity <= 45 and an empty slot ("You have no more slots for the item."); not in the special UI mode ctx==0x259; the same attack/cast gate as F3; def Type == 1.
1. C2S **0x11** `u16 item_id, u8 enchant_count, enchant[], u16 enchant_last` (same shape as 0x0F), from a double-click on an equipped item or a drag from window 7 to window 9.
2. Validate: some `equipped[slot]` has id plus an exact block; the bag model equip tab has room. Re-sending is possible, so if the item is no longer equipped, refuse.
3. Model: `equipped[slot] = None`; append the instance to the bag equip tab.
4. S2C **0x1E** to self: `{uid, item_id, option_count = n, option_value[], option_extra = w5}`. The client clears the grid slot (id + memcmp), recomposes the sprite, recalculates max HP/MP, and adds the item to the first empty bag slot.
5. S2C 0x1E with the same body to observers (grid clear only; the bag add is gated on uid == their local uid).
- **Duplication hazard:** the 0x1E grid-removal result is ignored, so if the block differs from what the client stored, the item stays equipped AND a copy lands in the bag. Only ever echo the block the server itself sent in 0x07/0x1D.

### F5. Use consumable (C2S 0x15 UseItem -> S2C 0x25 [+ 0x42/0x41])
Client gates: def Type == 0; not in an arena/play room (FUN_00444700); item_id != 0xD1 (209 Arrow is never sent); per-item cooldown `def+0x1EC + last_use <= now`. No lock is set, so the client can spam until 0x25 starts the cooldown.
1. C2S **0x15** `u16 item_id`. The same opcode carries type-3 skill casts (spec 0x44C239/0x15, skill group), so dispatch on the EN hii Type.
2. Validate: Type 0; the consume tab holds qty >= 1; server cooldown `now >= last_use[item] + CT` (CT from hii, with a small tolerance, never shorter than the client's); not in arena.
   - Not owned: send S2C 0x23 `{item_id, count 1, n 0, w5 0}` if the client might still show it, plus a chat line.
   - On cooldown: refuse silently. The client will also reject a 0x25 inside its own cooldown, which would desync a server-side decrement.
3. Model: qty -= 1 (mirror FUN_00423b70: take it from the first same-id stack), `last_use = now`, HP/MP += hii HP/MP clamped to max.
4. S2C **0x25** `{item_or_skill_id = item_id}` to self. The client removes 1 from its bag, stamps the cooldown, plays effect/sound 0x29, and **applies the HP/MP deltas itself** from its own record.
5. Only as a resync, and only AFTER 0x25: S2C 0x28/0x44 absolute HP/MP. Sending them before 0x25 double-applies.
6. If hii `Con` (Duration) != 0 (EN type-0 items 148, 153): S2C **0x42** `{target_uid = self uid, source_uid = 0, item_id}` to self (buff icon plus stat panel refresh), and S2C **0x41** with the same body to observers.

### F6. Monster loot to the ground (kill -> S2C 0x12; map entry -> S2C 0x11)
1. Monster dies (`_kill_monster` 1911). S2C 0x29 first; the entity stays in the scene list in its dying/dead state.
2. Roll drops from the NPC's `gamedef npcs.item` list (4-digit decimal ids), **filtered to ids present in the EN catalog**. Gold is still credited via 0x18/0x3F (no ground gold object is known).
3. For each dropped item: allocate `ground_id` (u16, unique per map, 1..65535, skip ids in use); `owner_uid` = killer uid; `source_uid` = mob uid; x/y = mob position; qty; block = zeros; `drop_ms = monotonic`.
4. S2C **0x12** `{entry_count: N, entries: {item_id, quantity, x, y, ground_id, drop_time = recipient's map clock, owner_uid, source_uid, opt_count 5, 5 x opt_word, opt_tail}}` to every session on the map. The client places the item at the source entity (else the owner entity, else wire x/y) and pops it in (state 1).
   - Alternative when the position must be exact: S2C **0x11** with `state = 1` and the wire x/y (0x11 always uses the wire position).
5. On map entry (after F1), S2C **0x11** `{..., state = 0}` for every unpicked item on that map, with `drop_time` = that client's clock minus the ms elapsed since the drop (floor 0), so the 15 s owner window stays consistent.
6. Expiry (retail timeout unknown; suggest 60-120 s): S2C 0x13 `{ground_id, quantity 0, picker_uid = 0}` to the map. This marks the item picked and frees it after 1 s. It needs a live test first (T-S13b, crash risk if the fly-to-picker code dereferences a missing entity). The fallback is no expiry plus a per-map cap.

### F7. Pickup (C2S 0x1F -> S2C 0x13)
Client gates: in-world (scene+0xF00 == 6); pickup key down; not in UI mode 0x259; within 25 px; ownership ("You don't have ownership of this item."); bag space by category ("There isn't empty space in the inventory.").
1. C2S **0x1F** `u16 ground_item_uid`. Holding the key auto-repeats this, so the server must be idempotent.
2. Validate: the registry for the session's map has the id and it is not picked. Ownership: `owner == 0 or owner == uid or now - drop_ms >= 15000`. Room in the tab (Type 0: a same-id stack with qty+count <= 999 or a free slot; Type 1: a free slot; Type 2: <= 99 or a free slot).
   - Range cannot be checked server-side: ordinary 0x0D packets carry no position, only the interact block does.
   - On failure refuse silently; the client keeps showing the item.
3. Model: mark picked; add to the bag with the ground record's block.
4. S2C **0x13** `{ground_id, quantity, picker_uid}` to every session on the map, including the picker. The picker's client adds `quantity` (types 0/2) or one instance with the 6 words (type 1) and prints "you've received %s. (Count:%u)"; observers see the item fly away.
5. Then run quest collection credit (`_quest_credit_item` 1443).
- Hazard: if the picker's client bag is full, it still marks the item picked and loses it. The server's room check must use the same capacities it sent in 0x03.

### F8. Drop a bag item (C2S 0x13 -> S2C 0x23 + 0x12)
Client gates: not in arena/play room; source window 9; selection valid; def+0x1F0 == 0 (not cash); amount != 0; Type 0 amount <= 999, Type 2 <= 99; owned ("You've exceeded the amount you have."). The amount is truncated to u16 before the checks.
1. C2S **0x13** `u16 item_id, u16 amount, u8 opt_count, opt[], u16 opt6`.
2. Validate: EN hii `Cash` == 0 and `NotTrade` semantics if adopted; owns >= amount (type 1: an exact block instance, amount treated as 1).
3. Model: remove the item; register a ground item `{owner_uid = 0 (or the dropper, policy), source_uid = dropper uid, qty = amount, block}`.
4. S2C **0x23** `{item_id, count = amount, opt_count = n, opt[], opt_extra = w5}` to self (silent remove).
5. S2C **0x12** to the map with `source_uid = dropper uid`, so each client places the item at the dropper's live position. The server does not know player positions (see F7), so wire x/y is only a fallback.
- The retail reply pair is not visible statically. 0x23 + 0x12 is the design choice (open question Q5).

### F9. Drop an equipped item (C2S 0x14 -> S2C 0x24 + 0x12)
Client gates: same arena gate; source window 7; def+0x1F0 == 0; Type == 1.
1. C2S **0x14** `u16 item_id, u8 opt_count, opt[], u16 opt6` (no amount).
2. Validate: equipped with an exact block; not cash.
3. Model: `equipped[slot] = None`; register a ground item (qty 1, block).
4. S2C **0x24** `{uid, item_id, option_count, option_value[], option_extra}` to self and observers. The grid slot is cleared with **no** bag add.
5. S2C **0x12** to the map (source = dropper uid).

### F10. Server-side removals (S2C 0x23 / 0x24)
- Destroy, expire, or hand over to storage/trade from the bag: S2C 0x23 `{id, count, block}`. Removing equipment needs the exact block; stacks need count <= owned and count != 0.
- Destroy or expire an equipped item: S2C 0x24 `{uid, id, block}` to self and observers.
- Do **not** use 0x23 for quest Demand items on turn-in: S2C 0x27 already removes them client-side ("Gave %u item(%s).").

### F11. Concoction / Mineral Refining (C2S 0x67 -> S2C 0x8D)
Client gates at start: window 0x28D Concoction (near alchemy, interaction 0x13B) or 0x28E Mineral Refining (near smelter, 0x13C); a recipe selected ("Choose item."); the FUN_00424c80/00424d00/00424d60 checks by Type (the 0x1F spec shows these are **bag-space** checks, although the 0x67 spec calls them skill checks); not busy. After 5000 ms the client sends the packet and closes the progress bar without waiting.
1. C2S **0x67** `u16 product_item_id`. The mode is implied by hii `Union_Kind` (2 = Refining, else Concoction).
2. Validate the recipe: `Union`/`Union_Cnt` decode to 8 x (id, count) pairs. Ignore zero pairs.
   - result 0x0F if any material is short ("Short of material item"); nothing is consumed.
   - result 0x10 if the required skill is not learned ("The skill hasn't been learned yet."); which skill ids apply is open question Q9.
   - result 0x11 if there is no bag room for the product; **materials are consumed on both sides**.
   - Otherwise roll success (rate unknown, Q9): result 1 on success, 0 on failure (materials consumed).
   - Rate-limit to one completion per ~5 s per session (client-timed feature).
3. Model: results 1/0/0x11 remove materials by type (type 1 materials remove exactly 1 instance regardless of count); result 1 also adds 1 product (zero block).
4. S2C **0x8D** `{result, product_item_id}` (3 B). Always echo the client's id; an unknown id leaves the busy flag set. Send exactly once; no 0x23/0x18 for the same craft.

### F12. Reinforcement (C2S 0x68 -> S2C 0x8E)
Client gates at start: Reinforcement window 0x28C (near the smithy); equipment placed must be Type 1 with a free word among w0..w4; stone placed must be in the id range 0xB9F..0xBA4. In EN, 2975..2979 are the Elementirium grades (Chipped / Flawed / plain / Flawless / Perfect, Lv 7/14/25/49/59) and **2980 is "Melee Attack Booster" (Type 0)**, so the upper bound is probably exclusive (Q8). Stone Lv <= equipment Lv.
1. C2S **0x68** `u16 equip_item_id, u16 stone_item_id, 6 x u16 equip_option_word` (16 B; the words are the equipment's full block).
2. Validate: the bag equip tab has the instance (id + block; F12 operates on the **bag** tab `inv+0x380`, not on worn gear); the etc tab has the stone; stone Lv <= equip Lv; a zero word exists in w0..w4. Otherwise send result 0x11 ("This equipment cannot be reinforced.").
3. Roll. Success picks `new_option_item_id`. Hypothesis from the EN catalog: one of the elemental stones 2945..2974 ("Chipped/Flawed/-/Flawless/Perfect Elemental Stone - Earth/Wind/Fire/...", 5 grades per element, Lv 1..5), with the grade derived from the Elementirium grade (Q8). It must exist in the EN catalog or the client takes the failure path.
4. Success model: remove 1 stone; set the first zero word of w0..w4 to new_option_item_id.
5. S2C **0x8E**:
   - Success: `{result 1, equip_item_id, stone_item_id, 6 x equip_option = the OLD words, new_option_item_id}` (19 B). The client finds the slot by the old block, writes the new word and removes 1 stone.
   - Failure: `{result 0 or 0x11, equip_item_id, stone_item_id}` (5 B). The client consumes nothing, so the server consumes nothing either; if a retail stone loss is wanted, add an explicit 0x23 for the stone.

### F13. Gathering: Mining / Herb Gathering (C2S 0x69 -> S2C 0x8F)
Client gates: not already gathering; tool 0x8A7..0x8AA (2215..2218 Crude/plain/Durable/Exceptional Garden Shovel, Lv 3/26/56/86) for herb nodes, 0x8AB..0x8AE (2219..2222 Crude/plain/Durable/Exceptional Shovel, Lv 4/26/56/86) for mineral veins; interaction code 0x138 herb / 0x139 mining; node record type +0x644 = 2 herb / 1 mineral; skill 0x56 = item 86 "Herb Gathering" (Type 3) / 0x52 = item 82 "Mining" (Type 3) learned; tool grade check FUN_00427310; tool Lv <= player level. The tool is used through the Type-2 use path (no C2S 0x15). After 5 s the client sends 0x69 and closes window 0x295.
1. C2S **0x69** `u32 gather_node_record_index, u16 tool_item_id`. The index is 1-based into the client's per-map content-record container (the same index space as the 0x1A anim index).
2. Server maps the index to a node definition using the per-map container table (content gap, Q10). Node NPCs in gamedef: herb 118/123/124/125 (Lv 1/26/56/86), vein 128/127/126/122 (Lv 1/26/56/86), with drop lists in `npcs.item`.
3. Validate: the tool is owned (etc tab); the skill is learned (82/86); tool grade >= node grade.
   - Out of bag room: result 0x11 ("There isn't empty space in the inventory.").
   - Other failure: any value other than 1/0x11, e.g. 0 ("Failed to obtian item.").
4. Model: **remove 1 tool on every result** (the client does this whenever the tool id has a def); on result 1 add 1 reward (zero block). The reward must be in the EN catalog: filter out 4356 and similar from the node lists.
5. S2C **0x8F**: `{result 1, tool_item_id, reward_item_id}` (5 B), or `{result, tool_item_id}` (3 B). Always echo the tool id the client sent.

### F14. Elemental stone extraction (C2S 0x72 -> S2C 0x9C [+ 0x18])
Client gates: not busy; `mode_id != 0`; equipment placed ("Place your equipment") with at least one socket; for mode 0xF70/0xD6D a stone must be selected ("Select the elemental stone to extract.").
1. C2S **0x72** `u16 mode_id, u16 equip_item_id, u16 stone_item_id, 5 x u16 socket_stone_id, u16 equip_extra` (18 B).
   - **mode_id is the cash tool item id** (EN catalog, Type 5, Cash=1): 3952 (0xF70) = "attribute selective separation x1", 3437 (0xD6D) = "selective x6", 3951 = "random x1", 3436 = "random x6". Random tools send stone 0.
2. Validate: the player owns the cash tool (premium-cash inventory record and serial, from the premium_cash group); the bag equip tab has the instance (id + words); for selective tools the stone is in the sockets; for random tools the server picks a **non-zero** filled socket.
3. Model: remove that word and shift the later words down; consume one use of the tool; add the extracted stone to the etc tab.
4. S2C **0x9C** success: `{result 1, equip_item_id, socket_stone[5] = words BEFORE extraction, socket_extra = w5, stone_id (never 0), cash_item_serial}` (21 B); failure: `{result 0}` (1 B, "Elemental stone extraction failed...").
   - **stone_id 0 with all sockets empty = client divide-by-zero crash** (0x42430D).
5. S2C **0x18** `{gold, victy, item_id = stone_id, count 1}`. 0x9C does not deliver the stone.

### F15. Item buffs (S2C 0x41 / 0x42)
The caller is F5 (a consumable with Duration) or a scroll used on another player. S2C 0x42 goes to the target when the target is the local player (it also refreshes the stat panel); S2C 0x41 goes to everyone else who sees the target. Body `{target_uid, source_uid (0 = self-cast), item_id}` (10 B). The target entity must already exist on the receiver, otherwise the rest is not read. Max 21 buff slots. Buff expiry and removal packets belong to the skill group (0x3C/0x43).

---

## 3. Server state and data model

### 3.1 Content (read-only)
- **ItemCatalog** loaded from `WindSlayer2Game/hs/windslayer.hii` (decode as above; ~2 MB, 4248 records). Fields: `type, kind, spr_num, gender, lv, job[7], hp, mp, ct, con, buy, sell, cash, union[8x(id,cnt)] (decimal), union_kind`. It replaces `_gamedef_item` (49-65, KR `items`, wrong Kind for 289 items) and `data/items.json` (used by `_handle_use_item` 1182) as the authority for EN ids. The gamedef `items` table stays usable only for columns hii lacks (`NotTrade`, `PvPItem`) and only for idx <= 4248.
- **QuestCatalog** from `hs/windslayer.hqi` (or gamedef `quests` decoded as **decimal**: Send/Demand 10 x 4 digits, Send_Num/Demand_Num 10 x 3 digits, Reward 20 x 4 digits, Reward_Num 20 x 3 digits). The server only needs it to mirror what 0x26/0x27 do client-side.
- **Drop tables**: gamedef `npcs.item` (4-digit decimal list), filtered to the EN catalog. `MONSTER_DB` (113-128) hard-codes the same lists including 4356.
- **Gather nodes**: gamedef npcs type 1 (vein) / type 2 (herb, UI 662/663) with `Lv` = grade, plus a per-map table `container_index -> npc idx` (missing content, Q10).
- **Kind -> equip slot table** for FUN_00426680 (missing RE, I-06).

### 3.2 Per-character persistent state
The server now saves only in `_handle_create_character` at 2612. Everything below must survive relog: store it in the character dict in `accounts.json` or a sqlite table.
```
char['wallet']   = {'gold': u64, 'victy': u32}
char['bag_caps'] = {'equip': 35, 'consume': 35, 'etc': 35}          # 1..45, sent in 0x03
char['bag'] = {
  'equip':   [ {'id': 179, 'w': [0,0,0,0,0,0]} , ... ],              # ordered, compacted, len <= caps
  'consume': [ {'id': 5,   'qty': 3} , ... ],                          # qty 1..999
  'etc':     [ {'id': 281, 'qty': 10}, ... ],                          # qty 1..99
}
char['equipped'] = { slot(0..24): {'id': 179, 'w': [six words]} }      # slots 16..24 id only
char['cash_items'] = [...]  # premium_cash group (serials for 0x9C, 0x6F)
```
Invariants (mirroring the client):
- Equipment matching uses id + all 6 words.
- Words w0..w4 are kept packed.
- Add: type 0/2 merge into the **first** same-id stack with room, else append if below capacity, else fail. Type 1 always appends one instance.
- Remove: compact, i.e. shift later slots down (0x19 spec). This matters for the 0x03 list order.
- Never let a model mutation happen when the matching S2C would be ignored client-side: unknown id, full tab, capacity mismatch.

### 3.3 Session (volatile) state
- `session['uid']`: unique per character. It is currently always 1 (`account_id` = 1), which blocks every broadcast in this group (B19).
- `session['map']` (exists as `current_map`), `session['clock_base']` (the value sent in the last 0x03/0x08), `session['clock']` (+= `logic_elapsed_ms` of each C2S 0x0D; `_handle_world_sync` 669-684 currently discards the packet).
- `session['use_cd'] = {item_id: monotonic_ms}` for consumable cooldowns.
- `session['craft_last_ms']` and `session['gather_last_ms']` for rate limits of the client-timed features.

### 3.4 World (shared) state
```
self.ground[map_id] = { ground_id(u16): GroundItem(id, qty, w[6], x, y, owner_uid, source_uid,
                                                    drop_ms, picked=False, expires_ms) }
self.next_ground_id[map_id] = 1..65535 (wraps, skips live ids)
```
`GROUND_ITEM_UID_BASE = 0x00200000` (line 140) must go: the wire field is u16.

### 3.5 Packet construction
Build every packet in this group from the spec grammar (`wsproto.Grammar(spec['grammar']).encode(rec)`, specs loaded once from `re_tools/corpus/protocol_spec.json`), and decode C2S the same way. The repeat keys are `repeat[<count field>]`, and **field names differ per opcode**, so keep one small adapter per opcode:

| opcode | count / words / tail field names |
|---|---|
| C2S 0x0F | `stone_count` / `repeat[stone_count]:[{stone_id}]` / `extra_option` |
| C2S 0x11 | `enchant_count` / `enchant` / `enchant_last` |
| C2S 0x13, 0x14 | `opt_count` / `opt` / `opt6` |
| S2C 0x1D | `stone_count` / `stone_id` / `block_tail` |
| S2C 0x1E, 0x24 | `option_count` / `option_value` / `option_extra` |
| S2C 0x23 | `opt_count` / `opt` / `opt_extra` |
| S2C 0x11, 0x12 | per entry `opt_count` (always 5) / `opt_word` / `opt_tail` |
| S2C 0x8E | `repeat[6]:[{equip_option}]` + `new_option_item_id` |
| S2C 0x9C | `repeat[5]:[{socket_stone}]` + `socket_extra`, `stone_id`, `cash_item_serial` |

Verified with wsproto for this doc:
- 0x1D `{uid 1, item 179, n 0}` = `01 00 00 00 b3 00 00 00 00`
- 0x8E success = 19 B
- 0x8F success = `01 ab 08 1f 01`
- 0x9C success = 21 B
- 0x11 with one entry and 5 words = 37 B
- 0x12 = 36 B

---

## 4. Current implementation status and proven bugs

| # | severity | opcode | bug | evidence | fix |
|---|---|---|---|---|---|
| B1 | wrong_behavior | C2S 0x0F / S2C 0x1D, 0x23 | Equipping one of two sticks removes both from the bag. For the local player, 0x1D already removes the matching bag entry (0x452BC2 FUN_00423FF0 delta -1); the server then sends 0x23, which removes the second. | LIVE_TEST_LOG bug 5 plus its "Bug root causes"; `_handle_equip_item` 1512-1520 sends 0x1D then 0x23 | Delete the 0x23 send (1515-1523). The bag model removes the instance and re-adds the previously equipped one (F3). |
| B2 | wrong_behavior | S2C 0x26 / 0x18 | Quest accept duplicates Send items ("you've received Wooden Stick" twice, 2 sticks). The client grants hqi Send items itself on 0x26 (FUN_00440920 loop over +0xC94); the server also sends 0x18 per item. | LIVE_TEST_LOG bug 4; spec S2C 0x26 hazard (2); EN hqi quest 26 Send = `0179` x `001`; `_handle_accept_quest` 1362-1372 | Stop sending 0x18 for `q['send']`; only mirror the add in the bag model (skip if the client's add would fail). Same for `_complete_quest` 1427-1434: 0x27 grants Reward items and Money and removes Demand items client-side (mirror only; exp via 0x21 stays). |
| B3 | wrong_behavior | (quest content feeding 0x18/0x26) | `quest_defs.py` parses item-id fields as hex and Reward as 10 x 4 / 10 x 6: `_ids` 14-16 `int(...,16)`, `_num3` 18-20, `_num6` 22-24. Consequences for quest 26 (`Send 0179`/`001`, `Reward 0005 0094`/`005 001`): Send becomes item 377 (a Lv-53 skill), which the `_EN_ITEM_OVERRIDE = {377: 179}` hack (80-83) papers over; Reward entry 2 becomes id 0x94 = 148 with count 0 and is dropped, so the Double Jump (94) reward is lost. | EN hqi and gamedef store the same decimal strings. Of 556 EN hqi item refs, 0 fall outside the catalog when read as decimal and 197 when read as hex. | Parse decimal: Send/Demand 10 x 4, *_Num 10 x 3, Reward 20 x 4, Reward_Num 20 x 3; delete `_EN_ITEM_OVERRIDE`/`_en_item`. (Quest group owns the code; it changes item grants.) |
| B4 | wrong_behavior | S2C 0x18 | Monster drop 4356 (Random Cube Fragment) never arrives, yet `_inv_add` records it server-side. The EN catalog has 4248 items and handlers ignore ids past the table. | LIVE_TEST_LOG bug 6 plus injection test (4356 no-op, 179 ok); hii header `Number_of_ITEM: 4248`; `MONSTER_DB` 113-128 lists 4356 for every mob; `_kill_monster` 1918-1922 | Validate every granted id against the EN catalog (I-01); filter drop lists; never mutate the model for an id the client will ignore. |
| B5 | crash_or_desync | S2C 0x03 | **Every portal wipes the client bag.** 0x03 memsets all item tabs and the server always sends 4 zero list counts, on enter-world (733) and map change (1298-1300). The server model (`session['inventory']`, 1137) keeps the items, so client and server diverge after the first portal; on relog everything is lost (session-only, never saved). | spec S2C 0x03 grammar (`equip_item_count`...) and semantics ("after memset"); `_build_pyslayer_opcode_03` 2074-2079 | Populate the 0x03 lists and wallet from the persistent model (I-04/I-05). |
| B6 | wrong_behavior | S2C 0x03 / 0x18 | Gold/victy disagree: 0x03 sends gold 100000 (2044) and winnie 0 (2047), while `_wallet` defaults are 999999/999999 (994-995). The client shows 100000 until the first 0x18 jumps it to 999999+. | code lines cited | Both packets read one persistent wallet. |
| B7 | wrong_behavior | C2S 0x0F / S2C 0x1D | `_handle_equip_item`: (a) reads only `u16 item` (1493) and always answers with a zero block (`_build_en_opcode_1D_equip` 2234-2235), so socketed gear no longer matches and 0x1D fails its bag lookup (sprite changes, bag keeps the item); (b) sends 0x1D even when the server bag lacks the item (1513, before `_inv_has` 1517), a free-equip exploit; (c) overwrites `equip_grid[grid_idx]` (1508) without returning the old item to the model, while the client does return it; (d) `_KIND_TO_GRID` (73) ignores EN Kinds 0,3,7,8,12,14,17 (not persisted to 0x07) and uses KR Kind (`_gamedef_item`), which differs for 289 EN items; (e) no Lv/Job/Gender validation. | spec C2S 0x0F references and fields; S2C 0x1D gates; hii vs gamedef Kind comparison | Rewrite per F3 (I-07). |
| B8 | crash_or_desync | S2C 0x07 | The 0x07 equipment grid always sends 6 zero attr words (2355-2358). After any reinforcement, the next map change stores a zero block, so a later 0x1E cannot clear the slot but still adds a bag copy (duplication), and 0x24 does nothing. | spec S2C 0x1E DUPLICATION HAZARD; `_build_en_opcode_07` 2351-2358 | Emit the persisted `equipped[slot]['w']` (I-05). |
| B9 | wrong_behavior | C2S 0x11 | Unequip is impossible: `_dispatch` (601-667) has no 0x11 branch, so the double-click logs "Unhandled opcode" and the item stays worn. | spec C2S 0x11 server_status missing | Add `_handle_unequip_item` (F4, I-08). |
| B10 | wrong_behavior | C2S 0x15 / S2C 0x25 | Potions: no 0x25 is sent, so the client bag count never drops and the cooldown never starts (infinite client-side stack). The server "gate" at 1195-1198 is ineffective: `_inv_remove` pops the key at 0 (1153), so `item in inv` is False and use continues forever. HP/MP use absolute 0x28/0x44 (1228-1231); once 0x25 is added, the client applies the delta too, so those must come after 0x25 or be dropped. Uses `data/items.json` (1182) instead of the EN catalog. | spec C2S 0x15 references; S2C 0x25 gates 2 and 5; code lines | Implement F5 (I-09). |
| B11 | wrong_behavior | C2S 0x1F; S2C 0x11/0x12/0x13 | Ground items unusable: no 0x1F handler; `_build_opcode_12_drop` (1700-1722) writes item,x,y,count,0,u32 uid,u32 x,u32 0 while the client reads item,quantity,x,y,ground_id,drop_time,owner,source (so quantity=x, x=y, y=1, ground_id=0, drop_time=0x200000 (the 15 s owner lock never lapses), owner=x, which explains the old "misaligned" and "You don't have ownership" notes); `_build_opcode_13` (1724-1730) labels the quantity field "item"; `GROUND_ITEM_UID_BASE` 0x200000 (140) truncates to 0 in u16. | specs S2C 0x12/0x13 references; C2S 0x1F references | Replace with grammar builders plus a ground registry (F6/F7, I-10). |
| B12 | wrong_behavior | C2S 0x67/0x68/0x69/0x72 | Crafting, reinforcement, gathering and extraction are never answered, so the client busy flags ([ctx+0x2C] / [ctx+0x28]) stay set and every later attempt shows "Not allowed during concoction, mineral refining, or reinforcement." until relog. | spec 0x68 gates ("busy then stays 1 until a 0x8E arrives"), 0x8D/0x8F gates; `_dispatch` has no branch. The 0x67/0x69 specs disagree on whether closing the window already clears the flag (Q16); either way the client inventory is never updated. | Implement F11-F14. As an interim step, answer 0x67 with 0x8D(0x0F, id), 0x68 with 0x8E(0x11, eq, stone), 0x69 with 0x8F(2, tool) and 0x72 with 0x9C(0), all no-consumption failures except 0x8F (the tool is consumed client-side, so mirror it). |
| B13 | crash_or_desync (latent) | S2C 0x1D / 0x1E | Dead builders are wrong: `_build_en_opcode_1D` (2192-2218) sends class as item_id and N=14 (overflows the 12-byte block into stack locals); `_build_en_opcode_1E` (2238-2266) claims "persistent equip merge" with 14 values, while the real handler REMOVES the item and 14 words corrupt the record. `_build_opcode_23` docstring (1079-1101) says 0x404210 returns a bag element; it is the catalog lookup. | spec S2C 0x1D/0x1E/0x23 references | Delete both builders; fix the docstring (I-15). |
| B14 | wrong_behavior | S2C 0x18 | `_handle_buy_item` (1024) and `_send_drop` callers `_inv_add(count)` for Type-1 items, but the client adds exactly one equipment slot per 0x18 regardless of count. | spec S2C 0x18 field `count` | Model add follows the client rule (count forced to 1 for type 1, or send N packets). |
| B15 | wrong_behavior | C2S 0x0C (shop, cross-ref) | `_handle_sell_item` reads `count = payload[2]` (u8) (1039); the EN grammar is `u16 item, u16 qty, u8 socket_count, sockets, u16 extra`, so qty >= 256 is misread and equipment sells ignore the instance block (0x19 needs it). | spec 0x46A679/0x0C | shop_storage group: decode by grammar; echo the block in 0x19. |
| B16 | cosmetic | (memory) | reference_combat_render.md says 0x1D "keeps the item IN THE BAG" and "0x23 did nothing because no bag element". Both are superseded: 0x1D removes the bag entry when the block matches, and 0x23 looks up the catalog, not the bag. | live bug 5; spec 0x1D/0x23 | Update the notes when implementing. |
| B17 | wrong_behavior | C2S 0x15 (skill variant) | `_handle_use_item` ignores Type-3 skill casts (1188-1191), so scene+0x258 stays set and **all equip/unequip attempts fail** with "You can't equip or unequip while attacking." after the first cast. | PROTOCOL.md "C2S 0x15 — UseSkill" (line 7954) gates and hazard; spec C2S 0x0F hazard; S2C 0x5F SkillRequestUnlock semantics | skill group answers 0x25 or 0x5F; the dispatcher routes 0x15 by hii Type. |
| B18 | wrong_behavior | (persistence) | Inventory, equipment and wallet live only in the session dict (`_inventory` 1137, `equip_grid` 1508, `gold` 1023) and the session is deleted on disconnect (578-580). `char[char_key] = Spr_Num` (1510) mutates accounts in memory but `_save_accounts` is only called at 2612. | code | Persist per 3.2 (I-04). |
| B19 | crash_or_desync (multiplayer) | S2C 0x1D/0x1E/0x24/0x12/0x13/0x41 | Every session uses uid 1 (`account_id` = 1, `char.get('uid',1)`), so broadcasting equip or pickup packets would edit the receiver's OWN local player, since uid == scene+0x220 on every client. | memory project_local_player_spawn / multiclient; spec 0x1D/0x13 local-uid branches | Unique uids first (login group); only then add observer broadcasts. |

---

## 5. Implementation plan
IDs are referenced in the structured summary. Order = dependency order within each priority.

- **I-01 item_inventory-en-catalog (P0, M)**: `ItemCatalog` parses `hs/windslayer.hii` (decode, parse `key: value` lines; `Job`/`Job2` are space-separated lists; `Union`/`Union_Cnt` are 8 x 4-digit decimal). API: `get(id)`, `exists(id)`, `tab_of(type)`. Replace `_gamedef_item` and `item_table` use in the item paths. Filter `MONSTER_DB` drop lists and quest grants through `exists`. Opcodes: 0x18, 0x15, 0x0F.
- **I-02 item_inventory-no-double-remove (P0, S)**: remove the 0x23 after 0x1D in `_handle_equip_item`. Opcodes 0x0F, 0x1D, 0x23.
- **I-03 item_inventory-quest-grant-mirror (P0, S; depends I-01)**: decimal parsing in quest_defs; delete `_EN_ITEM_OVERRIDE`; stop 0x18 for Send (accept) and Reward (turn-in); mirror the adds/removes in the model; keep 0x21 exp. Coordinate with the quest group. Opcodes 0x26, 0x27, 0x18.
- **I-04 item_inventory-model-persist (P0, L; depends I-01)**: `Inventory` class per 3.2 with the client's tab rules (stack 999/99, compaction, capacity 1..45, exact-block matching, packed sockets), wallet, equipped slots; load/save in accounts.json (or sqlite) on every mutation or on a timer plus disconnect. Migrate the `_inv_*` helpers (1137-1158) onto it.
- **I-05 item_inventory-seed-03-07 (P0, M; depends I-04)**: fill 0x03 wallet, capacities and the three lists; fill the 0x07 16-slot grid (ids + words) and 9 cash ids from `equipped`; build both from grammars. Opcodes 0x03, 0x07.
- **I-06 item_inventory-kind-slot-table (P1, S)**: RE FUN_00426680 (Kind at def+0x1CC and def+0x1F0 to slot). Alternatively derive live: for one item of each EN Kind, inject 0x18 then 0x1D and read entity+0x13C..+0x16D with wsview/memory. Deliverable: `KIND_TO_SLOT` covering EN Kinds 0,3,5..12,14..17 (rings and earrings may take two slots).
- **I-07 item_inventory-equip (P1, M; depends I-04, I-06)**: F3 in full: grammar decode, validation, swap, 0x1D echo with the client's words, no 0x23.
- **I-08 item_inventory-unequip (P1, M; depends I-07)**: `_handle_unequip_item` for C2S 0x11 answered with 0x1E (F4).
- **I-09 item_inventory-use-consumable (P1, M; depends I-04)**: F5: route 0x15 by Type; 0x25; cooldown; HP/MP bookkeeping without pre-0x25 absolutes; 0x42/0x41 for Con > 0 (depends on I-13 for the builders).
- **I-10 item_inventory-ground-loot-pickup (P1, L; depends I-04; broadcast part depends on I-17)**: ground registry (3.4), u16 ids, per-session map clock from 0x0D `logic_elapsed_ms`, 0x12 on kill (replaces direct-to-bag loot in `_kill_monster` 1917-1927), 0x11 on map entry, C2S 0x1F answered with 0x13 (idempotent), expiry policy after T-S13b.
- **I-11 item_inventory-drop-bag-item (P2, M; depends I-10)**: C2S 0x13 answered with 0x23 + 0x12 (F8).
- **I-12 item_inventory-drop-equipped (P2, S; depends I-10, I-07)**: C2S 0x14 answered with 0x24 + 0x12 (F9).
- **I-13 item_inventory-buff-and-gold-builders (P2, S)**: grammar builders for 0x3F, 0x41, 0x42; use 0x3F for gold-only changes (quest money mirror, fees).
- **I-14 item_inventory-interim-craft-replies (P1, S)**: until I-16/I-18/I-19 land, answer 0x67/0x68/0x69/0x72 with the failure replies listed in B12 so the client never locks.
- **I-15 item_inventory-dead-code-cleanup (P3, S)**: delete `_build_en_opcode_1D`, `_build_en_opcode_1E`, `_build_opcode_12_drop`, `_build_opcode_13`; fix the `_build_opcode_23` and `_build_en_opcode_1D_equip` docstrings; fix memory note reference_combat_render.md.
- **I-16 item_inventory-crafting (P2, M; depends I-01, I-04, I-14)**: F11 with the `Union` recipes, rate limit, result codes 1/0/0x0F/0x10/0x11.
- **I-17 item_inventory-observer-broadcast (P1, M; depends on login-group unique uids)**: `sessions_on_map(map)` helper; broadcast 0x1D/0x1E/0x24/0x12/0x11/0x13/0x41.
- **I-18 item_inventory-reinforcement (P2, M; depends I-04, I-14)**: F12 (needs the Q8 answers for the new-option id and rates).
- **I-19 item_inventory-gathering (P2, L; depends I-04, I-14, Q10 content)**: F13 plus the per-map node table.
- **I-20 item_inventory-stone-extraction (P3, M; depends I-04, premium_cash cash-item inventory)**: F14 plus the 0x18 stone grant.
- **I-21 item_inventory-harness-drag-dblclick (P2, S)**: add `dblclick x y` and `drag x1 y1 x2 y2` to wsview.py so C2S 0x11/0x13/0x14 (and bag double-clicks) can be captured reliably. Tooling only.

---

## 6. Live test plan
Common prerequisites: `python wsdev.py up` (in-world on map 101, uid 1). Open the bag with `python wsdev.py key i` and find slot coordinates with `python wsdev.py shot`. Get the player position from `python wsdev.py state` (player uid 1, x/y).

Injected S2C packets change **only the client**: the server model does not learn about them, and any portal (0x03 with empty lists) wipes the bag again until I-05 lands. Run the S2C tests in the order shown, because later ones reuse items granted earlier. Current gold should be passed in 0x18 so the label does not jump (use 999999).

### 6.1 S2C (inject)
| id | opcode | action | expected | risk |
|---|---|---|---|---|
| T-S3F | 0x3F | `python wsdev.py sendspec 3F '{"gold":123456}'` then `key i` + `shot` | Inventory gold label shows 123456; no chat line | safe |
| T-S18 | 0x18 | `sendspec 18 '{"gold":999999,"victy":777,"item_id":5,"count":3}'`, then again with `"item_id":4356,"count":1`, then `"item_id":179,"count":1` | Herb x3 in the consumable tab plus "you've received ... (Count:3)"; 4356 gives nothing (confirms the catalog bound); stick in the equipment tab. Victy label 777. | state_change |
| T-S23 | 0x23 | `sendspec 23 '{"item_id":5,"count":1,"opt_count":0,"opt_extra":0}'` | Herb stack 3 becomes 2, no message | state_change |
| T-S1D | 0x1D | (stick in bag from T-S18) `sendspec 1D '{"uid":1,"item_id":179,"stone_count":0,"block_tail":0}'` | Stick in hand, bag stick removed **exactly once**, Equipment window shows it (proves B1 root cause) | state_change |
| T-S1E | 0x1E | after T-S1D: `sendspec 1E '{"uid":1,"item_id":179,"option_count":0,"option_extra":0}'` | Stick leaves the hand and the equipment window, reappears in the bag (one copy) | state_change |
| T-S1Eneg | 0x1E | with the stick equipped: `sendspec 1E '{"uid":1,"item_id":179,"option_count":1,"repeat[option_count]":[{"option_value":7}],"option_extra":0}'` | Documents the dup hazard: stick STAYS equipped AND a second stick appears in the bag. Relog/portal afterwards. | state_change |
| T-S24 | 0x24 | re-equip via T-S1D, then `sendspec 24 '{"uid":1,"item_id":179,"option_count":0,"option_extra":0}'` | Stick disappears from hand/equipment, NOT added to the bag | state_change |
| T-S11 | 0x11 | `sendspec 11 '{"entry_count":1,"repeat[entry_count]":[{"item_id":5,"quantity":2,"x":1411,"y":714,"ground_id":100,"drop_time":0,"owner_uid":0,"source_uid":0,"opt_count":5,"repeat[opt_count]":[{"opt_word":0},{"opt_word":0},{"opt_word":0},{"opt_word":0},{"opt_word":0}],"opt_tail":0,"state":0}]}'` (replace 1411/714 with the player's x/y from `wsdev.py state`, truncated to integers) | Herb icon lying at the player's feet (37 B packet) | state_change (crash_risk if opt_count >= 7, never do that) |
| T-C1F | C2S 0x1F | after T-S11: `python wsdev.py cap 2 hold w 300` | C2S 0x1F `64 00` (possibly repeated while held); no server reply today; item stays | safe |
| T-S13 | 0x13 | after T-S11: `sendspec 13 '{"ground_id":100,"quantity":2,"picker_uid":1}'` | Item flies to the player, "you've received ... (Count:2)", herb stack +2 | state_change |
| T-S12 | 0x12 | `sendspec 12 '{"entry_count":1,"repeat[entry_count]":[{"item_id":287,"quantity":1,"x":0,"y":0,"ground_id":101,"drop_time":0,"owner_uid":1,"source_uid":1,"opt_count":5,"repeat[opt_count]":[{"opt_word":0},{"opt_word":0},{"opt_word":0},{"opt_word":0},{"opt_word":0}],"opt_tail":0}]}'`; on map 102 repeat with `source_uid` = a Pupu uid (0xF0000 = 983040) | Item pops in at the player's feet (source entity lookup) even though the wire x/y is 0; second run appears on the monster. Then `hold w 300` to pick up: owner 1 is allowed. | state_change |
| T-S13b | 0x13 expiry | spawn with T-S11 (ground_id 102), then `sendspec 13 '{"ground_id":102,"quantity":0,"picker_uid":0}'` | Desired: the item vanishes about 1 s later with no bag change. Watch for a client crash (picker entity 0). Decides F6 step 6. | crash_risk |
| T-S42 | 0x42 | `sendspec 42 '{"target_uid":1,"source_uid":0,"item_id":148}'` | Buff icon for ~10 s plus a stat panel refresh. Proves hii `Con` = Duration (def+0x1C8). If nothing appears, try item 2499 (skill, Con 8010). | state_change |
| T-S41 | 0x41 | `sendspec 41 '{"target_uid":1,"source_uid":0,"item_id":148}'` | Same buff icon, no stat-panel refresh | state_change |
| T-S8D | 0x8D | (a) `sendspec 8D '{"result":15,"product_item_id":2975}'`; (b) grant `sendspec 18 '{"gold":999999,"victy":0,"item_id":281,"count":10}'` then `sendspec 8D '{"result":1,"product_item_id":2975}'` | (a) result window 0x28F "Short of material item ... ConcoctionFailed.", no inventory change; (b) 10 Eledust (281) removed, 1 Chipped Elementirium (2975) added, "Concoction Successful." (Union_Kind 1 means Concoction) | disruptive (result window) |
| T-S8E | 0x8E | stick in the BAG (T-S18 or T-S1E), 2975 in the bag (T-S8D b): `sendspec 8E '{"result":1,"equip_item_id":179,"stone_item_id":2975,"repeat[6]":[{"equip_option":0},{"equip_option":0},{"equip_option":0},{"equip_option":0},{"equip_option":0},{"equip_option":0}],"new_option_item_id":2945}'`; failure variant `'{"result":17,"equip_item_id":179,"stone_item_id":2975}'` | Success: "Reinforcement Successful." window, 2975 count -1, the stick tooltip shows a socketed stone. Verify with T-S1D using `"stone_count":1,"repeat[stone_count]":[{"stone_id":2945}]` (equips only if the block now matches). Failure: "This equipment cannot be reinforced.", no change. | disruptive |
| T-S8F | 0x8F | grant a shovel `sendspec 18 '{"gold":999999,"victy":0,"item_id":2219,"count":2}'`; then `sendspec 8F '{"result":1,"tool_item_id":2219,"reward_item_id":287}'`; then `sendspec 8F '{"result":0,"tool_item_id":2219}'` | First: shovel -1, +1 item 287 with a message. Second: shovel -1, "Failed to obtian item." (tool consumed on failure) | state_change |
| T-S9C | 0x9C | only after T-S8E succeeded (stick w0 = 2945): `sendspec 9C '{"result":1,"equip_item_id":179,"repeat[5]":[{"socket_stone":2945},{"socket_stone":0},{"socket_stone":0},{"socket_stone":0},{"socket_stone":0}],"socket_extra":0,"stone_id":2945,"cash_item_serial":0}'`; failure `sendspec 9C '{"result":0}'` | Modal "Extracted elemental stone."; the stick's socket is cleared; no stone added (0x9C does not grant it). Failure: modal "...extraction failed...". **Never send stone_id 0 while all sockets are empty (divide-by-zero crash).** | crash_risk (if misused) / disruptive |

### 6.2 C2S (capture)
| id | opcode | action | expected capture | risk |
|---|---|---|---|---|
| T-C0F | 0x0F | stick in bag (T-S18); double-click its slot. wsview.py has no `dblclick` yet, and two separate `click` processes are probably slower than the Windows double-click time. So either double-click by hand while `python wsdev.py cap 8 shot` waits (`shot` is a harmless action), or use `cap 3 dblclick X Y` after I-21. (The 2026-09-17 capture was a real double-click.) | `0x0F 5B B3 00 00 00 00`; with current server: 0x1D 9B + 0x23 7B (B1). After I-02/I-07: 0x1D only. Socketed variant after T-S8E: `B3 00 01 81 0B 00 00` (7 B). | state_change |
| T-C11 | 0x11 | open the Equipment window (window 7; find the key with `key c`/`key i` + `shot`), double-click the worn stick under `cap 3` | `0x11 5B B3 00 00 00 00`; today "Unhandled opcode 0x11" in logs, stick stays worn (B9). After I-08: S2C 0x1E 9B and the stick back in the bag. | state_change |
| T-C13 | 0x13 | drag a herb from the bag onto the ground (needs a manual drag, or I-21), set amount 1, OK, under `cap 5` | `0x13 7B 05 00 01 00 00 00 00`; today no reply and the item stays. After I-11: 0x23 7B + 0x12. Also test the arena gate message in a play room. | state_change |
| T-C14 | 0x14 | drag the worn stick from window 7 onto the ground, OK | `0x14 5B B3 00 00 00 00`; after I-12: 0x24 + 0x12 | state_change |
| T-C15 | 0x15 | herb in bag (T-S18): double-click it (or put it on quick slot 1 and `key 1`) under `cap 2` | `0x15 2B 05 00`; today only 0x28 (HP) and the herb count unchanged (B10). After I-09: 0x25 2B `05 00`, stack -1, cooldown 4000 ms (a second press within 4 s sends nothing). Arrow 209 never sends. | state_change |
| T-C1F | 0x1F | see 6.1 (after T-S11) | `64 00` | safe |
| T-C67 | 0x67 | needs a map with an alchemy station (Q11): grant materials (281 Eledust x10), open Concoction near the station, pick 2975 Chipped Elementirium, start, wait 6 s under `cap 8 ...` | `0x67 2B 9F 0B`; today no reply. Then start a second craft: if it shows "Not allowed during concoction...", the result packet is what releases the lock; if it starts, the window close already cleared it. This settles Q16. | disruptive |
| T-C68 | 0x68 | at the smithy (Q11): place stick (Lv 1) and 2975 (Lv 7). The client should refuse (stone Lv > equip Lv); use a Lv >= 7 weapon for the positive case. Reinforce, wait 6 s. | `0x68 16B <equip id> 9F 0B 00 00 x6`; today no reply and the lock stays | disruptive |
| T-C69 | 0x69 | learn Herb Gathering: `sendspec 18 '{"gold":999999,"victy":0,"item_id":86,"count":1}'` ("You've learned skill(Herb Gathering)."); grant garden shovel 2215; stand at a common herb node (npc 118, map Q10); use the shovel; wait 6 s under `cap 8` | `0x69 6B <u32 node index> A7 08` (record the node index for Q10); today no reply. Use the shovel again to check whether the gather lock stays (Q16). | disruptive |
| T-C72 | 0x72 | needs cash tool 3952 in the cash tab (premium_cash 0x6F) and a socketed item (T-S8E); open extraction window 0x473, place the item, pick the socket, start | `0x72 18B 70 0F B3 00 81 0B 81 0B 00 00 x4 00 00` | disruptive |

---

## 7. Open questions
1. **Q1 Kind to slot**: the FUN_00426680 table from EN Kind (and def+0x1F0) to grid slots 0..24, including which Kinds occupy two slots (rings/earrings). Needed for 0x07 persistence and swap logic (I-06).
2. **Q2 Option word w5**: meaning of the 6th block word (tail/extra), e.g. refine level or durability. No client writer found; always echo it.
3. **Q3 hii column to def offset**: is def+0x1C8 Duration = `Con`, and def+0x1F0 = `Cash`? (T-S42 tests the first.) Is `NotTrade` (KR only) enforced anywhere in EN?
4. **Q4 Bag compaction**: does FUN_00423FF0/FUN_00423b70 removal compact slots or leave holes? For type-0/2 removal across several stacks, which stack is decremented first? This affects the server list order sent in 0x03.
5. **Q5 Drop replies**: the retail reply to C2S 0x13/0x14 (0x23/0x24 + 0x12 is the design guess); owner of player-dropped items; ground item lifetime and whether 0x13 with picker 0 is a safe despawn (T-S13b).
6. **Q6 0x41 vs 0x42**: which one retail sent to the user versus observers; whether consumable buffs also need 0x25 (F5 sends both).
7. **Q7 Server-side cooldown**: is the client cooldown per slot (scene+0x6F4) or per item id? A shorter server cooldown desyncs, because the client ignores 0x25 inside its own cooldown.
8. **Q8 Reinforcement**:
   - Is the stone id range 0xB9F..0xBA4 inclusive? In EN, 2980 is a type-0 amplifier.
   - How is `new_option_item_id` chosen (element and grade of attribute stones 2945..2974)?
   - What are the success rates, and is the stone lost on failure (the client keeps it)?
9. **Q9 Crafting**:
   - Success rates.
   - Which skills gate Concoction/Refining (result 0x10).
   - Whether retail consumed materials on 0x11 (the client does).
   - The spec text calls FUN_00424c80/00424d00/00424d60 "skill checks", but the 0x1F spec identifies them as bag-space checks.
10. **Q10 Gathering content**:
    - The per-map mapping from the 0x69 `gather_node_record_index` (content container index) to node NPC 118/122-128.
    - Which maps have nodes, and at what coordinates.
    - What the `npcs` drop-rate column is (the `Drop` column is all zeros for nodes).
    - Cube NPCs (205, 206, ...) also have type 2 and must not be accepted as herb nodes.
11. **Q11 Station locations**: which EN maps and coordinates have the smelter (0x13C), alchemy station (0x13B) and smithy for live tests of 0x67/0x68.
12. **Q12 Extraction tools**: how the cash serial in 0x9C maps to premium_cash records (0x6F), how multi-use tools (Cash_V 6) count down, and whether other tools (4378 is KR-only) matter.
13. **Q13 Bag capacity**: the retail default capacity (35 today) and how the cash "slot extension" (C2S 0x43 CashShopBuySlotExtension) raises it live without a 0x03 resend.
14. **Q14 Ground items across map loads**: does 0x03/0x08 really drop the client ground list (FUN_004271e0 "frees scene item lists")? That decides whether F1 step 4 must re-send 0x11.
15. **Q15 Harness**: the key for Equipment window 7, plus drag and double-click support (I-21) for reliable C2S 0x11/0x13/0x14 captures.
16. **Q16 Busy-flag release**: does closing progress window 0x293 / 0x295 in code (FUN_004684d0 after the send) already clear `[ctx+0x2C]` / `[ctx+0x28]` through FUN_00446300?
    - The C2S 0x67 and 0x69 specs say yes. The C2S 0x68 and S2C 0x8D/0x8E/0x8F specs say only the result packet clears it.
    - If the window event does clear it, a missing result costs only the inventory sync, not a feature lock.
    - Test: run T-C67/T-C69 against today's server (no reply) and try to start a second craft or gather.
