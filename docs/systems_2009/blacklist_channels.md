# blacklist_channels: the messenger Blacklist (S2C 0xBD-0xBF, C2S 0x93/0x94) and Channels (version list, load labels, in-game channel change, per-channel isolation)

Client: EN Outspark v1.04 Build 14 (2009-01-23), `WindSlayer.exe` in `Desktop\WindSlayer2009`.

Sources, in priority order:

1. The 2009 Ghidra corpus `re_tools/corpus_2009`:
   - `decomp/` and `asm/`;
   - `protocol_spec_2009.json`, the 0x01/0x93/0x94/0xBD/0xBE/0xBF entries;
   - `dispatch_2009.json`, `strings.tsv`, and the exe copy `WindSlayer_2009.exe` for data tables.
2. The 2009 UI data. `hs/windslayer.hui` was decoded to a copy in `_work/blacklist_channels/windslayer_hui.txt` (`dec_hs.py`). `hs/UILngKo.lng` is plain text (English). Window and control dumps are in `_work/blacklist_channels/ui_windows*.txt` (`ui_dump.py`).
3. The retail video catalog, `RETAIL_VIDEO_CATALOG_2026-09-24.json`.
4. The live server, `WindSlayer2Game/server`. It was read only, never edited.

PySlayer has no blacklist, and it runs a single channel (`channel_server.py` sends one fixed 0x01).

**Tags.** Every claim carries one of these tags:

- **[V]**: VERIFIED. I read it in the 2009 decomp or asm at the VA given, or in the decoded data file.
- **[V-2008]**: the 2009 handler has the same fingerprint as the 2008 one, and the meaning comes from the 2008 analysis.
- **[I]**: INFERRED. A design proposal or a reasoned guess. It is not read in code.

**Notation:**

- `gs` = game_state (0x54EBD0).
- `M` = messenger object `[gs+0x508]`.
- `scene` = `[gs+0x4F0]`.
- Window/control ids are the ones FUN_0049e070 / FUN_00498120 take. Control numbers are 1-based. The client walks the control list 0-based: "index 3" is ctrl 4.

| Part | Dir | Opcodes / UI |
|---|---|---|
| A Blacklist | C2S | 0x93 BlacklistAdd, 0x94 BlacklistRemove |
| | S2C | 0xBD BlacklistList, 0xBE BlacklistAddResult, 0xBF BlacklistRemoveResult |
| | Uses | S2C 0x0A, 0x16, 0x45, 0x4E, 0x0D, 0x10 (client-side filters); C2S 0x02, 0x20, 0x27, 0x30, 0x33, 0x5C (client-side blocks); C2S 0x2F (resync cue) |
| B Channels | S2C | 0x01 channel list (version socket), 0x03 `channel_id`, 0x99 sub 8 "In channel N.", 0x02 (relogin path) |
| | C2S | 0x01 relogin, 0x2B automatic enter-world. There is **no channel-change packet**. |
| | UI | Server select window 0x73, system menu 0x1AE ("Change Avatar" / "Change Channel"), minimap windows 4 / 0x17 |

---

## Part A. Blacklist

### A.1 Client state [V]

The blacklist lives on the same messenger object as friends, mentors and mentees. That object is `M = [gs+0x508]`. The whisper handler loads it as `MOV EAX,[EDX+0x508]` at 0x456061.

| Field | Meaning | Written by |
|---|---|---|
| M+0x34 list (head +0x38, tail +0x3C, count +0x40) | Blacklist, 0x24-byte nodes from `operator new`, **not zeroed**: +0x00 char[17] name, +0x18 u32 char_id. Bytes +0x11..+0x17 and +0x1C..+0x23 are uninitialised. | 0xBD (replace), 0xBE (append), 0xBF (remove by name), FUN_0047a850 (free all) |
| M+0x04 list (head +0x08) | Friend list. Used by the "is a friend" check FUN_0047fd80. | 0x0B/0x0C/0x0E |
| M+0x14 list (head +0x18) | Mentor list. 0x7A appends with `FUN_0045f3f0(M+0x14)` (0x47CB1A/0x47CB1E). | 0x03 mentor block, 0x7A |
| M+0x130 | Copy of g_sock_game. All 0x93/0x94 sends and all SubHandler3 reads go through it. | FUN_0047a1a0 (the 0x03 messenger reset) |
| M+0x138 / +0x14C / +0x150 | Own name, own channel, own uid. They are copied from `[M+0x128]` +0x1C4/+0x1D5/+0x1DC in FUN_0047a1a0. | every S2C 0x03 |

- The three fields in the last row line up with scene+0x20C (name), scene+0x21D (0x03 `channel_id`, written at 0x452A95) and scene+0x224 (local uid) if `[M+0x128]` = scene+0x48. That mapping is [I].
- **Every S2C 0x03 wipes the blacklist.**
  - The 0x03 handler calls FUN_0047a1a0 (the messenger reset), which calls FUN_0047a850. That function frees every node of M+0x38 and sets head, tail and count to 0. [V]
  - At the end of every 0x03 the client sends C2S 0x2F (0x45353B) [V, spec]. The server must answer that 0x2F with S2C 0xBD, the same way it re-sends the friend list.
- **SubHandler3 gate** (FUN_0047bd60 prologue): the handler does nothing unless `[M+0x124]` and `[M+0x12C]` are non-NULL. [V]
  - The reads use `[M+0x130]`, which is NULL until the first 0x03.
  - **Send 0xBD/0xBE/0xBF only to sessions that have received a 0x03.** In practice that means in reply to 0x2F or later.
  - What `GetDataFromPacket` does with a NULL `this` was not traced. [I]

### A.2 UI [V]

The UI definitions come from the decoded `windslayer.hui`, with texts from `UILngKo.lng`.

The Community window is window 0x176. Its title text is UI 3653 "Buddy list". The retail 2010 video 5FvE81G417w at 11:15 shows it with the tabs "Friend / Guild / Mentor / Blacklist".

**Blacklist tab and buttons of window 0x176:**

| Ctrl | UI id / text | Role (FUN_00480430, window 0x176) |
|---|---|---|
| 29 (0x1D) | 9113 "Blacklist" (tab) | Case 0x1D: FUN_0047b950, then FUN_0047b3e0. The redraw lists the blacklist names. |
| 19 (0x13) | 2662 "(20/20)" label | On the blacklist tab, FUN_0047b3e0 writes `"(%u/%u)"` with (count, **10**). |
| 30 (0x1E) | 2651 "Add" | If count != 10: open dialog 0x4C9 with focus on ctrl 5 (0x480999). If count == 10: popup **"Maximum character level"** (0x48097D, string 0x52A2B8). This is a wrong string in the EN build; the text is kept as the client shows it. |
| 31 (0x1F) | 2652 "Delete" | No row selected: "Please select a character name. " (0x4809F6). Otherwise dialog 0x4CA: `+0x134` = selected node +0x18 (char_id), ctrl 4 caption = node name (0x480A41-0x480A57). |
| 7 / 8 / 9 / 27 / 28 | Converse / Add / Delete / Add Mento / Delete Mento | Hidden while the blacklist tab is shown (FUN_0047b3e0 blacklist branch). |

**Dialogs:**

