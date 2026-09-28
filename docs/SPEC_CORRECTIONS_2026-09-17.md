# Spec corrections applied 2026-09-17 (live verification C1-C52)

Source: `re_tools/live_verify/VERIFICATION_MATRIX.md` section 3 (52 de-duplicated corrections), with the evidence in
sections 2, 4 and 5 and the per-test detail in `re_tools/live_verify/results_*.json`.
Target: `re_tools/corpus/protocol_spec.json` (308 specs) -> rendered to `re_tools/docs/PROTOCOL.md`.

- Campaign totals: **343 unique tests, 249 confirmed, 28 partial, 5 contradicted, 1 not_observable, 59 blocked, 0 crashes**
  (381 result entries across 13 passes). Coverage: **S2C 152/165 opcodes exercised, C2S 84/106**.
- **129 spec entries changed**, 228 `spec_errata` entries appended (no existing errata removed).
- **Wire format: unchanged.** No correction proved a different byte layout. Exactly one grammar text changed (C23, a
  field rename); all 308 grammars still encode/decode round-trip against `server/wsproto.py`.
- Confidence: raised to `high` on 6 specs, lowered to `medium` on 3 (see the end of this document).

## C1-C52 change table

`Spec keys changed` uses the corpus key (`<VA>/<opcode>` for C2S, `<opcode>` for S2C).

