# chat_mail_gm: chat, mail/gifts, GM/admin (system design)

- **Specs:** `re_tools/corpus/systems/chat_mail_gm.json` (21 specs, all confidence high). Cross-lookups come from `corpus/protocol_spec.json` and `docs/PROTOCOL.md`.
- **Client:** EN 2008 OUTSPARK `WindSlayer_patched.exe`, with `WindSlayer_p2.exe` as the second client.
- **Server:** `WindSlayer2Game/server/windslayer_server.py`, the build of 2026-09-17 05:35. Every `Lnnn` below refers to that file unless another file is named. The codec is `server/wsproto.py` (06:51 build).
- **How this doc was checked:** every server line cited was re-read. Codec claims were reproduced by running `wsproto.Grammar` on the spec grammars. Content ids were queried from `server/gamedef.sqlite3` and `server/data/en/items_en.json`. HUD coordinates come from `server/_cap_click_603_545.png`, `_cap_buttons.png` and `_chatcrop.png`.

| Sub-system | C2S (client -> server) owned here | S2C (server -> client) owned here |
|---|---|---|
| chat | 0x03 NormalChat, 0x02 WhisperChat, 0x6B FriendChatMessage | 0x16 ChatMessage, 0x09 WhisperSendResult, 0x0A WhisperReceived, 0x15 SystemMessage, 0x90 ChatLineOrange, 0x91 ChatLineGreen, 0x0F MessengerChatRoomJoinResult, 0x61 MessengerChatRoomMessage |
| mail_gift | 0x4B SendNote, gift thank-you branch (`note_item_id` 9999) | 0x78 MemoList, 0x6D GiftInboxList |
| admin_gm | 0x06 GM commands (6 send sites: /manner, /업데이트+/shadow+/stop+/kick, /not, /proom, /reset, /go), 0x6E PlayerReport | none. GM replies use 0x15, 0x53, 0x97, 0x3A, 0x5D, 0xA3/0xA4 and the world map-transfer sequence |

**Other groups' docs this design must stay consistent with** (all in `docs/systems/`):
- **social_friend.md**
  - Owns C2S 0x2F/0x33/0x34/0x35/0x36/0x44 and the Note-item 0x4B path, plus S2C 0x0B/0x10/0x60/0x62/0x95/0x96/0x97.
  - Its F1 ("resync on C2S 0x2F") is where memos (0x78) get re-sent.
  - Its F5 drives the 0x0F/0x61 builders defined here.
  - Its 3.1-3.5 define the shared uid, online-index and persistence model reused below.
- **premium_cash.md:** owns C2S 0x47/0x4C and S2C 0x6A/0x6F/0x77. Its F1/F7 decide *when* 0x6D is sent, F11 routes megaphones to 0x90, F12 covers the Note 0x77 reply.
- **party.md:** F8 relays C2S 0x6A party chat as S2C 0x90 (builder defined here).
- **login_character.md:** S2C 0x02 `manner_points`, per-account uid.
- **world_movement_npc.md:** the `MapTransfer()` primitive (used by /go), S2C 0x04/0x05/0x06 visibility, B23 "welcome whisper on every map change".
- **pvp_arena.md:** play rooms and 0xA3/0xA4, used by /proom.

---

## 1. How the client implements this system

### 1.1 Chat input pipeline: `FUN_00444a90`

Every chat line typed or fired from a macro goes through one parser: `FUN_00444a90(this=game_state 0x70EA00, text, strlen)`. It has three callers:
- the chat-box IME submit, `FUN_004449b0` (0x4449DD);
- the 12 chat-macro hotkey slots, `FUN_0043db00` (37 B each at this+0xE88);
- UI list control 0x294, `FUN_00446300` (0x44AE75), which forces "All" mode 5.

Gates run in this order and apply to chat and GM text alike:
1. `strlen > 0`.
2. `ENGINE.DLL Curse_Engine_Clean(text)` must pass. If it fails, the client shows "No cursing please" and sends nothing.
3. Manner `[scene+0xEE0] > -40`. If not, the client shows "You can't chat with others due to your low manner point."
   - scene+0xEE0 is loaded from **S2C 0x02 `manner_points`**.
   - It changes only through S2C 0x96/0x97.
4. `[scene+0xF00] == 6`, meaning in-world (set by S2C 0x07 via `FUN_004400C0(6)`). If not, the client returns silently.
5. **GM branch.** If `word [[scene+0x970]+0x12] == 1` (S2C 0x07 `gm_level`), the text is tested against the GM prefixes in this order: `/manner `, `/업데이트`, `/not`, `/proom `, `/shadow`, `/reset `, `/go `, `/stop`, `/kick`. A match sends C2S 0x06 (section 1.5). Anything else, and every line from a non-GM, continues to normal parsing at 0x445063.
6. Prefix parsing and chat mode `[g_ui_root+0x44]`:

| Prefix | Wire opcode | Sets mode |
|---|---|---|
| `/w `, `/W `, `/ㅈ `, `/message `, `/whisper ` | 0x02 | 1 |
| `/p `, `/ㅔ ` | 0x6A (party group) | 2 |
| `/f `, `/ㄹ ` | 0x6B | 4 |
| `/a `, `/ㅁ ` | 0x03 | 5 |
| none | 0x03, then routed by mode: 1 -> 0x02 (target taken from game_state+0x4B0), 2 -> 0x6A, 4 -> 0x6B, 0/3/5/>5 -> 0x03 | unchanged |

   Any other `/xyz` line is ordinary 0x03 chat, including emotes like `/heart` that S2C 0x16 turns into animations. The chat-tab buttons write the mode too: 0x448A9E=1, 0x448B04=2, 0x448B64=4, 0x448BC5=5. The live chat bar reads "To All" (`_chatcrop.png`).

7. **Anti-spam.** Normal, party and friend chat share a 700 ms timer. A second line inside the window shows "Do not Spam.", refreshes the timer and sends nothing. Whispers are exempt.
8. **Local echo.** 0x03, 0x02 and 0x6A get none: the server must echo them or the sender never sees the line. 0x6B is echoed locally in green `0xFF8EE085` (0x445C28), so the server must NOT echo it.
9. **Client abort hazard.** `sprintf_s(61,"%s",text+prefix)` at 0x4458C8 is the real CRT function and does not truncate. A stripped line over 60 bytes reaches the CRT invalid-parameter handler, and no handler is installed, so the client terminates.

### 1.2 Chat display opcodes

None of these has a client reply. Every "limit" in the table is a crash or overflow boundary, not a cosmetic one.

| S2C (handler) | Grammar | What the client does | Server limits |
|---|---|---|---|
| **0x16** ChatMessage (0x451AF0) | `str[17] sender_name; u8 text_len; str[text_len] text` | White `name : text` line. The remote entity (alive state 3) with that exact name gets a speech bubble. A text starting with `/` that matches an emote alias plays emote `0x118+i` on that entity and prints no line. | text <= 60 (61-byte stack buffer); name <= 16 + NUL |
| **0x09** WhisperSendResult (0x4511C7) | `u8 status; str[17] target_name; if status not 0x66/0x67 { u8 msg_len; bytes msg }` | 0x66: `<name>can not be found.` (log line). 0x67: `<name>is rejecting whispers.` (notice). 0x65: `<To: name> msg`. Any other value: `<To: name[Channel-N]> msg`. | msg <= 60; 0x65 form name+msg <= 80; channel form name+digits+msg <= 70 |
| **0x0A** WhisperReceived (0x451383) | `u8 channel; str[17] sender_name; u8 msg_len; bytes msg` | `<From: name> msg` (0x65) or `<From: name[Channel-N]> msg`. Adds sender_name to the whisper dropdown (UI 0x6E) for the session. | msg <= 60; name+msg <= 78; name MUST have a NUL within 17 (heap strcpy into 17 B) |
| **0x15** SystemMessage (0x4519C9) | `u8 msg_type; u8 text_len; str text` | type 0: light blue 0xFF00E4FF. type 1: yellow-green 0xFFE4FF00. type 2: red if the text starts `[Warni`, light blue if it starts `[Annou`, else green. Also appended to the CMessenger secondary list. | text <= 88; msg_type 0..2 only (>=3 picks a garbage colour) |
| **0x90** ChatLineOrange (0x457C74) | `u8 text_len; str text` | Pre-formatted orange 0xFFFF8000 line | text <= 87 |
| **0x91** ChatLineGreen (0x457C01) | `u8 text_len; str text` | Pre-formatted green 0xFF8EE085 line, the same colour as the local `/f` echo | text <= 87 |
| **0x0F** MessengerChatRoomJoinResult (Sub3 0x470E31) | `u8 subtype; if 1 { u32 room_id; u8 member_count; if 0 stop; repeat str[17] member_name } else { str[17] target_name }` | subtype 1: store room_id at CMessenger+0xCC, open window 0x177, append names (not de-duplicated), and if count <= 2 print `<last> has entered.`. subtype 2 other channel, 3 refused, 4 rejecting chat, 5 room full, 6 busy in another conversation, 8 not on line (also marks the friend offline): modal popups. | subtypes 1-6 and 8 only (0/7/9+ print an uninitialised 0x80 buffer); names NUL-terminated (nodes not zeroed) |
| **0x61** MessengerChatRoomMessage (Sub3 0x47132D) | `if(client.messenger_room_id != 0) { str[17] sender_name; u8 msg_len; bytes msg } else stop` | In window 0x177: a `name : ` header (pink 0xFFC766A6 if own name, else blue 0xFF3277CC), then `  msg` in grey | msg 1..58, no embedded NUL; ignored (0 bytes read) while +0xCC == 0 |

