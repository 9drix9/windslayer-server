# social_friend: friends, messenger chat rooms, mentors, notes, manner points

Design doc for the server team. Sources, in priority order: client binary evidence in
`corpus/systems/social_friend.json` and `corpus/protocol_spec.json`, then `LIVE_TEST_LOG.md`,
then `server/windslayer_server.py` (line numbers are from the 2793-line version dated 2026-09-17),
then memory notes, then PySlayer. PySlayer implements none of this group; its only related data is a
17-byte mentor name inside its KR 0x03.

Scope (31 specs + 1 spec gap):

| Dir | Opcodes |
|---|---|
| C2S | 0x2F FriendListRequest, 0x30 FriendAdd, 0x31 FriendRequestReply, **0x32 FriendDelete (spec gap, see 1.3)**, 0x33 ChatInvite, 0x34 ChatInviteReply, 0x35 RoomChat, 0x36 RoomLeave, 0x37 SetMyStatus, 0x44 MemosDeleteAll, 0x4B SendNote, 0x50 MentorRemove, 0x5C MentorRegister, 0x6D Compliment, 0x73 FriendSlotExpand |
| S2C | 0x0B FriendList, 0x0C FriendAddResult, 0x0D FriendRequestPrompt, 0x0E FriendDeleteResult, 0x10 ChatInvite, 0x60 FriendPresence, 0x62 RoomMemberLeft, 0x7A MentorRegisterResult, 0x7B MenteeLoggedIn, 0x7C MenteeRemove, 0x7D MentorStatus, 0x7E MenteeList, 0x94 ComplimentResult, 0x95 ReportResult, 0x96 MannerCompliment, 0x97 MannerUpdate, 0x9D FriendSlotExpandResult |
| Other groups this system needs | S2C 0x0F RoomJoinResult and 0x61 RoomMessage (chat), S2C 0x78 MemoList (mail_gift), S2C 0x77 NoteResult (premium_cash), C2S 0x6B /f friend chat and S2C 0x91 (chat), C2S 0x6E report (admin_gm), C2S 0x06 sub 0x0A /manner (admin_gm), C2S 0x71 and S2C 0x9B Friend Warp Stone (premium_cash), S2C 0x03/0x04/0x05/0x06/0x07 (world), C2S 0x2B refuse flags (login) |

---

## 1. How the client implements it

### 1.1 One client object: CMessenger

Every packet here goes through a single client object, **CMessenger** (RTTI `.?AVCMessenger@@`,
ctor `FUN_0046f670`, instance at `game_state(0x70EA00)+0x4E4`).

- **S2C:** handled in `SubHandler3_PacketHandler3 @0x4708C0`. It ignores everything unless
  CMessenger `+0x68` (app) and `+0x70` (UI root) are non-NULL, which is true from startup.
- **C2S:** sent on CMessenger `+0x74`, a copy of `g_sock_game` (TCP 7022). The copy is made by
  `FUN_0046f860`, which the **S2C 0x03** handler calls at `0x44E52F`.
  - Until the first 0x03 arrives, `+0x74` is NULL. The C2S builders then skip sending, and the
    S2C readers would read through a NULL socket.
  - 0x44 and 0x73 do not NULL-check the socket at all.

Key CMessenger fields:

| Offset | Meaning | Written by |
|---|---|---|
| +0x04 (head +0x08, count +0x10) | friend list, 0x28-byte nodes: name[17] +0x00, channel +0x14, friend_id +0x18, status +0x20, type +0x24 | 0x0B (replace), 0x0C (append), 0x0E (remove by name), 0x60 (update) |
| +0x14 (head +0x18, count +0x20) | mentor list (type 1). The UI allows one entry | 0x03 mentor block, 0x7A (append), 0x50 (local clear), 0x7D (updates head only) |
| +0x24 (head +0x28, count +0x30) | mentee list (type 2) | 0x7E (append), 0x7B (upsert by uid), 0x7C (remove by uid) |
| +0x34 (head +0x38, count +0x40) | memo list, 126-byte records | 0x78 (append), 0x44 (local clear) |
| +0x7C | own character name, used for self-checks and chat colouring | 0x03 |
| +0x90 | own channel ("same channel" substitute for value 100) | 0x03 (from player+0x1CF) |
| +0x94 | own uid (player+0x1D8) | 0x03 |
| +0x9C | own messenger status 0/2/3 | C2S 0x37 path; **reset to 0 by every 0x03** |
| +0xCC | current messenger room id (0 = none) | 0x0F subtype 1; cleared by 0x36 path and by every 0x03 |
| +0xD0 / +0xD1 | friend count / friend capacity | FUN_00470270 / 0x0B, 0x9D |
| +0xD8 | report category | report window |

**Every S2C 0x03 resets the messenger** (`FUN_0046f860`). It frees the friend, mentor, mentee and
memo lists (plus lists +0x44 and +0x54), sets capacity and count to 0, room id to 0 and status to 0.
Then it re-arms the socket.

The server replays 0x03 on every portal (`_handle_change_map`, windslayer_server.py:1298-1300). So
the client loses all messenger state on each map change. It then sends a bare **C2S 0x2F**, which
is the server's cue to resync everything.

### 1.2 UI entry points (window ids from `FUN_00472900`, `FUN_00446300`, `FUN_0044c4b0`)

| Window | Contents / controls | Leads to |
|---|---|---|
| 0x176 "My Friends" (UI 2643) | ctrl 7 Invite (friend selected; blocked if status==1 "is not on line." or room has >=10 members); ctrl 8 Add (blocked with "Can't be add. Visit Frenaiga." when count == capacity); ctrl 9 Delete (needs selection, opens 0x17A); ctrl 0x14 status dropdown with options 0x17->0, 0x18->3, 0x19->2 (labels Online/AFK/Busy: UI 2666, 8987, 2668); tabs 0x15/0x16/0x1A (0x1A = Mentor tab, count label "(%u)"); ctrl 0x1B Add Mentor (only if own job byte == 0, novice); ctrl 0x1C Delete Mentor (only if a mentor entry exists) | C2S 0x33, dialog 0x179, dialog 0x17A, C2S 0x37, dialog 0x1FE, window 0x200 |
| 0x179 add-friend name dialog | ctrl 5 edit, OK ctrl 2 (rejects an empty name) | C2S 0x30 {uid 0, name} |
| 0x17A delete confirm (UI 8001) | ctrl 4 = name label, +0x134 = friend_id; OK ctrl 2 | C2S 0x32 |
| 0x17C "Request of Friend Addition" (UI 2721/8006) | ctrl 2 = requester name, +0x134 = request_id; ctrl 4 accept, ctrl 5/6 refuse | C2S 0x31 |
| 0x177 Messenger chat room (UI 2675) | Enter in its input, or chat macro while focused; ctrl 7 invite-more (opens 0x178); closing the window | C2S 0x35, dialog 0x178, C2S 0x36 |
| 0x178 invite-by-name dialog | OK ctrl 2 (does NOT reject an empty name) | C2S 0x33 {uid 0, name} |
| 0x17D "Request to Conversate" (UI 2729/8007) | +0x134 = inviter_id; ctrl 4 accept, ctrl 5/6 refuse. Only opens if +0xCC == 0 | C2S 0x34 |
| 0x1FE register-mentor dialog | ctrl 5 edit, OK ctrl 2 | C2S 0x5C |
| 0x200 delete-mentor confirm | ctrl 4 mentor name; OK ctrl 2 | C2S 0x50 (client clears its mentor list immediately) |
| 0x3FE memo window | opened from HUD window 3 ctrl 8 (the "Msg" HUD button); ctrl 2/9 close, then confirm box 0x16 OK | C2S 0x44 |
| 0x3FD note window | opened by using item 1894 "Note (x1)" or 3320 "Notes x11"; ctrl 7 recipient, ctrl 9 contents, ctrl 3 send | C2S 0x4B, then modal "Waiting for the server to response." |
| 0x50 player popup | **right-click (WM_RBUTTONUP 0x205) or double-click (0x203)** on an entity with alive==3 and uid != own uid (asm 0x44C77D-0x44C7F0); +0x134 = entity uid; ctrl 7 add friend, ctrl 8 invite to chat, ctrl 10 compliment (window 0x435), ctrl 11 report (window 0x434) | C2S 0x30 / 0x33 with the real uid |
| 0x435 compliment | ctrl 8 name label, confirm ctrl 10 | C2S 0x6D |
| 0x47B Frenaiga slot expansion (UI 8990: "+5 slots", UI 8991 "Maximum of 50") | OK ctrl 2 (client gates: gold >= 50000 and capacity < 50) | C2S 0x73 |

Frenaiga is `gamedef.npcs idx 162` (Title_Idx 272, `UI` = 1147 = 0x47B, hsi npc001). She is not
in maps 101/102. Decrypted `hs/stageXX_01.hmi` place `NpcId="162"` at:

