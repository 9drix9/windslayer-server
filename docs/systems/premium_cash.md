# premium_cash: Item Mall, Wind Cash / Mileage, cash-item use

Design doc for the server team. Date: 2026-09-17.

**Sources, most authoritative first:**
1. Client spec `re_tools/corpus/systems/premium_cash.json`: 32 specs, all confidence=high, all server_status=missing. Cross-group specs come from `protocol_spec.json`.
2. `LIVE_TEST_LOG.md`.
3. `WindSlayer2Game/server/windslayer_server.py` (below "ws:"), `wsproto.py`, `wsdev.py`.
4. Memory notes.
5. PySlayer. `server_packets/__init__.py` try-imports `opcode_0x6A..0x71`, but none of those files exist. The only file is a KR `opcode_0x80.py` stub (`p8(1)+p32u(0x4CF)`).

Every claim marked **[bin]** was re-checked in this pass against `re_tools/WindSlayer.exe` (pefile) or the Ghidra asm corpus `re_tools/corpus/asm/`. Every claim marked **[run]** was checked by running code (`wsproto`, sqlite).

Opcode keys follow the spec. A C2S key is `sendsiteVA/opcode`, for example `0x460A72/0x49`. An S2C key is just the opcode. "mall" means the client object at `game_state+0x700` (VA `0x70F100`).

---

## 1. How the client implements the system

### 1.1 Client objects

| Thing | Where | Filled / changed by |
|---|---|---|
| Mall controller | `game_state+0x700` = 0x70F100. Init `FUN_0045AE40` runs in the S2C 0x5A key-exchange handler (0x44D808). It stores +0x14 game_state, +0x18 UI root, +0x1C item table (`scene+0xF84`), +0x20 game socket, +0x24 player_info (`scene+0x48`), and zeroes +0x588/+0x58C/+0x5AC/+0x5B8 | 0x5A (every connect) |
| Wind Cash | `mall+0x588`, UI label 0x67 | 0x6A, 0x6C, 0x70, 0x71. All carry absolute values |
| Mileage | `mall+0x58C`, UI label 0x68 | 0x6A, 0x6C, 0x6E, 0x70, 0x71. All absolute |
| First-purchase popup flag | byte `mall+0x00` | S2C 0x02 byte `cash_first_purchase_flag`. Shown once, then cleared, by 0x6A |
| Charge-pending flag | `mall+0x5AC` | Set by the Item Mall "charge" button (window 0x1CF ctrl 5). Cleared only by S2C 0x70 (or by 0x5A re-init) |
| Box-dirty flag | `mall+0x5B8` | Set by a successful 0x6E, cleared by 0x6A, reported in C2S 0x42 |
| **Cash item box** (account storage) | `mall+0x28` list, window 0x1FC | Rebuilt by 0x6A. 0x6C and 0x79 append (no duplicate check). 0x6E removes |
| **Owned cash items** (this character, not worn) | `mall+0x04` list, window 0x1FD, and the fixed 45-slot cash bag tab `player_info+0x7E8` (= `scene+0x830`) | Rebuilt only by 0x6F (`is_equipped=0`). Consumed through `FUN_00464380` (from 0x72, 0x73, 0x76, 0x77, 0x9A, 0x9B, 0x9C). 0x93 removes |
| Worn cash items | `mall+0x38` list | 0x6F (`is_equipped=1`). 0x1D / 0x1E / 0x24 move records to and from +0x04 (item_inventory group) |
| Snapshots | `mall+0x48` (box), `mall+0x58` (owned) | Taken only by 0x6A. C2S 0x42 reports what was added since the snapshot |
| Gift queue | `mall+0x5F4` | S2C 0x6D (mail_gift group). Popup 0x3EF opens at the end of 0x6A (`FUN_0045E260`) and after each gift popup is confirmed |
| SS# flag | `scene+0x218` | Set in the 0x5A handler (0x44D845): `strncmp(scene+0x131, MD5hex(username), 32) != 0`. Gates the password dialog 0x201 |

All mall S2C opcodes except 0x75 and 0x80 go through `SubHandler4_PacketHandler4` (0x461890). SubHandler4 returns without reading unless `mall+0x14` and `mall+0x18` are set, which is always true after 0x5A. 0x75 (0x457F91) and 0x80 (0x44E2FE) are handled in the main TCP handler.

### 1.2 The 28-byte cash record

0x6A, 0x6C, 0x6F and 0x79 each read this record with a single `GetDataFromPacket(char*,0x1C)`:

```
+0x00 u32 serial     unique key used by 0x42/0x45/0x6E/0x72/0x73/0x76/0x77/0x93/0x9A/0x9B/0x9C
+0x04 u16 item_id    catalog id (1-based)
+0x06 u8  kind       1 = counted (qty -= n on use, record freed at 0)
                     2 = period (expiry SYSTEMTIME used); anything else = permanent
+0x07 u8  0
+0x08 u16 quantity   0 => FUN_0045E8E0 never places the item in a bag
+0x0A 16B expiry     SYSTEMTIME (u16 y,m,dow,d,h,min,s,ms). year < 1900 = none.
                     kind 2 with year != 0 = already activated: not usable (FUN_0045E760),
                     not bagged; 0x6F prints "[%s] will expire in %d day(s)."
+0x1A u8  origin     0 = bought with cash (delete dialog shows a 30% refund),
                     3 = event item (0x6C shows "Congratulation... event item")
+0x1B u8  0
```

In S2C 0x6F each record is preceded by `bool is_equipped`, so each entry is 29 bytes.

### 1.3 Currencies: Wind Cash is not "winnie"

Wind Cash and Mileage exist only in the mall object. The client compares them against item prices 1:1, and the "pay with mileage" checkbox checks `mileage >= price`.

The `u32` that our server calls **winnie** (in S2C 0x18 and 0x03) is **Victy** (`player_info+0x238`, 'You are short of Victy.'), per spec 0x18. It is a separate currency with no link to the Item Mall. Nothing that writes `mall+0x588/+0x58C` reads it.

### 1.4 The EN client cannot ask to open the Item Mall

**[bin]** verified:
- LIVE_TEST_LOG, HUD row: the HUD "Premium Shop" button sends nothing and shows a "Coming Soon" tooltip.
- Clicking an entity whose `+0xE2 room_no == 0x81` (set by S2C 0x75 or 0x5B) opens dialog 0x133 'Enter the cash shop?' (`FUN_0044C4B0`). Choosing Yes re-dispatches `FUN_00446300(window 3, control 1)` at 0x446CF8..0x446D1C (`CMP EAX,0x81`, `[EBP+0xC]=1`, `EBX=3`).
  - The window switch at 0x446AD3 computes `EBX-1=2`. Byte table 0x44BA54[2] = 1, and jump table 0x44B9EC[1] = **0x448F21**.
  - 0x448F21 does `ADD ECX,-2; CMP ECX,8; JA 0x44B830`. Control 1 wraps to 0xFFFFFFFF and falls to the plain return at 0x44B830. **No `Send`.**
- The window-3 (HUD) control jump table 0x44BBC0 is: ctrl 2 → toggle window 0x1CE, ctrl 3 → 0x1CD, ctrl 4 → 0x1AE, ctrl 8 → memo window 0x3FE (`FUN_00470640`), ctrl 10 → `FUN_00473BB0`. **Controls 5, 6, 7 and 9 → 0x44B830 (no-op).**
  - So the HUD control 6 that S2C 0x6D enables (state 0) and S2C 0x6B disables (state 3), probably the "Gift" icon, does nothing when clicked.
- All 89 C2S send sites are attributed (PROTOCOL.md completeness), and none is an "enter mall" request. `FUN_0045F2E0` only handles windows that exist once the mall is open.

**Consequence:** mall entry is always started by the server pushing S2C 0x6A. This design uses a chat command, `!mall`. Leaving is started by the client, which sends C2S 0x42 on close.

### 1.5 Item Mall UI (windows seen in the code)