KR PySlayer (`doc/server_to_client/README.md`) names 0x90 GUILD_CHAT and 0x91 FRIENDS_CHAT. The EN client has no guilds (the char-info panel hardcodes " Not in the Guild"), so other groups have already claimed 0x90 for party chat (party.md F8) and megaphones (premium_cash.md F11).

### 1.3 CMessenger state and the S2C 0x03 reset

`CMessenger` lives at `[main+0x4E4]`.
- **Gate.** SubHandler3 opcodes (0x0F, 0x61, 0x78, 0x95, 0x97, ...) need `+0x68/+0x70` non-NULL, which is true from startup.
- **Socket.** `+0x74` is set by `FUN_0046f860`. The **S2C 0x03 handler** calls it at 0x44E52F; the 0x10 spec's "0x07 handler" wording is a typo, since 0x44E52F lies inside the 0x03 handler, which starts at 0x44E38F.
- **What each 0x03 wipes.** The same call clears the friend list, sets room id `+0xCC` = 0 and status `+0x9C` = 0, and frees the memo list `+0x34` (via `FUN_0046fc40`).
- **Resync hook.** At the end of the 0x03 handler the client sends a bare **C2S 0x2F**, then C2S 0x63. **C2S 0x2F is therefore the correct hook** for re-sending messenger state: friends 0x0B (social_friend), memos 0x78 (here).
- **Room lines.** 0x61/0x62 are silently ignored once +0xCC is 0.
- **NULL socket checks.** C2S 0x6B/0x35/0x36 are only sent when `+0x74 != 0`. C2S 0x6E dereferences `+0x74` without a NULL check, but our server always sends 0x03 before the player is in world.
- **What 0x03 leaves alone.** The gift queue (cash ctx `game_state+0x700` list `+0x5F4`) is **not** touched by 0x03. Only a session reset (`FUN_0045B0F0`) clears it.

### 1.4 Mail: memos and gifts

**Memos (offline messages).**
1. Sending is the Note item path (social_friend F9): items 1894 "Note (x1)" and 3320 "Notes x11" open window 0x3FD, which sends C2S 0x4B `{u16 item_id, str[17] recipient, str[91] contents}` and shows a modal "Waiting for the server to response.". The reply is S2C 0x77.
2. Stored memos arrive as **S2C 0x78**: `u8 count` + 126-byte records `name[17] | text[93] | SYSTEMTIME(8 x u16)`. Records are appended to CMessenger `+0x34`. Nothing de-duplicates them, and only 0x03 or C2S 0x44 clears them.
3. After the loop, including when count == 0, the client enables HUD window 3, control #8 (index 7), which is the **Msg** button at about client (553,545). Its tooltip says "The message icon blinks when you receive a new message".
4. Clicking Msg opens window 0x3FE (sender, `Y/M/D h:m` from +0x6E/+0x70/+0x74/+0x76/+0x78, body). Selecting a row copies the sender name into `CMessenger+0xA4`.
5. Closing the window asks "All the messages will be deleted when closing the window. Will you continue?". OK sends **C2S 0x44** (no body), greys the button (state 3) and frees the list.

**Gifts.**
1. **S2C 0x6D:** `u8 count` + 126-byte records `sender[17] | message[91] | u16 item_id | 16 unknown bytes`. They are appended to cash ctx `+0x5F4`. After the loop, even when count == 0, HUD window 3 control #6 is enabled (**Gift** button, about (603,545)).
2. No popup appears yet. Gift popup 0x3EF opens only from `FUN_0045e260`, which runs at the **end of S2C 0x6A CashShopEnter** and again after each popup is confirmed.
3. `FUN_0045e260` shows the queue head only if the item exists and (itemdef+0x1F0 != 0, or `3327 <= item_id <= 3332`, the "WS Gift Card" range).
   - Evidence that +0x1F0 is `gamedef.items.Cash`: in `gamedef.sqlite3`, notes 1894/3320 and megaphones 3377-3381 have `Cash=1`, while gift cards 3327-3332 have `Cash=0`. That is exactly why the client special-cases their id range. PROTOCOL.md also calls +0x1F0 the "is-cash flag" (0x43/0x6F).
   - A head record that fails the check never shows, and it blocks every later gift for the session.
4. Pressing popup button 2:
   - With non-empty reply text, the client sends **C2S 0x4B `{note_item_id=9999, recipient_name=<gift sender>, message[91]}`** (send site 0x46098E), then pops the gift and shows the next one.
   - With empty text it only pops.
   - No waiting box is shown.
   - The client pops only if the sender name passes `FUN_0043df40` (non-empty, no space), so a gift whose sender name fails that check can never be dismissed.

### 1.5 GM commands (C2S 0x06, one opcode, sub-command in byte 0)

These exist only for a local entity whose S2C 0x07 record has `u16 gm_level == 1`. The same flag draws the "Wind Master" title under the name. It is followed by `bool gm_hidden` (entity+0x14; non-zero means render and sound paths skip the entity). After a command the client clears the input and waits for nothing.

| Typed | Body after opcode 0x06 | Spec key | Client-side notes |
|---|---|---|---|
| `/manner <name> <n>` | `0A, str[17] name, i32 delta` (22 B) | 0x444C52/0x06 | n == 0 sends nothing; a non-numeric n sends uninitialised stack garbage; a name over 16 chars overflows the stack |
| `/업데이트` (exact CP949 `2F BE F7 B5 A5 C0 CC C6 AE`) | `00` | 0x444C91/0x06 | Needs a Korean IME or a macro |
| `/not...` (only 4 bytes compared, L > 4) | `01, u8 (L-4), bytes text+4` | 0x444CFD/0x06 | `/notice hi` sends `ice hi`; `/not hi` sends ` hi`. The length byte can wrap if L-4 >= 256 |
| `/proom <room#> [pts<=0]` | `0B, u32 gm_uid (scene+0x220), u16 room_no, i32 pts` (11 B) | 0x444DE2/0x06 | pts > 0: "Points to deduct must be less than 0.", nothing sent; `/proom ` followed only by spaces calls `_atol(NULL)` (client crash) |
| `/shadow` (prefix match) | `02` | 0x444C91/0x06 | `/shadowy` also matches |
| `/reset <tok129> <tok2>` | `03, str[129], str[3]` (133 B) | 0x444F22/0x06 | Either token empty: nothing sent |
| `/go <name>` | `08, str[17] name` (18 B) | 0x444FC6/0x06 | A 17+ char token is sent without a NUL |
| `/stop N` (strlen > 5) | `07, u8 atol(N)` | 0x444C91/0x06 | Truncated to 8 bits; non-numeric gives 0 |
| `/kick N` (strlen > 5) | `0C, u8 atol(N)` | 0x444C91/0x06 | A NUMBER (low 8 bits), not a name |

Non-GM players who type these lines send them as ordinary C2S 0x03 chat.

### 1.6 Player report (C2S 0x6E -> S2C 0x95)

1. Window 0x434 opens from the HUD **Report** button (about (650,545); live screenshot "Report fee: 100 Gold", "You can report someone only once a day") or from player right-click popup 0x50, item 11.
2. The fee is computed when the window opens: `ceil(level/10)*100`, with level taken from `[[info+0x928]+0x99]`.
3. On ctrl 0x1C the client checks, in order: gold >= fee ("You are short of gold."), `FUN_0043df40(name)`, name != own name ("You can't report your own character."), content non-empty ("Please enter the contents.").
4. It sends `u32 target_uid, str[17] target_name, u32 fee, u8 category (0 Abuse, 1 Spamming, 2 Item Fraud, 3 Real Trading, 4 Assuming a GM, 5 Leakage Info, 6 Hacking Tools), u8 len, bytes content` and hides the window. It does not deduct gold.
5. `category` (CMessenger+0xD8) is not reset between reports. `target_uid` is 0 or stale on the HUD path (not traced).
6. S2C 0x95 (social_friend) handles the reply:
   - `1 + u64 gold` (absolute): "Reported successively.";
   - `2`: "The character doesn't exist. Please check and try again.";
   - `7`: "You can report a player only once a day.";
   - any other value is ignored.

---

## 2. Request/response flows

Notation:
- **Map peers** = in-world sessions with the same `current_map`, sender included unless stated.
- **Push** = `_push(target_session, opcode, payload)`, defined in social_friend.md 3.3. It uses the *target's* `no_enc`; every live session so far is `no_enc=True`, and server_live.log shows only `by_array=True` sends in world.
- All payloads are built with `wsproto.Grammar(spec.grammar).encode(rec)` after the clamps in 3.5.

### F1. Normal chat: C2S 0x03 -> S2C 0x16
1. The player types `hi` + Enter. Client gates 1.1 apply (manner > -40, in-world, 700 ms). The wire is `C2S 0x03 {msg_len=2, message="hi"}` (live: `03 02 68 69`).
   - In mode 5 with a prefix, msg_len counts the whole original line: `/a hi` sends `05 68 69 00 68 69`.
