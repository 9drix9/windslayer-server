# shop_storage: NPC shop, bank (storage) and player item stalls

Design doc for the server team. It covers the 26 specs in `re_tools/corpus/systems/shop_storage.json` plus four cross-group packets these features can't work without:
- C2S `0x51` PasswordVerifyForWindow (premium_cash group)
- C2S `0x61` ItemStallVisitRequest (trade group)
- C2S `0x15` / S2C `0x25` for skill 194 "Open Stall" (combat_skill group)
- S2C `0x18` GetItem (item group; it is the buy reply)

Sources, most authoritative first: client asm/decomp in `re_tools/corpus` (cited by VA or `FUN_`) > `LIVE_TEST_LOG.md` > `WindSlayer2Game/server/windslayer_server.py` (cited as `ws:<line>`, 2793-line file) > memory notes > PySlayer.

| Sub-system | C2S | S2C |
|---|---|---|
| NPC shop (window `0xD`) | `0x0B` NpcShopBuy, `0x0C` NpcShopSell | `0x18` GetItem, `0x19` SellItemResult |
| Bank (window `0x1A7`) | `0x51` password, `0x3C` deposit item, `0x3D` withdraw item (2 send paths), `0x3E` deposit gold, `0x3F` withdraw gold | `0x80` SecondPasswordResult, `0x65` BankContents, `0x66` deposit result, `0x67` withdraw result, `0x68`/`0x69` StorageGoldUpdate |
| Item stall (window `0x259`) | `0x15` (skill 194), `0x5E` StallOpen, `0x5F` StopForEdit, `0x60` Close, `0x61` Visit, `0x62` StallBuyItem | `0x25` (skill 194), `0x82` open result, `0x83` stop result, `0x84` close result, `0x85` sign broadcast, `0x86` sign remove, `0x87` item list, `0x88` buy result, `0x89` sold notify |

**New evidence gathered for this doc** (read-only):
- **EN NPC table = `WindSlayer2Game/hs/windslayer.hni`.** It uses the same asset cipher as `.hii` (`plain[i] = raw[i] + [0xE9,0xDE,0xE0][i%3]`, drop the 20-byte SHA-1 footer) and decodes to `Number_of_NPC: 180`. Each record is `<idx> <title> <hsi> ... item: <4-digit decimal ids x100> ... UI: <n> ...`.
  - 56 merchants (`UI: 13`), one bank NPC (idx 97, `UI: 423` = `0x1A7`), and one village-transfer NPC (idx 117, `UI: 600`).
  - EN shop lists equal KR `gamedef npcs.item` except for npc 19 and npc 23 (KR appends 4281) and npc 83 (KR adds 4521/4520/4356/4357 and 2200-2214, none of which exist in the 4248-item EN catalog).
- **The stall entry point is a skill.** Item 194 "Open Stall" has Type 3, Lv 6, Buy 3000, PMoney 100, CT 3000 in EN `hii`. NPC 148 (Leon) sells it, and `windslayer.hbi` says: *"Open Stall can be learned from Leon when you reach level 6."*
  - Casting it sends C2S `0x15 {u16 194}`. Only the server's S2C `0x25 {194}` opens the stall window: asm `0x453D1C CMP CX,0xC2 / PUSH 1 / PUSH 3 / PUSH 0x259 / CALL FUN_00483430`.
  - That window open reaches `FUN_00465fe0` and then `FUN_00466390(1)`, which runs the flea-market and manner checks.
- **S2C `0x87` clears the list before rebuilding.** Its `FUN_00466390(0)` calls `FUN_0047d700`, whose default case frees every node of window `0x259`'s list at `+0xB0`. Re-sending `0x87` therefore rebuilds the buyer list without duplicates.
- **Leaving mode `0x259` clears the browsed-stall uid.** `FUN_004665e0` sets `ctx+0x1C = ctx+0x20 = ctx+0x04 = 0`, so a stale `0x86` can't close a later window. The `0x86` spec's concern about this doesn't apply.
- **Client bank-stacking quirk.** `FUN_00427940` (bank etc-tab add/remove) ends its compaction loop on the **consumable** cap byte (`+1`) instead of the etc cap (`+2`). Keep both caps equal.

---

## 1. How the client implements it

### 1.1 One mode controller for all three windows
All three features share `ctx = game_state+0xD5C` (game_state is the static `0x70EA00`).

| ctx offset | Meaning |
|---|---|
| `+0x00` | active trade mode: `0xD` NPC shop, `0x1A7` bank, `0x259` item stall, 0 none |
| `+0x04` | uid of the stall being browsed (written by S2C `0x87`, compared by `0x86`, sent in C2S `0x62`) |
| `+0x08` / `+0x0C` | game_state / UI root. SubHandler1 (`0x46CF40`) ignores S2C `0x66..0x89` unless both are set |
| `+0x14` | game socket |
| `+0x18` | player_info = scene+0x48 |
| `+0x1C` | own stall in setup (edit) mode |
| `+0x20` | own stall selling |

`FUN_00465fe0(ctx, mode)` switches modes:
- It refuses in arena or play-room maps (`[[gs+0x4D8]+0x70]==1`).
- It refuses while `+0x20 != 0` and the mode is `0x259`: a selling stall can't be left.
- It calls `FUN_004665e0(old_mode)` first.
- Inventory window 9 buttons change with the mode:
  - NPC shop: Sell `0xC` enabled; deposit `0xA` and gold-deposit `0x26` disabled.
  - Bank: deposit and gold-deposit enabled; Sell disabled.
  - Stall: all three disabled.

**Leaving `0x259`** (`FUN_004665e0`): if `+0x20` or `+0x1C` is set, every item still listed in the stall window goes back into the bag (`FUN_00467200`). Then `+0x1C`, `+0x20` and `+0x04` are zeroed.

NPC click (`FUN_004447d0`) is blocked while your own stall is open or in setup.
- Bank NPC (`npc+0xE6C == 0x1A7`): calls `FUN_00464480(…, 0x1A7)`, the password gate.
- Other NPCs: `FUN_0048a260(npc+0xE60)` stores the NPC index into the target window, then `FUN_00446300(UI id, 0, 3, 0, 1)`.

### 1.2 Shared item descriptor
Every item-bearing packet in this group names an item as:
- `u16 item_id`
- a `u16` quantity
- `u8 opt_count`, then `opt_count × u16 opt`
- `u16 item_extra`

That is the client's 12-byte option blob: words 0..4 plus word 5 = record `+0x0C`.

- **Client to server:** `opt_count` is the number of leading non-zero words among 0..4. Items created by `0x18` have an all-zero blob, so they encode as `n=0, extra=0`.
- **Equipment (type 1) is matched by exact 12-byte blob compare** on every removal: `0x19`, `0x66`, `0x67`, `0x88`, `0x89`, and bank `FUN_00427390`. The server must echo the descriptor it stores, never rebuild one.
- **Crash hazard:** S2C handlers read opts into a 6-word stack array without bounds checks.
  - `0x19` / `0x66`..`0x89`: `>= 63` reaches the /GS cookie.
  - `0x87`: `>= 11` corrupts the price local, `>= 69` hits the cookie.
  - `0x65`: `> 5` corrupts the next slot.
  - **Always clamp `opt_count` to 5 or less.**

Item types (`def+0x154`): 0 u16 stack (consumable, 999 max), 1 equipment, 2 u8 stack (etc, 99 max), 3 skill book (`0x18` *learns* it), 4 class change, 5 cash-use item. Only 0/1/2 live in bag or bank tabs.

### 1.3 Currency and manner
| Value | Client location | Written by |
|---|---|---|
| gold u64 | player_info+0x228 (scene+0x270) | `0x03`, `0x18`, `0x19`, `0x66`, `0x68/69`, `0x81`, `0x88`, `0x89` |
| Victy u32 (our code: "winnie") | player_info+0x238 | `0x03` `winnie_points`, `0x18` |
| bank gold u64 | player_info+0xE28 | `0x65` `bank_gold`, `0x68/69` `storage_gold` |
| manner i32 | player_info+0xE98 | `0x02` `manner_points` (ws:2678 sends 0) |

