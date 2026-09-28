# Trade system design (group `trade`)

Player-to-player item and gold exchange ("Exchange" window), plus the C2S request that opens another player's item stall.

Sources, in order of authority: `re_tools/corpus/systems/trade.json` (18 specs, all confidence `high`, all `server_status: missing`), `corpus/protocol_spec.json` for other groups, `LIVE_TEST_LOG.md`, `WindSlayer2Game/server/windslayer_server.py` (line numbers below refer to the file as of 2026-09-17, 2793 lines), memory notes, PySlayer. PySlayer has no trade code at all (no parser, no builder, no multi-client broadcast), so everything here comes from the EN client.

---

## 0. Summary

- **Opcodes.** C2S `0x20` request, `0x21` accept, `0x22` add item (2 send sites, 1 grammar), `0x23` lock with gold, `0x24` cancel, `0x25` final confirm, `0x26` remove item, `0x61` visit item stall. S2C `0x45` request prompt, `0x46` open window, `0x47` request failed, `0x48` side locked, `0x49` canceled, `0x4A` completed, `0x4B` item added, `0x4C` partner item removed, `0x4D` partner has no room. None of them are implemented.
- **Who moves what.** The client moves items in its own bag and the server never sends item packets for trade. The client takes its own offered item out of the bag when the server echoes `0x4B` with `owner_uid == own uid`. It puts its own items back on `0x49`, and adds the partner's items (from its partner list) plus the new absolute gold on `0x4A`. The server holds offered items in escrow and changes its own inventory model only at commit. **It must not send 0x18/0x19/0x23 for traded items**, or the client ends up with duplicates.
- **Hard prerequisites (both missing today).** (1) Each account needs its own uid. Today every login gets uid 1. (2) The client needs the partner's entity in its scene (S2C `0x05`/`0x04`, removed with `0x06`). Without that entity the client refuses to send `0x20` ("Both players must be in same field to trade items."), and it can't even open the right-click menu on a player whose uid equals its own.
- **Proven live bug.** C2S `0x25` is TradeFinalConfirm (its only send site in the client), but `_dispatch` sends it to `_handle_attack`. That parser happens to read the trade layout and treats offered item ids as monster uids (see B1).

---

## 1. How the client implements trade

### 1.1 UI objects

