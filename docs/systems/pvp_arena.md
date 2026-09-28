# pvp_arena: Arena rooms, Play&Chat rooms, Battlefield (Death Match)

Design doc for the server team. Scope: all 47 specs in `re_tools/corpus/systems/pvp_arena.json` (23 C2S send sites, 24 S2C handlers).
Evidence order: client binary (spec) > `LIVE_TEST_LOG.md` > `server/windslayer_server.py` > memory notes > PySlayer.
Grammar keys below are the `key` values in `protocol_spec.json` (S2C keys are the bare opcode, e.g. `0x2F`; C2S keys are `site/opcode`, e.g. `0x449384/0x18`).

`[UDP]` marks every step that depends on the arena UDP channel (client port 42907, dispatcher `FUN_00458640`, per-tick sender `FUN_00423260`, target `host_ip:room_no+10000` from S2C 0x2F). That channel is being specified in parallel. This doc only records what the arena needs from it.

---

## 0. Key findings (read first)

1. **The server is the "PVP server".** The client contains the room-host logic (round winner `FUN_00428090`, scoreboard `FUN_0041a230`, effect expiry `FUN_00417e10`), but it only runs when `scene+0xF24 != 0`. The EN binary never writes that flag non-zero (only `0x411FC3` writes 0), and `scene+0xF98` (the host socket manager) is only ever written as NULL (`0x440301`). So no client can host. The C2S 0x93/0x94/0x95/0x97/0x99 builders are dead code, and port 7022 will never receive them. Our server must play the host role. The popup text of S2C 0x5E ("Connection to PVP server was lost") confirms that retail used a separate PvP server.
2. **Arena, play-room and battlefield maps are "mode 1" maps, and the client stops simulating on them.** `map+0x70` is the `mode` XML attribute. It is `mode="1"` in `stage98_01..04` (9801-9804 arenas), `stage99_02` (9902 battlefield) and `stage99_03` (9903 play room), all present in `WindSlayer2Game/hs/`. In `FUN_0042b260` (per-tick), `FUN_00417e10`, `FUN_0042c940` (C2S 0x0D sender), `FUN_00412360`, `FUN_00413920`, `FUN_004158d0` and `FUN_00416380` all run only when `mode == 0` (verified in `0042B260_FUN_0042b260.c`: both simulation blocks test `[[this+0x4D8]+0x70] == 0`). On a mode-1 map the tick still runs `FUN_0042cf20` (portal-line check, not mode-gated) and `FUN_00423260`, which sends UDP opcode 3 (`u8 3, u32 uid, u8 keyA, u8 keyB`) to `scene+0xF38:room_no+10000`. It sends every tick while the local player is state 3 (playing) or before the first snapshot (`scene+0xF08 == 0`), otherwise only every 9 s (spectator keepalive). On field maps the same function loops its own snapshot and input to `127.0.0.1:scene+0x244` (the client is its own host there), which is why field play needs no server UDP. The snapshot header's top 14 bits must equal the map's `MapInfo name` code (`FUN_00458640` compares it with `map+0x1B8` and drops mismatches): 248/249/250/123 for 9801-9804, 253 for 9902, 1 for 9903. **All in-room movement, combat, HP and round phases arrive as UDP host snapshots** (`FUN_00458640` opcodes 7/8/9 snapshot, 6/0xC/0xE HP, 0x0A hit, 0x0B/0x0D/0x0F/0x10 MP). Without a UDP host, players in a room cannot move or fight. The TCP layer in this doc covers lobby, entry, rosters, teams, scores and results. `[UDP]`
3. **Every patched client exe NOPs the arena UDP send.** File offset `0x236E0` (VA `0x4236E0..0x4236FA`, 27 bytes: `push 1; add eax,10000; push eax; lea ecx,[esi+0xF38]; push ecx; mov ecx,[esi+0xF90]; call [0x4A410C] SendTo`) is `90`x27 in `WindSlayer_patched.exe`, `WindSlayer_p2.exe`, `WindSlayer_hires.exe` and `WindSlayer_patched.beforealive4.exe`. It is intact in `WindSlayer.exe` and `WSGame.exe`. `memory/project_status.md` calls it the "unicast SendTo to 10.5.0.2". That is wrong: it is the arena peer-to-host input send. Arena play needs a new exe variant without that patch. `[UDP]`
4. **C2S 0x38 is the Battlefield window request (T key), not a basic attack.** The server routes it to `_handle_melee` (`windslayer_server.py:648-652`), so pressing T hits the nearest monster (LIVE_TEST_LOG bug 1).
5. **The client soft-locks on a modal "Waiting for the server to response."** after C2S 0x18, 0x1A, 0x1B, 0x1C and 0x39, and shows the same text after 0x3A. Our server does not answer any of them (0x1A is consumed as a "mob query" at `:641-643`; the rest fall through to "Unhandled" at `:659`).
6. **The server's reply to C2S 0x63 is wrong.** It sends an empty S2C 0x63 (`:610-611`). C2S 0x63 is the card-deck request. S2C 0x63 is the Battlefield-window counter (`u16 + u8`), so every map load writes garbage "Join(?/12)" / "? Players" and raises two Fireway short-read errors.
7. **Two encoder bugs make the grammar tools emit bad arena packets.** (a) `wsproto._eval` (`wsproto.py:183-187`) treats hex literals in a compound expression as identifiers (`x0A31`), so `buff_id >= 0x0A31 && buff_id <= 0x0A3B` is always false. This was verified: an 0x2E entry with buff 0x0A32 encodes to 327 B instead of 331 B, a 4-byte desync. (b) Client-state conditions default to false unless `assume` is passed. `wsdev.py sendspec` (`wsdev.py:447`) passes none, so 0x37 entries encode as uid-only (9 B instead of 15 B for one entry) and 0x5B as uid-only (4 B instead of 24 B).
8. **All room kinds share one room-number space.** Join-by-number and world-entrance joins accept only 1..128 (0x81 is the cash-shop sentinel). The world-entrance join sends `room_kind=0` for every arena sub-type, so the server must derive the type from `room_no` alone. Use one pool, 1..128, for arena, play and battle rooms. The UDP host port is `room_no+10000` (10001..10128).

### 0.1 Cross-group dependencies (ids from the sibling design docs)

| Needed for | Owner doc / item | What this group relies on |
|---|---|---|
| Any multi-player room | `login_character.md` `lc-data-model` (B13) | Unique, persistent `account_id` per account = in-world uid (`scene+0x220`). Today every login gets uid 1 (`:2656`). |
| Broadcasts to other sessions | `party.md` M3 registry | `self.sessions_by_uid`, `session['in_world']`, `session['no_enc']` (last flag seen at `:557`), `self._send_to(target, opcode, payload)`, `world_lock`. This doc uses the same names instead of defining its own. |
| Return to field (leave, 0x5E/0xA3/0xA4) | `world_movement_npc.md` `world-maptransfer` | `MapTransfer()` primitive: 6-byte 0x08, then 0x03, 0x07, HP/MP, 0x1A. This group needs a parameter to replace the leading 0x08 with 0x5E/0xA3/0xA4 (same 6-byte body). |
| C2S 0x38 routing | `combat_skill.md` `cs-dispatch-fix` (B2) | Removes `0x38 -> _handle_melee`. This group adds the Battlefield handler in the same branch. |
| C2S 0x63 reply | card-deck owner (`quest_cards_misc` group, S2C 0x8A) | Stop answering C2S 0x63 with S2C 0x63 (B2 below). |
| Manner penalty (0xA4) | `social_friend.md` | S2C 0x97 MannerPointUpdate `{uid, i32 delta}`. |
| Fame / Victy / battle record in 0x03 | `world_movement_npc.md` (0x03 builder) | Emit persisted `fame`, `victy`, `battle_win/lose/ko/down` instead of the zeros at `:2046-2051`. |
| In-room movement and combat | parallel UDP channel spec | Snapshot, HP/MP, hit and round-phase datagrams (`FUN_00458640`). Every `[UDP]` step. |

---

## 1. System overview (as the client implements it)

### 1.1 Three sub-systems, one room model

| Sub-system | Open key / window | List request / reply | Create dialog | In-room HUD | Map(s) (`data/map_codes.json`) | `room_type` / `scene+0xF70` |
|---|---|---|---|---|---|---|
| Arena rooms | F / 0x2A (hotkey slot 10) | C2S 0x2C / S2C 0x33 (+0x32, 0x31) ; close C2S 0x2D | 0x2B (ctrl 3 = Create) | 0x47E (team buttons 0x7B blue, 0x7C red, 0x7D spectator, 0x7E exit), 0x4D, 0x480 | 9801 Popola, 9802 Ohaengsan, 9803 Amakusa, 9804 Balderan (`stage98_0x`, mode 1) | 0 = "Lv.N" level bracket, 1 = "ULMT", 2 = "STD", 3 = "Novice" |
| Play&Chat rooms | P / 0x478 (slot 12) | C2S 0x74 / S2C 0xA1 (+0xA0) ; close C2S 0x75 | 0x479 | 0x47A (ctrl 1 opens exit dialog 0x4B) | 9903 doll play room (`stage99_03`, mode 1, `playroom.hsi`). The client hardcodes 0x26AF | 4 |
| Battlefield / Death Match | T / 0x14 (slot 11) | C2S 0x38 / S2C 0x63 | none (queue) | 0x47E with team buttons greyed | 9902 mine battlefield (`stage99_02`, mode 1) is the only EN battle map. 9901 waiting room has no mode. 9904-9908 files are absent in EN `hs/` | >= 5 (5 = Death Match) |

