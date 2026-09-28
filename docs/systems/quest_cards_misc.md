# quest_cards_misc: system design (EN 2008 client)

Group scope: `quest`, `card_deck`, `system_misc`, `unknown`. 19 specs from `re_tools/corpus/systems/quest_cards_misc.json`:

- **C2S (8):** 0x05 KeepAlive, 0x16 QuestAcceptRequest, 0x17 QuestCompleteRequest, 0x1E QuestAbandonRequest, 0x40 SetPrivacyRefuseOptions, 0x63 CardDeckListRequest, 0x64 CardDeckRegisterRequest, 0x7C Window4A4Confirm (identified below as **DungeonExitConfirm**).
- **S2C (11):** 0x17 SuppressDisconnectNotice, 0x26 QuestAccepted, 0x27 QuestCompleted, 0x38 QuestAbandoned, 0x3A ServerMaintenanceWarning, 0x59 QuestProgressUpdate, 0x5D ForcedDisconnect, 0x8A CardDeckList, 0x8B CardDeckRegisterResult, 0x99 SystemNotice_ChannelOrItemGrant, 0xA7 ScreenSpriteFlash.

Sources, in order of authority: client binary (spec + Ghidra corpus `re_tools/corpus/decomp|asm`), `LIVE_TEST_LOG.md`, `WindSlayer2Game/server/windslayer_server.py` (line numbers below are from the current 2793-line file), memory notes, PySlayer. PySlayer has almost nothing for this group (a no-op `AcceptQuest`, KR-only 0x99 sub 0x13/0x14 builders, and import stubs for 0x8A/0x8B).

### New evidence gathered for this doc (not in the spec)

- **E1. EN quest table.** `WindSlayer2Game/hs/windslayer.hqi` decodes with the asset cipher (`plain[i] = (raw[i] + [0xE9,0xDE,0xE0][i%3]) & 0xFF`, then drop the 20-byte SHA-1 footer) to text: `Number_of_Quest: 291`. The loader `FUN_00409FE0` parses the id arrays with `sscanf("%04d")` and the count arrays with `sscanf("%03d")`, so they are **decimal**. Reward is **20 ids x 4 digits** (loop to 0x50) and **20 counts x 3 digits**. The KR `gamedef.sqlite3` `quests` rows hold the same strings (42 of 291 shared rows differ in content). EN quest 26 decodes to Send `179 x1` (Wooden Stick), Reward `5 x5` (Herb) + `94 x1` (Double Jump, type-3 skill), Exp 10, Money 0, **ReqPro 1, NPC 1** (kill 1 Pupu), SNPC = ENPC = 75, Lv 1..20, Repeat 0.
- **E2. Client offer filter `FUN_004781C0`** (builds the "Available" list; called on NPC click, on 0x26 and on quest-window refresh). A quest is offered only if all of these hold:
  - SNPC (`+0xE08`) != 0;
  - the quest is not in an active slot;
  - `FUN_00477FF0(id, Repeat)` is false. It returns "done" when the id is in the completed list AND (Repeat == 0 OR times >= Repeat);
  - `Start_Lev <= level(entity+0x99) <= End_Lev`;
  - the Job flag `record[+0xC40 + (class*3 + tier)*4] != 0` (class = entity+0x110, tier = entity+0x111). In the hqi `Job:` string (21 chars) that is **char `class + 7*tier`**;
  - PrevQuest (`+0xE00`) is 0, or it appears in the completed list.
- **E3. UI data.** `hs/windslayer.hui` (same cipher) plus the plaintext `hs/UILngKo.lng` resolve every window this group touches:

  | window | caption | relevant controls |
  |---|---|---|
  | 6 | Quest | tab 23 Available / 24 Accepted / 25 Completed; ctrl 7 "Abandon" (Link 41) |
  | 0x29 (41) | Abandon Quest | ctrl 2 OK, ctrl 3 Cancel |
  | 0x236 (566) | Quest Expansion (NPC quest list) | ctrl 1..5 quest rows |
  | 8 | Card Deck | tabs Monster/NPC/Item, 30 slots, ctrl 41 "000/000 (000%)" |
  | 0x292 (658) | Card Registration | ctrl 2 OK, ctrl 3 Cancel. Text 8952: "Once the card is registered to the Card Deck, it cannot be returned back to your inventory. Are you sure...?" |
  | 0x4A3 (1187) | **Dungeon Exit** (one-button HUD panel at 664,205) | ctrl 1 "Dungeon Exit" |
  | 0x4A4 (1188) | **Finishing Dungeon** (confirm) | ctrl 2 OK, ctrl 3 Cancel |

  Quest dialog chains are data-driven. For quest 26: offer window 169 "Next" -> 170 **"Yes" (Event 2)** / "No". **Across all 291 quests, Event-2 buttons occur only in offer (First) chains.** Every NPC-offered quest has one, and no Ing/Finish/None chain (followed through `Link`) contains one. So the spec hazard "the completion dialog could emit a stray 0x16" cannot happen with the shipped UI data.
- **E4. Card items.** In `hs/windslayer.hii`, `CardSpr != 0` holds for exactly **64 items**, all Type 2 "Monster Card <...>": 2030-2064, 3106-3114, 3125, 3289-3295, 3421-3422, 3457-3461, 3989-3993. `CardNpc` = monster npccode (2030 Pupu -> 1, 2031 Blue Pupu -> 2, ...). This is the `+0x14C != 0` card flag and the `itemtable+0x24` total (inferred: it is the only column whose non-zero set is exactly those 64 items). The card tooltip `FUN_00485F40` always ends with "Card effect: EXP + 10%".
- **E5. Client-side kill credit exists (P2P path).** `FUN_0041A230` @0x41A593 increments a progress byte when a monster dies, if quest `ReqPro(+0xDF8) != 0` and quest `NPC(+0xDFC) == dead entity npccode(+0xE60)`. This proves the hqi `NPC` field is the **monster npccode** to kill. That path works on an entity mirror (`entity+0x114/+0x11A`) that no known packet fills, so under server-driven combat the server must send 0x59.
- **E6. Codec defect.** The spec grammar for S2C 0x59 is `u8 slot / if((int)slot - 1 < 3) { u8 progress } else { stop }`. `wsproto._eval` sees `int` as an unknown name, so `Grammar.encode({"slot":1,"progress":3})` returns `01` (verified). As a result `wsdev.py sendspec 59` sends a 1-byte packet. Use `wsdev.py send 59 <slot> <progress>` until the grammar is changed to `if(slot < 4)`.

---

## 1. Overview: how the client implements these systems

### 1.1 Client memory map (scene = `*0x70EECC`, char = scene+0x48 = `[quest_mgr+0x18]`)

| data | char off | scene off | written by |
|---|---|---|---|
| active quest ids u16[3] (slot 1..3) | +0x256 | +0x29E | S2C 0x03, 0x26 (first empty), 0x27/0x38 (zero) |
| active progress u8[3] | +0x25C | +0x2A4 | 0x03, 0x59 (absolute), 0x27/0x38 (zero). **0x26 does NOT reset it** |
| ready flags u8[3] | +0x25F | +0x2A7 | 0x59 (set once), 0x26 (clear), 0x03 (zeroed, not sent) |
| completed ids u16[85] | +0x262 | +0x2AA | 0x03; 0x26 (talk-only quest) and 0x27 via `FUN_004264A0` |
| completed times u8[85] | +0x30C | +0x354 | same (cap 99; a full list shifts left and appends) |
| gold u64 | +0x228 | +0x270 | 0x03, 0x18 (absolute), 0x3F (absolute), 0x27 (adds Money locally) |
| card deck count u8 | +0xE30 | +0xE78 | **0x03 `unk_e78` byte**, 0x8A, 0x8B |
| card deck ids u16[] | +0xE32 | +0xE7A | 0x8A, 0x8B. Only 51 fit before the i32 at +0xE98 (manner points) |
| disconnect latch | game_state+0x1528 = `0x70FF28` | | 0x17, 0x5D (never cleared) |
| refuse flags (sent in 0x2B) | game_state +0x500 whisper, +0x504 exchange, +0x508 party, +0x510 talk, +0x50C friend | | registry `HKCU\Software\Hamelin\WindSlayer\<char>` |