Client rules the server must mirror (all verified in decomp):
- **Bank deposit fee** (`FUN_00469090` case `0x1A8`): manner >= 100 pays 0, 30..99 pays 25, below 30 pays 50. Our login sends 0, so the fee is **50**.
- **NPC purchase cost** (`FUN_00467680`, `param_7`=npc flag): `cost = ROUND(qty × price × d)`, with `d = 0.9` when npc flag != 0, type != 3 and manner > 199, else 1.0. If `d < 1` and the cost rounds to 0, the cost is 1. The same formula applies to Victy (`def+0x1E8`). **Stall purchases (npc flag 0) get no discount**; the `0x62` spec text saying otherwise is wrong.
- **Client cost overflow.** `qty × price` is computed as a 32-bit int, and the gold check only runs when the high dword of gold is 0. A server must compute in u64 and never trust the client's check.
- **Stall use** needs manner >= -79 (`FUN_00466e30`, and `FUN_00467680` with npc flag 0).

### 1.4 NPC shop (window `0xD`)
- **Opening.** Hold Space on a merchant (`UI 13`). No packet is sent. Window `0xD`+0x11C = NPC index (`npc+0xE60` = hni/gamedef idx).
- **Listing** (`FUN_00465a80`) is fully client-local. It reads the roster record `[gs+0x4E0]`, entry `+0x278` (= hni `item:`).
  - For type-3 books with max level `def+0x142 > 0`, the shown id is advanced past the levels the player already knows. The client can therefore buy `base .. base+maxlv-1`.
- **Buy.** Select an item, amount dialog `0x10`, OK.
  - `FUN_00467680` checks gold, Victy and bag space (type 0: cap `+0x362` <= 45 and `FUN_00424c80`; type 2: `+0x363`; type 1: `+0x361`).
  - Type 3 checks instead: `Lv <= level` ("You need higher level"), class ("You can't learn other class skills."), not learned ("You already have learned this skill.").
  - Then C2S `0x0B {u16 item_id, u16 qty, u16 npc_id}`. **Nothing changes locally**; item and gold arrive via S2C `0x18`.
- **Sell.** Select a bag item, Sell (`0xC`), dialog `0xF`, OK.
  - The client checks qty against owned (`FUN_004679c0`), then sends C2S `0x0C` with the full descriptor. Nothing changes locally.
  - S2C `0x19` sets gold, removes the item (blob match for equipment), and prints `Sold %u (%s).`.
- Items with `def+0x1F0 != 0` (EN hii `Cash != 0`) can't be bought, sold, deposited, dropped or listed.

### 1.5 Bank (window `0x1A7`)
- **Opening.**
  - Talking to NPC 97 "Mikomakisho" (UI 423) calls `FUN_00464480(…, 0x1A7)`. If `scene+0x218 == 0` it shows *"no SS# in this ID…"* and stops.
  - `scene+0x218` is set by the S2C `0x5A` handler when MD5(username) differs from the 32-char string at `scene+0x131` (origin untraced).
  - Otherwise password dialog `0x201` opens. OK sends C2S `0x51 {str[21] password, u32 0x1A7}`.
  - S2C `0x80 {1, 0x1A7}` opens the window (mode switch to bank). `0x80 {!=1}` shows *"Invalid password."*.
- **Contents.**
  - The bank grid is drawn **only** from the block that S2C `0x65` fills. `0x65` memsets `scene+0x980..+0xE77`, then reads 3 tab capacities (<= 60), 3 slot-ordered lists and bank gold.
  - It then calls `FUN_00465110` (gold label + grid rebuild).
  - The client never requests `0x65`, and the bank mode switch doesn't rebuild the grid. `FUN_00465c70` runs only from `FUN_00465110` (`0x03`/`0x65`), `0x66`/`0x67`, the slot-extension item and the tab switch.
  - **Send `0x65` before and again right after `0x80 {1,0x1A7}`.**
- **Deposit item.** In bank mode: select a bag item, deposit (`0xA`), qty dialog `0x1A8` for stackables (equipment is qty 1).
  - `FUN_00427B70` checks free bank space. Fee dialog `0x1AC` checks the fee against gold. OK sends C2S `0x3C {id, qty, desc}`.
  - Reply S2C `0x66 {u64 gold after fee, id, qty, desc}`: set gold, **bank add first**, then bag remove, then refresh.
  - If the bag removal fails, the bank change stays with no refresh (desync).
- **Withdraw item.** Two send paths share one wire format:
  - **(a) Equipment** (`0x469BA8`): double-click or drag a bank equipment slot to the inventory, after a bag-space check. Sends `{id, 1, full desc}`.
  - **(b) Other types** (`0x469D9C`): qty dialog `0x1A9`, which checks `qty != 0`, <=999/99, bank holds enough, bag space. Sends `{id, qty, n=0, extra=0}`.
  - Reply S2C `0x67 {id, qty, desc}`: bank remove (equipment needs an exact blob match against the **bank slot**), then bag add.
- **Gold.**
  - Inventory `0x26` → dialog `0x1AA` → C2S `0x3E {u64}`.
  - Bank gold button → dialog `0x1AB` → C2S `0x3F {u64}`.
  - Amounts come from `_atol` (31-bit, sign-extended). Replies are S2C `0x68`/`0x69 {u64 amount (ignored), u64 storage_gold, u64 gold}`; both opcodes run the same code.
- **Client bank tab algorithms.** The server must port these exactly, because `0x66`/`0x67` make the client re-run them on its own copy.

| Algorithm | Function | Rule |
|---|---|---|
| Consumable add (qty 1..999) | `FUN_00427740` | The **first** same-id slot with qty < 999 absorbs; if the sum reaches 1000 that slot becomes 999 and the remainder goes to the **first empty** slot. If there is no empty slot, it returns failure **after** already setting the slot to 999 |
| Etc add (1..99) | `FUN_00427940` | The **first** same-id slot absorbs (even when it is at 99); overflow as above, with 99 |
| Stack remove | both | Requires total >= qty. Subtracts walking from the **last** slot backwards, then closes only the **first** hole by shifting the following contiguous slots left |
| Equipment add / remove | `FUN_00427610` | Add = first empty slot plus blob. Remove = first id + blob match (`FUN_00427390`), then shift left |
| Space pre-check | `FUN_00427B70` | Looser than the add: a slot with the same id and `qty+n < 1000/100`, or any empty slot |
| Capacity | all | A cap byte > 60 makes every operation fail |

Because the space pre-check is looser than the add, the client can send a `0x3C` that the add would fail. The server must simulate the add (§3.6).

### 1.6 Item stall (window `0x259`)
- **Entry** (new finding):
  1. Learn skill 194 "Open Stall": buy it from NPC 148 (Leon), Lv 6, 3000 gold + 100 Victy. S2C `0x18 {…, 194, 1}` teaches it.
  2. Cast it (C2S `0x15 {194}`); the client sets its pending-skill lock `scene+0x258`.
  3. Server replies S2C `0x25 {194}`: clears the lock, starts the 3000 ms cooldown, and calls `FUN_00483430(0x259,3,1)`, which reaches `FUN_00465fe0` → `FUN_00466390(1)`.
  4. Gates: map flag `[[gs+0x4D8]+0x18] != 0`, else *"Item stall can be opened only in the flea market."* Then `FUN_00466e30`: manner >= -79, and no remote stall within `dx²+dy² <= 19999` (about 141 px on the u16 coords `+0x15A4/+0x15A8`), else *"It's too close to other item stall."* Then not queued for battlefield.
  5. Then `FUN_00467030`: setup mode.
  - The flea market is map code 9701 (`stage97_01.hmi`, `MapInfo mode="2"`, xml code 247). No portal in `portals.json`/`gamedef.maps` leads there.
- **Registration is client-local.**
  - Dialog `0x25A` (equipment) or `0x25B` (stackables, qty-checked) takes a unit price that must be > 0 (*"The Item price must be higher than 0."*).
  - `FUN_00467140` **removes the item from the bag locally** and `FUN_00467300` appends it to the list. No packet is sent.
- **Open.** Start (`0x19`) sends C2S `0x5E {u8 count, str[25] title, count × {u16 id, u16 qty, u32 unit_price, desc}}`.
  - The client immediately sets `+0x20=1, +0x1C=0`.
  - S2C `0x82 {1}` switches the window to selling mode. `{9}` shows the "invalid transaction… recorded" box; any other value shows "opening failed". **Both failures leave `+0x20=1`.**