Shared dialogs: 0x49 (arena enter-number), 0x47D (play-room enter-number), 0x4A (password), 0x4B (exit confirm), 0x133 (world-object/balloon join confirm), 0x17B (battlefield "Standing by"), 0x16 (generic message box, which holds "Waiting for the server to response.").

### 1.2 Client room state (scene = `*0x70EECC`)

| Offset | Written by | Meaning |
|---|---|---|
| +0xF36 | S2C 0x2F | round_count (max rounds; F5B wraps to 1 when equal) |
| +0xF37 | S2C 0x2F | round_time_min (countdown = F37*60 - elapsed seconds from UDP header bits 0-8) `[UDP]` |
| +0xF38 str[16] | S2C 0x2F | host_ip = UDP target of the per-tick input send |
| +0xF48 u16 | C2S join paths / S2C 0x2F | room_no (pending on join; "Room%u" label; UDP port base) |
| +0xF4A str[17] | S2C 0x2F | room title (window 0x47E ctrl 0x81) |
| +0xF5B..F5F | S2C 0x2F / 0x36 | current_round, blue wins, blue losses, red wins, red losses |
| +0xF60 | S2C 0x36 | last round result 0 tie / 1 blue / 2 red |
| +0xF61/+0xF62 | roster rebuild `FUN_00443280` | blue / red member counts (next-team). Used by the team-button gate |
| +0xF6C | derived in 0x2F | team-mode flag = 1 for mode 2, 3, 5 |
| +0xF70 | C2S join paths / S2C 0x2F | room mode/type |
| +0xF71/+0xF72 | S2C 0x2F / 0x37 | blue / red team kill score |
| +0xF73/+0xF74 | S2C 0x37 / 0x35 recount | blue / red alive counts |
| +0x27C / +0x280 | S2C 0x03 / 0x36 (+=) | fame (reputation) / Victy (`session['winnie']` on our server) |
| +0x284/288/28C/290 | S2C 0x03 / 0x36 | battle Win / Lose / KO / Down totals |
| +0xEE0 i32 | S2C 0x02 manner_points | < -19 blocks all room/battle entry |

Entity arena fields (entity = 0x15F0 bytes): `+0x14EB` team (0 spectator, 1 blue, 2 red), `+0x14EC` next team (3 = leave, converted to flag `+0x14E9`), `+0x14ED` kills/KOs, `+0x14EE` deaths/downs, `+0x14F0` score, `+0x98` state (3 playing, 1 spectator), `+0x15DC` acted-this-round (set by UDP input), `+0xE2` room_no, `+0xE1` room type, `+0xD0` str[17] room title (the room "balloon" over a player; shared with 0x07 spawn and 0x75 cash shop = 0x81).

### 1.3 Client-side gates the server must mirror

`FUN_00444400` (arena list join/create/number and battlefield subscribe) and `FUN_00444590` (play room: the same checks without the level rule) refuse the action, with a message, when:
- manner `scene+0xEE0 < -19` ("You can't use play room, battlefield, or arena due to your low manner point.")
- the current map is mode 1 (silent: already in a room)
- the map flea-market flag `map+0x18 != 0`
- **arena only:** local level `+0x99 < 5` ("Level 5 or above is allowed to join.")
- party-play window 0x75 is open, or battle standby window 0x17B is open ("already subscribed")
- the player is not standing still (`+0x904 != 8`, "Only able during stop motion.")
- the player is hunting (`FUN_00418A90`)
- an item stall or the cash shop is open, or `FUN_00468FA0 != 0`

Extra list gates for arena rows (`FUN_00446300` 0x2A case):
- A novice (`job1 +0x110 == 0`) may only enter type 3.
- A non-novice may not enter type 3.
- In a type 0 room, the player's level must lie in `[room_level-2, room_level+1]`.

Battlefield (window 0x14 ctrl 9) additionally needs a class change (`+0x110 != 0`).
Map-mode gate on the list windows: opening F/P/T on a mode-1 map sends nothing and shows no window.

### 1.4 Dead host code (never received; use as reference only)