2. Server `_handle_chat`:
   1. `text = payload[1:1+msg_len].split(b'\0')[0][:60]` (bytes, no ASCII decoding).
   2. Drop silently if `text` is empty, `session['in_world']` is false, `account manner <= -40` (the server mirror of the client gate makes GM mutes stick against patched clients), or the per-session rate limit is hit (5 lines / 3 s).
   3. If `session['gm']` and `text` starts with `!`, run the dev-command router (F11), do not broadcast, and stop. Never intercept `/`: emotes (`/heart`, `/love`, `/:)`) must reach other clients. PySlayer's `/`-command interception (game_server.py L92) is the pattern NOT to copy.
3. Build `S2C 0x16 {sender_name=name17(session['char_name']), text_len=len(text), text}` and push it to all map peers **including the sender**.
4. Receivers print `TestHero : hi`. A receiver whose scene has a remote entity named TestHero also shows a bubble or emote; that needs world-group visibility (0x04/0x05).
5. Refusals are all client-side ("No cursing please", low manner, "Do not Spam."). A server-side drop sends nothing.

### F2. Whisper: C2S 0x02 -> S2C 0x09 (+ S2C 0x0A)
1. The player types `/w test hi`. Client gates: common gates, manner > -20 ("You can't whisper to others due to your low manner point."), name <= 16 chars, target != own name ("You can't send a message to yourself.").
   - Quirk 1: `/message` and `/whisper` parse the name from index 6, so `/whisper bob hi` targets "er".
   - Quirk 2: `/w  hi` and the whisper-tab mode with no stored target send an all-zero name and msg_len 0.
2. The wire is `C2S 0x02 {target_name="test", msg_len=2, message="hi"}` (20 B).
3. Server:
   1. `target = payload[:17].split(b'\0')[0]`, `msg = payload[18:18+min(msg_len, len-18)].split(b'\0')[0]`.
   2. Empty target or empty msg: drop, no reply.
   3. Sender `manner <= -20`: drop.
   4. Rate limit 10 per 5 s: drop.
4. Resolve the target: `online_by_name[target]`, then a case-insensitive fallback, in-world sessions only.
   - Not found, offline or at character select: push `S2C 0x09 {status=0x66, target_name=name17(target)}` (18 B). The sender sees `<test>can not be found.`
   - Target is the sender (for example a case-variant name): push `S2C 0x15 {2, "[Warning] You can't send a message to yourself."}`.
   - `T.refuse['whisper']` (C2S 0x2B byte 0 / C2S 0x40 byte 0) and the sender is not GM: push `S2C 0x09 {status=0x67, target_name=T.char_name}` (18 B). The sender gets a notice `<test>is rejecting whispers.`
5. Delivered: clamp `msg` to `min(60, 78-len(sender_name), 80-len(T.char_name))` bytes.
   - To T: `S2C 0x0A {channel=0x65, sender_name=name17(sender), msg_len, message}`, shown as `<From: TestHero> hi`.
   - To the sender: `S2C 0x09 {status=0x65, target_name=name17(T.char_name) (canonical case), msg_len, message}`, shown as `<To: test> hi`.
6. Multi-channel later: put the target's channel number in `status`/`channel`. It must never equal 0x65/0x66/0x67, and the channel-form clamp (70 / 68) applies.

### F3. Friend chat: C2S 0x6B -> S2C 0x91
1. Precondition: the client friend list (S2C 0x0B, social_friend F1) has entries with status 0 or 4 and a non-zero channel byte. Today no 0x0B is ever sent, so `/f hi` only prints the local echo and sends nothing.
2. The client prints green `TestHero : hi`, then sends `C2S 0x6B {recipient_count=n, repeat{u8 friend_channel, u32 friend_id}, u8 msg_len, message="TestHero : hi"}`.
3. Server:
   1. For each `friend_id`, look up `online_by_uid[friend_id]`. Keep only sessions whose `char_name` is in the sender's stored `friends` list (social_friend 3.5). The client-supplied list is never trusted.
   2. Anti-spoof: if `message` does not start with `<sender char_name> : `, replace everything before the first ` : ` with the real name.
   3. Clamp to 87 bytes.
4. Push `S2C 0x91 {text_len, text}` to each kept recipient. Send **nothing** to the sender.