- **Advertise.** S2C `0x85 {u32 uid, u16 sign_sprite, str[25] title}` sets `entity+0x14F4=1` on that remote player. `FUN_004316b0` draws the sign sprite and title for alive==3 entities.
  - `0x85` is ignored while the flag is set.
  - Late arrivals learn about stalls from the `shop_open` block at the end of the `0x04`/`0x07` player record (`0x05` has none).
- **Browse.** Clicking a sign sends C2S `0x61 {u32 owner_uid}`. S2C `0x87 {1, count, owner_uid, title, items}` opens `0x259` in buyer mode (list cleared first).
  - `{6}` shows *"Open stall is opened…"*. Other values, or count 0, show *"The shop is closed or adjusting."*
  - If the viewer's own stall is open or pending, the client reads nothing.
  - **The client sends no packet when a buyer closes the window.**
- **Buy.** Buy (`0x1C`) → dialog `0x10` → `FUN_00467680` (unit price×qty, no discount, manner, bag space) → C2S `0x62 {u32 seller_uid, u16 id, u16 qty, desc}`.
  - Buyer gets S2C `0x88 {1, u64 gold, id, qty, desc}`: gold set, list entry decremented, item added with *"you've received %s. (Count:%u)"*. `{!=1}` shows *"You failed to buy the items…"*.
  - Seller gets S2C `0x89 {u64 gold, id, qty, desc}`: gold set, own list entry decremented, *"Sold %u (%s)."*. **The seller's bag is not touched**; the item already left it at registration.
- **Stop for edit.** `0x1B` sends C2S `0x5F` (client sets `+0x1C=0`). S2C `0x83 {1}` returns to setup mode (`+0x20=0, +0x1C=1`); anything else leaves the window stuck in selling mode with setup disabled.
- **Close.** `0x1A`, or closing the window while `+0x20 != 0`, sends C2S `0x60`. S2C `0x84 {1}` closes `0x259`, and `FUN_004665e0` returns unsold items to the bag.
  - The client also auto-sends `0x60` from the S2C `0x08`/`0x5E`/`0xA3`/`0xA4` handler block (`0x450919`), and from `FUN_0042cf20` when the local state `+0x15B4 != 8`.
  - **Without `0x84 {1}` the player can never leave the stall window.**
- **Sign removal.** S2C `0x86 {u32 uid}` clears the sign. If uid == `ctx+4`, it also closes the buyer window with *"The shop is closed or adjusting."*

---

## 2. Request/response flows

`C>` = client to server, `S>` = server to client. **Resync** = `S> 0x18 {gold, victy, item_id=0, count=0}` (absolute currency refresh, no grant).

### F1. NPC shop buy
1. Client gates (§1.4): window `0xD`; `def+0x1F0==0`; qty 1..999 (type 0) / 1..99 (type 2) / 1 (others); gold and Victy (discount rule); bag space; type 3: level, class, not learned.
2. `C> 0x0B {item_id, qty, npc_id}` (6 B).
3. Server:
   1. Decode `u16 item_id / u16 qty / u16 npc_id`.
   2. `shop = hni[npc_id]`. Refuse unless `hni.UI == 13` and `item_id` is in `shop.items`. For a type-3 entry `base`, accept `base <= item_id < base + maxlv`, and require that the player knows `item_id-1` whenever `item_id > base`.
   3. `def = hii[item_id]`. Refuse if missing or `Cash != 0`.
   4. qty rule by type; force 1 for types 1/3/4.
   5. `gold_cost = cost(Buy)` and `victy_cost = cost(PMoney)` using §1.3 (u64). Refuse if gold or Victy is short, if a type 0/1/2 item doesn't fit the bag model (item group I-04 rules), or if a type 3 skill is already known or the level/class is wrong.
   6. Deduct both, then add the item (types 0/1/2) or record the learned skill (type 3; combat_skill `cs-skill-learn`). Persist.
4. `S> 0x18 {gold, victy, item_id, qty}`.
5. On refusal: Resync plus a system chat line. **Never send `0x18` with a non-zero item on refusal.**

### F2. NPC shop sell
1. Client gates: shop mode; `def+0x1F0==0`; qty 1..999/1..99 and owned; equipment qty 1.
2. `C> 0x0C {item_id, u16 qty, u8 n, n×u16, u16 extra}` (7..17 B).
3. Server:
   1. Decode the descriptor (§3.5). Reject `n > 5`.
   2. Refuse if `Cash != 0`.
   3. Ownership: types 0/2 need the bag total >= qty; type 1 needs an exact (id, blob) slot, qty 1.
   4. Remove using the client algorithm (item group), credit `hii.Sell × qty` gold (u64), persist.
4. `S> 0x19 {gold, item_id, qty, n, opts, extra}`, echoing the descriptor.
5. On refusal: Resync plus chat. **Never send `0x19` with qty on refusal**, because it removes client items.

### F3. Opening the bank
1. The player talks to NPC 97 (UI 423).
2. Client gate `scene+0x218`: if 0, *"no SS#…"* appears and nothing is sent (use F3b). Otherwise dialog `0x201` opens.
3. `C> 0x51 {str[21] password (read to the first NUL; trailing bytes are stack garbage), u32 target_window_id}` (25 B).
4. Server (shared handler with premium_cash): compare with `account.second_password`, falling back to `password`.
   - **Mismatch:** `S> 0x80 {0}`, which shows *"Invalid password."*
   - **Match, target `0x1A7`:** `S> 0x65` (current bank), `S> 0x80 {1, 0x1A7}`, `S> 0x65` again (grid redraw, §1.5). Set `session['bank_open']=now`.
   - **Match, target `0x1F9` or `0x235`:** `S> 0x80 {1, target}` (other groups continue).
   - **Any other target:** `S> 0x80 {0}`. Never echo arbitrary ids; the client opens whatever window id it receives.
5. **F3b fallback** if the SS# gate blocks our launch path: dev chat command `/bank` sends `0x65`, `0x80 {1,0x1A7}`, `0x65`. `0x80` has no state gate.

### F4. Bank deposit item
1. Client gates: bank mode; `FUN_00427B70` space; qty checks and owned; fee <= gold.
2. `C> 0x3C {item_id, qty, n, opts, extra}`. Non-equipment always sends `n=0, extra=0`.
3. Server:
   1. Decode the descriptor.
   2. Soft gate `session['bank_open']` (the client never reports closing): log if unset, but accept while in-world.
   3. Refuse if `Cash != 0` or type not in {0,1,2}.
   4. Ownership as in F2.
   5. `fee = 0 if manner>=100 else 25 if manner>=30 else 50`; refuse if gold < fee.
   6. **Simulate the client add on a copy of the bank tab** (§3.6). Refuse if it fails, even though `FUN_00427B70` passed.
   7. Commit: gold -= fee; bag remove; bank add (same algorithm); persist.
4. `S> 0x66 {gold, item_id, qty, n, opts, extra}`, echoing the descriptor.
5. On refusal: `S> 0x65` (full resync; it also undoes nothing because the client never mutated) plus a chat line. `0x66` has no failure code.

### F5. Bank withdraw item
1. Client gates: bank mode. Path (a) equipment: bag space. Path (b): `qty != 0`, <=999/99, <= bank amount, bag space.
2. `C> 0x3D {item_id, qty, n, opts, extra}`. Path (b) always sends `n=0, extra=0`; path (a) sends qty 1 plus the full blob.
3. Server:
   1. Decode the descriptor and look up the type.
   2. Type 1: pick the bank slot with exact (id, blob). If there is none and the request blob is all zero (path b), pick the first zero-blob slot with that id, else the first slot with that id. qty = 1.
   3. Types 0/2: bank total by id >= qty.
   4. Bag space by the client add rules.
   5. Commit: bank remove (client algorithm), bag add, persist.
4. `S> 0x67 {item_id, qty, n, opts, extra}` carrying **the blob stored in the chosen bank slot**, not the request's. The client compares against its own slot bytes.
5. On refusal: `S> 0x65` plus chat.