| # | Opcode(s) | Correction (one line) | Spec keys changed | Grammar changed? |
|---|---|---|---|---|
| C1 | S2C 0x08, 0x2F (+0x03, 0x07, 0x2E) | 0x08 alone never completes a map change (client hangs on the loading overlay); the mandatory sequence is 0x08 -> 0x03 -> 0x07 -> 0x0A/0x28/0x44 -> 0x1A, and 0x2F likewise stalls until 0x2E | `0x08`, `0x2F`, `0x03`, `0x07`, `0x2E` | no |
| C2 | S2C 0x05 / 0x04 / 0x07 | CONTRADICTED: remote spawn records use `pos_y` verbatim - the entity hangs in mid-air, no gravity settle; send a floor-level y or follow with 0x1B/0x2A | `0x05`, `0x04`, `0x07` | no |
| C3 | S2C 0x9E, 0x9F, 0x1B, 0x2A | CONTRADICTED: 0x9E is not an immediate stop and 0x9F's 990 ms is not a maximum wait - both append behind pending nodes (measured 750 ms / 1.81 s / 1.08 s; 0x2A instant). `+0x8E4` is written at receipt. A type-3 entity simulates only while `+0xE48 > 0`, so 0x1B must be streamed | `0x9E`, `0x9F`, `0x1B`, `0x2A` | no |
| C4 | S2C 0x3B / 0x41 / 0x42 (+0x43 / 0x3C) | The client never expires a buff (counts `+0xC` into negative values); the server must send 0x43 or 0x3C at Duration end. Identical item buffs stack; wire `source_uid` 0 is stored as the target uid | `0x3B`, `0x41`, `0x42`, `0x43`, `0x3C` | no |
| C5 | S2C 0x1F | CONTRADICTED: the delete shifts everything after the first match **in list order** (head-inserted list = the lower slots, drawn left); the `uid = 30000000+index` / `x = 110*(index+1)` invariants break and a later 0x1C can collide | `0x1F`, `0x1C` | no |
| C6 | S2C 0x02 | A second success 0x02 at char select does not free the old entities - they are appended, `scene+0x14` increments and uids duplicate; never send one (use 0x1C) | `0x02` | no |
| C7 | C2S 0x12 / 0x51, S2C 0x80 | Character delete is a round trip the spec omitted: C2S 0x51 {password, 0x235} -> S2C 0x80 {1, 0x235} -> C2S 0x12 -> S2C 0x1F. Written into all four specs' `expected_response` | `0x44ABA4/0x12`, `0x460831/0x51`, `0x80`, `0x1F` | no |
| C8 | S2C 0x83, 0x82 (+0x84, 0x87) | Client-side item duplication: `0x83 result==1` / `0x82 result!=1` latch `ctx+0x1C` in any mode, and a later 0x87 window close returns its listed items into the bag (5 -> 15). Not "inert outside mode 0x259" | `0x83`, `0x82`, `0x84`, `0x87` | no |
| C9 | S2C 0x89 | The handler matches whatever window 0x259 still holds, including a stale buyer list; send 0x89 only to the real selling owner | `0x89` | no |
| C10 | S2C 0x1E | Block mismatch is a three-way desync: bag duplicate + grid/Equipment keep the item + sprite recompose drops the visual | `0x1E` | no |
| C11 | S2C 0x54 / 0x55 (+0x4F) | `max = 0` is not a no-op: it zeroes the receiver's copy and blanks the overhead bar; the "never send max 0" rule extends from 0x4F to 0x54/0x55 | `0x54`, `0x55`, `0x4F` | no |
| C12 | C2S 0x0D | CONTRADICTED: no ~28-packet stream from an IP-octet seed (local state resets from input; expect <= 5 idle sends). A basic swing is `+0x8B4 = 1` with `+0x904 = 4`, `+0x94C` stays 0, so the 22-byte `event_source_uid` form is never produced by a plain attack. Remote records still need idle defaults | `0x42CE94/0x0D`, `0x04`, `0x05`, `0x07` | no |
| C13 | S2C 0x6F / C2S 0x4A | The stat-reset allowance is the cash record's `u16 quantity` at `+0x08`, not the item def's `Cash_V`; send quantity=5 for a "Reset 5 Stat Points" record and validate `sum(removed) <= quantity` | `0x6F`, `0x460C50/0x4A` | no |
| C14 | S2C 0x77 | `cash_item_serial = 0` shows the success modal but consumes nothing - always echo the real serial | `0x77` | no |
| C15 | S2C 0x76 | Free points are drawn as `total(level) - sum(stats)` with no clamp (observed -7); the server must keep `sum(stats) <= total(level)` | `0x76` | no |
| C16 | S2C 0x29 | (a) The self-respawn branch is for **type-4 template** entities (the local player is type 3), clears the whole buff table incl. the 0xB3B render entry and resets HP to the template max. (b) For the local player it never fires, even with `respawn_tick=0` - the player stays dead | `0x29` | no |
| C17 | `portals.json` (data) + C2S 0x7E | EN portal indices: 101->102 = 23, 102->101 = 31, 102->103 = **17** (portals.json only has 102_14); map 201 has an unlisted event portal to 9701 at (2600,1600). Recorded in the 0x7E spec (the data file is outside this corpus) | `0x42F76B/0x7E` | no |
| C18 | C2S 0x7E | Input made during the ~1.5 s map load is not dropped - a second press fires another 0x7E right after the 2.5 s cooldown (3.06 s apart); the server must ignore 0x7E within ~3 s of a transfer | `0x42F76B/0x7E` | no |
| C19 | S2C 0x11 / 0x12 | The map change clears the client ground list - re-send 0x11 on map entry; container `scene+0x18 = {vtable 0x6F7F3C, head, tail, u32 count@+0x24}` | `0x11`, `0x12` | no |
| C20 | S2C 0x6D / 0x78 | The map change resets the Gift and Msg HUD buttons; send `count = 0` on each resync to relight them without duplicating queue entries | `0x6D`, `0x78` | no |
| C21 | S2C 0x27 | Double-send re-grants items, gold and the completion count; an already-learned type-3 skill reward prints nothing the second time | `0x27` | no |
| C22 | S2C 0x8A / 0x8B | `[itemtable+0x24]` is **880** in EN (items with `CardNpc != 0`), not 64; label = `n / 880`, `floor(n*100/880)%`; before any 0x8A/0x8B the template `000/000 (000%)` shows | `0x8A`, `0x8B` | no |
| C23 | C2S 0x18 | Field 3 is the arena **Time** dropdown (round minutes, 3 or 5; play rooms 1) - renamed `room_option` -> `round_time_min`; `max_users` clamps 2..20; EN map/caption names recorded | `0x449384/0x18` | **yes - field rename only** (`u8 room_option` -> `u8 round_time_min`; same byte, offset and width, **no wire-format change**) |
| C24 | C2S 0x22 / catalog `+0x1F0` | Q5 settled: the client sent 0x22 for ordinary equipment, so `+0x1F0 == 0` there - `+0x1F0` is **not** gamedef `Kind`, it is the cash / no-trade flag | `0x469D9C/0x22`, `0x46C6B5/0x22`, `0x1D`, `0x1E`, `0x469BA8/0x3D`, `0x469D9C/0x3D` | no |
| C25 | C2S 0x25 | The server-side attack misparse reads past `my_item_count` and yields one bogus target per offered item on **both** sides | `0x469BA8/0x25` | no |
| C26 | S2C 0x3A | Self-exit at **~170 s**, not 180 + 5 s; the terminal modal was never captured | `0x3A` | no |
| C27 | S2C 0x25 (id 0x88C) | `FUN_004687f0` is distance-gated on a smithy ("Too far for Reinforcement."); the cast lock `scene+0x255 = 7` self-clears within 3 s, so no unlock packet is needed for it | `0x25` | no |
| C28 | C2S 0x6C | The REACHABILITY HAZARD is wrong - the field-mode local player is state 3 and 0x6C fires. Trap lives ~7 s (`+0xC` from Con 7020 ms). Unexplained victim gate on `+0xDF0`/action. C2S 0x0D interact-0xC tail confirmed at 45 B | `0x417DB0/0x6C` | no |
| C29 | S2C 0x1B | Monster motion codes 0/1/3/5/6 mapped (walk 82.5 px/s); with `server_controlled = 1` physics stops when the hold expires, even mid-air | `0x1B` | no |
| C30 | C2S 0x1D | Dialog 0x4B has three controls: "Leave Now" (= 0x04, usually disabled), "Declare" (-> 0x03) and "Cancel" (sends nothing); 0x01 is unreachable from a blue slot | `0x44840E/0x1D`, `0x4484CC/0x1D` | no |
| C31 | S2C 0x87 | `result == 6` = "Open stall is opened..."; `result != 1` (and `count == 0`) = "The shop is closed or adjusting." | `0x87` | no |
| C32 | S2C 0x80 / 0x65 | Q15: any 0x65 **before** `0x80 {1, 0x1A7}` is enough; a post-open 0x65 is optional (it resets the tab) | `0x65`, `0x80` | no |
| C33 | S2C 0x1D / 0x24 | 0x1D itself removes one bag copy (root cause of the double-removal bug); equip-tab removal compacts; EN Kind 11 (item 179) -> equip grid index 5 | `0x1D`, `0x24` | no |
| C34 | S2C 0x16 | Bubbles/emotes attach to **any** scene player entity with that name, the local player included; `text_len` excludes the NUL | `0x16` | no |
| C35 | C2S 0x02 | Whisper with no stored target sends a zeroed name but `msg_len` **and** the message are present (not msg_len 0) | `0x445ADD/0x02` | no |
| C36 | S2C 0x72 | The window-5 period list prints the absolute expiry SYSTEMTIME ("Nmonth Dday HH:MM"), not remaining days; item 3323 `+0x1F6 == 2 == Cash_T` so the 26-byte form is right | `0x72` | no |
| C37 | S2C 0x75 | The "Enter the cash shop?" prompt is reachable only for a non-local entity | `0x75` | no |
| C38 | S2C 0x02 `transfer_status` | Hiding window-0x12 controls 5/6/7 is **sticky**; statuses 1/2 only set the ctrl-7 text. The ID-transfer UI depends on the session's *first* 0x02 (untested - open question added) | `0x02` | no |
| C39 | S2C 0x5E (+0xA3/0xA4) | The popup sits under the loading overlay; the world is torn down with no local player and the client hangs until 0x03/0x07 arrive | `0x5E`, `0xA3`, `0xA4` | no |
| C40 | S2C 0x5B | A 0x5B for the receiver's own uid does draw the balloon over the local player - a deliberate server choice | `0x5B` | no |
| C41 | `wsproto` codec | `str[<length field>]` must not force a NUL into the last byte (fixed `str[N]` only). Recorded in the affected specs and in `meta.live_verification.tooling_followups`; the grammars themselves were already correct | `0x16`, `0x445BD9/0x6A` (+ meta) | no |
| C42 | `wsproto` codec | Result-branch encoder bugs: 0x0C success = 24 B, 0x7A result 0x64 = 22 B, 0x96 = 25 B - all three long forms were accepted live, so the grammars are right | `0x0C`, `0x7A`, `0x96` (+ meta) | no |
| C43 | `protocol_spec.json` gap | C2S 0x32 FriendDelete was missing from the matrix's snapshot; it is **present and correct** in this corpus (`u32 friend_id; str[17] friend_name` = 21 B) and is now marked live-confirmed | `0x470018/0x32` | no |
| C44 | `tests_social_friend.json` | The 0x9D hex was one byte short - the success form is 10 B (`u8 result + u8 capacity + u64 gold`), live-confirmed | `0x9D` | no |
| C45 | `tests_premium_cash.json` | The 0x74 action needs `"__assume__": {"entity_with_uid_exists": true}`; the server must never send the short 4-byte form (the client reads out of bounds) | `0x74` | no |
| C46 | Test plans (chat samples) | The client-side Curse_Engine matches substrings ("hell" in "hello"), shows "No cursing please" and sends **nothing** - an unmodified client never delivers a blocked word | `0x460F58/0x4C`, `0x445BD9/0x6A`, `0x445ADD/0x02`, `0x445CA7/0x03` | no |
| C47 | C2S 0x64 | The waiting box has an OK button - no soft-lock; the trigger for case 0x65 is **dragging** a card from the bag onto the Card Deck window (window 8, hotkey B) | `0x45A4F8/0x64` | no |
| C48 | S2C 0x04 / 0x85 | `shop_open = 1` draws the title but no signboard: ids 0, 1 and 0x168 drew nothing on the vendor path, yet id 1 rendered on a 0x05 ghost - unresolved conflict (0x85 lowered to medium); an all-zero appearance block yields an invisible body | `0x85`, `0x04`, `0x05` | no |
| C49 | Notice vs chat-log wording | 0x50, 0x56, 0x53, 0x19, 0x89, 0x18 and C2S 0x27's line render in the floating notice overlay; 0x94 and every 0x0C failure are modal popups | `0x50`, `0x56`, `0x53`, `0x19`, `0x89`, `0x18`, `0x4484CC/0x27`, `0x94`, `0x0C`, `0x445BD9/0x6A` | no |
| C50 | EN window captions | 0x4E = "TRADE", 0x291 = "Comfirming Trade" [sic], 0x259 = "Pedding" [sic], window 0x70/0x71 ctrl 1 = caption, friend ctrl 7 = "Converse", tab 0x1A = "Mento", bank Storage/Deposit/Search/Withdrawal mapping | `0x46`, `0x48`, `0x87`, `0x45`, `0x4E`, `0x0B`, `0x469D9C/0x24`, `0x469D9C/0x3C`, `0x469BA8/0x3D`, `0x469D9C/0x3D`, `0x469D9C/0x3E`, `0x469D9C/0x3F` | no |
| C51 | S2C 0x4F / party model | Only the two gauges render (no `%u/%u` text); 0x4F does not recompute `scene+0x96D`; the cap is 4 remote members (`scene+0x26C` unreachable); 0x51 does not compact the frame array | `0x4F`, `0x51` | no |
| C52 | Answered open questions | 0x0F subtype 5 omits `target_name` and subtype 1 includes the receiver; 0x60 presence icon map; window 0x176 opens from HUD "Avatar" -> menu 0x1CE; popup 0x50 entry order (right-click alone); window 0x71 "Refuse" and 0x4B "Cancel" send nothing; 0x97 `manner_delta` is a signed delta | `0x0F`, `0x60`, `0x4E`, `0x4484CC/0x28`, `0x4484CC/0x20`, `0x4484CC/0x27`, `0x4484CC/0x2A`, `0x473701/0x6D`, `0x97` | no |