The S2C 0x03 spec calls scene+0xE78 `unk_e78`. It is the card deck count (0x8A writes charinfo+0xE30 = scene+0xE78).

### 1.2 Quest record (hqi line -> client record)

`<idx> <title_text> Send: 10x%04d Send_Num: 10x%03d Reward: 20x%04d Reward_Num: 20x%03d Demand: 10x%04d Demand_Num: 10x%03d First Ing Finish None Job(21 chars) Exp Money Start_Lev End_Lev Repeat Explain ReqPro NPC PrevQuest Intro Completed SNPC ENPC NextQuest IndunMapName`

Record offsets: +0xC40 job flags[21], +0xC94 Send[10], +0xCBC Reward[20], +0xD0C Demand[10], +0xD34 Send_Num, +0xD5C Reward_Num, +0xDAC Demand_Num, +0xDD4 First/+0xDD8 Ing/+0xDDC Finish/+0xDE0 None (dialog window ids), +0xDE4 Exp, +0xDE8 Money (signed), +0xDEC Start_Lev, +0xDF0 End_Lev, +0xDF4 Repeat, +0xDF8 ReqPro, +0xDFC NPC, +0xE00 PrevQuest, +0xE04 NextQuest, +0xE08 SNPC, +0xE0C ENPC.

EN content statistics (computed from the decoded hqi and `server/en_item_catalog.json`):
- 291 quests. 88 use **ReqPro kill counts** (NPC = npccode). 7 have **Money -1** (courier quests, SNPC != ENPC). **9 are talk-only** (Money >= 0, no Demand, ReqPro 0: ids 1, 2, 65, 103, 110, 126, 222, 243, 283). 152 are repeatable (Repeat 1/3/5/10/99). 45 have SNPC 0 (never offered by an NPC). 80 have SNPC != ENPC.
- Item refs: Demand items are type 2 (243) or type 0 (18). Reward entries: 197 consumable, 31 etc, **18 class change (type 4)**, **8 skill (type 3)**, 7 equipment. All item refs exist in the EN catalog when read as decimal.
- IndunMapName is `None` for all 291.

### 1.3 Quest logic in the client (authoritative bookkeeping is client-side)

- **Offer (client-local).** Hold Space on an NPC -> `FUN_004447D0` (gate: roster +0x638 != 0) -> `FUN_00477AF0`.
  1. It first scans the 3 slots for a quest whose ENPC == this NPC and whose requirements are met. If one is found it **sends C2S 0x17** and opens the Finish dialog.
  2. Otherwise it opens the in-progress (Ing) dialog if a held quest's SNPC matches.
  3. It rebuilds the Available list (E2) and shows window 0x236 with this NPC's offers.
  4. Clicking an offer opens its First dialog chain. "Yes" (Event 2) -> `FUN_00477210` -> **C2S 0x16** after these client checks: 3-slot limit ("You can only take 3 quests at a time..."), and bag space for the Send items via `FUN_00426040` ("There isn't empty space in the inventory.").
- **S2C 0x26.** The client looks up the record.
  - If `Money >= 0 && Demand[0] == 0 && ReqPro == 0` (talk-only), it appends to the **completed list** and uses no slot.
  - Otherwise it writes the first empty slot and clears that slot's ready flag.
  - In both cases the client **grants the Send items itself** (`FUN_00440920`, prints "you've received %s. (Count:%u)") and refreshes window 6.
- **S2C 0x59 (slot 1..3, progress).**
  1. Writes the progress byte.
  2. If the ready flag is 0, `FUN_00426500` checks: gold >= |Money| when Money < 0; progress >= ReqPro; Demand items in the bag.
  3. On success it sets the flag and prints green "[<name>] You are ready to complete the quest." (once).
- **S2C 0x27.** The client clears matching slots and their progress and appends/increments the completed list. It then adds Money to gold ("Gave %u Gold." / "You've received %u Gold."), removes the Demand items ("Gave %u item(%s)."), grants the Reward items (skill type 3 is learned, class change type 4 is applied) and refreshes. **Exp is not applied; the server must send S2C 0x21.** The packet is not tied to held state, so every 0x27 re-applies the rewards.
- **Abandon.** Q -> Accepted tab (24) -> select row -> "Abandon" (ctrl 7) -> window 0x29 "Abandon Quest" -> OK (ctrl 2) -> **C2S 0x1E**. Refused locally in play room/arena. **S2C 0x38** zeroes the matching slots and their progress (the ready flag is not cleared). It gives no message and does not return the Send items.

### 1.4 Card Deck (Monster Card collection)

- On S2C 0x02 success the client initialises the card object (`FUN_0045A130`, app+0xDC4).
- Every S2C 0x03 ends by sending **C2S 0x63** (0 B). The expected reply is **S2C 0x8A** (the whole list), and the window-8 label becomes `count / 64 (pct%)`.
- **Registering a card:**
  1. Bag action `FUN_00469090` command 0x65 on a selected bag cell -> `FUN_0045A3C0`.
  2. The item must be Type 2 with the card flag, else "This item cannot be registered.". It must not be in the local list, else "This card is already registered...".
  3. Window 0x292 "Card Registration" opens. OK (ctrl 2) -> **C2S 0x64** u16 id -> the client shows the modal "Waiting for the server to response.".
  4. **S2C 0x8B** closes the modal. result 1 removes one card from the bag client-side and appends it to the deck ("Registered %s in the Card Deck."). 2 = "Failed to receive dictionary information. Please log in again.". 6 = "Already registered.". Anything else = "Failure to register card. Please try again in a few minutes.".

### 1.5 System misc

- **C2S 0x05 KeepAlive.** 0 B every 240 s (timer 6, re-armed by each S2C 0x03). No reply.
- **Privacy flags.** The 5 refuse bytes are the first 5 bytes of C2S 0x2B and are re-sent as **C2S 0x40** when the Options window (0x1AD) OK changes one. Fire-and-forget. The server must enforce them in other groups.
- **S2C 0x5D ForcedDisconnect** (u8 reason). Shows a modal (1 unusual play, 2 input delay, 3 macro, 99 transfer failed, other = generic) and the client closes itself 5 s later. Sets the latch.
- **S2C 0x17.** Sets the latch only. From then on the client ignores socket close, Fireway error 0x83 and later 0x5D (no dialog, no self-close) for the rest of the process.
- **S2C 0x3A.** Prints "[Announcement] Server will be down in 3 minutes for the maintenance." (red chat plus green center notice). After 180 s a modal appears and ~185 s after the packet the client closes itself. There is no cancel.
- **S2C 0x99.** Sub 8 prints "In channel %u.". Sub 9 is a client-side item add (u16 id, u16 count) that does not touch gold. Other subs are no-ops.
- **S2C 0xA7.** One out-of-frame DrawSprite(0x144). Nothing observable is expected.
- **C2S 0x7C (DungeonExitConfirm).** Window 0x4A3 "Dungeon Exit" ctrl 1 opens 0x4A4 "Finishing Dungeon". OK (ctrl 2) sends 0x7C (0 B). No client wait or modal. The server must move the player out of the dungeon (world group; see `world_movement_npc.md` F10, where the dungeon C2S was listed as unknown).