### F6. Bank deposit gold
1. Client gates: amount != 0 and <= carried gold.
2. `C> 0x3E {u64 gold}`.
3. Server: reject 0, `>= 2^63` (sign-extended negative) or `> gold`. Then gold -= a, bank_gold += a, persist.
4. `S> 0x68 {a, bank_gold, gold}`. On refusal: `S> 0x68 {0, bank_gold, gold}`, a no-op resync.

### F7. Bank withdraw gold
1. Client gates: bank window open; amount != 0 and <= bank gold.
2. `C> 0x3F {u64 gold}`.
3. Server: `0 < a <= bank_gold`; then bank_gold -= a, gold += a, persist.
4. `S> 0x69 {a, bank_gold, gold}`. On refusal: `0x69 {0, …}`.
   - 0x68 for deposit and 0x69 for withdraw is our convention; the client treats them identically.

### F8. Stall entry (skill 194)
1. Prerequisite: the player learned 194 via F1 (NPC 148) and stands on map 9701.
2. Client gates at cast: skill known, cooldown, no pending skill, idle state.
3. `C> 0x15 {u16 194}`. The client sets `scene+0x258` and waits.
4. Server (the combat_skill cast handler hands off to this group):
   - If the skill is known and not on cooldown: `S> 0x25 {194}`. The client runs the flea-market, manner and distance gates and opens setup, or shows the gate message.
   - Optional server pre-checks (map 9701, manner >= -79) may reply `S> 0x5F` instead, which clears the lock silently. Prefer `0x25` so the client shows its own message.
   - **Never leave `0x15` unanswered**: the lock also blocks equip/unequip.

### F9. Stall open (seller)
1. Client gates: F8 setup window; items registered (they left the client bag); price > 0; title non-empty and not the placeholder.
2. `C> 0x5E {u8 item_count, str[25] title, items[…]}`. The client sets `+0x20=1, +0x1C=0`.
3. Server. First set `session['stall_client_selling'] = True`, whatever the outcome (see F13). Then:
   1. Map == 9701, else result 2.
   2. Manner >= -79, else 2.
   3. Decode fails, or the trailing length doesn't match `item_count` (the client can count NULL list nodes): result 2.
   4. `1 <= item_count <= STALL_MAX_ITEMS`; every `opt_count <= 5`, `unit_price > 0`, `Cash == 0`, type in {0,1,2}, and type-1 qty == 1. Otherwise result **9** (tampering).
   5. The seller owns every listed item, with same-id quantities summed and equipment matched by blob. Otherwise 9.
   6. Optional: squared distance > 19999 to other open stalls (needs server-side positions from the world group), else 2.
   7. If a stall record in state `edit` exists (re-open after F12): return its escrow to the inventory model first.
   8. Commit: move the listed items from inventory into `stall.escrow`, `state='open'`, title truncated to 24 bytes, persist the seller (including `stall_escrow`, §3.1).
4. Success: `S> 0x82 {1}` to the seller, then `S> 0x85 {uid, STALL_SIGN_SPRITE, title}` to every other in-world session on map 9701.
5. Failure: `S> 0x82 {2|9}`, no escrow. The client keeps `+0x20=1`; recovery is F14.

### F10. Stall browse
1. Client gates: clicked a remote player with `entity+0x14F4 != 0`; own stall not open or pending; no blocking modal.
2. `C> 0x61 {u32 owner_uid}`.
3. Server:
   - Viewer's own stall record exists (`open` or `edit`): `S> 0x87 {6}`.
   - Else no stall, a different map, or state != `open`: `S> 0x87 {0}`.
   - Else if the stall has no items left: `S> 0x87 {1, 0}` (client shows "closed").
   - Else `S> 0x87 {1, n, owner_uid, title, items[{id, qty, unit_price, n, opts, extra}]}`, and set `stall.viewers[viewer_uid] = now`.

### F11. Stall purchase
1. Client gates: buyer window; unit price×qty <= gold (32-bit, no discount); manner >= -79; bag space; qty <= 999/99.
2. `C> 0x62 {u32 seller_uid, u16 item_id, u16 qty, desc}`.
3. Server, under `world_lock`:
   1. The stall exists, is `open` and on the buyer's map; buyer != seller; the seller session is online.
   2. The entry matches (id + exact blob + extra); `1 <= qty <= entry.qty`; qty is 1 for type 1.
   3. `total = unit_price × qty` (u64); buyer gold >= total; buyer manner >= -79; buyer bag fits.
   4. Commit: buyer gold -= total; seller gold += total (cap at 2^63-1); entry.qty -= qty (drop at 0); remove from escrow; add to the buyer inventory; persist both.
4. Success: `S> 0x88 {1, buyer_gold, id, qty, desc}` to the buyer, and `S> 0x89 {seller_gold, id, qty, desc}` to the seller.
5. Failure: `S> 0x88 {0}`, followed by a fresh `S> 0x87` of the current list **to that buyer only**; their window is open because they just clicked Buy, and `0x87` rebuilds it. Do not push `0x87` to other viewers: the client doesn't report closing the window, so a push would re-open it.

### F12. Stall stop-for-edit
1. Client gates: `+0x20 != 0`; the client sets `+0x1C=0`.
2. `C> 0x5F`.
3. Server: if the stall is `open`, set it to `edit` (keep the escrow; the client keeps its list), broadcast `S> 0x86 {uid}` to the other sessions on the map, and clear `viewers`.
4. `S> 0x83 {1}` **always**. Any other value strands the client in selling mode with setup disabled.
5. The seller edits (client-local) and presses Start again: F9, with the reconcile at step 3.7.

### F13. Stall close (seller, user or automatic)
1. Client gates: `+0x20 != 0`. The packet is also auto-sent after S2C `0x08` and when `+0x15B4 != 8`.
2. `C> 0x60`.
3. Server:
   - If a stall record exists: return the remaining escrow to the inventory model, persist, broadcast `0x86 {uid}` if its state was `open`, and delete the record.
   - If `session['stall_client_selling']`: `S> 0x84 {1}` and clear the flag.
   - Otherwise ignore it (de-duplicates the automatic re-sends).
4. The client closes `0x259` and returns items to its bag, matching the server model.
5. A failed F9 still left `+0x20=1`, so the next `0x60` must get `0x84 {1}`. That is why the flag is set at F9 step 3 regardless of the result.

### F14. Map change, disconnect, presence
1. **Server-initiated map change** (`_handle_change_map` ws:1244, and future warps): **before** `S> 0x08` (ws:1283), if a stall record exists or `stall_client_selling` is set:
   1. Return the escrow to the inventory model.
   2. Broadcast `0x86` on the old map.
   3. Send `S> 0x84 {1}`, then clear the flag and the record.

   The client closes the window and returns the items before `0x08`, so it doesn't auto-send `0x60`. The following `0x03` bag (built from the model) then already contains those items.

   If `0x84` were sent after `0x08`/`0x03`, the client's `FUN_004665e0` would add the items a second time on top of the reloaded bag.
2. **Player enters a map** (enter-world or portal): for each open stall on that map, set `shop_open=1, shop_sign_sprite, shop_title` in the seller's `0x04`/`0x07` record (ws:2395 always writes 0). If the seller is announced with `0x05`, follow with `S> 0x85`.
3. **Seller disconnect** (the `finally` at ws:578): return escrow, persist, broadcast `0x86`, delete the stall.
4. **Buyer disconnect:** drop it from every `stall.viewers`.
5. **Login:** if `character.stall_escrow` is non-empty (server crashed while selling), merge it back into the inventory and clear it.

---

## 3. Server state and data model