### F4. System lines: S2C 0x15 / 0x90 / 0x91 helpers (replaces `_send_chat_line`)
1. `_notice(session, text, kind)` -> `S2C 0x15`:
   - `kind='info'`: msg_type 1 (yellow-green);
   - `kind='warn'`: msg_type 2, text prefixed `[Warning] ` (red);
   - `kind='announce'`: msg_type 2, text prefixed `[Announce] ` (light blue, the same look as the client's own `[Announce]` tip lines in `_chatcrop.png`);
   - `kind='plain'`: msg_type 0.
   - Clamp the text to 88 bytes.
2. Move all six current `_send_chat_line` callers to `_notice`:

   | Line | Text | kind |
   |---|---|---|
   | L1020 | "Not enough gold." | warn |
   | L1347 | "Unknown quest." | warn |
   | L1356 | "Quest log full (3)." | warn |
   | L1375 | "Quest N accepted." | info |
   | L1383 | "Quest N: not complete (a/b)." | warn |
   | L1440 | "Quest N complete! +x exp, +y gold." | info |

3. Welcome: replace the S2C 0x0A whisper from "Server" (L765-772, L1306-1309) with `_notice(session, "Welcome to WindSlayer!", 'announce')`, **on the first enter-world of a connection only** (world_movement_npc.md B23).
4. `_orange(sessions, text)` (S2C 0x90) is used by party chat (party.md F8) and megaphones (premium_cash.md F11). `_green(session, text)` (S2C 0x91) is used by F3. Both clamp to 87 bytes.

### F5. Messenger room display side (flow owned by social_friend.md F5)
This group supplies grammar builders with the client limits; social_friend.md F5 decides when to send them.
- `_room_joined(target, room_id, names)`: S2C 0x0F subtype 1. Names are NUL-terminated 16-byte max. A 2-person room lists the *other* person last so the line reads `<other> has entered.`
- `_room_refused(target, subtype in {2,3,4,5,6,8}, name)`.
- `_room_line(target, sender, text)`: S2C 0x61 with text cut at NUL and clamped to 58. Encode with `assume={'client.messenger_room_id != 0': True}`, or the encoder emits 0 bytes (B10).

Map change: social_friend.md F5.9 leaves the room *before* any server-sent 0x03, because 0x03 zeroes +0xCC. Test T-0F-restore checks the alternative (re-send `0x0F {1, room_id, count=0}`, which restores +0xCC without adding rows).

### F6. Memo delivery and deletion: S2C 0x78, C2S 0x44
Storage and the Note send path are defined in social_friend.md F9 (`char['memos']`). This design adds an `id` per record and a client-view guard.
1. **Store.** `{id, from, text (<= 90 bytes, NUL-cut), t=[year, month, dow, day, hour, minute, second, ms]}` for local time. At most 100 per character; drop the oldest.
2. **Resync on C2S 0x2F** (social_friend F1 step 6):
   - If `char['memos']` is non-empty, push `S2C 0x78 {count, records}`, at most 255 records per packet (chunk beyond that). Record fields: `sender_name=name17(from)`, `text=text[:92]`, `year..millisecond=t`.
   - If there are none, **send no 0x78 at all**: count 0 still lights the Msg button.
   - Set `session['memo_ids_on_client'] = {ids sent}` and `session['msgr_synced'] = True`.
3. **Live push.** When a memo is stored for an online, in-world recipient with `msgr_synced`: push `S2C 0x78 {count=1, [record]}` and add its id to `memo_ids_on_client`. While `msgr_synced` is False (a 0x03 was sent and 0x2F has not come back yet), skip the push; the coming 0x2F resync delivers it. Without this, the memo would show twice.
4. **Before any server-sent S2C 0x03** (enter-world L735, map change L1300): set `session['msgr_synced'] = False` and `memo_ids_on_client = set()`.
5. **C2S 0x44** (no body, sent after the player confirms): delete the memos whose id is in `memo_ids_on_client`, persist, send no reply. A memo stored after the last delivery survives.
6. The player sees window 0x3FE with the sender, `2026/9/17 12:30` and the body. Closing and confirming empties it; the Msg button greys out.

### F7. Gift thank-you reply: C2S 0x4B with `note_item_id == 9999`
1. The popup 0x3EF is open (it needs F8 plus S2C 0x6A from premium_cash). The player types thanks and clicks OK. The wire is `C2S 0x4B {9999, recipient_name=<gift sender>, message[91]}` (110 B, `0f 27 ...`).
2. The `_handle_note` dispatcher branches on the first u16:
   - `9999`: this flow;
   - `1894` / `3320`: social_friend.md F9 (consume a Note, then S2C 0x77, which is mandatory because the client shows a wait box);
   - anything else: `S2C 0x77 {result=0}`.
3. Server, 9999 branch:
   1. `rec = name_index.get(recipient)`. Missing, recipient == sender, or empty text after the NUL cut: drop and log. Optionally `_notice(warn, "Thank-you note could not be delivered.")`.
   2. Store the memo for the recipient (F6.1). Consume no item. Push 0x78 if the recipient is online (F6.3).
   3. **Reply: none by default.** The client shows no wait box and has already opened the next gift popup. A 0x77 `{1,0}` (premium_cash.md F12 proposal) would pop the "You successfully sent the message." modal and close window 0x3FD. Whether that is harmless is open question Q6 / test T-77-9999.

### F8. Gift notification: S2C 0x6D (send timing owned by premium_cash.md F1/F7)
1. **Gift store.** Records are created by premium_cash C2S 0x47 CashShopSendGift: `{id, from, message (<= 90 bytes), item_id, delivered_session=None}` in the recipient's premium `gift_inbox`.
2. **Queue gate (this group).** Only queue gifts that will display. The item must exist in `data/en/items_en.json`, and either `gamedef.items.Cash == 1` or `3327 <= item_id <= 3332`. The `from` name must be non-empty, contain no space and be <= 16 bytes. Anything else would permanently block the client's gift queue (1.4), so convert it into a memo (F6) instead.
3. **Delivery.** Once per connection, on the first C2S 0x2F after login and when a gift arrives for an online recipient: push `S2C 0x6D {gift_count=n (<=255), records {sender_name, message, item_id, unk_6e=16 x 00}}` for gifts not yet delivered on this connection. Mark them. **Never re-send on later 0x2F:** the list survives 0x03 and the client would duplicate them.
4. The Gift HUD button lights up. The popup appears only at the end of the recipient's next S2C 0x6A (entering the cash shop). The server marks the gifts as notified then.

### F9. Player report: C2S 0x6E -> S2C 0x95 (result semantics from social_friend.md F11)
1. The player opens window 0x434, fills name, category and content, and clicks Report. The client pre-checks per 1.6.
2. The wire is `C2S 0x6E {target_uid, target_name, report_fee, category, content_len, content}`. Decode with the grammar; `content = payload[27:27+content_len]`.
3. Server, in order:
   1. **Resolve the target.** If `target_uid != 0`, use the online session with that uid **and** a matching name. Otherwise use `name_index[target_name]` (any character, online or offline). No match: push `S2C 0x95 {result=2}`.
   2. Target is one of the reporter's own account characters: drop (the client already blocks its own name).
   3. `account['social']['report_day'] == today` (server-local date): push `S2C 0x95 {result=7}`.
   4. `fee = max(100, ceil(char_level/10)*100)` computed server-side; ignore the client `report_fee`, but log any mismatch. `category = min(category, 6)`.
   5. Wallet gold < fee: drop and log. The client has no code for this and waits for nothing.
   6. Deduct the fee from the unified wallet (social_friend.md 4.1-12, item group), set `report_day`, persist.
   7. Append `{ts, reporter_account, reporter_char, target_name, target_uid, category, category_name, content (cp949-decoded), fee}` to `server/reports.jsonl`.
   8. Push `S2C 0x95 {result=1, gold=<absolute post-fee balance>}` (9 B). The client shows "Reported successively." and updates the gold label.
   9. Every online GM gets `_notice(warn, "Report: <reporter> -> <target> (<category_name>)")`.
4. Penalties are applied later by a GM with /manner (F10.1).

### F10. GM commands: C2S 0x06
**F10.0 Enable and authorize.**
1. Add `"gm": 1` (and optionally `"gm_hidden": 0`) to a character in accounts.json.
2. `_build_en_opcode_07` (L2319-2323) emits `u16 gm_level = char.gm` and, when it is 1, `bool gm_hidden`.
3. `_handle_enter_world` sets `session['gm']`.
4. `_handle_gm` decodes `payload[0]` with the grammar for that sub-command. **Any 0x06 from a session without `gm` is logged and dropped**, because a patched client can send it.
5. Every accepted command is appended to `server/gm_audit.log` (`ts, account, char, sub, decoded fields`).

| Sub | Command | Server logic | Replies |
|---|---|---|---|
| 0x0A | **/manner** name delta | 1) Resolve the target character (online, then `name_index`). 2) `account.manner += delta`, persist. 3) If the target is online: push `S2C 0x97 {uid=T.uid, manner_delta=delta}` **to T only** until uids are unique. With the shared uid 1, a bystander's client would apply the delta to its *own* local player. Once uids are unique, also push to T's map peers. | Unknown target: `S2C 0x53 {char_name}` to the GM (`<name>is not in server.`). Success: `_notice(info, "Manner <name>: <+d> (now <m>)")`. T's client: manner label updates, red name at <= -10, chat blocked at <= -40, whisper blocked at <= -20 (the "mute"). |
| 0x00 | **/업데이트** | Reload content caches: `quest_defs._CACHE = None`, `del self._portals_cache`, `_gamedef_cache.clear()`, MONSTER tables if data-driven. Do not reload accounts.json (live sessions hold references). | `_notice(info, "Content reloaded.")` |
| 0x01 | **/not** text | `text = payload[2:]` (do not trust the u8, it can wrap). If it starts with `ice ` (the GM typed `/notice`), strip it; then strip spaces. Empty: drop. | `S2C 0x15 {2, "[Announce] " + text[:77]}` to **every** in-world session |
| 0x02 | **/shadow** | Toggle `char['gm_hidden']` and persist. Hide: push `S2C 0x06 PlayerDespawn {uid}` to map peers (needs unique uids and world visibility). Unhide: push `S2C 0x05` with the GM's record to peers (world group). Future 0x04/0x05/0x07 records for the GM carry `gm_hidden`. | `_notice(info, "Shadow ON/OFF")`. Q16: does `gm_hidden=1` on the GM's own 0x07 hide them from themselves? |
| 0x03 | **/reset** target code | Proposal (retail semantics unknown, Q8): target = character name (or account name if no such character); codes `qu` clear quest state, `st` reset stats, `mn` manner := 0. Apply, persist; takes effect on the target's next 0x03/0x07. | Unknown code or target: `_notice(warn, ...)`. Success: `_notice(info, ...)` |
| 0x08 | **/go** name | T must be in world, else `S2C 0x53 {name}`. T on another map: run the world group's `MapTransfer(gm_session, T.current_map, T.x, T.y)` (0x08 + 0x03 + 0x07 + HP/MP + 0x1A; needs position tracking from C2S 0x0D, world group). Same map: position keyframe per world_movement_npc.md, or `_notice(info, "Already on this map")` until that exists. Never re-send 0x07 on the same map (duplicate-entity risk, L1272-1275). | as stated |
| 0x07 | **/stop** N | **Destructive.** N == 0 (a typo such as `/stop x`): ignore. Otherwise: `_notice(announce, "Server maintenance in 3 minutes.")` + `S2C 0x3A` to all in-world sessions (every client self-closes about 185 s later). Set `self.maintenance = True`: new logins get `S2C 0x02 {result=0x0E}` ("Server is under maintenance"). Close all game sockets at T+190 s. Log N; the client text hardcodes 3 minutes. | as stated |
| 0x0C | **/kick** N | **Destructive.** N (u8) = slot number printed by `!who` (F11). Push `S2C 0x5D {reason=0}` ("Connection to Server got disconnected"; the client closes after 5 s), then close its socket after 6 s. | `_notice(info, "Kicked <name>")`, or `warn` for an unknown slot |
| 0x0B | **/proom** gm_uid room_no pts | Look up play room `room_no` (pvp_arena.md). Unknown: `_notice(warn, "room not found")`. Otherwise: members get `MapTransfer` variant `S2C 0xA3 {field map_code, game_time_ms}`; the creator gets `S2C 0xA4` and, if pts < 0, `account.manner += pts` + `S2C 0x97`; delete the room and update the room list. Ignore the client `gm_uid` except for logging. **Use this instead of pvp_arena.md's proposed `/delroom`**: a non-GM-parsed `/delroom` line is sent as C2S 0x03 and would be broadcast as chat. | as stated |

### F11. Dev commands (server-side, GM sessions only, C2S 0x03 lines starting with `!`, never broadcast)
- One router `_gm_chat_command(session, text) -> bool` with a registry dict, so other groups register there too: login_character's `/job` becomes `!job`, premium_cash's `!mall`/`!cash`.
- Replies go through `_notice`.
- Commands:
  - `!who`: list `slot: name map` (the slot numbers that /kick uses);
  - `!notice <text>`;
  - `!kick <name>`;
  - `!manner <name> <n>`;
  - `!mail <name> <text>` (injects a memo, test aid for F6);
  - `!gift <name> <item_id> <msg>` (test aid for F8);
  - `!gm <name> 0|1` (toggles the flag; takes effect at the next enter-world).
- A non-GM `!` line is ordinary chat.

---

## 3. Server state and data model

### 3.1 Session dict additions (the session is created at L509-518)
Keys shared with social_friend.md 3.2 are listed once; reuse the same names.