| Window | Role | C2S it can emit |
|---|---|---|
| 0x1CF | Mall main: item list, balances, wish list. ctrl 3 = close; ctrl 5 = charge (minimizes the game and ShellExecutes the dead `windslayer.kr.games.yahoo.com/item/item_filling.html`); ctrl 0xA8 = refresh | 0x42 (close), 0x46 (refresh / restore-after-charge) |
| 0x1FB | Try-on preview panel (preview avatar on `./hs/preview99_01.hmi`) | via 0x1F7 |
| 0x1F7 | Cart confirm (up to 28 items; mileage checkbox ctrl 0xF) | 0x43 cart |
| 0x1F8 | Single-item buy confirm (mileage checkbox ctrl 4) | 0x43 single |
| 0x1FC | Cash item box (account storage); ctrl 1 = delete | via 0x1FA |
| 0x1FA | Delete confirm; shows the round(price*0.3) refund | 0x45 |
| 0x1FD | Character cash inventory panel; buttons 1/2 open the inventory / bank slot-extension pickers 0x41C / 0x41D | via 0x1FF |
| 0x1FF | Slot-extension buy confirm | 0x43 (item 0x75C..0x761) |
| 0x201 | Password (SS#) dialog. Also used by bank 0x1A7, window 0x235, and character delete (C2S 0x12) | 0x51 |
| 0x1F9 | Gift dialog (recipient ctrl 9, message ctrl 0xB). S2C 0x80 {1,0x1F9} opens it | 0x47 |
| 0x3EF | Received-gift popup (from the 0x6D queue) | 0x4B with item 9999 (mail_gift group) |

Moves between box and character are done **locally** by the client: 0x1FC notification 0xCA moves storage→char (with slot and duplicate checks), 0x1FD notification 0xC9 moves char→storage. The server only learns about them from C2S 0x42 when the mall closes.

### 1.6 Using a cash item in the world (`FUN_00463D90`)

Double-clicking an item with catalog type (`+0x154`) 5 in the cash bag tab goes through `FUN_0046CAD0` to `FUN_00463D90`. The client first finds the owned record with `FUN_0045E760`: `item_id` matches, `qty != 0`, and the record is not an activated period item. If there is no such record, nothing happens.

| Item ids (EN catalog name) **[run]** | gamedef Cash_Cls / Cash_T / Cash_V | Client window | C2S | Modal "Waiting for the server"? | S2C reply |
|---|---|---|---|---|---|
| 1894 1 Message Pad, 3320 11 Message Pads | 15 / 1 / 1, 11 | 0x3FD | 0x4B (social_friend key `0x461064/0x4B`; handler owned by chat_mail_gm) | yes | 0x77 |
| 1895 Change Nickname | 14 / 1 / 1 | 0x3F1 | 0x49 | yes | 0x73 (+ 0x74 name broadcast) |
| 3214 / 3215 / 3234 / 3235 Reset 5 / 1 / 10 / 20 Stat Points | 14 / 1 / 5, 1, 10, 20 | 0x3FA | 0x4A | yes | 0x76 |
| 3377-3379 Super Megaphone x13 / x5 / x1, 3380-3381 Megaphone x11 / x1 | 15 / 1 / 13, 5, 1, 11, 1 | 0x3FF | 0x4C | no | none (broadcast + consume) |
| 3429 / 3431 / 3433 Friend Teleport Stones 50 / 20 / 10 | 14 / 1 / 50, 20, 10 | 0x472 | 0x71 | no | 0x9B |
| 3430 / 3432 / 3434 General Teleport Stones 50 / 20 / 10 | 14 / 1 / 50, 20, 10 | world map 0x0A → region window → 0x471 | 0x70 | no | 0x9A |
| 3436 / 3437 / 3951 / 3952 Element Separators | 14 / 1 / 6, 6, 1, 1 | `FUN_004688D0` → window 0x473 (item group) | 0x72 | ? | 0x9C (item group) |
| 3385 / 3386 Random Hairstyle F / M, 3408 Random Hair Coloring, other Cash_Cls 1/2 items | 1, 2 / 1 / 1 | 0x3F4 | 0x48 | yes | 0x72 (+hair_code for 0xD39/0xD3A/0xD50) |
| 3321-3326 +50% / +100% EXP for 60 / 30 / 7 days | 14 / **2** / 60, 30, 7 | 0x3F4, period path (the client refuses if the effect is already in the skill/buff list) | 0x48 | yes | 0x72 (+16-byte expiry), later 0x93 |
| any other type-5 cash item | - | 0x3F4 | 0x48 | yes | 0x72 |

### 1.7 Content: gamedef.sqlite3 `items`

**[run]** `server/gamedef.sqlite3` is byte-identical to `C:\Users\ohdri\Desktop\PySlayer\gamedef.sqlite3`, the file that `_GAMEDEF_PATH` (ws:45) actually opens. It has these cash columns:

| Column | Meaning |
|---|---|
| `Cash` | 1 = sold in the mall (675 rows) |
| `Cash_Cls` | Category / use type (1 hair style, 2 hair dye, 14 utility, 15 communication, ...) |
| `Cash_T` | 0 = permanent, 1 = counted, 2 = period |
| `Cash_P` | Price in Wind Cash |
| `Cash_V` | Count (counted items) or days (period items) |
| `Cash_B` | Shop badge (0..5) |
| `Cash_ST` | 0 / 1 / 2 / 5, meaning unknown |
| `Gender` | 1 = male-only, 2 = female-only (3386 "Random Hair (M)" has 1, 3385 "(F)" has 2) |

For the cash range, item ids match the EN client catalog (`server/en_item_catalog.json`): 1884, 1894, 1895, 3214, 3321, 3327, 3377, 3408, 3429 and 3430 all match by name.

Slot extensions 1884-1889 have Cash=0 and Cash_P=4600. In both gamedef and the client catalog, 1888 and 1889 have Type 0 rather than 5, which is harmless because they are never stored. Gift certificates 3327-3332 have Cash=0 and Cash_P equal to their face value.

**Inferred client item-def mapping** (verify with T-CAT):

| Client def offset | gamedef column | Evidence |
|---|---|---|
| +0x1F0 (is-cash / alternate grid) | `Cash` | Gift certificates 3327-3332 have Cash=0, and the client special-cases 0xCFF..0xD04 because their +0x1F0 is 0. The 0x6F purge uses +0x1F0 != 0. `FUN_00427430` exits when +0x1F0 == 0 **[bin]** 0x427479 |
| +0x1F4 (use type) | `Cash_Cls` | **[bin]** `FUN_00427430` switch on +0x1F4 (≤ 0xE). Case 1 special-cases 0xD39/0xD3A (Cash_Cls 1); case 2 special-cases 0xD50 (Cash_Cls 2); case 0xE returns `+0x1F6 == 2` (EXP items are Cash_Cls 14 with Cash_T 2) |
| +0x1F6 (duration type) | `Cash_T` | 0x72 reads the expiry only when +0x1F6==2 |
| +0x1FC (price) | `Cash_P` | Compared against cash in 0x1F7 / 0x1F8 / 0x1FF |

### 1.8 Spec errata found while designing (fix in the corpus, not the server)

1. **S2C 0x71 `trigger`** says "Reply to C2S 0x71 (gift send) built at 0x461405-0x461430: u16 id + str[17] recipient". That send site is **C2S 0x71 FriendWarpStoneUse**. The gift request is **C2S 0x47** (`0x46050C/0x47`), whose `expected_response` correctly names S2C 0x71. The result strings (short of cash / character doesn't exist / other gender / own character) belong to the gift flow.
2. **S2C 0x73 `blind_open_questions`** ask whether "C2S 0x71 is the rename request". It is C2S 0x49 (the main fields are right).
3. **S2C 0x6E `blind_open_questions`** say "Which C2S opcode requests the delete (not traced)". It is C2S 0x45 (`0x460676/0x45`).
4. **C2S `0x46072C/0x43` open question** (which slot-extension id maps to which tab) is **resolved [bin]** by `FUN_0045E9F0` strings: 0x75C equipment (+0x3A9), 0x75D consumable/"spend" (+0x3AA), 0x75E misc (+0x3AB), 0x75F/0x760/0x761 the same three tabs in the bank (+0x980/+0x981/+0x982).
5. **S2C 0x9A open question** "Which cash item ids open window 0x471" is answered by the C2S 0x70 spec: 3430/3432/3434 through world map 0x0A.
6. **C2S `0x43E302/0x46` `socket`** says `FUN_0045AE40` runs "during S2C 0x01 login (0x44D808)". 0x44D808 is inside the **S2C 0x5A** handler, as the 0x42 and 0x4C specs say.

---

## 2. Request / response flows

Field values are what the server should send. **"Replay"** means the proven map-load sequence from `_handle_change_map` (ws:1244-1320): `0x08 {map_code, game_time_ms}` → `0x03` → `0x07` (local player) → `0x0A` → `0x28` → `0x44` → `0x1A`×n. Implementation item `premium_cash-map-replay-helper` factors it out. Keep the byte sequence unchanged (feedback_experiments: change one thing at a time) and only append 0x6F after 0x44.

### F1: Session start and every map load (→ 0x6F, 0x6D)
1. S2C 0x5A: the client resets the mall (cash = mileage = 0) and computes `scene+0x218`.
2. S2C 0x02 (login):
   - Send `cash_first_purchase_flag = 1 if account.first_purchase_notice else 0`. Today this byte is labelled `unknown_1` and is always 0.
   - Send the real account gender as `account_gender_flag`. Gift and gender-item checks use it.
3. **After the local 0x07 spawn that follows every S2C 0x03** (enter world, portal, mall exit, warp), and after 0x28/0x44, send **S2C 0x6F** `{count=N, N × {is_equipped, record}}` with the character's full owned cash list.
   - Why after 0x03: 0x03 memsets the cash bag tab `scene+0x830/+0x88A` (spec 0x03 semantics).
   - Why the full list: 0x6F first purges every cash item from every bag.
   - Why after 0x07 and not straight after 0x03: the 0x6F tail calls `FUN_00441C20(1)`, which checks `scene+0x970` at **[bin]** 0x441D85 and skips the period-item list rebuild while no local player exists. 0x03 destroys the local player.
   - `count=0` is valid and means "none".
   - Leave out records whose period has already expired. For the "[x] is expired." line, send them and then a 0x93 for each.
   - Encoding: `Grammar(0x6F).encode(rec, assume={"char_bag_slots_0x361 <= 45 && char_bag_slots_0x362 <= 45 && char_bag_slots_0x363 <= 45": True})`. Without `assume` it encodes 0 bytes **[run]** (B8). The client gate passes only while all three tab capacities are ≤ 45 (see F4 slot extensions).
4. If the account has gifts not yet delivered this login session, send S2C 0x6D `{gift_count, records}` once per session (mail_gift group; the client list only grows). Never send 0x6D with `count=0`.

### F2: Enter the Item Mall (server-initiated: chat `!mall` → 0x6A)
1. C2S 0x03 chat whose text is `!mall`. Intercept it in `_handle_chat` (ws:945) before the echo.
2. Refuse unless all of these hold:
   - The session is in world: `char_name` set, local 0x07 already sent.
   - Not `in_cash_shop`.
   - Not in an arena, play room, trade or stall (once those exist).
   - **Hazard [bin]:** `FUN_0045C470` dereferences `[mall+0x24]+0x928` (= `scene+0x970`, the local player) at 0x45C532..0x45C5CF with no null check. Sending 0x6A before the 0x07 spawn crashes the client.
   - Refusal: one S2C 0x15 SystemMessage or 0x90 line built from the grammar. Do not use `_send_chat_line`, which has the chat group's extra-byte bug. Send no 0x6A.
3. Save `pre_mall = {map: session['current_map'], x, y}`. Take x/y from the combat driver's last memory read of `player+0x11F8/+0x1288` (ws:1800-1803), falling back to `char['x'/'y']`.
   - Set `session['in_cash_shop']=True`.
   - **Freeze** position sampling, `_memory_melee`, `_tick_respawns` and wander for this session (B9). The preview avatar has uid 1 and fixed coordinates.
4. ~~Multiplayer only: send S2C 0x75 `{uid}` to other sessions on the same map. They draw sign sprite 0x168 over the player.~~ **Superseded (P8 stage 4):** the player leaves the map instead (hidden from it, see F17).
5. Send S2C 0x6F (full owned list). 0x6A snapshots +0x04, so 0x6F must come first.
6. If there are undelivered gifts, send S2C 0x6D, so their popups appear at the end of 0x6A.
7. Send S2C 0x6A `{cash_balance=account.cash, mileage_balance=account.mileage, box_count=len(box), box records}`. Record `session['mall_snapshot'] = {box: set(serials), char: set(serials)}`.
   - Client result: the world map is unloaded (`FUN_00406FD0`, then `FUN_00422F40` destroys entities); the preview map is loaded; the preview avatar is registered as local player (uid = `player_info+0x1D8`); scene mode is set to 4; windows 0x1CF/0x1FB/0x1FC/0x1FD open; the first-purchase popup shows if `mall+0x00` is set; then gift popups.

### F3: Balance refresh (C2S 0x46 → S2C 0x70)
1. The player presses charge (0x1CF ctrl 5). The client sets `+0x5AC=1`, minimizes the game and opens the dead Yahoo URL.
   - On window restore (`FUN_0043E2E0`, vtable slot 0x34) or refresh (ctrl 0xA8, `FUN_00463D70`), it sends **C2S 0x46** with an empty body. Both sites are gated on `+0x5AC != 0`.
2. Server: reload the account. Cash may have been credited meanwhile by GM `!cash`, the admin port or a web tool.
3. Reply **S2C 0x70** `{cash_balance, mileage_balance, first_purchase_bonus}`. Set `first_purchase_bonus = 1` if `account.first_purchase_notice` (and clear it), else 0.
   - The client clears `+0x5AC`, which stops the resend on every restore.
4. There is no failure path: always answer.

### F4: Buy (C2S 0x43 → S2C 0x6C, one per item)
One opcode has three send sites: single buy (0x1F8, `item_count=1`), cart (0x1F7, `item_count` ≤ 28) and slot extension (0x1FF, item 0x75C..0x761). Grammar: `u8 pay_with_mileage, u8 item_count, item_count × u16 item_code`. The client sets no waiting box.

1. Validate: `in_cash_shop`; `1 ≤ item_count ≤ 28`; payload length is exactly `2+2n`.
2. For each code:
   - The gamedef row exists.
   - (`Cash==1` and `Cash_P>0`) **or** the code is in 1884..1889.
   - Slot extensions only: 1884/1885/1886 → `char.tab_slots[0/1/2] < 45`; 1887/1888/1889 → `char.bank_slots[0/1/2] < 60`.
   - Any failure → **S2C 0x6C `{result=0}`** ('You failed to buy the item. Please try again in a few minutes.').
3. `total = Σ Cash_P`.
   - `pay_with_mileage==1`: need `mileage ≥ total`.
   - `pay_with_mileage==0`: need `cash ≥ total`.
   - Otherwise send **0x6C `{result=0x0E}`** ('You are short of cash.').
4. Debit the whole cart atomically.
   - Mileage bonus: for cash payments only, `mileage += floor(Cash_P × MILEAGE_BONUS_RATE)`. This is config, default 0. When mileage goes up, the client shows '50% bonus mileage has been deposited.'
5. Optional, first-ever cash purchase: `mileage += 500` and `account.first_purchase_notice = True`.
6. For **each** item, in order (the 0x6C handler reads exactly one record):
   - **Slot extension:** `tab_slots[i] += 5` or `bank_slots[i] += 5`, then persist. A record goes on the wire but is not stored: `FUN_0045E9F0` applies +5 and frees it.
     - **Hazard [bin]:** 0x45EA16 does `CMP byte [scene+0x3A9],0x2D; JNC skip; ADD +5` with no clamp. A tab at 42 becomes 47, which fails the 0x6F gate (≤ 45, F1) and the 0x1D local-equip gate. Capacities must stay in {35, 40, 45} and {35..60 step 5}. Enforce `new = min(cap, old+5)` and never send a 0x03 capacity above 45.
   - **Otherwise** build a record: `serial=next_serial()`, `item_id`, `kind=Cash_T`, `quantity = Cash_V if Cash_T==1 and Cash_V>0 else 1`, `expiry=0`, `origin = 0 if paid with cash else 1`. Mileage purchases get no refund. Append it to `account.cash_box`.
   - Send **S2C 0x6C `{result=1, cash_balance=<running>, mileage_balance=<running>, record}`**.
7. Persist `accounts.json`.

### F5: Delete from the cash box (C2S 0x45 → S2C 0x6E)
1. **C2S 0x45** `{u16 item_code, u32 cash_item_serial}`. The client sends it only when both are non-zero, and shows no waiting box.
2. Find the record in `account.cash_box` by serial and check `item_id == item_code`. If not found → **S2C 0x6E `{result=0}`** ('You failed to delete the item.').
3. `refund = round(Cash_P × 0.3)` if `origin==0` and the item is not partly used (`kind != 1`, or `quantity == Cash_V`); otherwise 0. This mirrors the dialog: the double constant at `0x6F0C20` is 0.3, and a partly used stack shows 0.
4. `mileage += refund`, remove the record, persist.
5. Send **S2C 0x6E `{result=1, mileage_balance, item_serial}`**. The client removes the record from +0x28 and sets `+0x5B8=1`.

### F6: Gift (C2S 0x51 → S2C 0x80 → C2S 0x47 → S2C 0x71)
1. Client: the row Gift action runs `FUN_0045DFF0` → `FUN_00464480`.
   - Gate: `scene+0x218 != 0`, otherwise 'no SS# in this ID...' and nothing is sent. The flag is 1 whenever the 32 bytes at `scene+0x131` differ from MD5hex(username). If our launch path leaves +0x131 empty, the flag is 1 and the dialog opens. Check with T-218.
   - Dialog 0x201. OK sends **C2S 0x51** `{str[21] password, u32 target_window_id=0x1F9}`. Bytes after the NUL are stack garbage. The dialog closes; there is no waiting box.
2. Server: compare the password, up to the first NUL, with `account.second_password`, falling back to the login `password`.
   - Mismatch → **S2C 0x80 `{result=0}`** ('Invalid password').
   - Match and `target_window_id ∈ {0x1F9, 0x1A7, 0x235}` → **S2C 0x80 `{result=1, target_window_id}`**, and set `session['pw_ok'][id]=now`.
   - Never send 0x80 with an id outside that whitelist: the client opens whatever window id it gets. The same handler serves the bank (0x1A7, shop_storage group).
3. Client (target 0x1F9): `FUN_0045E0D0` re-checks cash ≥ price and opens gift dialog 0x1F9.
   - Send produces **C2S 0x47** `{u8 0, u16 item_code, str[17] recipient, u8 message_len, bytes message}`.
   - The client then shows the modal waiting box, **so a reply is mandatory**.
4. Server checks, in order. The first failure sends **S2C 0x71 `{result}`**:
   - `pw_ok[0x1F9]` is younger than 120 s, `in_cash_shop` is set, and the item is valid (`Cash==1`, `Cash_P>0`). Otherwise `0x00` (generic failure).
   - The recipient character exists in `accounts.json`. Otherwise `0x02` ('The character doesn't exist').
   - The recipient is not one of the sender's own characters. Otherwise `0x0C`.
   - If the item `Gender` is 1 or 2, it matches the recipient account's gender. Otherwise `0x14`.
   - `cash ≥ Cash_P`. Otherwise `0x0E`.
5. Success:
   - Debit cash, and apply the mileage bonus with the same rule as F4.
   - Create the record in the **recipient account's** `cash_box` with `origin=2` (no refund).
   - Append `{sender=<sender char name>, message[:90], item_id, serial}` to the recipient's `gift_inbox`.
   - Send **S2C 0x71 `{result=1, cash_balance, mileage_balance}`**.
6. If the recipient is online:
   - If they are in the mall → **S2C 0x79** `{record}`, which appears in their box.
   - Always → **S2C 0x6D** `{gift_count=1, {sender, message, item_id, 16×0}}`. This enables HUD window 3 ctrl 6, which is inert (§1.4). The popup shows at the end of their next 0x6A.
   - **Queue-stall hazard (spec 0x6D):** a record whose item def has +0x1F0 == 0 and is outside 3327..3332 never pops, and it blocks every later gift. Only gift items with `Cash==1`.
7. The recipient's thank-you (popup 0x3EF → C2S 0x4B, `note_item_id=9999`) is handled by the chat_mail_gm group. It must not consume an item.

### F7: Close the mall (C2S 0x42 → 0x6B + replay + 0x6F)
1. **C2S 0x42** `{u8 storage_deleted_flag, i32 to_storage_count, i32 to_character_count, to_storage_count×u32 serial, to_character_count×u32 serial}`. Length is exactly `9+4(a+b)`. No reply is awaited, but the client stays on the preview map until the server acts.
2. List A (now in storage):
   - Serial is in `char.cash_items` → move it to `account.cash_box` (clear `equipped`).
   - Serial is already in the box (bought or gifted this session) → no-op.
   - Unknown serial → log and ignore.
3. List B (now on the character):
   - Serial is in the box → move it to `char.cash_items`, but only if the destination bag category still has room (catalog type 0/1/5 → consume/equip/cash tab; capacity from `tab_slots`, cash tab 45). Otherwise leave it in the box and log.
   - Serial is already on the character → no-op.
4. `storage_deleted_flag` is informational only; F5 already applied the deletes. Persist, then clear `mall_snapshot`.
5. The handler must be **idempotent**. If the window is closed again without a fresh 0x6A, the client re-sends the same serials.
6. Send **S2C 0x6B** (empty). The client saves the wish list to `HKCU\Software\Hamelin\WindSlayer\<name>\Wish0..11`, sets window 3 ctrl 6 to state 3 and rebuilds the inventory. This **does not** restore the world.
7. **Replay** to `pre_mall.map` at `pre_mall.x/y`: 0x08, 0x03, 0x07, 0x0A, 0x28, 0x44, then **S2C 0x6F** (post-move owned list), then `_spawn_map_monsters`.
8. Set `session['in_cash_shop']=False` and unfreeze combat. ~~Multiplayer only: send **S2C 0x5C `{uid}`** to others on the map.~~ **Superseded (P8 stage 4):** the replay of step 7 brings him back as any map load does (0x05 to the peers, see F17).

### F8: Use a generic cash item (C2S 0x48 → S2C 0x72)
1. **C2S 0x48** `{u16 item_id}`. The client shows the waiting box, **so a reply is mandatory**.
2. Find the first `char.cash_items` record with a matching `item_id`, `quantity>0`, and not (kind 2 with expiry set).
   - None found → refusal packet **S2C 0x72 `{player_uid=uid, item_id=0, item_serial=0}`** (10 B).
     - Why it works **[bin]**: `FUN_00404210(0)` computes `id-1 = 0xFFFFFFFF`, and the `JNC` bounds check returns NULL. So `FUN_00427430` finds no def and changes no appearance. The client closes box 0x16, the serial matches nothing, and `item_def(0)` is NULL, so it stops.
     - Side effect: effect and sound 0x140 play. Verify with T-72c.
3. Dispatch on the gamedef row:
   - **Cash_Cls 1 or 2** (hair style / dye):
     - Check item `Gender` against the character.
     - For 0xD39 / 0xD3A / 0xD50, the server picks `hair_code`.
     - For other Cash_Cls 2 ids, the client computes the look itself **[bin]** 0x427587..0x4275AC: the new hair keeps the old value but replaces its tens digit with `(u8)(item_id - 0x50)`, i.e. `item_id - 3408` for 0xD50..0xD59. Mirror this formula to persist `char['hair']`.
     - Other Cash_Cls 1 ids: the client uses `FUN_00427080` (style formula, open question).
     - Then `quantity -= 1` (remove at 0) and persist.
     - Send **0x72 `{player_uid, item_id, [hair_code if id ∈ {0xD39,0xD3A,0xD50}], item_serial}`** to the owner. Send the same body to other sessions on the map; their clients stop reading after item_id/hair_code.
   - **Cash_T 2** (period, e.g. 3321-3326 EXP):
     - Set `expire = now + Cash_V days`. The record stays with quantity 1. Persist.
     - Send **0x72 `{player_uid, item_id, item_serial, expire_time=SYSTEMTIME}`** (26 B), encoded with `assume={"item_def(item_id).duration_type == 2": True}`. Without the assume, the 16 bytes are dropped.
     - Apply the effect: EXP ×1.5 or ×2.0 in `_send_exp` (ws:1994) while `now < expire`.
   - **Anything else:** `quantity -= 1`, then send **0x72 `{player_uid, item_id, item_serial}`** (10 B).
4. **Hazard:** whether 0x72 carries the 16-byte expiry is decided by the *client's* def+0x1F6. If gamedef `Cash_T` ever disagrees with the client def, the packet is short (the client reads out of bounds) or long (harmless). Verify with T-CAT.
5. **Hazard (B7):** until the hex-literal fix lands, `Grammar(0x72)` never emits `hair_code`, because `item_id == 0x0D50` evaluates False. Build 0x72 by hand or pass `assume`.

### F9: Rename (C2S 0x49 → S2C 0x73 + 0x74)
1. **C2S 0x49** `{str[17] new_name}`. The client already ran `FUN_0043DF40(name,1)`, and it shows the waiting box, **so a reply is mandatory**. The packet carries no item id.
2. Server checks. Any failure → **S2C 0x73 `{result=0}`** ('The name is already being used'); the client treats every non-1 value that way.
   - The character owns item 1895 with `quantity>0`.
   - The name is 1..16 printable ASCII with no space, `'` or `\`.
   - The name contains none of the reserved words gamemaster, yahoo, wind, windmaster, windy, winsle, master, avocade, administrator, hamelin (the `FUN_0043DF40` list).
   - The name is unique across **all** characters in `accounts.json`. Today both a character and an account are called "test".
3. Success:
   - Rename the character dict and `session['char_name']`. Persist before replying, because the enter-world lookup (ws:709-713) matches by name.
   - Consume 1895 and persist.
   - Send **S2C 0x73 `{result=1, item_serial}`**.
   - Then send **S2C 0x74 `{uid, new_name}`** to self (updates `entity+0x00`, `scene+0x206` and the stat panel) and to other sessions on the map. 0x73 alone does not change the name.

### F10: Stat reset (C2S 0x4A → S2C 0x76)
1. **C2S 0x4A** `{u16 item_id, u8 str_removed, u8 dex_removed, u8 int_removed, u8 tol_removed}`. The client sends it only when something was removed, and shows the waiting box, **so a reply is mandatory**.
2. Server checks:
   - `item_id ∈ {3214,3215,3234,3235}` and the item is owned with qty>0.
   - `points = Cash_V` (5/1/10/20).
   - `0 < Σremoved ≤ points`.
   - Each `stat - removed ≥ minimum`. Use the class creation minimum, falling back to 1.
   - Characters without stored stats (TestHero in accounts.json has none) use the same defaults the 0x07 builder sends.
3. Failure → **S2C 0x76, owner form (18 B)** `{target_uid=uid, str, dex, int, spi (unchanged), cash_item_serial=0, consume_count=0}`. This closes the waiting box and consumes nothing. There is no dedicated failure form.
4. Success:
   - Apply the deltas to `char['str'/'dex'/'int'/'spr']`, consume the item and persist.
   - Send **S2C 0x76 `{uid, str, dex, int, spi, cash_item_serial, consume_count=1}`**, encoded with `assume={"target_uid == local_player_uid": True}` so all 18 bytes are emitted.
   - Other sessions get the 12-byte form.
   - Free points need no extra packet: the client derives them as `total(level) - Σstats` (spec 0x14).

### F11: Megaphone (C2S 0x4C → broadcast; no reply)
1. **C2S 0x4C** `{u16 item_id, str[61] message}`. The client closes the window and shows no waiting box.
2. Server checks. On failure, drop the message, optionally with a private S2C 0x15 line.
   - `item_id ∈ 3377..3381`, owned, qty>0.
   - Cut the text at the first NUL, max 60 bytes.
   - Run a server curse filter (the client already ran `Curse_Engine_Clean`).
   - Rate limit: 1 per 10 s per character.
3. Broadcast `"[Megaphone] <char> : <text>"`, clamped to ≤ 87 bytes (0x90 reads into an 88-byte buffer), as **S2C 0x90** (orange).
   - Super Megaphone (3377-3379) → every session.
   - Megaphone (3380-3381) → every session on the same channel (single channel today, so all).
   - chat_mail_gm Q3 records that the original display opcode is unknown.
4. Consume 1 and persist. Then update the owner's client with **S2C 0x72 `{uid, item_id, serial}`** (megaphone Cash_Cls 15 takes the `FUN_00427430` default path, so no appearance change, but effect 0x140 plays) **or** with a full 0x6F resync. Pick whichever T-4C shows is cleaner.

### F12: Note item (C2S 0x4B → S2C 0x77). Handler owned by chat_mail_gm
This group only provides `cash_find(char, item_id)` and `cash_consume(serial)`. chat_mail_gm F6/F8 sends **S2C 0x77 `{result=1, cash_item_serial}`** for items 1894/3320 and `{1, 0}` for 9999, or `{result=0}` when the recipient does not exist. The client shows the waiting box for 1894/3320, so a reply is mandatory.

### F13: Region warp (C2S 0x70 → S2C 0x9A + replay)
1. **C2S 0x70** `{u16 item_id, i32 dest_map_id}`. `dest_map_id` = region_code×100 + suffix (e.g. 101). The client shows no waiting box and closes the region window itself.
2. Server checks. Failure → **S2C 0x9A `{result=0}`** ('You are unable to transfer to the area.').
   - `item_id ∈ {3430,3432,3434}`, owned, qty>0.
   - `dest_map_id` is a known field map: it appears as `next_map_id` in gamedef `maps` (697 rows) or `portals.json` values. Use that row's `xpos/ypos` as the landing point.
   - The destination is not an arena or instance map.
   - The session is in world, not in the mall, and not in a room.
3. Success: consume, persist, send **S2C 0x9A `{result=1, cash_item_serial}`**, set `current_map=dest`, **replay** to dest, then send **0x6F**.

### F14: Friend warp (C2S 0x71 → S2C 0x9B + replay). Needs multiplayer
1. **C2S 0x71** `{u16 item_id, str[17] friend_name}`. No waiting box.
2. Server checks. Failure → **S2C 0x9B `{result=0}`** ('The character doesn't exist...').
   - `item_id ∈ {3429,3431,3433}` and owned.
   - The target is an online session with `char_name == friend_name`, not self, in world, and not in the mall, an arena or a room.
3. Success: consume, send **S2C 0x9B `{1, serial}`**, **replay** to the target's map at the target's last-known position, then send **0x6F**.

### F15: Expiry ticker (→ S2C 0x93)
1. Every 60 s, and at login, scan online characters' `cash_items` for kind-2 records with `expire ≤ now`.
2. For each one: remove the record, clear any boost, persist, and send **S2C 0x93 `{item_serial}`**. The client prints "[name] is expired." and removes the record from list +0x04.
   - 0x93 does **not** touch the bags. Send it only for *activated* records, which are never bagged. For anything else, resend 0x6F.

### F16: Event mileage (→ S2C 0x70 + 0x98)
1. The server credits mileage (event or GM).
2. Send **S2C 0x70 `{cash, mileage, 0}`**, then **S2C 0x98** (empty body). The client adds the chat line "※Mileage Event※ You got bonus mileage."

### F17: Presence. Needs multiplayer. SUPERSEDED by "hidden from the map" (P8 stage 4)

**Superseded.** The server implements presence as **hidden from the map**, not with the retail marker below (server `mall.py` module docstring "Presence"; roadmap item premium_cash-presence):
- Entering the mall departs the map like a map load: the peers holding the player get **S2C 0x06**, and a late arrival gets no record of him (he is in no map instance, `in_world` is False, and `presence._can_show` also checks `in_cash_shop`).
- The exit's map-load replay (F7 step 7) brings him back: **S2C 0x05** to every peer on the map at that time, and 0x04 of them to him.
- Every other server-driven map load (GM `!warp` / `/go`, warp stones, revive) is refused while he is inside. A 0x08 into the preview scene would put the client back in the world with the mall still open server-side.
- Why not the marker: the signboard needs the entity on the peers' clients, which the world-leave removes. The marker's only extra is the "Enter the cash shop?" prompt, whose Yes is the same client no-op as the HUD button (§1.4).

The retail marker, kept for reference and **not sent**:
- ~~Entering the mall: send **S2C 0x75 `{uid}`** to others on the map.~~
- ~~Leaving: send **S2C 0x5C `{uid}`**.~~
- ~~When a session spawns another player (0x07) who is in the mall, it must send that player's `+0xE2 room_no = 0x81` (2009: entity+0xEA = 0xC5). In our 0x07 builder this is the u16 labelled "marriage" (B10).~~
  - With a non-zero value, the client also reads the `+0xE1` type and the `+0xD0` title, so emit `u8 0` plus 17 zero bytes.
  - Clicking that player only re-dispatches to a no-op (§1.4), so the sign is cosmetic.

### F18: Password gate for other windows (shared with shop_storage)
C2S 0x51 with `target_window_id=0x1A7` (bank) or `0x235`: run the same check as F6.2 and reply **S2C 0x80 `{1, id}`**. For the bank, shop_storage must send S2C 0x65 first.

---

## 3. Server state and data model

### 3.1 Persistence (accounts.json)
Keep one JSON file. Add fields only, no new top-level keys, because the account map is iterated by username. Do every mutation plus `_save_accounts()` (ws:405) under one `threading.Lock`, since connection threads and the combat thread both touch it.

```jsonc
"test": {
  "password": "test",
  "second_password": null,            // null -> use "password" for C2S 0x51
  "gender": 1,                        // S2C 0x02 account_gender_flag; gift 0x14 check
  "cash": 50000,                      // Wind Cash  (mall+0x588)
  "mileage": 0,                       // Mileage    (mall+0x58C)
  "first_purchase_done": false,
  "first_purchase_notice": false,     // -> 0x02 flag byte / 0x70 first_purchase_bonus
  "cash_box": [                       // account storage (mall+0x28)
    {"serial": 4097, "item_id": 3379, "kind": 1, "qty": 1, "expire": null, "origin": 0}
  ],
  "gift_inbox": [                     // S2C 0x6D source
    {"sender": "Bob", "message": "hi", "item_id": 3381, "serial": 4100, "delivered": false}
  ],
  "characters": [{
      "name": "TestHero", "...": "...",
      "str": 3, "dex": 3, "int": 1, "spr": 2,
      "hair": 123,
      "tab_slots": [35, 35, 35],      // replaces the hardcoded 35/35/35 at ws:2071-2073; each <= 45
      "bank_slots": [35, 35, 35],     // shop_storage S2C 0x65; each <= 60
      "cash_items": [                 // mall+0x04 (+0x38 when equipped)
        {"serial": 4098, "item_id": 3323, "kind": 2, "qty": 1,
         "expire": "2026-09-24T23:59:00", "origin": 0, "equipped": false}
      ]
  }]
}
```

- **Serials:** u32, unique across all accounts. At startup, `next_serial = max(every serial in all boxes / cash_items / inboxes) + 1`, with a minimum of 0x1000. No extra key is needed, and serials are never reused.
- **`origin`:** 0 = bought with cash, 1 = bought with mileage, 2 = gift, 3 = event/GM grant. Only 0 and 3 mean anything to the client.
- **`expire`:** stored as ISO text. On the wire it becomes a SYSTEMTIME (dow may be 0, ms 0); `null` becomes 16 zero bytes.

### 3.2 Session fields
| Key | Use |
|---|---|
| `in_cash_shop` (bool) | F2/F7. Gates 0x43/0x45/0x47. Freezes combat, respawn, wander and position sampling |
| `mall_snapshot` {box:set, char:set} | Serials sent in 0x6A (diagnostics, 0x42 validation) |
| `pre_mall` {map, x, y} | World restore target for F7 |
| `pos` (x, y) | Last-known player position. Copy it from the combat driver's memory read (ws:1800-1803) while not in the mall. Needed by F2/F7/F14 |
| `pw_ok` {window_id: time} | F6/F18 gate |
| `gifts_sent` set | 0x6D records already delivered this session |
| `megaphone_last` | Rate limit |

### 3.3 Content
- **gamedef `items`:** `Cash, Cash_Cls, Cash_T, Cash_P, Cash_V, Cash_B, Gender, Type, Kind`. Today's `_gamedef_item()` SELECT (ws:59) leaves them out. Add `_cash_def(idx)` with its own query and cache, and do not change `_gamedef_item`'s return shape, which the shop uses.
- **Constants:**
  - `SLOT_EXT = {1884:('tab',0), 1885:('tab',1), 1886:('tab',2), 1887:('bank',0), 1888:('bank',1), 1889:('bank',2)}` **[bin]** order confirmed
  - `STAT_RESET = {3214,3215,3234,3235}`, `NAME_CHANGE = 1895`, `NOTES = {1894,3320}`
  - `MEGA_SUPER = {3377,3378,3379}`, `MEGA = {3380,3381}`
  - `REGION_STONE = {3430,3432,3434}`, `FRIEND_STONE = {3429,3431,3433}`
  - `RANDOM_LOOK = {0xD39,0xD3A,0xD50}`, `GIFT_CARDS = range(3327,3333)`
- **gamedef `maps`** (`index, current_map_id, portal_code, next_map_id, xpos, ypos, map_name`) and `server/portals.json`: landing points for F13.
- **Config:**
  - `MILEAGE_BONUS_RATE` (0.0)
  - `DELETE_REFUND_RATE` (0.3, the client constant)
  - `DEFAULT_CASH_NEW_ACCOUNT` (dev: 50000)
  - `EXP_BOOST = {3321:1.5, 3322:1.5, 3323:1.5, 3324:2.0, 3325:2.0, 3326:2.0}`
- **Packet building:** load grammars from `protocol_spec.json` through `wsproto.Grammar` (cache by key) and add one helper, `_send_spec(sock, session, opcode, fields, assume=None)`. Give it a per-opcode default assume table for server-side encoding:
  - 0x6F gate → True
  - 0x76 `target_uid == local_player_uid` → (recipient is the owner)
  - 0x72 `item_def(item_id).duration_type == 2` → (`Cash_T == 2`)
  - 0x72 random-look condition → (`item_id in RANDOM_LOOK`)

  Map records with `_wire_record(rec) -> dict` (serial, item_id, limit_type/item_kind, quantity, expire_*, origin).

---

## 4. Current server status and provable bugs

**Status: nothing in this group is implemented.**
- `_dispatch` (ws:601-667) has no branch for C2S 0x42, 0x43, 0x45, 0x46, 0x47, 0x48, 0x49, 0x4A, 0x4C, 0x51, 0x70 or 0x71. Each one logs "Unhandled opcode" (ws:660).
- None of the 17 S2C opcodes (0x6A, 0x6B, 0x6C, 0x6E, 0x6F, 0x70, 0x71, 0x72, 0x73, 0x75, 0x76, 0x77, 0x79, 0x93, 0x98, 0x9A, 0x9B) has a builder, apart from the disabled 0x6F stub.

| # | Bug | Evidence (spec ↔ code) | Severity | Fix |
|---|---|---|---|---|
| B1 | **S2C 0x80 misread as "CashShopBalance".** The u32 is a UI window id, not a balance | Comment at ws:984-986; `_build_opcode_80` ws:1107-1112; `_send_cash_balance` ws:1124-1132 sends winnie 999999. Spec 0x80: `u8 result; if(result==1){u32 target_window_id}`. Handler 0x44E2FE calls `FUN_00446300(id,0,3,0,1)`, so 999999 would try to open window 999999. Dead code today (grep: never called) | wrong_behavior | Delete both. Add `_handle_password_verify` (C2S 0x51) → `0x80 {1,id}` for whitelisted ids, else `{0}` |
| B2 | Mall and cash-item C2S unhandled | ws:601-667. Specs for 0x47, 0x48, 0x49, 0x4A and the Note 0x4B all open "Waiting for the server to response." and wait for 0x71/0x72/0x73/0x76/0x77, so the client **stays stuck in a modal**. 0x46 is re-sent on every minimize/restore until 0x70 arrives. After 0x42 the client stays on the preview map | wrong_behavior | Handlers per §2 |
| B3 | Disabled "0x6F render+input activation" block describes the wrong packet and would wipe cash items | ws:789-821. The comment (rep movsd, `[+0x5B8]=0`, `FUN_0045AF00`, `FUN_0045C470`) describes the **0x6A** block (spec 0x6A references). `b'\x00'*28` sent as 0x6F = `count=0` (purge every cash item from every bag) plus 24 ignored bytes | wrong_behavior (latent) | Delete the block. Build 0x6F from the grammar (F1) |
| B4 | Wind Cash conflated with Winnie/Victy | ws:980 `u32 winnie(cash)`; ws:995 `_DEFAULT_WINNIE = 999999 # cash/premium currency`; `_wallet` ws:997-1000. Spec 0x18: the u32 is **Victy** → `player_info+0x238`. Wind Cash is `mall+0x588`, written only by 0x6A/0x6C/0x70/0x71. Also, 0x03 sends `winnie_points=0` (ws:2047) while 0x18 sends 999999, so the Victy label flips between 0 and 999999 | cosmetic (design conflation) | Rename to victy. Add `account['cash']` / `['mileage']` (§3) |
| B5 | Tab capacities hardcoded, so slot extensions will revert | 0x03 builder ws:2071-2073 always sends 35/35/35. `FUN_0045E9F0` (0x6C for 0x75C-0x75E) adds +5 client-side, and the next 0x03 overwrites it | wrong_behavior (future) | Send `char['tab_slots']`, clamped ≤ 45. Persist on purchase (F4) |
| B6 | No 0x6F after 0x03/0x07, so cash items vanish on every map load | Enter-world ws:733-787 and portal replay ws:1280-1320 send 0x03 but never 0x6F. Spec 0x03: the handler "memsets the unsent 4th item tab (scene+0x830 u16x45, +0x88A u8x45)", which is the cash bag `player_info+0x7E8` that 0x6F fills | wrong_behavior (future) | Send 0x6F after 0x44 in both sequences (F1) |
| B7 | **wsproto treats hex literals in conditions as client state** (same defect as chat_mail_gm B9) | `wsproto.py` `_eval` lines 183-184: `re.findall(r'[A-Za-z_]\w*')` pulls `x0D50` out of `0x0D50`, and the filter only drops names that fullmatch `0x..`. The condition becomes a ClientStateCondition and evaluates **False** on encode and decode. **[run]** `Grammar("u16 a\nif(a == 0x10) { u8 b }").encode({'a':16,'b':7})` → `1000` (b dropped); with `a == 16` → `100007`. **0x72** random-hair `hair_code` is never emitted, so the client reads the serial from the wrong offset. The same pattern appears in C2S 0x0D movement (`& 0xF`) and in S2C 0x07/0x04/0x2B/0x2E/0x09/0x0C/0x01 conditions | crash_or_desync | Match names with `(?<![\w])[A-Za-z_]\w*`, or strip numeric literals before collecting names. Add a unit test |
| B8 | `wsdev.py sendspec` cannot pass `assume` | wsdev.py `cmd_sendspec` 432-452: `Grammar(spec['grammar']).encode(fields)`. With client-state gates forced False: **0x6F encodes to 0 bytes** **[run]**; 0x76 encodes the 12-byte non-local form (the owner then reads a stale serial/count); 0x72 drops the 16-byte expiry for period items | wrong_behavior (tooling) | Accept an `"__assume__": {expr: bool}` key in the JSON, or default client-state conditions to True for S2C encode |
| B9 | The combat driver will treat the mall preview avatar as the player | `_combat_driver` ws:1737-1810 caches the scene entity with uid==1. **[bin]** `FUN_0045C470` gives the preview avatar uid `player_info+0x1D8` (the same uid 1) and calls `RegisterLocalPlayer` (0x45C65C). `_memory_melee` (ws:1678) can then resolve hits against server-side monsters that 0x6A destroyed client-side, and `_tick_respawns` / wander can send 0x1A/0x12 into the preview scene | cosmetic | Skip sessions with `in_cash_shop` in `_memory_melee`, `_tick_respawns` and wander. Do not sample `pos` |
| B10 | 0x07 builder labels `+0xE2` "marriage" | ws:2310-2317 (also ws:2105 in the PySlayer-style builder). Specs 0x75 / 0x5B: `entity+0xE2` is **room_no** (0x81 = cash-shop marker), with the `+0xE1` room type and the `+0xD0` title. Sending 0 is correct today | cosmetic | Rename to room_no. ~~F17 needs 0x81 for players in the mall~~ F17 is superseded (a player in the mall is hidden from the map), so 0 stays correct |
| B11 | 0x02 login builder field labels | Docstring ws:2666 and appends ws:2676-2678: `unknown_1` = `cash_first_purchase_flag` (→ `game_state+0x700`), `has_premium` = `account_gender_flag` (`scene+0x130`), `premium_flags` = manner points. The values 0/0/0 are safe, but gender 0 makes Gender-1 items unequippable and breaks the gift 0x14 check (spec 0x02) | cosmetic | Relabel. Source the values from the account (F1) |

**Cross-group dependency:** refusal and megaphone lines need a correct chat builder. `_handle_chat` (ws:954) and `_send_chat_line` (ws:1535) prepend an extra `u8 1` before the 17-byte name, but spec 0x16 is `str[17] sender_name, u8 len, text` (LIVE_TEST_LOG chat bug #3). That fix belongs to the chat group; this group uses 0x15/0x90 built from the grammar.

---

## 5. Implementation plan

Priorities use the lead's scale. This group is cash / GM content, so most items are P3. The codec, tooling and shared password gate rank higher because other groups need them.

| id | Title | Opcodes | Effort | Pri | Depends on |
|---|---|---|---|---|---|
| premium_cash-wsproto-hexfix | Fix the wsproto hex-literal condition bug (B7) + unit test | 0x72 (and 0x0D, 0x07, 0x09, ...) | S | P1 | - |
| premium_cash-sendspec-assume | `wsdev sendspec` supports `assume` (B8); `_send_spec` helper with per-opcode default assumes | 0x6F, 0x72, 0x76 | S | P2 | - |
| premium_cash-password-gate | C2S 0x51 → S2C 0x80. Delete `_build_opcode_80`/`_send_cash_balance` (B1). Shared with shop_storage (bank 0x1A7) | 0x51, 0x80 | S | P2 | - |
| premium_cash-wallet-model | Account cash/mileage/box/gift_inbox/serials; `char.cash_items/tab_slots/bank_slots`; lock + persistence; rename winnie→victy (B4); 0x02 flag bytes (B11) | 0x02 | M | P2 | - |
| premium_cash-catalog | `_cash_def` loader + constants. Extend `dump_item_catalog.py` to export def +0x1F0/+0x1F4/+0x1F6/+0x1FC and diff against gamedef (T-CAT) | - | S | P2 | - |
| premium_cash-cash-inventory-api | `cash_find(char,item_id)`, `cash_consume(serial,n)`, `next_serial()`. Used by F8-F14 and by other groups (chat_mail_gm 0x4B→0x77, item 0x72→0x9C) | - | S | P2 | wallet-model |
| premium_cash-owned-list-sync | 0x6F builder (with assume); send after 0x44 in enter-world and change-map (B6); delete the dead block (B3); tab caps from `tab_slots`, clamped ≤ 45 (B5) | 0x6F, 0x03 | S | P2 | wallet-model, catalog, sendspec-assume |
| premium_cash-map-replay-helper | Factor `_replay_map(sock, session, map_id, x, y)` out of ws:1280-1320 (0x08/0x03/0x07/0x0A/0x28/0x44/0x6F/0x1A); track `session['pos']` | 0x08, 0x03, 0x07, 0x6F | S | P2 | owned-list-sync |
| premium_cash-mall-enter | `!mall` chat command → 0x6F/0x6D/0x6A. Gates, snapshot, combat freeze (B9); GM `!cash N` → 0x70 | 0x03(C2S), 0x6A, 0x6D, 0x70 | M | P3 | wallet-model, owned-list-sync |
| premium_cash-mall-exit | C2S 0x42 applies moves (idempotent) → 0x6B → replay → 0x6F | 0x42, 0x6B | M | P3 | mall-enter, map-replay-helper |
| premium_cash-balance-refresh | C2S 0x46 → 0x70 (first-purchase bonus flag) | 0x46, 0x70 | S | P3 | wallet-model |
| premium_cash-buy | C2S 0x43 single / cart / slot extension → one 0x6C per item; mileage payment; slot caps clamped and persisted | 0x43, 0x6C | M | P3 | mall-enter, catalog |
| premium_cash-box-delete | C2S 0x45 → 0x6E with the 30% mileage refund | 0x45, 0x6E | S | P3 | mall-enter |
| premium_cash-gift | C2S 0x47 → 0x71; record in recipient box + inbox; online push of 0x79 / 0x6D | 0x47, 0x71, 0x79, 0x6D | M | P3 | buy, password-gate |
| premium_cash-use-generic | C2S 0x48 → 0x72: random/fixed hair and dye (persist `char.hair`), period activation + EXP multiplier, refusal packet | 0x48, 0x72 | M | P3 | cash-inventory-api, owned-list-sync, wsproto-hexfix |
| premium_cash-expiry | 60 s ticker → 0x93 for activated records; expired records dropped from 0x6F | 0x93 | S | P3 | use-generic |
| premium_cash-rename | C2S 0x49 → 0x73 + 0x74; name unique across accounts | 0x49, 0x73, 0x74 | S | P3 | cash-inventory-api |
| premium_cash-stat-reset | C2S 0x4A → 0x76 (18 B owner / 12 B others); failure = unchanged stats + serial 0 | 0x4A, 0x76 | M | P3 | cash-inventory-api, sendspec-assume |
| premium_cash-megaphone | C2S 0x4C → 0x90 broadcast + consume (0x72 or 0x6F); rate limit | 0x4C, 0x90, 0x72 | S | P3 | cash-inventory-api |
| premium_cash-region-warp | C2S 0x70 → 0x9A + replay to dest | 0x70, 0x9A | M | P3 | map-replay-helper, cash-inventory-api |
| premium_cash-friend-warp | C2S 0x71 → 0x9B + replay to the target (needs per-session uid + position) | 0x71, 0x9B | M | P3 | map-replay-helper, multiplayer |
| premium_cash-presence | ~~0x75 / 0x5C broadcast; 0x07 room_no=0x81 for players in the mall (B10)~~ Done as "hidden from the map" (P8 stage 4; F17 superseded): 0x06 on entry, the exit replay's 0x05 | ~~0x75, 0x5C,~~ 0x06, 0x05 | S | P3 | mall-enter, multiplayer |
| premium_cash-mileage-event | GM/event mileage credit → 0x70 + 0x98 | 0x70, 0x98 | S | P3 | balance-refresh |

---

## 6. Live test plan

**Harness:**
- `python wsdev.py up` puts the client in world as TestHero, uid 1.
- Inject with `wsdev.py sendspec <op> '<json>'`, or raw with `wsdev.py send <op> <hex>`.
- Capture C2S with `wsdev.py cap <secs> <action>`.
- Screenshot with `wsdev.py shot`.
- UI coordinates of the mall and cash-item dialogs are not known yet: take a `shot` first, then `click x y`.

**[run]** Every raw hex payload below was decoded with the spec grammar (with `assume` where needed) and has zero trailing bytes. Serial plan: owned records 0x1001-0x1008, box records 0x2001+.

### 6.0 Prerequisites
- **T-CAT (content, safe).** Extend `dump_item_catalog.py` to read def +0x1F0 (u32), +0x1F4 (u16), +0x1F6 (u16), +0x1FC (u32) for ids 1884-1895, 3214-3235, 3320-3332, 3377-3437, 3951-3952. Expect them to equal gamedef `Cash`, `Cash_Cls`, `Cash_T`, `Cash_P`.
- **T-218 (safe).** Read byte `[0x70EECC]+0x218` and the 32 bytes at `[0x70EECC]+0x131` after login. Expect +0x131 to be empty or not the MD5, giving flag 1. If the flag is 0, the gift password dialog cannot open ('no SS# in this ID...').
- **T-6F seed (state_change).** Give TestHero 8 owned cash items:
  - Megaphone 3381 x5 = 0x1001
  - Change Nickname 1895 = 0x1002
  - Reset 5 Stat Points 3214 = 0x1003
  - General Stones 3434 x10 = 0x1004
  - Friend Stones 3433 x10 = 0x1005
  - Message Pad 1894 = 0x1006
  - +50% EXP/7 Days 3323 (kind 2, not activated) = 0x1007
  - Random Hair Coloring 3408 = 0x1008

  `sendspec` would send 0 bytes (B8), so use raw:
  ```
  python wsdev.py send 6F 080000000001100000350d01000500000000000000000000000000000000000000000210000067070100010000000000000000000000000000000000000000031000008e0c0100010000000000000000000000000000000000000000041000006a0d01000a000000000000000000000000000000000000000005100000690d01000a0000000000000000000000000000000000000000061000006607010001000000000000000000000000000000000000000007100000fb0c020001000000000000000000000000000000000000000008100000500d01000100000000000000000000000000000000000000
  ```
  - Expected: 8 icons in the inventory's cash (4th) tab, with counts 5 / 1 / 1 / 10 / 10 / 1 / 1 / 1.
  - Re-injecting replaces them rather than duplicating, because the purge runs first.
  - A portal walk (0x03) empties the tab, which reproduces B6.

### 6.1 S2C injection tests

| Test | Opcode | Action | Expected | Risk |
|---|---|---|---|---|
| T-70 | 0x70 | `sendspec 70 '{"cash_balance":50000,"mileage_balance":1200,"first_purchase_bonus":1}'` | Popup "Thank you for your first purchase of wind cash. 500 mileage..." (works in world). Inside the mall, labels 0x67/0x68 show 50000/1200 | safe |
| T-98 | 0x98 | `sendspec 98 '{}'` | Chat line "※Mileage Event※ You got bonus mileage." (the CP949 ※ may render as garbage) | safe |
| T-93 | 0x93 | After T-6F: `sendspec 93 '{"item_serial":4103}'` | Chat "[+50% EXP/7 Days] is expired."; the record leaves list +0x04. The bag icon likely **stays**, because 0x93 does not touch bags; this confirms F15 | state_change |
| T-73a | 0x73 | `sendspec 73 '{"result":0}'` | Popup "The name is already being used. Please try another name." | safe |
| T-73b | 0x73 | After T-6F: `sendspec 73 '{"result":1,"item_serial":4098}'` | Popup "Your character name has successively changed."; the Change Nickname icon disappears. The name itself does not change | state_change |
| T-74 | 0x74 | `sendspec 74 '{"uid":1,"new_name":"Renamed"}'` | Overhead and stat-panel name become "Renamed" (client memory only; a relog restores it) | state_change |
| T-77a/b | 0x77 | `sendspec 77 '{"result":0}'`, then `sendspec 77 '{"result":1,"cash_item_serial":4102}'` | a: "Failed to send the message ." b: "You successfully sent the message."; Message Pad removed | safe / state_change |
| T-9A | 0x9A | `sendspec 9A '{"result":0}'`, then `sendspec 9A '{"result":1,"cash_item_serial":4100}'` | a: "You are unable to transfer to the area." b: General Stones count 10→9; no map change | safe / state_change |
| T-9B | 0x9B | `sendspec 9B '{"result":0}'`, then `'{"result":1,"cash_item_serial":4101}'` | a: "The character doesn't exist..." b: Friend Stones 10→9 | safe / state_change |
| T-76 | 0x76 | After T-6F: `send 76 010000000500050003000300031000000100` (uid 1, stats 5/5/3/3, serial 0x1003, count 1) | Stat window shows 5/5/3/3 with recomputed free points; max HP/MP recomputed; sparkle effect and sound 0x140; the Reset-5 icon is removed | state_change |
| T-72a | 0x72 | After T-6F: `send 72 01000000500d6f0008100000` (Random Dye, hair_code 111, serial 0x1008). Hand-built because of B7 | Hair slot set to code 111 (may render oddly if 111 is not a valid hair sprite); effect and sound 0x140; dye icon removed | state_change |
| T-72b | 0x72 | After T-6F: `send 72 01000000fb0c07100000ea0709000000180017003b0000000000` (EXP item 3323, expiry 2026-09-24 23:59) | Effect and sound; the EXP item leaves the bag and appears in the window-5 period list with its remaining days. Fails or garbles if the client def+0x1F6 != 2 for 3323 (T-CAT) | state_change |
| T-72c | 0x72 | `send 72 01000000000000000000` (refusal form: item 0, serial 0) | Only effect and sound 0x140 on the player; no appearance or item change. If a waiting box is open (T-48), it closes | safe |
| T-75 | 0x75 / 0x5C | `sendspec 75 '{"uid":1}'`, then `sendspec 5C '{"uid":1}'` | Cash-shop sign sprite 0x168 above TestHero; gone after 0x5C. While the sign is set, arena/room actions may be refused | state_change |
| T-80 | 0x80 | `sendspec 80 '{"result":0}'`; optionally `sendspec 80 '{"result":1,"target_window_id":505}'` | a: "Invalid password. Please check your password again." b: nothing happens (no cash item selected, `+0x5BC==0`). Do NOT inject any other ids | safe |
| T-6A | 0x6A | In world with the player visible: `send 6A 50c30000b00400000100000001200000330d01000100000000000000000000000000000000000000` (cash 50000, mileage 1200, box = Super Megaphone 3379 serial 0x2001) | World disappears; preview map with a preview avatar in TestHero's look; windows 0x1CF/0x1FB/0x1FC/0x1FD open; labels 50000/1200; box lists 1 Super Megaphone. Take a `shot` for UI coordinates. **No way back until the F7 handler exists: run `wsdev.py restart` afterwards.** Sending 0x6A before the 0x07 spawn would crash (null `scene+0x970`) | disruptive |
| T-6C | 0x6C | Inside the mall (after T-6A): `send 6C 015cc10000e204000002200000350d01000100000000000000000000000000000000000000` (cash 49500, mileage 1250, record 3381 serial 0x2002); then `sendspec 6C '{"result":14}'` | a: labels 49500/1250, popup "50% bonus mileage has been deposited.", box gains a Megaphone. b: "You are short of cash." | state_change |
| T-6C-ext | 0x6C | Inside the mall: `sendspec 6C '{"result":1,"cash_balance":45000,"mileage_balance":1250,"item_serial":8196,"item_id":1884,"item_kind":1,"quantity":1}'` | Popup "5 inventory equipment slot was extended."; the box does not gain an item; `[0x70EECC]+0x3A9` goes 35→40 | state_change |
| T-6E | 0x6E | Inside the mall: `sendspec 6E '{"result":1,"mileage_balance":2000,"item_serial":8193}'`; then `'{"result":0}'` | a: Super Megaphone gone from the box, mileage 2000. b: "You failed to delete the item." | state_change |
| T-79 | 0x79 | Inside the mall: `send 79 03200000340d01000b00000000000000000000000000000000000300` | Box gains "11 Megaphones" (origin 3, no popup: 0x79 does not read origin) | state_change |
| T-71 | 0x71 | Inside the mall: `sendspec 71 '{"result":2}'`, `'{"result":20}'`, `'{"result":12}'`, `'{"result":1,"cash_balance":40000,"mileage_balance":1300}'` | Popups in order: character doesn't exist / other gender / own character / "You sent the gift. 50% bonus mileage..." + labels | safe |
| T-6B | 0x6B | Inside the mall: `sendspec 6B '{}'` | Inventory UI rebuilt; registry `HKCU\Software\Hamelin\WindSlayer\TestHero\Wish0..11` written; the client **stays on the preview map** (confirms F7 needs a replay) | state_change |

### 6.2 C2S capture tests

Each test needs the T-6F seed or T-6A first. After a capture that leaves a modal open, inject the listed reply to unstick the UI.

| Test | Opcode | Action | Expected capture | Risk |
|---|---|---|---|---|
| T-4C | 0x4C | Double-click Megaphone in the cash tab → window 0x3FF → type "hello" → click send (ctrl 3): `wsdev.py cap 3 click <send x y>` | C2S 0x4C 63 B: `35 0d` + "hello" NUL-padded to 61. The window closes; no modal | safe |
| T-49 | 0x49 | Double-click Change Nickname → 0x3F1 → type "NewHero" → OK (`cap 3 click ...`) | C2S 0x49 17 B `"NewHero\0..."`; waiting box shown. Recover with T-73a | state_change |
| T-4A | 0x4A | Double-click Reset 5 Stat Points → 0x3FA → press STR decrement twice → confirm (ctrl 0xC) | C2S 0x4A 6 B `8e 0c 02 00 00 00`; waiting box. Recover with T-76 (18 B) | state_change |
| T-48a | 0x48 | Double-click Random Hair Coloring → 0x3F4 → OK | C2S 0x48 2 B `50 0d`; waiting box. Recover with T-72c or T-72a | state_change |
| T-48b | 0x48 | Double-click +50% EXP/7 Days → 0x3F4 → OK | C2S 0x48 2 B `fb 0c` (the period path is the same on the wire). Recover with T-72b | state_change |
| T-70c | 0x70 | Double-click General Teleport Stones → world map 0x0A → pick a region → pick a destination → OK in 0x471 | C2S 0x70 6 B: `6a 0d` + i32 map id (e.g. `65 00 00 00` = 101). No modal. Confirms the map-id encoding | safe |
| T-71c | 0x71 | Double-click Friend Teleport Stones → 0x472 → type "Bob" → ctrl 2 | C2S 0x71 19 B: `69 0d` + "Bob" padded to 17. No modal | safe |
| T-4B | 0x4B | Double-click Message Pad → 0x3FD → recipient "Bob", text "hi" → send (ctrl 3) | C2S 0x4B 110 B: `66 07` + str[17] + str[91]; waiting box. Recover with T-77a | state_change |
| T-43 | 0x43 | T-6A (cash 50000), then pick a cheap item in 0x1CF → Buy → OK in 0x1F8. Repeat with the mileage checkbox on. Then a 2-item cart via 0x1FB → 0x1F7 | Single: `00 01 <u16 id>` (4 B). Mileage: `01 01 <id>`. Cart: `00 02 <id1> <id2>`. No modal. Then T-6C checks the per-item reply | state_change |
| T-43-ext | 0x43 | In the mall: panel 0x1FD button 1 → picker 0x41C → equipment tab → OK in 0x1FF | `00 01 5c 07` (item 1884) | state_change |
| T-45 | 0x45 | In the mall with the T-6A box item: select it in 0x1FC → ctrl 1 → confirm 0x1FA (refund shows 150 = 30% of 500) → OK | C2S 0x45 6 B: `33 0d 01 20 00 00` | state_change |
| T-51 | 0x51 | In the mall, only if T-218 = 1: item row Gift → dialog 0x201 → type "test" → OK | C2S 0x51 25 B: "test\0" + stack bytes + `f9 01 00 00`. Then `sendspec 80 '{"result":1,"target_window_id":505}'` → gift dialog 0x1F9 opens | state_change |
| T-47 | 0x47 | Continue T-51: recipient "Bob", message "hi" → Send | C2S 0x47 23 B: `00 <u16 id> "Bob"x17 02 68 69`; waiting box. Recover with `sendspec 71 '{"result":2}'` | state_change |
| T-46 | 0x46 | In the mall, press ctrl 0xA8 (refresh) **before** charge: no packet expected. Then press charge (ctrl 5): **the game minimizes and a browser opens a dead URL**. Restore the game window (alt-tab or `wsview` autoborderless) | First click: nothing. After restore: C2S 0x46, 0 B. Restoring again without a reply sends another 0x46. `sendspec 70 ...` stops the repeats | disruptive |
| T-42 | 0x42 | In the mall after T-6C / T-79 (box grew): move a box item to the character panel, then close 0x1CF (ctrl 3) | C2S 0x42: `00` (or `01` after T-6E), i32 A = number of new box serials (e.g. 0x2002, 0x2003), i32 B = 1, then the serials. The client stays on the preview map with windows hidden. **Run `wsdev.py restart` afterwards** (until F7 is implemented) | disruptive |

### 6.3 End-to-end tests (after implementation)
- **E1.** `!mall` → buy a Megaphone with cash → close → after the replay the cash tab shows the megaphone → use it → the second client (`WindSlayer_p2.exe`) sees the orange line → the count drops.
- **E2.** Portal walk after E1: the megaphone is still in the cash tab (B6 fixed).
- **E3.** Buy the equipment slot extension twice → close → portal: the inventory tab has 45 slots. A third purchase is refused with 0x6C result 0 (cap), and 0x6F still decodes.
- **E4.** Rename, then relog: the character list shows the new name and enter-world works.
- **E5.** Activate the 7-day EXP item: killing a Pupu gives +15 exp instead of +10. Edit the expiry in the JSON to now+2 min and wait: the 0x93 chat line appears.

---

## 7. Open questions

1. **Mall entry in EN:** the client cannot request it (§1.4, verified down to the HUD jump table). Did OUTSPARK leave the mall disabled ("Coming Soon"), and does a server-pushed 0x6A make every mall feature work in this build? The mall UI may be partly stubbed. T-6A answers this first.
2. **World restore after close:** is 0x6B plus the full replay right, or is 0x03 alone enough from scene mode 4? 0x08 special-cases only mode 5, so mode 4 should take the normal path.
3. **Cart replies:** one 0x6C per item (the handler reads exactly one record) or something else? Test with T-43 cart + T-6C.
4. **Client def vs gamedef:** is +0x1F0/+0x1F4/+0x1F6/+0x1FC = Cash/Cash_Cls/Cash_T/Cash_P? This decides the 0x72 length (T-CAT).
5. **`scene+0x131`:** what writes the 32-byte string (launcher args, login dialog)? It decides `scene+0x218` and so whether gifts are reachable (T-218).
6. **Hair codes:** the digit encoding of the hair appearance code, and the style formula in `FUN_00427080` / `FUN_004270F0`. Needed to persist `char['hair']` and to pick random results server-side.
7. **Megaphone delivery:** the original S2C for the broadcast and the item decrement is unknown (not in SubHandler4, and no S2C spec mentions a megaphone). Is 0x90 plus 0x72 acceptable (T-4C + T-72c)?
8. **0x72 as a generic refusal:** acceptable UX, given it sparkles on failure? The alternatives (0x73/0x77 failure popups) also close box 0x16 but show the wrong text.
9. **Mileage economy:** what was the original accrual rate (the popup says "50% bonus mileage")? Do mileage purchases earn mileage? Config for now.
10. **Stat reset minimums:** per-class minimum stats (creation values?). The client only enforces ≥ 0.
11. **Items 0xD6C/0xD6D/0xF6F/0xF70 (Element Separators):** the `FUN_004688D0` → C2S 0x72 → S2C 0x9C path belongs to the item group. It needs this group's `cash_consume` for the tool serial.
12. **Region destination ids:** are all region-window control values below 100 (map suffix)? Which spawn point should a warp land on? gamedef `maps` rows are portal landings, not town spawns.
13. **Friend warp semantics:** does it move the user to the friend (assumed) or summon the friend? Refuse cross-channel and offline targets?
14. **Cash avatar equipment** (488 Type-1 cash items): the equip/unequip path moves records between +0x04 and +0x38 via 0x1D / 0x1E / 0x24, and the worn look uses the alternate equip grid (+0x1F0). Needs a joint design with item_inventory.
15. **0x6D thank-you path:** what did the original server answer to C2S 0x4B with item 9999? A 0x77 success pops "You successfully sent the message." (chat_mail_gm Q6).
16. **Window 3 ctrl 6** is inert in `FUN_00446300`. Is there any other handler for the HUD Gift icon (e.g. a tooltip-only control)? If not, received gifts only surface inside the mall.