### 3.1 Persistent (accounts.json, aligned with premium_cash §3.1 and the item group's I-04)
```jsonc
"test": {
  "password": "test",
  "second_password": null,               // null -> password (C2S 0x51; shared with premium_cash)
  "characters": [{
    "name": "TestHero", "...": "...",
    "gold": 100000, "victy": 0, "manner": 0,            // wallet (0x02/0x03/0x18/0x19/0x66/0x68/0x88/0x89)
    "tab_slots": [35, 35, 35],                          // bag caps (premium_cash / item group)
    "inventory": {"equip": [...], "consume": [...], "etc": [...]},   // item group I-04 slot model
    "skills": [194, ...],                               // combat_skill cs-skill-learn (Open Stall = 194)
    "bank_slots": [35, 35, 35],                         // 0x65 caps; keep [1] == [2] (FUN_00427940 quirk); <= 60
    "bank": {
      "equip":   [{"id": 179, "opts": [0,0,0,0,0], "extra": 0}, null, ...],  // slot order == 0x65 order
      "consume": [{"id": 3, "qty": 10}, null, ...],     // qty <= 999
      "etc":     [null, ...],                           // qty <= 99
      "gold": 0
    },
    "stall_escrow": []                                  // crash safety, F14.5
  }]
}
```
- Every mutation and save runs under one `accounts_lock`, and saves write a temp file then rename.
- Bank scope is per character, matching premium_cash's `bank_slots` placement (open question 4).

### 3.2 World state (in memory, `GameServer`)
- `sessions_by_uid` and `map_members` come from the world/trade shared registry (`world-registry` / `trade-mp-registry`). **Per-account uids are required** (`world-uid-alloc`); ws:2656 sets `account_id = 1` for every session.
- `stalls: {owner_uid -> {map, title, sign_sprite, state: 'open'|'edit', items: [{id, opts[5], extra, qty, unit_price}], viewers: {uid: t}, opened_at}}`. `items` is the escrow.
- `world_lock` (RLock) wraps every stall mutation and cross-session gold transfer.

### 3.3 Per-session
| Key | Use |
|---|---|
| `bank_open` | time of the last `0x80 {1,0x1A7}` (soft gate, F4/F5) |
| `stall_client_selling` | the client's `+0x20` mirror (F9/F13/F14) |
| `current_map`, `char_name` | already exist (ws:721-722) |

There is no NPC-shop session state; the shop is client-local.

### 3.4 Content
| Need | Source now (KR) | EN source (preferred) |
|---|---|---|
| item Type, Lv, Job, Cash | `gamedef items` | `hs/windslayer.hii` (item group I-01 `ItemCatalog`); `server/en_item_catalog.json` has name+type from memory |
| NPC gold price | `items.Buy` (ws:1017) | hii `Buy` (= `def+0x1E0`) |
| NPC Victy price | ignored | hii `PMoney` (= `def+0x1E8`; e.g. 1270/1340 = 10, 194 = 100) |
| sell refund | `items.Sell` | hii `Sell` (probably `def+0x1E4`, not read by the client) |
| no-trade flag | ignored | hii `Cash` (= `def+0x1F0`) |
| skill max level | n/a | `def+0x142` (hii column not mapped yet, Q6) |
| shop list per NPC | `gamedef npcs.item` | **`hs/windslayer.hni` `item:` where `UI: 13`** (56 NPCs; 4-digit decimal) |
| bank NPC / village NPC | `npcs.UI` 423 / 600 | hni idx 97 / 117 |
| stall skill | n/a | item 194 (NPC 148 list: 94, 80, 88, 90, 194, 86, 82, 2188) |
| flea market | `data/map_codes.json` 9701 → `stage97_01` | same; `MapInfo mode="2"` |
| constants | `STALL_SIGN_SPRITE = 0x168` (provisional, Q9), `STALL_MAX_ITEMS = 20` (provisional, Q10), `BANK_DEFAULT_SLOTS = 35`, `STALL_MAP = 9701` | |

### 3.5 Wire grammars (wsproto)
Build and parse through `wsproto.Grammar`, preferably through the shared `G(key)` spec loader (`trade-codec`). Some spec grammars contain client-state conditions that `wsproto` evaluates as false, so use these forms:

| Opcode | Grammar | Why |
|---|---|---|
| C2S `0x0C`, `0x3C`, both `0x3D` keys | `u16 item_id\nu16 qty\nu8 opt_count\nrepeat(opt_count) { u16 opt }\nu16 item_extra` | The `0x3C` spec's `if(item_type == 1)` isn't a wire field: wsproto takes the else branch and raises "trailing bytes" on equipment with options. The `0x469D9C/0x3D` spec hard-codes count 0 and would reject path (a). `u8 0, u16 0` is byte-identical to `n=0`. |
| C2S `0x0B` | `u16 item_id\nu16 qty\nu16 npc_id` | as spec |
| C2S `0x3E`/`0x3F` | `u64 gold` | as spec |
| C2S `0x51` | `str[21] password\nu32 target_window_id` | decode stops `str` at the first NUL, which drops the stack garbage |
| C2S `0x5E`, `0x62`, `0x61`, `0x15` | as spec | `0x5E`: catch GrammarError → result 2 |
| S2C `0x85` | `u32 owner_uid\nu16 sign_sprite_id\nstr[25] stall_title` | The spec's `if(entity_by_uid…)` is client state; `encode()` takes it as false and **emits only 4 bytes** |
| S2C `0x87`, `0x88` | as spec | The leading `if(local_own_stall_open_or_pending){stop}` encodes as false, which is correct |
| S2C `0x19`, `0x65`..`0x69`, `0x80`, `0x82`..`0x84`, `0x86`, `0x89`, `0x25` | as spec | Clamp opt/option counts to 5 and `0x65` list counts to 60 |

wsproto `str[N]` encode truncates without a terminator, so cap titles at 24 bytes before encoding (`0x85`/`0x87`/`0x04`).

### 3.6 Bank algorithms module
`bank_tabs.py`: pure functions `consume_add/remove`, `etc_add/remove`, `equip_add/remove`, `space_check`. They are line-for-line ports of `FUN_00427740`, `FUN_00427940` (including the cap-byte quirk), `FUN_00427610`/`FUN_00427390` and `FUN_00427B70`, and each returns `(ok, new_tab)` without mutating its input.

Unit tests replay the client quirks:
- the partial 999 write on a failed spill
- first-hole-only compaction
- backwards removal

The same functions build the `0x65` lists. Keep them apart from the bag (45-slot) versions in the item group, because the bag rules differ (`FUN_00423b70` merges differently).

---

## 4. Current server status and proven bugs

**Implemented:**
- `0x0B` → `0x18` (ws:1002-1027) and `0x0C` → `0x19` (ws:1029-1051), backed by a session-only `{item_id: count}` inventory (ws:1137-1158) and an in-memory wallet (ws:994-1000).

**Partial:**
- A `0x80` builder with the right bytes but the wrong meaning (ws:1107-1132), never called.

**Missing:**
- Everything else. `_dispatch` (ws:601-667) has no branch for `0x3C`-`0x3F`, `0x51`, `0x5E`-`0x62`; they log "Unhandled opcode" (ws:660).
- `0x15` for skills returns early (ws:1188-1191).