### Additional pass: section 4 termination / soft-lock classes (errata id `S4`)

Section 4 of the matrix lists 12 termination or soft-lock classes. Each one's hazard **and its recovery** is now in the
relevant spec's `gates_and_hazards` (and, where a reply clears it, in `expected_response`):

| Class | Where documented |
|---|---|
| 0x3A maintenance self-exit (~170 s, restart) | `0x3A` (C26) |
| 0x5D forced disconnect (process gone in 8 s, restart) | `0x5D` |
| 0x17 disconnect suppression (latch never clears; restart) | `0x17` |
| 0x08 alone = loading hang (send 0x03/0x07) | `0x08` (C1) |
| 0x5E / 0xA3 / 0xA4 world torn down, no player | `0x5E`, `0xA3`, `0xA4` (C39) |
| 0x29 player permanently dead (server must revive) | `0x29` (C16) |
| 0x3E death window with no exit (server must answer C2S 0x2E) | `0x3E`, `0x4484CC/0x2E` |
| 0x6A cash mall with no return path (server must push the world back) | `0x6A` |
| C2S 0x24 unclosable TRADE window (inject/send S2C 0x49) | `0x469D9C/0x24` |
| "Waiting for the server to response." soft-locks (11 C2S opcodes) | `0x18`, `0x1A` (x2), `0x1B` (x2), `0x1C`, `0x39`, `0x3A`, `0x48`, `0x49`, `0x4A`, `0x4B` (x2), `0x64`, plus the must-reply note on `0x34`, `0x20`, `0x72`, `0x73`, `0x76`, `0x77`, `0x8B` |
| Client-side item duplication (relog) | `0x83`, `0x82`, `0x84`, `0x87` (C8) |
| Unexplained graceful exit after 0x26 + Q | `0x26` |