| Map | Position |
|---|---|
| 201 | (1600,1100) |
| 401 | (2194,1220) |
| 501 | (3200,1000) |
| 601 | (2300,715) |
| 801 | (1400,756) |
| 901 | (2500,700) |
| 1001 | (1600,529) |
| 1101 | (1600,710) |

The KR `portals.json` route from 101 to 201 is 101->102 (code 22), ->103 (14), ->104 (25),
->105 (42), ->106 (80), ->201 (51). EN codes can differ; the 101->102 code is 23 in EN.

### 1.3 Spec gap: C2S 0x32 FriendDelete

This opcode is not in `social_friend.json`. It only appears in the completeness audit of
`protocol_spec.json` as the "missed tail call 0x470018", so wsdev/wsproto have no grammar for it.

Verified here from `decomp/0046FFE0_FUN_0046ffe0.c` and the caller asm at 0x473004-0x47303D:

```
u32 friend_id        // window 0x17A +0x134, copied from friend node +0x18 by window 0x176 ctrl 9
str[17] friend_name  // window 0x17A ctrl 4 label (+0x50), 17 bytes copied
```

It is 21 bytes, sent on CMessenger+0x74. No send happens if the socket or the label is NULL. The
reply is S2C 0x0E. **Add this entry to protocol_spec.json (social_friend) before implementing.**

### 1.4 Identity values the client compares

| Value | Where the client compares it | Consequence |
|---|---|---|
| entity uid (+0x84) | 0x50 popup reads it; 0x96/0x97 look it up; 0x06 removes by it | Must be unique per online player |
| local uid (scene+0x220) | written by S2C 0x02 `account_id` **before character select**; 0x07 registers the local player only if the record uid equals it; 0x96 reads the complimenter name only if target_uid equals it | uid is effectively **per account** |
| friend_id (node+0x18) | echoed in C2S 0x32/0x33/0x6B; 0x60 applies only if name AND uid both match | Must equal the uid the friend uses in-world |
| mentor uid (+0x14 head +0x18) | 0x7D matches the head entry's uid | Same uid space |
| mentee uid | 0x7B upserts and 0x7C removes by uid | Same uid space |
| names | 0x0E removes by exact strcmp; 0x60 looks up by strcmp; 0x62 removes a room member by strcmp; 0x61 colours own lines by strcmp against +0x7C | Case-sensitive and unique; always NUL-terminated within 17 bytes (at most 16 chars) |

### 1.5 Constants the client uses

- **Status / presence byte** (friend node +0x20):

  | Value | Meaning |
  |---|---|
  | 0 | online, same channel. 0x60 prints "<name> has logged in. (Friend)" |
  | 1 | offline: greyed, sorted last, cannot be invited, excluded from /f chat |
  | 4 | online on another channel. 0x60 prints "(Friend, Channel N)" |
  | 2, 3 | stored and drawn as online, no message; C2S 0x37 sends 2 = Busy and 3 = AFK |

  - `/f` friend chat (C2S 0x6B) only includes entries with status 0 or 4 **and channel byte != 0**.
  - **Online friends must be sent with channel = 1**, never 0.
- **Channel:** single-channel server. `_build_pyslayer_opcode_03` sends channel_id=1 (line 2041).
- **Mentor/mentee channel sentinels:**

  | Packet | Sentinels |
  |---|---|
  | 0x7A / 0x7B / 0x7D / 0x7E | 100 = same channel (the client substitutes +0x90) |
  | 0x7D and the 0x03 mentor block | 0x66 = offline |
  | 0x03 mentor block | "same channel" means value == channel_id (so send 1). 100 is not special there |

- **Friend capacity:**
  - Byte +0xD1, max 50: C2S 0x73 is gated at < 50, UI 8991 "Maximum of 50".
  - +5 per Frenaiga purchase (UI 8990).
  - Client gold gate for the purchase: 50,000.
  - Default capacity is not in the client. UI placeholder 2662 is "(20/20)", so **default 20** is proposed.
- **Manner points:**
  - **Account-wide** (UI 8944/8957: "All characters in one account share the same amount of Manner Points").
  - Local value: scene+0xEE0, from S2C 0x02 `manner_points`.
  - Per-entity value: +0x15D8, from the i32 right after the uid in 0x07/0x04/0x05.
  - Client gates: chat needs > -40, whisper > -20, play rooms and battlefield need >= -19, trade needs >= -59.
  - Nameplate turns red below -9.
- **Compliment rules** (UI 8944): "only once a day"; "the same person only once a week". Result codes: 7 = once a day, 6 = same player twice, 5 = target reached today's cap.
- **Report rules** (UI 8957): once a day; fee by level. Client formula: `fee = ceil(level/10)*100`; the live log saw a 100-gold fee at level 1.

---

## 2. Request/response flows

Notation:

- `A` = acting session, `B` = other player.
- `A.uid` = account uid (see 3.1).
- `name17(x)` = name truncated to 16 bytes and NUL-padded to 17.
- All S2C payloads are built with `wsproto.Grammar` from protocol_spec.json after the codec fixes in 4.2.

### F1. Friend/messenger resync (C2S 0x2F). Runs after every 0x03.

1. Client handles S2C 0x03: the messenger resets, then the map loads.
2. At the end of the 0x03 handler the client sends **C2S 0x2F** (0 bytes), immediately followed by C2S 0x63. It does not wait for a reply.
3. Server resolves A's character (`_session_char`, line 1233) and builds the friend rows from `char['friends']` (ordered names, see 3.3). For each name:
   - **Online** (in `online_by_name`): `{name, channel=1, friend_id=<that session uid>, status=<that session msgr_status: 0/2/3>}`.
   - **Offline**: `{name, channel=0, friend_id=<uid of the owning account, from the name index>, status=1}`.
   - **Deleted character**: drop the name from the stored list.
4. Send **S2C 0x0B** `{friend_capacity=char.friend_capacity (default 20, <=50), friend_count=n (<= capacity, <=255), repeat[friend_count]=rows}`.
   - Always send it, even with n=0, otherwise capacity stays 0 and Add Friend is blocked ("Can't be add. Visit Frenaiga.", FUN_00472900 case 8).
5. If A has mentees online, send **S2C 0x7E** `{count, repeat[count]={channel=100, mentee_name, mentee_uid}}`.
   - Online only: 0x7E has no offline sentinel.
   - Skip the packet when count=0; the list was already cleared by 0x03.
6. If `char['memos']` is non-empty, send **S2C 0x78** `{count<=255, records}` (see F9).
   - Never send count=0: it lights the HUD memo button anyway.
7. The mentor is not sent here; it travels inside 0x03 (F8.1).

Refusals: none. Ignore 0x2F if `session['char_name']` is unset.

### F2. Add friend (C2S 0x30 -> S2C 0x0D -> C2S 0x31 -> S2C 0x0C)