### 1.6 Opcode status

| key | dir | name | spec status | real status (this review) |
|---|---|---|---|---|
| 0x43E8AB/0x05 | C2S | KeepAlive | implemented | consumed OK (632-635); no idle reaper |
| 0x47734D/0x16 | C2S | QuestAcceptRequest | implemented | **partial/buggy** (Q-B1..B6) |
| 0x477F3E/0x17 | C2S | QuestCompleteRequest | missing | missing (falls to "Unhandled", 659) |
| 0x4774E7/0x1E | C2S | QuestAbandonRequest | missing | missing |
| 0x4414BA/0x40 | C2S | SetPrivacyRefuseOptions | missing | missing |
| 0x44EF67/0x63 | C2S | CardDeckListRequest | partial | **wrong reply** (empty S2C 0x63, 610-611) |
| 0x45A4F8/0x64 | C2S | CardDeckRegisterRequest | missing | missing (client soft-locks on the modal) |
| 0x4484CC/0x7C | C2S | Window4A4Confirm = DungeonExitConfirm | missing | missing (unreachable in EN content today) |
| 0x17 | S2C | SuppressDisconnectNotice | missing | missing (reserve) |
| 0x26 | S2C | QuestAccepted | implemented | wire OK (1400-1403); used wrongly |
| 0x27 | S2C | QuestCompleted | partial | wire OK (1413-1416); followed by duplicate grants |
| 0x38 | S2C | QuestAbandoned | missing | missing |
| 0x3A | S2C | ServerMaintenanceWarning | missing | missing |
| 0x59 | S2C | QuestProgressUpdate | implemented | wire OK (1405-1411); fed item counts, never kills |
| 0x5D | S2C | ForcedDisconnect | missing | missing |
| 0x8A | S2C | CardDeckList | missing | missing |
| 0x8B | S2C | CardDeckRegisterResult | missing | missing |
| 0x99 | S2C | SystemNotice_ChannelOrItemGrant | missing | missing |
| 0xA7 | S2C | ScreenSpriteFlash | missing | do not implement |

Cross-group packets used here: S2C 0x03 (world: quest/card fields), 0x21 ExpDelta and 0x22 SetLevel (character), 0x3F GoldUpdate (item), 0x15 SystemMessage (chat, for refusal text), 0x09/0x0C/0x0F/0x47 refusal replies (chat/social/trade).

---

## 2. Request/response flows

Conventions: `->` C2S, `<-` S2C to self. "Mirror" means update the server model exactly as the client updates itself, **without sending a packet**. "Refuse" means no quest packet. Optionally send S2C 0x15 `{msg_type 0, text <= 88 B}` to explain. Every quest mutation ends with `save_character()`.

### F1. Accept a quest that uses a slot (C2S 0x16 -> S2C 0x26 [+ 0x59])
1. Client gates, all client-side: offer filter E2, 3-slot limit, bag space for Send items. The dialog and window 0x236 close whether or not the packet is sent.
2. `-> 0x16 {quest_id}` (e.g. `1A 00`).
3. Server validation, in order. On any failure refuse:
   1. `q = QuestCatalog[quest_id]` exists and `q.snpc != 0`;
   2. quest_id is not in `char.quests.active`;
   3. not exhausted: `times = completed.get(id)`; refuse if `times and (q.repeat == 0 or times >= q.repeat)`;
   4. `q.start_lev <= level <= q.end_lev` (level = `session['level']`);
   5. `q.job[class + 7*tier] == '1'`;
   6. `q.prev_q == 0 or q.prev_q in completed`;
   7. slot quest (`q.money < 0 or q.demand or q.reqpro`): at least one `active[i] == 0`. Otherwise the client drops 0x26 silently, so never send it;
   8. bag model can take every Send item (item_inventory add rules).
4. Mirror: `i = first index with active[i]==0`, `active[i] = id`, `progress[i] = 0`, add Send items to the bag model (type 3 -> learned skills, type 4 -> class change; see F4 step 6).
5. `<- 0x26 {quest_id}`.
6. `<- 0x59 {slot: i+1, progress: 0}`. This clears any stale progress byte (0x26 does not reset it) and triggers the readiness check if the Demand items are already in the bag.
7. Do **not** send 0x18 for Send items (the client grants them) and do not send the "Quest N accepted." chat (1375).

### F2. Accept a talk-only quest (Money >= 0, no Demand, ReqPro 0)
1. Same as F1 steps 1-3, skipping 3.7.
2. Mirror: add Send items to the bag. Append/increment the completed list with the `FUN_004264A0` rules: existing id -> times+1 (cap 99); else first free entry with times 1; if all 85 are used, shift left and put the id last.
3. `<- 0x26 {quest_id}`. The client files it straight under Completed and grants the Send items.
4. If `q.exp != 0`: `<- 0x21 {exp_delta: q.exp}` via `_send_exp` (plus 0x22 if the level changes). For example quest 1 +5, quest 126 +2500, quest 283 +6000. The client applies no exp for these on its own.
5. If `q.money > 0` or `q.reward` is non-empty (none in EN data): the client does not grant them on 0x26. Log a content warning. Do not send 0x27, which would increment the completion count a second time.

### F3. Kill progress (server-driven kill -> S2C 0x59)
1. In `_kill_monster` (1911), after the 0x29/0x21 sends, for `i in 0..2`: `q = active[i]`. If `q.reqpro > 0 and q.npc == mob.npccode and progress[i] < q.reqpro`, then `progress[i] += 1`, `<- 0x59 {slot: i+1, progress: progress[i]}`, and **break** (credit one slot per kill, same as `FUN_0041A230`).
2. Client: progress/ReqPro updates in the Accepted tab. When ReqPro is reached (and Demand items and gold are satisfied) it prints the green "ready" line once.
3. Party-shared credit: not defined by the client (open question Q5). Credit only the killer for now.

### F4. Item-demand readiness (bag change -> S2C 0x59 re-evaluation)
1. Whenever the bag model gains an item that is in `q.demand` of an active quest (pickup, drop-to-bag fallback, trade, mail), `<- 0x59 {slot, progress: progress[i]}` with the **unchanged** progress value.
2. Never put item counts into the progress byte (Q-B10).

### F5. Turn in (C2S 0x17 -> S2C 0x27 + 0x21 [+ 0x3F])
1. Client gates: NPC click where `ENPC == npc`; gold >= |Money| if Money < 0; progress >= ReqPro; Demand items in the bag; reward space (`FUN_00426270`). Otherwise the Ing dialog opens and nothing is sent. After sending, the client opens the Finish dialog immediately, before the reply.
2. `-> 0x17 {quest_id}`.
3. Server validation. On any failure refuse (no 0x27); the player then sees the Finish text but no rewards:
   1. `i` with `active[i] == quest_id`. Otherwise ignore silently. This also makes duplicate 0x17 idempotent;
   2. `q.money >= 0 or gold >= -q.money`;
   3. `q.reqpro == 0 or progress[i] >= q.reqpro`;
   4. every `(id, n)` in `q.demand` is in the bag model (type 1 = equipped/held item per `FUN_00424DF0`, open question Q12);
   5. the bag model can take every Reward item;
   6. optional anti-cheat: the player's current map contains NPC `q.enpc` (needs NPC placement from the `.hmi`, Q4).
