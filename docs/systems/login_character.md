# login_character: system design (EN 2008 client)

Group spec: `re_tools/corpus/systems/login_character.json` (23 keys; systems `login` and `character`).
Sources, most trusted first: client binary evidence (the spec, `corpus/decomp/*.c`, bytes read from `WindSlayer.exe` / `WindSlayer_patched.exe`), then `LIVE_TEST_LOG.md`, then `server/windslayer_server.py` (line numbers are for the current 2793-line file), then memory notes, then PySlayer. PySlayer is a newer KR build, and its layouts for 0x02, 0x07, 0x14-KR, 0x1C, 0x52 and 0x58 do **not** apply to EN.

Direction: S2C = server->client, C2S = client->server. Payload lengths exclude the opcode byte. Plan ids use the prefix `lc-` (= login_character); other design docs already refer to `lc-data-model`.

**Re-verified for this revision (not only taken from the spec):**
- Both exp tables were read from the exe. 0x6F0C28 and 0x6F0EB0 are identical: 99 per-level **increments** summing to 1,200,382,231. `FUN_00440DF0` accumulates them.
- The job-change table comes from the `FUN_004249E0` decomp. Item names come from `server/en_item_catalog.json`.
- The class-name table at 0x70C588 was read as bytes: tier-2 row off by one.
- The creation look defaults and the 0x1C look copy come from the `FUN_00446300` / `OnReceive` decomp.
- WM_TIMER jump table 0x43ED30: entry 0 (timer 2) is 0x43E851 in `WindSlayer.exe` and 0x43E9DB (no-op) in `WindSlayer_patched.exe`.
- Gender column vs "(M)"/"(F)" item names.
- The `wsproto` encoder behaviour for 0x58/0x74.
- The gamedef check: no quest or NPC list grants job-change items.

---

## 1. Overview: how the client implements it

### 1.1 Opcode inventory

| Key | Dir | Name | Len | Server today | Plan prio |
|---|---|---|---|---|---|
| 0x01 | S2C (version socket 7011) | VersionCheckAndServerList | var | implemented (IP hard-coded) | P1 |
| 0x5A | S2C | KeyExchangeSeed | 4 | implemented, correct | keep |
| 0x44D8BF/0x01 | C2S | LoginRequest | 62 | implemented | P1 (uid, errors) |
| 0x02 | S2C | LoginResultCharacterList | 1, or 13+75n (+33) | implemented, fields mislabelled | **P0** |
| 0x449384/0x0E | C2S | CharacterCreate | 35 | implemented, wrong model | **P0/P1** |
| 0x1C | S2C | CreateCharacterResult | 1 | missing (server re-sends 0x02) | P1 |
| 0x44ABA4/0x12 | C2S | DeleteCharacter | 38 | **missing (client soft-locks)** | P1 |
| 0x1F | S2C | DeleteCharacterResult | 1 | missing | P1 |
| 0x42F904/0x2B | C2S | EnterWorldRequest | 42 | implemented (hand-parsed) | P1 |
| 0x44840E/0x04, 0x4484CC/0x04 | C2S | StatPointAllocate | 1 | partial (fake reply) | P1 |
| 0x14 | S2C | SetBaseStat | 7 | partial (value hard-coded 4) | P1 |
| 0x21 | S2C | ExpDelta | 4 | implemented, wrong baseline | **P0** |
| 0x22 | S2C | SetLevel | 5 | implemented, wrong thresholds, sent to owner | **P0** |
| 0x58 | S2C | PlayerClassChange | 6 (4 if uid unknown) | missing | P1 |
| 0x4484CC/0x2A | C2S | CharacterInfoRequest | 17 | missing | P2 |
| 0x52 | S2C | PlayerInfoView | 34 + entries | missing | P2 |
| 0x53 | S2C | TargetNotOnServer | 17 | missing | P2 |
| 0x74 | S2C | CharacterNameChanged | 21 (4 if uid unknown) | missing | P3 |
| 0x7F | S2C | ExpDeltaFromMenti | 8 if delta>0, else 4 | missing | P3 |
| 0x4484CC/0x65, /0x66 | C2S | AccountTransferRequest | 33 | missing | P3 |
| 0x8C | S2C | IdTransferResult | 1, or 34 when result==2 | missing | P3 |

### 1.2 Connection and login pipeline
1. The launcher opens TCP 7011. The version server sends **S2C 0x01**:
   - `u16 version_code`: 3 = OK, >= 0xEA61 = notice/maintenance, anything else = dead auto-patcher;
   - `u16 notice_len` + notice text;
   - the server/channel table. Each channel carries a **host-order u32 game IP**.
   The client closes the version socket. When version_code == 3 it arms timer 8, which blinks START.
   In the >= 0xEA61 path it shows the notice, then "Connection to Server got disconnected", and arms **timer 7**. The WM_TIMER handler `FUN_0043E3F0` case 7 posts WM_CLOSE, so **the client exits after 5 s**.
2. Login screen checks: non-empty ID/password (0x4481A9/0x4481C9) and channel status == 3 (0x448211). Then `ConnectToGameServer(ip, 7022)`. The port is hard-coded at 0x44080E.
3. The game server's first packet is **S2C 0x5A** `i32 seed` (NoEncode + EncodebyArray). The client:
   - calls SetCodeKey;
   - computes MD5(account_id) and compares it with scene+0x131, setting scene+0x218 = (differ);
   - `Sleep(1000)` **inside the receive critical section**;
   - sends **C2S 0x01** `str[41] account_id, str[21] password`. Both strings are raw copies from larger, unbounded strcpy buffers, so the server must split each at its first NUL.
4. **S2C 0x02** with `result != 1`: the client **closes the game socket first**, then shows a dialog (§2 F2 has the code table).
   **S2C 0x02** with `result == 1`:
   - fade, load `./hs/main99_02.hmi`, scene+0xF00 = 5 (character select);
   - `account_id -> scene+0x220`. This is **the uid every later local-player 0x07 must carry** (registration gate 0x4221A2);
   - billing flag -> gs+0x700; gender -> scene+0x130; manner -> scene+0xEE0;
   - per character, one 0x15F0 entity:
     - name, class +0x110, job branch +0x111;
     - **total exp +0xB0, with the level derived client-side** (FUN_00440DF0 -> +0x99);
     - 3 ranks + 3 signed rank changes;
     - 14 look u16s at +0x120;
     - x = 110*(i+1) (+0x15A4 int, +0x11F8 f64), y = 300 (+0x15A8 int, +0x1288 f64);
     - uid 30000000+i, alive 3.
   - Finally `transfer_status` (+ str[33] if == 1). The UDP 0x11 broadcast call is NOP-patched in our exe.

### 1.3 Character select (window 0x12)
- ctrl 1 **Create**. Refused when scene+0x14 (entity count) == 5: "No more characters can be created." Otherwise it sets defaults (§1.4) and opens window 0x13 with free points = `FUN_00440E10(level)` = 9.
- ctrl 2 **Delete**. Needs count != 0, a selected entity, `+0x15A8 == 0x214` and `+0x904 == 8`. +0x15A8 is the int Y that 0x02/0x1C set to 300, so 0x214 = 532 most likely means "the selected character has walked to its front spot". +0x904 == 8 is the idle action state. It then calls `FUN_00464480`, which opens password dialog 0x201. That function refuses with "no SS# in this ID..." when scene+0x218 == 0.
- Clicking a character (`FUN_0044C4B0`, needs +0x904 == 8) selects it (+0x8B4 = 4) and shows info panel 0x15: "Lv.%d" from +0x99, the class label, and ranks ("No Data." when 0). A double-click on an idle character presses Start.
- ctrl 3 **Start**. Once the entity's int X exceeds 780 and window 0x16 is absent (or +0x3C == 1), the per-tick updater `FUN_0042CF20` sends C2S 0x2B once.
- ctrl 4 **Back**. The stock client's timer-2 expiry also calls it (§1.6).
- ctrl 5 opens ID-transfer dialog 0x290. Controls 5/6/7 are hidden unless `transfer_status` is 1/2 or scene+0x218 == 0.