Also recorded under `S4`: the empty S2C 0x63 corrupting the Battlefield label (`0x63`), 0x58 not redrawing the class
label (`0x58`), 0x22 not refreshing the HUD LV label (`0x22`), and 0x95's u32 being the absolute post-fee balance (`0x95`).

## Implications for the server, by roadmap phase

Phases follow `re_tools/docs/IMPLEMENTATION_ROADMAP.md` section 3.3.

### P0 - Stop the bleeding
- **C41, C42** invalidate the codec work items as written: fix `str[<length field>]` (no forced NUL) and the
  0x0C / 0x7A / 0x96 result-branch encoders before any packet built from those grammars is trusted. The *specs* were
  right; the codec was wrong.
- **C45** the `sendspec` test actions for 0x74 (and any packet with a client-state condition) need `__assume__`;
  a short 0x74 must never leave the server.
- **C8 + C9** turn the stall stubs into a *rule*: only answer C2S 0x5F with 0x83 and C2S 0x5E with 0x82, and only send
  0x89 to the real owner. The planned "0x83/0x82 are inert, send them freely" behaviour is an item-duplication bug.
- **C47** removes the planned soft-lock fix for C2S 0x64 (the box has OK); the must-reply list still needs the other 10
  waiting-box opcodes (see the S4 table) - each already has its reply named in `expected_response`.