| # | Severity | Bug | Evidence | Fix |
|---|---|---|---|---|
| B1 | wrong_behavior | **Sell qty parsed as u8.** `count = payload[2] or 1` (ws:1039), documented wrongly at ws:977-978. Selling 300 (`2C 01`) sells 44; selling 256 (`00 01`) sells 1. Sockets and extra are ignored, so equipment with a non-zero blob is removed from the model while the client's `0x19` blob match fails. | spec `0x46A679/0x0C` (Add u16 @0x46A613); PySlayer sample `0c 5400 0100 00 0000` fits u16 | §3.5 descriptor grammar; echo it in `0x19` (`_build_opcode_19` ws:1063 always sends n=0) |
| B2 | crash_or_desync | **Currency desync on the first economy packet.** `_wallet` defaults are gold 999999 and Victy 999999 (ws:994-995), but `0x03` tells the client gold 100000 (ws:2044) and Victy 0 (ws:2047). The first `0x18`/`0x19` (buy, sell, drop `_send_drop` ws:2015-2021, quest) jumps the client to 999999/999999. Victy is never charged (no `PMoney` check), so Victy-priced items (1270, 1340, skill 194) cost no Victy. | spec `0x18` (absolute values), `0x03` `gold`/`winnie_points` | persistent wallet (SS-wallet) feeding `0x02`/`0x03`/`0x18`/`0x19` |
| B3 | crash_or_desync | **Every portal resets client gold and empties the bag.** `_handle_change_map` re-sends `0x03` (ws:1298-1300) with gold 100000 and the four trailing counts 0 (ws:2076-2079). Per the grammar those are `completed_quest_count`, `equip_item_count`, `consume_item_count`, `etc_item_count`. The model keeps its items while the client bag is wiped. | spec `0x03` grammar; LIVE_TEST_LOG portal row (`0x03 64B`) | world `world-03-real-state` + item I-05 |
| B4 | wrong_behavior | **Buy accepts any item from anywhere.** `npc_id` is ignored (ws:1011-1012); no shop-list, `Cash`, `PMoney`, level/class or manner-discount handling; cost = KR `Buy × count` (ws:1017). A crafted `0x0B` buys any of 4609 KR ids, including ids > 4248 that the EN client drops silently, so gold is lost with no item. | spec `0x469D9C/0x0B` (npc_id = window 0xD+0x11C); hni lists | F1 (SS-npc-buy) |
| B5 | wrong_behavior | **Buy refusal sends no resync**, only a chat line (ws:1018-1021). With B2/B3 the UI can show enough gold while the server refuses. | ws:1018-1021 | Resync `0x18 {gold, victy, 0, 0}` + chat |
| B6 | wrong_behavior | **Sell refusal is silent** (ws:1040-1042). The model already diverges from the client bag (LIVE_TEST_LOG bugs 5/6), so legitimate sells do nothing with no feedback. | ws:1040-1042 | Resync + chat; slot-accurate inventory |
| B7 | wrong_behavior | **Skill books go into the bag model.** `_inv_add` runs for every buy (ws:1024), including type 3, which the client *learns* (`FUN_00440920` → `FUN_00426b80`). The model then holds a phantom sellable item and no learned skill. | spec `0x18` type 3 | F1 step 3.6 |
| B8 | wrong_behavior | **`0x80` mislabeled "CashShopBalance".** `_build_opcode_80(value)` / `_send_cash_balance` send `{1, winnie}` (ws:1107-1132, comment ws:984-985). The u32 is a window id, so calling it would try to open window 999999. Latent (never called). | spec `0x80` (FUN_00446300(target,…)) | `_send_password_result(result, target)`; delete `_send_cash_balance` |
| B9 | crash_or_desync | **Stall softlock (latent).** Nothing handles `0x5E`/`0x60`. Once a stall is started the client sets `+0x20=1` (0x46B63B); every Close and every portal `0x08` sends `0x60`, and with no `0x84 {1}` the window can never close and mode switches are refused (`FUN_00465fe0`). Reachable as soon as `0x25 {194}` is answered (combat_skill `cs-utility-skills`) or injected on map 9701. | spec `0x84` gates; ws:660 | SS-stall-stub must ship with or before `cs-utility-skills` |
| B10 | crash_or_desync | **Open Stall cast locks the player.** C2S `0x15 {194}` hits `_handle_use_item`, which returns for `type != 0` (ws:1188-1191) without `0x25`/`0x5F`. `scene+0x258` stays set: no further casts, *"You can't equip or unequip while attacking."* on every equip. The stall is unreachable. | spec `0x44C239/0x15` hazards; asm 0x453D1C | combat_skill `cs-cast-unlock` / `cs-utility-skills` + F8 |
| B11 | wrong_behavior | **Bank unusable.** There is no `0x51` handler, so the password dialog closes with no reply and the bank never opens; no `0x65` builder; `0x3C`-`0x3F` are unhandled. | spec `0x80`/`0x65`; ws:660 | SS-password-gate, SS-bank-* |
| B12 | wrong_behavior | **Shared uid 1 blocks stalls between two clients.** A second client's `0x85 {uid 1}` resolves to the receiver's *local* entity (alive 4, never drawn); `0x61`/`0x62` uid 1 are ambiguous. | ws:2656; memory project_multiclient | `world-uid-alloc` |
| B13 | wrong_behavior | **`0x07` always writes stall bool 0** (ws:2395), so late joiners never see open stalls. Latent. | spec `0x04`/`0x07` `shop_open` | F14.2 |
| B14 | cosmetic | **Stale comments.** ws:975-976 says handler 0x47091D is the "shop-inventory listing" (it is S2C `0x0B` FriendList, `s2c_dispatch_map.json`); ws:974 calls byte 5 of `0x0B` "u8 undef" (it is the high byte of u16 npc_id); ws:977-978 claims sell count is u8. | s2c_dispatch_map.json; spec `0x0B`/`0x0C` | fix comments |

**Spec corrections** for the RE corpus maintainers:
- **`0x66`/`0x67` "trigger C2S 0x13/0x14" is wrong.** `0x13`/`0x14` are DropInventoryItem/DropEquippedItem (`0x469AC9/0x13`, `0x469BA8/0x14`). The bank requests are `0x3C`/`0x3D`. The confusion comes from the shared Send tail `0x469BA8`.
- **`0x62` gates:** the 10% manner discount needs the npc flag (`FUN_00467680` `param_7 != 0`); stall purchases pass 0.
- **`0x66` "why gold":** it is the balance after the 0/25/50 deposit fee.
- **`0x86` "ctx+4 never cleared":** `FUN_004665e0` clears it when mode `0x259` is left.
- **`0x89` "are stall items removed at open":** they leave the bag at registration (`0x25A`/`0x25B` → `FUN_00467140`) and return on close (`FUN_004665e0` → `FUN_00467200`).
- **`0x65` "what opens the bank":** C2S `0x51` target `0x1A7` → S2C `0x80`. The grid redraw needs a `0x65` after the window exists.
- **`0x82`/`0x5E` "how the stall window opens":** S2C `0x25 {194}` (asm 0x453D1C) → `FUN_00483430(0x259,3,1)`.

---

## 5. Implementation plan

P0 = breaks core play today; P1 = core MMO loop; P2 = secondary systems; P3 = nice to have.

| ID | P | Effort | Depends on | Work |
|---|---|---|---|---|
| `shop_storage-sell-parse` | P0 | S | - | `_handle_sell_item` decodes u16 qty and the descriptor (§3.5), rejects `n > 5`; `_build_opcode_19` echoes the descriptor (clamp 5); refusal sends a resync plus chat. Fixes B1, B6 (partly). |
| `shop_storage-wallet` | P0 | M | world `world-persistence` / trade `trade-economy-persist` (share one wallet) | Persistent `gold/victy/manner` per character. `_wallet` reads/writes them (delete the 999999 defaults ws:994-995). `0x03` gold/winnie (ws:2044/2047) and `0x02` manner (ws:2678) come from them. Add `_send_currency_resync()`. Fixes B2 and the gold half of B3. |
| `shop_storage-en-content` | P1 | S | item I-01 (`ItemCatalog` from hii) | `NpcTable` from `hs/windslayer.hni` (decode, parse `UI:` and `item:` as 4-digit decimal); `shop_list(npc_id)`; expose hii `Buy/Sell/PMoney/Cash/Lv/Job`. Replaces the KR `npcs.item`/`items.Buy` use. |
| `shop_storage-npc-buy` | P1 | M | en-content, wallet, item I-04, combat_skill `cs-skill-learn` | F1 in full: list check (with the skill-level offset), Cash flag, gold+Victy cost with the discount, level/class for books, bag-fit, type-3 learn branch; refusal sends a resync. Fixes B4, B5, B7. |
| `shop_storage-bank-model` | P1 | M | wallet, item I-04 | §3.1 bank persistence; `bank_tabs.py` (§3.6) with unit tests; `_build_opcode_65` from the grammar (clamp 60/5). |
| `shop_storage-password-gate` | P1 | S | bank-model; shared with premium_cash `premium_cash-password-gate` | Dispatch C2S `0x51`: parse, compare, reply `0x80` per F3 with allowlist `{0x1A7, 0x1F9, 0x235}`; `0x65` before and after for `0x1A7`. Replaces B8's builder. Fixes B11 (open). |
| `shop_storage-bank-gold` | P1 | S | bank-model, wallet | `0x3E`/`0x3F` → `0x68`/`0x69` (F6/F7) with no-op resync on refusal. |
| `shop_storage-bank-items` | P1 | M | bank-model, sell-parse, item I-04 | `0x3C` (F4: fee, simulate, `0x66` echo) and `0x3D` (F5: slot pick, `0x67` with the stored blob); `0x65` resync on refusal. |
| `shop_storage-stall-stub` | P2 | S | - (must land with or before combat_skill `cs-utility-skills`) | Interim: `0x5E` → `0x82 {2}`, `0x5F` → `0x83 {1}`, `0x60` → `0x84 {1}` (idempotent via `stall_client_selling`), `0x61` → `0x87 {0}` (supersedes trade `trade-stall-visit` stub). Removes softlock B9. |
| `shop_storage-stall-entry` | P2 | S | combat_skill `cs-skill-cast`/`cs-utility-skills`, npc-buy | F8: skill 194 cast → `0x25 {194}` (known-skill and cooldown check); `0x5F` on refusal. Fixes B10 for this skill. |
| `shop_storage-flea-warp` | P2 | S | world `world-maptransfer` | Reach map 9701: dev chat `/warp 9701 <x> <y>` on the MapTransfer primitive; real entry TBD (Q13). Candidate spawn (1450, 2500), verify live. |
| `shop_storage-stall-registry` | P2 | M | world `world-uid-alloc`, `world-registry`, wallet, item I-04 | §3.2 stall registry, escrow, `world_lock`, map broadcast helper, disconnect/login cleanup (F14.3-5) in the `finally` at ws:578. |
| `shop_storage-stall-open-close` | P2 | M | stall-registry, stall-stub | `0x5E` → `0x82` + `0x85` broadcast (F9); `0x5F` → `0x83` + `0x86` (F12); `0x60` → `0x84` + `0x86` + escrow return (F13); close-before-`0x08` in `_handle_change_map` (F14.1). |
| `shop_storage-stall-browse-buy` | P2 | M | stall-open-close | `0x61` → `0x87` (F10); `0x62` → `0x88` + `0x89`, atomic, with the failure `0x87` refresh to the buyer (F11). |
| `shop_storage-stall-presence` | P2 | S | stall-open-close, world `world-presence` / `world-player-record` | `shop_open/sprite/title` in the `0x04`/`0x07` record (ws:2395); `0x85` after `0x05` (F14.2). Fixes B13. |
| `shop_storage-bank-fallback` | P3 | S | password-gate | `/bank` dev command (F3b) for when the SS# gate blocks. |
| `shop_storage-cleanup` | P3 | S | password-gate | Fix comments ws:969-991 (B14); remove `_send_cash_balance`; route `_send_buy_result`/`_send_sell_result` through the grammar builders. |