### 1.4 Character creation (window 0x13), decomp `FUN_00446300` lines 1183-1350
- Defaults on open (verified):
  - gender flag scene+0x130 == 0: s10 = s4 = s13 = 1, s1 = 1, s6 = 2, s5 = 2, s9 = 2;
  - flag != 0: s10 = s4 = s13 = 0x65, s1 = 0x65, s6 = 0x66, s5 = 0x66, s9 = 2;
  - slot 11 = 0 in both cases.
  (sN = u16 look array element N at scene+0x3AC+2N.)
- The arrow controls cycle s10 through 1..4 (0x65..0x68) and **always write s4 and s13 with the same value**. The other slots cycle as described in the spec: s1 1..4 / 101..104, s6 and s5 2..5 / 102..105, s9 2..5.
- The stat +/- buttons move points between the pool (+0x294) and STR/DEX/INT/SPR (+0x296..+0x29C). OK is refused while the pool != 0: "You must use all of your stat points."
- Name check `FUN_0043DF40(name, 1)` (verified):
  - empty -> "Enter your name.";
  - contains 0x20 or CP949 `A1 A1` -> "No blank is allowed in Name.";
  - lowercased copy contains `'`, `\`, `gamemaster`, `yahoo`, `wind`, `windmaster`, `windy`, `winsle`, `master`, `avocade`, `administrator` or `hamelin`, or fails `Curse_Engine_Clean` -> "Invalid name. Please try another name.".
- It then sends **C2S 0x0E** `u16 s10, s1, s6, s5, s9, str[17] name, u16 str, dex, int, spr` and opens "Waiting for the server to response." (window 0x16).
- **S2C 0x1C result 1** closes window 0x16 and builds the new select-screen entity **from its own creation state**, with no packet data (verified decomp lines 2318-2376):
  - name;
  - look `[0, s1, 0, 0, s4(=s10), s5, s6, 0, 0, s9, s10, 0, 0, s13(=s10)]`;
  - +0x110 left 0 (**Novice**), level 1, HP 100, uid 30000000+count, x = (count+1)*110, y = 300.
  So **in EN a new character is a Novice, and s10 is appearance, not class.**

### 1.5 Deletion
C2S 0x12 carries `str[17] name` (the selected entity) + `str[21] password` (dialog 0x201 ctrl 5). The client then opens the waiting modal.
S2C 0x1F results:
- 1: removes the **selected** entity. `FUN_00422B90` shifts later entities 110 px left with uid-1, and the selection is cleared.
- 2: DB busy.
- 3: "You can't delete a character created within 24 hours."
- 12: "Invalid password."
- anything else: only closes the modal.

### 1.6 Enter world (hand-off to the `world` group)
- **C2S 0x2B** `u8 refuse_whisper, refuse_exchange, refuse_party, refuse_talk, refuse_friend` (from `HKCU\Software\Hamelin\WindSlayer\<name>`), `str[16] p2p_ip`, `u32 p2p_udp_port` (always 42907), `str[17] char_name`.
- The client then arms TIMER_RECEIVELOADED (id 2, 5500 ms) and a wait dialog. S2C 0x03 calls KillTimer(2).
- **Timer 2 expiry (verified):** stock `WindSlayer.exe` shows "No response from the server.", kills the timer and runs char-select ctrl 4 (Back). Our `WindSlayer_patched.exe` redirects jump-table 0x43ED30[0] to the default case, so expiry does **nothing**. **With the patched client, a 0x2B that gets no 0x03 leaves the wait dialog up forever.**
- The server answers with S2C 0x03, which carries **`u32 exp_total` -> scene+0x278** and `fame_points` (-> rank via table 0x6F11D8). It then sends 0x07, with `uid == scene+0x220`, `job`, `job2`, `level`, `rank_icon`, `gender`, `appearance[14]`, and **`stat_str/dex/int/tol`** (+0xE6..+0xEC). S2C 0x03 and 0x08 destroy every entity, so the local player is unregistered until the next 0x07.

### 1.7 Progression (client-computed from server-sent totals)
**Exp/level.**
- Level = smallest L with `exp < cum[L-1]` (FUN_00440DF0).
- Thresholds: Lv2 = 56, Lv3 = 177, Lv4 = 415, Lv5 = 840, Lv10 = 10,260, Lv12 = 21,616, Lv13 = 30,485, Lv99 = 1,200,382,230.
- exp >= 1,200,382,231 makes the function return low byte 1 (Lv1).

**S2C 0x21** `i32 delta`:
- Dropped entirely while scene+0x970 == 0 (no local player).
- Otherwise scene+0x278 += delta, the chat line "You've received (+%d) experience points." / "Lost (%d) EXP." is printed, and the exp bar is redrawn.
- If a cumulative threshold is crossed, the client **levels itself**: event 0xA2, effect 0x24, sound, +0x99 = level_from_exp, full HP/MP heal, stat window refresh. Level-down works the same way with no heal.

**S2C 0x22** `u32 uid, u8 level`:
- Sets +0x99 on any loaded entity, recomputes max HP/MP with a full heal (level 1..99 and class <= 6 only), and **always** plays effect/sound 0x24.
- Also refreshes party frames and the quest list.
- Does not touch scene+0x278 or the local stat window. level 0 = no-op.

**Stats.**
- Base STR/DEX/INT/SPR live at entity +0xE6/+0xE8/+0xEA/+0xEC (EN strings Strength/Dexterity/Intelligence/Spirit).
- Free points = `total(level) - sum`, with `total(L) = 9 + 4*min(L-1,28) + 5*max(0,L-29)` for L <= 99 and 0 above (FUN_00440E10, verified). That gives 9 at L1, 13 at L2, 121 at L29, 471 at L99. Level-ups create free points without any packet.
- The status window's + buttons send **C2S 0x04** `u8 idx` (0..3). Arena maps are blocked client-side ("Not allowed in arena."). The client does not check free points.
- **S2C 0x14** `u32 uid, u8 idx, u16 value` sets an **absolute** value. INT recomputes max MP and SPR recomputes max HP (clamp down only). If uid is the local player, the stat window is redrawn.

**Class.**
- +0x110: 0 Novice, 1 Warrior, 2 Monk, 3 Archer, 4 Rogue, 5 Mage, 6 Priest. +0x111: tier 0..2.
- EN applies a class change **through a type-4 "Job Change" item delivered by S2C 0x18** (SubHandler1 case 0x18 -> `FUN_00440920` case 4 -> `FUN_004249E0` -> `FUN_00440BC0`). The item is **not** added to the bag. It shows the popup "<class> is your new class." and recomputes stats/HP/MP on base-class changes.
- **S2C 0x58** `u32 uid, u8 class, u8 tier` sets the two bytes on any loaded entity. It recomputes only when tier == 0.

### 1.8 Remote info, rename, mentee exp, ID transfer
- **Player popup 0x50** opens on right-click or double-click on a remote entity (`alive == 3`, uid != own, field mode), or by clicking a party frame 0x75..0x78.
  - ctrl 3 (Info): if the uid is loaded, window 0x72 is filled locally and nothing is sent.
  - Otherwise the client sends **C2S 0x2A** `str[17] name` (no modal). The server answers **S2C 0x52** (opens window 0x72) or **S2C 0x53** (chat "<name>is not in server.", with no space).
- **S2C 0x74** `u32 uid, str[17] new_name` renames a loaded entity in place (scene+0x206 and the stat window too when it is the local player). It is the broadcast half of the Change Nickname cash item (item 1895, "Change Nickname" in the EN catalog): C2S 0x49 -> S2C 0x73 (premium_cash group). **0x73 renames nothing locally.**
- **S2C 0x7F** = 0x21 plus `u32 menti_id` when delta > 0. It prints "(Menti[name])" only when the id is in the mentee list at messenger(gs+0x4E4)+0x28, which **S2C 0x7B/0x7E** fill. The spec text's "0x0B" is wrong: 0x0B fills the friend list at messenger+0x04.
- ID transfer (legacy Yahoo-ID migration) is C2S 0x65/0x66 `str[33]` -> S2C 0x8C. It is unreachable while `transfer_status` = 3 and scene+0x218 = 1.

---

## 2. Request/response flows

### F1. Version check (TCP 7011)
1. The client connects to 7011.
2. The server sends S2C 0x01 (framing unchanged: `make_raw_packet(body, seq=1)`, line 345):
   `version_code=3, notice_len, notice, server_count=1, server_status=3, channel_slot_count=N, channel_entry_count=N, repeat N {channel_no=i+1 ascending, user_count=online_in_channel, game_server_ip=host-order u32 of PUBLIC_IP}`.
   - Maintenance: `version_code=0xEA61` + notice. The client shows it, disconnects and exits after 5 s (timer 7).
   - **Never** send another version code: the launcher starts the dead CDN patcher.
   - Limits: server_count <= 4; slot_count <= 10; notice <= 1000 bytes; channel_no strictly ascending. User load colour = user_count/200.
3. The server closes the socket (as today).
4. Gate: port 7022 is hard-coded, so separate channels need **separate IPs**.

### F2. Login
1. On accept, the server sends S2C 0x5A `seed` (lines 490-503, keep).
2. About 1 s later C2S 0x01 arrives. Decode it with the grammar and cut each field at its first NUL. Then:
   - a. The session is already logged in: ignore it (line 2629, keep). Any stray 0x5A makes the client re-send the login.
   - b. Payload < 62 bytes: **0x02 result=0x0B** "Login process was corrupted (RETURN)" (line 2636, keep).
   - c. `MAINTENANCE` set: `0x0E`. Online count >= `MAX_ONLINE`: `0x06`.
   - d. Unknown account, or password mismatch: **`0x11`** "Enter the correct password" (same code for both, to avoid account enumeration). Exception: with `AUTO_REGISTER`, create the account and continue.
   - e. `account.banned`: `0x05`. `account.deleted`: `0x13`.
   - f. The account is already online: close the **old** session's socket (remove it from the online indexes and save its character), then send `0x04` "Connection already exists. Disconnecting existing connection" to the **new** socket. The client closes it, and the user logs in again.
   - g. Success:
     - set `session.account_id = account.account_id` (persistent, unique, 1..0xEFFFF) and register it in `online_by_account`;
     - send **S2C 0x02 result 1**: `account_id, cash_first_purchase_flag=0, account_gender_flag=account.gender, manner_points=account.manner_points, char_count (<= 5), per char {name, class_id=char.class, job_branch=char.job2, total_exp=char.exp, rank_1..3=0, rank_change_1..3=0, appearance[14]=char.look}, transfer_status=3`.
3. Client gates: results other than 1 close the socket before the dialog; 3, 0 and > 0x13 close silently. **Never send a success 0x02 while the client is in the world**: it tears the client back to character select.

Result codes:
- 2 DB busy; 3 silent; 4 already connected; 5 blocked; 6 server full;
- 7 HIO; 8 HIS; 9 READ; 0x0A InternetQuery (these four are internal errors);
- 0x0B corrupted (RETURN); 0x0C corrupted; 0x0D 24 hours logged in;
- 0x0E maintenance; 0x0F blank fields; 0x10 cannot log in now;
- 0x11 wrong password; 0x12 needs email confirmation; 0x13 account deleted.

### F3. Character create
1. Client gates (§1.4): count < 5, pool == 0, local name check. Then C2S 0x0E (35 bytes) and the waiting modal.
2. The server decodes `look_slot10, look_slot1, look_slot6, look_slot5, look_slot9, name, str, dex, int, spr` and validates in this order:
   - a. Name fails the §3.5.4 rules -> **S2C 0x1C `{result: 4}`** "Invalid name.".
   - b. Name already used by any character on any account (case-insensitive `name_index`) -> **`{result: 2}`** "The name is already being used.".
   - c. The account already has >= 5 characters, `str+dex+int+spr != stat_total(1) = 9`, or a look value is outside the gender range (§3.5.3) -> **`{result: 3}`** "Server process is running... (DB)", and log it as a tamper.
   - d. OK:
     - append the character record (§3.2): `class=0, job2=0, exp=0, look=[0,s1,0,0,s10,s5,s6,0,0,s9,s10,0,0,s10], str/dex/int/spr, created_at=now, map/x/y = START_MAP/START_X/START_Y`;
     - add the name to `name_index`, save;
     - send **S2C 0x1C `{result: 1}`**.
3. The client builds the entity itself and leaves window 0x13. **Do not also send 0x02** (open question Q3: possible duplicate entities).
4. Fallback: the current behaviour (answer with the full 0x02 list, lines 2614-2617) is live-proven. Keep it behind `CREATE_REPLY_0x02 = True` until T-0E/T-1C pass.

### F4. Character delete
1. Client gates (§1.3): a selected idle character, scene+0x218 != 0, dialog 0x201 confirmed. Then C2S 0x12 (38 bytes) and the waiting modal.
2. The server decodes `char_name` and `confirm_password`:
   - a. `char_name` is not on this account -> **S2C 0x1F `{result: 2}`** (DB busy; the client keeps its list unchanged).
   - b. `confirm_password != account.password` (compare with the stored hash once lc-login-errors lands) -> **`{result: 12}`** "Invalid password.".
   - c. `now - char.created_at < DELETE_MIN_AGE_HOURS*3600` -> **`{result: 3}`**. Retail used 24 h; the suggested dev default is 0.
   - d. OK: remove the character. **Keep the order of the remaining characters**: the client drops the selected entity and shifts the rest left, so the list indices must stay aligned. Remove it from `name_index`, save, and send **`{result: 1}`**.
3. **Every 0x12 must get a 0x1F**, or the modal never closes (today's bug B11).

### F5. Enter world (hand-off to `world`)
1. Decode C2S 0x2B (42 bytes) with the grammar. Store `session.refuse = {whisper, exchange, party, talk, friend}` (for the chat/trade/party/social groups) and `session.p2p = (p2p_ip, p2p_udp_port)` (pvp_arena). p2p_ip may be empty.
2. Look up `char_name` on the session's account:
   - **Not found** -> send S2C 0x02 (success list). That closes window 0x16 and reloads the select screen. Without a reply the patched client waits forever (§1.6). Today the server just `return`s (B14).
3. Found:
   - set `session.char = char`, `session.level = level_for_exp(char.exp)`, and register `online_by_name[name.lower()] = session`;
   - the world builders send S2C 0x03 with **`exp_total = char.exp`**, `fame_points = char.fame`, then S2C 0x07 with:
     - `uid = session.account_id`, `karma = account.manner_points`;
     - `job = char.class`, `job2 = char.job2`, `level = session.level`, `rank_icon = rank(char.fame)`;
     - `gender = account.gender`, `appearance = char.look`;
     - `stat_str/dex/int/tol = char.str/dex/int/spr`.
   - The world/combat groups follow with 0x28/0x44 (or real cur_hp/cur_mp inside 0x07).
4. Set `session.in_world = True` **after** 0x07 is sent (0x21 sent earlier is dropped by the client).
5. The map-change path (`_handle_change_map` lines 1297-1306) replays 0x03+0x07. It must use the same builders, or it re-installs a wrong exp baseline on every portal.

### F6. Exp gain / level change (used by combat, quests and death penalty)
1. `grant_exp(session, delta)`:
   - `old = char.exp`, `new = clamp(old + delta, 0, EXP_MAX = 1,200,382,230)`, `d = new - old`. Stop if `d == 0`.
   - `char.exp = new`, `old_lv = level_for_exp(old)`, `new_lv = level_for_exp(new)`, mark dirty.
2. If `session.in_world`: send **S2C 0x21 `{exp_delta: d}`** to the owner only. The client prints the message and levels up or down by itself.
3. If `new_lv != old_lv`:
   - set `session.level = new_lv`;
   - **do not send 0x22 to the owner** (the effect would play twice, B4);
   - send **S2C 0x22 `{uid: session.account_id, level: new_lv}`** to other in-world sessions on the same map (lc-level-broadcast);
   - on a level-up, mirror the client's full heal (`session.hp = max_hp`, `session.mp = max_mp`; the formulas are owned by combat).
4. Mentor share (P3): if the character has an online mentor, credit `share` to the mentor with `grant_exp` logic, but send **S2C 0x7F `{exp_delta: share, menti_id: <mentee uid exactly as sent in 0x7B/0x7E>}`** instead of 0x21.

### F7. Stat point allocation
1. Client gates: status window (UI 1) + button enabled (free >= 1) and not an arena map. The client sends **C2S 0x04 `u8 stat_index`**: 0 STR (ctrl 10), 1 DEX (ctrl 12), 2 INT (ctrl 14), 3 SPR (ctrl 16).
2. Server:
   - `stat_index > 3`, or not in world -> ignore.
   - `str+dex+int+spr >= stat_total(session.level)` -> send **S2C 0x14 `{uid, stat_index, value: current}`** as a harmless resync (tamper or race).
   - Otherwise: increment, mark dirty, send **S2C 0x14 `{uid: session.account_id, stat_index, value: new_value}`**.
3. Optional (P3): send the same 0x14 to observers on the map.

### F8. Class change
1. Trigger. The retail source is unknown (Q6): no gamedef quest sends/rewards and no NPC item list contains items 180/203-207/3076-3088. Provide `apply_job_item(session, item_id)` and call it from:
   - a GM command `/job <item_id>`,
   - a configurable NPC/quest hook, or
   - using a job item from the bag (item group).
2. Validate against the §3.5.5 table using `char.class/job2`:
   - invalid -> chat line only; change nothing (the client's `FUN_004249E0` would refuse silently anyway);
   - valid -> update `char.class/job2`, save, then:
     - send the owner **S2C 0x18 `{gold: session gold, victy: session victy, item_id: job_item, count: 1}`**. gold/victy are **absolute** wallet writes, so always send the current wallet. The client applies the class, recomputes, and shows "<Class> is your new class.";
     - broadcast **S2C 0x58 `{uid, class, class_tier}`** to other in-world sessions on the map, encoded with `assume={'find_entity_by_uid(uid) != null': True}`. Without the assume flag the encoder emits only 4 bytes.
3. Silent GM fix-up (no popup): send S2C 0x58 to the owner with its own uid.

### F9. View another player's info
1. Client: popup 0x50 ctrl 3 on a uid that is not loaded -> **C2S 0x2A `str[17] target_name`** (no modal).
2. Server: look up `online_by_name[name.lower()]`.
   - Online -> **S2C 0x52**: `char_name, manner_points, level = level_for_exp(exp), rank = rank(fame) (0..98), job1 = class, job2, stat_str..stat_spr, equip_count = 25, entries[i] = {item_id = slot i (16 equip grid + 9 cash), option_count (<= 5), options, option_last}`. Empty slots use item_id 0, which the client skips.
     **Never exceed 25 entries or 5 options** (stack arrays in a 0x12FC-byte frame).
   - Not online, or unknown -> **S2C 0x53 `{char_name}`**, NUL-terminated within 17 bytes.

### F10. Rename (P3, together with premium_cash)
1. The client uses item 1895 (Change Nickname) and sends C2S 0x49 `str[17] new_name`, then opens the waiting modal.
2. The server validates the name as in F3 2a/2b and checks that the item is owned. Failure -> S2C 0x73 `{result: 0}` ("The name is already being used.").
3. Success:
   - update `char.name`, `name_index`, `online_by_name`, and every name-keyed reference (friends/mentor lists);
   - send S2C 0x73 `{result: 1, item_serial}`;
   - **then S2C 0x74 `{uid, new_name}`** to the requester and every in-world session on the same map (`assume={'entity_with_uid_exists': True}`).
4. The client renames the entity in place. Hazard: its registry options stay under the old name.

### F11. ID transfer (legacy, P3)
1. Always send `transfer_status=3`, so the UI stays hidden.
2. If C2S 0x65/0x66 arrives anyway: log it and reply **S2C 0x8C `{result: 6}`** ("No id information..."). Never send result 2 with a new_id of 32+ characters (sprintf_s into 64 bytes).

---

## 3. Server state and data model

### 3.1 Account record (accounts.json, keyed by login ID)
```json
"test": {
  "password": "test",          // plaintext today; later salted hash (lc-login-errors)
  "account_id": 1,             // u32, unique, persistent, 1..0xEFFFF; = in-world uid (scene+0x220)
  "gender": 0,                 // 0x02 account_gender_flag; !=0 may equip items.Gender==1 "(M)" (Q2)
  "manner_points": 0,          // 0x02 i32, 0x07 karma, 0x52 manner (Q8)
  "banned": false, "deleted": false,
  "cash_first_purchase": 0,    // 0x02 flag (premium_cash)
  "characters": [ ... ]        // ordered = select slot order, max 5
}
```
Uid namespace:

| Range | Use |
|---|---|
| 1..0xEFFFF | players (account_id) |
| 0xF0000+ | monsters (`MOB_UID_BASE`, line 138) |
| 0x200000+ | ground items |
| 30000000+i | client-side select-screen entities (never assign) |
| 33000000+n | client-side static map NPCs (never assign) |

### 3.2 Character record
```json
{
  "name": "drix", "created_at": 1726550000,
  "class": 0, "job2": 0,                  // 0x02 class_id/job_branch, 0x07 job/job2, 0x52 job1/job2, 0x58
  "exp": 0,                               // u32 total, the ONLY truth; level is always derived
  "look": [0,1,0,0,1,2,2,0,0,2,1,0,0,1],  // u16[14] exactly as 0x02 / 0x07 appearance
  "str": 3, "dex": 2, "int": 1, "spr": 3, // allocated base stats (+0xE6..+0xEC)
  "fame": 0,                              // 0x03 fame_points; rank via table 0x6F11D8 (world/pvp)
  "map": 101, "x": 1411.0, "y": 714.0,    // world group
  "hp": null, "mp": null                  // combat group
}
```
Items, equipment grid, gold, quests and friends belong to their own groups but hang off the same record.

### 3.3 Session fields (dict created in `_handle_fireway`, line 509)
Existing: `username`, `account_id` (was hard-coded 1), `char_name`, `current_map`.
New:

| Field | Purpose |
|---|---|
| `account` | ref to the account record |
| `char` | ref, set on 0x2B |
| `level` | derived |
| `in_world` | set after 0x07 |
| `refuse` | 5 flags from 0x2B |
| `p2p` | (ip, port) from 0x2B |
| `dirty` | needs save |

Remove the separate `session['exp']` (line 1995); use `char.exp`.

### 3.4 Global server state (GameServer)
- `online_by_account: {account_id: session}`: duplicate login (0x02 result 4), broadcasts.
- `online_by_name: {lower(name): session}`: 0x2A, whisper/party/friend by name.
- `name_index: {lower(name): (username, idx)}`: uniqueness on create/rename; rebuilt at load.
- `accounts_lock` + `save()`: `json.dump` to `accounts.json.tmp`, then `os.replace`. Call it on create/delete/class/rename/stat and on disconnect (the finally block, line 578). Debounce exp saves (e.g. 30 s). Today `_save_accounts` (405-409) truncates in place and runs only on create (2612).

### 3.5 Derived rules and constants (suggested module `server/progression.py`)
1. **Exp table.** `EXP_INC` = the 99 u32 at exe VA 0x6F0C28 (56, 121, 238, 425, 700, 1128, ... 77319382, 82635627, 1). `EXP_CUM[i] = sum(EXP_INC[:i+1])`. `level_for_exp(e)` = first `i` with `e < EXP_CUM[i]`, return `i+1`. Clamp e to <= `EXP_MAX = EXP_CUM[97] = 1,200,382,230` (Lv99). `exp_for_level(L) = 0 if L <= 1 else EXP_CUM[L-2]`.
2. **`stat_total(L)`** = `9 + 4*min(L-1,28) + 5*max(0,L-29)` for 1 <= L <= 99, else 0.
3. **Look composition** = `[0, s1, 0, 0, s10, s5, s6, 0, 0, s9, s10, 0, 0, s10]`.
   Ranges: `gender == 0`: s10 and s1 in 1..4, s6 and s5 in 2..5. `gender != 0`: s10 and s1 in 101..104, s6 and s5 in 102..105. s9 is 2..5 for both.
4. **Name rules** (mirror FUN_0043DF40, plus server policy):
   - bytes up to the first NUL, length 1..16;
   - no `0x20` and no `A1 A1`;
   - the lowercased name contains none of `' \ gamemaster yahoo wind windmaster windy winsle master avocade administrator hamelin`;
   - policy: `[A-Za-z0-9]` only (the Curse_Engine list is not available, Q12);
   - unique case-insensitively.