| Id | What | Relevant controls / fields |
|---|---|---|
| window `0x50` | Player popup menu. Opens on **right-click (WM_RBUTTONUP 0x205) or double-click (0x203)** on a remote player (alive 3, uid != `scene+0x220`) in field mode (`scene+0xF00 == 6`), in `FUN_0044c4b0` (decomp lines 74-78, 152-160). Stores the clicked uid at `+0x134`. | ctrl 4 = trade (C2S 0x20); ctrl 5 = party; ctrl 7 = friend; ctrl 10 = compliment |
| window `0x70` | Trade request Yes/No popup | ctrl 1 label = requester name (set by S2C 0x45); only event 3 (OK) is handled, and it sends C2S 0x21. **Decline sends nothing.** |
| window `0x4E` "Exchange" | Trade window | ctrl 2 "End" (X), ctrl 3 "OK" (lock), ctrl 4 "Cancel", ctrl `0x1E` own gold edit box, ctrl `0x1F` partner gold label, ctrl `0x21` partner name. Own offer list `+0xAC` (head `+0xB0`, count `+0xB8`, **max 12**); partner list `+0xBC` (head `+0xC0`, no client cap) |
| window `0x74` | Quantity prompt for stackables dropped on 0x4E | edit 5 = qty (default = the stack's count); OK (button 2) sends C2S 0x22 |
| window `0x291` "Confirm Exchange" | Final review window, opened when both sides are locked | ctrl 2 "End", ctrl 3 "Trade" (C2S 0x25), ctrl 4 "Cancel", ctrl `0x1D` my gold, ctrl `0x1E` partner gold; copies of both lists |
| window `9` | Inventory | drag source for 0x22 and drop target for 0x26; ctrl `0xB` gold label |
| window `0x259` | Item stall window (shop group) | opened in buyer mode by S2C 0x87 |

Client-side trade state: `ctx+0x24` = partner-locked flag (**cleared only by S2C 0x46**). Own lock = `0x4E` ctrl 3 state 0. 0x38-byte item record: `+0x00 u16 id`, `+0x02..+0x0D` 6 x u16 option block (words 0-4 = options/stones, word 5 = `attr_0c`/`opt_extra`), `+0x0E u16 count`, `+0x18 locked/greyed`.

**Item descriptor wire encoding** (same as 0x0F, 0x19, 0x23, 0x5E, 0x62, 0x87): `u8 n` (number of LEADING non-zero words among words 0-4, at most 5), then `n x u16`, then `u16 word5`. The client stops counting at the first zero word, so a block like `[5,0,7,...]` goes on the wire as `[5]`. The server must canonicalize blocks the same way (zero every word after the first zero in words 0-4) before comparing them. **Never send n > 5.** 0x4B overflows its 12-byte stack buffer, and n >= 59 kills the process through the /GS cookie.

### 1.2 Client-side gates

- **0x20 (request):** not in an arena or play room (`FUN_00444700`); own manner (`scene+0xEE0`) > -60; target entity found in the current scene by uid, else "Both players must be in same field to trade items."; target manner (`entity+0x15D8`, the i32 after uid in 0x05/0x07) > -60. Before sending, the client prints "You requested <name> to make an transaction."
- **0x22 (add):** 0x4E ctrls 2 and 3 enabled (not locked); own offer count < 12, else "There are no more spaces in trade slot"; catalog `+0x1F0 == 0` (cash/no-trade flag; silently ignored otherwise); catalog type `+0x154`: 1 (equipment) sends right away with qty 1, 0 or 2 (stackable) opens dialog 0x74 (max 999 for type 0, 99 for type 2, and no more than owned), any other type does nothing. The client keeps no pending state, so repeated drags send duplicate 0x22s.
- **0x23 (lock):** gold typed in ctrl 0x1E must be <= client gold (`player+0x228`), else "You are short of gold." and no send. The client disables ctrl 3 before sending.
- **0x24 (cancel):** no gate. The client disables ctrls 2 and 4 and waits. **If the server never sends 0x49, the window can't be closed.**
- **0x25 (final):** the client disables button 3 of 0x291 before sending. The packet echoes the WHOLE deal as the client sees it.
- **0x26 (remove):** ctrls 2 and 3 enabled. The client first puts the item back in its own bag (`FUN_00467200`) and sends only if that worked. The node is freed either way (hazard: bag full leaves the client with no node and no packet). Several identical nodes are all removed in one drag, one 0x26 each, and every `trade_slot_index` counts against the list as it was BEFORE the drag.
- **0x61 (stall visit):** left-button-up over a remote player whose `entity+0x14F4` stall flag is set (by S2C 0x85, or by the `shop_open` tail of 0x04/0x07); no own stall open or being browsed; no trade modal.

### 1.3 Opcode sheet (grammars from the spec)

| Key | Dir | Name | Grammar (wire, after opcode) | Len |
|---|---|---|---|---|
| `0x4484CC/0x20` | C2S | TradeRequest | `u32 target_uid` | 4 |
| `0x469C33/0x21` | C2S | TradeRequestAccept | `str[17] requester_name` | 17 |
| `0x46C6B5/0x22` (+ `0x469D9C/0x22`) | C2S | TradeAddItem | `u16 item_id, u16 quantity, u8 enchant_count, repeat(enchant_count){u16 enchant}, u16 enchant_last` (the stackable site always sends count 0 and last 0) | 7..17 |
| `0x469D05/0x23` | C2S | TradeLockOffer | `u64 gold` | 8 |
| `0x469D9C/0x24` | C2S | TradeCancel | `stop` | 0 |
| `0x469BA8/0x25` | C2S | TradeFinalConfirm | `u64 my_gold, u8 my_item_count, repeat{u16 item_id, u16 amount, u8 opt_count, repeat{u16 opt}, u16 attr_0c}, u64 partner_gold, u8 partner_item_count, repeat{same}` | 18 + items |
| `0x46C92C/0x26` | C2S | TradeRemoveItem | `u16 item_id, u16 quantity, u8 trade_slot_index, u8 enchant_count, repeat{u16 enchant}, u16 enchant_last` | 8..18 |
| `0x44CA2F/0x61` | C2S | ItemStallVisitRequest | `u32 stall_owner_uid` | 4 |
| `0x45` | S2C | TradeRequestReceived | `str[17] requester_name` (NUL within 16 chars) | 17 |
| `0x46` | S2C | TradeWindowOpen | `str[17] partner_name` (NUL within 16 chars) | 17 |
| `0x47` | S2C | TradeRequestResult | `u8 result` (6 "Trading.", 4 "The player is rejecting trade.", any other value ignored) | 1 |
| `0x48` | S2C | TradeConfirmed | `u32 confirmer_uid, u64 gold` | 12 |
| `0x49` | S2C | TradeCanceled | `stop` | 0 |
| `0x4A` | S2C | TradeCompleted | `u64 gold` (new ABSOLUTE gold of the receiver) | 8 |
| `0x4B` | S2C | TradeItemAdded | `u16 item_id, u16 count, u32 owner_uid, u8 opt_count, repeat{u16 opt}, u16 opt_extra` | 11..21 |
| `0x4C` | S2C | TradePartnerItemRemoved | `u8 slot_index` (0-based index into the partner list) | 1 |
| `0x4D` | S2C | TradePartnerSlotsFull | `stop` | 0 |

Related S2C used by these flows (other groups): `0x05` RemotePlayerAppear, `0x04` PlayerAppear (list form with stall tail), `0x06` PlayerDespawn, `0x2A`/`0x1B` movement relay, `0x85`/`0x86` stall sign, `0x87` stall item list, `0x3F` GoldUpdate.

### 1.4 Trade lifecycle

```
IDLE --0x20(A->B)--> INVITED (B.pending_invite = A)          [0x45 to B]
INVITED --0x21 from B--> OPEN                                [0x46 to A and B]
INVITED --expiry / A leaves / B busy--> IDLE                 [nothing, or 0x47 to A]
OPEN: 0x22 / 0x26 while the sender is unlocked                [0x4B to both / 0x4C to partner / 0x4D to sender]
OPEN --0x23 from X--> X locked                               [0x48{X,gold} to both]
both locked --> CONFIRMING (both clients open 0x291)
CONFIRMING --0x25 from X, echo valid--> X final               [nothing]
both final --commit ok--> DONE                               [0x4A{new gold} to each]
any state --0x24 / disconnect / portal / validation fail--> CANCELED  [0x49 to both]
```

---

## 2. Request/response flows

Notation: A = the player who acts, B = the partner. `uid_X` = X's session uid. Every S2C below is built with the spec grammar (see 5, item `trade-codec`).

### 2.1 Trade request: C2S 0x20 -> S2C 0x45 or 0x47

1. A right-clicks B's body; popup 0x50 opens with `+0x134 = uid_B`. A clicks the trade entry (ctrl 4). Client gates from 1.2 apply, and the local line "You requested <B> to make an transaction." is printed.
2. Client A sends **C2S 0x20** `{target_uid: uid_B}`.
3. Server (`_handle_trade_request`):
   1. Decode with `0x4484CC/0x20`. Ignore the packet if A is not in the world, `target_uid == uid_A`, or A already has a trade (`A.trade is not None`).
   2. `B = sessions_by_uid.get(target_uid)`. If B is missing, offline, not in the world, or `B.current_map != A.current_map`, **send nothing**. The client already checked "same field", so this only happens on a race. Optionally send a chat line.
   3. If `B.refuse.exchange` (byte 1 of C2S 0x2B, updated by C2S 0x40) -> **S2C 0x47 `{result: 4}` to A** ("The player is rejecting trade.").
   4. If B has an active trade, or a live pending invite from someone else -> **S2C 0x47 `{result: 6}` to A** ("Trading."). The 0x70 dialog has no busy check, so the server must not stack a second 0x45 on B.
   5. If either manner value is <= -60 (server model, default 0): send nothing. The client already gates this.
   6. If B already holds a pending invite from A that is younger than 10 s: ignore the packet (spam guard).
   7. Otherwise set `B.trade_invite = {from_uid: uid_A, from_name: A.char_name, ts: now}` and send **S2C 0x45 `{requester_name: A.char_name[:16]}` to B**. B sees dialog 0x70.
4. No S2C goes to A on success. A's client expects nothing.

### 2.2 Accept: C2S 0x21 -> S2C 0x46 to both

1. B clicks OK on 0x70, and the client sends **C2S 0x21** `{requester_name}` (the label text read back from the dialog).
2. Server (`_handle_trade_accept`):
   1. `inv = B.trade_invite`. It must exist, `inv.from_name == requester_name` (exact match), `now - inv.ts <= 30 s`, A = `sessions_by_uid[inv.from_uid]` must be online, in the world and on the same map as B, and neither side may have a trade. Clear `B.trade_invite` in every case.
   2. If any check fails: when A is now trading, send **S2C 0x47 `{result: 6}` to B**; otherwise send nothing (optionally a chat line "The request has expired.").
   3. Create a `TradeState` (see 3.3) and set `A.trade = B.trade = t`.
   4. Send **S2C 0x46 `{partner_name: B.char_name[:16]}` to A** and **S2C 0x46 `{partner_name: A.char_name[:16]}` to B**. Each client shows 0x4E, re-enables ctrls 2/3/4 on 0x4E and 0x291, frees both item lists of both windows, clears `ctx+0x24`, and sets both gold texts to "0".
3. Invariant: **never send 0x46 to a session whose trade is still open.** 0x46 frees the offer lists without putting items back in the bag, so the client's offered items would vanish. Always send 0x49 first.

### 2.3 Decline or expiry (no packet from the client)

1. Clicking No/X on 0x70 sends nothing.
2. A server timer (checked lazily on the next 0x20/0x21, or by a 5 s housekeeping tick) drops invites older than 30 s. Default: send nothing to A. Option (open question Q1): send 0x47 `{4}` to A on explicit expiry.

### 2.4 Add item: C2S 0x22 -> S2C 0x4B to both, or 0x4D, or silence

1. A drags an inventory item onto 0x4E.
   - Equipment (type 1): C2S 0x22 `{item_id, quantity: 1, enchant_count: n, enchant[0..n-1], enchant_last}`. The live capture shape for the Wooden Stick would be `B3 00 01 00 00 00 00`.
   - Stackable (type 0/2): dialog 0x74 opens, the player types a qty, OK sends C2S 0x22 `{item_id, qty, 0, 0}`.
2. Server (`_handle_trade_add`):
   1. Decode with `0x46C6B5/0x22` (it covers both send sites). `t = A.trade`; it must be OPEN (not CONFIRMING or DONE). `A` must not be locked. If `B` is locked, see the policy note below.
   2. `info = _gamedef_item(item_id)`. Require `info.Type in (0,1,2)`, `info.Cash == 0`, `info.NotTrade == 0`, and the id must be in the EN client catalog (see 3.5). Require `1 <= qty`, `qty <= 999` (type 0) or `<= 99` (type 2), and `qty == 1` for type 1.
   3. Ownership against escrow: `available(A, item) = owned - already offered in t`. For stackables `qty <= available`. For equipment, an owned instance with the same canonical 6-word block must exist that is not already offered.
   4. `len(t.side[A].offer) < 12`.
   5. Capacity on the receiving side: `can_receive(B, t.side[A].offer + [new])` minus what B gives away (3.6). If this fails, send **S2C 0x4D (empty) to A** and stop. Client A shows "There are no more spaces in the other player's item slot.".
   6. Any other failure (steps 1-4): **send nothing**. The client changes nothing locally until 0x4B arrives, so silence is safe. An optional chat line helps debugging.
   7. Success: append `{item_id, qty, block}` to `t.side[A].offer`. Send **S2C 0x4B `{item_id, count: qty, owner_uid: uid_A, opt_count, opt[], opt_extra}` to A** (self path: `FUN_00467140` removes the item from A's bag and appends it to A's own grid) and **the identical packet to B** (partner path: appended to B's partner list).
3. Hazards the server must respect: send 0x4B only while the owner's 0x4E window is open, otherwise the self path removes the item from the bag and leaks the record. The block must equal the bag copy byte for byte, or the self path fails silently and nothing enters the grid (the server can't detect this, but the 0x25 echo will catch it). `opt_count <= 5`.
4. Policy after the partner has locked (open question Q4): default to **rejecting 0x22 when either side is locked** (silence plus chat line "Offer is locked; cancel to change it."). No unlock packet exists, and the client leaves nothing to undo.

### 2.5 Remove item: C2S 0x26 -> S2C 0x4C to the partner only

1. A drags an item from its own side of 0x4E to the inventory or to empty space. The client has ALREADY put the item back in its bag and removed the node, and now sends C2S 0x26 `{item_id, quantity, trade_slot_index, enchant_count, enchant[], enchant_last}`.
2. Server (`_handle_trade_remove`):
   1. Decode with `0x46C92C/0x26`. `t = A.trade` must be OPEN.
   2. If A is locked (the client normally prevents this) or B is locked (policy Q4): the client state has already changed, so resynchronize by **cancelling the trade (0x49 to both, see 2.8)**.
   3. Find the entry: if `0 <= idx < len(offer)` and `offer[idx]` matches `(item_id, quantity, canonical block)`, use it. Otherwise use the first entry that matches by content. If nothing matches, log it and ignore (this happens after the burst-index shift in 1.2).
   4. `pos` = the entry's CURRENT index in `t.side[A].offer`. Remove it.
   5. Send **S2C 0x4C `{slot_index: pos}` to B only**. Never send it to A, because it would delete an entry from A's view of B's list. Identical duplicates are interchangeable, so removing by content with the compacted index keeps B's list the same as the server's list.
3. Known client hazard: if the bag restore fails (bag full), the node is freed and nothing is sent. The server still counts the item as offered and A's list is missing it, so the 0x25 echo mismatch (2.7) will cancel the trade.

### 2.6 Lock: C2S 0x23 -> S2C 0x48 to both (or cancel)

1. A types gold in ctrl 0x1E and clicks OK (ctrl 3). The client checks gold against its own copy, disables ctrl 3 and sends C2S 0x23 `{gold}`.
2. Server (`_handle_trade_lock`):
   1. `t = A.trade` must be OPEN and A not already locked. Otherwise ignore (a duplicate click can't happen because ctrl 3 is disabled).
   2. `gold <= A.gold` (server-authoritative). If this fails there is no "lock refused" packet, and A's ctrl 3 stays disabled, so **cancel: 0x49 to both** plus a chat line to A "Not enough gold.".
   3. `t.side[A].gold = gold; t.side[A].locked = True`.
   4. Send **S2C 0x48 `{confirmer_uid: uid_A, gold}` to A AND to B**. The self echo is required: the client that locks second opens 0x291 only when its own echo arrives.
   5. If both sides are locked: `t.state = CONFIRMING`. Snapshot `t.side[*].offer` and gold for echo validation. Both clients open 0x291 and hide 0x4E. The first locker opens it when the partner's 0x48 arrives; the second locker opens it on its own echo.
3. `confirmer_uid` must equal the receiver's registered local uid for the self branch. With duplicate uids (bug B3) every 0x48 takes the self branch and the final window never opens.

### 2.7 Final confirm: C2S 0x25 -> wait, or S2C 0x4A to both, or 0x49 to both

1. In 0x291, A clicks "Trade" (ctrl 3). The client disables the button and sends C2S 0x25 `{my_gold, my items, partner_gold, partner items}`, built from the 0x291 texts and lists.
2. Server (`_handle_trade_final`):
   1. Decode with `0x469BA8/0x25`. `t = A.trade` must be CONFIRMING and A not already final.
   2. **Echo validation:** `my_gold == t.side[A].gold`; `my items == t.side[A].offer` (same order, `item_id`, `amount == qty`, canonical block, where `opt[]` + `attr_0c` is the block); `partner_gold == t.side[B].gold`; `partner items == t.side[B].offer`. On any mismatch -> **cancel (0x49 to both)** and log a diff. Mismatch sources: the gold edit box changed after lock (0x291 copies the edit text when it opens), a missed 0x4B or 0x4C, the 0x26 bag-full hazard, or tampering.
   3. `t.side[A].final = True`. If B is not final yet, **send nothing**. A can still cancel, because ctrls 2 and 4 of 0x291 stay enabled.
   4. When both are final, run **commit** under the global lock:
      1. Re-validate, since inventories can change between lock and final (escrow guards in 3.6 should prevent it): A owns every offered qty/instance and `t.side[A].gold <= A.gold`; the same for B; `can_receive(B, A.offer, minus B.offer)` and `can_receive(A, B.offer, minus A.offer)`; neither new gold overflows u64 (clamp at 2^63-1 in practice).
      2. Failure -> cancel (0x49 to both).
      3. Apply: remove A.offer from A's inventory and add it to B's, and the reverse. `A.gold = A.gold - gA + gB`; `B.gold = B.gold - gB + gA`.
      4. Persist both characters (`_save_accounts()`) BEFORE sending anything.
      5. Send **S2C 0x4A `{gold: A.gold}` to A** and **S2C 0x4A `{gold: B.gold}` to B**. Each client prints "Trade completed.", overwrites gold, adds every partner-list item with "you've received %s. (Count:%u)", and closes 0x4E and 0x291.
      6. Clear `A.trade = B.trade = None`. Append a line to the trade audit log.
   5. **Never** send 0x18/0x19/0x23/0x3F for the traded items or gold. The 0x4A gold is absolute and the item adds are client-side, so extra packets would duplicate items in the bag view.

### 2.8 Cancel: C2S 0x24 -> S2C 0x49 to both

1. A clicks End/Cancel in 0x4E or 0x291. The client disables ctrls 2 and 4 and sends C2S 0x24 (empty).
2. Server (`_handle_trade_cancel`):
   1. If `A.trade` exists (any state except DONE): run `_trade_cancel(t, reason)`, which sends **S2C 0x49 to both** sessions that are still connected and clears both `.trade` fields. Server inventories are untouched: offered items were only in escrow.
   2. If A has no trade (stale window, or the partner already cancelled): **still send S2C 0x49 to A**. It is harmless with no trade open, and without it A's window stays stuck.
3. Client effect: "Trade has been canceled.", 0x4E and 0x291 hidden, own offered items put back in the bag (items already removed by 0x4B return). The partner list is left as is and freed by the next 0x46.

### 2.9 Cancels the server starts on its own

Call `_trade_cancel` (0x49 to every party still connected) and clear any `trade_invite` naming the session:
1. **Disconnect**: in the `finally` of `_handle_fireway` (server lines 578-581), before `del self.sessions[addr]`. Also send S2C 0x06 `{uid}` to map peers (presence).
2. **Portal / map change**: at the top of `_handle_change_map` (line 1244), **before** 0x08 (line 1283). 0x08 and 0x03 destroy every entity on the client, including the partner, and 0x08 closes a batch of windows. Sending 0x49 first lets the client restore its offered items while window 0x4E still exists.
3. **Re-entering the world** (a new C2S 0x2B on a session that has a trade): cancel first.
4. **Validation failures** in 2.6 and 2.7, and a partner that despawns for any other reason.

### 2.10 Stall visit: C2S 0x61 -> S2C 0x87

1. B has an open stall (shop group: C2S 0x5E -> S2C 0x82 result 1 -> broadcast S2C 0x85 `{owner_uid: uid_B, sign_sprite_id, stall_title}` to the map; late joiners get 0x85 after their 0x05/0x07 spawn, or the `shop_open` tail of 0x04/0x07).
2. A left-clicks B's sign or body, and the client sends **C2S 0x61** `{stall_owner_uid: uid_B}` (client gates from 1.2).
3. Server (`_handle_stall_visit`):
   1. Decode `0x44CA2F/0x61`. `B = sessions_by_uid.get(uid)`.
   2. If A's own stall is open or being set up -> **S2C 0x87 `{result: 6}`** ("Open stall is opened. Close the Open stall and try again."; the client usually catches this first).
   3. If B is missing, on another map, B's stall is not open, B is editing (after C2S 0x5F), or B's stall has no items -> **S2C 0x87 `{result: 0}`** ("The shop is closed or adjusting.").
   4. Otherwise -> **S2C 0x87 `{result: 1, item_count: n, owner_uid: uid_B, stall_name: title[:24], items: [{item_id, qty, price, opt_count(<=5), opt[], item_ext}]}`**. Client A opens 0x259 in buyer mode. Record `A.browsing_stall = uid_B` so the shop group can send 0x86 and validate C2S 0x62 buys (0x88/0x89).
   5. Until the shop group lands, a stub that always answers `{result: 0}` is correct and safe.

---

## 3. Server state and data model

### 3.1 Multi-session prerequisites (shared infrastructure; build once for trade, party, friend, whisper and PvP)

1. **Unique, persistent uid per account.** Add `"uid"` at account level in `accounts.json` (`test` -> 1 so the harness keeps working, `admin` -> 2, new accounts -> max+1). Keep player uids in `1..0xEFFFF`: they must not collide with `MOB_UID_BASE 0xF0000`, `GROUND_ITEM_UID_BASE 0x200000`, char-select entities `30000000+i`, or map NPCs `33000000+n`. `_handle_login` sets `session['account_id'] = session['uid'] = account['uid']`. It is sent in 0x02 (which writes `scene+0x220`) and must be used for the local 0x07 record, 0x1D, 0x22 SetLevel and 0x08, replacing `char.get('uid', 1)`. Refuse a second concurrent login of the same account, or kick the older session; otherwise two sockets share one uid.
2. **Registry:** `self.sessions_by_uid: {uid: session}` (set after login, removed in `finally`); `self.map_members: {map_id: set(uid)}` (maintained on enter-world, portal and disconnect); `session['in_world']` (True after the local 0x07 is sent, False from portal start until the new 0x07). One `self.world_lock = threading.RLock()` guards the registry and every TradeState. Connection threads are independent (`start`, line 424).
3. **Thread-safe cross-session send:** create `session['send_lock'] = threading.Lock()` when the session dict is built (lines 509-517), not lazily with `setdefault` (line 585). Store `session['no_enc']` from the last received packet and use it as `use_by_array` when another thread sends to this session. In-world traffic is NoEncode/by-array: the admin injector and the live captures both use by-array.
4. **Presence (needed before 0x20 can be sent):**
   - After the local 0x07 (+0x28/0x44) in `_handle_enter_world` (lines 750-783) and in `_handle_change_map` (lines 1302-1315): for each peer `P` in the same map and in the world, send S2C **0x05** (P's record) to the newcomer, and 0x05 (newcomer's record) to P. Follow up with 0x85 when the peer has an open stall. 0x04 is the list form with the stall tail. Never send a record whose uid equals the receiver's uid; it would replace the local player.
   - On leaving a map (portal, before 0x08) or disconnect: S2C **0x06** `{uid}` to the old map's peers. Send it only for remote uids, never the receiver's own.
   - The remote record builder uses the `0x05` grammar: real `cur_hp`/`cur_mp` (0 renders dead); idle defaults `attack_dir_8cf=8, motion_id=8, facing_dir=2, input_dir_8b3=8`, everything else 0; `manner_points` from the server model; last-known `pos_x/pos_y` (spawn or portal coordinates, updated from the C2S 0x0D interact tail when present).
   - Movement relay (C2S 0x0D -> S2C 0x2A/0x1B to peers) is **not required for trade**. Without it the ghost stays at its spawn point, and right-clicking where it is drawn still works. It is required for normal play (P1, other group).
5. **Harness targeting:** the admin injector sends to every session (lines 459-461). Add an optional `"uid"` / `"user"` key to the JSON line and `wsdev.py sendspec --to <uid>`. wsview also needs `rclick x y` (popup 0x50) and `drag x1 y1 x2 y2` (0x22/0x26 use drag-and-drop released with WM_LBUTTONUP).

### 3.2 Session fields (new)

| Field | Type | Source / use |
|---|---|---|
| `uid` | u32 | account uid (3.1) |
| `char_name` | str (<= 16 chars) | already set at line 721 |
| `current_map`, `in_world` | int, bool | presence, same-field check |
| `no_enc`, `send_lock` | bool, Lock | cross-thread sends |
| `refuse` | dict whisper/exchange/party/talk/friend (0/1) | C2S 0x2B bytes 0-4 (spec `0x42F904/0x2B`), C2S 0x40 `0x4414BA/0x40` |
| `manner` | i32 | 0 default; social group adds 0x96/0x97 |
| `x`, `y` | float | last known position for 0x05 |
| `trade` | TradeState or None | active trade |
| `trade_invite` | `{from_uid, from_name, ts}` or None | incoming pending request |
| `stall` | shop-group dict `{open, editing, title, sign, items}` | 0x61 |
| `browsing_stall` | uid or None | 0x61 -> 0x62/0x86 |

### 3.3 TradeState

```python
@dataclass
class TradeSide:
    uid: int
    offer: list          # [{'item_id': int, 'qty': int, 'block': (w0,w1,w2,w3,w4,w5)}], order = 0x4B order
    gold: int = 0
    locked: bool = False
    final: bool = False

@dataclass
class TradeState:
    id: int
    a: TradeSide          # requester
    b: TradeSide          # accepter
    state: str = 'OPEN'   # OPEN | CONFIRMING | DONE | CANCELED
    created: float = 0.0
    def side(self, uid): return self.a if self.a.uid == uid else self.b
    def other(self, uid): return self.b if self.a.uid == uid else self.a
```
Keep `self.trades: {id: TradeState}` on GameServer for admin inspection. It lives in memory only: a server restart during a trade needs no recovery because escrow never touched the inventories.

### 3.4 Persistent character economy (required for meaningful trades)

Today gold and inventory exist only in the per-connection session dict (see B6, B7). Proposed per-character layout in `accounts.json`:
```json
{"name": "TestHero", "gold": 100000,
 "inventory": {"equip":   [{"id": 179, "block": [0,0,0,0,0,0]}],
               "consume": {"7": 5},
               "etc":     {"8": 3}},
 "equip_grid": {"5": {"id": 179, "block": [0,0,0,0,0,0]}}}
```
- The tab is picked by gamedef `items.Type`: 0 -> `consume` (u16 stacks, <= 999 in the trade dialog), 1 -> `equip` (one instance per slot, 12-byte block), 2 -> `etc` (u8 stacks, <= 99 in the trade dialog). Capacities come from the 0x03 tab-slot bytes (currently 35/35/35, server lines 2071-2073).
- `_build_pyslayer_opcode_03` must send the real `gold` (not the constant 100000 at line 2044) and fill `equip_item_count`/`consume_item_count`/`etc_item_count` from this model (it sends 0 today, lines 2076-2079). Otherwise every 0x03 (enter-world and each portal, line 1300) rebuilds the client bag from empty lists while the server model still holds items.
- `_wallet` (lines 997-1000) reads and writes `char['gold']`.

### 3.5 Gamedef content

- `items` (gamedef.sqlite3, 4609 rows): `Type` (0 consumable / 1 equipment / 2 misc / 3 skill / 4 class change / 5-6 cash utility), `Cash` (675 rows = 1), `NotTrade` (36 rows = 1), `Kind`, `name`. Tradeable if `Type in (0,1,2) and Cash == 0 and NotTrade == 0`. The client's `+0x1F0` flag matches one of these columns (Q5), so the server enforces both.
- **EN catalog filter:** the client silently ignores ids outside its own `windslayer.hii` (LIVE_TEST_LOG: 0x18 item 4356 never arrives). An id missing from the EN catalog must never be offered: 0x4B self path does a DB lookup and drops unknown ids, so the offer would never show while the server escrows it. Needs an extracted EN id list (content item).
- `npcs`, `quests`, `maps`: not used by trade.

### 3.6 Validation helpers (pseudo-code)

```python
def canon(words5, last):            # client-equivalent block
    out = []
    for w in (list(words5) + [0]*5)[:5]:
        if w == 0: break
        out.append(w)
    return tuple(out + [0]*(5-len(out)) + [last])

def available(s, item_id, block=None):   # owned minus escrow
    offered = s.trade.side(s.uid).offer if s.trade else []
    ...                                  # stacks: count - sum(qty); equip: instances with block not in offer

def can_receive(s, incoming, outgoing):  # per-tab capacity after the swap
    equip_after = len(inv.equip) - n_equip(outgoing) + n_equip(incoming)      <= equip_cap(35)
    consume_ids = set(inv.consume) - fully_given(outgoing) | ids(incoming, 0)  -> len <= consume_cap(35)
    etc_ids     = same for type 2                                             -> len <= etc_cap(35)
    # stack ceilings (999 / 99 per id) - see Q6
```
**Escrow guards** (anything that removes items or gold must use `available` or `gold - offered_gold`): `_handle_sell_item` (1029-1051), `_handle_equip_item` (1467-1527), `_handle_use_item` (1160-1231), the quest demand consume in `_complete_quest` (1424-1425), `_handle_buy_item` gold (1016-1023), and the future drop (C2S 0x13/0x14), bank deposit (0x3C/0x3E) and stall open (0x5E). The simplest rule: **refuse these actions while `session.trade` is not None.**

---

## 4. Current server status and proven bugs

### 4.1 Opcode status

| Opcode | Status | Where it ends up today |
|---|---|---|
| C2S 0x20, 0x21, 0x22, 0x23, 0x24, 0x26, 0x61 | missing | `_dispatch` else-branch, lines 659-667 (log "Unhandled opcode") |
| C2S 0x25 | **wrong** | `_dispatch` lines 644-647 -> `_handle_attack` (1844-1881) |
| S2C 0x45-0x4D | missing | no builders |
| Prereqs S2C 0x05/0x04/0x06, uid, registry | missing | see B3/B4 |

### 4.2 Bugs (each backed by spec + code)

- **B1. C2S 0x25 TradeFinalConfirm is parsed as an attack hit list (wrong_behavior).** Spec `0x469BA8/0x25`: "the only literal PUSH 0x25 + Add(uchar) opcode site in the corpus", with layout `u64 gold, u8 n, n x {u16 id, u16 amount, u8 k, k x u16, u16}, u64 gold, u8 n, ...`. The server routes it at lines 644-647 to `_handle_attack`, whose parser (1853-1878) reads exactly that layout but calls the u64 gold `skill_id` and each item_id a target uid. `_resolve_hit` (1889-1897) then matches `(m.uid & 0xFFFF) == item_id`. In map 102 (8 mobs, uids 0xF0000-0xF0007), a final confirm offering item id 0-7 (e.g. 5 or 7, both consumables in gamedef) damages monster `0xF0000+id`. `_compute_damage` (1883-1887) uses a 1.5x multiplier when gold != 0, and a kill grants exp, gold and a drop (1911-1928). Combat does not need this route: live captures show basic attacks send only 0x0D (LIVE_TEST_LOG). Fix: route 0x25 to `_handle_trade_final`, delete `_handle_attack`, and leave the dead `_handle_use_skill` (1458) for the skill group.
- **B2. Trade C2S packets get no handler (wrong_behavior).** 0x20/0x21/0x22/0x23/0x24/0x26/0x61 fall into lines 659-667. Once a trade window can open, an unanswered 0x24 leaves ctrls 2/4 disabled and the window can't be closed (spec `0x469D9C/0x24` gates). An unanswered 0x25 leaves 0x291 waiting forever.
- **B3. Every session has uid 1 (crash_or_desync).** `_handle_login` hard-codes `session['account_id'] = 1` (line 2656). `_build_en_opcode_07` writes `char.get('uid', 1)` (2307) and no character in accounts.json has `uid`. `_build_en_opcode_1D_equip` (2232) and `_send_exp` -> `_send_level(uid=ch.get('uid',1))` (2011) default to 1 as well. Client effects with two players: the popup skips entities whose uid equals `scene+0x220` (FUN_0044c4b0 decomp lines 75-76 and 152-153), so the trade menu can't be opened. A remote 0x05/0x07 record with the receiver's uid replaces the local player pointer (spec 0x05 hazard 2). 0x48 and 0x4B compare against the local uid, so every echo takes the self branch: the partner's 0x4B would try to remove the item from the partner's own bag, and the final window never opens.
- **B4. No other players exist on any client (wrong_behavior).** `self.sessions` is keyed by `addr` (379, 518) with no uid index or map membership. There is no builder for 0x05/0x04/0x06, and `_handle_world_sync` (669-684) drops 0x0D without relaying. The client's 0x20 gate (`FUN_004189F0` finds the target in the scene) always fails, so trade can't be reached from the UI.
- **B5. The 0x07 record builder is unsafe for remote players (wrong_behavior).** `_build_en_opcode_07` sends `cur_hp`/`cur_mp` = 0 (2391-2392), which would render a remote player dead (spec 0x05 hazard 6). It writes client IP octets into `+0x8B3..+0x8B6` (2378-2387), which EN uses as input/state bytes (spec 0x07 references, idle default 8,0,0,0), and non-idle state constants 32/1/0/501/0 (2367-2372) instead of 0/8/8/0/2. Build remote records from the 0x05 grammar with idle defaults (3.1).
- **B6. Gold differs between client and server (wrong_behavior).** 0x03 sends gold = 100000 (line 2044), while `_wallet` defaults to `_DEFAULT_GOLD = 999999` (994, 997-1000) until the first 0x18. The client checks the 0x23 lock against 100000 and the server would check against 999999. The absolute 0x4A gold would make the client display jump. Gold is not persisted either.
- **B7. Inventory is neither persisted nor sent to the client (wrong_behavior).** `session['inventory']` (1137-1158) disappears with the socket. 0x03 always sends zero item-list counts (2076-2079; spec 0x03: `completed_quest_count, equip_item_count, consume_item_count, etc_item_count`). Every portal replays 0x03 (1298-1300), so the client bag is rebuilt from empty lists while the server model keeps its items. Trade escrow and commit need one persistent, authoritative inventory.
- **B8. The inventory model has no tabs or option blocks (wrong_behavior).** `{item_id: count}` can't tell stack tabs from equipment instances and can't hold the 12-byte block. The block is needed to validate 0x22/0x26/0x25 and to store equipment received through 0x4A, which the client stores with the partner's block.
- **B9. Race on the per-session send lock (crash_or_desync, latent).** `session.setdefault('send_lock', threading.Lock())` (585) is not atomic. When a trade partner's thread sends to this session while its own thread also sends, two locks can be created, packets can interleave, and the sequence and cipher state can desync. Create the lock at session construction (509-517).
- **B10. Disconnect leaves the partner stuck (wrong_behavior, latent).** The `finally` block (578-581) only deletes the session: no 0x49 to a trade partner, no 0x06 to map peers, and pending invites that name this session are not cleared.
- **B11. Refuse flags are ignored (wrong_behavior).** `_handle_enter_world` documents 0x2B bytes 0-4 as "zeros" (690), but they are refuse_whisper/exchange/party/talk/friend (spec `0x42F904/0x2B`). C2S 0x40 has no handler. The server can never send 0x47 result 4.
- **B12. No escrow guards (wrong_behavior, latent dupe).** Sell (1040-1044), equip (1517-1520), use (1195-1221) and quest consume (1424-1425) check only raw inventory. Once trade exists, "offer item, then sell it, then commit" duplicates value unless these handlers respect escrow (3.6).
- **B13. The name NUL guard is left to callers (crash_or_desync, latent).** `wsproto.Grammar.encode` pads `str[N]` with `raw[:size].ljust(size)` (wsproto.py 284-285), so a 17-char name is sent with no NUL. 0x45 and 0x46 strcpy into unzeroed stack buffers. `_handle_create_character` accepts up to 17 name bytes (2590). Trade builders must pass `name[:16]`, and character creation should reject names over 16 chars.
- **B14. Traded items can be ids the EN client doesn't know (wrong_behavior).** Drops and quest items use KR gamedef ids (LIVE_TEST_LOG root cause: "client silently ignores item ids outside its catalog"). The server inventory can hold them, but they can't appear in the client bag or the 0x4B self path. Filter them against the EN catalog (3.5).
- **B15. One client's swings hit monsters in every session (wrong_behavior, multiplayer side effect).** `_memory_melee` (1678-1698) applies one client's swing to every session's monsters, and `_find_client_pid` only matches `windslayer_patched.exe` (1673). With two clients, client 1's swings hit client 2's private monster set too. Not trade logic, but it pollutes two-client tests in map 102. Run trade tests in map 101 (town, no monsters).
- **B16. Test injection hits every client (cosmetic/harness).** `_admin_listener` sends each injected packet to all live sessions (459-461). In a two-client test a `sendspec 4B` self echo reaches both clients (see 3.1 item 5).

---

## 5. Implementation plan

Order: codec -> identity/registry -> presence -> economy persistence -> trade flows -> hardening. Items marked (shared) should be built once and reused by the party, social and PvP designs.

| Id | Pri | Effort | Depends on | Work |
|---|---|---|---|---|
| `trade-route-fix-0x25` | P1 | S | - | Remove 0x25 from `_handle_attack` (lines 644-647). For now make 0x25 answer 0x49 to the sender so an injected trade window can close. Delete `_handle_attack`/`parse_group`. |
| `trade-codec` (shared) | P1 | S | - | Spec-driven codec: load `protocol_spec.json` once, `G(key) -> Grammar`, `_send_spec(session, key, **fields)` (opcode from spec, `use_by_array=session['no_enc']`, under `send_lock`), `_decode(key, payload)` (raises GrammarError -> log + ignore). Guards: `str[17]` values cut to 16 chars; option lists canonicalized and capped at 5. C2S keys: `0x4484CC/0x20`, `0x469C33/0x21`, `0x46C6B5/0x22`, `0x469D05/0x23`, `0x469D9C/0x24`, `0x469BA8/0x25`, `0x46C92C/0x26`, `0x44CA2F/0x61`. |
| `trade-mp-uid` (shared) | P1 | S | - | Account-level persistent `uid` (3.1 item 1); use it in 0x02, the local 0x07, 0x1D, 0x22, 0x08; refuse or kick duplicate logins. Keep test = 1 so `wsdev status` / the combat driver still work for client 1. |
| `trade-mp-registry` (shared) | P1 | M | `trade-mp-uid` | `sessions_by_uid`, `map_members`, `in_world`, `world_lock`, eager `send_lock`, per-session `no_enc`; unregister in `finally` (578-581). |
| `trade-mp-presence` (shared) | P1 | M | `trade-mp-registry`, `trade-codec` | 0x05 remote-record builder (0x05 grammar, real HP/MP, idle defaults, last pos); send on enter-world and after portal 0x07; 0x06 to old-map peers on portal and disconnect; 0x85 follow-up for open stalls. |
| `trade-economy-persist` (shared with item/shop groups) | P1 | L | `trade-mp-uid` | Tabbed inventory with 6-word blocks and gold in accounts.json (3.4); `_wallet` reads/writes char gold; 0x03 sends real gold + the three item lists; migrate the `_inv_*` helpers; EN catalog filter. |
| `trade-privacy-flags` (shared with social/party) | P2 | S | `trade-mp-registry` | Parse the 0x2B refuse bytes 0-4 into `session['refuse']`; add the C2S 0x40 handler (5 u8). |
| `trade-request-accept` | P2 | M | `trade-mp-presence`, `trade-codec` | C2S 0x20 -> 0x45 / 0x47(4/6); C2S 0x21 -> 0x46 x2; invite expiry (30 s) and spam guard (2.1-2.3). |
| `trade-offer` | P2 | M | `trade-request-accept`, `trade-economy-persist` | C2S 0x22 -> 0x4B x2 / 0x4D / silence; C2S 0x26 -> 0x4C to partner (content match + compacted index); lock policy Q4 (2.4-2.5). |
| `trade-lock-commit` | P2 | M | `trade-offer` | C2S 0x23 -> 0x48 x2 (gold check -> cancel); C2S 0x25 echo validation, final flags, atomic commit, persist, 0x4A x2 (2.6-2.7). |
| `trade-cancel-lifecycle` | P2 | M | `trade-request-accept` | C2S 0x24 -> 0x49 (both, or sender alone when there is no trade); server-initiated cancel on disconnect, portal (before 0x08), re-enter-world and validation failure (2.8-2.9). |
| `trade-escrow-guards` | P2 | S | `trade-offer` | Block sell/equip/use/quest-consume/buy-gold (and future drop/bank/stall) while `session.trade` is set, or use `available()` (3.6). |
| `trade-stall-visit` | P2 | S (stub) / M (full) | `trade-mp-registry`, `trade-codec`; full list needs the shop group's stall state (0x5E/0x82/0x85) | C2S 0x61 -> 0x87 `{0}` stub now; later `{6}` / `{1, list}` + `browsing_stall` (2.10). |
| `trade-harness` | P2 | S | `trade-mp-uid` | Admin injector `"uid"` target + `wsdev sendspec --to`; wsview `rclick` and `drag`; wsdev in-world detection by the session's uid (not hard-coded 1). |
| `trade-audit-log` | P3 | S | `trade-lock-commit` | Append-only `trade_log.jsonl` (both uids, offers, gold, before/after inventories) for dupe forensics; `/trades` admin dump. |
| `trade-move-relay` (world group) | P1 (world) | L | `trade-mp-presence` | C2S 0x0D -> S2C 0x2A/0x1B to map peers (re-encode; never echo to sender). Not required for trade, only for the partner ghost to follow its player. |

---

## 6. Live test plan

Rules (feedback_experiments.md): one change per run, capture before and after, prefer map 101 (town, no monsters, avoids B1/B15). `cap` = `python wsdev.py cap <secs> <wsview action>`. Coordinates come from `wsdev.py shot` (the positions of trade windows and buttons are not yet measured). Right-click and drag need the `trade-harness` wsview additions, or a human doing the input.

### 6.1 Phase A: one client, injection only (runnable on today's server)

| # | Opcode | Action | Expected visible result | Risk |
|---|---|---|---|---|
| A1 | S2C 0x47 | `wsdev.py sendspec 47 '{"result":6}'`, then `{"result":4}`, then `{"result":0}` | chat/system lines "Trading." and "The player is rejecting trade."; nothing for 0 | safe |
| A2 | S2C 0x4D | `wsdev.py sendspec 4D '{}'` | system line "There are no more spaces in the other player's item slot." | safe |
| A3 | S2C 0x45 | `wsdev.py sendspec 45 '{"requester_name":"Ghost"}'` (payload `47 68 6F 73 74 00...` 17B) | trade request popup 0x70 showing "Ghost" | safe |
| A4 | C2S 0x21 | after A3: `cap 3 click <OK>` | C2S 0x21 17B `47 68 6F 73 74 00 ...`; server logs Unhandled 0x21 | safe |
| A5 | S2C 0x46 | `wsdev.py sendspec 46 '{"partner_name":"Ghost"}'` | "Exchange" window 0x4E with "Ghost", both gold fields "0" | state_change (window stays open until A6/A7) |
| A6 | C2S 0x24 | with 0x4E open: `cap 3 click <Cancel>` | C2S 0x24 payload 0B; the window stays open with Cancel/X disabled (confirms the B2 hazard) | state_change |
| A7 | S2C 0x49 | `wsdev.py sendspec 49 '{}'` | "Trade has been canceled."; 0x4E and 0x291 hidden. Also send once with no window open: no effect, no crash | safe |
| A8 | C2S 0x22 (equip) + `+0x1F0` check | A5, then drag the Wooden Stick (179) from the bag onto 0x4E: `cap 3 drag <bag slot> <trade grid>` | C2S 0x22 7B `B3 00 01 00 00 00 00`. **If nothing is sent, catalog `+0x1F0` is non-zero for 179**, which means the server comment at line 2223 ("Kind at +0x1F0") would be right (Q5) | state_change |
| A9 | S2C 0x4B self | after A8: `wsdev.py sendspec 4B '{"item_id":179,"count":1,"owner_uid":1,"opt_count":0,"opt_extra":0}'` (11B `B3 00 01 00 01 00 00 00 00 00 00`) | stick leaves the bag and appears in the own trade grid; `wsview.py state`/shot | state_change (client bag diverges until A7 or relog) |
| A10 | C2S 0x26 | drag the stick from the own grid back to the bag: `cap 3 drag <grid> <bag>` | C2S 0x26 8B `B3 00 01 00 00 00 00 00` (slot_index 0); stick back in bag | state_change |
| A11 | S2C 0x4B partner + 0x4C | with 0x4E open: `sendspec 4B '{"item_id":179,"count":1,"owner_uid":2,"opt_count":0,"opt_extra":0}'`, then `sendspec 4C '{"slot_index":0}'` | stick appears on the partner side, then disappears | safe |
| A12 | C2S 0x23 + S2C 0x48 | type `0` in the gold box, `cap 3 click <OK>` -> C2S 0x23 8B `00*8`. Then `sendspec 48 '{"confirmer_uid":1,"gold":0}'` (self), then `sendspec 48 '{"confirmer_uid":2,"gold":500}'` (partner) | OK stays disabled, own items greyed; after the partner echo "500" appears and **Confirm Exchange 0x291 opens** | state_change |
| A13 | C2S 0x25 | in 0x291 (from A12, optionally with A11's partner item re-added before the partner 0x48): `cap 3 click <Trade>` **in map 101 only** | C2S 0x25, e.g. 18B `00*8 00 F4 01 00 00 00 00 00 00 00` (no items) or 25B with one partner item; today the server logs `[ATK] skill=0 targets=[...]` (B1 evidence) | state_change (in map 102 with item ids 0-7 it damages monsters) |
| A14 | S2C 0x4A | after A11 (partner stick present) + A12: `sendspec 4A '{"gold":123456}'` (8B `40 E2 01 00 00 00 00 00`) | "Trade completed.", gold label 123456, "you've received Wooden Stick. (Count:1)", windows close | state_change (client gold/bag differ from the server; relog) |
| A15 | C2S 0x20 via ghost | inject a remote ghost near the player (read pos from `wsview.py state`): `sendspec 05 '{"name":"Ghost","uid":2,"manner_points":0,"job1":1,"level":1,"rank_emblem":99,"repeat[14]":[{"appearance":0},{"appearance":123},{"appearance":1},{"appearance":0},{"appearance":2},{"appearance":2},{"appearance":2}],"stat_str":3,"stat_dex":2,"stat_int":1,"stat_tol":3,"attack_dir_8cf":8,"motion_id":8,"facing_dir":2,"pos_x":<px+80>,"pos_y":<py>,"input_dir_8b3":8,"cur_hp":100,"cur_mp":50}'` (367B), right-click the ghost -> popup 0x50 -> `cap 3 click <trade entry>` | "You requested <Ghost> to make an transaction." + C2S 0x20 4B `02 00 00 00`. Cleanup: `sendspec 06 '{"uid":2}'` (NEVER uid 1) | state_change |
| A16 | C2S 0x61 + S2C 0x87 | with the A15 ghost present: **raw** `wsdev.py send 85 <31-byte hex below>` (uid 2, sign 1, 25-byte title; `sendspec 85` encodes only 4B because the grammar branch depends on client state). Left-click the sign/body: `cap 3 click <sign>`. Then `sendspec 87 '{"result":0}'` | sign "Test stall" drawn (if sprite 1 exists, Q13); C2S 0x61 4B `02 00 00 00`; then the message box "The shop is closed or adjusting." | state_change (clear with `sendspec 86 '{"owner_uid":2}'`, then 06) |
| A17 | S2C 0x87 list | same setup: `sendspec 87 '{"result":1,"item_count":1,"owner_uid":2,"stall_name":"Test stall","repeat[item_count]":[{"item_id":179,"qty":1,"price":100,"opt_count":0,"item_ext":0}]}'` (42B) | stall window 0x259 opens in buyer mode listing the Wooden Stick at 100 | state_change (buying sends 0x62, which is unhandled) |

Exact byte strings for A16: title bytes = `54 65 73 74 20 73 74 61 6C 6C` + 15 x `00`. Full payload hex: `02 00 00 00 01 00 54 65 73 74 20 73 74 61 6C 6C 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00`.

### 6.2 Phase B: two clients, after `trade-mp-*`, `trade-economy-persist` and the trade items

Setup: `wsdev.py up` (client 1: test/test -> TestHero, uid 1) and `wsdev.py up --client 2` (admin/admin -> "test", uid 2); both in map 101 near the spawn. Give each a tradeable EN item (quest 26 -> Wooden Stick 179) and known gold (persisted).

| # | Opcode(s) | Trigger | Expected | Risk |
|---|---|---|---|---|
| B1 | 0x05 / 0x06 presence | log both in; `wsdev.py state` and `--client 2 state` | each shows the other as alive 3 with the correct uid; portal client 2 away -> gone from client 1 (0x06); back -> reappears | state_change |
| B2 | 0x20 -> 0x45 | client 1 right-clicks client 2 -> trade | server log `[TRADE] invite 1->2`; client 2 popup "TestHero" | safe |
| B3 | 0x21 -> 0x46 x2 | client 2 clicks OK | both show Exchange with the other's name | state_change |
| B4 | 0x47(6) | while B3 is open, client-2-alt (or re-login admin as a third char) requests client 1 | requester gets "Trading." | safe |
| B5 | 0x40 / 0x47(4) | client 2 Options -> refuse exchange -> Apply (capture C2S 0x40 5B `00 01 00 00 00`); client 1 requests | client 1 gets "The player is rejecting trade." | safe |
| B6 | 0x22 -> 0x4B x2 | client 1 drags the stick onto Exchange | stick leaves client 1's bag, appears in its own grid and on client 2's partner side | state_change |
| B7 | 0x22 stackable | client 1 drags a potion stack, enters 3 in dialog 0x74 | C2S 0x22 `{id,3,0,0}`; both sides show x3 | state_change |
| B8 | 0x4D | fill client 2's equipment tab to capacity, client 1 offers equipment | client 1 gets the 0x4D message; nothing moves | state_change |
| B9 | 0x26 -> 0x4C | client 1 drags the stick back | client 1 bag restored locally; client 2 partner entry removed; server offer list empty | state_change |
| B10 | 0x23 -> 0x48 x2 | client 1 gold 50 + OK; then client 2 gold 0 + OK | after the first lock client 2 sees "50"; after the second, both open Confirm Exchange with matching lists | state_change |
| B11 | 0x23 over gold | client with 100 gold types 100, then server gold is lowered via an admin edit | server cancels: 0x49 both + chat | state_change |
| B12 | 0x25 -> 0x4A x2 | both click Trade | "Trade completed." on both; gold = old - given + received; the stick moves; relog both -> the 0x03 lists match | disruptive (persists to accounts.json; back it up first) |
| B13 | 0x25 echo mismatch | after lock, client 1 edits its gold box before 0x291 opens (Q12), both click Trade | server logs the diff, 0x49 to both; items back in both bags | state_change |
| B14 | 0x24 -> 0x49 x2 | either side clicks Cancel at OPEN and again at CONFIRMING | both get "Trade has been canceled."; offered items return; server inventories unchanged | safe |
| B15 | disconnect | during OPEN with items offered, kill client 2 (`wsdev.py down --client 2`) | client 1 gets 0x49 + 0x06; no ghost remains | state_change |
| B16 | portal | during OPEN, client 1 walks into the portal | 0x49 to both BEFORE 0x08; client 1's items restored after the map load | state_change |
| B17 | escrow guard | during OPEN, client 1 sells the offered potion stack at an NPC shop | sell refused (or limited to the unescrowed qty); commit still works | state_change |
| B18 | 0x61 -> 0x87 | once the shop group is done: client 2 opens a stall; client 1 clicks the sign | stall list 0x259 opens; while client 2 edits (0x5F) -> result 0 message | state_change |

---

## 7. Open questions

1. **Decline/timeout notice.** The client has no decline packet for dialog 0x70. Did retail send the requester 0x47 result 4 on decline or timeout, or nothing? (Default here: nothing.)
2. **Other 0x47 codes** (0, 1, 2, 3, 5) are ignored by the EN client. Do they only matter for KR?
3. **Meaning of 0x4D.** Does "no more spaces in the other player's item slot" mean the partner's inventory capacity (the design choice here) or something about the partner's trade grid (which has no client cap)?
4. **Changes after a partner lock.** No unlock packet exists. Did retail allow 0x22/0x26 after the partner locked (then the final 0x291 review is the safeguard), or reject them? The design rejects 0x22 and cancels on 0x26.
5. **Item def `+0x1F0`.** Is it gamedef `Cash`, `NotTrade`, or (per the server comment at line 2223) `Kind`? If it is Kind, no equipment could be traded. Settled by test A8 (or a memory read of the catalog entry for id 179).
6. **Stack ceilings and tab capacity.** Do stacks merge or split past 999 (type 0) / 99 (type 2)? Is the tab capacity counted in distinct ids? This affects `can_receive` and 0x4D.
7. **Option block words.** What do words 0-4 and word 5 (`attr_0c`/`opt_extra`) mean (stones, reinforcement level, durability)? Record `+0x0E` is the count in trade lists.
8. **Map change and window 0x4E.** Does 0x08's `FUN_00483470` batch close 0x4E? The design sends 0x49 before 0x08 either way.
9. **Remote visibility and P2P UDP.** C2S 0x2B reports a P2P ip/port (UDP 42907/42908). Do remote players need P2P state to render or move, or is TCP 0x05 + 0x2A/0x1B enough? What should the server write into 0x05's `+0x8B3..+0x8B6` (the spec says input bytes; PySlayer/KR says IP)?
10. **Duplicate login.** Which 0x02 result code means "account already connected" for the EN client (refuse the new login vs. kick the old one)?
11. **Echo validation.** Did the original server compare the 0x25 echo, or only commit its own state? The design validates and cancels on mismatch.
12. **Gold edit box after lock.** Is ctrl 0x1E still editable after OK? If so, the 0x25 `my_gold` can differ from the locked 0x23 value, and B13 is expected behaviour.
13. **Valid `sign_sprite_id` values** for 0x85 (needed for A16 and the shop group); C2S 0x5E carries no sprite choice.
14. **EN item catalog.** An extracted list of valid EN item ids (from `hs/windslayer.hii`, cipher known) is needed to filter tradeable items (B14).
15. **Right-click vs double-click.** `FUN_0044c4b0` opens popup 0x50 on both 0x205 and 0x203. Confirm which one the harness should use (0x203 needs the window class to have CS_DBLCLKS).