| Window | Contents | Leads to |
|---|---|---|
| 0x4C9 (UI 9205 "Pop-Up (Add to Blacklist)") | ctrl 5 = edit box (text at control+0xD1); ctrl 2 OK, ctrl 3 Cancel | OK with an empty name: "Please enter a character name." (0x4813AF). Otherwise FUN_0047afb0 (C2S 0x93), then the dialog closes (0x4813DB / 0x4813FF). **No wait box.** |
| 0x4CA (UI 9206 "Message of Delete Black") | ctrl 4 = name label (UI 9207 "Name", text at +0x50); `+0x134` = char_id; ctrl 2 OK | OK with window and name non-empty: C2S 0x94 (0x4814AD-0x4814E2), then the dialog closes. **No wait box.** |

- The guild window 0x4B3 has its own "Blacklist" tab button (ctrl 59, UI 9113). Its ctrls 0x0B, 0x0D and 0x3B all run FUN_0047b950 + FUN_0047b3e0 and then show window 0x176 (FUN_00480430 case 0x4B3). [V]
  - Whether the Blacklist tab ends up selected depends on FUN_0047b950, which was not traced. [I]
- There is **no "Add to Blacklist" entry in the player popup** 0x50. Its 11 controls are Char. Info / Trade / Make Party / Whisper / Add as Friend / Converse / Copy Name / Praise / Report. [V] A name can only be blacklisted by typing it in 0x4C9.
- The key that opens 0x176 was not traced. 0x176 sits at index 7 of the 13-entry window table at 0x54FCBC (FUN_0042ad10 writes 0x54FCD8 = 0x176). [V] The key-to-index binding is unknown. [I]
- **Row drawing:** FUN_0047b3e0 adds each node pointer with FUN_0049e830(0x176, node, 0). The renderer that draws a row was not traced. [I]
  - Friend nodes carry a status at +0x1C. The same offset is uninitialised in blacklist nodes. If the renderer reads it, blacklist rows could show a random status icon. See live test T-B1.

### A.3 Wire grammars

All five opcodes are on the game socket. The C2S side goes through `[M+0x130]`.

#### C2S 0x93 BlacklistAdd (17 B) [V]

| Off | Type | Field | Source | VA |
|---|---|---|---|---|
| 0 | str[17] | name | dialog 0x4C9 ctrl 5 edit text | Add(char*,0x11) 0x47B0C3 |

- Opcode `Add(u8 0x93)` at 0x47B0B4; `Send(1)` at 0x47B0D1.
- **Client pre-checks** in FUN_0047afb0, in this order. Each failure pops message box 0x16 with `FUN_0049ebc0(ui,3,3,0,0)`:

| # | Condition | Text (VA of the MOV) |
|---|---|---|
| 1 | socket `[M+0x130]` or the name pointer is NULL | silent return |
| 2 | count `[M+0x40]` == 10 | "Can't be add.\r\n\r\nVisit Frenaiga." (0x47AFDF) |
| 3 | name == own name (M+0x138) | "You can't register yourself." (0x47B029) |
| 4 | FUN_00484190(name, id 0) != 0, i.e. already blacklisted, **or any node has char_id 0** | "The player is already registered as friend." (0x47B050). This is the wrong text, shown as is. |
| 5 | mentor list head `[M+0x18]` is non-NULL and its data is non-NULL | "You can't add mentor to the blacklist." (0x47B079). **This fires for ANY name while the player has a mentor.** The check never compares the name. |
| 6 | FUN_0047fd80 finds the name in the friend list | "You can't add your friend to the blacklist." (0x47B0A0) |

- Only the dialog is modal, and it closes when C2S 0x93 is sent. Nothing waits for 0xBE.

#### C2S 0x94 BlacklistRemove (21 B) [V]

| Off | Type | Field | Source | VA |
|---|---|---|---|---|
| 0 | u32 | char_id | `[window 0x4CA +0x134]` = node+0x18 | 0x4814C5 |
| 4 | str[17] | name | window 0x4CA ctrl 4 caption (+0x50) | 0x4814D4 |

- `Add(u8 0x94)` at 0x4814B2; `Send(1)` at 0x4814E2.
- The only client check is a non-empty name. The protocol_spec text "control 3" means list index 3, which is ctrl 4.

#### S2C 0xBD BlacklistList (1 + 21·n B) [V]

```
u8 count                  0x47CE2D
repeat(count) {
  u32 char_id             0x47CE61 -> node+0x18   (id FIRST: the reverse of the friend-row order)
  str[17] name            0x47CE79 -> node+0x00
}
```

1. FUN_0047a850 frees the whole list.
2. Each record is appended with FUN_0045f3f0(M+0x34) (0x47CE84).
3. FUN_0047b3e0 redraws window 0x176 (0x47CE9F).

- No popup. No cap is enforced on receipt, but keep count ≤ 10.

#### S2C 0xBE BlacklistAddResult (1 or 22 B) [V]

```
u8 result                 0x47CED1
if (result == 1) { u32 char_id (0x47CEFD -> node+0x18); str[17] name (0x47CF15 -> node+0x00) }
```

- result 1: append one node (0x47CF20), redraw. No text.
- Any other value: nothing more is read. Popup **"Wrong user name.\r\nPlease, check again."** (0x47CF48, string 0x529458).
- There is no duplicate check on receipt.

#### S2C 0xBF BlacklistRemoveResult (1 or 18 B) [V]

```
u8 result                 0x47CF63
if (result == 1) { str[17] name (0x47CFB1, into a zeroed stack buffer) }
```

- result 1: FUN_0047fe60 unlinks and frees **every** node whose name equals the given name exactly (strcmp). It then redraws, and the handler redraws a second time.
- Any other value: "Wrong user name.\r\nPlease, check again." (0x47CFF5).
- The removal is by name only. An unknown name just redraws.

### A.4 The lookup FUN_00484190 and the id-0 hazard [V]

`FUN_00484190(EAX=M, EDI=name, stack=id)` returns 0 when the name pointer is NULL or the count `[M+0x40]` is 0. Otherwise it walks M+0x38 and returns 1 on the first node where **`node+0x18 == id` OR `strcmp(node, name) == 0`**. The id is compared first.

**Hazard (must-rule): never send char_id 0 in 0xBD/0xBE.**

- Most callers pass id 0 (see A.5). A node with char_id 0 therefore matches every call.
- Effect on the client: every incoming whisper, map-chat line, trade request and party invite is dropped, from **everyone**.
- Every outgoing whisper, trade, party invite, chat invite and friend/mentor add is refused with "Blacklisted user can't use this." / "Blacklist user can't be registered."
- Blacklist add then always fails with "The player is already registered as friend."

**Id space.**

- Some callers pass a real id:
  - 0x0D passes request_id;
  - 0x10 passes inviter_id;
  - the party-invite block passes the popup target uid.
- In our server all of these are **account uids** (social_friend.md 3.1).
- So the char_id in the blacklist must be the target's account uid, or an id from a range that can never equal an account uid. [I]
- With account uids, the id-matched paths (friend request, chat invite, party invite) also block the target's **sibling characters**. The name-matched paths (whisper, chat, trade) do not. [I] See open question E-A3.

### A.5 Effect on whispers, chat and requests [V]