Suggested order: sell-parse → wallet → stall-stub → bank-model → password-gate → bank-gold → bank-items → en-content → npc-buy → (multiplayer foundation: uid-alloc, registry, presence) → stall-entry + flea-warp → stall-registry → stall-open-close → stall-browse-buy → stall-presence.

---

## 6. Live test plan

Harness: `python wsdev.py up`, `cap <secs> <action>`, `send <op> <hex>`, `sendspec <op> '<json>'`, `shot`, `state`.

Rules:
- Take one `shot` before and after each step.
- Client gold after enter-world is 100000 (`0x03`), so pass 100000-based values to keep labels sane.
- Seed a stack first with `sendspec 18 '{"gold":100000,"victy":1000,"item_id":3,"count":5}'` (Blue Mushroom, type 0, on merchant list 8).
- `sendspec 85` can't be used (4 bytes only); use raw `send 85` or the `0x04` record.
- One experiment at a time (feedback_experiments); restart the client between state-changing tests.

### 6.1 S2C injections (no server code needed)
| Opcode | Prerequisites | Action | Expected | Risk |
|---|---|---|---|---|
| `0x19` | seeded 5 × item 3 | `sendspec 19 '{"gold":100020,"item_id":3,"count":2,"opt_count":0,"opt_extra":0}'` | bag 5→3, gold 100020, chat "Sold 2 (Blue Mushroom)." | safe |
| `0x65` | in world | `sendspec 65 '{"bank_equip_slots":35,"bank_consume_slots":35,"bank_misc_slots":35,"equip_count":1,"repeat[equip_count]":[{"item_id":179,"option_count":0,"equip_attr_last":0}],"consume_count":1,"repeat[consume_count]":[{"item_id":3,"quantity":10}],"misc_count":0,"bank_gold":12345}'` | nothing visible (bank block filled) | safe |
| `0x80` reject | in world | `sendspec 80 '{"result":0}'` | "Invalid password. Please check your password again." box | safe |
| `0x80` open bank | `0x65` above | `sendspec 80 '{"result":1,"target_window_id":423}'`, `shot`; then repeat the `0x65` and `shot` again | bank window opens. First shot: is the grid drawn (answers Q15)? After the second `0x65`: stick in the equipment tab, 10 × item 3 in the consumable tab, gold 12345, 35 unlocked slots per tab | state_change |
| `0x66` | bank open; seeded 5 × item 3 | `sendspec 66 '{"gold":99950,"item_id":3,"qty":2,"opt_count":0,"item_ext":0}'` | bag 5→3, bank item 3 10→12 in the same slot, gold 99950 | state_change |
| `0x67` | bank open (stick in bank) | `sendspec 67 '{"item_id":179,"qty":1,"opt_count":0,"item_ext":0}'` | stick leaves the bank equipment tab and appears in the bag | state_change |
| `0x68` | bank open | `sendspec 68 '{"amount":1000,"storage_gold":13345,"gold":99000}'` | bank label 13345, inventory gold 99000 | safe |
| `0x69` | bank open | `sendspec 69 '{"amount":500,"storage_gold":12845,"gold":99500}'` | labels 12845 / 99500 (same handler) | safe |
| `0x18`+`0x25` (stall entry) | town 101 | `sendspec 18 '{"gold":100000,"victy":1000,"item_id":194,"count":1}'`, then `sendspec 25 '{"item_or_skill_id":194}'` | "You've learned skill(Open Stall)."; then the "Item stall can be opened only in the flea market." box. On map 9701 (after the flea-warp item) the stall setup window opens instead | state_change |
| `0x04` fake vendor | town 101 near x≈1411 | `sendspec 04 '{"player_count":1,"repeat[player_count]":[{"name":"Vendor","uid":8738,"job":1,"level":10,"rank_icon":99,"cur_hp":100,"cur_mp":50,"anim_substate_8cf":8,"action_state_904":8,"direction_8bd":2,"input_state_8b3":8,"pos_x":1300.0,"pos_y":714.0,"shop_open":1,"shop_sign_sprite":360,"shop_title":"Test Stall"}]}'` | remote player at x 1300 with a sign (sprite 0x168) and "Test Stall" | crash_risk (unverified sprite id; ghost entity until relog) |
| `0x86` | fake vendor | `send 86 22220000` | sign and title disappear; the player stays | safe |
| `0x85` | fake vendor after `0x86` | `send 85 22220000 6801 546573742053746f7265000000000000000000000000000000` | sign returns with "Test Store". Repeat with sprite `0000`/`0100` to map valid ids (Q9) | crash_risk |
| `0x87` | in world, mode 0 | `sendspec 87 '{"result":1,"item_count":1,"owner_uid":8738,"stall_name":"Test Stall","repeat[item_count]":[{"item_id":3,"qty":10,"price":100,"opt_count":0,"item_ext":0}]}'`; **send it twice** | window `0x259` in buyer mode, title "Test Stall", item 3 × 10 @ 100; after the second send the list still has one row (confirms the list clear) | state_change |
| `0x87` errors | window closed | `sendspec 87 '{"result":6}'`, then `'{"result":0}'` | "Open stall is opened…" box; "The shop is closed or adjusting." box | safe |
| `0x88` | `0x87` window open | `sendspec 88 '{"result":1,"gold":99800,"item_id":3,"qty":2,"opt_count":0,"item_ext":0}'`, then `'{"result":0}'` | gold 99800, row qty 10→8, bag +2 with "you've received Blue Mushroom. (Count:2)"; then "You failed to buy the items…" | state_change |
| `0x86` closes buyer | `0x87` window for owner 8738 | `send 86 22220000` | window closes with "The shop is closed or adjusting." | safe |
| `0x84` | `0x87` window open | `send 84 01`; later with no window open (Q12) | window `0x259` closes (buyer mode: nothing returned); no-window case: note any effect | state_change |
| `0x89` | no own stall | `sendspec 89 '{"gold":100200,"item_id":3,"qty":2,"opt_count":0,"item_ext":0}'` | only the gold label changes (no matching list, no chat) | safe |
| `0x83` | town, mode 0 | `send 83 01` | no visible change (sets setup flag; inert outside mode `0x259`); real test in C2S `0x5F` | state_change |
| `0x82` | town, mode 0 | `send 82 02`, then `send 82 09` | "Item stall opening failed…" box; "Invalid information transaction…" box. **Never inject `01` outside a real stall window** (sets `+0x20`; may block arena/play room until relog) | state_change |