| Key | Set by | Used by |
|---|---|---|
| `no_enc` | `_dispatch`, on every packet | cross-session pushes |
| `in_world` | after 0x07 in `_handle_enter_world`; False in the `finally` at L578 | chat/whisper/notice fan-out |
| `char_name`, `current_map` | already set (L721-722, L1276) | whisper routing, map peers |
| `uid` | login (today `account_id = 1`, L2656; must become unique per account, social_friend 3.1 / login_character) | 0x97, /shadow, 0x6E uid resolution, 0x6B friend_id |
| `gm`, `gm_hidden` | enter-world, from the character | 0x06 authorization, 0x07 fields, whisper refusal bypass |
| `refuse` {whisper, exchange, party, talk, friend} | C2S 0x2B bytes 0..4 (grammar `0x42F904/0x2B`), C2S 0x40 | 0x09 status 0x67; 0x0F subtype 4 (social) |
| `chat_times`, `whisper_times` (deque) | F1, F2 | server rate limits |
| `msgr_synced`, `memo_ids_on_client` | F6 | memo push / 0x44 delete |
| `gifts_delivered` (set of gift ids) | F8 | once-per-connection 0x6D |
| `welcomed` | F4.3 | welcome line on first entry only |

### 3.2 GameServer-level state
- `online_by_name`, `online_by_uid`, `name_index`, `world_lock`, `db_lock`, `_push`: exactly as social_friend.md 3.3. Add `online_by_lname` (lower-case name -> session) for the whisper fallback.
- `self.maintenance` (bool) and `self.maintenance_close_at`.
- `self._grammars`: a `{spec_key: Grammar}` cache loaded once from `server/protocol_spec.json` (the wsdev.py `SPEC_PATHS[0]`, with a fallback to the corpus).
- `_slot_of(session)`: a stable per-connection slot counter for `!who` and `/kick`.

### 3.3 Persistence (accounts.json + new files)
```jsonc
"<username>": {
  "uid": 1,                     // social_friend 3.1 (login_character calls this account_id; pick ONE key)
  "manner": 0,                  // i32 -> S2C 0x02 manner_points (L2678) AND 0x07 karma (L2308). login_character.md names it manner_points; pick ONE key
  "social": {"report_day": "2026-09-17"},                         // F9
  "characters": [{
    "name": "TestHero",
    "gm": 0, "gm_hidden": 0,                                       // F10.0
    "friends": [], "memos": [{"id": 7, "from": "test", "text": "hi", "t": [2026,9,4,17,12,30,0,0]}]  // social_friend 3.5 + id
  }]
}
```
- Writes happen under `db_lock` (accounts.json is written from connection threads, L405-409).
- `server/reports.jsonl` and `server/gm_audit.log`: append-only.
- The gift inbox lives with premium_cash.md (`gift_inbox`). This group only reads it for 0x6D.

### 3.4 Content (gamedef.sqlite3 `items`, `data/en/items_en.json`)
| idx | EN name | Cash | Relevance |
|---|---|---|---|
| 1894 / 3320 | Note (x1) / Notes x11 | 1 | valid C2S 0x4B Note ids (Type 5, Cash_V 1/11) |
| 9999 | not in table | - | gift thank-you sentinel; consume nothing |
| 3327..3332 | WS Gift Card 1000..50000 | 0 | always displayable in the gift popup (client id range) |
| 3377..3379 / 3380..3381 | Super Megaphone / Megaphone | 1 | C2S 0x4C -> 0x90 (premium_cash) |

Gift gate column: `items.Cash`, i.e. itemdef+0x1F0 (1.4). The L2223 docstring that calls +0x1F0 "Kind" is wrong; Kind is itemdef+0x1CC per PROTOCOL.md S2C 0x1D.

### 3.5 Packet-building rules
```python
def _pkt(self, key, rec, assume=None):
    g = self._grammars.get(key) or self._grammars.setdefault(key, Grammar(SPECS[key]['grammar']))
    return g.encode(rec, assume=assume)
def name17(s):   return (s if isinstance(s, bytes) else s.encode('cp949', 'replace'))[:16]   # NUL guaranteed by padding
# length fields are NOT derived by the encoder: always set text_len/msg_len yourself
body = self._pkt('0x16', {'sender_name': name17(n), 'text_len': len(t), 'text': t})
```
- Text is **bytes** (the client uses CP949). `.decode('cp949','replace')` is only for logs and command parsing. `str` values go through latin-1 in the codec, so pass bytes.
- `str[N]` encode truncates to N and pads, but does not force a NUL (wsproto L285). Always truncate names to 16 bytes and fixed texts to N-1.

| Packet | Max bytes (after a NUL cut, no embedded NUL) |
|---|---|
| 0x16 text | 60 |
| 0x09 msg | min(60, 80-len(target)); channel form 70-len(target)-digits |
| 0x0A msg | min(60, 78-len(sender)); channel form 68-len(sender)-digits |
| 0x15 text | 88 (including `[Warning] ` / `[Announce] `) |
| 0x90 / 0x91 text | 87 |
| 0x61 msg | 58 |
| 0x78 sender / text | 16 / 92 |
| 0x6D sender / message | 16 / 90 |
| 0x0F names | 16, subtype in {1..6, 8} |

- C2S 0x06: pick the grammar by `payload[0]` (1.5). For sub 0x01, read the text as `payload[2:]`.
- The codec fixes B9 and B10 must land before 0x09 failure packets, `/stop`, `/kick`, 0x61 and 0x0C are built or parsed from grammars.

---

## 4. Current implementation status and proven bugs

| Spec | Status | Where |
|---|---|---|
| C2S 0x03 NormalChat | partial: echoed to the sender only, wrong format | `_dispatch` L612-614 -> `_handle_chat` L945-955 |
| S2C 0x16 ChatMessage | partial, broken wire format | L954; `_send_chat_line` L1535-1541 (6 callers) |
| S2C 0x0A WhisperReceived | wire-correct, but used only as a fake "welcome" | L765-772, L1306-1309 |
| C2S 0x02, 0x06 (x6), 0x4B (9999), 0x6B, 0x6E | missing: logged "Unhandled opcode" | `_dispatch` L601-667, else-branch L659-667 |
| S2C 0x09, 0x0F, 0x15, 0x61, 0x6D, 0x78, 0x90, 0x91 | missing: no builder | - |

**Proven bugs.** B1-B8 and B11-B13 are in `windslayer_server.py`; B9, B10 and B14 are in the tooling.