FUN_00484190 has exactly **14 call sites** in the exe. A grep for `CALL 0x00484190` over all asm gives 1 in FUN_004462e0, 3 in FUN_00448730, 3 in FUN_00451960, 1 in FUN_004774c0, 1 each in FUN_0047ad60 / FUN_0047aef0 / FUN_0047afb0 / FUN_0047b130, and 2 in FUN_0047bd60. Each one is below.

**Incoming. The client drops the packet silently when the sender is on the local blacklist.** Nothing is shown and nothing is sent back.

| S2C | Handler | Check (VA) | Effect when blacklisted |
|---|---|---|---|
| 0x0A WhisperReceived | 0x456024 | (sender_name, id 0) at 0x456070 | Nothing printed; the sender is not added to the whisper dropdown 0x6E; the message bytes are not read |
| 0x16 ChatMessage (map chat) | 0x456800 | (sender_name, 0) at 0x456889 | No chat-log line, no bubble, no emote |
| 0x45 TradeRequestReceived | sub1 0x4784FB | (name, 0) at 0x478524 | Window 0x70 (Accept/Refuse) not opened |
| 0x4E PartyInviteReceived | 0x45BCDE | (name, 0) at 0x45BD0E | Window 0x71 not opened |
| 0x0D FriendRequestPrompt | sub3 0x47C12C | (requester_name, request_id) at 0x47C180 | Window 0x17C not opened |
| 0x10 MessengerChatInvite | sub3 0x47C56E | (inviter_name, inviter_id) at 0x47C5CD | Window 0x17D not opened (it also needs `[M+0x180]==0`) |

**Outgoing. The client refuses its own action against a blacklisted name.**

| Action | Site | Text |
|---|---|---|
| Typed whisper `/w name msg` (C2S 0x02) | FUN_004462e0 0x446CF4 | "Blacklisted user can't use this." (chat log, colour 0xFFFD4C87) |
| Whisper-target dialog 0x6F OK (sets the whisper partner gs+0x4C8) | FUN_00448730 0x44B37C | same |
| Popup 0x50 Trade (C2S 0x20) | 0x44C9D2 | same (jumps to 0x44B385) |
| Popup 0x50 Make Party (C2S 0x27; the uid is passed as id) | 0x44CB70 | same |
| Converse / chat invite (C2S 0x33) from 0x176 ctrl 7 or popup 0x50 ctrl 8 | FUN_0047b130 0x47B169 | same |
| Add friend (C2S 0x30) | FUN_0047ad60 0x47AE32 | "Blacklist user can't be registered." (0x47AE4A, box 0x16) |
| Register mentor (C2S 0x5C) | FUN_0047aef0 0x47AF56 | same (0x47AF6E) |

**Not filtered by the client.** These handlers do not call FUN_00484190:

- the messenger room line 0x61;
- friend chat `/f` (S2C 0x91);
- party chat;
- guild traffic (0xB3 and others);
- shouts and notices (0x90/0x91);
- memos and notes 0x78;
- presence 0x60;
- stall and shop windows.

It is also unconfirmed whether whisper mode entered from popup 0x50 ctrl 6 ("Whisper") is checked. It is not one of the 14 sites, so it is probably unchecked until the typed-line path. [I]

### A.6 Must-reply rules and soft-locks

| Case | Rule | Tag |
|---|---|---|
| C2S 0x93 / 0x94 unanswered | **No soft-lock.** The dialog is already closed and there is no wait box. The list just does not change. Always answer anyway, so the UI matches the server. | [V] |
| 0xBE/0xBF result ≠ 1 | Pops "Wrong user name..." in box 0x16 (OK button). No state change. | [V] |
| 0xBD not sent after 0x03 | The list stays empty after every map change. All client-side filtering stops until the next 0xBD. **Must send after every 0x2F.** | [V] |
| Request to a player who blacklisted the requester (0x45/0x4E/0x0D/0x10 dropped client-side) | The requester's client only printed a local line ("You requested <X> to make an transaction." / "...to join the party." / "Requesting ... to be a friend" / "Inviting ..."). None of these is modal. **But the server's pending-request tables fill up.** Example: trade.py keeps ONE pending 0x45 per invitee and answers other requesters "busy" (0x47 6) for INVITE_TTL = 30 s. See A.7 step 5. | [V] client; [V] server file read |
| Whisper to a player who blacklisted the sender | The server sends 0x09 (delivered) to the sender and 0x0A to the target. The target drops it, so the sender believes it was delivered. | [V] |

### A.7 Server behaviour (proposed flows) [I unless marked]

Notation follows social_friend.md: `A` is the acting session, `name17(x)` is truncated to 16 bytes and NUL-padded. Store the list per **character** (`char['blacklist']`), because the client list is per character: it is reset by 0x03 and re-sent after 0x2F.

**F-B1. Resync (C2S 0x2F).** Messenger.friend_list already answers 0x2F with 0x0B [+0x7E] [+0x78]. Append:

- **S2C 0xBD** `{count=n, [{char_id=uid_of(name), name=name17(name)}...]}` with n ≤ 10.
- Drop entries whose character no longer exists.
- Sending count 0 is harmless: it frees the already-empty list and redraws. [V]

**F-B2. Add (C2S 0x93 → S2C 0xBE).** Checks in order. Every failure sends `{result=0}` (1 byte), which the client shows as "Wrong user name.\r\nPlease, check again.":

1. The name (cut at the first NUL, ≤ 16 bytes) is not a known character.
2. It is A's own character.
3. It is already in A's list (case-sensitive; the client compares with strcmp).
4. The list already has 10 entries.
5. It is in A's friend list, or it is A's mentor or mentee. The client blocks friends, and blocks everything while A has a mentor.

Success:

- Append `{name, uid}` and persist.
- Reply **S2C 0xBE** `{result=1, char_id=uid (never 0), name=name17}`.
- Send success exactly once, because the client appends without a duplicate check. [V]
- No message goes to the blacklisted player (retail unknown).

**F-B3. Remove (C2S 0x94 → S2C 0xBF).**

- Key the removal on the **name** and ignore `char_id` (the client removes by name). [V]
- Remove it if present and persist.
- Reply `{result=1, name=name17(name)}` **even if the name was not in the list**. Removal is idempotent and resyncs a stale client row.
- Reply `{result=0}` only for a malformed or empty name.

**F-B4. Server-side mirror of the filter.** Add a helper `blocks(receiver, sender)`: true when the sender's character name is in the receiver's `char['blacklist']`. Plug it into the existing refusal hook `GameServer.refuses(target, kind, actor)` (windslayer_server.py ~4749, the privacy.py consumer API). Config key `BLACKLIST_FILTER`:

| Mode | Behaviour | Why |
|---|---|---|
| `client` | Do nothing server-side (today). The client drops the packets. | Retail-faithful for the visible result; leaves the pending-state problem open |
| **`silent` (proposed default)** | Do not deliver the packet and **do not create pending state**. The requester gets the reply it would get if the receiver had dropped the packet: whisper → still 0x09 "delivered" (0x65/channel form); chat, trade, party, friend, chat-invite → no reply | Same result as the client filter for both players, without phantom pending requests |
| `refuse` | Answer with the existing refusal codes: whisper 0x09 0x67 "is rejecting whispers."; trade 0x47 4; party refusal (party.py code); friend 0x0C 4 "has turned friend function off."; chat 0x0F 4 "is rejecting chatting."; mentor 0x7A 0x66 "can not be found." | Tells the blocked player they are refused |