5. **Job-change items** (FUN_004249E0, verified; names from `en_item_catalog.json`, all items Type 4):

   | item id | EN name | requires | sets |
   |---|---|---|---|
   | 180 (0xB4) | Class Change: Warrior | class 0 | class 1 |
   | 203 (0xCB) | Job Change: Monk | class 0 | class 2 |
   | 204 (0xCC) | Job Change: Archer | class 0 | class 3 |
   | 205 (0xCD) | Job Change: Priest | class 0 | class 6 |
   | 206 (0xCE) | Job Change: Mage | class 0 | class 5 |
   | 207 (0xCF) | Job Change: Rogue | class 0 | class 4 |
   | 3076 / 3077 | Berserker / Paladin | class 1, tier 0 | tier 1 / 2 |
   | 3078 / 3079 | Fighter / Counter | class 2, tier 0 | tier 1 / 2 |
   | 3080 / 3081 | Assassin / Trapper | class 4, tier 0 | tier 1 / 2 |
   | 3083 / 3084 | Elementalist / Summoner | class 5, tier 0 | tier 1 / 2 |
   | 3085 / 3086 | Bishop / Dark Priest | class 6, tier 0 | tier 1 / 2 |
   | 3087 / 3088 | Sniper / Beast Master | class 3, tier 0 | tier 1 / 2 |
   | 3082 | "====" (unused) | never grant | - |

   Class-name table 0x70C588 (bytes read):
   - rows `Novice Warrior Monk Archer Rogue Mage Priest | "" Berserker Fighter Sniper Assassin Elementalist Bishop | Paladin Counter BeastMaster Trapper Summoner DarkPriest ""`;
   - the tier-2 row is **shifted by one**: index 14 = class 0. The client shows a tier-2 Warrior as "Counter", Monk as "BeastMaster", ..., and Priest as blank;
   - fix client-side only: optional exe patch lc-classname-patch.