| C2S key | What it is | Client-facing equivalent the server must send instead |
|---|---|---|
| 0x428244/0x93, 0x428884/0x93 (sub 1) | idle-reset and auto-balance team broadcast | S2C 0x35 per moved player |
| 0x4285DE/0x93 (sub 3) | remove player whose next_team == 3 | 0x2D to room + field warp for the leaver |
| 0x41AC20/0x94 | per-death scoreboard | S2C 0x37 (drop room_id and the per-entry `+0x1C` byte) |
| 0x4284D3/0x95 | round result | S2C 0x36 |
| 0x418294/0x97, /0x99 | effect/buff expired in room | none on TCP (the client's S2C 0x97/0x99 are unrelated opcodes) |

The server port of the host logic should follow `FUN_00428090` mode 2:
- **Winner.** If room_mode < 5 and only one team has living players, that team wins. Otherwise the team kill counters F71/F72 decide; equal is a tie.
- **Counters.** A blue win increments F5C and F5F; a red win increments F5E and F5D.
- **Idle reset.** A member with `+0x15DC == 0` in a room with mode <= 3 gets next_team = 0.
- **Auto-balance** (mode != 4): diff = to_blue - to_red; move players from the tail until |diff| <= 1.
- **Score bonus.** In modes >= 5, the winning team gets `+0xB4` added to score.

---

## 2. Request/response flows

Notation: `C2S 0xNN name {fields}` -> server logic -> `S2C 0xMM name {key values}`. "Modal" = message box 0x16 showing "Waiting for the server to response.", which only a server packet can close (0x34, 0x33/0xA1 prologue, 0x30/0xA2/0x34 error text, or 0x20 count 0).

### F1. Arena room list (F)
1. Client presses F. If window 0x2A changes to state 3/4 and the map is not mode 1, it sends `C2S 0x2C ArenaRoomListOpen {}` (0 bytes, live-captured). Nothing is awaited.
2. Server marks `session['list_sub'] |= ARENA`, then sends `S2C 0x33 ArenaRoomListReset {room_count=N, rows[] = {room_name, room_no, max_players, cur_players, room_level, room_type in 0..3, has_password}}` for every arena room (types 0..3, never 4 or >= 5). Split into 0x33 (first 255 rows) + 0x32 continuations if more than 255 (the 128-room pool makes that impossible).
   - Hazard: 0x33/0x32/0x31 dereference the local player (`FUN_00444770`). Send only in-world.
   - The handler also dismisses modal 0x16, so 0x33 doubles as a "close wait box".
3. While subscribed: on arena room create/delete/cur_players change, re-send the full `S2C 0x33` to subscribers. 0x31/0x32 append without de-duplication, so use them only for truly new rooms. The client keeps no per-room update path, so re-send the full list rather than a new row with the same room_no.
4. Client presses F again, clicks close, or is force-closed by S2C 0x08/0x5E/0xA3/0xA4/0x2F while the list is open. It sends `C2S 0x2D ArenaRoomListClose {}`. Server clears the subscription. **No reply** (a reply froze the client in an earlier experiment).

### F2. Play&Chat room list (P)
1. `C2S 0x74 PlayRoomListOpen {}` (0 bytes, live-captured) -> subscribe -> `S2C 0xA1 PlayRoomListReset {room_count, rows[] = {room_name, room_no, max_players, cur_players, room_level=1, room_type=4, map_id=9903, has_password}}`. `map_id` sits before `has_password` on the wire. The list filter tab 0x17 shows only `map_id == 0x26AF`. The 0xA1 prologue also dismisses 0x16. It does not dereference the player, so it is safe anywhere.
2. Push: re-send 0xA1 on play-room changes (0xA0 appends without de-duplication).
3. `C2S 0x75 PlayRoomListClose {}` (also auto-emitted by the 0x2F handler at `0x450BF5` if the list is open) -> unsubscribe, no reply.

### F3. Create arena room
1. Client window 0x2B ctrl 3 -> gate `FUN_00444400` -> `Curse_Engine_Clean(title)` (a failure shows "No cursing please" and sends nothing) -> `C2S 0x18 CreateRoom {room_title str[17], max_users u8, room_option u8, password str[5], map_id u16 in 9801..9804 (default 9801), room_type u8 in 0..3}` (27 B). Modal shown.
2. Server validation (in order):
   - Parse strings up to the first NUL (trailing bytes are raw edit-buffer garbage). Title 1..16 chars after strip, else use `"<char name>'s room"`.
   - `map_id` not in {9801..9804} -> force 9801. `room_type` > 3 -> 0.
   - Re-check the gates the server can see: `session` level >= 5, manner >= -19, not already in a room or queue.
     - **Refusal (no specific opcode):** `S2C 0x34 {result=6, room_type}` shows "Your level is not appropriate." and replaces the modal text.
   - Type 3 (novice room) ignores the password. A creator with class != 0 may not create type 3 (mirror of join rule 7): refuse with `0x34 {7, 3}`.
   - Room pool exhausted or arena limit reached -> `S2C 0x30 ArenaCreateFailed_TooManyChannels {}` (text replaces the modal, OK button enabled).
3. Create the Room:
   - `room_no` = lowest free in 1..128 except 0x81; `kind=arena`; `mode=room_type`
   - `room_level` = creator level (only meaningful for type 0)
   - `max_players = clamp(max_users, 2, 16)` (open question: dropdown values)
   - `round_count`, `round_time_min` from config; `room_option` recorded (open question)
   - `owner_uid`, `members = [creator]`
4. `S2C 0x34 RoomJoinResult {result=5, room_type}` closes the modal and flushes the arena list data. The creator's list subscription is dropped server-side (the client cleared its list).
5. Enter the room (F9).
6. Refresh lists for other arena subscribers (F1.3). Balloon broadcast (F18) if that model is confirmed.

### F4. Create play room
1. Window 0x479 ctrl 3 -> gate `FUN_00444590` -> `C2S 0x18 CreateRoom {title, max_users, room_option=1, password, map_id=0x26AF, room_type=4}`. Modal.
2. Server: same sanitising. No level rule. Limit reached -> `S2C 0xA2 PlayRoomCreateFailed_TooManyRooms {}`.
3. Success -> `S2C 0x34 {result=5, room_type=4}` (flushes the play list) -> F9 with mode 4 -> re-send 0xA1 to play-list subscribers.

### F5. Join from a list row (no password)
1. Arena list row click (window 0x2A ctrl 10..19) runs the gates and list rules (§1.3), then stores `F48=room_no` and `F70=row type`.
   - If the row is locked, the client opens dialog 0x4A instead (F8).
   - Otherwise it sends `C2S 0x1A ArenaRoomJoin {room_no u16, room_type u8}` (3 B). Modal.
   - Play list (window 0x478) sends the same layout with `room_type=4` (`C2S 0x1A JoinPlayRoom`).
2. Server `_join_room(session, room_no, kind_hint, password=None)`:
   - Room missing, or kind mismatch (hint 4 but room not a play room; hint 0..3 but room is a play/battle room) -> `0x34 {2, hint}` "Invalid room #.".
   - Battle rooms (mode >= 5) are never joinable by number or list -> result 2.
   - `room.password` set and `password is None` -> `0x34 {3, room.type}`: the client closes the modal and opens password dialog 0x4A.
   - Wrong password -> `0x34 {1, type}` "Incorrect password.".
   - `len(members) >= max_players` -> `0x34 {4, type}`. The client sets that row's cur = max in window 0x2A. It always searches the arena list, even for play rooms.
   - Arena only:
     - player job1 != 0 and room type 3 -> `0x34 {7,3}` "Only novice can enter a [novice-only room]."
     - job1 == 0 and type != 3 -> `0x34 {8,type}`
     - level < 5, or type 0 with level outside `[room_level-2, room_level+1]` -> `0x34 {6,type}`
   - Success -> `0x34 {5, room.type}` then F9.

### F6. Join by typed number
1. Arena list ctrl 9 opens dialog 0x49; OK (ctrl 2) runs gate `FUN_00444400`. If atol(text) is not in 1..128, the client shows "Please, enter the room number." and sends nothing. Otherwise: `F48=n`, `F70=0`, `C2S 0x1B RoomJoinByNumber {room_no, room_kind=0}`. Modal.
2. Play list ctrl 9 opens dialog 0x47D -> gate `FUN_00444590` -> `C2S 0x1B {room_no, room_kind=4}`.
3. Server: `_join_room(room_no, kind_hint=0|4)`. kind 0 means "any arena type 0..3"; derive the real type from the room. The client did NOT run the novice/level rules on this path, so results 6/7/8 matter here. A locked room -> result 3 (the client already holds F48/F70, so dialog 0x4A then sends 0x1C with `room_category=0` or 4).

### F7. Join from a world balloon (click a player whose `+0xE2 != 0`)
1. `FUN_0044c4b0` opens dialog 0x133: "[title] Enter the arena?" (type < 4), "Would you enter the play room?" (4), "Will you subscribe to join the battle?" (> 4), "Enter the cash shop?" (0x81).
2. Yes (ctrl 2):
   - kind < 4: gate `FUN_00444400`, `C2S 0x1B {room_no, 0}`
   - kind == 4: gate `FUN_00444590`, `C2S 0x1B {room_no, 4}`
   - kind > 4: `C2S 0x39 BattleSubscribe {battle_type=kind}` (F14)
   - number 0 or > 128 (except 0x81): nothing sent
3. Server: same as F6 / F14.

### F8. Password-protected room
1. Dialog 0x4A OK (ctrl 2) -> `C2S 0x1C JoinRoomWithPassword {room_no=F48, room_category=F70, room_password str[5]}` (8 B). Modal. Cancel (ctrl 4) sends nothing.
2. Server: `_join_room(room_no, kind_hint=room_category (0..3 treated as "arena"), password=first NUL-terminated <= 5 bytes)`. Compare exactly (case-sensitive), then continue as in F5.

### F9. Enter a room (server-driven; arena, play room or battle)
Precondition: 0x34 result 5 already sent (or battlefield summon F15).
1. Server bookkeeping:
   - Save `session['return_map']`, `session['return_xy']` (last field map and position)
   - `session['room'] = room_no`, `session['current_map'] = room.map_id`
   - `session['monsters'] = {}` (so `_memory_melee`/`_combat_driver` stop hitting field mobs)
   - Member record `{uid, team=0, next_team=0, kills=0, deaths=0, score=0, acted=False}`
   - For battlefield: team preassigned 1/2 and next_team = team
2. To the joiner: `S2C 0x2F RoomGameEnter`:

   | Field | Value |
   |---|---|
   | round_count | room.round_count |
   | round_time_min | room.round_time_min |
   | map_id | room.map_id |
   | host_ip | server's UDP address as a string reachable by this client (e.g. "127.0.0.1") `[UDP]` |
   | room_no | room_no |
   | current_round | room.round_no (1-based) |
   | blue_round_wins, blue_round_losses, red_round_wins, red_round_losses | F5C, F5D, F5E, F5F |
   | red_score, blue_score | red first, then blue |
   | room_mode | room.mode (0..3 arena, 4 play, 5 battle) |
   | room_title | room.title |

   47 B fixed. Effects:
   - Fade, then load `stage<map_id/100>_<map_id%100>.hmi`.
   - Free every entity, including the local player (`scene+0x970 = NULL`); `scene+0xF00 = 7`.
   - Close windows 0x16/0x2A/0x14/0x478/0x17B/0x236. The client may emit C2S 0x2D/0x75; consume them.
   - Open the mode-specific HUD: 0x4D/0x480/0x47E for arena, 0x47A for play, greyed team buttons for battle.
   - **Do not send 0x03 or 0x07 here.** 0x03 would load a field map from its map_code and free all entities again.
3. To the joiner, immediately after: `S2C 0x2E ArenaEnterRoster {player_count, rows[]}` listing **every member including the joiner**. Each row is the 0x07 character prefix (name, uid, karma, status_12=0, job1, job2, level, rank_icon, gender, 14 appearance parts, 4 stats, 16 x (item + 6 options), 9 extra ids, buff list, skill list) followed by `{arena_team, arena_next_team, arena_kills, arena_deaths, arena_score}`.
   - Effects: `RegisterLocalPlayer` makes uid == `scene+0x220` the local player; state 3 (team != 0) or 1 (spectator); HP/MP = client-computed max; `scene+0xF00 = 6`; HUD, camera, stat panel and "You are in blue team." refreshed.
   - **No positions on the wire** (entities sit at 0,0 until UDP snapshots place them). `[UDP]`
4. To every other member: `S2C 0x2C ArenaPlayerSpawn {same prefix without buff list, ..., skill list, team, next_team}` (313 + 2*skill_count B). This refreshes 0x47E and the roster. Never re-send an existing uid (no de-duplication; it would create a ghost).
5. `[UDP]` The client starts sending UDP opcode 3 input to `host_ip:room_no+10000`. The server host learns the member's UDP address from the packet source (the client does the same at `FUN_00423850`, storing `+0x6A` ip / `+0x80` port) and includes the member in snapshots.
6. Update list subscribers (cur_players changed).

### F10. Team / spectator selection (arena HUD 0x47E)
1. Client:
   - Blue (0x7B) / Red (0x7C) run the client-side balance gate on `F61/F62`; a block shows "The team you've selected have too many members..." and sends nothing.
   - Otherwise `C2S 0x1D ArenaTeamSubscribe {team=1|2}`. Spectator (0x7D) sends `{team=0}` with no gate.
2. Server:
   - Recompute next-team counts. Refuse silently if the move would make `|blue-red| > 1` (mirror the client gate; nothing to send on refusal, since the client did not show a modal).
   - Otherwise `member.next_team = team`, then broadcast `S2C 0x35 ArenaPlayerTeamUpdate {uid, team=member.team, next_team}` to all room members (6 B).
   - The client shows "You subscribed to enter blue team..." for the local uid and rebuilds the scoreboard.
3. If no round is running (room idle, or the member is still a spectator before the first round starts), apply immediately: `member.team = next_team`, then broadcast `0x35 {uid, team, team}`. `FUN_00428bc0` sets state 3 and refills HP/MP.
4. Hazard: never send 0x35 for a uid while that client is outside a room map. It rewrites `+0x98` (the local player is normally 3 or 4) and wipes buffs.

### F11. Leave the room
1. Exit dialog 0x4B opens from 0x47E ctrl 0x7E (arena/battle) or 0x47A ctrl 1 (play room, mode-1 map only):
   - ctrl 2 -> `C2S 0x1D {team=3}` = leave when the round ends
   - ctrl 3 -> `C2S 0x1D {team=4}` = immediate exit, no EXP/Victy. Disabled on battlefield.
2. Arena/battle, code 3: `member.next_team = 3`, then broadcast `0x35 {uid, team, next_team=3}` (the client converts it to leave flag `+0x14E9`). At the next round end (F12.3), the member is removed as in step 3.
   - A spectator (`team == 0`) has no round to finish: treat code 3 like code 4. `FUN_00442EE0` shows no leave text for team 0 anyway.
   - Known client text bug (not fixable server-side): a **blue** member with the leave flag set reads "You are in red team." because `FUN_00442EE0` jumps to the shared red label `LAB_00442ff1`. Do not file it as a server bug in live tests.
3. Code 4 (or code 3 when no round is running, or any code in a play room):
   - Remove the member, then send `S2C 0x2D ArenaPlayerDespawn {uid}` to the remaining members. Never send it to the leaver itself (it leaves a dangling `scene+0x970`).
   - `[UDP]` Drop the member from the host peer table.
   - Warp the leaver to the field with the world group's `MapTransfer(session, return_map, return_xy, first_opcode=0x08)` (pvp-warp-refactor; today the same sequence lives in `_handle_change_map`, `windslayer_server.py:1281-1320`, whose 0x08 body is 10 B instead of the 6-B grammar):
     1. `S2C 0x08 ChangeMap {map_code=return_map, game_time_ms}` (6 B)
     2. `S2C 0x03` for `return_map`
     3. `S2C 0x07` at `return_xy`
     4. `0x0A`, `0x28` HP, `0x44` MP, map mobs
   - The 0x08 handler closes 0x480/0x4D/0x47A/0x47E/0x4B and clears the PvP flags because the old map was mode 1.
   - Clear `session['room']`.
   - Room empty -> delete it and refresh lists. Owner left -> transfer ownership (only matters for moderation, F17).
4. TCP disconnect of a member: same as step 3 without the warp.

### F12. Scoring and rounds (arena mode <= 3, battle mode >= 5) `[UDP]`
1. The round lifecycle runs on the UDP host (retail messages come from `FUN_00458640`):
   - opcode 8 = round start: resets the alive counters, "The round will start in 5 seconds." then 4/3/2/1, "Fight!" (from header seconds).
   - opcode 7 = running.
   - opcode 9 = round end (`FUN_00428090(scene, 2)`; with F24 == 0 this only refreshes the client).
   - The TCP packets below are sent by the server's host logic at those moments.
2. On each kill in the server simulation:
   - Update victim `deaths += 1`, killer `kills += 1`, `score += points`, team kill counters (a red death gives blue +1 in F71, a blue death gives red +1 in F72), alive counts.
   - Broadcast `S2C 0x37 ArenaScoreUpdate {entry_count, red_team_score, blue_team_score, red_alive, blue_alive, entries[] = {uid, score, downs, kos}}` to the room.
     - Modes < 5: the client **adds** entry values (send deltas: only changed members, delta values).
     - Modes >= 5: the client **overwrites** (send absolutes).
     - List only uids spawned on the receiver (all members are): an unknown uid consumes only 4 bytes and misaligns the rest.
     - Encode with `assume={'entity_with_uid_exists_in_scene': True}`.
   - Modes <= 3 print "[Blue a:b Red]" alive counts.
3. Round end (the server port of the `FUN_00428090` mode-2 winner rule, §1.4):
   1. Update F5B..F5F.
   2. For each member, send `S2C 0x36 ArenaRoundResult`:

      | Field | Value |
      |---|---|
      | round_result | 0 tie / 1 blue / 2 red |
      | round_no | the round that just ended |
      | blue_round_wins, blue_round_losses, red_round_wins, red_round_losses | F5C, F5D, F5E, F5F |
      | reward_points | personal, additive: winners > losers > spectators = 0; leavers with code 4 = 0 |
      | total_downs, total_kos, total_wins, total_losses | persisted absolutes |

      Details:
      - Read only on a mode-1 map, 26 B.
      - The client adds reward to fame (`+0x27C`) and Victy (`+0x280`). The server must add the same amount to `char['fame']` and `session['winnie']`; otherwise the next S2C 0x18 (absolute Victy) resets it.
      - EXP is **not** applied by 0x36: also send `0x21 ExpDelta` (via `_send_exp`) if EXP is awarded.
      - The client then applies each entity's next_team (`FUN_00428bc0`), refills HP/MP and clears buffs.
   3. Process leavers (next_team == 3) with the F11.3 removal.
   4. Idle reset: members with `acted == False` (no non-idle UDP input this round) and mode <= 3 get `next_team = 0`, broadcast via 0x35.
   5. Auto-balance (mode != 4): broadcast `0x35 {uid, team, next_team}` for each moved member.
   6. Mode >= 5: when blue or red wins reaches 3, the client prints "Final winner: ..." and reopens 0x47E. After ~10 s the server warps everyone to the field (F11.3) and deletes the room.
   7. Otherwise start the next round (UDP opcode 8) `[UDP]`.

### F13. Battlefield window (T)
1. T -> window 0x14 opens. If the map is not mode 1 and at least 2 s have passed since the clock baseline, `C2S 0x38 BattlefieldInfoRequest {}` (0 B, live-captured). No modal.
2. Server: `S2C 0x63 BattlefieldQueueCounts {player_count u16 = players currently in battle rooms (or online), join_count u8 = len(queue) capped at 12}`. The client writes "N Players" (ctrl 4) and "Join(n/12)" (ctrl 9). **Always send all 3 bytes.**
3. **Do not** answer C2S 0x63 (card deck, sent after every map load) with S2C 0x63.

### F14. Battlefield subscribe
1. Window 0x14 ctrl 9 checks the class change ("You can use [Battlefield] after class change") and gate `FUN_00444400`, then `C2S 0x39 BattleSubscribe {battle_type=5}`. Or dialog 0x133 Yes with kind > 4 -> `{battle_type=kind}`. Modal.
2. Server:
   - battle_type != 5 (no EN map for others), level too low (config `BF_MIN_LEVEL`), class 0, already in a room -> `S2C 0x39 LevelNotAppropriate {}` (orange system text), then `S2C 0x20 {waiting_count=0}` to close the modal. 0x39 alone does not close 0x16.
   - Already queued -> just re-send `0x20 {len(queue), 0}`.
   - Else append to `bf_queue[5]`, then broadcast `S2C 0x20 DeathMatchWaitingListStatus {waiting_count=len(queue), unk_01=0}` to every queued session. The client updates "Join(n/12)" and "Standing by (n/12)", closes modal 0x16 and opens standby window 0x17B if it is closed.
3. 0x20 is ignored by clients on a mode-1 map (nothing read), so never queue players who are in rooms.

### F15. Queue full -> summon
1. The queue reaches `BF_MATCH_SIZE` (12 retail; configurable for tests). Broadcast `S2C 0x20 {waiting_count=12}` to the chosen players. **Send exactly 12 even if the dev match size is smaller**: only 12 starts the countdown. The client sets 5 s, shows "5sec. You will be summoned to the battle field in.", closes 0x14/0x17B and arms timer 10.
2. About 5 s later each client sends `C2S 0x3A BattlefieldSummonReady {}` (0 B) and shows "Waiting for the server to response.".
3. Server waits up to 10 s for all 0x3A. Players missing after the timeout are dropped with `0x20 {0}` (closes their box) and returned to the queue front or removed.
4. Create battle room: `mode=5`, `map_id=9902`, `round_count` config (retail shows first to 3 wins, so 5), teams auto-balanced by level (`team = next_team = 1|2`). For each player, run F9 (0x2F with room_mode 5, 0x2E roster with teams). The in-match simulation is `[UDP]`.
5. Remaining queue: broadcast `0x20 {new count}`.

### F16. Cancel subscription
1. Standby window 0x17B ctrl 3: the client hides the window first, then sends `C2S 0x3B BattleSubscribeCancel {}` (0 B). No modal.
2. Server: remove from the queue, then broadcast `0x20 {len(queue)}` to the rest. Send nothing to the canceller: a non-zero count would reopen 0x17B; 0 is harmless but unnecessary. If the canceller was in a 12-player summon batch, abort the summon for everyone by sending `0x20 {count}` again, which restarts the countdown when it is 12.

### F17. Forced return to field (server-initiated)
Each packet is followed by the F11.3 warp tail (0x03, 0x07, ...), with the 0x08 step replaced by:
- UDP host failure or room teardown: `S2C 0x5E ReturnToFieldPvpConnectionLost {map_code, game_time_ms}` (6 B). Popup "Connection to PVP server was lost." plus a full 0x08-style map change.
- Moderation, room deleted for a bad name: `S2C 0xA3 {map_code, game_time_ms}` to members.
- The creator of that room: `S2C 0xA4 {map_code, game_time_ms}` plus a manner penalty (manner update is `S2C 0x97 MannerPointUpdate`, social group). The popup does not change manner itself.

### F18. Room balloon over a player (optional; model unconfirmed, see Q1)
1. Send `S2C 0x5B PlayerRoomBalloonSet {uid, room_no != 0, room_type, room_title}` (24 B; always all 24; encode with `assume={'find_entity_by_uid(uid) != null': True}`) to field-map sessions that have `uid` spawned. The client draws sprite 0x87/0x19C with the title (only on mode-0 maps). Clicking opens F7.
2. `S2C 0x5C PlayerRoomBalloonClear {uid}` sets `+0xE2 = 0`.
3. 0x07 spawns carry the same state (`room_id`, then `room_type` + `room_title` only if `room_id != 0`). The server's 0x07 builder must emit them from the session instead of the hardcoded "marriage = 0".

---

## 3. Server state and data model

### 3.1 Session fields (add to the dict built at `windslayer_server.py:509-517`)
| Key | Type | Set by | Used by |
|---|---|---|---|
| `uid` (unique per account) | int | login (external dependency: login group, replaces `account_id = 1` at `:2656`) | every roster, 0x35/0x37/0x2D, UDP host |
| `no_enc` | bool | every received packet (`_handle_fireway:557`; party M3 owns this field) | `_send_to` broadcasts to other sessions (default True, as `_admin_listener:461` and `_memory_melee:1696` already use `use_by_array=True`) |
| `p2p_ip`, `p2p_port` | str, int | C2S 0x2B (`str[16]` at payload 5..20, `u32` at 21..24; `_handle_enter_world:700-705` currently drops them) | UDP host fallback address `[UDP]` |
| `list_sub` | set{'arena','play'} | 0x2C/0x2D, 0x74/0x75 | list pushes |
| `room` | int or None | F9 / F11 | dispatch gating, disconnect cleanup |
| `return_map`, `return_xy` | int, (x,y) | F9 | F11/F17 warp |
| `bf_queued` | int or None (battle type) | F14/F16 | F15 |
| `bf_ready` | bool | C2S 0x3A | F15 |
| `level`, `job1` | int | existing (`session['level']`, char `class`) | gates |

### 3.2 Room model (in-memory, `RoomManager` on `GameServer`)
```
Room: room_no (1..128, not 0x81), kind in {'arena','play','battle'}, mode (0..3 | 4 | 5),
      title (<=16 chars), password (<=5 bytes or ''), map_id (9801..9804 | 9903 | 9902),
      max_players, room_level, room_option (raw), owner_uid,
      round_count, round_time_min, round_no, f5c,f5d,f5e,f5f, blue_kills(F71), red_kills(F72),
      phase in {'idle','countdown','running','ended'}, phase_started,
      members: {uid: Member(team, next_team, kills, deaths, score, acted, udp_addr)}
```
Limits (config constants next to `MOB_RESPAWN_SECS`): `ARENA_ROOM_LIMIT=100`, `PLAY_ROOM_LIMIT=28`, `ROOM_MAX_PLAYERS=16`, `ARENA_ROUND_COUNT=3`, `ARENA_ROUND_MIN=3`, `BF_MATCH_SIZE=12`, `BF_ROUND_COUNT=5`, `BF_SUMMON_TIMEOUT=10`, `ARENA_MIN_LEVEL=5`.
Rooms are ephemeral (not persisted). Battlefield queue: `bf_queue = {5: [uid, ...]}`.

### 3.3 Persistence (accounts.json character dict)
| Key | Client field | Feeds |
|---|---|---|
| `battle_win`, `battle_lose`, `battle_ko`, `battle_down` | scene+0x284/288/28C/290 | S2C 0x03 (`_build_pyslayer_opcode_03:2048-2051` currently hardcodes 0), S2C 0x36 totals |
| `fame` | scene+0x27C (rank icon via table 0x6F11D8) | 0x03 (`:2046` hardcodes 0), 0x36 reward |
| `victy` (= `session['winnie']`) | scene+0x280 | 0x03 (`:2047` hardcodes 0), 0x18 (absolute), 0x36 reward |
| `manner` | scene+0xEE0 | S2C 0x02 manner_points; gate < -19 |
| `rank_icon` | entity +0x9A | 0x07 / 0x2B/0x2C/0x2E rows |

### 3.4 Content
- Maps: `server/data/map_codes.json` entries 9801-9804, 9901-9908. EN `hs/` contains `stage97_01`, `stage98_01..04` and `stage99_01..03` only.
- `gamedef.sqlite3.maps` has no portal rows for these maps (entry/exit is only through the packets above). The `npcs`/`items` tables are not needed. `items.PvPItem` could later restrict equipment in rooms (open question).
- Sprites/HUD: client-side only.

### 3.5 UDP host state `[UDP]`
Per room:
- one UDP socket bound to `(server_ip, 10000+room_no)`
- peer table `uid -> (ip, port)` learned from the source of input opcode 3
- per-entity simulation state (x, y, direction, action, hp, mp)
- tick timer (the client tick is 30 ms)
- round phase clock (header bits 0-8 = elapsed seconds)

The exact packet formats are owned by the parallel UDP spec. The observed layouts in the `FUN_00423260` and `FUN_00458640` decomps are:
- **Header:** `u32` = seconds (9 bits) | entity count (9 bits) | map xml code (14 bits).
- **Per entity (8 bytes):** x/2 (12 bits), y/2 (11 bits), direction (1), action-1 (6), hit flags (2); then a flag (1) and uid (25).
- **Input:** opcode 3 = `u32 uid, u8 keyA, u8 keyB`.

---

## 4. Current server status and proven bugs

| # | Opcode | Status / bug | Evidence (spec + code) | Fix | Severity |
|---|---|---|---|---|---|
| B1 | C2S 0x38 | Routed to `_handle_melee`: T damages the nearest monster (and ticks respawns) | spec `0x446628/0x38` (opcode-only Battlefield window request); `windslayer_server.py:648-652`, `:1634-1653`; LIVE_TEST_LOG bug 1 | route to `_handle_battlefield_info` -> S2C 0x63 {u16,u8} | wrong_behavior |
| B2 | C2S 0x63 / S2C 0x63 | Reply to card-deck request is an EMPTY S2C 0x63 -> two Fireway short reads plus garbage "Join(?/12)" / "? Players" labels after every map load | spec S2C `0x63` (u16+u8, "Always send all 3 bytes"); spec `0x44EF67/0x63` CardDeckListRequest; `:610-611` | stop replying (card_deck group owns the 0x8A reply); send full 0x63 only for C2S 0x38 | cosmetic |
| B3 | C2S 0x1A | Labelled "mob query", consumed with no reply -> modal soft-lock on any list-row join | spec `0x449384/0x1A`, `0x44B0AC/0x1A` (the only C2S 0x1A sites); `:641-643` | handle as room join -> 0x34 | wrong_behavior |
| B4 | C2S 0x18, 0x1B, 0x1C, 0x39 | Unhandled -> modal "Waiting for the server to response." never closes (create room, join by number, password join, battle subscribe) | specs `0x449384/0x18`, `0x449384/0x1B`, `0x449419/0x1B`, `0x4495F4/0x1C`, `0x449419/0x39`; `_dispatch` else-branch `:659` | at minimum refuse with 0x30/0xA2/0x34{2}/0x39+0x20{0} | wrong_behavior |
| B5 | C2S 0x3A | Unhandled -> client stuck on "Waiting for the server to response." after a 12/12 countdown | spec `0x43E965/0x3A`; `:659` | summon (F15) or `0x20{0}` | wrong_behavior |
| B6 | C2S 0x2C | `_handle_room_query` reads a non-existent code byte (`payload[0]` -> 0) and never sends 0x33 -> the list is always empty. Docstring "sent once at spawn" / "answering with 0x33 unprompted opens the arena UI" contradicts the handler (0x33 opens no window) | spec `0x44647A/0x2C` (0 B, live capture), S2C `0x33`; `:653-654`, `:1529-1533` | send 0x33 | wrong_behavior |
| B7 | C2S 0x2D | Consumed (correct) but labelled "scene-finalize / map-ready ack" | spec `0x446A93/0x2D`; `:655-658`; LIVE_TEST_LOG bug 2 | relabel; unsubscribe | cosmetic |
| B8 | C2S 0x74/0x75/0x1D/0x3B | Unhandled: play list always empty, team/exit buttons do nothing, queue cancel ignored | specs; `:659` | F2/F10/F11/F16 | wrong_behavior |
| B9 | C2S 0x2F | Labelled "arena query - ignoring"; it is the messenger friend-list request (social_friend) sent after every 0x03 | spec `0x44EF4F/0x2F`; `:630-631` | relabel (no arena meaning) | cosmetic |
| B10 | S2C 0x2F builder | `_build_map_enter_packet` is labelled "Opcode 0x2E (Map/Channel Enter)" but its 47-byte layout is S2C 0x2F. Mislabels: 16-byte "map name" = host_ip (UDP target), 17-byte "character name" = room_title, "local USHORT" = map_id, F36/F37 = round_count/round_time_min. Never called | spec S2C `0x2F`; `:900-939` | replace with grammar builder `0x2F` | cosmetic |
| B11 | S2C 0x2B/0x2E builders | `_build_enter_world_response` (claims the 0x4503E1 layout, pads to 2000) and `_build_pyslayer_enter_world` (KR 17-appearance/15-equip layout) do not match EN 0x2B/0x2E (321 B per bare row). Never called | spec `0x2B`/`0x2E` references; `:2410-2470`, `:2472-2570` | delete; build rows from grammar | cosmetic |
| B12 | S2C 0x07 +0xE2 | `_build_en_opcode_07` names the room-id gate "marriage" (always 0). Wire bytes are correct today, but the field is the room balloon (room_no, then room_type + room_title) | spec `0x5B` adjudication, `0x07` grammar; `:2310-2317` | rename; source from `session['room']` (F18) | cosmetic |
| B13 | C2S 0x2B | `_handle_enter_world` discards `p2p_ip`/`p2p_udp_port`; docstring layout is wrong (IP is str[16] at 5..20, port u32 at 21..24, not "0xA79B magic") | spec `0x42F904/0x2B`; `:686-705` | store in session | wrong_behavior (arena `[UDP]`) |
| B14 | all rosters | `session['account_id'] = 1` for every login -> two clients share uid 1. In 0x2E/0x2C the second player's row re-registers as the receiver's local player (`RegisterLocalPlayer` uid gate); 0x2D for "the other" uid 1 would free the receiver's own entity (dangling `scene+0x970`) | spec `0x2E`/`0x2D` hazards; `:2651-2656`; memory project_multiclient.md | unique uid per account (login group dependency) | crash_or_desync |
| B15 | client exe | All patched exes NOP the arena peer-to-host UDP SendTo at VA 0x4236E0-0x4236FA (file 0x236E0, 27 B). Memory note misattributes it to "SendTo 10.5.0.2" | asm `00423260_FUN_00423260.asm:312-318`; byte diff `WindSlayer.exe` vs `WindSlayer_patched.exe`/`_p2.exe`/`_hires.exe` | build new arena exe variants with the original 27 bytes restored | crash_or_desync (arena inputs never sent) |
| B16 | wsproto `_eval` | Hex literals in compound conditions are parsed as identifiers (`x0A31`) -> ClientStateCondition -> false. Encode omits buff_param1/2 (4 B) and decode under-reads for buff ids 0x0A31..0x0A3B in 0x2B/0x2E (and 0x04/0x05/0x07) | `wsproto.py:183-187`; reproduced: 0x2E row with buff 0x0A32 encodes to 327 B, expected 331 B | strip `0x[0-9a-fA-F]+` before identifier extraction | crash_or_desync |
| B17 | wsdev sendspec / server encodes | `Grammar.encode` without `assume` truncates S2C 0x37 entries to uid-only (1 entry = 9 B instead of 15 B) and S2C 0x5B to uid-only (4 B instead of 24 B) | `wsdev.py:447`; `wsproto.py:293-298`; reproduced | pass `assume` (add `--assume` to sendspec; per-key assume table in the builder module) | crash_or_desync |
| B18 | UDP helpers | `_fake_map_server` sends Fireway opcode 0x11 + account_id + zeros to client:42907. In `FUN_00458640`, UDP opcode 0x11 copies a string into `scene+0x224` (own P2P IP) and sets port 0xA79B, so it would corrupt the p2p_ip reported in C2S 0x2B. `UDPMapServer` docstring claims the broadcast follows 0x2B (it follows S2C 0x02). Both unused | decomp `00458640_FUN_00458640.c:705-722`; spec S2C `0x02` semantics; `:862-898`, `:2724-2760` | delete; replace with the arena UDP host | cosmetic |

Implemented today: nothing in this group beyond silent consumption of 0x2D (and the wrong handling above). All 24 S2C builders are missing.

---

## 5. Implementation plan

| id | Priority | Effort | Depends on | needs MP | needs UDP | Work |
|---|---|---|---|---|---|---|
| pvp-dispatch-cleanup | P0 | S | none (land together with combat `cs-dispatch-fix`) | no | no | `_dispatch`: 0x38 -> `_handle_battlefield_info` (send `0x63 {player_count, join_count}`; combat's `cs-dispatch-fix` deletes the melee branch, `_combat_driver` stays the attack source); delete the 0x63 echo (`:610-611`; the card-deck owner answers C2S 0x63 with S2C 0x8A); consume 0x2D/0x75 with correct labels; relabel 0x1A/0x2F. |
| pvp-modal-guard | P0 | S | none | no | no | Minimal replies so no C2S leaves a modal up. 0x18 type<4 -> `0x30`, type 4 -> `0xA2`. 0x1A/0x1B/0x1C -> `0x34 {2, room_type}`. 0x39 -> `0x39` + `0x20 {0}`. 0x3A -> `0x20 {0}`. Replaced piecewise by the full handlers below. |
| pvp-wsproto-fixes | P1 | S | none | no | no | Fix hex-literal evaluation in `wsproto._eval`. Add `assume` passthrough to `wsdev.py sendspec` (e.g. `--assume 'entity_with_uid_exists_in_scene=1'`). Re-verify with pilot captures (0x07 369B unchanged). |
| pvp-packets-module | P1 | M | pvp-wsproto-fixes | no | no | New `server/pvp_packets.py`: load grammars by key from `protocol_spec.json`. `build(key, rec)` with per-key assume table (`0x37`, `0x5B`, and False for `0x35`/`0x36`/`0x20` conditions). `parse(key, payload)` for C2S 0x18/0x1A/0x1B/0x1C/0x1D/0x39. `roster_row(session, char, arena_tail)` shared by 0x2B/0x2C/0x2E, reusing the 0x07 field sources (`_build_en_opcode_07:2306-2362`). |
| pvp-session-registry | P1 | S | login `lc-data-model` (unique uid), party M3 (`sessions_by_uid`, `no_enc`, `_send_to`) | yes | no | Reuse the party M3 registry. Add only: room / list-subscriber / queue broadcast helpers on top of `_send_to`; store `p2p_ip`/`p2p_port` from C2S 0x2B; register a room/queue cleanup callback in the disconnect hook (`_handle_fireway` finally `:578`). |
| pvp-warp-refactor | P1 | S | world `world-maptransfer` | no | no | Add a `first_opcode` parameter (0x08 default, or 0x5E/0xA3/0xA4 with the same 6-byte `{u16 map_code, u32 game_time_ms}` body) to the world group's `MapTransfer()` so room exits and forced returns reuse it. Until that lands, a stop-gap is to extract the tail of `_handle_change_map:1281-1320`. |
| pvp-room-model | P2 | M | pvp-packets-module | no | no | `RoomManager` + `Room`/`Member` dataclasses (§3.2), shared room-number pool 1..128 (skip 0x81), limits/config constants. |
| pvp-room-lists | P2 | M | pvp-room-model, pvp-session-registry | no | no | F1/F2: 0x2C -> 0x33, 0x2D, 0x74 -> 0xA1, 0x75; full re-send on changes; never list battle rooms or type 4 in 0x33. |
| pvp-room-create | P2 | M | pvp-room-lists, pvp-room-enter-leave | no | no | F3/F4 validation -> 0x30/0xA2/0x34 -> enter. Title/password NUL-stop parsing; map whitelist; room_level = creator level. |
| pvp-room-join | P2 | M | pvp-room-model, pvp-room-enter-leave | no | no | F5/F6/F7/F8 `_join_room` with results 1..8; kind-0 type derivation. |
| pvp-room-enter-leave | P2 | L | pvp-room-model, pvp-session-registry, pvp-warp-refactor | yes | no | F9/F11 TCP layer: 0x2F + 0x2E to joiner, 0x2C to others, 0x2D on leave, `session['monsters']={}`, return warp, room deletion, disconnect cleanup, play-room exit (0x1D 3/4). |
| pvp-team-select | P2 | M | pvp-room-enter-leave | yes | no | F10: 0x1D 0/1/2/3 -> 0x35 broadcast with balance mirror; immediate apply while idle. |
| pvp-arena-client-exe | P2 | S | none | no | yes | Build `WindSlayer_arena.exe` and `WindSlayer_arena_p2.exe` from the patched variants with file 0x236E0..0x236FA restored to `6a 01 05 10 27 00 00 50 8d 8e 38 0f 00 00 51 8b 8e 90 0f 00 00 ff 15 0c 41 4a 00`. Point wsview/wsdev `--client` at them for arena tests. Check whether this re-introduces the WSA error flood on field maps (it should not: the branch runs only when map mode == 1). |
| pvp-udp-host | P2 | L | pvp-room-enter-leave, pvp-arena-client-exe, UDP channel spec (parallel) | yes | yes | Per-room UDP host at `server_ip:10000+room_no`: learn peers from opcode 3, apply inputs, simulate movement/attacks (port of the client tick functions `FUN_00412360`/`FUN_00413920` or a simplified model), send snapshots 7/8/9 and HP/MP/hit events each tick, drive round phases, mark `member.acted`. |
| pvp-mode1-sim-spike | P2 | M | pvp-room-enter-leave | yes | no | Timeboxed alternative to pvp-udp-host: evaluate a client patch that lets mode-1 maps run the mode-0 local sim (`FUN_0042b260` gates at the 0x70 compares) plus the world group's TCP 0x0D relay. Decide between faithful UDP host and gate patch before building pvp-udp-host. |
| pvp-round-scoring | P2 | M | pvp-udp-host, pvp-team-select, pvp-record-persistence | yes | yes | F12: kill accounting -> 0x37 (delta vs absolute by mode); round end -> winner rule, 0x36 per member, `_send_exp`, fame/Victy wallet updates, leaver removal, idle reset + auto-balance 0x35, final winner warp. |
| pvp-record-persistence | P2 | S | none | no | no | Persist battle W/L/KO/Down, fame, Victy, manner, rank_icon on the char; feed `_build_pyslayer_opcode_03` (`:2046-2051`) and `_build_login_success` manner (`:2678`). |
| pvp-battlefield-queue | P2 | M | pvp-room-enter-leave, pvp-session-registry | yes | no | F13-F16: 0x38 -> 0x63, 0x39 -> 0x20 or 0x39+0x20{0}, 0x3B, 0x20{12} summon, 0x3A collection with timeout, mode-5 room on 9902 with auto teams. The match itself needs pvp-udp-host. |
| pvp-room-balloon | P3 | S | pvp-room-model, pvp-session-registry | yes | no | F18: 0x5B/0x5C broadcast and 0x07 room fields (B12), if Q1 confirms the model. |
| pvp-moderation | P3 | S | pvp-room-enter-leave, pvp-warp-refactor | yes | no | F17: GM command `/delroom <no>` -> 0xA3 members, 0xA4 creator (+manner), 0x5E on UDP-host failure or server-side teardown. |
| pvp-stale-cleanup | P3 | S | pvp-packets-module | no | no | Remove/replace `_build_map_enter_packet`, `_build_enter_world_response`, `_build_pyslayer_enter_world`, `_fake_map_server`, `UDPMapServer`; fix labels B7/B9/B12/B13; add a dispatch comment that C2S 0x93/0x94/0x95/0x97/0x99 are host-only dead code (log a warning if ever seen). |

---

## 6. Live test plan

Common prerequisites:
- `python wsdev.py up` (in-world on map 101 as TestHero: class 0 = novice, level 1).
- Several gates need level >= 5: inject `python wsdev.py send 22 01 00 00 00 0a` (S2C 0x22 SetLevel uid 1, level 10).
- Class-change gates need a character with class != 0: `--char 1` (drix, class 1).
- Window coordinates for dialog buttons are not recorded yet: take `python wsview.py shot` after opening a window and click the named control.
- **Always use `send` with the given hex for 0x37 and 0x5B** (sendspec truncates them until pvp-wsproto-fixes lands).
- After any 0x2F / 0x5E / 0xA3 / 0xA4 injection the world is torn down: recover with `python wsdev.py restart`.
- `wsdev.py send/sendspec` goes through `_admin_listener` (`:459-461`), which injects into **every** live session. With client 2 running, both clients receive the packet: stop client 2 (`--client 2 down`) before single-client injections.
- Arena UDP tests need an exe without the 0x236E0 NOP patch (`pvp-arena-client-exe`). TCP-only injections below work with the stock `WindSlayer_patched.exe`.

### 6.1 C2S (trigger in the UI, capture with `wsdev.py cap`)
| Opcode | Action | Expected | Risk |
|---|---|---|---|
| 0x2C | `wsdev.py cap 2 key f` | C2S 0x2C 0 B; ARENA window opens. After pvp-room-lists: S2C 0x33 in the reply column | safe |
| 0x2D | with F list open: `wsdev.py cap 2 key f` | C2S 0x2D 0 B; no server reply | safe |
| 0x74 | `wsdev.py cap 2 key p` | C2S 0x74 0 B; Play&Chat list opens; after F2: S2C 0xA1 | safe |
| 0x75 | with P list open: `wsdev.py cap 2 key p` | C2S 0x75 0 B; no reply | safe |
| 0x38 | stand away from mobs on map 101: `wsdev.py cap 2 key t` | C2S 0x38 0 B. Today: no [ATK] in logs only because no mob is in reach (on map 102 next to a Pupu it deals damage = B1). After the fix: S2C 0x63 3 B, window shows "0 Players" / "Join(0/12)" | safe (map 101) |
| 0x18 arena | level 10 injected; F -> click Create (0x2B) -> type title "Duel", pick map/type -> `wsdev.py cap 3 click <Create ctrl 3>` | C2S 0x18 27 B: `44 75 65 6c 00 ...` (17) + u8 + u8 + 5 B + `49 26`..`4c 26` + type 0..3. Today: modal stuck (B4). Record the dropdown captions in a screenshot to resolve Q3/Q4 | state_change (modal soft-lock today) |
| 0x18 play | P -> Create (0x479) -> title -> `cap 3 click <ctrl 3>` | C2S 0x18 with `room_option=01`, `map_id=af 26`, `room_type=04` | state_change |
| 0x1A | F list open; inject `sendspec 33` (row room_no 1, type 3 Novice, TestHero is novice, level 10 injected) -> `cap 3 click <row 1>` | C2S 0x1A 3 B `01 00 03`, modal shown. Today modal stuck (B3); close it by injecting `sendspec 34 '{"result":2,"room_type":3}'` | state_change |
| 0x1B arena | F list -> ctrl 9 (enter number) -> type `7` -> OK: `cap 3 click <OK>` | C2S 0x1B `07 00 00`; modal. Recover with `sendspec 34 '{"result":2,"room_type":0}'` | state_change |
| 0x1B play | P list -> ctrl 9 -> `7` -> OK | C2S 0x1B `07 00 04` | state_change |
| 0x1C | inject `sendspec 33` with a row `has_password=1`, click it -> password dialog -> type `abcd` -> OK | C2S 0x1C 8 B `<no> <type> 61 62 63 64 00` | state_change |
| 0x1D | after the 0x2F+0x2E injection (6.2): click Blue (0x7B), Red, Spectator, Exit -> both exit buttons | C2S 0x1D `01`/`02`/`00`; exit dialog ctrl 2 -> `03`, ctrl 3 -> `04`. Blue/Red may be refused client-side ("too many members") depending on F61/F62 | disruptive (needs restart) |
| 0x39 | `--char 1` (class 1), level 10 injected; T -> Join (ctrl 9) | C2S 0x39 1 B `05`; modal. Recover: `sendspec 20 '{"waiting_count":0}'` | state_change |
| 0x3A | on a field map: `sendspec 20 '{"waiting_count":12}'`, wait 7 s, `wsdev.py logs 40` | countdown 5..1 box, then `Pkt: opcode=0x3A size=9` (0 B body) and the text "Waiting for the server to response.". Recover: `sendspec 20 '{"waiting_count":0}'` | state_change |
| 0x3B | `sendspec 20 '{"waiting_count":3}'` -> standby window opens -> `cap 3 click <cancel ctrl 3>` | C2S 0x3B 0 B; window hides client-side first | safe |
| 0x93/0x94/0x95/0x97/0x99 | none (dead host code) | Never seen: `grep "opcode=0x9[34579]" server_history.log` stays empty for client packets | safe |

### 6.2 S2C (inject)
| Opcode | Action | Expected visible result | Risk |
|---|---|---|---|
| 0x33 | open F list first; `wsdev.py sendspec 33 '{"room_count":2,"repeat[room_count]":[{"room_name":"Popola Lv10","room_no":1,"max_players":8,"cur_players":3,"room_level":10,"room_type":0,"has_password":0},{"room_name":"Locked STD","room_no":2,"max_players":8,"cur_players":1,"room_level":0,"room_type":2,"has_password":1}]}'` | rows "001 Popola Lv10(03/08) Lv.10" and "002 Locked STD(01/08) STD" with lock sprite; counter "x/2" (TestHero novice -> 0/2, rows grey); any open wait box closes | safe (in-world only; null-deref if no local player) |
| 0x32 | after 0x33: `sendspec 32 '{"room_count":1,"repeat[room_count]":[{"room_name":"Append","room_no":3,"max_players":4,"cur_players":1,"room_level":0,"room_type":1,"has_password":0}]}'` | row "003 Append(01/04) ULMT" added, total "x/3"; re-sending adds a duplicate row | safe |
| 0x31 | `sendspec 31 '{"room_name":"Novice","room_no":4,"max_players":6,"cur_players":2,"room_level":0,"room_type":3,"has_password":0}'` | one row with novice icon (enterable for TestHero -> counter increments) | safe |
| 0x34 | after triggering a join modal (6.1 0x1A): `sendspec 34 '{"result":1,"room_type":0}'`, then repeat for results 2, 4, 6, 7, 8, then 3, then 5 | 1 "Incorrect password.", 2 "Invalid room #.", 4 "The room is full" plus row 001 shows (08/08), 6 "Your level is not appropriate.", 7/8 novice texts, 3 closes box and opens password dialog, 5 closes box and clears the arena list ("0/0") | safe |
| 0x30 | during the create modal: `wsdev.py send 30` | box text "The number of the arena channels are excessive..." with OK enabled | safe |
| 0xA2 | during the play create modal: `wsdev.py send A2` | "The number of the play rooms are excessive..." | safe |
| 0xA1 | P list open; `sendspec A1 '{"room_count":1,"repeat[room_count]":[{"room_name":"Chat","room_no":5,"max_players":8,"cur_players":1,"room_level":1,"room_type":4,"map_id":9903,"has_password":0}]}'` | row "005 Chat(01/08)", count label "1" | safe |
| 0xA0 | after 0xA1: same JSON with room_no 6 | second row, count "2" | safe |
| 0x63 | T window open: `sendspec 63 '{"player_count":37,"join_count":4}'` | ctrl 4 "37 Players", ctrl 9 "Join(4/12)" | safe |
| 0x20 | field map: `sendspec 20 '{"waiting_count":3}'`, then `'{"waiting_count":0}'` | standby window 0x17B "Death Match waiting list." / "Standing by (3/12)"; T button "Join(3/12)"; count 0 closes box 0x16 (not 0x17B) | state_change |
| 0x39 | `wsdev.py send 39` | orange system line "Your level is not appropriate." | safe |
| 0x5B | field map 101: `wsdev.py send 5B 01 00 00 00 07 00 01 44 75 65 6c 20 52 6f 6f 6d 00 00 00 00 00 00 00 00` (uid 1, room 7, type 1, "Duel Room"); also try a Pupu uid on map 102 | balloon sprite 0x87 with "Duel Room" above the entity; clicking a non-local balloon opens 0x133 "[Duel Room] Enter the arena?" (Yes sends C2S 0x1B `07 00 00`) | safe |
| 0x5C | `sendspec 5C '{"uid":1}'` | balloon disappears | safe |
| 0x2F | level 10 injected; `sendspec 2F '{"round_count":3,"round_time_min":3,"map_id":9801,"host_ip":"127.0.0.1","room_no":1,"current_round":1,"room_mode":1,"room_title":"Test Arena"}'` then immediately 0x2E below | fade; `stage98_01` (Popola arena) loads; windows 0x47E (" Room1", "Test Arena"), 0x4D, 0x480 appear; all entities including self vanish; the client sends nothing on TCP (maybe 0x2D if F list was open) | disruptive |
| 0x2E | right after 0x2F: `sendspec 2E '{"player_count":1,"repeat[player_count]":[{"name":"TestHero","uid":1,"level":10,"rank_icon":99,"repeat[14]":[{"appearance_part":0},{"appearance_part":1},{"appearance_part":1},{"appearance_part":0},{"appearance_part":2},{"appearance_part":2},{"appearance_part":2}],"arena_team":1,"arena_next_team":1}]}'` (321 B) | "You are in blue team." in 0x47E; roster shows TestHero under Blue; HP/MP bars full; `wsview.py state` shows uid 1 state 3 at (0,0). Movement is frozen (mode-1 map, no UDP host); verify no crash | crash_risk |
| 0x2C | after 0x2E: `sendspec 2C '{"name":"Rival","uid":2,"job1":1,"level":10,"rank":99,"repeat[14]":[{"appearance_id":0},{"appearance_id":1},{"appearance_id":1},{"appearance_id":0},{"appearance_id":2},{"appearance_id":2},{"appearance_id":2}],"team":2,"next_team":2}'` (313 B) | "Rival" appears in the red roster; entity uid 2 in `wsview state` | state_change |
| 0x2B | after 0x2E: `sendspec 2B '{"player_count":1,"repeat[player_count]":[{"name":"Watcher","uid":3,"level":7,"rank_icon":99,"arena_team":0,"arena_next_team":0,"arena_kills":2,"arena_deaths":1,"arena_score":50}]}'` | Watcher added (spectator, state 1) with score 50; no HUD transition | state_change |
| 0x35 | after 0x2C: `sendspec 35 '{"uid":2,"team":2,"next_team":1}'`; then for self `'{"uid":1,"team":1,"next_team":3}'` | Rival listed as moving to blue next round; self text "You subscribed to leave the arena... when this round ends". Never inject for uid 1 outside a room map | state_change |
| 0x37 | after 0x2E (mode 1): `wsdev.py send 37 01 00 01 00 01 01 00 00 00 0a 00 00 00 00 01` | "[Blue1:0Red]" line; scoreboard TestHero score +10, KO +1 (repeat to confirm additive in mode < 5) | state_change |
| 0x36 | after 0x2E: `sendspec 36 '{"round_result":1,"round_no":1,"blue_round_wins":1,"red_round_losses":1,"reward_points":10,"total_downs":0,"total_kos":1,"total_wins":1,"total_losses":0}'` | "Blue team wins", "+10Reputation Points, +10Victy, +10EXP."; record panel "1 Win / 0 Lose / 1 KO / 0 Down"; all entities HP/MP refilled. Ignored (nothing read) if injected on a field map | state_change |
| 0x2D | after 0x2C: `sendspec 2D '{"uid":2}'` | Rival removed from roster and scene. **Never uid 1** (dangling local pointer) | state_change |
| 0x5E | on the arena map from the 0x2F test: `sendspec 5E '{"map_code":101,"game_time_ms":0}'` | popup "Connection to PVP server was lost..." then map 101 loads with no player (needs 0x03/0x07; server does that after pvp-warp-refactor). Recover: restart | disruptive |
| 0xA3 | `sendspec A3 '{"map_code":101,"game_time_ms":0}'` | popup "The room has deleted due to inappropriate room name..." + map change | disruptive |
| 0xA4 | `sendspec A4 '{"map_code":101,"game_time_ms":0}'` | popup "Your manner point has reduced..." + map change | disruptive |

### 6.3 End-to-end (after implementation)
1. Solo room: level 10 -> F -> Create (type 1 ULMT, Popola) -> expect 0x34{5,1}, 0x2F 47B, 0x2E 321B -> blue button -> 0x1D 01 -> 0x35 `01000000 01 01` -> exit ctrl 3 -> 0x1D 04 -> 0x08 + 0x03 + 0x07 back on map 101 at the saved position.
2. Two clients (`--client 2`, unique uids required): client 2 joins room 1 from its list -> client 1 receives 0x2C (Rival appears); client 2 exits -> client 1 receives 0x2D.
3. `[UDP]` With `WindSlayer_arena.exe`: in a room, check `server_live.log` for UDP opcode-3 datagrams on port 10001 from 127.0.0.1:42907. With the stock patched exe expect none (B15).
4. Battlefield (dev `BF_MATCH_SIZE=2`, two class-1 characters): both Join -> 0x20{1}, 0x20{2}, then 0x20{12} -> ~5 s -> 0x3A from both -> 0x2F(mode 5, 9902) + 0x2E with teams 1/2.

---

## 7. Open questions

1. **Room lifecycle.** No C2S "start game" exists, and 0x34 result 5 only flushes the list. Does retail move a joiner into the arena immediately (0x2F), or keep room members in the field advertised by a 0x5B balloon until a start condition? Balloons draw only on mode-0 maps and are clickable joins, so they must sit on players who are still in a field. This design enters immediately (configurable) and makes balloons P3.
2. Is 0x34 result 5 also the success reply for C2S 0x18 create, or did retail go straight to 0x2F?
3. Meaning of `room_option` (arena create ctrl 7, two numeric choices): round count, round minutes, or something else? Capture both captions.
4. Values of the `max_users` dropdown (10 options).
5. Arena room types: 0 "Lv", 1 "ULMT", 2 "STD", 3 "Novice". The client derives team-mode flag F6C for 2, 3, 5. Are 0/1 free-for-all? How are team buttons and scoring meant to behave there?
6. Arena spawn positions. 0x2B/0x2C/0x2E carry no coordinates. Do positions come only from UDP snapshots, or from map start points? `[UDP]`
7. UDP host protocol details (owned by the parallel spec): Fireway framing and encryption on `SendTo`, snapshot cadence, required per-tick opcodes, and whether a full server-side movement/collision simulation is needed (vs. the mode-1 gate-patch alternative, pvp-mode1-sim-spike). `[UDP]`
8. Battlefield content: is battle_type 5 on map 9902, and what is 9901 "battlefield waiting room" (mode 0) for? The T window lists Capture the Jewel/Flag, but `stage99_04..08` are missing from EN `hs/`.
9. `player_count` in S2C 0x63: players in the battlefield, in the queue, or online?
10. Reply to C2S 0x3A: straight 0x2F, or 0x08-style? What does the client do if 0x2F never comes (the text stays up)?
11. C2S 0x1D codes 3/4 inside a play room (the same 0x4B dialog, "Would you exit from play room?"): is ctrl 3 enabled there?
12. Reward formula for 0x36 `reward_points`, and entity `+0xB4` (the winners' bonus in modes >= 5).
13. Does the owner's own entity need 0x5B, and does the balloon draw over the local player?
14. Team colour mapping: 1 = blue is inferred from the "[Blue%d:%dRed]" argument order and name colours 0xFF5D9DFF / 0xFFFF5D5D. Confirm visually with the 0x2E/0x2C injection.
15. Safety of a local player registered via 0x2E at (0,0) on a mode-1 map (nameplate path `+0x11DC`). Verify with the 0x2E crash_risk test.
16. On mode-1 maps the client stops sending C2S 0x0D (spec `0x42CE94/0x0D` gate). The portal sender `FUN_0042cf20` is NOT mode-gated in `FUN_0042b260`, so a spurious C2S 0x7E is still possible: ignore 0x7E while `session['room']` is set. Confirm no other periodic TCP traffic, other than the 0x05 keepalive, is expected while in a room.
17. Password edit length (4 or 5 characters). The server compares the first NUL-terminated <= 5 bytes either way.
18. GM moderation (0xA3/0xA4) triggers and the size of the manner penalty (social group owns S2C 0x97).
19. Should `items.PvPItem` / `NotTrade` restrict equipment or consumables inside rooms?