Apply it at these points:

- C2S 0x02 whisper;
- the 0x16 fan-out: skip receivers who block the speaker, which saves bandwidth with the same visible result;
- C2S 0x20 trade, 0x27 party, 0x30 friend, 0x33 chat invite, 0x5C mentor;
- C2S 0x6B `/f`: drop recipients who block the sender, because the client does NOT filter 0x91.

**F-B5. Reverse direction.** If A blacklisted B, forged A→B actions (0x02/0x20/0x27/0x30/0x33/0x5C) should be dropped server-side the same way. The client already blocks them.

### A.8 Persistence and constants [I]

```jsonc
"characters": [{
  "name": "TestHero",
  "blacklist": [{"name": "Bob", "uid": 2}],   // NEW, ordered, <= 10, names <= 16 bytes, uid != 0
  "...": "..."
}]
```

- `BLACKLIST_MAX` = 10. [V] from the client "(n/10)" label and the add gate.
- Name renames and deletions: drop them lazily on F-B1.
- The uid is re-resolved from the name index on every F-B1, so a character moving to a new account cannot leave a stale id.
- Names are case-sensitive. Character creation must keep names unique and ≤ 16 bytes (social_friend.md 3.5).

### A.9 Current server status [V: files read, not edited]

| Item | Status |
|---|---|
| C2S 0x93/0x94 | Consumed and logged "no blacklist model (social_friend)" (windslayer_server.py ~563). No reply. Harmless, since there is no wait box. |
| S2C 0xBD/0xBE/0xBF | Never sent. Grammars exist in protocol_spec_2009.json. |
| 0x0A builder | Sends `sender_on_local_blacklist: False` (packets.py ~687). The client-side gate still works if the client has a list. |
| trade.py / party.py | Their docstrings note the client gate "needs nothing from the server". The pending-invite interaction (A.6) is not handled. |

---

## Part B. Channels

### B.1 The version-server channel list (S2C 0x01) [V]

Handler 0x4519F1, parser ParseChannelList FUN_00440a00. The full grammar is in protocol_spec_2009.json.

```
u16 version_code (must be 14)  u16 notice_len  str notice
u8 server_count (<= 4)
repeat(server_count) {
  u8 server_status                      -> gs+0x1338+i*0xA8 (u32)
  if (server_status != 1) {
    u8 channel_slot_count (<= 10)       -> gs+0x13DC+i*0xA8
    u8 channel_entry_count              -> gs+0x13DD+i*0xA8
    entries, strictly ascending channel_no in 1..slot_count:
      u8 channel_no, u16 user_count, u32 ip (host order), u32 port        (11 B each)
  }
}
```

Each slot j is a 16-byte record at `gs+0x133C + i*0xA8 + j*0x10`:

| Offset | Field |
|---|---|
| +0 | u32 state |
| +4 | channel_no |
| +6 | u16 users |
| +8 | u32 ip |
| +0xC | u32 port |

**World (server) status**, as drawn by FUN_00442080 for world buttons ctrl 0x17..0x1A of window 0x73:

| server_status | UI |
|---|---|
| < 1 or > 3 | hidden |
| 1, 2 | shown grey (0xFF808080). Clicking it gives "Server is under maintenance.\r\nPlease try another server." (0x44A784). With status 1 no channel data follows. |
| 3 | shown white, selectable |

**Channel slot state**, for channel button ctrl 1..10 and load label ctrl 13..22:

| Slot state | How it arises | Channel button | Load label |
|---|---|---|---|
| 3 | listed in the entries | "Channel - N" (`"%s - %d"`, 0x4421B3) | shown, see B.2 |
| 2 | slot ≤ slot_count but not listed | "Channel - N (inspection)" (`"%s - %d (%s)"` with "inspection", 0x4422A4) | hidden; the slot is not selectable |
| other | beyond slot_count | hidden | hidden |

Consequence: **per-channel maintenance is free.** Keep `slot_count` and omit the channel's entry. [V]

**Window 0x73** (UI 1123 "Server Selection"):

| Ctrl | Content |
|---|---|
| 1..10 | Channel buttons (UI 1124-1133 placeholders "Channel-1".."Channel-10", overwritten at runtime) |
| 11 | OK |
| 12 | Exit |
| 13..22 | Load labels (placeholders UI 1136-1145 "Busy1".."Busy0") |
| 23..26 | Worlds: UI 8931 "Cecilia", 1147 "Fiona", 1148 "Emilia", 1149 "Eryn" |

**Window 0x73 handler** (FUN_00448730 case 0x73):

- **World click:** the world becomes selected (gs+0x15D8 and gs+0x15DA). If the current channel slot gs+0x15D9 is not open, the first open slot is auto-selected.
- **Channel click:** accepted only when the slot state is 3.
- **OK:**
  - A slot that is not open gives "Select a available channel to enter." (0x44A8AE).
  - Otherwise it calls ConnectToGameServer.
  - If the connect fails, it shows "You failed to enter the channel.\r\nPlease try later." (0x44A890). The box is then **immediately overwritten** by "Waiting for the server to respond." (0x44B975).
- **Exit (ctrl 12):** fade, then `PostMessage(WM_CLOSE)` (0x44A8D8). **Exit quits the game, also on the in-game channel-change screen.**

### B.2 Load label mapping (users → label) [V]

In FUN_00442080, for each open slot (0x4421DE-0x442276):

```
idx = min(user_count / 200, 2)            ; magic 0x51EB851F >> 6 = /200, clamp at 0x442208
text  = table 0x54B214 + idx*7           ; "Idle\0\0\0" | "Normal\0" | "Busy\0\0\0"
colour = idx 0: 0xFF00FF00 (green) | 1: 0xFFFFFF00 (yellow) | 2: 0xFFFF0000 (red)
```

| user_count | Label | Colour |
|---|---|---|
| 0-199 | **Idle** | green |
| 200-399 | **Normal** | yellow |
| ≥ 400 | **Busy** | red |

The three strings were read from the exe copy at 0x54B214 / 0x54B21B / 0x54B222.

- The Dec 2009 retail video 2b70F9hk_Lc shows "a green 'Low' load label". That is a later build.
- **B14 prints "Idle", not "Low".** Do not expect "Low" with this exe.
- `user_count` is display-only. Nothing else in the client reads it. [V] (it is read only by FUN_00442080)

### B.3 World names and the minimap title "Cecilia(channel-N)" [V]

ConnectToGameServer FUN_00440c70 runs on OK, and also automatically on the change-avatar path:

1. Requires slot < slot_count (else it logs "Wrong server number %d(ConnectToGameServer)") and slot state == 3 (else "Failed to connect - no server in list"). Both failures return 0.
2. Closes and re-initialises g_sock_game, then `Sleep(1000)`.
3. `sprintf("%s(channel-%u)", worldname[gs+0x15D8], gs+0x15D9 + 1)` (0x440D46).
4. Writes the result into **window 4 ctrl 4** (the big minimap) and **window 0x17 ctrl 4** (UI 468 "Mini Map (Small)"). Both are FUN_00498120 calls, at 0x440D6D and 0x440D7B. The default caption is UI 71 "World Name(channel00)".
5. `Connect(ip, port)` from the slot. Returns 1 (0x440DC1) or 0. This answers the 0x01 spec's open question about the return value.