| # | Severity | Bug | Evidence | Fix |
|---|---|---|---|---|
| B1 | wrong_behavior | S2C 0x16 carries an extra leading `u8 1`. The client reads name = `\x01` + 16 name bytes and text_len = the 17th name byte (always 0), so the text is lost and the tail is ignored. | Spec 0x16: the first read is `char[17]` at 0x451B0D. L954 and L1540 `struct.pack('<B', 1)`; the docstring at L1536-1537 wrongly claims a leading count. LIVE_TEST_LOG "Chat hi": `S2C 0x16 21B` (= 1+17+1+2), display `test :`. `_chatcrop.png` shows a box glyph + `test :`. | Remove the byte; build from grammar `0x16` (F1). |
| B2 | wrong_behavior | Every server notice through `_send_chat_line` renders as `\x01Server : ` with no text | Callers L1020, L1347, L1356, L1375, L1383, L1440. Live quest accept `S2C 0x16 37B` = 1+17+1+18 ("Quest 26 accepted.") | F4: S2C 0x15 `_notice`. |
| B3 | wrong_behavior | Chat sender is the account username, not the character name | L949 `session.get('username')`; `char_name` exists since L721; LIVE_TEST_LOG bug 3 | `session['char_name']`. |
| B4 | wrong_behavior | Chat is never broadcast: only the sender's socket gets 0x16 | L955 `self._send_encrypted(sock, session, ...)` | F1 map-peer fan-out. |
| B5 | crash_or_desync (latent) | Outbound text cap is 255 but the client buffer is 61 B (0x16). Any server text over 60 bytes overflows the receiver's stack; exactly 61 has no NUL, and the `sprintf_s` then aborts. | L953 and L1539 `[:255]`; spec 0x16 gates (61-byte buffer, 0x58 sprintf) | Clamp per 3.5. |
| B6 | cosmetic | ASCII decode/encode turns every byte >= 0x80 (CP949) into `?` | L948 `.decode('ascii', errors='replace')`, L953 and L1538-1539 `.encode('ascii','replace')` | Keep bytes. |
| B7 | cosmetic | The "welcome" is a whisper from a fake player: it renders `<From: Server[Channel-1]> Welcome to WindSlayer!` (channel byte 1, so a channel tag appears), adds "Server" to the whisper dropdown, repeats as "Welcome!" on every map change, and would impersonate a real character named Server. The comment at L762-763 ("may be the trigger that finalizes the in-game transition") is disproved: the handler only touches the chat log and whisper list. | L765-772, L1306-1309; spec 0x0A semantics/references; screenshot `_cap_click_603_545.png` bottom line | F4.3. |
| B8 | wrong_behavior | C2S 0x02/0x06/0x4B/0x6B/0x6E are unhandled. Whispers vanish (no 0x09, so the sender gets no feedback at all). A Note 0x4B leaves the client stuck on "Waiting for the server to response." GM commands and reports do nothing. | `_dispatch` L601-667 has no branches; spec 0x461064/0x4B hazard "server MUST send S2C 0x77" | Section 5 items. |
| B9 | wrong_behavior (codec) | `wsproto._eval` treats hex literals in conditions as unknown client-state names: `re.findall(r'[A-Za-z_]\w*')` extracts `x66` from `0x66`, and the `0x...` filter on L184 never matches it. | wsproto.py L183-187. **Reproduced:** `Grammar(0x09).encode({'status':0x66,'target_name':'Alice'})` gives **19 B** (a spurious msg_len byte; should be 18); decoding a real 18-B 0x66 packet raises "underflow: need 1 byte(s) at offset 18"; `Grammar(0x444C91/0x06).decode(07 03)` and `(0C 01)` raise "1 trailing byte(s)"; `Grammar(0x0C).encode({'result':0x0B,...})` gives 18 B instead of 24. | Replace hex literals before the name scan: `py = re.sub(r'0x[0-9a-fA-F]+', lambda m: str(int(m.group(), 16)), py)` (fixes social_friend.md 4.2-1 too). |
| B10 | cosmetic (tooling) | `wsdev.py sendspec 61` injects a 0-byte packet: the grammar is gated on `client.messenger_room_id != 0` and `cmd_sendspec` calls `encode(fields)` with no `assume` | wsdev.py L447. Reproduced: without assume `0x61.encode(...)` gives `b''`; with assume, 23 B | `--assume` flag or `"__assume__"` JSON key. Until then use raw `send`. |
| B11 | wrong_behavior | A GM can never exist: the 0x07 `gm_level` is hardcoded 0 and mislabelled `field12`. `+0xE2` is labelled "marriage/spouse", but the spec says `u16 room_id` + `u8 room_type` + `str[17] room_title` (wire order is right, label wrong). | L2310-2323; spec 0x07 fields `room_id`, `gm_level` (entity+0x12, read at 0x44F08D), `gm_hidden` (entity+0x14) | F10.0. |
| B12 | wrong_behavior | The privacy refuse flags are ignored and the 0x2B docstring is wrong. Payload bytes 0..4 are refuse_{whisper, exchange, party, talk, friend}, then `str[16] p2p_ip`, `u32 p2p_udp_port` (42907), `str[17] name` - not "zeros / 10.5.0.2 / 0xA79B magic". | L689-695, L701-703; spec 0x42F904/0x2B (capture `00 00 00 00 00 | 31 32 37 ...`); spec 0x4414BA/0x40 | Decode into `session['refuse']`; handle C2S 0x40. |
| B13 | wrong_behavior | Manner points are neither persisted nor consistent, so a GM mute or report penalty cannot stick across relog: S2C 0x02 sends i32 0 in the `manner_points` slot (labelled `premium_flags`), and 0x07 sends karma i32 1 | L2678 (`struct.pack('<i', 0)` after two u8), L2308 `i32(1)`; spec 0x02 `manner_points` -> scene+0xEE0, spec 0x07 `karma` -> entity+0x15D8 | Account `manner` in both. |
| B14 | cosmetic (harness) | wsview cannot type slash commands, negative numbers or capitals: `_vk_of('/')` returns `ord('/')` = 0x2F (VK_HELP, no scancode), `'-'` gives 0x2D (VK_INSERT), and there is no shift chord | wsview.py `_VK` L163-168, `_vk_of` L172-181 | Add `slash` 0xBF, `minus` 0xBD, `period` 0xBE, `shift+<key>` chords, and `type "<text>"`. |

Related bugs owned elsewhere that block this group:
- 0x2F never answered and logged as "arena query" (L630-631; social_friend 4.1-1). It is the memo resync hook.
- uid 1 for every session (L2656; social_friend 4.1-2).
- Two gold sources: 0x03 sends 100000 (L2044) while `_wallet` defaults to 999999 (L994-1000). This skews the 0x95 absolute gold (social_friend 4.1-12).
- Admin injection reaches char-select sessions (L459-461; social_friend 4.1-9).
- `str[N]` encode does not force a NUL (wsproto L285; social_friend 4.2-3).

---

## 5. Implementation plan

Ids are prefixed `chat_mail_gm-`. Effort: S < 1 day, M = 1-3 days, L > 3 days.

| Id | Pri | Eff | Depends on | Work |
|---|---|---|---|---|
| chat-builders | P0 | S | - | Grammar-driven `_pkt`, `name17`, clamps (3.5). Rewrite `_handle_chat` for self-echo: no count byte, `char_name`, bytes, NUL cut, clamp 60, `!` router hook (B1, B3, B5, B6). Keep `_send_chat_line` as a thin wrapper over `_notice` until callers move. |
| system-notices | P0 | S | chat-builders | `_notice` (0x15), `_orange` (0x90), `_green` (0x91). Migrate L1020/1347/1356/1375/1383/1440. Replace the 0x0A welcome with a first-entry-only 0x15 (B2, B7). |
| codec-fixes | P1 | S | - | B9 hex literals in `_eval`; B10 `assume` for `sendspec`; unit tests on 0x09/0x06/0x0C/0x61 round-trips. |
| harness-typing | P1 | S | - | B14 wsview key tokens + `type` command, so slash commands and `/manner x -50` can be driven live. |
| offline-dispatch-tests | P1 | S | chat-builders | `server/test_chat_gm.py`: a fake session with a `socketpair`, calling `_dispatch` with captured C2S payloads (0x02, 0x06 x9 sub-ops, 0x4B 9999, 0x6B, 0x6E) and decoding the replies with CEncMsg + grammars. Covers `/업데이트`, which cannot be typed. |
| online-registry | P1 | M | login_character/social_friend unique uid | Shared with social_friend.md 3.3 (implement once): `in_world`, `no_enc` capture, `online_by_name/uid/lname`, `_push`, `_map_peers`, disconnect cleanup, `db_lock`. |
| privacy-flags | P1 | S | - | Decode C2S 0x2B bytes 0..4 into `session['refuse']`; add a C2S 0x40 handler (5 x u8) (B12). |
| map-chat-broadcast | P1 | S | chat-builders, online-registry | F1: 0x16 to map peers including the sender, server manner gate, rate limit (B4). |
| whisper | P1 | M | codec-fixes, chat-builders, online-registry, privacy-flags | F2: C2S 0x02 -> 0x09 (0x65/0x66/0x67) + 0x0A with clamps. |
| gm-flag-manner | P1 | S | - | accounts.json `gm`/`gm_hidden`/`manner`; 0x07 `gm_level` + `gm_hidden` (B11); 0x02 `manner_points` L2678 + 0x07 karma L2308 (B13); `session['gm']`. |
| gm-dispatch | P1 | M | gm-flag-manner, codec-fixes, system-notices | `_handle_gm` sub-op decoder, authorization, audit log. Sub 0x01 /not broadcast (needs online-registry for fan-out; a sender-only fallback is fine before that); sub 0x00 reload. |
| dev-commands | P2 | S | gm-flag-manner, system-notices | F11 `!` router with a registry (`!who`, `!notice`, `!kick`, `!manner`, `!mail`, `!gift`, `!gm`); expose it to other groups. |
| gm-manner | P2 | M | gm-dispatch, online-registry | F10 /manner: persist, 0x97 to the target (peers once uids are unique), 0x53 for an unknown target, 0x15 feedback. |
| gm-go | P2 | M | gm-dispatch, world `world-maptransfer`, C2S 0x0D position tracking | F10 /go via `MapTransfer`; 0x53. |
| gm-stop-kick | P3 | M | gm-dispatch, dev-commands | F10 /stop (0x15 + 0x3A + maintenance + 0x02 result 0x0E + socket close) and /kick (0x5D + close). |
| gm-shadow-reset-proom | P3 | M | gm-dispatch; world 0x05/0x06 visibility; pvp_arena play rooms + `world-maptransfer` variant | F10 /shadow, /reset, /proom (replaces pvp_arena `/delroom`). |
| memo-delivery | P2 | M | codec-fixes, online-registry, social_friend F1 (0x2F handler) and F9 (memo store) | 0x78 builder with SYSTEMTIME, resync on 0x2F, live push with the `msgr_synced` guard, C2S 0x44 delete by delivered ids (F6). |
| note-reply-9999 | P2 | S | memo-delivery | `_handle_note` branch for 9999 (F7): memo to the gift sender, no item, no reply (pending T-77-9999). Routes 1894/3320 to social_friend F9. |
| report | P2 | M | online-registry, gold wallet unification (item/social groups) | F9: C2S 0x6E -> 0x95 (2/7/1+gold), fee recompute, `report_day`, reports.jsonl, GM alert. |
| friend-chat-relay | P2 | S | social_friend friend store + 0x0B, online-registry | F3: C2S 0x6B -> 0x91 to online stored friends, name anti-spoof, no echo. |
| room-builders | P2 | S | codec-fixes | F5 builders `_room_joined`/`_room_refused`/`_room_line` with clamps and assume, for social_friend F5. |
| gift-inbox | P3 | M | premium_cash 0x47/0x6A/gift_inbox, codec-fixes | F8: display gate (`items.Cash` or 3327-3332, sender name rules), once-per-connection 0x6D, notified on 0x6A. |