### 6.2 C2S captures (verify real bytes; today the server logs "Unhandled" where noted)
| Opcode | Prerequisites | Action | Expected capture | Risk |
|---|---|---|---|---|
| `0x0B` | next to a UI-13 merchant (find with `shot`; idx 8 sells item 3) | `hold space 350` on the NPC, select Blue Mushroom, Buy, type 2, `cap 3 click <OK>` | `0x0B 6B 03 00 02 00 <npc_id u16>`; npc_id must equal the hni idx (Q7). Today: S2C `0x18` 16B and the gold label jumps to ~999899 (B2) | state_change |
| `0x0C` | shop open; `sendspec 18` item 3 count 300 | select the stack, Sell, type 300, `cap 3 click <OK>` | `0x0C 7B 03 00 2C 01 00 00 00`; today the server logs count 44 and the client removes 44 (B1) | state_change |
| `0x51` | bank NPC 97 found via `shot`; optionally read byte `[0x70EECC]+0x218` first | `hold space 350` on the NPC, type `test`, `cap 3 click <OK>` | either the "no SS# in this ID" box (Q2 → F3b) or `0x51 25B 74 65 73 74 00 … A7 01 00 00`; today no reply | safe |
| `0x3C` | `0x65` + `0x80 {1,423}` injected; bag item 3 × 5 | select item 3, deposit, qty 2, fee dialog shows **50**, `cap 3 click <OK>` | `0x3C 7B 03 00 02 00 00 00 00`; with the stick: `B3 00 01 00 00 00 00` | safe |
| `0x3D` (a) | bank open, stick in the equipment tab | `cap 3` double-click or drag the stick to the inventory | `0x3D 7B B3 00 01 00 00 00 00` | safe |
| `0x3D` (b) | bank open, item 3 in the consumable tab | select, withdraw, qty 1, `cap 3 click <OK>` | `0x3D 7B 03 00 01 00 00 00 00` | safe |
| `0x3E` | bank open | inventory gold-deposit `0x26`, type 1000, `cap 3 click <OK>` | `0x3E 8B E8 03 00 00 00 00 00 00` | safe |
| `0x3F` | bank open, bank_gold 12345 | bank gold button, type 500, `cap 3 click <OK>` | `0x3F 8B F4 01 00 00 00 00 00 00` | safe |
| `0x15` (194) | `sendspec 18` item 194 (learned) | open skills (`key k`), `cap 3` use Open Stall | `0x15 2B C2 00`; today no reply → the equip lock appears (B10). Recover with `sendspec 25 '{"item_or_skill_id":194}'` or relog | state_change |
| `0x61` | fake vendor with shop_open | `cap 3 click <sign>` | `0x61 4B 22 22 00 00` | safe |
| `0x62` | `0x87` buyer window for owner 8738 | select item 3, Buy (`0x1C`), qty 2, `cap 3 click <OK>` | `0x62 11B 22 22 00 00 03 00 02 00 00 00 00` | safe |
| `0x5E` | map 9701 (flea-warp), skill 194 learned, bag item 3 × 5, `0x25 {194}` injected | register item 3 × 5 at 100, title "Test", `cap 3 click <Start>` | `0x5E 01 "Test"+NUL pad(25) 03 00 05 00 64 00 00 00 00 00 00`; today no reply → close is impossible (B9); unstick with `send 84 01` | disruptive |
| `0x5F` | after `0x5E` + `send 82 01` | `cap 3 click <control 0x1B>` | `0x5F 0B`; then `send 83 01` → setup mode again | state_change |
| `0x60` | selling (as above) | `cap 3 click <Close 0x1A>`; also a portal on 9701 for the auto-send | `0x60 0B` (possibly repeated); `send 84 01` → window closes and items return to the bag | disruptive |

### 6.3 Acceptance after server items land
- **sell-parse / wallet:** buy 2 × item 3, portal to 102. Gold equals the post-buy value, the bag still holds the potions (with item I-05), and selling 300 of a 300-stack removes exactly 300.
- **bank items:** deposit 2 potions (gold −50), relog, open the bank: items and bank gold persist. Fill the consumable tab so slot 0 holds 998 × item 3 and slot 1 holds 10 × item 3 with no empty slot. Deposit 5: the server refuses with a `0x65` resync and the client bank is unchanged (§1.5 quirk).
- **stalls** (two clients, distinct uids, both on 9701): A casts 194, lists 5 × item 3 at 100, starts; B sees the sign. B buys 2: B −200 gold and +2 items, A +200 with "Sold 2 (…)". A portals away: the sign disappears for B, A's 3 unsold items are in A's bag after the map load, not duplicated. Kill A mid-sale: B's window closes with "closed", and A's escrow returns on relog.

---

## 7. Open questions

1. **`0x68` vs `0x69`.** Which did the original server use for deposit and which for withdraw? The client runs identical code; we use `0x68` for deposit and `0x69` for withdraw.
2. **SS# gate.** Where does `scene+0x131` come from, and is `scene+0x218` non-zero on our launch path? That decides whether the bank NPC shows the password dialog (live test `0x51`; premium_cash T-218).
3. **Second password.** Is it the login password or a separate SS#-derived code? The field is 21 bytes, the same width as the login password; default to the login password.
4. **Bank scope.** Per account or per character? The client keeps one block per session. We follow premium_cash's per-character `bank_slots`.
5. **Default bank capacities.** The client allows 0..60; items 1887-1889 add 5 up to 60. We use 35 to match premium_cash. Were the original defaults lower?
6. **Item def offsets.** Is Sell at `def+0x1E4`, and which hii column is `def+0x142` (skill max level)? Map it by reading a few defs live (dump_item_catalog.py pattern).
7. **`npc_id` in `0x0B`.** Is it always the hni idx (`npc+0xE60`)? Confirm with the `0x0B` capture.
8. **`item_extra`.** What is record `+0x0C` / option word 5 (refine, durability, grade)? Stored and echoed opaquely.
9. **Stall sign sprite.** Which `sign_sprite_id` values are valid CSpriteControl indices? The client never sends a choice. `0x168` draws the cash-shop sign; the real stall sign may be a different index.
10. **Stall limits.** What is the maximum item count of window `0x259` (list capacity) and the maximum title length of edit control `0x1F` (buffer 25, so at most 24 chars + NUL)? `STALL_MAX_ITEMS = 20` is provisional.
11. **Refreshing other viewers.** `0x87` rebuilds cleanly, but viewers who closed the window send nothing, so a pushed `0x87` would re-open it. Is there any client signal for a buyer closing? None was found; the design refreshes only the buyer who just failed a purchase.
12. **Duplicate `0x84`.** What does `0x84 {1}` do when window `0x259` is already closed (`FUN_00483430(0x259,1,1)`)? It matters for the repeated automatic `0x60`.
13. **Flea market access.** How did players reach map 9701? There is no portal in `portals.json`/`gamedef.maps`. The village-transfer NPC 117 (UI 600, C2S `0x5D`) builds its destination list from data not found in the exe. What are the valid spawn coordinates on `stage97_01`?
14. **Stall fees.** Did the original server charge a listing fee or sales tax? There is no client evidence.
15. **Bank grid on open.** Is window `0x1A7` pre-created, so that a `0x65` sent before `0x80` already draws the grid? The design sends `0x65` both before and after.
16. **Arena/play-room gate.** Do "Can't do it with opened item stall" checks (`FUN_00444400`/`FUN_00444590`) read `ctx+0x20` alone? That decides how risky a stray `0x82 {1}` is.
17. **Window `0x235`.** What is the third password-gated window? It is allowlisted only so the shared `0x51` reply stays correct.
18. **Cast refusal for 194.** Does the original server refuse the cast off-map with `0x5F`, or confirm it and let the client show the flea-market message (our choice)?