Consequences:

- The **title is computed by the client from its own selection, at connect time.** The server never sends it.
  - N is slot index + 1, which equals channel_no (the parser stores entry `channel_no` at slot `channel_no-1`).
  - The world name comes from a fixed exe table at 0x54A80C, 15-byte stride: "Cecilia", "Fiona", "Emilia", "Erin". The world *buttons* use the .lng texts, where index 3 is spelled "Eryn".
  - Our `WORLD_NAME` config cannot change the title without an exe patch.
- The title changes even if the TCP connect then fails.

### B.4 "In channel N." and 0x03 channel_id

- **S2C 0x99 sub 8** `{u8 sub_type=8, u8 channel_no}` (2 B). Read at 0x45D6ED (MOVZX byte). `sprintf "In channel %u."` at 0x45D6F8. Chat log colour 0xFFFDF34C (0x45D717). [V]
  - Purely cosmetic. The client does not compare it with anything. [V]
  - The server decides when to send it. Ours sends it on the first C2S 0x63 of each connection (windslayer_server.py `_send_channel_notice`). When retail sent it is unknown. [I]
- **S2C 0x03 `channel_id`** (u8, first field) → scene+0x21D (0x452A95/0x452A9C). [V]
  - It becomes the messenger's own channel M+0x14C at the reset. [V copy; I for the scene mapping]
  - It is compared with the 0x03 `mentor_channel`: equal → mentor state 0, 0x66 → offline, other → state 4 "(Mentor, Channel %d)". [V-2008, spec]
  - The 100 "same channel" sentinel in 0x7A/0x7B/0x7D/0x7E is replaced by M+0x14C (0x7A case: `*(M+0x14C)`). [V]
  - **Must equal the channel of the listener the client connected to.**

### B.5 Changing channel in-game (Build 14 has it) [V]

**Entry point.**

- HUD window 3 ctrl 4 "System Settings" (UI 53) toggles window **0x1AE** (0x44B526). It is also entry 9 of the window table (0x54FCE0).
- Window 0x1AE (UI 3055 "Option Selection Window") controls:

| Ctrl | Text |
|---|---|
| 1 | Keyboard Settings |
| 2 | Macro Settings |
| 3 | System Settings |
| 4 | Exit |
| **5** | **"Change Avatar"** (UI 9275) |
| **6** | **"Change Channel"** (UI 9276) |

- The KR 2012 video DRPIyKrUmvY shows the same menu (채널선택 / 캐릭터 선택).

**Gate.** Both paths call FUN_00445e30(1) first. When map mode `[[gs+0x4FC]+0x7C] == 1` (a room), it refuses:

| scene+0xF90 | Message(s) |
|---|---|
| 4 (play room) | "Not allowed in play room." **and then** "Not allowed in arena." (falls through, 0x445E58/0x445E70) |
| 5 (instance dungeon) | "You can't do that in the dungeon." (0x445E9B) |
| anything else | "Not allowed in arena." |

In the field there is no refusal.

**There is no C2S "change channel" packet.** The client just drops the game connection and logs in again elsewhere.

**Change Channel (ctrl 6)** (FUN_00448730 case 0x1AE, decomp l.672-689):

1. `FUN_0043d810` fades out.
2. **FUN_00440880 ConnectToVersionServer**:
   - clears the 4 world records (memset gs+0x1338, 0x2A0);
   - resets window 0x73;
   - connects to the **hard-coded `"207.211.84.46":7011`** (string 0x52DD54, port imm 0x4409C3). `patch_2009.py` redirects it.
   - Its return value is ignored here.
3. `CSNSocket::Close(g_sock_game)`. The server just sees the socket close.
4. `FUN_004980d0`; `KillTimer(6)` (the C2S 0x05 keep-alive timer); unload the map (`FUN_00407640`); load `./hs/main99_01.hmi`; destroy entities (`FUN_00424130`); `FUN_00428750`.
5. `FUN_0042dbf0` zeroes gs+0x404/+0x408. Then **gs+0x408 = 1** (0x44D24E). Window 0x73 is shown.
6. S2C 0x01 arrives on the version socket. ParseChannelList:
   - with gs+0x408 set, sets every world except gs+0x15D8 to status 0, i.e. hidden (0x440C12);
   - re-selects the old world (gs+0x15DA → gs+0x15D8);
   - the old channel slot gs+0x15D9 stays selected.
   - The current channel is **not** disabled: picking it just reconnects to the same channel.
7. The user picks a channel and presses OK. ConnectToGameServer runs: title "Cecilia(channel-N)", `Sleep(1000)`, TCP connect. Then "Waiting for the server to respond."
8. The server sends 0x5A (key exchange). The client auto-sends **C2S 0x01** `{str[131] sso_account (the same launcher args), u32 session_key = scene+0x154}` (0x451CE5). [V, spec]
9. **S2C 0x02 with result 1 while gs+0x408 is set** (0x452028):
   - windows 0x16/0x73 are hidden;
   - FUN_00441c60 loads the character's registry settings;
   - gs+0x400 = 0;
   - **the client immediately sends C2S 0x2B**: u8 flags from gs+0x524/+0x528/+0x52C/+0x534/+0x530, str[16] scene+0x228, u32 scene+0x248, **str[17] scene+0x20C = the current character**, u8 scene+0x152 (0x45206A-0x452133);
   - `SetTimer(2, 5500 ms)`.
   - **Nothing else of 0x02 is read.** So the account uid (scene+0x224), the session key (scene+0x154) and the character list are **not refreshed**.
10. The server sends S2C 0x03 (which kills timer 2) and 0x07, and the player is in the world on the new channel.

- Timer 2 fires before a 0x03 arrives: "No response from the server." (0x43EBA7), then `FUN_00448730(0x12,4,...)`.

**Change Avatar (ctrl 5):**

- Same teardown, plus `SetTimer(0xC, 10000)` before the version connect.
- If the version connect fails: main99_01 is loaded and window 0x73 is shown.
- Sets **gs+0x404 = 1** (0x44D33C).
- On S2C 0x01, the handler kills timer 0xC (0x4519FC). With gs+0x404 set it **auto-connects to the same world/channel** (0x451B69) with no OK click.
  - On failure: main99_01, window 0x73, "You failed to enter the channel.\r\nPlease try later." (0x451BC0).
  - Then, in all cases: "Waiting for the server to respond." (0x451BD9).
- Then comes 0x01 login and 0x02 success on the normal path (gs+0x408 == 0): the client loads main99_02 and shows the **character select** on the same channel.
- Timer 0xC fires if the version server does not answer within 10 s: main99_01, window 0x73, "You failed to enter the channel.\r\nPlease try later." (FUN_0043e6f0 case 0xC, 0x43ED7D).
- If S2C 0x02 fails while gs+0x404 is set: close, then reload main99_01 (0x451D1D).

**Flag lifetime.**

- gs+0x404/0x408 are cleared only by FUN_0042dbf0 (0x42DCB7/0x42DCBD), and 0x404 also at 0x44A480. [V]
- So gs+0x408 **stays set after a successful channel change** until the next Change Avatar/Channel. [V]
- The consequence is [I]: a later successful 0x02 in the same client run would also take the auto-0x2B path and skip character select. One example is a reconnect after a server kick, if that path does not call FUN_0042dbf0. Open question E-B5.