Suggested order: chat-builders -> system-notices -> codec-fixes -> harness-typing -> offline-dispatch-tests -> gm-flag-manner -> gm-dispatch -> dev-commands -> privacy-flags -> online-registry -> map-chat-broadcast -> whisper -> memo-delivery -> report -> gm-manner -> note-reply-9999 -> friend-chat-relay -> room-builders -> gm-go -> gm-stop-kick -> gift-inbox -> gm-shadow-reset-proom.

---

## 6. Live test plan

**Setup**
- Client 1: `python wsdev.py up` (test/test, TestHero).
- Two-player tests: `python wsdev.py --client 2 up` (admin/admin, character `test`).
- `send`/`sendspec` inject into **every** live session.
- Name-routed packets (0x16, 0x09, 0x0A, 0x61, 0x91, 0x53) work even while both sessions share uid 1. uid-routed ones (0x97, 0x06 despawn) do not.

**HUD targets** (800x600 client coordinates)
- Msg (553,545), Gift (603,545), Report (650,545).
- Chat input: the bottom-left strip at about (250,590). Confirm with `python wsdev.py shot`.
- Msg and Gift are greyed until 0x78 / 0x6D.

**Typing**
- Letters and digits work today (`key h i enter`); they are lower-case.
- `/`, `-` and capitals need item harness-typing.

**Crash-risk rule:** never inject 0x16 text_len > 60, 0x15 > 88, 0x61 > 58, 0x0F subtype 0/7/9+, 0x09/0x0A with a 17-byte non-NUL name, or 0x78/0x6D records with non-terminated strings.