**Client-side gates before 0x30:** name non-empty; name != own name ("You can't register
yourself."); name not already in list ("The player is already registered as friend."); count !=
capacity. The client then prints "Requesting <name> to be a friend".

1. **C2S 0x30** `{target_uid, target_name}`. target_uid is 0 from dialog 0x179, and the entity uid from popup 0x50.
2. Server resolves the target. If target_uid != 0, look up `online_by_uid[target_uid]` and require its char name == target_name; otherwise look up `online_by_name[target_name]`. Checks, in order; each failure sends **S2C 0x0C** to A:

   | Check | Reply to A |
   |---|---|
   | Name unknown in the name index | `{result=0x15, target_name}` "An error occurred while adding.<name>" |
   | Target is A's own character | drop (client already blocks) |
   | Name already in A's `friends` | `{result=6, target_name}` |
   | `len(A.friends) >= A.friend_capacity` | `{result=7, target_name}` (name ignored by client) |
   | Target not online | `{result=2, target_name}` "is not on line." |
   | Target session `refuse['friend']` != 0 (from C2S 0x2B, see 3.2) | `{result=4, target_name}` |
   | `len(B.friends) >= B.friend_capacity` | `{result=5, target_name}` |
   | A pending request A->B already exists | drop (no duplicate prompt) |

3. Record `friend_requests[B.uid][A.uid] = (A.char_name, now)`. Expiry is 120 s; drop the entry when either side disconnects.
4. Send **S2C 0x0D** to B: `{request_id=A.uid, requester_name=name17(A.char_name)}`. B's client opens dialog 0x17C.
5. **C2S 0x31** from B `{requester_id, requester_name, accept}`.
6. Server pops `friend_requests[B.uid][requester_id]`. If missing or expired, drop.
   - If A is gone (not in `online_by_uid`): **S2C 0x0C** to B `{result=2, target_name=requester_name}`.
   - **accept == 0:** **S2C 0x0C** to A `{result=3, target_name=B.char_name}` "has refused to register you as a friend."
   - **accept == 1:** re-check both capacities.
     - A full: A gets `{7}`, B gets `{5, A.name}`.
     - B full: A gets `{5, B.name}`, B gets `{7}`.
     - Otherwise append B.name to A.friends and A.name to B.friends (skip a side if already present), then persist (`_save_accounts` under `db_lock`).
     - **S2C 0x0C** to A: `{result=1, name=B.name, channel=1, friend_id=B.uid, status=B.msgr_status}`.
     - **S2C 0x0C** to B: `{result=0x0B, name=A.name, channel=1, friend_id=A.uid, status=A.msgr_status}`.
     - Both clients print "<name>has been registered in your friend list."

**Hazard:** the 0x0C success branch is `if(result == 1 || result == 0x0B)`. The current wsproto
encodes it as the 18-byte failure layout (bug 4.2-1). Also, never send result values outside
{1..7, 0x0B, 0x15}: the client shows a popup built from an uninitialised stack buffer.

### F3. Delete friend (C2S 0x32 -> S2C 0x0E)

1. UI: select a friend, then 0x176 ctrl 9, then dialog 0x17A OK. **C2S 0x32** `{friend_id, friend_name}`.
2. Server: if friend_name is not in A.friends, send **S2C 0x0E** `{success=0, name}` ("Deleting <name> from your friend list failed...").
3. Otherwise remove the name from A.friends (key on the name; ignore friend_id, which may be stale for offline entries) and persist. Send **S2C 0x0E** `{success=1, name}`. The client pops "<name>is deleted from friend list." and removes the node by name.
4. Directed lists: B's list is not touched and B gets nothing, because 0x0E always pops a modal. See open question 7.1.

### F4. Presence and my-status (S2C 0x60, C2S 0x37)

Watchers of a character X are the online sessions W whose `char['friends']` contains X's name.

1. **X enters the world** (end of `_handle_enter_world`, after 0x07 is sent; not on map change): every watcher W gets **S2C 0x60** `{friend_uid=X.uid, friend_name=X.name, channel=1, presence=0}`. W prints "<X> has logged in. (Friend)" and re-sorts.
2. **X disconnects** (the `finally:` in `_handle_fireway`, lines 578-581; only if X was in world): every W gets **S2C 0x60** `{X.uid, X.name, channel=0, presence=1}`. The client prints nothing and greys the entry.
3. **C2S 0x37** `{status}` with status in {0,2,3}; anything else is clamped to 0. Server sets `session['msgr_status']=status` and every W gets **S2C 0x60** `{X.uid, X.name, 1, status}`. Presence 0 re-prints "has logged in"; this is cosmetic, see 7.2.
4. **Server-initiated 0x03** (map change, line 1298): the client resets +0x9C to 0 and only sends 0x37 when the new value differs from +0x9C. If the server kept status 3, the player could never return to Online. So when `session['msgr_status'] != 0`, set it to 0 **before** sending 0x03 and push 0x60 presence 0 to watchers.
5. The 0x60 grammar tail is gated on client state (`if(friend_list contains entry named friend_name && entry.uid == friend_uid)`). The server only sends to real watchers, so it always emits the full 23 bytes (encode with the client-state assumption true, fix 4.2-2).

### F5. Messenger chat room (C2S 0x33/0x34/0x35/0x36; S2C 0x10/0x0F/0x61/0x62)

Server state: `rooms[room_id] = {'members': [uid,...]}` (ordered, max 10), `session['room_id']`,
and `chat_invites[invitee_uid][inviter_uid] = ts` (60 s expiry). room_id is a u32 from a
counter starting at 1 (never 0).

**Invite**

1. **C2S 0x33** `{target_uid, target_name}`. The client prints "Inviting <name> for a conversation." locally.
2. Server checks; each failure sends **S2C 0x0F** to A:

   | Check | Reply to A |
   |---|---|
   | A is in a room with >= 10 members | `{subtype=5, target_name}` |
   | Resolve B (uid first, then name); not found/offline, **including the all-zero name that dialog 0x178 can send** | `{subtype=8, target_name}` ("is not on line.", also marks the friend offline client-side) |
   | B is A | drop |
   | B `refuse['talk']` != 0 | `{subtype=4, B.name}` "is rejecting chatting." |
   | B already in a room (server state) | `{subtype=6, B.name}` |
   | Duplicate pending invite | drop |

   **Never send subtype 0, 7 or >= 9**: the client shows a popup built from an uninitialised buffer.
3. Store the invite and send **S2C 0x10** to B `{inviter_id=A.uid, inviter_name=A.name}`. B's client opens 0x17D. If B's client is somehow in a room it silently ignores this, so never block waiting.

**Reply**

4. **C2S 0x34** from B `{inviter_id, accept}`. Pop `chat_invites[B.uid][inviter_id]`; if missing, drop.
   - **accept=0:** A gets **S2C 0x0F** `{3, B.name}` "has refused to have a personal chat."
   - A offline: B gets `{8, A.name}`.
   - A's room full: B gets `{5, A.name}`.
5. **accept=1, A not in a room:** create the room with members [A, B].
   - A gets **S2C 0x0F** `{subtype=1, room_id, member_count=2, [A.name, B.name]}` and prints "B has entered."
   - B gets `{1, room_id, 2, [B.name, A.name]}` (see 7.3 on ordering).
6. **accept=1, A already in room R:**
   - B gets `{1, R, n+1, [all names...]}` (no "has entered" line, since the count is > 2).
   - Each existing member gets `{1, R, 1, [B.name]}`. The count-1 path appends B and prints "<B> has entered."
   - Member name nodes are not zeroed client-side: every name must be NUL-terminated within 17 bytes.

**Chat**

7. **C2S 0x35** `{text_len, text}`. Only valid when `session['room_id']` is set; otherwise drop.
   - Cut the text at the first NUL, clamp to **<= 58 bytes** (0x61 `sprintf_s(61,"  %s")` hazard), optionally apply the server-side curse filter.
   - Send **S2C 0x61** `{sender_name=A.name, msg_len, message}` to **every member including A**. The client never echoes its own line.

**Leave**

8. **C2S 0x36** (0 bytes). If A is in a room, remove it; every remaining member gets **S2C 0x62** `{member_name=A.name}` ("<A>has logged out.."). Delete the room when 0 members remain; keep it with 1 member so that member can invite again.
   - Accept 0x36 from a session in no room: the client sends it whenever window 0x177 hides.
   - The leaver is not sent 0x62; its +0xCC is already 0.
9. **Implicit leave:** run step 8 on disconnect and **before any server-initiated 0x03** (map change), because 0x03 zeroes +0xCC without sending 0x36.

### F6. Register mentor (C2S 0x5C -> S2C 0x7A + S2C 0x7B)

1. **C2S 0x5C** `{mentor_name}`. Client gates: dialog only for a novice (job byte == 0), name != own.
2. Server checks:

   | Check | Result |
   |---|---|
   | R = A's char has `class != 0` | drop (client gate bypassed) |
   | R already has a `mentor` | drop (the UI only offers Add when the mentor count is 0) |
   | M = char named mentor_name is missing or offline | **S2C 0x7A** `{result=0x66, mentor_name}` "can not be found." |
   | M `class == 0` | `{result=0x65, mentor_name}` "is a novice. Only a class-upgraded player can become a mentor." |
   | M is R | drop |

   - Error packets are 18 bytes (no uid).
   - Mentor name must be NUL-terminated (sprintf_s into a 0x80 buffer).
3. Success: `R.mentor = M.name`; `M.mentees += [R.name]` (dedupe); persist.
   - A gets **S2C 0x7A** `{result=0x64, mentor_name=M.name, mentor_uid=M.uid}` (22 bytes; 0x64 = same channel, status 0).
   - Send success exactly once: the client appends without checking for duplicates.
4. M's session gets **S2C 0x7B** `{mentee_uid=R.uid, channel=100, mentee_name=R.name}` (upsert; prints "<R> has logged in. (Menti)").

### F7. Remove mentor (C2S 0x50 -> S2C 0x7C)

1. **C2S 0x50** (0 bytes) from window 0x200 OK. The client has already freed its mentor list.
2. If R has no mentor, drop.
3. Otherwise clear `R.mentor`, remove R from `M.mentees`, persist. If M is online, send **S2C 0x7C** `{mentee_uid=R.uid}`.
4. No reply to R. If the server ever refuses, it must re-send the mentor (only possible through a new 0x03, or 0x7A success) to resync.

### F8. Mentor/mentee presence

1. **Mentee's 0x03** (enter world and every map change): `_build_pyslayer_opcode_03` must emit the mentor block instead of the hard-coded 0 at line 2059:
   `mentor_id = M.uid` (0 = none), `mentor_name = name17(M.name)`, `mentor_channel = 1 if M online else 0x66`.
2. **Mentee R logs in:** M (if online) gets **S2C 0x7B** `{R.uid, 100, R.name}`.
3. **Mentee R disconnects:** M gets **S2C 0x7C** `{R.uid}` (mentees are shown online-only).
4. **Mentor M logs in:** each online mentee R gets **S2C 0x7D** `{mentor_uid=M.uid, channel_or_state=100}` ("<M> has logged in.(Mentor)").
5. **Mentor M disconnects:** each online mentee gets **S2C 0x7D** `{M.uid, 0x66}` (silent, status 1).
6. **Mentor's own list:** 0x7E on every 0x2F (F1 step 5).

### F9. Notes / memos (C2S 0x4B -> S2C 0x77 + S2C 0x78; C2S 0x44)

1. **C2S 0x4B** `{item_id, recipient_name, contents}` (110 bytes).
   - After sending, the client closes 0x3FD and shows a **modal wait box**.
   - **The server must always answer S2C 0x77**, or the UI stays stuck.
2. Server checks:
   - item_id in {1894, 3320} and owned (premium inventory, premium_cash group); recipient exists in the name index. Otherwise send **S2C 0x77** `{result=0}` "Failed to send the message ."
   - item_id **9999** is the gift-dialog reply path from S2C 0x6D (mail_gift). Deliver it as a memo without consuming an item; whether 0x77 is expected is open question 7.6.
3. Store `{from: A.name, text: contents cut at NUL (<=90 chars), t: [year, month, dow, day, hour, minute, second, ms]}` in `recipient_char['memos']` (cap 100), then persist.
4. If the recipient is online, send **S2C 0x78** `{count=1, repeat[count]=[{sender_name, text (str[93]), year...millisecond}]}`. The client appends and lights HUD window 3 ctrl 8.
5. Send **S2C 0x77** `{result=1, cash_item_serial=<serial of the consumed premium record>}` to A. The client decrements that premium item.
6. **C2S 0x44** (0 bytes) after the "All the messages will be deleted" confirm: `char['memos'] = []`, persist, no reply.
7. Re-send the stored memos on every 0x2F (F1 step 6), because 0x03 frees the client list.

### F10. Compliment / manner points (C2S 0x6D -> S2C 0x94 + 0x96 + 0x97)

1. **C2S 0x6D** `{target_id, target_name}` from window 0x435. It needs a visible remote entity (popup 0x50), which depends on section 3.4.
2. Server checks:

   | Check | Reply to A |
   |---|---|
   | Resolve target T by `online_by_uid[target_id]` with matching name; else by name (online). Not found, or T is A | **S2C 0x94** `{result=2}` (1 byte) |
   | A's account already complimented today | `{result=7}` |
   | A's account complimented T's account within 7 days | `{result=6}` |
   | T's account received >= `COMPLIMENT_DAILY_CAP` today | `{result=5}` |

3. Apply `T.account.manner += COMPLIMENT_DELTA` (proposed 1) and persist both logs.
4. A gets **S2C 0x94** `{result=1, target_name=T.name}` (18 bytes).
5. T gets **S2C 0x96** `{target_uid=T.uid, manner_delta=+1, complimenter_name=A.name}`. **Always 25 bytes**; the name is read only by the target.
6. Other in-world sessions on T's map get **S2C 0x97** `{uid=T.uid, manner_delta=+1}`, so their entity+0x15D8 copy stays right.
7. Persisted manner must be sent back to the client:
   - S2C 0x02 `manner_points` (currently 0 at line 2678);
   - the 0x07/0x04/0x05 i32 after the uid (currently 1 at line 2308).

### F11. Report result (C2S 0x6E owned by admin_gm -> S2C 0x95 owned here)

1. **C2S 0x6E** `{target_uid, target_name, report_fee, category, content_len, content}`. It comes from HUD Report (uid may be 0) or popup ctrl 11.
2. Server checks:
   - Target not found by uid or name: **S2C 0x95** `{result=2}`.
   - Reporter's account already reported today: `{result=7}`.
   - Recompute the fee server-side: `ceil(level/10)*100`, minimum 100. If gold < fee, drop (the client pre-checked; there is no "short of gold" code).
3. Deduct the fee through the wallet (see bug 4.1-12), append `{reporter, target, category clamp 0..6, content, time}` to `reports.jsonl`, and send **S2C 0x95** `{result=1, gold=<absolute post-fee balance>}` (9 bytes).
4. Penalties are applied later by a GM through `/manner` (C2S 0x06 sub 0x0A), which broadcasts **S2C 0x97** `{uid, negative delta}` to T and T's map.

### F12. Friend slot expansion (C2S 0x73 -> S2C 0x9D)

1. **C2S 0x73** (0 bytes) from Frenaiga window 0x47B OK.
2. Server:
   - Optional proximity check: `session['current_map']` in {201,401,501,601,801,901,1001,1101}.
   - If `char.friend_capacity >= 50` or gold < `FRIEND_SLOT_PRICE` (50000), send **S2C 0x9D** `{result=0}` ("You failed to added him/her on your friend list.").
3. Success: `capacity = min(50, capacity+5)`, gold -= 50000, persist. Send **S2C 0x9D** `{result=1, friend_capacity=capacity, gold=<absolute balance>}` (10 bytes). The client updates +0xD1, the gold HUD and "(count/capacity)".

### F13. Cross-group dependencies that users will see as "friend features"

| Feature | Owner | What this system must provide |
|---|---|---|
| **/f friend chat** (C2S 0x6B, relayed as S2C 0x91) | chat | Online friend entries must carry channel != 0 and status 0/4 (F1), and friend_id must equal the recipient's session uid. A relay should check each (channel, friend_id) against the sender's `friends`. |
| **Friend Warp Stone** (C2S 0x71 -> S2C 0x9B + map change) | premium_cash | Uses the name index from 3.3. |
| **GM /manner** (C2S 0x06 sub 0x0A) | admin_gm | Builds S2C 0x97 from this group. |

---

## 3. Server state and data model (including multi-session support)

### 3.1 Identity: persistent per-account uid (prerequisite for everything)

- **Where:** `accounts.json` -> `accounts[user]['uid']`, a u32.
- **Assignment:** on load if missing. Keep `test` = 1, because the combat driver (lines 1774/1781) and the wsdev in-world detection look for uid 1. Other accounts get the next free integer.
- **Range:** must be != 0 (0 means "unknown" in C2S 0x30/0x33 and is refused by 0x6D) and < 0xF0000 (`MOB_UID_BASE`, line 138). Client NPCs use 33,000,000+ and char-select entities 30,000,000+.
- **`_handle_login` (line 2656):** `session['account_id'] = account['uid']` instead of `1`. This value goes out in S2C 0x02 (line 2675) and becomes scene+0x220.
- **`_handle_enter_world` (line 750) and every other 0x07/0x1D/0x1E/0x22 builder:** set `char['uid'] = session['account_id']` (or pass the uid explicitly). Today `_build_en_opcode_07` reads `char.get('uid', 1)` (line 2307) and only `_handle_change_map` sets it (line 1296). An account with uid != 1 would spawn unregistered, i.e. invisible and uncontrollable.
- **One uid per account:** sibling characters share it. This is safe because 0x60 matches on name AND uid, and only one character per account can be online.

### 3.2 Runtime session fields (session dict, created at lines 509-518)

| Field | Set when | Used by |
|---|---|---|
| `uid` (= `account_id`) | login | all builders, indexes |
| `char_name` (exists, line 721) | enter world | name index, self checks |
| `in_world` bool | after 0x07 is sent in `_handle_enter_world` | presence fan-out; skip sessions at char select for pushes and admin injection |
| `no_enc` | **every C2S packet in `_dispatch`** (`session['no_enc'] = no_enc`) | pushes to *another* session must use *that* session's mode, not the sender's |
| `current_map` (exists, lines 722/1276) | enter world, portal | 0x97 bystanders, 0x05/0x06 visibility |
| `refuse` {whisper, exchange, party, talk, friend} | C2S 0x2B bytes 0..4 (grammar `u8 refuse_whisper; u8 refuse_exchange; u8 refuse_party; u8 refuse_talk; u8 refuse_friend; str[16] p2p_ip; u32 p2p_udp_port; str[17] char_name`), currently ignored at lines 701-703 | 0x0C code 4, 0x0F code 4 (whisper and party for other groups) |
| `msgr_status` 0/2/3 | C2S 0x37; reset to 0 before every server-sent 0x03 | 0x0B/0x0C status, 0x60 |
| `room_id` | F5 | 0x35/0x36, invite checks |

### 3.3 GameServer-level state

```
self.world_lock      = threading.RLock()   # guards everything below
self.db_lock         = threading.Lock()    # guards accounts.json mutation + _save_accounts (lines 405-409)
self.online_by_uid   = {}   # uid -> session (in_world only)
self.online_by_name  = {}   # char_name -> session
self.name_index      = {}   # char_name -> (username, char_dict)  built at load, updated on create
self.friend_requests = {}   # target_uid -> {requester_uid: (name, ts)}
self.chat_invites    = {}   # invitee_uid -> {inviter_uid: ts}
self.rooms           = {}   # room_id -> {'members': [uid, ...]}
self.next_room_id    = 1
```

- **Register:** in `_handle_enter_world`, after 0x07.
- **Unregister:** in the `finally:` of `_handle_fireway` (line 578), through a new `self._on_disconnect(session)`. It does, in order: room leave (F5.8), presence offline (F4.2), mentor/mentee offline (F8.3/F8.5), drop pending requests/invites, 0x06 despawn to map peers (3.4), persist.
- **Push helper.** `_send_encrypted` already serialises per session (`send_lock`, line 585), so cross-thread sends are safe:

```python
def _push(self, target, opcode, payload):
    if target.get('sock') and target.get('in_world'):
        self._send_encrypted(target['sock'], target, opcode, payload,
                             use_by_array=target.get('no_enc', True))
```

- **Grammar-driven builders.** Load `protocol_spec.json` once into `{(dir, opcode): Grammar}` and build every S2C with `Grammar.encode(rec, assume=...)`. C2S 0x32 needs a local grammar until the spec is updated (1.3). Parse C2S with `Grammar.decode(payload)`.

### 3.4 Field visibility (needed for popup-based add/invite/compliment/report)

The popup requires a remote entity with alive==3 and a distinct uid. The world group owns these
packets; the social system needs at least:

1. **Enter world or portal arrival of X on map m:**
   - X gets its own 0x07 (as today);
   - then X gets **S2C 0x04** `{player_count=k, records of the k other in-world sessions on m}` (same record layout as 0x07);
   - each peer on m gets **S2C 0x05** `{X's record without the trailing shop bool}` (367 bytes).
   - Never send a record whose uid equals the receiver's own uid: it re-registers the local player.
2. **X leaves m** (portal or disconnect): peers on m get **S2C 0x06** `{uid=X.uid}`. Never send the receiver its own uid (dangling local-player pointer).
3. **Remote records built from `_build_en_opcode_07`** must fix three things for remote use:
   - cur_hp/cur_mp are 0 (lines 2391-2392): remote players spawn dead and untargetable;
   - +0x8B3..+0x8B6 carry IP octets (lines 2379-2387): send idle defaults 8,0,0,0;
   - motion defaults (lines 2367-2372): send 0/0/8/8/0/2.
4. **Movement relay** of C2S 0x0D to peers (S2C 0x1B, movement group) keeps click positions right. Without it, remote entities sit at their spawn point, but right-clicking them still works.

### 3.5 Persistence (accounts.json)

```jsonc
"<username>": {
  "password": "...",
  "uid": 2,                                   // NEW, u32, stable
  "manner": 0,                                // NEW, account-wide i32 -> 0x02 manner_points, 0x07/0x04/0x05 i32
  "social": {                                 // NEW, account-wide limits
    "compliment_given_day": "2026-09-17",
    "complimented_accounts": {"<username>": "2026-09-17"},   // weekly rule
    "compliments_received": {"day": "2026-09-17", "count": 0},
    "report_day": "2026-09-17"
  },
  "characters": [{
    "name": "TestHero", "class": 0, "...": "...",
    "friends": ["test"],                     // NEW, ordered, directed list, names <= 16 bytes
    "friend_capacity": 20,                   // NEW, 20..50 step 5
    "mentor": null,                          // NEW, name or null
    "mentees": [],                           // NEW, names
    "memos": [{"from": "test", "text": "hi", "t": [2026,9,4,17,6,55,0,0]}]   // NEW, cap 100
  }]
}
```

- **Renames/deletes:** a character name missing from `name_index` is dropped lazily from friend/mentee lists on the next F1.
- **Name rules:** character creation (`_handle_create_character`, line 2572) must reject duplicate names and names longer than 16 bytes (payload[10:27] can carry 17 non-NUL bytes, line 2590). Names are the relation key and the client compares them with strcmp.
- **Other content:**
  - gamedef `items` rows 1894/3320 (Note, Type 5, Cash 1), 3429/3431/3433 (Friend Warp Stone), 3327-3332 (gift certificates);
  - `npcs` row 162 (Frenaiga, UI 1147).
  - `quests`/`maps` are not used.
- **Constants:**

  | Constant | Value | Source |
  |---|---|---|
  | `FRIEND_CAP_DEFAULT` | 20 | UI placeholder 2662 |
  | `FRIEND_CAP_MAX` | 50 | client gate + UI 8991 |
  | `FRIEND_SLOT_STEP` | 5 | UI 8990 |
  | `FRIEND_SLOT_PRICE` | 50000 | client gold gate |
  | `ROOM_MAX` | 10 | client +0xC8 check |
  | `ROOM_TEXT_MAX` | 58 | |
  | `MEMO_MAX` | 100 | |
  | `COMPLIMENT_DELTA` | 1 | |
  | `COMPLIMENT_DAILY_CAP` | 5 | proposed |
  | `REPORT_FEE` | `ceil(level/10)*100` | |

---

## 4. Current server status and proven bugs

### 4.0 Status per opcode

| Opcode | Status in windslayer_server.py |
|---|---|
| C2S 0x2F | **partial**: consumed at `_dispatch` lines 630-631 with the wrong label "arena query - ignoring"; no 0x0B reply |
| C2S 0x30/0x31/0x32/0x33/0x34/0x35/0x36/0x37/0x44/0x4B/0x50/0x5C/0x6D/0x73 | missing; they fall to "Unhandled opcode" (line 660). This is harmless for all except 0x4B, which leaves the client in a modal wait |
| S2C 0x0B/0x0C/0x0D/0x0E/0x10/0x60/0x62/0x7A-0x7E/0x94-0x97/0x9D | missing (never built) |
| S2C 0x03 mentor block | hard-coded `mentor_id=0` (line 2059) |
| Multi-session | sessions dict keyed by addr (line 518); no online index, no pushes to other sessions, no visibility broadcast |

### 4.1 Server bugs (windslayer_server.py)

1. **0x2F is never answered, so friends are unusable** (lines 630-631).
   - The client's 0x03 handler zeroes capacity and count (FUN_0046f860). Without S2C 0x0B, count == capacity == 0.
   - Window 0x176 Add (FUN_00472900 case 8) therefore always shows "Can't be add. Visit Frenaiga.", and the list shows "(0/0)".
   - The log label "arena query" is wrong; arena lists are C2S 0x2C/0x74.
   - Fix: F1.
2. **Every login gets uid 1** (line 2656 `session['account_id'] = 1`).
   - Two clients share scene+0x220 = 1 and the entity uid 1.
   - Friend ids, 0x60/0x7D matching, 0x96 targeting and the popup's target uid cannot tell the players apart.
   - Once visibility is added, a 0x04/0x05 record with uid == the receiver's uid re-registers the receiver's local player pointer (0x05 hazard 2).
   - Fix: 3.1.
3. **Enter-world 0x07 uses the stored char dict's uid, not the session uid** (line 750 -> `_build_en_opcode_07` line 2307 `u32(char.get('uid', 1))`).
   - Only `_handle_change_map` sets `char['uid']` (line 1296).
   - With any account uid != 1, the local player fails the registration gate (entity+0x84 != scene+0x220): invisible and uncontrollable.
   - The same default hides in `_build_en_opcode_1D`/`_1D_equip`/`_1E` (lines 2212, 2232, 2260) and `_send_exp` (line 2011).
   - Fix: pass `session['account_id']` everywhere.
4. **0x03 never carries a mentor** (line 2059 writes u32 0).
   - Mentor relations cannot survive login or map change, and S2C 0x7D is ignored by the client (no head entry to match).
   - Fix: F8.1.
5. **Server-initiated 0x03 on portal wipes messenger state with no server-side follow-up** (lines 1298-1300). The client clears room id +0xCC and status +0x9C without sending C2S 0x36/0x37.
   - A server with rooms would keep the player in a room whose lines the client now ignores.
   - A player left at status Busy could never return to Online (0x37 is only sent when the value differs from the reset 0).
   - Fix: F4.4 and F5.9 hooks before sending 0x03.
6. **C2S 0x2B refuse flags ignored** (lines 701-703 parse only `payload[25:42]`).
   - "Refuse friend addition" (UI 3049) and refuse-talk cannot be honoured (0x0C code 4, 0x0F code 4).
   - Fix: decode with the C2S 0x2B grammar into `session['refuse']`.
7. **Character names are not unique and can be 17 bytes** (`_handle_create_character` lines 2590 and 2601-2612).
   - Name-keyed relations become ambiguous.
   - A 17-byte name with no NUL, echoed in str[17] fields (0x0C/0x0D/0x0E/0x10/0x60/0x61/0x62/0x7A/0x94/0x96), makes client `sprintf_s`/strcpy over-read: garbage text or a CRT abort.
   - Fix: reject duplicates and names over 16 bytes at creation.
8. **`accounts.json` writes are not thread-safe** (`_save_accounts` lines 405-409, called from per-connection threads).
   - Two sessions mutating friends at once can raise "dictionary changed size during iteration" or write a torn file.
   - Fix: `db_lock` around mutate+dump.
9. **Admin injection hits every connected session, including ones at char select** (`_admin_listener` lines 459-461).
   - In two-client tests you cannot target one client.
   - A client at char select has CMessenger+0x74 == NULL, and injected SubHandler3 packets would read through a NULL socket (0x0B spec gate). This is a possible crash.
   - Fix: optional `"user"`/`"uid"` filter, and skip sessions not `in_world`.
10. **Local-memory combat driver is single-client** (`_find_client_pid` lines 1659-1676 matches only `windslayer_patched.exe`; `_memory_melee` lines 1678-1698 applies that client's swing to every session's monsters).
    - With two clients, client 2 can never hit, and client 1's swings damage client 2's monsters.
    - Not a social bug, but it corrupts two-player test sessions. Scope `_memory_melee` to the session whose uid is 1 until combat is packet-driven.
11. **Manner points are neither persisted nor consistent**:
    - S2C 0x02 sends `manner_points = 0` (line 2678);
    - 0x07 sends i32 1 at +0x15D8 (line 2308).
    - Compliments and reports cannot persist, and the local and entity copies disagree by 1.
    - Fix: account `manner` in both.
12. **Two gold sources of truth**:
    - 0x03 tells the client gold = 100000 (line 2044);
    - `_wallet` defaults to 999999 (lines 994-1000).
    - The absolute gold in 0x9D/0x95 (and 0x18/0x19) will jump the HUD from 100,000 to ~949,999 on the first purchase.
    - Fix: one persisted `gold` used by both 0x03 and `_wallet`.
13. **Wrong comment**: lines 975-976 say the client's receive handler 0x0047091D is "the shop-inventory listing". It is the **S2C 0x0B FriendList** handler.

### 4.2 Codec and harness bugs (block grammar-driven implementation and injection tests)

1. **`wsproto._eval` treats hex literals as client-state conditions** (wsproto.py lines 183-187).
   - **Cause:** `re.findall(r'[A-Za-z_]\w*', py)` extracts `x0B` from `0x0B`, finds it missing from env, and raises `ClientStateCondition`, so the branch is taken as false.
   - **Verified by running the codec:**
     - `_eval('result == 1 || result == 0x0B', {'result': 1})` raises ClientStateCondition;
     - `Grammar(0x0C).encode({'result':1,...})` returns **18 bytes (failure layout)**; the client reads 6 bytes past the end for channel/friend_id/status;
     - `Grammar(0x7A).encode({'result':0x64,...})` returns 18 bytes with **no mentor_uid**;
     - decoding a real 22-byte 0x7A raises "4 trailing byte(s)".
   - Also affects the 0x04/0x07 buff condition `0x0A31..0x0A3B` (world group).
   - **Fix:** strip hex literals before the name scan: `names = set(re.findall(r'[A-Za-z_]\w*', re.sub(r'0x[0-9a-fA-F]+', '0', py)))`.
2. **Client-state conditional blocks encode as absent** (`Grammar.encode` without `assume`, and `wsdev.py cmd_sendspec` lines 398-415, which never passes `assume`):

   | Packet | Encodes as | Should be |
   |---|---|---|
   | 0x60 | 21 bytes | 23 (channel+presence missing) |
   | 0x61 | 0 bytes | full message |
   | 0x62 | 0 bytes | 17 |
   | 0x96 | 8 bytes | 25 (name missing) |
   | 0x6F (premium_cash) | 0 bytes | full list |

   - **Fix:** a `assume_client_state=True` default for server builders (the server only sends when the condition holds), plus a `--assume` flag on `sendspec`. Until fixed, use `wsdev.py send <op> <hex>` with the hex in section 6.
3. **`str[N]` encode does not guarantee a NUL** (wsproto.py line 285, `raw[:size].ljust(size)`). Builders must pass names truncated to 16 bytes (`name17`) and texts truncated to N-1.
4. **wsview has no right-click** (`mouse_click_screen` lines 198-203 sends only LEFTDOWN/LEFTUP). Player popup 0x50 opens on WM_RBUTTONUP (0x205) or WM_LBUTTONDBLCLK (0x203) (asm 0x44C786-0x44C79D). Add `rclick x y` (MOUSEEVENTF_RIGHTDOWN 0x0008 / RIGHTUP 0x0010) and `dclick`.

---

## 5. Implementation plan

Ordered by dependency. Effort: S < 1 day, M 1-3 days, L > 3 days.

| # | Item | Pri | Eff | Depends | Opcodes |
|---|---|---|---|---|---|
| 1 | **codec-fixes**: wsproto hex-literal fix; `assume_client_state` encode option; `sendspec --assume`; `name17()` helper. Add a C2S 0x32 spec entry | P0 | S | - | 0x0C, 0x7A, 0x60, 0x61, 0x62, 0x96, 0x32 |
| 2 | **session-identity**: persistent `accounts[*]['uid']`; login uses it; all builders use `session['account_id']`; `session['no_enc']`, `in_world`, `refuse` (0x2B decode), `msgr_status`; `online_by_uid`/`online_by_name`/`name_index`; `_push`; `_on_disconnect` hook; `world_lock`/`db_lock` | P1 | M | 1 | 0x02, 0x07, 0x2B |
| 3 | **social-persistence**: char fields `friends`/`friend_capacity`/`mentor`/`mentees`/`memos`, account `manner`/`social`; migration on load; unique names of at most 16 bytes at creation | P1 | S | 2 | 0x0E (C2S create) |
| 4 | **friend-list-sync**: C2S 0x2F -> S2C 0x0B (+0x7E, +0x78 when non-empty); fix the 0x2F log label | P1 | S | 1, 2, 3 | 0x2F, 0x0B |
| 5 | **friend-add**: 0x30 -> 0x0D -> 0x31 -> 0x0C with codes 2/3/4/5/6/7/0x15, pending-request table with expiry | P1 | M | 4 | 0x30, 0x0D, 0x31, 0x0C |
| 6 | **friend-delete**: 0x32 -> 0x0E (directed) | P1 | S | 4 | 0x32, 0x0E |
| 7 | **presence-status**: 0x60 on enter/disconnect/0x37; reset status before server-sent 0x03 | P1 | M | 4 | 0x60, 0x37, 0x03 |
| 8 | **harness-multiclient**: wsview `rclick`/`dclick`; admin injection target filter + in_world skip; `cap --client 2` passthrough; scope `_memory_melee` to uid 1 | P1 | S | 2 | injection of 0x05/0x0B/0x0D/0x10 |
| 9 | **player-visibility** (with the world group): 0x04 to the arriving player, 0x05 to peers, 0x06 on leave; remote record fixes (HP/MP, idle motion bytes) | P1 | L | 2 | 0x04, 0x05, 0x06, 0x07 |
| 10 | **messenger-chat-room**: invites, rooms, relay, leave, implicit leave on disconnect and before server-sent 0x03 | P2 | M | 2 | 0x33, 0x10, 0x34, 0x0F, 0x35, 0x61, 0x36, 0x62 |
| 11 | **mentor**: 0x5C/0x7A/0x7B, 0x50/0x7C, 0x03 mentor block, 0x7D/0x7B/0x7C presence, 0x7E on 0x2F | P2 | M | 3, 7 | 0x5C, 0x7A, 0x50, 0x7B, 0x7C, 0x7D, 0x7E, 0x03 |
| 12 | **memos**: 0x4B -> store + 0x78 push + 0x77 reply (always); 0x44 delete; resend on 0x2F. Item consumption needs the premium inventory serial (premium_cash) | P2 | M | 3, 4 | 0x4B, 0x77, 0x78, 0x44 |
| 13 | **friend-slot-expand**: 0x73 -> 0x9D with one gold source of truth (fix 4.1-12) | P2 | S | 4 | 0x73, 0x9D |
| 14 | **manner-compliment**: 0x6D -> 0x94/0x96/0x97; daily/weekly limits; manner in 0x02 and 0x07/0x04/0x05 | P2 | M | 3, 9 | 0x6D, 0x94, 0x96, 0x97, 0x02 |
| 15 | **report-result**: 0x6E (admin_gm) -> 0x95 with fee, once per day, report log | P3 | S | 3 | 0x6E, 0x95 |

Suggested code shape:

- A new `server/social.py` with `class Social` holding the section 3.3 state and one method per C2S opcode.
- `_dispatch` routes 0x2F/0x30/0x31/0x32/0x33/0x34/0x35/0x36/0x37/0x44/0x4B/0x50/0x5C/0x6D/0x73 to it.
- Lifecycle calls: `social.on_enter_world(session)` at the end of `_handle_enter_world`, `social.before_server_0x03(session)` in `_handle_change_map` before line 1300, and `social.on_disconnect(session)` in `_handle_fireway`'s `finally`.

---

## 6. Live test plan

### Conventions

- **Single-client injection tests:** client 1 = `test`/`TestHero` (uid 1, class 0 = novice), in world on map 101. Fake remote players are Bob uid 2, Carol uid 3, Dave uid 4, Eve uid 5.
- **Commands** run from `WindSlayer2Game\server`:
  - `python wsdev.py send <op> <hex...>` injects raw bytes;
  - `python wsdev.py cap <secs> <wsview action>` captures C2S;
  - `python wsdev.py shot` takes a screenshot.
- **Hex vs sendspec:** use `send` with the hex below wherever 4.2 bugs apply (0x0C success, 0x7A success, 0x60, 0x61, 0x62, 0x96, 0x6F). UI coordinates are not yet mapped, so locate controls with `shot` first.
- **Injection targets every session:** inject only while every connected client is in world (bug 4.1-9).

### 6.1 S2C injection (works on today's server)

| # | Opcode | Prereq | Command | Expected | Risk |
|---|---|---|---|---|---|
| 1 | 0x0B | in world | `send 0B 14 02 42 6F 62 00 00 00 00 00 00 00 00 00 00 00 00 00 00 01 02 00 00 00 00 43 61 72 6F 6C 00 00 00 00 00 00 00 00 00 00 00 00 00 03 00 00 00 01` | Friend window 0x176 shows Bob (online) above Carol (greyed), label "(2/20)" | safe |
| 2 | 0x60 | #1 | `send 60 03 00 00 00 43 61 72 6F 6C 00 00 00 00 00 00 00 00 00 00 00 00 01 00`, then `send 60 03 00 00 00 43 61 72 6F 6C 00 00 00 00 00 00 00 00 00 00 00 00 02 04` | Green "<Carol> has logged in. (Friend)", then "<Carol> has logged in.(Friend, Channel 2)"; Carol no longer greyed | safe |
| 3 | 0x60 (status 2/3 icons) | #1 | `send 60 02 00 00 00 42 6F 62 00 00 00 00 00 00 00 00 00 00 00 00 00 00 01 02`, then the same with last byte `03`; screenshot the list | No chat line; record which icon status 2 and status 3 draw (open question 7.2) | safe |
| 4 | 0x60 offline | #1 | `send 60 02 00 00 00 42 6F 62 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 01` | Bob greyed and sorted last, no message | safe |
| 5 | 0x0C success | #1 | `send 0C 01 44 61 76 65 00 00 00 00 00 00 00 00 00 00 00 00 00 01 04 00 00 00 00` | "Dave has been registered in your friend list.", "(3/20)" | safe |
| 6 | 0x0C failure | - | `send 0C 06 42 6F 62 00 00 00 00 00 00 00 00 00 00 00 00 00 00` | Popup "<Bob>is already in your friend list." (repeat with 02/03/04/05/07/15 as the first byte). **Never test 00/08..0A/0C..14/16+** | safe (listed codes only) |
| 7 | 0x0D | in world | `send 0D 02 00 00 00 42 6F 62 00 00 00 00 00 00 00 00 00 00 00 00 00 00` | Dialog 0x17C "Bob would like to be friends with you. Will you accept?" | safe |
| 8 | 0x0E | #1 | `send 0E 01 42 6F 62 00 00 00 00 00 00 00 00 00 00 00 00 00 00` | Popup "<Bob>is deleted from friend list.", Bob removed, count -1 | safe |
| 9 | 0x10 | not in room | `send 10 02 00 00 00 42 6F 62 00 00 00 00 00 00 00 00 00 00 00 00 00 00` | Dialog 0x17D "Bob would like to have a chat with you" | safe |
| 10 | 0x0F join (chat group, needed here) | - | `send 0F 01 07 00 00 00 02 54 65 73 74 48 65 72 6F 00 00 00 00 00 00 00 00 00 42 6F 62 00 00 00 00 00 00 00 00 00 00 00 00 00 00` | Window 0x177 opens; members TestHero, Bob; line "Bob has entered."; room id 7 stored | state_change |
| 11 | 0x61 | #10 | `send 61 42 6F 62 00 00 00 00 00 00 00 00 00 00 00 00 00 00 05 68 65 6C 6C 6F` | Room window "Bob : " (blue) then "  hello". Keep len <= 58 | safe (len <= 58); crash_risk if longer |
| 12 | 0x62 | #10 | `send 62 42 6F 62 00 00 00 00 00 00 00 00 00 00 00 00 00 00` | "Bob has logged out.." in the room window; Bob removed from members | safe |
| 13 | 0x7A success | TestHero has no mentor | `send 7A 64 42 6F 62 00 00 00 00 00 00 00 00 00 00 00 00 00 00 02 00 00 00` | Green "<Bob>is registered as your mentor."; Mentor tab shows Bob; Delete Mentor enabled | state_change |
| 14 | 0x7A errors | - | `send 7A 65 42 6F 62 00 00 00 00 00 00 00 00 00 00 00 00 00 00` / first byte `66` | Red "<Bob>is a novice..." / "<Bob>can not be found." | safe |
| 15 | 0x7D | #13 | `send 7D 02 00 00 00 64`, then `send 7D 02 00 00 00 66` | "<Bob> has logged in.(Mentor)"; then no message (status offline on next refresh) | safe |
| 16 | 0x7E / 0x7B / 0x7C | - | `send 7E 01 64 44 61 76 65 00 00 00 00 00 00 00 00 00 00 00 00 00 04 00 00 00`; `send 7B 05 00 00 00 03 45 76 65 00 00 00 00 00 00 00 00 00 00 00 00 00 00`; `send 7C 04 00 00 00` | Mentor tab lists Dave; "<Eve> has logged in.(Menti, Channel 3)" and Eve added; Dave removed; "(n)" label updates | safe |
| 17 | 0x94 | - | `send 94 01 42 6F 62 00 00 00 00 00 00 00 00 00 00 00 00 00 00`; `send 94 07` | Notice "<Bob> has been complimented"; "You can make a compliment only once a day." | safe |
| 18 | 0x95 | note the current gold | `send 95 01 3C 86 01 00 00 00 00 00`; `send 95 07` | "Reported successively.", gold HUD shows 99,900 (absolute); "only once a day" | state_change (client gold) |
| 19 | 0x96 | uid 1 in world | `send 96 01 00 00 00 01 00 00 00 42 6F 62 00 00 00 00 00 00 00 00 00 00 00 00 00 00` | Green "<Bob> added 1 of your manner points."; manner number in window 0x1B +1 | state_change (client-local) |
| 20 | 0x97 | uid 1 in world | `send 97 01 00 00 00 FB FF FF FF` | Manner number -5 (red if negative), no message | state_change (client-local) |
| 21 | 0x9D | #1 | `send 9D 01 19 50 C3 00 00 00 00 00 00`; `send 9D 00` | Notice "You added him/her on your friend list...", label "(n/25)", gold 50,000; failure notice | state_change (client gold) |
| 22 | 0x78 (mail_gift, needed for 0x44) | - | `send 78 01 42 6F 62 00 00 00 00 00 00 00 00 00 00 00 00 00 00` + `48 65 6C 6C 6F 20 66 72 6F 6D 20 42 6F 62` + 79x `00` + `EA 07 09 00 04 00 11 00 06 00 37 00 00 00 00 00` (127 bytes total; or `sendspec 78 '{"count":1,"repeat[count]":[{"sender_name":"Bob","text":"Hello from Bob","year":2026,"month":9,"day_of_week":4,"day":17,"hour":6,"minute":55}]}'`) | HUD Msg button lights; memo window shows Bob, "Hello from Bob", "2026/9/17 6:55" | safe |
| 23 | 0x05 fake remote (world group, needed for popup tests) | map 101 | `sendspec 05 '{"name":"Bob","uid":2,"manner_points":0,"job1":1,"level":10,"rank_emblem":99,"repeat[14]":[{"appearance":0},{"appearance":1},{"appearance":1},{"appearance":0},{"appearance":2},{"appearance":2},{"appearance":2},{"appearance":0},{"appearance":0},{"appearance":0},{"appearance":0},{"appearance":0},{"appearance":0},{"appearance":0}],"motion_id":8,"attack_dir_8cf":8,"facing_dir":2,"input_dir_8b3":8,"pos_x":260.0,"pos_y":667.0,"cur_hp":100,"cur_mp":50}'`, then `wsview.py state` | Entity "Bob" alive=3 uid 0x00000002 near (260,667). Remove afterwards with `send 06 02 00 00 00` | state_change |

### 6.2 C2S captures (triggerable today; the server logs "Unhandled")

| # | Opcode | Prereq | Steps | Expected capture | Risk |
|---|---|---|---|---|---|
| 24 | 0x2F | in world | `cap 8 key down` while standing on the map-101 portal (live log x~1404) | `C2S 0x2F payload=0B` right after 0x03, followed by 0x63 | state_change (map change) |
| 25 | 0x30 (typed) | 6.1 #1 (capacity 20 > count) | Open window 0x176 (find the hotkey or Char Info path by screenshot, see 7.8) -> Add (ctrl 8) -> type `Bob` in dialog 0x179 -> `cap 3 click <OK>` | `0x30 payload=21B 00 00 00 00 42 6F 62 00 ...`; chat "Requesting Bob to be a friend" | safe |
| 26 | 0x30 (popup) | 6.1 #23 Bob uid 2 spawned; wsview `rclick` (4.2-4) or two fast clicks | Right-click Bob -> popup 0x50 ctrl 7 -> `cap 3 click <ctrl7>` | `0x30 payload=21B 02 00 00 00 42 6F 62 ...` | safe |
| 27 | 0x31 | 6.1 #7 dialog open | `cap 3 click <Accept ctrl4>`; repeat #7 then click Refuse | `0x31 22B 02 00 00 00 42 6F 62 00.. 01` and `... 00` | safe |
| 28 | 0x32 | 6.1 #1 | Select Bob in 0x176 -> Delete (ctrl 9) -> dialog 0x17A -> `cap 3 click <OK>` | `0x32 21B 02 00 00 00 42 6F 62 ...` (confirms the spec gap grammar) | safe |
| 29 | 0x33 | 6.1 #1 (Bob status 0) | Select Bob -> `cap 3 click <Invite ctrl7>` | `0x33 21B 02 00 00 00 42 6F 62 ...`; system line "Inviting Bob for a conversation." | safe |
| 30 | 0x34 | 6.1 #9 dialog open | `cap 3 click <Accept ctrl4>`; again with Refuse | `0x34 5B 02 00 00 00 01` / `... 00` | safe |
| 31 | 0x35 | 6.1 #10 (room id 7) | Click the room input, type `hi`, `cap 3 key enter` | `0x35 3B 02 68 69`; no local line appears (proves the server must echo 0x61) | safe |
| 32 | 0x36 | 6.1 #10 | `cap 3 click <room window close>` | `0x36 0B` | safe |
| 33 | 0x37 | in world | 0x176 ctrl 0x14 dropdown -> `cap 3 click <option 0x18>`, then 0x19, then 0x17 | `0x37 1B 03`, `0x37 1B 02`, `0x37 1B 00`; re-selecting the same option sends nothing | safe |
| 34 | 0x5C | TestHero class 0 | Mentor tab (ctrl 0x1A) -> Add Mentor (0x1B) -> dialog 0x1FE type `Bob` -> `cap 3 click <OK>` | `0x5C 17B 42 6F 62 00 ...` | safe |
| 35 | 0x50 | 6.1 #13 | Delete Mentor (0x1C) -> window 0x200 -> `cap 3 click <OK ctrl2>` | `0x50 0B`; Bob disappears from the Mentor tab immediately (client-side) | state_change |
| 36 | 0x44 | 6.1 #22 | HUD Msg -> memo window 0x3FE -> close (ctrl 2) -> confirm -> `cap 3 click <OK>` | `0x44 0B`; memo icon off, list cleared | safe |
| 37 | 0x6D | 6.1 #23 + rclick | Right-click Bob -> popup ctrl 10 -> window 0x435 -> `cap 3 click <confirm ctrl10>` | `0x6D 21B 02 00 00 00 42 6F 62 ...` | safe |
| 38 | 0x73 | at Frenaiga (map 201 (1600,1100) etc.; walk 5 portals or add a debug start map, since the server ignores `char['map']`: line 733 uses map 101); gold >= 50000 (0x03 sends 100000) | Talk to Frenaiga (`hold space 350`) -> window 0x47B -> `cap 3 click <OK>` | `0x73 0B`; no client-side gold change until 0x9D | safe |
| 39 | 0x4B | Note item in the bag: `send 6F 01 00 00 00 00 01 00 00 00 66 07 00 00 01 00` + 16x `00` + `00 00` (33 bytes: count 1, serial 1, item 1894, qty 1). **0x6F purges and replaces all cash items** | Use Note -> window 0x3FD: recipient `Bob`, contents `hi` -> `cap 3 click <send ctrl3>`; then dismiss the modal with `send 77 01 01 00 00 00` (or `send 77 00`) | `0x4B 110B 66 07 42 6F 62 00(x14) 68 69 00(x89)`; after 0x77 result 1: "You successfully sent the message." and the note count decremented | **disruptive** (modal until 0x77) |

### 6.3 Two-client end-to-end tests (after items 2-12)

Setup: client 1 `test`/TestHero (uid 1); client 2 `WindSlayer_p2.exe` logged in as `admin`/`test`
(uid 2, class 1). Drive client 2 with `wsview.py --client 2 ...`.

| # | Flow | Steps | Expected | Risk |
|---|---|---|---|---|
| 40 | F2 add friend | C1 Add `test` -> C2 dialog 0x17C -> Accept | C1 "test has been registered..."; C2 "TestHero has been registered..."; both lists "(1/20)" online; accounts.json has friends on both chars | state_change |
| 41 | F4 presence | C2 exits the game | C1 list greys test (no message). Relaunch C2 + login -> C1 "<test> has logged in. (Friend)". C2 status Busy -> C1 icon changes | state_change |
| 42 | F4.4 status reset | C2 sets Busy, walks a portal, then selects Online | C2 sends 0x37 00 after the portal only if the server reset status; C1 icon returns to online | safe |
| 43 | F5 chat room | C1 Invite test -> C2 accept -> both type -> C2 closes the room | Both room windows open; each line appears in both (sender included); C1 "test has logged out.." | safe |
| 44 | F5.9 implicit leave | In a room, C2 walks a portal | C1 receives 0x62 test; C2's later lines are not relayed (C2 has no room) | safe |
| 45 | F6/F8 mentor | TestHero Add Mentor `test` | C1 "<test>is registered as your mentor."; C2 "<TestHero> has logged in. (Menti)". C1 portal -> mentor persists (0x03 block). C2 relog -> C1 "<test> has logged in.(Mentor)" | state_change |
| 46 | F9 memo | C1 note to `test` (item via 0x6F injection as in #39) | C1 "You successfully sent the message."; C2 memo icon lights; C2 closes the memo window -> accounts.json memos emptied | state_change |
| 47 | F10 compliment | Visibility in place; C1 right-clicks test -> compliment; retry | C1 "<test> has been complimented"; C2 "<TestHero> added 1 of your manner points."; second try C1 "only once a day"; manner persists across relog (0x02) | state_change |
| 48 | F12 expansion | C1 at Frenaiga, OK | "(n/25)", gold -50,000 consistent with the server wallet | state_change |

---

## 7. Open questions

1. **Directed vs mutual lists:** F2 adds both sides on accept. Should delete (C2S 0x32) also remove the other side? S2C 0x0E always pops a modal, so a silent mutual removal is impossible. The doc proposes one-sided delete.
2. **Status values 2 and 3:**
   - Which icons do they draw (6.1 #3)?
   - Should Busy/AFK auto-refuse invites (0x0F code 4) or friend requests (0x0C code 4)?
   - Presence 0 re-prints "has logged in" when a friend returns to Online. Is that acceptable, or should the server skip 0x60 for 2/3 -> 0 transitions (leaving a stale icon)?
3. **0x0F subtype 1 member list:**
   - Does the list include the receiver?
   - Which order gives the right "<name> has entered." line on each side?
   - Does closing and reopening window 0x177 clear the member list (FUN_00472790 frees it on hide)?
4. **0x0C result 1 vs 0x0B:** the client treats them identically. The doc assumes 1 goes to the requester and 0x0B to the accepter.
5. **Default friend capacity (20) and the real slot price:** the client only gates on >= 50,000. The price in UI 8990 is filled in at runtime from an unknown source.
6. **Notes:**
   - Does the gift-reply path (C2S 0x4B with item_id 9999 from S2C 0x6D) expect S2C 0x77?
   - How does item_id map to the premium `cash_item_serial` (premium_cash group)?
   - Should notes to non-existent names fail, or notes to offline names be stored? The item text says offline friends are supported.
7. **Mentor rules:**
   - Mentee limit per mentor.
   - EXP bonus amounts (UI 2672/8970).
   - Automatic graduation when a mentee class-upgrades.
   - Whether 0x66 "can not be found" was also used for an offline mentor.
8. **Opening the messenger window 0x176:**
   - Which key or HUD element opens it? `FUN_004295e0` puts 0x176 at index 7 of the 13-entry window table at game_state+0x1088.
   - Live-tested letters (Q, I, K, C, G, M, O, U, H, L) opened client-local windows, but none were identified as 0x176.
   - UI 8064 says the Char Info window shows "messenger".
9. **Compliments:** delta amount, daily received cap (code 5), and whether bystanders originally got 0x96 or 0x97.
10. **C2S 0x32 in the corpus:** needs a proper spec entry (grammar derived in 1.3 from asm 0x473004-0x47303D and FUN_0046ffe0). Confirm with capture #28.
11. **Short-packet reads:** does Fireway `GetDataFromPacket(char*,N)` bounds-check N against the remaining length? This decides how dangerous the 4.2-1/4.2-2 truncated packets are (garbage vs over-read).