4. Mirror, in the client's order:
   1. `active[i] = 0; progress[i] = 0`;
   2. completed-list append/increment (F2 step 2 rules);
   3. `gold += q.money` (signed);
   4. remove Demand items from the bag model;
   5. add Reward items to the bag model;
   6. type-3 rewards -> `char.skills` add; type-4 rewards -> `char['class']/['tier']` update (character group).
5. `<- 0x27 {quest_id}`.
6. If `q.exp`: `<- 0x21 {q.exp}` via `_send_exp`.
7. If `q.money != 0`: optionally `<- 0x3F {gold}` (absolute, silent) to force agreement if the wallets ever diverge (see Q-B20).
8. Do **not** send 0x18 for rewards or 0x23/0x19 for Demand items (the client already did both).

### F6. Abandon (C2S 0x1E -> S2C 0x38)
1. Client gates: window 0x29 ctrl 2; not in play room/arena (`FUN_00444700`); the selected row is from the Accepted tab (row+8 flag).
2. `-> 0x1E {quest_id}`. Can arrive twice per click (no event-type check): tolerate duplicates.
3. Server: `i` with `active[i] == quest_id`, else ignore. Mirror `active[i] = 0; progress[i] = 0`. Send items stay in the bag.
4. `<- 0x38 {quest_id}`.

### F7. World entry / map change (S2C 0x03 quest + card fields, then 0x8A and 0x59)
1. The world group builds 0x03 (`world_movement_npc.md` `world-03-real-state`). This group supplies:
   - `active_quest_id[3]` and `active_quest_progress[3]` from `char.quests`;
   - `completed_quest_count <= 85` plus `(id, times)` from `char.quests.completed`, in list order;
   - `unk_e78` = `len(char.card_deck)`.
2. The client zeroes the ready flags and sends `-> 0x2F`, then `-> 0x63` (0 B).
3. On `-> 0x63`:
   1. `<- 0x8A {deck_count, repeat[deck_count]: [{card_item_id}]}` (F9);
   2. then, for every non-empty slot, `<- 0x59 {slot, progress}` to re-arm the ready flags. Doing this on 0x63 guarantees it lands after 0x03/0x07.
4. Result: portals and relogs keep the quest log (today they wipe it, Q-B8).

### F8. Server-forced quest removal (S2C 0x38 unsolicited)
- For GM reset, or when a persisted active id is no longer in the EN catalog: clear the slot in the model and `<- 0x38 {quest_id}`. At login, simply omit such ids from 0x03.

### F9. Card deck list (C2S 0x63 -> S2C 0x8A)
1. `-> 0x63` (0 B). Precondition on the client: an earlier S2C 0x02 success, so the card object exists.
2. `<- 0x8A {deck_count: n, ids...}` with `n <= 51`, ids in registration order and never 0.
3. **Never reply with S2C 0x63.** That opcode is BattlefieldQueueCounts `{u16 player_count, u8 join_count}` and belongs to pvp_arena.

### F10. Register a card (C2S 0x64 -> S2C 0x8B)
1. Client gates: Type 2 + card flag; not already in the local list; window 0x292 OK. Then the blocking modal "Waiting for the server to response.". The server **must always answer**.
2. `-> 0x64 {card_item_id}` (e.g. Pupu card `EE 07`).
3. Server:
   1. deck not loaded (should not happen) -> `<- 0x8B {result: 2}`;
   2. `card_item_id` not in `CardCatalog` (hii Type 2 + CardSpr != 0) -> `<- 0x8B {result: 3}`;
   3. already in `char.card_deck` -> `<- 0x8B {result: 6}`;
   4. `len(deck) >= 51` -> `<- 0x8B {result: 3}` (capacity guard, Q1);
   5. bag model lacks the card -> `<- 0x8B {result: 3}`. Also schedule a bag resync, owned by item_inventory. If the client lacks the card while the server sends 1, the client silently ignores the 0x8B and desyncs;
   6. success: remove 1 card from the bag model (no packet), append to the deck, save, `<- 0x8B {result: 1, new_deck_count: len(deck), card_item_id}`.
4. Client on success: removes one card from the bag, stores the id at index count-1, prints "Registered Monster Card <Pupu> in the Card Deck.", refreshes the label.
5. Do not send 0x23/0x19 for the card and do not re-send 0x8A.

### F11. Card EXP bonus (design proposal, flag-gated)
- In `_kill_monster`, before `_send_exp`: if some `card in char.card_deck` has `CardCatalog[card].card_npc == mob.npccode`, then `exp = floor(exp * 1.10)`. Source: the unconditional tooltip "Card effect: EXP + 10%" (E4). Not proven server behaviour (Q2). Keep it behind `CARD_EXP_BONUS = True`.

### F12. Privacy refuse flags (C2S 0x2B bytes 0-4, C2S 0x40)
1. `-> 0x2B`: decode with the spec grammar. Store `session['refuse'] = {whisper, exchange, party, talk, friend}` (each 0/1).
2. `-> 0x40 {refuse_whisper, refuse_exchange, refuse_party, refuse_talk, refuse_friend}` (5 B, wire order whisper, exchange, party, talk, friend): overwrite `session['refuse']` and persist (optional; the client re-sends at every 0x2B). No reply.
3. Enforcement API for other groups: `self.refuses(target_session, kind) -> bool`. When true:

   | request (C2S) | refusal reply to requester |
   |---|---|
   | whisper 0x02 | S2C 0x09 `{status: 0x67, target_name}` ("is rejecting whispers") |
   | trade 0x20 | S2C 0x47 `{result: 4}` ("The player is rejecting trade.") |
   | messenger chat invite 0x33 (talk) | S2C 0x0F `{subtype: 3, target_name}` ("has refused to have a personal chat.") |
   | friend add 0x30 | S2C 0x0C `{result: 3, target_name}` ("has refused to register you as a friend.") |
   | party invite 0x27 | no dedicated refusal packet found (Q9). Drop the invite silently |