### 6.1 S2C injection (works against today's server)
| Id | Opcode | Exact action | Expected visible result | Risk |
|---|---|---|---|---|
| T-16 | 0x16 | `python wsdev.py sendspec 16 '{"sender_name":"Alice","text_len":5,"text":"hello"}'` | White `Alice : hello` in the chat log (proves B1's fix format) | safe |
| T-16-self | 0x16 | `sendspec 16 '{"sender_name":"TestHero","text_len":2,"text":"hi"}'` then `shot` | `TestHero : hi`; answers Q2 (bubble over own character?) | safe |
| T-16-emote | 0x16 | `sendspec 16 '{"sender_name":"TestHero","text_len":6,"text":"/heart"}'` | No chat line; a heart emote only if the local entity matches (Q2) | safe |
| T-0A | 0x0A | `sendspec 0A '{"channel":101,"sender_name":"Alice","msg_len":5,"message":"hello"}'` | `<From: Alice> hello` (whisper colour); Alice appears in the whisper dropdown | safe |
| T-09 | 0x09 | `sendspec 09 '{"status":101,"target_name":"Alice","msg_len":2,"message":"hi"}'`, then the same with `"status":3` | `<To: Alice> hi`, then `<To: Alice[Channel-3]> hi` | safe |
| T-09-fail | 0x09 | Raw, because of B9: `send 09 66416c696365000000000000000000000000` then `send 09 67416c696365000000000000000000000000` | `<Alice>can not be found.` in the log; then notice `<Alice>is rejecting whispers.` | safe |
| T-15 | 0x15 | `sendspec 15 '{"msg_type":2,"text_len":14,"text":"[Warning] test"}'`; `{"msg_type":2,"text_len":16,"text":"[Announce] hello"}`; `{"msg_type":1,"text_len":11,"text":"Quest done."}`; `{"msg_type":0,"text_len":4,"text":"info"}` | Red, light blue, yellow-green, light blue lines with no sender prefix | safe |
| T-90/91 | 0x90, 0x91 | `sendspec 90 '{"text_len":11,"text":"Bob : hello"}'`; same with `91` | Orange line; green line | safe |
| T-0F-fail | 0x0F | `sendspec 0F '{"subtype":8,"target_name":"Alice"}'`, then subtypes 3, 4, 5, 6, 2 | Modal popups: `<Alice>is not on line.` / refused personal chat / rejecting chatting / room full / in conversation / other channel; OK closes each | safe |
| T-0F-room | 0x0F | `sendspec 0F '{"subtype":1,"room_id":7,"member_count":2,"repeat[member_count]":[{"member_name":"TestHero"},{"member_name":"Alice"}]}'` (40 B) | Window 0x177 opens with 2 members and `<Alice> has entered.` | state_change |
| T-61 | 0x61 | Before T-0F-room: `send 61 416c6963650000000000000000000000000568656c6c6f` (nothing shows). After T-0F-room: the same, then `send 61 546573744865726f0000000000000000000568656c6c6f` | First send: nothing. Then `Alice : ` (blue) + `  hello`; own-name header pink | state_change |
| T-35/36-cap | C2S 0x35, 0x36 | After T-0F-room: click the room input (locate with `shot`), `cap 3 key h i enter`; then close window 0x177 with `cap 3 click <x> <y>` | `C2S 0x35 payload=3B 02 68 69`; `C2S 0x36 payload=0B`; server logs "Unhandled" | safe |
| T-0F-restore | 0x0F | After T-0F-room: take the map-101 portal (`hold down` on the sparkle), `send 61 ...` (expect nothing), `sendspec 0F '{"subtype":1,"room_id":7,"member_count":0}'` (6 B), `send 61 ...` again | Decides F5: after the count-0 0x0F, lines print again with no duplicate member rows; also shows whether 0x03 closes window 0x177 (and sends 0x36) | state_change |
| T-78 | 0x78 | `sendspec 78 '{"count":1,"repeat[count]":[{"sender_name":"Alice","text":"Hi from Alice","year":2026,"month":9,"day_of_week":4,"day":17,"hour":12,"minute":30}]}'` (127 B), then `click 553 545`, `shot` | Msg button enabled and blinking; window 0x3FE lists Alice, `2026/9/17 12:30`, body | state_change |
| T-78-reset | 0x78 | T-78, take a portal (0x03 replay), click Msg | Window empty and button state noted: confirms 0x03 frees the memo list (Q5) and whether the button stays lit | state_change |
| T-44-cap | C2S 0x44 | With 0x3FE open: close it (ctrl 2/9), then `cap 3 click <OK x> <OK y>` on the confirm box | `C2S 0x44 payload=0B`; Msg greys out; list empty | safe |
| T-6D | 0x6D | `sendspec 6D '{"gift_count":1,"repeat[gift_count]":[{"sender_name":"Alice","message":"enjoy","item_id":3327}]}'` (127 B), `shot` | Gift button (603,545) enabled; no popup (needs 0x6A). The queue persists for the connection. | state_change |
| T-53 | 0x53 | `sendspec 53 '{"char_name":"Alice"}'` | `Aliceis not in server.` (the /go and /manner not-found reply) | safe |
| T-95 | 0x95 | `sendspec 95 '{"result":7}'`; `{"result":2}`; `{"result":1,"gold":12345}` | "only once a day" / "doesn't exist" / "Reported successively." with the gold label at 12345 | state_change (client gold != server gold until the next 0x03) |
| T-97-mute | 0x97 | `sendspec 97 '{"uid":1,"manner_delta":-50}'`, then type `hi` + Enter; then `{"uid":1,"manner_delta":50}` | Manner shows -50 in red and chat is refused with the low-manner message; restored after +50 (validates the /manner mute design) | state_change |
| T-3A | 0x3A | **Last test of a session only:** `sendspec 3A '{}'` | Red `[Announcement] Server will be down in 3 minutes...`; client closes itself about 185 s later | disruptive |
| T-5D | 0x5D | **Last test only:** `sendspec 5D '{"reason":0}'` | Dialog "Connection to Server got disconnected"; client closes after 5 s (/kick design) | disruptive |

### 6.2 C2S capture (ground truth now; re-run after each item for the reply)
| Id | Opcode | Exact action | Capture today -> expected after implementation | Risk |
|---|---|---|---|---|
| T-03 | C2S 0x03 | Click chat input, `python wsdev.py cap 3 key h i enter` | `C2S 0x03 payload=3B 02 68 69`. Today: `S2C 0x16 21B`, line `\x01test :`. After chat-builders: `S2C 0x16 20B`, line `TestHero : hi`. | safe |
| T-03-spam | C2S 0x03 | `cap 3 key h enter h enter` | One 0x03 only; the second line shows "Do not Spam." | safe |
| T-03-mode5 | C2S 0x03 | (harness-typing) `cap 3 key slash a space h i enter` | `payload=6B 05 68 69 00 68 69`; after the fix the echo reads `TestHero : hi` (NUL cut) | safe |
| T-03-emote | C2S 0x03 | (harness-typing) `cap 3 key slash h e a r t enter` | `payload=7B 06 2f6865617274`; after the fix it is echoed as 0x16 and the client shows no line (emote path) | safe |
| T-02 | C2S 0x02 | (harness-typing) `cap 3 key slash w space a l i c e space h i enter` | `C2S 0x02 payload=20B 616c696365 00x12 02 6869`. Today: nothing comes back (B8). After whisper: `S2C 0x09 18B`, line `<alice>can not be found.` | safe |
| T-02-self | C2S 0x02 | (harness-typing, shift) `/w TestHero hi` | No packet; client message "You can't send a message to yourself." | safe |
| T-06-nongm | C2S 0x03 | GM flag off: type `/not hello` | Sent as chat: `C2S 0x03 payload=11B 0a 2f6e6f742068656c6c6f` | safe |
| T-06-notice | C2S 0x06 sub 01 | After gm-flag-manner (`"gm":1` on TestHero, `wsdev.py restart`): `shot` shows "Wind Master" under the name; then `cap 3 key slash n o t space h e l l o enter` | `C2S 0x06 payload=8B 01 06 2068656c6c6f`. After gm-dispatch: `S2C 0x15` light blue `[Announce] hello` on every client (leading space stripped) | safe |
| T-06-manner | C2S 0x06 sub 0A | GM: `/manner test -50` (harness `minus`) | `payload=22B 0a 74657374 00x13 ceffffff`. After gm-manner: GM gets `S2C 0x53` if `test` is offline, else 0x15 feedback, and client 2 gets 0x97 (chat blocked) | state_change |
| T-06-go | C2S 0x06 sub 08 | GM: `/go test` | `payload=18B 08 74657374 00x13`. After gm-go: 0x53 `testis not in server.` when offline; map transfer when online | safe / state_change |
| T-06-simple | C2S 0x06 sub 02/07/0C | GM: `/shadow`, `/stop 3`, `/kick 1` | `payload=1B 02`; `2B 07 03`; `2B 0c 01`. **Capture only while unimplemented**; after gm-stop-kick these disconnect clients | safe now / disruptive later |
| T-06-reset | C2S 0x06 sub 03 | GM: `/reset alice ab` | `payload=133B 03 616c696365 00x124 616200` | safe |
| T-06-proom | C2S 0x06 sub 0B | GM: `/proom 5 -10`; then `/proom 5 10` | `payload=11B 0b 01000000 0500 f6ffffff`; the second shows "Points to deduct must be less than 0." with no packet | safe |
| T-06-update | C2S 0x06 sub 00 | Not typeable without a Korean IME: run `offline-dispatch-tests` with payload `00` | Handler replies `S2C 0x15 {1,"Content reloaded."}` | safe |
| T-6B | C2S 0x6B | `sendspec 0B '{"friend_capacity":20,"friend_count":1,"repeat[friend_count]":[{"name":"Alice","channel":1,"friend_id":2,"status":0}]}'`, then (harness-typing) `cap 3 key slash f space h i enter` | Local green `TestHero : hi`; `C2S 0x6B payload=20B 01 01 02000000 0d 546573744865726f203a206869`. After friend-chat-relay: nothing echoed to the sender (Alice offline, so no 0x91). Without the 0x0B: echo only, no packet. | safe |
| T-6E | C2S 0x6E | `click 650 545`; click the name edit (`shot` to locate) and `key a l i c e`; click the content box and `key s p a m`; `cap 3 click <OK>` | `C2S 0x6E payload=31B 00000000 616c696365 00x12 64000000 00 04 7370616d` (category 0 = Abuse by default). After report: `S2C 0x95 1B {2}` "The character doesn't exist." | safe |
| T-4B-note | C2S 0x4B (Note) | Needs an owned Note 1894 (premium_cash 0x6F delivery); double-click it, fill recipient/text, Send | `payload=110B 6607 <name17> <text91>`. Today the client stays on the wait modal (B8). After social_friend F9: `S2C 0x77` closes it | state_change |
| T-4B-gift / T-77-9999 | C2S 0x4B (9999) | Needs T-6D + premium_cash `!mall` (0x6F/0x6D/0x6A); type `thanks` in popup 0x3EF, OK | `payload=110B 0f27 416c696365 00x12 7468616e6b73 00x85`. After note-reply-9999: memo stored for Alice (`!mail`-style check via T-78 on Alice). Then inject `sendspec 77 '{"result":1,"cash_item_serial":0}'` with the next popup open to settle Q6. | disruptive (0x6A swaps the world map to the cash-shop preview) |

### 6.3 Two-client end-to-end (after online-registry + the named item)
| Id | Opcode | Action | Expected | Risk |
|---|---|---|---|---|
| T-03-2p | 0x03 -> 0x16 | Both clients on map 101; client 1 types `hi` | Both print `TestHero : hi`; client 2 shows a bubble over TestHero once world visibility (0x04/0x05) exists | safe |
| T-03-2p-map | 0x03 -> 0x16 | Client 2 on map 102; client 1 types `hi` | Only client 1 prints it (map scope) | safe |
| T-02-2p | 0x02 -> 0x09/0x0A | Client 1: `/w test hi` | Client 1 `<To: test> hi`; client 2 `<From: TestHero> hi` and TestHero added to its dropdown | safe |
| T-02-refuse | 0x40 -> 0x09(0x67) | Client 2: Option -> refuse whisper -> Apply (`python wsdev.py --client 2 cap 3 click <apply>` shows `C2S 0x40 payload=5B 01 00 00 00 00`); client 1 whispers again | Client 1 notice `<test>is rejecting whispers.` | safe |
| T-6E-ok | 0x6E -> 0x95 | Client 1 reports `test` (Spamming); then reports again | First: `S2C 0x95 9B {1, gold-100}`, "Reported successively.", gold -100, one line in reports.jsonl. Second: `{7}` "only once a day" | state_change |
| T-06-manner-2p | 0x06 sub 0A -> 0x97 | GM client 1: `/manner test -50`; client 2 types `hi`; GM: `/manner test 50` | Client 2's manner goes red and its chat is refused; restored after +50; persisted across `--client 2 restart` (0x02 `manner_points`) | state_change |
| T-6B-2p | 0x6B -> 0x91 | After social_friend friends: client 1 `/f hi` | Client 2 green `TestHero : hi`; client 1 gets only its local echo (no duplicate) | safe |
| T-78-2p | 0x4B(9999)/`!mail` -> 0x78 | GM client 1: `!mail test hello`; client 2 opens Msg, closes it and confirms; client 2 takes a portal | Client 2 Msg lights, 1 memo; after 0x44 and the portal, no memo returns | state_change |

---

## 7. Open questions
1. **Q1.** What is the chat mode `[g_ui_root+0x44]` right after login? The "To All" label suggests 5. It only changes msg_len for prefixed or >60-byte lines.
2. **Q2.** Does S2C 0x16 give the *local* player a bubble or emote? The spec matches only remote entities (alive state 3), but our S2C 0x07 local entity also has alive=3 (T-16-self).
3. **Q3.** Which S2C did retail EN use for party chat (C2S 0x6A) and megaphones (C2S 0x4C)? Both other groups picked 0x90 orange; no EN evidence ties either one to it.
4. **Q4.** Does S2C 0x0F subtype 1 go to both sides, and does member_list include the receiver? Does hiding window 0x177 clear its +0xBC member list? Does a server-sent 0x03 close window 0x177 (which would send C2S 0x36)? See T-0F-restore.
5. **Q5.** Does 0x03 (`FUN_0046f860` -> `FUN_0046fc40`) free the memo list and also reset the Msg button state? This decides "re-send all memos on every 0x2F" (T-78-reset).
6. **Q6.** Should the 9999 thank-you 0x4B get S2C 0x77 `{1,0}` (premium_cash.md F12) or no reply (this doc)? The 0x77 success path closes window 0x3FD and pops a modal while the next gift popup may be open (T-77-9999).
7. **Q7.** Is itemdef+0x1F0 exactly `gamedef.items.Cash`? The evidence is strong (1.4): gift cards Cash=0 are special-cased by id, notes and megaphones Cash=1. Confirm with one injected 0x6D using a Cash=1 item that is not a gift card.
8. **Q8.** What did retail do for GM sub-ops 0x00 (/업데이트), 0x02 (/shadow), 0x03 (/reset code values), 0x07 (/stop N units) and 0x0C (/kick u8 N)? The client never reads a reply, so F10 is a proposal.
9. **Q9.** Are manner points per account (S2C 0x02 carries them before character select) or per character (0x07 karma, 0x52)? Also pick one JSON key: `manner` (social_friend) or `manner_points` (login_character). Likewise `uid` vs `account_id`.
10. **Q10.** Are HUD window 3 controls #6 (0x6D) and #8 (0x78) really the Gift and Msg buttons (T-6D, T-78)?
11. **Q11.** Does Fireway `GetDataFromPacket(char*, N)` bounds-check N against the packet? This governs how bad a short or long 0x09/0x0A/0x16/0x61/0x78 packet is.
12. **Q12.** What is the maximum IME chat buffer length? Can a typed `/not` line wrap its u8 length, or does a 61+ byte line abort in `sprintf_s` first?
13. **Q13.** Does `FUN_0043df40(name, 0)` skip the reserved-word list (0x4B spec says flag 0 skips it; the 0x6D spec lists 'gamemaster'/'yahoo')? This matters for system-generated gift sender names; use plain player-like names.
14. **Q14.** Is C2S 0x6B `friend_id` the session uid? Whatever social_friend sends in 0x0B is what comes back.
15. **Q15.** Is the retail "once a day" report limit per reporter (design) or per reporter-target pair? Did retail resolve by `target_uid` or by name?
16. **Q16.** Does `gm_hidden=1` in the GM's *own* 0x07 record hide the GM from themselves, or only from others?
17. **Q17.** Which target_uid does the HUD Report path send: 0, or the stale value of window 0x434 +0x134?
18. **Q18.** Is normal chat (0x16) map-scoped (design) or channel-wide on retail? The "To All" label is ambiguous.