**What the server must guarantee on this path:**

| # | Rule | Tag |
|---|---|---|
| R1 | Every channel is a distinct (ip, port) in 0x01. **C2S 0x01 carries no channel number** (grammar `str[131] + u32`), so the listening socket is the only way to know the channel. | [V] |
| R2 | The old connection closes with no goodbye packet. Treat it as a normal disconnect: save, despawn (0x06 to map peers), friend presence, mentor, party/trade/room/stall cleanup. | [V] no C2S; [I] cleanup |
| R3 | The relogin C2S 0x01 carries the **same session key** as the first login, because scene+0x154 is not refreshed on the 0x408 path. Accept it even if the old socket has not been reaped yet. World.claim(replace=True) already does this for one process; it must see all channels. Otherwise the client gets 0x02 result 4 "Connection already exists...". | [V] client; [V] server code read |
| R4 | The account uid must stay the same across channels. The client keeps scene+0x224 from the first 0x02 and registers the local player only if the 0x07 uid equals it. | [V] (nothing re-read) + project_local_player_spawn |
| R5 | After 0x02 success, expect C2S 0x2B **immediately** (the 0x02 tail is ignored). Answer with 0x03 within **5.5 s** (timer 2). | [V] |
| R6 | 0x03 `channel_id`, 0x99 sub 8, whisper/friend channel numbers and the minimap slot must all agree. Send `channel_id = channel_no` of the listener. | [V] fields; [I] policy |
| R7 | The version server must be reachable on the in-game path. The client reconnects to 7011 at the patched address. If it is not reachable, the Change Channel screen stays empty and only Exit (quit) works. | [V] |
| R8 | A channel whose game port refuses the connection leaves the player on "Waiting for the server to respond." with no dismiss. That box is shown with (3,0,3,0) instead of the OK form (3,3,0,0), and the button semantics of FUN_0049ebc0 were not traced. **Only advertise channels whose listener is up.** | [V] sequence; [I] soft-lock |

### B.6 Cross-channel semantics the client already implements

| Feature | Client evidence | Server consequence |
|---|---|---|
| Whisper | `"<To: %s[%s-%u]> %s"` (0x52E0F0) and `"<From: %s[%s-%u]> %s"` (0x52E114) with "Channel" (pushed at 0x455FE4 in 0x09 and 0x45618E in 0x0A). Status/channel 0x65 = no tag. [V] | Whisper routing is **global**. 0x09 status = the target's channel number when it differs, else 0x65. 0x0A channel = the sender's channel when it differs, else 0x65. Never use 0x66/0x67 as a channel number. [I policy] |
| Friend presence | 0x0B/0x0C rows carry `channel` + `status` (0 same channel, 1 offline, 4 other channel); 0x60 presence 4 prints "<%s> has logged in.(Friend, Channel %d)" (0x52C0C8). [V] | Presence is **global**, and the server computes 0 vs 4 per watcher. [I] |
| Mentor/mentee | 0x03 `mentor_channel` vs `channel_id`; 100 = same channel in 0x7A/0x7B/0x7D/0x7E; "(Mentor, Channel %d)" (0x52C0A0), "(Menti, Channel %d)" (0x52C0F0). [V] | Global; the server sends 100 or the real channel per receiver. |
| Friend chat `/f` | C2S 0x6B lists `(u8 friend_channel, u32 friend_id)` per recipient (status 0 or 4, channel != 0). [V spec] | Relay by friend_id across channels. |
| Messenger chat room | 0x0F subtype 2 = "is in other channel. You can't have a personal chat." (text found at ~0x52B27C). [V string; V-2008 code] | Rooms are **channel-local**. A cross-channel 0x33 invite is answered with 0x0F `{2, name}`. |
| Trade | "Both players must be in same field to trade items." (client, FUN_00448730). The request needs popup 0x50 on a visible entity. [V] | Per channel by construction. |
| Party invite | Needs popup 0x50 on a visible entity (same map). The party frames 0x4F/0x51/0x54/0x55 carry **no channel or map**. [V spec] | Party formation is per channel. Whether a party survives a member's channel change: retail unknown (E-B2). |
| Arena/room creation | "The number of the arena channels are excessive..." (S2C 0x30) is the room-limit text, **not** game channels. [V-2008] | Rooms/arena/battlefield/instance lists per channel. [I] |

### B.7 Isolation matrix (what the server keeps per channel vs global) [I, derived from B.6]

| Per channel | Global (all channels) |
|---|---|
| Map instances: player visibility 0x04/0x05/0x06, movement relay, monsters and spawns, drops, AI/ticks, map timers | Accounts, characters, inventory, bank, persistence store |
| Trades, stalls (field shops), party formation (and the party itself, until E-B2 is answered) | Identity: by_uid / by_char_name. **One online session per account across all channels.** Relogin key handoff |
| Messenger chat rooms (0x0F subtype 2 across channels) | Whisper routing, friend lists and presence, mentor/mentee, `/f` friend chat, memos/notes |
| Arena, battlefield, play rooms, instance-dungeon rooms and their lists; UDP room ports | Guild membership, guild chat and notices (defer to the guild spec) |
| Map chat 0x16 (map-scoped already) | GM tools, `/stop`, maintenance, server announcements |
| User count shown in 0x01 | Blacklists (Part A) |

### B.8 Recommendation: one process, many ports

Run **one server process** that binds one game listener per channel (2009: same IP, a different port per channel). The session's channel comes from the listener that accepted it. Keep one shared store and identity/social layer, and give each channel its own world state (map instances, monsters, trades, stalls, rooms, parties).

Reasons:

1. **The channel is only knowable from the socket (R1).** Both designs can do that. But the channel change is a disconnect plus a relogin within ~2 s that must hand over the account (R3). With separate processes, the old process's session registry and the new process's login would need IPC and a distributed lock.
2. **Most social features are cross-channel by design (B.6).** Whisper, presence, mentor, `/f`, guild and blacklist are all global. In one process they are dict lookups (`World.by_uid`, `by_char_name`, messenger watchers). Across processes each becomes a message bus.
3. **Persistence is a single JSON store.** accounts.json has a debounced writer and a store lock. Several processes writing it would overwrite each other. Splitting would first require a real DB.
4. **Scale does not need it.** A private server with tens of players fits in one Python process. The only gain from separate processes is crash isolation, which does not justify points 1-3.
5. **The version server** (same process) can report live per-channel user counts (B.2) and per-channel inspection status (B.1) with no IPC.

Code shape [I]:

- `config.CHANNELS` entries get a `"port"`.
- `GameServer.start()` opens one listening socket per channel and tags each accepted session `session['channel']`. `_channel_no` already reads that key.
- Map instances key on `(channel, map_code)`: `World.maps`, or one `World` per channel behind a global identity index.
- The 2008 build (hard-coded port 7022) can still get channels, using one IP per channel (loopback aliases 127.0.0.x) in the same process. This is the existing config.py warning.

### B.9 Current server status [V: files read]