### F13. KeepAlive and idle reaping (C2S 0x05)
1. `-> 0x05` (0 B) every 240 s. No reply. Replying breaks the client.
2. Server: on **any** C2S packet set `session['last_rx'] = monotonic()`.
3. Reaper (the recv loop's timeout branch, currently `continue` at 527-529): if `monotonic() - last_rx > 300`, save the character, log, close the socket. Nothing is sent to the client, which is already dead or silent.

### F14. Kick (S2C 0x5D) and silent close (S2C 0x17)
1. Admin `kick <char> <reason>` (extend `_admin_listener` 428-469 with a JSON `{"cmd": "kick", ...}`), or an anti-cheat trigger.
2. `<- 0x5D {reason}` (1/2/3/99/other). Save the character. Close the socket after ~1 s. The client shows the dialog, suppresses the close notice (latch set) and exits after 5 s.
3. S2C 0x17: send **only** right before a close where the client is expected to move on by itself (a future server-transfer flow). After 0x17 plus a close, the client sits silently with a dead socket. Do not use it for kicks.

### F15. Maintenance shutdown (S2C 0x3A)
1. Admin `shutdown`: set `self.login_locked = True` (login group answers new logins with the maintenance failure).
2. Broadcast `<- 0x3A` (0 B) to every in-world session.
3. At T+175 s save all characters. At T+180 s close all sockets (clients exit on their own at ~185 s). Do not send 0x3A unless committed, because there is no cancel.

### F16. System notices (S2C 0x99)
- Sub 8: after the enter-world welcome, optionally `<- 0x99 {sub_type: 8, channel_no}` -> "In channel 1.".
- Sub 9 (GM give / event reward that must not touch gold): mirror the add in the bag model with the client rules (consumable stack 999, etc stack 99, equip 1 per packet and only if equip capacity <= 45), then `<- 0x99 {sub_type: 9, item_id, count}`. Skip if the client add would fail, since it gives no ack.

### F17. Dungeon exit (C2S 0x7C)
1. `-> 0x7C` (0 B) from window 0x4A4 OK.
2. Now: log `[DUNGEON] exit confirm` and ignore. EN has no instance-dungeon quest content, and the entry request is still unknown.
3. Later (world group `world-instance-dungeon`): if `session['instance']` is set, move the player to `instance.return_map/x/y` with the map-change sequence (0x08, 0x03, 0x07, ...) and clear the instance. Otherwise ignore.

### F18. S2C 0xA7
- Not used. It has no payload and no state effect.

---

## 3. Server state and data model

### 3.1 Content (read-only), new module `server/en_content.py`
- `decode_hs(path) -> bytes`: add `[0xE9,0xDE,0xE0][i%3]` mod 256, drop the last 20 bytes. Shared with item_inventory `I-01`.
- `QuestCatalog` from `WindSlayer2Game/hs/windslayer.hqi`. Parse the header `Number_of_Quest: N`, then one CRLF line per quest as `idx title (Key: value)*`. Fields:
  ```
  idx, title_text,
  send   = [(id,n)]  from Send 10x4-digit decimal + Send_Num 10x3-digit decimal (drop id 0)
  reward = [(id,n)]  from Reward 20x4 + Reward_Num 20x3
  demand = [(id,n)]  from Demand 10x4 + Demand_Num 10x3
  job    = 21-char str (flag for class c, tier t = job[c + 7*t] == '1')
  exp, money (signed), start_lev, end_lev, repeat, reqpro, npc, prev_q, next_q, snpc, enpc
  dialogs = (first, ing, finish, none)   # informational
  needs_slot = money < 0 or bool(demand) or reqpro != 0
  ```
  This replaces `quest_defs.load_quests` (hex parsing) and the `_EN_ITEM_OVERRIDE`/`_en_item` hack (75-83). The gamedef `quests` table is still useful for KR `Title_Str` only.
- `CardCatalog` from `hs/windslayer.hii`, parsed by key (Job/Job2 are multi-token): `{id: {'card_spr', 'card_npc'}}` for Type 2 and CardSpr != 0. 64 entries; `TOTAL_CARDS = 64`.
- `ItemCatalog` (item_inventory `I-01`): type per id, for Send/Reward/Demand handling.
- Constants: `MAX_ACTIVE_QUESTS = 3` (exists at 178), `MAX_COMPLETED = 85`, `MAX_COMPLETED_TIMES = 99`, `MAX_DECK = 51`, `KEEPALIVE_TIMEOUT_S = 300`, `MAINTENANCE_LEAD_S = 180`.

### 3.2 Per-character persistent state (`accounts.json` character dict, same store as item_inventory 3.2 / world 3.x)
```
char['quests'] = {
  'active':    [26, 0, 0],            # u16 x3, index = client slot-1, first-empty rule
  'progress':  [0, 0, 0],             # u8 x3, ReqPro kill counter per slot
  'completed': [[1, 1], [26, 1]],     # ordered [id, times<=99], len <= 85, FIFO shift on overflow
}
char['card_deck'] = [2030, 2031]       # ordered, len <= 51, unique, CardCatalog ids
char['refuse']    = {'whisper': 0, 'exchange': 0, 'party': 0, 'talk': 0, 'friend': 0}
char['skills']    = [...]              # combat_skill group; quest type-3 rewards append
char['class'], char['tier']            # character group; quest type-4 rewards update
char['wallet'], char['bag']            # item_inventory group; quest mirror mutates these
```
Invariants: an id is never both active and exhausted-completed. `active[i] == 0` implies `progress[i] == 0`. Every S2C 0x26/0x27/0x38/0x59/0x8B goes out only after the model mutation succeeded. Replaces the session-only keys `quests`, `quests_done`, `quest_progress`, `quest_slot` (1349-1351, 1390).

### 3.3 Session (volatile)
- `session['char']`: the resolved character dict (avoid re-resolving through `_session_char` on every packet).
- `session['level']` (exists, `_send_exp` 2000), `session['refuse']` (copy of `char['refuse']`, refreshed by 0x2B/0x40).
- `session['last_rx']` (F13), `session['in_world']`.
- Server-wide: `self.login_locked`, `self.shutdown_at` (F15).

### 3.4 Packet construction
Build and parse from the spec grammars (`wsproto.Grammar`), loaded once from `re_tools/corpus/protocol_spec.json`:

| opcode | record keys |
|---|---|
| C2S 0x16/0x17/0x1E | `quest_id` |
| C2S 0x40 | `refuse_whisper, refuse_exchange, refuse_party, refuse_talk, refuse_friend` |
| C2S 0x64 | `card_item_id` |
| S2C 0x26/0x27/0x38 | `quest_id` (`1a 00`) |
| S2C 0x59 | `slot, progress`. **Encode by hand or fix the grammar first (E6)** |
| S2C 0x5D | `reason` |
| S2C 0x8A | `deck_count`, `repeat[deck_count]: [{card_item_id}]` (2 cards 2030, 2031 = `02 ee 07 ef 07`) |
| S2C 0x8B | `result` [, `new_deck_count`, `card_item_id`] (`01 01 ee 07` / `06`) |
| S2C 0x99 | `sub_type` [, `channel_no` \| `item_id, count`] (`09 b3 00 01 00`) |
| S2C 0x17/0x3A/0xA7 | empty |

---

## 4. Current implementation status and proven bugs

| id | severity | opcode | defect | evidence | fix |
|---|---|---|---|---|---|
| Q-B1 | wrong_behavior | (content for 0x16/0x17/0x26/0x27/0x59) | `quest_defs.py` parses ids and counts as **hex** (`_ids` 14-16, `_num3` 18-20) and Reward as 10 ids with 6-hex counts (`_num6` 22-24). The client uses decimal and 20 reward entries. **242 of 291** EN quests parse differently (quest 26: Send 377 instead of 179; the Double Jump reward is lost). | E1 (`FUN_00409FE0` `%04d`/`%03d`, reward loop 0x50); decoded hqi; same finding as item_inventory B3 | `QuestCatalog` decimal loader (3.1); delete `_EN_ITEM_OVERRIDE` (80) and `_en_item` (82-83) |
| Q-B2 | wrong_behavior | S2C 0x26 + 0x18 | Accept sends 0x18 per Send item (1362-1372). The client already grants them on 0x26, so the item is duplicated. | LIVE_TEST_LOG: "you've received Wooden Stick" printed twice, 2 sticks in bag; spec S2C 0x26 hazard (2) | Mirror only (F1 step 7) |
| Q-B3 | crash_or_desync | S2C 0x26/0x59 | `_quest_assign_slot` (1389-1398) gives **every** accepted quest a slot, including the 9 talk-only quests the client files as completed. Server slot numbers then drift from the client's first-empty rule, and later 0x59 packets write the wrong slot's progress. | spec S2C 0x26 semantics (a)/(b) and hazard (4); code 1358 | `needs_slot` branch (F1/F2); slots = index of `char.quests.active` |
| Q-B4 | wrong_behavior | C2S 0x16 | Repeatable quests can never be re-accepted: the accept branch requires `quest_id not in done` (1353) and the fall-through sends nothing. 152 EN quests have Repeat > 0. | E2 (`FUN_00477FF0` allows times < Repeat); code 1353/1378 | F1 step 3.3 |
| Q-B5 | crash_or_desync | C2S 0x16 | A 0x16 for an **active** quest is treated as TURN-IN (1378-1386). `_quest_demand_met` returns True for an empty demand list (1418-1420). **Exploit:** accept quest 26, take a portal (0x03 wipes the client log, Q-B8), re-accept at Murubisiri. The server "completes" the quest and the client applies Herb x5 plus the Double Jump skill without the kill. | spec C2S 0x17 (the real turn-in); E3 (only the offer Yes sends 0x16); code 1378-1386 | Remove the turn-in branch; 0x16 for a held or exhausted quest = refuse |
| Q-B6 | wrong_behavior | C2S 0x16 | No server-side validation of SNPC != 0, level range, Job flag, PrevQuest or Repeat (1353-1376). Only the client filter applies. | E2 | F1 step 3 |
| Q-B7 | wrong_behavior | C2S 0x17 | No handler; falls to "Unhandled opcode" (659-660). No quest can ever be turned in with a real client. | spec 0x477F3E/0x17 | F5 |
| Q-B8 | crash_or_desync | S2C 0x03 (quest/card fields) | `_build_pyslayer_opcode_03` always sends empty active slots and progress (2064-2067), `completed_quest_count = 0` (2076) and card count 0 (2060, mislabelled "+0xe78 flag"). It is re-sent on every portal (1298-1300), so the client log is wiped while `session['quests']` keeps the quest. Then re-accept is possible (Q-B5) and 0x59 targets empty slots. | spec S2C 0x03 fields; world doc B5 / T-7E-3 | F7 (world `world-03-real-state` + this group's serializer) |
| Q-B9 | wrong_behavior | S2C 0x59 | Kill progress is never tracked: `_kill_monster` (1911-1928) has no ReqPro/NPC credit. 88 quests, including the starter quest 26 (kill 1 Pupu, npccode 1), can never become ready. The client turn-in gate `progress >= ReqPro` blocks 0x17. | E1, E5; spec C2S 0x17 gates | F3 |
| Q-B10 | wrong_behavior | S2C 0x59 | `_quest_credit_item` (1443-1456) writes the **collected item count** into the progress byte (1455-1456). Progress is the ReqPro kill counter: wrong display, and the 5 quests with both ReqPro and Demand get corrupted counters. | spec S2C 0x59 field `progress`; `FUN_00426500` | F4 (re-send the unchanged progress) |
| Q-B11 | wrong_behavior | S2C 0x27 + 0x18 | `_complete_quest` sends 0x18 per reward item plus gold (1427-1434). The client grants Reward items and Money itself on 0x27, so rewards are duplicated (and gold is overwritten with the server's differing absolute value). | spec S2C 0x27 hazards; item_inventory B2 | F5 steps 4-8 |
| Q-B12 | wrong_behavior | C2S 0x1E / S2C 0x38 | No abandon handler and no 0x38 builder. The Abandon -> OK click does nothing. | spec 0x4774E7/0x1E, 0x38 | F6 |
| Q-B13 | wrong_behavior | C2S 0x63 / S2C 0x63 | `_dispatch` answers C2S 0x63 with an **empty S2C 0x63** (610-611) on every map load: Fireway short-read errors and garbage Battlefield labels. The deck list 0x8A is never sent. | LIVE_TEST_LOG "Mismatches"; spec S2C 0x63; world B13 | F9 |
| Q-B14 | wrong_behavior | C2S 0x64 / S2C 0x8B | No handler: after confirming Card Registration the client is stuck behind the modal "Waiting for the server to response.". | spec 0x45A4F8/0x64 hazards | F10 |
| Q-B15 | wrong_behavior | (persistence) | Quest state lives only in the per-connection session dict (509-517, 1349-1351). Relog loses the whole quest history. The deck does not exist. | code; world B16 | 3.2 + save on every mutation |
| Q-B16 | cosmetic | C2S 0x2B / 0x40 | Refuse flags are ignored. The `_handle_enter_world` docstring (686-699) says bytes [0-4] are zeros and [21-22] a magic; they are the 5 refuse flags and a u32 P2P port. C2S 0x40 is unhandled. | spec 0x42F904/0x2B, 0x4414BA/0x40 | F12 |
| Q-B17 | cosmetic | C2S 0x05 | Dead or idle connections are never reaped: the recv timeout does `continue` (527-529). | code; spec 0x05 gates | F13 |
| Q-B18 | cosmetic (tooling) | S2C 0x59 | The spec grammar's `(int)` cast makes `wsproto` encode only `slot`. `wsdev.py sendspec 59 ...` sends 1 byte (short read, progress not written). | E6 (encode `{slot:1,progress:3}` -> `01`) | Change the grammar to `if(slot < 4)` or teach `_eval` to strip C casts |
| Q-B19 | cosmetic | S2C 0x16 chat | Accept/turn-in/refusal feedback is sent as chat lines from "Server" (1347, 1356, 1375, 1383-1384, 1440-1441): noise in the chat log next to the client's own messages. | code | Drop them; use S2C 0x15 for refusals |
| Q-B20 | wrong_behavior (cross-group) | S2C 0x03/0x18 | The gold baseline disagrees: 0x03 sends gold 100000 and exp_total 30000 (2044-2045), but `_wallet` defaults to 999999 (994-1000) and `session['exp']` starts at 0. This breaks Money < 0 quest checks and makes every later 0x18 jump the client's gold. | code; world B5 | One wallet (item_inventory/world) |
| Q-B21 | cosmetic (spec) | S2C 0x03 | The spec field `unk_e78` is the card deck count (scene+0xE78 = charinfo+0xE30). The server comment (2060) and the memory note `feedback_experiments.md` ("Echo 0x63") are stale. | spec 0x8A `deck_count` dest | Relabel; update the note |

What already works and should be kept: the C2S 0x05 consume (632-635); the 2-byte wire formats of `_send_quest_grant` (1400-1403), `_send_quest_complete` (1413-1416) and `_send_quest_progress` (1405-1411, slot 1..3 never 0); `_send_exp` for quest exp (1435-1436); the demand-item removal from the bag model on turn-in (1424-1425).

---

## 5. Implementation plan

| id | title | opcodes | priority | effort | depends on | MP | UDP |
|---|---|---|---|---|---|---|---|
| quest_cards_misc-en-quest-card-catalog | `en_content.py`: decode hqi/hii; decimal QuestCatalog; CardCatalog (64); remove `quest_defs` hex parse and `_EN_ITEM_OVERRIDE` | 0x16 0x17 0x26 0x27 0x59 0x64 0x8B | P0 | M | item_inventory I-01 (shared `decode_hs`/ItemCatalog) | no | no |
| quest_cards_misc-stop-0x63-echo | Replace the empty S2C 0x63 reply with S2C 0x8A (empty deck `00` until the deck exists) | 0x63 0x8A | P0 | S | — | no | no |
| quest_cards_misc-quest-state-model | `char['quests']` active/progress/completed with `FUN_004264A0` rules, helpers `first_free_slot`, `complete_append`, save on mutation | — | P0 | M | world `world-persistence` (store), can start session-backed | no | no |
| quest_cards_misc-accept-rework | 0x16: full validation (F1.3), needs_slot vs talk-only (F2), 0x26 + 0x59(slot,0), 0x21 for talk-only, bag mirror without 0x18, delete the turn-in branch | 0x16 0x26 0x59 0x21 | P0 | M | en-quest-card-catalog, quest-state-model, item_inventory I-03 | no | no |
| quest_cards_misc-turnin-0x17 | New 0x17 handler: validation, mirror (gold/demand/reward/skill/class), 0x27 + 0x21 (+0x3F), no 0x18 | 0x17 0x27 0x21 0x3F | P0 | M | accept-rework | no | no |
| quest_cards_misc-kill-progress | `_kill_monster`: ReqPro/NPC credit -> 0x59; `_quest_credit_item` re-sends unchanged progress | 0x59 | P0 | S | accept-rework | no | no |
| quest_cards_misc-03-quest-card-fields | Serializer for 0x03 (active, progress, completed, deck count) used by world `world-03-real-state`; on C2S 0x63 send 0x8A then 0x59 for each slot | 0x03 0x63 0x8A 0x59 | P0 | S | quest-state-model, world-03-real-state | no | no |
| quest_cards_misc-abandon-0x1E | 0x1E handler + 0x38 builder (duplicate-tolerant) | 0x1E 0x38 | P1 | S | quest-state-model | no | no |
| quest_cards_misc-wsproto-0x59-grammar | Fix 0x59 grammar (`if(slot < 4)`) or `_eval` cast support so `sendspec 59` works | 0x59 | P1 | S | — | no | no |
| quest_cards_misc-card-register | `char['card_deck']`; 0x64 -> 0x8B (results 1/2/3/6, cap 51, bag mirror); 0x8A from the stored deck | 0x64 0x8B 0x8A | P2 | M | en-quest-card-catalog, stop-0x63-echo, item_inventory bag model | no | no |
| quest_cards_misc-privacy-flags | Parse 0x2B bytes 0-4 and C2S 0x40; `refuses(target, kind)` API; wire it into whisper/trade/party/friend/messenger handlers | 0x40 0x2B 0x09 0x47 0x0C 0x0F | P2 | S | chat/trade/social groups for enforcement | yes | no |
| quest_cards_misc-quest-rewards-skill-class | Type-3 reward -> skill list, type-4 -> class/tier persistence (0x07 must reflect them) | 0x27 0x26 | P2 | M | turnin-0x17, combat_skill + login_character models | no | no |
| quest_cards_misc-keepalive-reaper | `last_rx` on every packet; close and save after 300 s silence | 0x05 | P3 | S | quest-state-model (save) | no | no |
| quest_cards_misc-admin-kick-maintenance | Admin JSON commands `kick` (0x5D + delayed close) and `shutdown` (login lock, 0x3A broadcast, save, close at 180 s); 0x17 builder kept but unused | 0x5D 0x3A 0x17 | P3 | M | login group maintenance refusal | no | no |
| quest_cards_misc-system-notice-0x99 | 0x99 sub 8 after enter-world; sub 9 GM/event grant with bag mirror | 0x99 | P3 | S | item_inventory bag model | no | no |
| quest_cards_misc-card-exp-bonus | +10% exp for kills whose card is registered (flag) | 0x21 | P3 | S | card-register | no | no |
| quest_cards_misc-dungeon-exit-0x7C | Consume/log 0x7C now; return-to-field when instances exist | 0x7C 0x08 0x03 0x07 | P3 | S | world `world-instance-dungeon` | no | no |

Suggested order: stop-0x63-echo -> en-quest-card-catalog -> quest-state-model -> accept-rework -> kill-progress -> turnin-0x17 -> 03-quest-card-fields -> abandon -> wsproto fix -> card-register -> the rest.

---

## 6. Live test plan

Baseline for every test: `python wsdev.py up` (client 1, test/test, TestHero in map 101). After any client-only injection that the server does not know about, run `python wsdev.py restart`. Button coordinates are the `hs/windslayer.hui` layout centres in 800x600 client space; confirm with `python wsdev.py shot` first because dialogs can be moved. Murubisiri (NPC 75, quest 26) is the live-proven quest giver in map 101. Pupus (npccode 1) are in map 102 through the portal at the map-101 ledge (x ~1404, press Down).

Client-state memory for verification: scene = `*0x70EECC`; active ids `scene+0x29E` (u16 x3), progress `+0x2A4`, ready `+0x2A7`, completed `+0x2AA`/`+0x354`, deck count `+0xE78`, deck ids `+0xE7A`.

### 6.1 C2S (capture)

- **T-05 KeepAlive (safe).** Stand idle in map 101 for 5 min, then `python wsdev.py logs 200`. Expected: one `[0x05] client heartbeat - consumed` about 240 s after the last 0x03, and no S2C reply. After the reaper: the session is not closed while 0x05/0x0D keep arriving.
- **T-16 Accept (state_change).**
  1. Stand overlapping Murubisiri.
  2. `python wsdev.py cap 3 hold space 350`, then `cap 2 click 488 203` (Next), then `cap 3 click 410 204` (Yes).
  3. Expected C2S `0x16 payload=2B 1a 00`.
  4. After the fix, the server sends exactly `0x26 1a 00` and `0x59 01 00`, with **no 0x18**. Chat shows "you've received Wooden Stick. (Count:1)" once. `key i` shows 1 stick. `key q` -> Accepted tab (132,156) lists Elder's Test at 0/1.
  5. Before the fix (baseline): 0x26 + 0x18 + chat 0x16, 2 sticks.
- **T-16b Talk-only accept (state_change, after the catalog fix).** Needs a character matching quest 1 (NPC 10, Lv 1-19). Accept through its NPC. Expected: `0x26 01 00` + `0x21 05 00 00 00`; quest under the Completed tab; "you've received Herb. (Count:3)" once.
- **T-16c Re-accept after portal (state_change; regression test for Q-B5/Q-B8).** Accept quest 26, portal to 102 and back. `key q`: after the fix Elder's Test is still under Accepted and Murubisiri no longer offers it. Before the fix: the log is empty, re-accept produces a 0x27 and rewards without a kill.
- **T-17 Turn-in (state_change).**
  1. With quest 26 active, go to map 102 and `hold s 450` next to a Pupu until it dies. Expected `0x59 01 01` and the green "[Elder's Test] You are ready to complete the quest.".
  2. Portal back and `cap 4 hold space 350` at Murubisiri.
  3. Expected C2S `0x17 1a 00`, then S2C `0x27 1a 00` + `0x21 0a 00 00 00`.
  4. Chat shows "you've received Herb. (Count:5)", "You've learned skill(Double Jump).", "+10 experience". The quest moves to Completed. No 0x18.
  5. Before the fix: server log `Unhandled opcode 0x17`, the Finish dialog opens, nothing else happens.
- **T-1E Abandon (state_change).**
  1. Accept 26, then `key q`, `click 132 156` (Accepted), click the quest row, `click 650 404` (Abandon), `cap 3 click 389 294` (OK in window 0x29).
  2. Expected C2S `0x1e 1a 00` (possibly twice). After the fix: S2C `0x38 1a 00`, the quest leaves Accepted, the Wooden Stick stays in the bag, Murubisiri offers the quest again.
- **T-40 Privacy (safe).**
  1. Click the HUD "Option" button, tick "Refuse whisper", then `cap 3 click <OK>` (window 0x1AD ctrl 0xB; locate with `shot`).
  2. Expected C2S `0x40 01 00 00 00 00`, no reply.
  3. `python wsdev.py restart`: the next C2S 0x2B starts with `01`. The multiplayer part (after enforcement): client 2 (`--client 2`) whispers TestHero and receives S2C `0x09` status 0x67 "is rejecting whispers" (state_change, needs 2 clients).
- **T-63 Deck list (safe).**
  1. `python wsdev.py cap 8 key down` on the map-101 portal sparkle. Expected C2S 0x2F and 0x63 (0 B).
  2. After the fix: S2C `0x8A 00` (or the stored deck) and no S2C 0x63.
  3. `key t` (Battlefield window) shows "0 Players / Join (0/12)", not garbage.
- **T-64 Register card (state_change).**
  1. Give a card: `python wsdev.py sendspec 18 "{\"gold\":100000,\"victy\":0,\"item_id\":2030,\"count\":1}"` (or kill Pupus until Monster Card <Pupu> drops; it is in `MONSTER_DB` id 1 drops). The server bag model must also hold it (use a real drop after the item fix).
  2. Open the bag (`key i`, Miscellanies tab), use the card's register action (Q3; try right-click and double-click, take `shot`), then `cap 4 click 385 294` (OK in window 0x292).
  3. Expected C2S `0x64 ee 07`. After the fix: S2C `0x8b 01 01 ee 07`, the modal closes, "Registered Monster Card <Pupu> in the Card Deck.", the card is gone from the bag, the Card Deck window shows 1 / 64 (1%).
  4. Before the fix: stuck on "Waiting for the server to response." Recover with `python wsdev.py send 8B 03`.
- **T-7C Dungeon exit (safe).** Not reachable: window 0x4A3 is only shown in instance dungeons, which EN content does not reference. Just check with `python wsdev.py logs` that no 0x7C ever appears. Revisit when `world-instance-dungeon` exists.

### 6.2 S2C (inject)

- **T-S26 (state_change).** Empty quest log: `python wsdev.py sendspec 26 "{\"quest_id\":26}"`. Expected: Elder's Test under Accepted, "you've received Wooden Stick. (Count:1)", scene+0x29E = 0x1A. Also `sendspec 26 "{\"quest_id\":1}"`: quest 1 goes under **Completed** and 3 Herbs are added (proves the talk-only branch).
- **T-S59 (state_change).** After T-S26 with 26: `python wsdev.py send 59 01 01` (raw; `sendspec` is broken, Q-B18). Expected: the green ready line and 1/1 in the Accepted tab. Then `send 59 01 01` again: no second message. **Never send slot 00** (it corrupts slot 3's quest id high byte).
- **T-S27 (state_change).** With 26 active: `sendspec 27 "{\"quest_id\":26}"`. Expected: the quest moves to Completed, Herb x5 and the Double Jump skill are granted, no exp change. A second injection grants the rewards again (confirms the "never double-send" rule).
- **T-S38 (state_change).** With 26 active: `sendspec 38 "{\"quest_id\":26}"`. Expected: silently removed from Accepted; scene+0x29E = 0.
- **T-S8A (state_change).** `sendspec 8A "{\"deck_count\":2,\"repeat[deck_count]\":[{\"card_item_id\":2030},{\"card_item_id\":2031}]}"`. Expected: the Card Deck window lists Pupu and Blue Pupu, the label reads "2 / 64 (3%)", and registering card 2030 afterwards shows "This card is already registered." locally. Never send deck_count > 51.
- **T-S8B (state_change).** Open the Card Registration flow with the Pupu card in the bag and no server handler, so the modal is up. Then inject in turn: `send 8B 06` -> "Already registered.", `send 8B 02` -> "Failed to receive dictionary information...", `send 8B 01 01 ee 07` -> success line, card removed from the bag. Never send new_deck_count 0.
- **T-S99 (state_change).** `sendspec 99 "{\"sub_type\":8,\"channel_no\":1}"` -> "In channel 1." in the chat log. `sendspec 99 "{\"sub_type\":9,\"item_id\":179,\"count\":1}"` -> "you've received Wooden Stick. (Count:1)", stick in the bag, gold label unchanged. `sendspec 99 "{\"sub_type\":5}"` -> nothing (safe no-op).
- **T-SA7 (safe, medium confidence).** `python wsdev.py send A7`, then `shot` immediately. Expected: nothing visible, or a one-frame flash; the client stays stable.
- **T-S5D (disruptive).** `sendspec 5D "{\"reason\":1}"`. Expected: modal "Disconnected due to unusual game play." and the client exits about 5 s later. Then `wsdev.py up`. Repeat with reason 99 ("Transferring to server failed.") and 7 (generic).
- **T-S17 (disruptive).** `python wsdev.py send 17`, then `sendspec 5D "{\"reason\":1}"`. Expected: **no** dialog and the client does not exit (latch). Then `wsdev.py down` / `up`, because the latch lasts for the whole process.
- **T-S3A (disruptive).** `python wsdev.py send 3A`. Expected: red chat line "[Announcement] Server will be down in 3 minutes for the maintenance." plus a green center notice. About 180 s later the modal "Server will be down for the maintenance." appears, and the client exits about 5 s after that. Run it last in a session.

---

## 7. Open questions

1. **Deck capacity.** EN has 64 cards but only 51 u16 slots fit before the i32 manner field at charinfo+0xE98 (scene+0xEE0). Did retail cap registration at 51, or does the EN build overflow into the manner points? Design caps at 51 (result 3). A live probe with 52 entries would corrupt manner points, so do it only on a throwaway character.
2. **"Card effect: EXP + 10%".** The tooltip is unconditional. Did the original server apply +10% EXP for kills of monsters whose card is registered? (F11 is flag-gated.)
3. **UI trigger for card registration.** `FUN_00469090` command 0x65 reaches `FUN_0045A3C0`, but the user gesture (right-click menu, double-click, or a Card Deck window control) was not resolved. The Card Deck window 8 hotkey is also unknown. Find both live with `cap` + `shot`.
4. **NPC proximity.** The client sends no NPC id in 0x16/0x17. Should the server require the SNPC/ENPC on the current map? That needs NPC placement from the stage `.hmi` files (world group).
5. **Party kill credit** for ReqPro quests, and credit on kills that were not last-hit. The client P2P path (`FUN_0041A230`) credits only the last hitter.
6. **Entity quest mirror.** `FUN_0041A230` increments `entity+0x11A` for ids at `entity+0x114`. No known S2C fills these; confirm the path is dead under server-driven combat (it would double-count if it ran).
7. **Class tier for the Job check.** The character store has `class` but not `tier` (entity+0x111). Where does the server get the tier, and how are type-4 class-change quest rewards persisted and broadcast (S2C 0x58)?
8. **Refusal feedback for 0x16/0x17.** No reject packet exists. Is S2C 0x15 SystemMessage acceptable UX, or did retail stay silent?
9. **Party invite refusal.** `refuse_party` has no dedicated refusal packet in the spec. Should the invite just be dropped silently?
10. **Where the original server used S2C 0x17** (server/channel transfer?). The latch never clears, so its correct use depends on a reconnect flow that is not traced.
11. **Instance dungeons in EN.** All 291 hqi IndunMapName values are `None`, and the entry request is unknown. Are windows 0x4A3/0x4A4 and C2S 0x7C reachable in EN 2008 at all? What was the retail reply to 0x7C?
12. **Demand type 1.** Exact semantics of `FUN_00424DF0` (equipped-item requirement) for the server-side re-check.
13. **Quests with SNPC 0** (45). Were they started by other means (item use, events, NextQuest chaining)? The design refuses 0x16 for them.
14. **Unexplained idle exit.** LIVE_TEST_LOG shows the client leaving after ~19 min idle with no server traffic. Is there a client inactivity watchdog that expects some S2C? 0x05 itself expects no reply.
15. **0x3A cancel.** Confirm no other packet or scene change kills timers 9/7 once armed.
16. **Completed-list overflow.** At 85 entries the client shifts out the oldest. Did retail persist more than 85 server-side (affecting PrevQuest checks)? The design mirrors the client's FIFO.