- **C25** sharpens the 0x25 misroute: the trade-confirm packet produces one bogus attack target per offered item on both
  sides, so the dispatch fix is a security fix, not a cosmetic one.
- **C49 + C50** change the acceptance criteria of the notice/label work: these strings appear in the floating notice
  overlay (not the chat log) and the EN captions are TRADE / "Comfirming Trade" / "Pedding" / "Mento" / "Converse".
- **C34** means the chat echo plays emotes on the sender's own character - the planned "echo 0x16 back to the sender"
  behaviour is correct, but its side effect is now documented.

### P1 - Account and character truth
- **C6** invalidates "re-send 0x02 to refresh the character list": a second success 0x02 at char select duplicates uids.
  Use 0x1C for a create, and only send 0x02 on a fresh login.
- **C5 + C7** rewrite the delete flow: the client's post-delete list no longer satisfies `uid = 30000000+index`, and the
  delete needs the C2S 0x51 -> S2C 0x80{1,0x235} -> C2S 0x12 round trip (`lc-delete` must implement the 0x51 step or
  deletion is impossible).
- **C38** makes `lc-id-transfer-stub` conditional: the ID-transfer controls are only reachable if the session's *first*
  0x02 carries `transfer_status` 1/2 - still untested, so treat C2S 0x65/0x66 as unreachable for now.