6. **Quest class filter.** `quests.Job` is a 21-char string indexed `class*3 + tier` (client filter 0x4782F0). Owned by the quest group.

### 3.6 gamedef / content used
- `items` (idx, Type, Kind, Gender, Job, Job2, Lv): Type 4 = job change (all Lv 0, so no level requirement in data); 1895 = Change Nickname (EN type 5).
- Gender: in `en_item_catalog.json`, 491 "(M)" items have Gender 1 and 494 "(F)" items have Gender 2 (3 outliers).
- `quests.Job`, `Start_Lev` / `End_Lev`: class/level gating (quest group).
- No gamedef table stores accounts or characters (`users` is PySlayer's login table; unused). Persistence stays in accounts.json (a later option is `server_state.sqlite3`).

### 3.7 One-shot migration of the existing accounts.json (at load)
Existing records came from `_handle_create_character` as `class = s10, face = s1, top = s6, bottom = s5, shoes = s9`. Per character:
- `look` missing -> `look = [0, face, 0, 0, class, bottom, top, 0, 0, shoes, class, 0, 0, class]`. Without looks (TestHero): use the gender-0 defaults `[0,1,0,0,1,2,2,0,0,2,1,0,0,1]`.
- `class = 0, job2 = 0`, unless `MIGRATE_KEEP_CLASS` is set. TestHero (class 0) renders and plays today (LIVE_TEST_LOG), so Novice is safe.
- `exp = exp_for_level(level)`; add `created_at = 0`; default missing `str/dex/int/spr` to 3/2/1/3 (sum 9).

Per account, assign `account_id` as test = 1, admin = 2, then sequential. wsdev/wsview in-world detection and the combat driver assume uid 1 for `test`. Also set `gender = 0` (current behaviour) and `manner_points = 0`.

---

## 4. Current implementation status and proven bugs

| # | Severity | Opcode | Bug | Evidence (spec + server line) | Fix |
|---|---|---|---|---|---|
| B1 | crash_or_desync | 0x21/0x22 | `EXP_TABLE` holds the client's **per-level increments** but treats them as cumulative thresholds, and has 98 entries instead of 99. Server vs client level: 176 exp = 3 vs 2; 500 = 5 vs 4; 5000 = 11 vs 8; 30000 = 18 vs 12. The owner 0x22 then **forces** the wrong level. | Lines 148-166, 2010-2012. Exe 0x6F0C28/0x6F0EB0 identical, sum 1,200,382,231. FUN_00440DF0 decomp accumulates. Spec 0x02 `total_exp`, 0x21. | §3.5.1 |
| B2 | crash_or_desync | 0x03/0x21 | S2C 0x03 `exp_total` is hard-coded **30000** (comment "fame related"), while server exp starts at 0. The client exp baseline is Lv12 progress; +485 exp auto-levels the client to 13. It is re-sent on every portal. | Line 2045 (grammar: `u64 gold, u32 exp_total`); map change 1300-1302; spec 0x21 gates ("server's exp total must match the client's starting exp"). | F5: `exp_total = char.exp` |
| B3 | wrong_behavior | 0x21/0x07/0x02 | Exp is not persisted: `session['exp']` restarts at 0 each login. `ch['level']` is overwritten from that 0-based exp on the first kill after relog (level regression), and `_save_accounts` never runs for it. | Lines 1995-2003; save only at 2612. | `char.exp` is the truth; level derived; debounced save |
| B4 | wrong_behavior | 0x22 | The server sends 0x22 to the **owner** right after 0x21, so effect/sound 0x24 plays twice (at B1's wrong thresholds). | Spec 0x22 gates; lines 2006-2012. | Owner gets 0x21 only; 0x22 goes to observers |
| B5 | wrong_behavior | 0x02 | The byte after class_id is **job_branch** (+0x111), but the server writes `level` there. Level 1 labels class 1 "Berserker" and class 0 blank; level 2 uses the tier-2 row; level >= 3 indexes past the 21-entry name table. | Spec 0x02 references; line 2687; table bytes §3.5.5. | `job_branch = char.job2` |
| B6 | cosmetic | 0x02 | `total_exp` is sent as 0, so the select screen always shows Lv.1. The "6 fields" are 3 u32 ranks + 3 i32 rank changes. 16 trailing bytes are ignored. | Spec 0x02; lines 2688-2690, 2713-2714. | Send `char.exp`, ranks 0, no trailing bytes |
| B7 | wrong_behavior | 0x0E/0x02/0x07 | `class = look_slot10` (1..4, or 101..104 when gender != 0). Characters are born Warrior/Monk/Archer/Rogue instead of Novice. A class > 6 makes the HP/MP recompute (FUN_00427D40) return early and indexes past the name table. | Spec C2S 0x0E (`+0x3C0` = look slot 10); 0x1C decomp leaves +0x110 = 0; lines 2585, 2603, 2686, 2325. | `class = 0`; store `look` (§3.5.3); migrate (§3.7) |
| B8 | wrong_behavior | 0x02/0x07 | Look array mis-slotted: constant 123 in slot 1, face -> 2, top -> 4, bottom -> 5, shoes -> 6, weapon -> 11. The client's own layout is `[0,s1,0,0,s10,s5,s6,0,0,s9,s10,0,0,s10]`. TestHero has no look keys, so 0x07 sends zeros. Probable cause of LIVE_TEST_LOG bug #8 "Character renders unclothed". | 0x1C decomp (OnReceive lines 2344-2350); lines 2694-2711, 2332-2346. | Send `char.look` verbatim (keep the equip-grid/weapon merge with the item group) |
| B9 | wrong_behavior | 0x07/0x04 | 0x07 sends the four base stats as **0** (commented "equip-appearance / dye ids"), `karma = 1` and `gender = 0`. In world the status window shows 0/0/0/0 and 9 free points; allocations are lost at every spawn. | Spec 0x07 grammar `stat_str..stat_tol`, `karma`, `gender`; lines 2308, 2329, 2349. | Send `char.str/dex/int/spr`, `account.manner_points`, `account.gender` |
| B10 | wrong_behavior | 0x04/0x14 | `_handle_set_stats` replies with a hard-coded `value=4`. The value is absolute, so the stat is overwritten. No free-point check, no persistence, and indexes > 3 are answered too. | Spec 0x14 gates; lines 957-966. | F7 |
| B11 | crash_or_desync | 0x12/0x1F | C2S 0x12 is not dispatched, so the client stays on "Waiting for the server to response." forever (it must restart). | Spec C2S 0x12 ("modal waiting box that needs S2C 0x1F"); `_dispatch` 601-667 has no 0x12 branch. | F4 |
| B12 | wrong_behavior | 0x0E/0x1C | No name validation, uniqueness, 5-character cap or stat-sum check, and no 0x1C error replies. Duplicate names break the 0x2B lookup (first match wins, 710-713) and every later name-addressed feature. | Lines 2581-2617. | F3 |
| B13 | crash_or_desync | 0x01/0x02/0x07 | Every login gets `account_id = 1`, and 0x07/0x1D/0x22 take `char.get('uid', 1)`. A second client registers the same uid as its own local player, which blocks multiplayer. No duplicate-login handling (result 4) and no online index. | Lines 2656, 2307, 1623, 2011; project_multiclient.md; world_movement_npc.md B12. | §3.1 account_id; F2 2f/2g |
| B14 | crash_or_desync | 0x2B | Unknown character name: `return` with no reply. With the patched exe (timer-2 case removed from jump table 0x43ED30) the wait dialog never clears. | Lines 715-717; exe bytes 0x43ED30[0] = 0x43E9DB (stock 0x43E851); decomp FUN_0043E3F0 case 2. | F5 step 2: re-send 0x02 |
| B15 | wrong_behavior | 0x02/0x07 | `account_gender_flag` is hard-coded 0. Equip gate FUN_0044C2C0: items with Gender 1 ("(M)", 491 items) need flag != 0 -> "You can not equip other gender's item.". | Spec 0x02 field; decomp FUN_0044C2C0 lines 19-24; line 2677. | Per-account `gender` (confirm polarity, Q2) |
| B16 | wrong_behavior | 0x01 (S2C) | Game IP hard-coded to 127.0.0.1 and user_count always 0, so no remote client can connect. | Line 333 (`game_ip_host_order = 0x7F000001`), 338. | Config `PUBLIC_IP` / channels (F1) |
| B17 | wrong_behavior | all persistence | `_save_accounts` truncates accounts.json in place with no lock, while every connection thread mutates `self.accounts`. A crash mid-dump, or two concurrent creates, can corrupt or lose the file. | Lines 405-409, 2611-2612. | Atomic tmp+`os.replace` under a lock (§3.4) |
| B18 | cosmetic | 0x14/0x2B/0x02 | Misleading docs/dead code: 0x14 called monster HP (88, 1817); unused `_build_opcode_14` (1930); 0x2B docstring "10.5.0.2 / 0xA79B magic" (689-695; really str[16] ip + u32 port); 0x02 docstring labels (2663-2672); game port "7012" (10, 286, 364, 376). | Specs 0x14, C2S 0x2B, 0x02. | Fix while touching the code |
| B19 | cosmetic (tooling) | 0x58/0x74 | `wsproto.Grammar.encode` treats client-state conditions (`find_entity_by_uid(uid) != null`, `entity_with_uid_exists`) as false, so `wsdev.py sendspec 58/74` emits **only the 4-byte uid**. `sendspec` has no way to pass `assume`. Verified by running the encoder: `58 -> 01 00 00 00`, with assume `01 00 00 00 01 00`. | wsproto.py 293-298; wsdev.py 432-449. | Builders pass `assume`; add `--assume` to sendspec; use raw `send` meanwhile |

**Correct today, keep:**
- 0x5A framing (490-503);
- the mid-flow 0x01 ignore (2629);
- the login NUL split (2639-2640);
- the 0x02 success byte layout (13+75n) and error codes 0x0B/0x11;
- 0x0E parse offsets (2585-2591);
- the 0x2B name at payload[25:42] (703);
- `_send_exp` 0x21 packing `<i` and `_send_level` 0x22 packing `<IB`.

---

## 5. Implementation plan

| id | P | Effort | Depends on | Opcodes | Work |
|---|---|---|---|---|---|
| lc-exp-table | P0 | S | none | 0x21, 0x22, 0x02, 0x07 | Replace `EXP_TABLE` / `_level_for_exp` (148-166) with a 99-entry `EXP_INC` + cumulative `level_for_exp`, `exp_for_level`, `EXP_MAX`, `stat_total()` (§3.5.1-2). Unit test against the exe values: Lv2 = 56, Lv3 = 177, Lv13 = 30485, Lv99 = 1200382230, stat_total(99) = 471. |
| lc-data-model | P0 | M | none | 0x02, 0x07, 0x0E | Account/character schema (§3.1-3.2), load-time migration (§3.7), `name_index`, `accounts_lock`, atomic `save()` with dirty flag + debounce, save on disconnect. Fixes B17. |
| lc-exp-persist | P0 | M | lc-exp-table, lc-data-model | 0x21, 0x22, 0x03, 0x07 | Rewrite `_send_exp` as `grant_exp` (F6). 0x03 `exp_total = char.exp` in **both** enter-world and map-change (2045). 0x07 level from exp. No owner 0x22. Fixes B1-B4. |
| lc-charlist | P0 | S | lc-data-model, lc-codec | 0x02 | Rebuild `_build_login_success` (2661-2717) from the 0x02 grammar: gender, manner, job_branch, total_exp, ranks 0, `char.look`, transfer_status 3, max 5 chars. Fixes B5, B6, B8 (select screen), B15. |
| lc-spawn-fields | P0 | S | lc-data-model, lc-exp-persist | 0x07, 0x03 | In `_build_en_opcode_07` (2268-2397): uid = account_id, karma, job/job2, derived level, gender, `char.look` (merged with the item group's equip/weapon), stats. Coordinate with world_movement_npc (same builder). Fixes B8, B9. |
| lc-codec | P1 | S | none | all in group | `spec_packet(key, rec, assume=None)` / `spec_decode(key, payload, allow_trailing=True)` over `wsproto.Grammar`, loading `protocol_spec.json` once. 0x58 builder passes `{'find_entity_by_uid(uid) != null': True}`; 0x74 passes `{'entity_with_uid_exists': True}`. Add `--assume '{json}'` to `wsdev.py sendspec`. Fixes B19. |
| lc-create | P1 | M | lc-data-model, lc-codec | 0x0E, 0x1C | F3 validation, 0x1C results 1/2/3/4, Novice class, look composition, `created_at`, START_MAP config. Keep the 0x02 reply behind `CREATE_REPLY_0x02` until T-0E passes. Fixes B7, B12. |
| lc-delete | P1 | S | lc-data-model, lc-codec | 0x12, 0x1F | New `_dispatch` branch + F4, `DELETE_MIN_AGE_HOURS`. Fixes B11. |
| lc-stats | P1 | S | lc-data-model, lc-exp-persist | 0x04, 0x14 | F7 validation, persistence, absolute reply (replaces 957-966). Fixes B10. |
| lc-enter-world | P1 | S | lc-data-model, lc-codec | 0x2B, 0x02 | Grammar decode; store refuse/p2p; not-found -> 0x02; set `session.char/level/in_world`; register `online_by_name`. Fixes B14. |
| lc-uid-online | P1 | M | lc-data-model | 0x01, 0x02, 0x07, 0x22 | Per-account uid everywhere (`_send_level` default, `_build_en_opcode_1D*`, combat driver's uid==1 assumption kept only for account 1); `online_by_account` / `online_by_name`; cleanup in the finally block (578); duplicate login -> old socket closed + result 4. Fixes B13. |
| lc-version-config | P1 | S | none | 0x01 | Config file: PUBLIC_IP (host-order u32), channel list (<= 10), notice text, maintenance -> version_code 0xEA61, live user_count per channel. Fixes B16. |
| lc-class-change | P1 | M | lc-data-model, lc-codec | 0x58, 0x18 | `apply_job_item` with the §3.5.5 table; owner 0x18 with the real wallet; observers 0x58 (assume flag); GM `/job <item>`; configurable NPC/quest hook; persistence. |
| lc-login-errors | P2 | S | lc-uid-online | 0x01, 0x02 | Codes 0x05/0x06/0x0E/0x13; `AUTO_REGISTER`, `MAINTENANCE`, `MAX_ONLINE` flags; salted password hash at rest with plaintext migration. |
| lc-level-broadcast | P2 | S | lc-exp-persist, lc-uid-online | 0x22 | On a level change send 0x22 to other in-world sessions on the same map. |
| lc-player-info | P2 | M | lc-uid-online, lc-codec | 0x2A, 0x52, 0x53 | F9; equip grid + options from the item group (`session['equip_grid']` exists). Clamp entries/options. |
| lc-harness-charselect | P2 | S | none | 0x0E, 0x12, 0x1C, 0x1F, 0x02, 0x65, 0x66, 0x8C | Tooling: `wsdev.py up --select` (stop auto_login before the slot click), recorded coordinates for Create/Delete/OK/+ buttons, and `wsview.py rclick` (right-click for popup 0x50, as also asked for in social_friend.md). |
| lc-rename | P3 | S | lc-uid-online, lc-create | 0x74 (+0x49, 0x73) | F10, together with premium_cash's 0x49/0x73. |
| lc-menti-exp | P3 | S | lc-exp-persist | 0x7F | F6 step 4. The id space must match social_friend's 0x7B/0x7E `mentee_uid` (use account_id). |
| lc-id-transfer-stub | P3 | S | lc-codec | 0x65, 0x66, 0x8C | Log and reply 0x8C result 6 (F11). |
| lc-classname-patch | P3 | S | none | 0x02, 0x52, 0x58 | Optional exe patch: rewrite the 7 x 15-byte tier-2 row at VA 0x70C588+14*15 as `"", Paladin, Counter, BeastMaster, Trapper, Summoner, DarkPriest`, so tier-2 names match the job items. Apply to both patched exes. |

Suggested order:
1. lc-exp-table -> lc-data-model -> lc-exp-persist -> lc-charlist -> lc-spawn-fields. One restart then fixes the leveling desync, the unclothed look and the zero stats.
2. lc-codec -> lc-enter-world -> lc-delete -> lc-create -> lc-stats -> lc-class-change.
3. The multiplayer items (lc-uid-online first).

---

## 6. Live test plan

**Conventions**
- Run from `WindSlayer2Game/server`. `python wsdev.py up` stops stale processes, starts the server, launches the client non-elevated and auto-logs in `test` (uid 1) to the world.
- For **character-select tests** until lc-harness-charselect exists: `wsdev.py up`, then `wsdev.py down`. Start the server manually (`python windslayer_server.py` in the background) and launch `WindSlayer_patched.exe` with `__COMPAT_LAYER=RunAsInvoker`. Drive with `wsview.py`:
  - launcher START `click 100 235` (wait 3 s first);
  - ID `click 215 66` then `key t e s t`;
  - PW `click 253 97` then `key t e s t`;
  - Channel `click 250 193`, OK `click 189 468`;
  - character slots x = 110/222/330/440 at y = 470; START `click 703 507`.
  Locate Create/Delete/+ buttons with `wsview.py shot` first; they are not recorded yet.
- Inject with `wsdev.py sendspec <op> '<json>'`, or raw `wsdev.py send <op> <hex...>`. Injection reaches **every** live session.
- One experiment per restart (feedback_experiments.md).

| Test | Opcode (dir) | Prerequisite | Action | Expected | Risk |
|---|---|---|---|---|---|
| T-01v | 0x01 S2C | server restart | Change the notice/IP config (after lc-version-config), `wsdev.py restart`, `wsview.py shot` on the launcher. **Do not inject** 0x01: the game socket would run ParseChannelList. | Notice text in launcher control 0x403; START blinks. Maintenance config (0xEA61): notice popup, disconnect message, client exits after about 5 s. | safe |
| T-5A | 0x5A S2C | none | Observe only: `wsdev.py up`, `wsdev.py logs 80`. | `[FIREWAY] seed=...`, then C2S 0x01 (62B) about 1 s later. Do not inject 0x5A mid-session (re-key and a 1 s stall). | safe |
| T-01c | 0x01 C2S | login screen | Type test/test as above, `wsdev.py cap 6 click 189 468`. Repeat with password `bad`. | Good: `C2S 0x01 payload=62B 74 65 73 74 00 ...` then `S2C 0x02` (13+75n B). Bad: `S2C 0x02` 1B `11`, dialog "Enter the correct password", socket closed. | safe |
| T-02a | 0x02 S2C | lc-charlist deployed | Log in and `wsview.py shot` the select screen; click each slot. | Novice labels (not "Berserker"); Lv from exp; clothes visible; stats panel ranks "No Data.". | safe |
| T-02b | 0x02 S2C | char select | `sendspec 02 '{"result":1,"account_id":1,"account_gender_flag":0,"char_count":1,"repeat[char_count]":[{"name":"Probe","class_id":1,"job_branch":2,"total_exp":177,"repeat[14]":[{"appearance":0},{"appearance":1},{"appearance":0},{"appearance":0},{"appearance":1},{"appearance":2},{"appearance":2},{"appearance":0},{"appearance":0},{"appearance":2},{"appearance":1},{"appearance":0},{"appearance":0},{"appearance":1}]}],"transfer_status":3}'` (88B), then `wsview.py state`, `shot`, and click Probe. | Select map reloads; Probe "Lv.3", class label "Counter" (proves the tier-2 off-by-one); body drawn with the default look (answers Q1 in part). `state` shows whether the old entities remain (Q3). | state_change |
| T-02e | 0x02 S2C | char select | `wsdev.py send 02 05`. | Socket closed; "This ID is blocked due to excessive abuse..." dialog; relaunch needed. | disruptive |
| T-0E | 0x0E C2S | char select, fewer than 5 chars | Create; name `Probe1`; spend 9 points (3/2/1/3); `wsdev.py cap 4 click <OK>`. | `C2S 0x0E payload=35B 01 00 01 00 02 00 02 00 02 00 50 72 6F 62 65 31 00... 03 00 02 00 01 00 03 00` (default looks, gender 0). Today: S2C 0x02 (list reload). After lc-create: `S2C 0x1C 01`, new slot without map reload; accounts.json has class 0 and a look array. | state_change |
| T-0E-neg | 0x0E C2S | char select | Name `wind1` (client-blocked, expect no packet). Then name `test` (exists on admin). | `wind1`: "Invalid name." and no C2S. `test` after lc-create: `S2C 0x1C 02` "The name is already being used.". | safe |
| T-1C | 0x1C S2C | char select | `sendspec 1C '{"result":2}'`, then `'{"result":4}'`, then `'{"result":3}'`. | Popups "The name is already being used." / "Invalid name." / "Server process is running... (DB)". Result 1 only makes sense right after a real Create click. | safe |
| T-12 | 0x12 C2S | throwaway character exists | Select it (wait for it to walk forward), Delete, type the account password in dialog 0x201, `wsdev.py cap 4 click <OK>`. | `C2S 0x12 payload=38B` = name[17] + pw[21]. **Today no reply; the modal sticks (proves B11).** After lc-delete: `S2C 0x1F 01`; entity removed; later ones shift left; gone after relog. "no SS# in this ID" instead means scene+0x218 == 0 (Q4). | state_change |
| T-1F | 0x1F S2C | char select | `sendspec 1F '{"result":3}'`, `'{"result":12}'`; then select a character and `'{"result":1}'`. | 24-hour message; "Invalid password..."; result 1 removes the selected entity client-side only (relog restores it). | state_change |
| T-2B | 0x2B C2S | char select | `click 110 470`, then `wsdev.py cap 8 click 703 507`. After lc-enter-world, also try a character whose name was removed from accounts.json. | `C2S 0x2B payload=42B 00 00 00 00 00 31 32 37 2E 30 2E 30 2E 31 00... 9B A7 00 00 <name>`, then S2C 0x03 (64B) and 0x07. Unknown name: S2C 0x02 and back to select (not a hang). | safe |
| T-04 | 0x04 C2S | in world, free points > 0 | Open the status window (key via shot), `wsdev.py cap 3 click <STR +>`. | `C2S 0x04 payload=1B 00` -> `S2C 0x14` 7B. Today STR becomes 4 on every click (B10). After lc-stats: +1, free -1, persists over relog; at 0 free the buttons grey out. | state_change |
| T-14 | 0x14 S2C | in world | `sendspec 14 '{"uid":1,"stat_index":3,"value":30}'`, `wsdev.py status`; then `'{"uid":1,"stat_index":7,"value":1}'`. | SPR shows 30, max HP changes in `status`, free points drop (negative allowed). Index 7 writes nothing but redraws. Relog restores. | state_change |
| T-21 | 0x21 S2C | in world; before and after lc-exp-persist | Before: `sendspec 21 '{"exp_delta":500}'` then `status`. After (char exp 0): `'{"exp_delta":60}'`. | Before: jumps to **Lv 13** (proves B2). After: "+60 experience points", one level-up effect, Lv 2, full HP/MP. Injection does not update server exp, so relog to resync. | state_change |
| T-21neg | 0x21 S2C | in world, low exp | `sendspec 21 '{"exp_delta":-100}'`. | "Lost (-100) EXP."; the exp bar clamps at 0; level-down only if a threshold is crossed. | state_change |
| T-22 | 0x22 S2C | in world | `sendspec 22 '{"uid":1,"level":5}'`, `status`; then `'{"uid":1,"level":0}'`. | Effect + sound, Lv 5 in `status`, HP/MP full; the status window "Lv" label stays stale. level 0: nothing. | state_change |
| T-22dup | 0x21+0x22 | in world, map 102 | Before B4 is fixed: `hold s 450` on Pupus until the server logs `[LEVEL]`. | Level-up effect/sound plays twice. After the fix: once, at the client's threshold (56 exp = Lv2). | safe |
| T-58 | 0x58 S2C | in world as a Novice | Raw (B19): `wsdev.py send 58 01000000 01 00`; open the character window (shot). Then `send 58 01000000 01 02`; then unknown uid `send 58 39050000 01 00`. | Class "Warrior" and the HP/MP recompute; tier 2 labelled "Counter" (off-by-one); unknown uid ignored. Relog restores. | state_change |
| T-18job | 0x18 S2C (class change) | in world as a Novice; wallet known from logs | Before lc-class-change: `sendspec 18 '{"gold":999999,"victy":999999,"item_id":180,"count":1}'`. After: GM `/job 180`. Repeat once more. | Popup "Warrior is your new class."; no bag entry and no "you've received" line; the second attempt does nothing (FUN_004249E0 needs class 0). | state_change |
| T-2A | 0x2A C2S | in world | Inject an off-scene party member: `sendspec 4F '{"member_name":"Ghost","member_uid":2,"hp_max":100,"hp_cur":80,"mp_max":50,"mp_cur":25}'`; click party frame 1 (shot) -> popup 0x50 -> `wsdev.py cap 3 click <Info ctrl 3>`. (Alternative: spawn Ghost uid 2 with world T-05-1, right-click after lc-harness-charselect, despawn with `send 06 02000000`, then Info.) | `C2S 0x2A payload=17B 47 68 6F 73 74 00...`. Today: no reply. After lc-player-info: `S2C 0x53` "Ghostis not in server.". Clean up with `sendspec 51 '{"member_uid":2}'`. | state_change |
| T-52 | 0x52 S2C | in world | `sendspec 52 '{"char_name":"Ghost","manner_points":-5,"level":12,"rank":3,"job1":1,"job2":1,"stat_str":20,"stat_dex":10,"stat_int":5,"stat_spr":18,"equip_count":1,"repeat[equip_count]":[{"item_id":179,"option_count":0,"option_last":0}]}'` (39B). | Window 0x72: "Ghost", -5 in red, "Lv.12", "Berserker", " Not in the Guild", 20/10/5/18, rank label from table 0x70BDC8 index 3, one Wooden Stick. Never test equip_count > 25 or option_count > 5. | safe |
| T-53 | 0x53 S2C | in world | `sendspec 53 '{"char_name":"Ghost"}'`. | System chat "Ghostis not in server." (colour 0xFFFD4C87). | safe |
| T-74 | 0x74 S2C | in world | Raw (B19): `wsdev.py send 74 01000000 52656E616D6564 00000000000000000000` (21B); then `wsview.py state` and `shot`. Unknown uid: `send 74 39050000`. | Local entity name "Renamed" in `state` and the stat window; overhead name refresh is unverified (open question in the spec). Unknown uid: nothing. Relog restores. | state_change |
| T-7F | 0x7F S2C | in world (after 0x03) | (a) `sendspec 7F '{"exp_delta":10,"menti_id":7}'` (8B). (b) `sendspec 7E '{"count":1,"repeat[count]":[{"channel":100,"mentee_name":"Pupil","mentee_uid":7}]}'`, then repeat (a). | (a) Exp bar +10 with **no** chat line. (b) "You've received (+10) experience points.(Menti[Pupil])". | state_change |
| T-65/66 | 0x65/0x66 C2S | char select | Inject T-02b with `"transfer_status":2`; open ctrl 5 (shot), type `newid`, `cap 3 click <OK ctrl 1>`. Repeat with `"transfer_status":1,"transfer_text":"old"`. | status 2 -> `C2S 0x66 payload=33B 6E 65 77 69 64 00...`; status 1 -> `C2S 0x65 33B` and label "oldApplying". Status 1/2 skips the scene+0x218 hide branch, so ctrl 5 should be visible. If it is not, record it: the hide logic differs from the spec. | state_change |
| T-8C | 0x8C S2C | char select | `sendspec 8C '{"result":3}'`, `'{"result":2,"new_id":"newid"}'`, `'{"result":9}'`. | "same ID as present..." popup; window 0x12 ctrl 7 "newidApplying" + success popup; 9 ignored. | safe |

**Multiplayer tests** (after lc-uid-online):
- Run `wsdev.py up --client 2 --user admin --pass admin` (uid 2) next to client 1.
- Log `test` in twice: the second login gets result 4, and the first client drops.
- Kill a Pupu on client 1: client 2 sees one 0x22 effect on uid 1 (lc-level-broadcast).
- `/job 180` on client 1: client 2 receives 0x58 (6B).
- Client 2 right-clicks client 1 in town: window 0x72 fills locally with no 0x2A. Move client 1 to map 102 and use Info from a party frame: 0x2A -> 0x52.

---

## 7. Open questions

1. **Look-slot semantics.** Which of s1/s5/s6/s9/s10 are hair/face/top/bottom/shoes, and does the default look `[0,1,0,0,1,2,2,0,0,2,1,0,0,1]` render clothed? The server stores raw slots, so naming is cosmetic. Answer with T-02b plus cycling each create arrow and reading scene+0x3AC..+0x3C6.
2. **Gender polarity.** The equip gate says Gender 1 "(M)" items need flag != 0, so != 0 = male. Then the 101-based create looks are the male set, which contradicts the spec's "101 = female" guess from `hs/face101`. Verify: 0x02 with flag 1 -> Create -> compare models, then equip an "(M)" item.
3. Does a success 0x02 at char select free the previous select entities (map reload FUN_00406FD0 / scene reset), and do FUN_00422120/FUN_00422B90 keep scene+0x14 right after 0x1C/0x1F? This decides the create reply (0x1C alone vs 0x02).
4. The value of scene+0x218 in our setup (MD5(account) vs scene+0x131; the source of +0x131 is untraced). It decides whether Delete opens the password dialog and whether the transfer controls show.
5. Resolved: timer 2 (stock: "No response" + Back; patched: no-op) and timer 7 (WM_CLOSE). Still open: is the patched no-op worth keeping once every 0x2B is answered? Restoring 0x43E851 would give users a way out of a stuck load.
6. **Retail class-change source.** No gamedef quest or NPC item list grants 180/203-207/3076-3088, and items.Lv is 0. Was it an NPC dialog sending an untraced C2S, an EN-only quest, or a cash/shop item? What level requirement (retail WindSlayer: first job around Lv10)? Needs NPC dialog RE (hs NPC scripts) or community knowledge.
7. What rank_1..rank_3 in 0x02 are (level/arena/battlefield?), and the fame -> rank thresholds (table 0x6F11D8) needed for 0x52 `rank` and 0x07 `rank_icon`.
8. Is manner/karma per account (0x02 reads it before character choice) or per character (0x07 +0x15D8, 0x52)? Modelled per account for now.
9. The id space for 0x7F `menti_id` / 0x7B `mentee_uid` / friend ids: account uid (= in-world uid) or a separate character id? Agree with social_friend before lc-menti-exp.
10. Confirm with T-21 (after the fix) that 0x21 alone sets +0x99, so 0x22 stays observer-only.
11. The server-side HP/MP mirror after a client-side level-up needs FUN_00427D40 / FUN_00427F40 (per-class tables 0x6F149C / 0x6F142C). Owned by combat.
12. Name charset: the client allows CP949 bytes and its Curse_Engine list is unknown. Is ASCII-only acceptable for English-patch users?
13. Which 0x8C result codes the original server returned for 0x65 vs 0x66 (low value).
14. Multiple channels: the port is hard-coded, so channels need distinct IPs. Must 0x03 `channel_id` match the channel picked in the launcher (friend/mentor "same channel" logic)?
15. Does the lc-classname-patch row shift break anything that expects the shifted layout? Every consumer seen (0x02 panel, 0x52, FUN_00440BC0 popup) uses `(tier*7+class)*15`.