| Item | Status |
|---|---|
| VersionServer 0x01 | One world, status 3. Every configured channel is listed with `GAME_PORT` (2009) and the PUBLIC_IP or a per-channel ip. `user_count` = all online accounts, per channel (`channel_user_counts`, "per-channel split lands with the channel model"). |
| Channel identity | `_channel_no(session)` = `session['channel']` or the first CHANNELS entry. Nothing sets `session['channel']` yet. |
| 0x03 channel_id / 0x99 sub 8 | Both come from `_channel_no` (windslayer_server.py ~9245 / ~1649). |
| Relogin | `World.claim(replace=True)` for a 0x01 carrying the live session key. The 0x02 builder knows the 0x408 path (packets.py ~657). |
| Isolation | None. One World for everything. |

---

## C. Implementation plan (each stage testable)

Effort: S < 1 day, M 1-3 days, L > 3 days. Blacklist and channel stages are independent, except BL-3 × CH-3 (whisper channel tags).

| Stage | Content | Eff | Depends | Test gate |
|---|---|---|---|---|
| **BL-1 list sync** | `char['blacklist']` (≤ 10, uid ≠ 0, migration default `[]`); S2C 0xBD built from the spec grammar after 0x0B in `Messenger.friend_list` (C2S 0x2F). Unit tests: encode of 0/1/10 entries; never an id 0; names NUL-terminated | S | none | Hand-edit accounts.json → log in → tab shows names "(n/10)"; survives a portal (T-B1, T-B10) |
| **BL-2 add/remove** | C2S 0x93 → rules F-B2 → 0xBE; C2S 0x94 → F-B3 → 0xBF; persist; unit tests per failure rule | S | BL-1 | T-B2, T-B11, T-B12 |
| **BL-3 server mirror** | `blocks()` + `BLACKLIST_FILTER` (`client`/`silent`/`refuse`) wired into `refuses()` and into whisper, 0x16 fan-out, trade, party, friend, chat invite, mentor, `/f`; no pending state for blocked requests | M | BL-2 | T-B3..T-B9 with both modes; trade.py "busy" no longer triggered by a blocked requester |
| **CH-1 channel identity** | CHANNELS `port` (validated unique (ip,port)); one listener per channel in one process; `session['channel']`; 0x01 lists each channel's port and **its own** user count; per-channel `open` flag (closed → omitted → "(inspection)"); 0x03 `channel_id` and 0x99 sub 8 from the listener. **No isolation yet**: a dev-only mode that proves the client flows | M | none | T-C1..T-C5 (minimap, notice, change channel/avatar) |
| **CH-2 world isolation** | Map instances keyed by channel: visibility, movement relay, monsters/spawns/ticks, drops; trade/stall/party formation filtered by channel; room/arena/battlefield/instance lists per channel; UDP room port base per channel | L | CH-1 | T-C6 (same map, different channels, no mutual visibility, separate mob HP) |
| **CH-3 cross-channel social** | Whisper tags (0x09 status/0x0A channel), friend rows/0x60 status 0 vs 4 with channel, mentor 100 vs channel, 0x0F subtype 2 for cross-channel chat invites, `/f` global relay; guild routing handed to the guild spec | M | CH-1 (CH-2 for meaning) | T-C7..T-C9 |
| **CH-4 channel-change robustness** | Relogin across listeners (R3-R5); disconnect cleanup order (save before accepting the relogin's 0x2B, so position/HP carry over); duplicate-login race test; decide the party policy (E-B2); gs+0x408 stale-flag handling (E-B5) | M | CH-1 | T-C4 repeated 10× quickly, T-C10, T-C11 |
| **CH-5 operations** | Per-channel capacity (0x02 result 6 "The server is full.\r\nPlease try another server." when a channel is full); `LOAD_SCALE` knob (displayed users = online × scale, so small servers can show Normal/Busy); admin `{"channel_open": [n, 0/1]}` | S | CH-1 | T-C1 with scaled counts; admin toggle → "(inspection)" on the next version fetch |

---

## D. Live test plan (two 2009 clients)

Setup, following social_friend.md 6:

- C1 = `WindSlayer_patched.exe` as `test`/TestHero (uid 1).
- C2 = `WindSlayer_p2.exe` as `admin`, with a character named "Bob" (uid 2).
- Inject with `python wsdev.py --build 2009 send <op> <hex> --to c:1`.
- Capture with `wsdev cap`.
- Fake names Bob (2), Carol (3), Dave (4).

All hex below is payload after the opcode byte.

### D.1 Injection (works on today's server, single client)

| # | Packet | Command (payload hex) | Expected | Risk |
|---|---|---|---|---|
| I-1 | 0xBD 2 entries | `send BD 02 02 00 00 00 42 6F 62 00 00 00 00 00 00 00 00 00 00 00 00 00 00 03 00 00 00 43 61 72 6F 6C 00 00 00 00 00 00 00 00 00 00 00 00` | Blacklist tab: Bob, Carol, "(2/10)"; friend-tab buttons hidden, Add/Delete shown. **Note which row icon shows** (A.2 open item) | safe |
| I-2 | 0x0A from Bob | `send 0A 02 42 6F 62 00 00 00 00 00 00 00 00 00 00 00 00 00 00 02 68 69` | Nothing in the chat log (blocked). Without I-1: "<From: Bob[Channel-2]> hi" | safe |
| I-3 | 0x16 from Bob | `send 16 42 6F 62 00 00 00 00 00 00 00 00 00 00 00 00 00 00 05 68 65 6C 6C 6F` | No line, no bubble | safe |
| I-4 | 0x45 / 0x4E from Bob | `send 45 42 6F 62 00 00 00 00 00 00 00 00 00 00 00 00 00 00`, the same with `4E` | No window 0x70 / 0x71 | safe |
| I-5 | 0x0D / 0x10 from Bob | `send 0D 02 00 00 00 42 6F 62 00 00 00 00 00 00 00 00 00 00 00 00 00 00`, the same with `10` | No window 0x17C / 0x17D | safe |
| I-6 | 0xBE ok / fail | `send BE 01 04 00 00 00 44 61 76 65 00 00 00 00 00 00 00 00 00 00 00 00 00`; `send BE 00` | Dave appended "(3/10)"; then popup "Wrong user name.\r\nPlease, check again." | safe |
| I-7 | 0xBF ok / fail | `send BF 01 42 6F 62 00 00 00 00 00 00 00 00 00 00 00 00 00 00`; `send BF 00` | Bob gone; popup | safe |
| I-8 | Outgoing blocks (after I-1) | Type `/w Carol hi`; set the whisper target to Carol via dialog 0x6F; Add friend "Carol" in 0x176 | "Blacklisted user can't use this."; "Blacklist user can't be registered."; **no** C2S 0x02/0x30 in `cap` | safe |
| I-9 | Add-dialog pre-checks | Blacklist tab → Add → own name; a friend's name; then `send BD` with 10 entries and press Add | "You can't register yourself."; "You can't add your friend to the blacklist."; "Maximum character level" (sic) | safe |
| I-10 | id-0 hazard (diagnostic) | `send BD 01 00 00 00 00 5A 7A 7A 00 00 00 00 00 00 00 00 00 00 00 00 00 00`, then `send 0A 65 43 61 72 6F 6C 00 00 00 00 00 00 00 00 00 00 00 00 02 68 69` | Carol's whisper is **dropped** although Carol is not listed (proves A.4). Clear it with `send BD 00` | safe (client-local) |
| I-11 | 0x99 sub 8 | `send 99 08 03` | "In channel 3." (chat log) | safe |
| I-12 | Captures | Blacklist tab → Add "Bob" → OK; select Bob → Delete → OK | `0x93` 17 B `42 6F 62 00...`; `0x94` 21 B `02 00 00 00 42 6F 62 00...` | safe |

### D.2 Two-client blacklist (after BL-1..BL-3)

| # | Flow | Expected |
|---|---|---|
| T-B1 | C1 opens 0x176 → Blacklist tab | "(0/10)", Add/Delete only |
| T-B2 | C1 adds "Bob" | "(1/10)", row Bob; accounts.json TestHero.blacklist = [{Bob, 2}] |
| T-B3 | C2 whispers C1 | C1 sees nothing. C2 sees "<To: TestHero> msg" (`silent`) or "TestHero is rejecting whispers." (`refuse`) |
| T-B4 | C2 map-chats next to C1 | C1 shows no line or bubble. A third client or the log shows the line |
| T-B5 | C2 right-clicks C1 → Trade, then a third player trades C1 within 30 s | C1 gets no window 0x70. The third player's request **is** delivered (no phantom "busy" 0x47 6) |
| T-B6 | C2 → Make Party / Add as Friend / Converse on C1 | No window on C1; no error on C2 in `silent` mode |
| T-B7 | C1 → Trade / Party / Whisper / Converse on C2 | "Blacklisted user can't use this." client-side; server logs nothing |
| T-B8 | C2 `/f` while C1 is in C2's friend list (directed) | C1 does not get the 0x91 line (server mirror; the client would show it) |
| T-B9 | C1 has a mentor, adds any name | "You can't add mentor to the blacklist." (client quirk, no packet) |
| T-B10 | C1 walks a portal; relogs | List still "(1/10)" (0xBD after each 0x2F) |
| T-B11 | C1 adds "Nobody"; adds "Bob" twice via a forged 0x93 | "Wrong user name.\r\nPlease, check again." for both; one row only |
| T-B12 | C1 deletes Bob; then C2 whispers | Row gone, "(0/10)"; the whisper arrives |

### D.3 Two-client channels (after CH-1..CH-4)

Config: CHANNELS 1..4 on 127.0.0.1 ports 7022..7025. Channel 4 is closed (listed in slot_count, omitted from the entries).

| # | Flow | Expected |
|---|---|---|
| T-C1 | Launch C1 | Window 0x73: "Channel - 1/2/3" with Idle (green); "Channel - 4 (inspection)" with no label. With `LOAD_SCALE` / a users override of 250 and 450: Normal (yellow), Busy (red). Clicking channel 4 does nothing; OK on it gives "Select a available channel to enter." |
| T-C2 | C1 enters channel 1 | Minimap and small minimap "Cecilia(channel-1)"; chat "In channel 1." |
| T-C3 | C2 enters channel 2 | "Cecilia(channel-2)", "In channel 2."; 0x01 on the next launch shows 1 user on each |
| T-C4 | C1: HUD System Settings → Change Channel → only Cecilia shown, channel 1 pre-selected → pick 2 → OK | No character select; C2S 0x01 then **0x2B within ~1 s of 0x02**; in the world on channel 2 at the saved position; "Cecilia(channel-2)", "In channel 2." The server log shows C1's channel-1 disconnect before or during the relogin, and **no 0x02 result 4** |
| T-C5 | C1: Change Avatar | Character select on the same channel with no OK click |
| T-C6 | C1 on channel 1 and C2 on channel 2, both on map 101 | They do not see each other; each has its own monsters (hit one on C1, full HP on C2); portals stay on the channel |
| T-C7 | C1 whispers C2 across channels | C1 "<To: Bob[Channel-2]> hi"; C2 "<From: TestHero[Channel-1]> hi"; same channel → no tag |
| T-C8 | Friends across channels: C2 logs in on channel 2 | C1 "<Bob> has logged in.(Friend, Channel 2)"; the row is online |
| T-C9 | C1 Converse invite to C2 on another channel | C1 popup "<Bob> is in other channel. You can't have a personal chat." |
| T-C10 | C1 Change Channel inside an arena / play room / instance dungeon | "Not allowed in arena." / play room (two lines) / "You can't do that in the dungeon."; stays connected |
| T-C11 | Stop the channel 3 listener while it is advertised; C1 picks 3 | "You failed to enter the channel..." then the stuck "Waiting..." box. Documents R8. Then never advertise a dead port |
| T-C12 | C1 in a party with C2 (same channel), then C1 changes channel | Record what the chosen party policy (E-B2) shows on both clients |

---

## E. Open questions

**Blacklist**

1. **E-A1:** How is a blacklist row drawn? Does the 0x176 renderer read node+0x1C (uninitialised in blacklist nodes) and show a status icon (I-1)?
2. **E-A2:** Did retail filter server-side, and did the blocked player see "is rejecting ..."? Only `BLACKLIST_FILTER` covers this. Retail footage shows the Blacklist tab but no blocked interaction.
3. **E-A3:** Which id should 0xBD carry: account uid (blocks the target's sibling characters on the id-matched paths 0x0D/0x10/party) or a per-character id from a disjoint range (name-only matching)? Retail ids were probably per character.
4. **E-A4:** Per character or per account list? Per character is proposed, because the list is re-sent per character after 0x03.
5. **E-A5:** Should blacklisting also remove the target from a shared messenger room, party or trade in progress? The client does nothing.
6. **E-A6:** Which key or HUD element opens 0x176 in B14? It is window-table index 7. The binding was not traced. This carries over from social_friend.md Q8.
7. **E-A7:** Does the guild window's Blacklist tab (0x4B3 ctrl 59) select the blacklist tab of 0x176 (FUN_0047b950 not read)?
8. **E-A8:** Popup 0x50 "Whisper" (ctrl 6) sets whisper mode without one of the 14 checks. Is the plain-line send in whisper mode checked anywhere? Not traced.

**Channels**

1. **E-B1:** Retail channel count and load labels for B14. Retail showed 4 channels ("Channel - 1..4") and "Low" in a Dec 2009 build. B14 prints Idle/Normal/Busy. Were the 200/400 thresholds ever reached?
2. **E-B2:** Does a party survive a member's channel change? The party frames have no channel field. Options: auto-leave on disconnect (the current disconnect path) or keep with frozen HP bars.
3. **E-B3:** Where should the player spawn after a channel change: the saved map and position, or a town? The client asks nothing. Retail is unknown.
4. **E-B4:** What does "Waiting for the server to respond." with args (3,0,3,0) do? Is it dismissable, and is there any timeout for the login phase? The 2009 "no-response timer" at 0x43F0FC is patched out by patch_2009.py.
5. **E-B5:** gs+0x408 stays set after a channel change. Which later paths (server kick → server select, window 0x19 case 6 at FUN_00448730) reach a 0x02 success without FUN_0042dbf0, and so skip character select?
6. **E-B6:** Was the version server's 0x01 on the in-game path ever different from the launcher one (for example the same world forced)? The client hides other worlds itself.
7. **E-B7:** Guild traffic across channels (member lists, guild chat) belongs to the guild spec, but must share the global layer of B.7.
8. **E-B8:** Is S2C 0x0F subtype 2 still mapped to "is in other channel" in 2009? The string exists and the handler fingerprint matches 2008, but the jump-table entry was not re-read.