- **C12a** keeps `lc-spawn-fields` / `world-player-record` honest: idle defaults matter for remote records, not for local
  packet volume; a post-spawn burst of ~5 idle 0x0D is normal.

### P2 - Persistent world state, inventory and quest loop
- **C1** is the phase's biggest constraint: `world-maptransfer` must emit 0x08 -> 0x03 -> 0x07 (+HP/MP + spawns) as one
  unit. Any code path that sends a bare 0x08 (teleports, village transfer, dungeon entry) hangs the client.
- **C17** blocks map reachability until `portals.json` gets the EN rows (102_17 -> 103 especially); **C18** adds a
  server-side ~3 s rate limit on C2S 0x7E, otherwise players bounce back through the arrival portal.
- **C19** adds a required step to map entry: re-send ground items with 0x11 (the client clears its list).
- **C33 + C10** change the equip/unequip plan: 0x1D already removes one bag copy, so the follow-up 0x23 must go
  (`item_inventory-no-double-remove`), and a mismatched option block on 0x1E now has three visible symptoms.
- **C24** invalidates the `Kind at +0x1F0` comment used for slot mapping; use `+0x1CC` (kind) for the grid slot and treat
  `+0x1F0` as the cash/no-trade flag. **C33** fixes the one known mapping: EN Kind 11 -> grid index 5.
- **C21** confirms the quest-grant mirror must not double-send 0x27, and **C20** means the Gift/Msg buttons need a
  `count = 0` refresh after every transfer.

### P3 - Combat, skills and world content
- **C4** adds a required server behaviour: a buff expiry scheduler. The client never expires anything, so `cs-buffs`
  must send 0x43 (stat recompute) or 0x3C (icon only) at Duration end, and must expect stacking.
- **C16** invalidates "0x29 with respawn_tick 0 revives the player": `cs-player-death` must revive explicitly
  (HP + map/spawn sequence); a 0x29 to the local player is a permanent death.
- **C3 + C29** rewrite the movement relay: stream 0x1B (one node per C2S 0x0D, `hold = logic_elapsed_ms`), use 0x2A for
  stops and keyframes, and keep sending monster commands before the hold expires, otherwise the mob freezes mid-air.
- **C2** forces the spawn code to send floor-level y values (or a follow-up 0x1B/0x2A) for every remote record.
- **C27** removes the planned P1 "cast unlock" item for `scene+0x255` (it self-clears); only the pending-skill flag
  `scene+0x258` needs a reply.
- **C28** re-enables C2S 0x6C as a real inbound packet for `cs-traps` (it is not unreachable), with an unresolved
  victim-eligibility rule to re-derive.

### P4 - Economy extensions
- **C8/C9** (stalls) and **C31** (0x87 result codes) define the stall replies; **C50** fixes the bank button mapping
  (Storage = item deposit, Deposit = gold deposit, Search = item withdraw, Withdrawal = gold withdraw).
- **C32** simplifies the bank open: any 0x65 before `0x80 {1, 0x1A7}` is enough; no post-open resync is required.

### P5-P7 - Multiplayer, communication, exchange
- **C11** extends the "never send max = 0" rule to 0x54/0x55 in `party-vitals-sync`.
- **C51** caps the party HUD at 4 remote members, removes the `%u/%u` text from any test expectation and moves the
  average-level recompute to player registration.
- **C52** removes any reliance on a decline packet: window 0x71 "Refuse" and window 0x4B "Cancel" send nothing, so
  invites need a server-side TTL.
- **C43/C44** confirm the friend packets byte-for-byte (C2S 0x32 = 21 B, S2C 0x9D success = 10 B).
- **C25** (trade confirm) and **C24** (no-trade flag) belong to `trade-route-fix-0x25` and the trade validation rules.

### P8 - Premium cash / Item Mall
- **C13** changes the 0x6F record builder: `quantity` is the stat-reset allowance, so a "Reset 5" item must ship
  `quantity = 5`, and C2S 0x4A must be validated against it (not against `Cash_V`).
- **C15** adds a server invariant when answering 0x4A: `sum(stats) <= total(level)` or the client paints negative points.
- **C14** makes the real serial mandatory in 0x77 (serial 0 consumes nothing while claiming success).
- **C36** replaces "remaining days" with an absolute expiry SYSTEMTIME in the 0x72 period form.
- **C37** limits the 0x75 prompt to non-local entities (no behaviour change for the broadcast).
- Section 4: entering the mall with 0x6A has **no client-side exit**, so the mall flow must end with a server-driven
  map restore.

### P9-P10 - PvP rooms and UDP
- **C1** applies again: 0x2F must be followed by 0x2E or the arena hangs at ~99%.
- **C23** changes the room-create parser: field 3 is `round_time_min` (3 or 5; play rooms 1) and `max_users` is 2..20,
  not 2..16 - and the value is echoed back in S2C 0x2F's `scene+0xF37`.
- **C30** fixes the arena exit dialog handling (0x04 = "Leave Now", "Cancel" sends nothing, 0x01 unreachable from a
  blue slot).
- **C40** makes the room-owner balloon a deliberate decision (own-uid 0x5B does render).
- The five dead C2S opcodes (0x93 0x94 0x95 0x97 0x99) were confirmed absent from the EN client - no host-side C2S
  handling is needed for them.

### P11 - Hardening
- **C26** (~170 s to self-exit) and the 0x5D / 0x17 notes define the shutdown and kick behaviour the server can rely on.
- **C46** means server-side profanity filtering never sees a blocked word from an unmodified client - treat any such
  text as a modified client.
- **C22** fixes the card-dictionary total (880) used by any card-collection feature.
- **C39** (0x5E/0xA3/0xA4) must reuse the same MapTransfer primitive as 0x08.

## Unresolved after this pass (open questions added)

| Spec | Question | New confidence |
|---|---|---|
| S2C 0x29 | Why the local-player revive gate did not fire with `respawn_tick = 0` (unsigned compare? extra gate?) | medium |
| S2C 0x85 | Which sign-sprite ids are valid, and why id 1 rendered on the 0x05 path but not on the 0x04 vendor path | medium |
| C2S 0x6C | The `+0xDF0` / action victim-eligibility condition | medium |
| S2C 0x02 | Whether a *first* 0x02 with `transfer_status` 1/2 exposes window-0x12 controls 5-7 | high (wire format unaffected) |
| C2S 0x0D | Which action produces the 22-byte `event_source_uid` tail (not a basic attack) | high (wire format unaffected) |
| S2C 0x77 | Whether `cash_item_serial == 0` is a deliberate "no item" sentinel | high |
| S2C 0x3A | The literal behind the measured ~170 s self-exit | high |

Raised to `high` on live confirmation: S2C 0x3E, S2C 0x92, C2S 0x2E, C2S 0x28, C2S 0x3A, C2S 0x72.

## Validation

```
python -c "import json; d=json.load(open('corpus/protocol_spec.json', encoding='utf-8')); print(len(d['specs']))"
-> 308

Grammar(s['grammar']).decode(Grammar(s['grammar']).encode({})) for all specs  (server/wsproto.py, imported read-only)
-> round-trip ok: 308   fail: 0

python gen_protocol_doc.py
-> wrote re_tools/docs/PROTOCOL.md (1169 KB, 308 specs, 24 systems)
```
