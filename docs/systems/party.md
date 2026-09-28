# Party system: server design (EN 2008 client)

Group: `party`. Spec source: `re_tools/corpus/systems/party.json` (11 specs). Cross-system lookups: `corpus/protocol_spec.json`, client decomp `corpus/decomp/*.c`, asm `corpus/asm/*.asm`.
Server: `WindSlayer2Game/server/windslayer_server.py` (line numbers below are for the 2793-line version read on 2026-09-17). Codec: `server/wsproto.py`.

| Opcode | Dir | Name | Grammar | Spec confidence | Server today |
|---|---|---|---|---|---|
| 0x27 | C2S | PartyInvite | `u32 target_uid` | high | not dispatched |
| 0x28 | C2S | PartyInviteAccept | `str[17] inviter_name` | medium (inferred from 0x4E/0x4F adjacency) | not dispatched |
| 0x29 | C2S | PartyBreak | empty | high | not dispatched |
| 0x6A | C2S | PartyChat | `u8 text_len; bytes[text_len] text` | high | not dispatched |
| 0x4E | S2C | PartyInviteReceived | `str[17] inviter_name` | high | missing |
| 0x4F | S2C | PartyMemberAdd | `str[17] member_name; u32 member_uid; u16 hp_max; u16 hp_cur; [stop if hp_max==0]; u16 mp_max; u16 mp_cur; [stop if mp_max==0]` (the client reads nothing if all 4 frames are taken) | high | missing |
| 0x50 | S2C | PartyInviteFailAlreadyInParty | empty | high | missing |
| 0x51 | S2C | PartyMemberRemove | `u32 member_uid` | high | missing |
| 0x54 | S2C | EntityHpUpdate (party frame HP) | `u32 uid; u16 max_hp; u16 cur_hp` | high | builder only (`_build_opcode_54` :1933), never sent |
| 0x55 | S2C | EntityMpUpdate (party frame MP) | `u32 uid; u16 max_mp; u16 cur_mp` | high | missing |
| 0x56 | S2C | PartyInviteFailLevelGap | empty | high | missing |

Related opcodes from other groups that this design uses: 0x02 (login uid), 0x04/0x05/0x06/0x07 (player presence), 0x0D/0x1B/0x2A/0x9F (movement relay), 0x15 (system line), 0x22 (SetLevel), 0x2B/0x40 (refuse_party flag), 0x53 (TargetNotOnServer), 0x90 (orange chat line), 0x92/0x3B/0x25 (party heal and auras).

All 11 party grammars were run through `wsproto.Grammar` (corpus copy and `server/protocol_spec.json` copy are identical). They encode and decode cleanly. Example: `0x4F {"member_name":"Bob","member_uid":2,"hp_max":100,"hp_cur":80,"mp_max":50,"mp_cur":50}` gives 29 bytes, and it decodes back to the same values.

---

## 1. How the client implements parties

The EN client has no party leader, no party id and no party name. The party exists only as UI state and a few scene fields:

- **Member frames: UI windows 0x75, 0x76, 0x77, 0x78.** These are four HUD frames with a name (control 3), an HP text and gauge (control 1) and an MP text and gauge (control 2). A frame is "bound" when `frame+0x134 = member uid`. S2C 0x4F binds the first free frame and opens it. S2C 0x51 closes a frame, which clears `+0x134`. Frames hold **other** members only: 0x4F never flags the local entity, and 0x51 with your own uid clears all frames. So the maximum party size is **5** (you plus 4 frames). The spec still lists this as an open question.
- **Scene party uid array `scene+0x25C..+0x26C`** has 5 u32 slots. 0x4F fills the first zero slot. 0x51 zeroes matching slots, or all slots for the self-uid form. `RegisterLocalPlayer` (0x422120) walks this array whenever any player entity is registered (0x04/0x05/0x07). If the entity's uid is in the array it sets `entity+0x13A0 = 1` (party-member flag, which gives a longer nameplate draw range). It also recomputes the party average level at `scene+0x96D`. **So a member who arrives in your scene later is flagged automatically. No 0x4F resend is needed.**
- **"In a party" test:** `FUN_004837c0(g_ui_root, 0x75)` returns window 0x75's state (`+0x3C`). The value 1 (closed or missing) means "not in a party". Only frame **0x75** is checked. Party chat (C2S 0x6A, "Only possible when you are in a party."), the party chat tab (0x448AF4) and the arena/play-room/battlefield gates `FUN_00444400/FUN_00444590` ("Unable to do during the party play.") all use this test.
- **0x51 does not compact frames.** Its compaction loop is dead code: it compares the window id, which is at least 0x75, with 3. When the member in frame 0x75 leaves while a member in 0x76 stays, the client thinks it is not in a party. **The server must repack frames** (see F4).
- **Player context menu, window 0x50.** `FUN_0044C4B0` opens it on WM_LBUTTONDBLCLK (0x203) or WM_RBUTTONUP (0x205). Both apply only in field mode (`scene+0xF00 == 6`), on a **remote player entity** (`+0x98 == 3`, uid != `scene+0x220`), with the cursor within about 51 px (`d² < 0xA28`) of the entity anchor. The client stores the uid in `window0x50+0x134` and labels control 5 "Make Party" or "Break Party" using the frame-uid loop. Clicking a party frame (FUN_00446300 case 0x75..0x78) opens the same menu with that frame's uid. That is how you reach "Break Party" for a member on another map.
- **Control 5 click (FUN_00446300 @0x44A4C0).** First the gates: `FUN_00444700` blocks in an arena or play room, and battlefield standby (window 0x17B state 3) is blocked. Then the client counts bound frames and compares each one with the target uid:
  - If the target is in a frame, it sends **0x29** (empty).
  - If 4 or more frames are bound, it prints "The party is full" and sends nothing.
  - Otherwise it prints "You requested <name> to join the party." and sends **0x27 u32 uid**.
- **Invite dialog, window 0x71.** S2C 0x4E opens it and puts the inviter name in control 1. Control 3 (Accept) sends **0x28 str[17]** with that name. Decline sends nothing.
- **Party chat.** You type `/p msg` (or `/ㅁ`-jamo) or type in party chat mode 2. The client formats `"%s : %s"` (own name, message) into 88 bytes, so the line is at most 87 characters. It checks party membership (frame 0x75) and the 700 ms anti-spam timer, then sends **0x6A u8 len + raw bytes**. There is **no local echo**. The display opcode is not proven. The design uses **S2C 0x90** (orange, `u8 len + text`, 88-byte buffer, at most 87 characters), which matches the C2S frame one-for-one. See the open questions.
- **Refuse option.** `RefuseParty` is a registry value that the Options window checkbox 0x12 sets. The client sends it as the 3rd byte of C2S 0x2B EnterWorld and of C2S 0x40 SetPrivacyRefuseOptions. The EN client has **no** "refusing party" message; that is KR 0x56 subtype 3.
- **Frame refreshers on other packets.** 0x14 (stat 2/3), 0x22 SetLevel, 0x76, 0x92 PartySkillHpEffect, 0x06 despawn and 0x3B/0x43 auras all update frames through `FUN_0043d3f0` / `FUN_0043d4e0`. 0x92 and the 0x25 skill range 0xAD8-0xAE2 heal every uid in `scene+0x25C..`. Aura buffs 0x8E7-0x912 and 0xAE3-0xAF8 fan out over the same array. These are owned by the skill group, and they need the server's party membership.

---

## 2. Request/response flows

Conventions: A = inviter, B = invitee. `uid(X)` = X's session uid (after multi-session fix M1 this equals account_id). `name(X)` = character name, at most 16 bytes, NUL-padded to 17. All sends use the **target** session's encoding mode (live clients send everything with `no_enc=True`, see server_live.log).

### F1. Invite: C2S 0x27
Client side, before sending (A): A must be in field mode, not in an arena, play room or battlefield standby, and B must be a remote entity in A's scene (needs 0x04/0x05/0x07 presence). A needs fewer than 4 bound frames, and B must not be in a frame.
1. A → **C2S 0x27** `target_uid`.
2. Server `_handle_party_invite` takes `world_lock` and runs these checks in order:
   1. A is not in world, or payload < 4 B → drop.
   2. `target_uid == uid(A)` or B is not an in-world session → **S2C 0x15** `{msg_type:0, text:"That player is not online."}` to A. There is no EN opcode keyed by uid; 0x53 needs a name.
   3. `B.refuse['party']` → **S2C 0x15** `"<B> is refusing party invitations."` to A (server wording), no 0x4E.
   4. B is already in a party → **S2C 0x50** (empty) to A ("The player is already in a party."). If B is in A's own party (a stale client), resync A's HUD instead (F6 step 3).
   5. A's party has `PARTY_MAX` (5) members → **S2C 0x15** `"The party is full"` to A (the client normally blocks this first).
   6. Level gap: `max(levels of A's party ∪ {B}) - min(...) > 10` → **S2C 0x56** (empty) to A ("...must be within 10.").
   7. Otherwise record `B.party_invites[name(A)] = (uid(A), time.monotonic() + INVITE_TTL)`. A repeat invite from A refreshes it. Send **S2C 0x4E** `{inviter_name: name(A)}` to B. A gets no confirmation; the client already printed "You requested...".

### F2. Accept: C2S 0x28 → join
1. B clicks Accept in window 0x71 → **C2S 0x28** `inviter_name`.
2. Server `_handle_party_accept` (under `world_lock`):
   1. Pop `B.party_invites[inviter_name]`. The match is exact and case-sensitive on the NUL-stripped name, as echoed. If there is no entry or it has expired → **S2C 0x15** `"The invitation has expired."` to B, stop.
   2. A (from the stored uid) is no longer in world → **S2C 0x53** `{char_name: inviter_name}` to B ("<name>is not in server."), stop.
   3. B is in a party now → **S2C 0x15** `"You are already in a party."` to B, stop.
   4. Repeat the full-party check and the level-gap check (levels may have changed). On a gap send **S2C 0x56** to B and to A. On full send **S2C 0x15** `"The party is full"` to B.
   5. If A has no party, create `Party(pid, members=[uid(A)])`. Append `uid(B)` and set `B.party_id = pid`. Delete every other pending invite of B.
   6. HUD fan-out through `_hud_add(viewer, member)`, which sends 0x4F and updates the viewer's HUD mirror:
      - To **B**: one **S2C 0x4F** per existing member M, in join order. Fields: `member_name=name(M)`, `member_uid=uid(M)`, `hp_max=max(1,M.max_hp)`, `hp_cur=M.hp`, `mp_max=max(1,M.max_mp)`, `mp_cur=M.mp`.
      - To **each existing member M**: one **S2C 0x4F** describing B.
      - Never send 0x4F about the receiver itself. Never send a uid the receiver's mirror already holds (client hazard 2: duplicates use up frames). Never send `hp_max`/`mp_max` = 0 (hazard 1: a hidden, bound frame that can never be freed).
   7. Log `[PARTY] pid=.. join uid=.. members=[..]`.

### F3. Decline or ignore
The client sends nothing. The server lets `party_invites` entries expire (`INVITE_TTL = 60 s`, purged on access and on disconnect). The client dialog stays open until the player clicks. A late Accept gets the "expired" 0x15 line.

### F4. Leave / Break Party: C2S 0x29
Client side: sent from the context menu of a party member (right-click in the scene, or a click on a frame). It is blocked in an arena or play room and during battlefield standby. The packet carries no target, so it always means **"the sender leaves"**. There is no kick in the EN UI.
1. X → **C2S 0x29** (0 bytes).
2. Server `_party_remove(X, reason='leave')` (under `world_lock`). If X has no party, drop.
3. Remove `uid(X)` from `party.members`. Set `X.party_id = None`. Send X **S2C 0x51** `{member_uid: uid(X)}`. This is the self form: the client closes all 4 frames and clears `scene+0x25C..`. Reset X's HUD mirror to `[0,0,0,0]`.
4. If exactly 1 member R remains: dissolve. Send R **S2C 0x51** `{member_uid: uid(R)}` (self form), reset R's mirror and delete the party.
5. Otherwise, for each remaining member R:
   - Send **S2C 0x51** `{member_uid: uid(X)}` and clear that uid in R's mirror.
   - **Compaction rule:** if `R.hud[0] == 0` and any other mirror slot is non-zero, rebuild R's HUD. Send **S2C 0x51** `{member_uid: uid(R)}` (clear all), then one **S2C 0x4F** per other member in join order. Without this, frame 0x75 is closed and R's client treats R as partyless: /p is blocked, and the arena gates change.
6. The same routine handles disconnect (F5).

### F5. Disconnect or logout
Hook it in `_handle_fireway`'s `finally` (:578-581) before `del self.sessions[addr]`:
1. `_party_remove(session, reason='disconnect')`. Skip the self 0x51, because the socket is gone.
2. Drop every pending invite **from** this uid held by other sessions, and this session's own invite table.
3. The presence layer (M4) broadcasts **S2C 0x06** `{uid}` to players on the same map.

### F6. Map change and re-entry while in a party
1. `_handle_change_map` (:1244) sends 0x08 + 0x03 + 0x07. Both 0x08 and 0x03 destroy all entities, which clears every `+0x13A0` flag. The member entities that the presence layer re-sends in the new 0x07 list, or later through 0x05, get re-flagged by `RegisterLocalPlayer` from `scene+0x25C..`.
2. The spec does not show 0x08 or 0x03 closing windows 0x75..0x78 or zeroing `scene+0x25C` (0x08 closes 0x2A, 0x480, 0x4D, 0x47A, 0x47E, 0x4B plus an untraced batch through FUN_00483470). **Assume the HUD persists, and verify it live (test T-MAP).**
3. If the live test shows frames closing: after the 0x07 and the 0x28/0x44 in `_handle_change_map` (:1302-1315), call `_hud_rebuild(session)`. That is 0x51 self, then 0x4F for each other member. Clear the mirror first.
4. Parties are **session-scoped**. A relog starts partyless, because F5 removed the member.
5. `_handle_change_map` refills HP/MP (:1312-1315). Fan out 0x54/0x55 (F7).

### F7. HP/MP/level sync to party frames
Whenever a member X's HP or MP (current or max) changes, run `_party_push_vitals(X)`: for each other member R send **S2C 0x54** `{uid:uid(X), max_hp:max(1,X.max_hp), cur_hp:X.hp}` and **S2C 0x55** `{uid:uid(X), max_mp:max(1,X.max_mp), cur_mp:X.mp}`.
- Never send these to X itself. On the local uid they write the entity fields but not the HUD; X's own HUD uses 0x28/0x44.
- They work across maps. The entity write is skipped when the uid is not in R's scene, but the frame still updates when `max != 0`.
- Call sites today:
  - enter-world (:779-782)
  - `_handle_use_item` (:1228-1231)
  - `_handle_change_map` refill (:1312-1315)
  - level-up in `_send_exp` (:2010-2013). The client recomputes max HP/MP on 0x22 and does a full heal, so the server must update `session['max_hp'/'max_mp'/'hp'/'mp']` to the same values first.
  - Future: damage taken, regen, death (0x29/0x3E), revive (C2S 0x2E).
- Level-up: also send **S2C 0x22** `{uid:uid(X), level}` to each member R that has X in its scene (same map). It sets the level and refreshes R's frame from the entity. For off-scene R, 0x54/0x55 carry the new max.
- Coalesce: at most one 0x54/0x55 pair per member per 250 ms. Remember that per-hit packets caused lag before (0x40).

### F8. Party chat: C2S 0x6A → S2C 0x90
Client gates: frame 0x75 open, 700 ms anti-spam, curse filter, manner > -40.
1. X → **C2S 0x6A** `text_len, text`. Example: "TestHero : hello" gives `6A 10 54 65 73 74 48 65 72 6F 20 3A 20 68 65 6C 6C 6F`.
2. Server `_handle_party_chat`:
   1. Decode the grammar and cut the text at the first NUL. If X has no party → drop (optionally 0x15 "Only possible when you are in a party.").
   2. Server-side anti-spam: ignore a message within 700 ms of the previous one.
   3. **Do not trust the embedded name** (it is client-controlled). Take `msg = text.split(b' : ', 1)[1]` if the separator exists, else the whole text. Rebuild `line = name(X) + " : " + msg` and truncate to **87** bytes.
   4. Send **S2C 0x90** `{text_len: len(line), text: line}` to **every member including X**, on any map.
   5. Log the line.
3. Fallback if the T-CHAT live test rejects 0x90: S2C 0x16 `{sender_name:name(X), text_len, text:msg[:60]}`. That shows a white "Name : msg" line, and it also pops a speech bubble over X's entity in same-map viewers. That fallback depends on the chat group's 0x16 fix (drop the leading count byte, :954/:1540).

### F9. Refuse-party option
1. **C2S 0x2B** EnterWorld: `refuse_whisper, refuse_exchange, refuse_party, refuse_talk, refuse_friend` are payload[0..4]. Today `_handle_enter_world` ignores them (:700-705). Store them in `session['refuse']`.
2. **C2S 0x40** SetPrivacyRefuseOptions (5 u8, same order) → update `session['refuse']`. There is no reply. `_dispatch` has no branch for it yet.
3. F1 step 2.3 enforces the flag.

### F10. Leaving through the frame
Clicking frame 0x75..0x78 opens menu 0x50 for that uid. "Break Party" → C2S 0x29 → F4. Control 3 (info) for an off-scene member sends C2S 0x2A str[17]; that belongs to the character group and the reply is 0x52/0x53.

---

## 3. State and data model

### 3.1 Multi-session prerequisites (required before any real party test)
| # | Need | Where / why |
|---|---|---|
| M1 | **Per-account uid.** Persist `account['account_id']`: `test`→1 (keeps the wsdev `uid==1` in-world probe working), `admin`→2, new accounts get max+1. `_handle_login` must set `session['account_id'] = session['uid'] = account['account_id']` (today it is hardcoded `1` at :2656). `_build_en_opcode_07` must use `session['uid']`, not `char.get('uid', 1)` (:2307). `_handle_change_map` (:1280) and `_send_exp` (:2011) must use `session['uid']`. The uid must equal the 0x02 `account_id`, because `scene+0x220` comes from 0x02 and the registration gate compares entity uid with it. All characters of an account therefore share the account uid. | Two sessions with uid 1: each client's 0x05/0x07 record for the other player passes `uid == scene+0x220`, so `RegisterLocalPlayer` overwrites the local-player pointer (0x05 spec hazard 2). Party routing by uid is impossible. |
| M2 | **Single login per account.** A second login for an online account → 0x02 `result=4` ("Connection already exists...") or kick the old session. | Otherwise two sessions share a uid. |
| M3 | **In-world registry + cross-session send.** Set `session['in_world']=True` after the 0x07 in `_handle_enter_world`. Keep `self.sessions_by_uid` and a name lookup. Store `session['no_enc']` (last flag seen in `_handle_fireway` :557). Add `self._send_to(target, opcode, payload)` = `_send_encrypted(target['sock'], target, opcode, payload, use_by_array=target.get('no_enc', True))`. Per-session `send_lock` and `send_seq` already exist (:585-588). Add `self.world_lock = threading.RLock()` around party and presence mutations, because each connection runs on its own thread. | Party packets always go to *other* sessions. |
| M4 | **Presence.** On enter-world and map change, send 0x07 = [self + other in-world sessions on `current_map`]. Send **0x05** (single record) to those others. On map leave or disconnect, send **0x06** `{uid}` to the old map's players. Never send a receiver its own uid in 0x04/0x05/0x06. | The party UI (context menu 0x50) only opens on a remote entity with state 3 in the scene. |
| M5 | **Remote-safe player record.** Build the 0x05/0x07 record from the spec grammar (`0x05` / `0x07`) with **real `cur_hp`/`cur_mp`** (today 0 at :2391-2392, so remote players render dead) and idle motion defaults: `anim_substate_8cf=8, action_state_904=8, direction_8bd=2, input_state_8b3=8`, the rest 0. Today the builder writes `+0x954=32, +0x8CF=1, +0x904=0, +0xE00=501` (:2367-2372) and IP octets into the input-state bytes (:2378-2387). | Spec 0x07 references (4) and (3). |
| M6 | **Movement relay** (movement group): C2S 0x0D → S2C 0x2A/0x9F (or 0x1B) to same-map others. Not strictly needed for party: a frozen remote entity can still be right-clicked. | Visual only. |
| M7 | **Name length.** Cap every str[17] name at 16 bytes before encoding. `wsproto` `str[N]` encode is `raw[:size].ljust(size)` (wsproto.py:285), so a 17-byte name would have no NUL. 0x4E and 0x4F copy names with unbounded strcpy on the client. Character creation does not cap name length (:2590). | Client stack overflow. |

### 3.2 Party state (in memory only; nothing persisted)
```python
PARTY_MAX = 5            # self + 4 frames (0x75..0x78)
PARTY_LEVEL_GAP = 10     # 0x56 text
INVITE_TTL = 60.0        # seconds; server policy
PARTY_CHAT_MAX = 87      # 0x90 / 0x6A 88-byte buffers

@dataclass
class Party:
    pid: int
    members: list          # uids in join order (HUD order)

GameServer:
    world_lock: threading.RLock
    sessions_by_uid: dict[int, dict]     # in-world only
    parties: dict[int, Party]
    next_party_id: int

session (new keys):
    uid: int                              # == account_id (M1)
    in_world: bool; no_enc: bool
    level: int                            # init from char['level'] at enter-world (today only _send_exp sets it)
    hp, max_hp, mp, max_mp                # already set at :779-780
    party_id: int | None
    party_invites: dict[str, tuple[int, float]]   # inviter_name -> (inviter_uid, expires_at)
    party_hud: list[int]                  # 4-slot mirror of frames 0x75..0x78 on THIS client
    refuse: dict[str, int]                # whisper/exchange/party/talk/friend from 0x2B / 0x40
    last_party_chat: float
```
- **gamedef.sqlite3:** the party logic needs no content tables. Levels come from the session or `accounts.json` `characters[].level`. Party-skill ids (0xAD8-0xAE2 heal, 0x8E7-0x912/0xAE3-0xAF8 auras) belong to the skill group.
- **accounts.json:** add a persistent `account_id` per account (M1). Party membership is not persisted.
- **Max HP/MP caveat:** `session['max_hp'] = char['hp']` (:779) is a saved current value, not the client-computed max (`FUN_00427d40` from job, level and stats). Frames will show the server's number. This is cosmetic until the stat system computes the real max.

### 3.3 Packet building from spec grammars
```python
from wsproto import Grammar
SPEC = {s['key']: s for s in json.load(open(SPEC_PATH, encoding='utf-8'))['specs']}
_G = {}
def _grammar(key):            # S2C keys '0x4F'; C2S keys '0x4484CC/0x27', '0x445BD9/0x6A'
    if key not in _G: _G[key] = Grammar(SPEC[key]['grammar'])
    return _G[key]
def _pkt(key, **f): return _grammar(key).encode(f)
# e.g. self._send_to(r, 0x4F, _pkt('0x4F', member_name=nm16, member_uid=u, hp_max=hm, hp_cur=h, mp_max=mm, mp_cur=m))
#      uid = _grammar('0x4484CC/0x27').decode(payload)['target_uid']
```
`_dispatch` additions (after :653): `0x27 → _handle_party_invite`, `0x28 → _handle_party_accept`, `0x29 → _handle_party_leave`, `0x6A → _handle_party_chat`, `0x40 → _handle_privacy_options`. None of these clash with existing C2S branches.

---

## 4. Current implementation status and proven bugs

**Status:** no party logic exists. The only related artifacts are `_build_opcode_54` (:1933-1941), which is wire-correct but never sent (its caller `_send_hit_feedback` :1972 has no callers), and a correct comment at `_resolve_hit` :1902-1907 saying the HP-bar widget pool 0x75.. is party-only.

| # | Bug (proof) | Severity |
|---|---|---|
| B1 | `_dispatch` (:601-667) has no branch for C2S 0x27/0x28/0x29/0x6A (or 0x40). They fall to "Unhandled opcode" (:659-667), so invites, accepts, leaves and party chat are silently dropped. Spec status is `missing` for all four. | wrong_behavior |
| B2 | `session['account_id'] = 1` for every login (:2656). The 0x07 record uses `char.get('uid', 1)` (:2307). Change-map uses `session.get('account_id',1)` (:1280). `_send_exp` 0x22 uses `ch.get('uid',1)` (:2011). With two clients, both players are uid 1. Any presence record for the other player would register as the receiver's local player (0x05/0x07 specs: gate `uid == scene+0x220`), and party routing by uid cannot tell the players apart. | crash_or_desync |
| B3 | No player presence: there are no 0x04/0x05/0x06 builders, and `_handle_world_sync` (:669-684) swallows 0x0D. Menu 0x50 only opens on a remote state-3 entity (`FUN_0044C4B0`), so **C2S 0x27 cannot be produced in normal play**. | wrong_behavior |
| B4 | `_build_en_opcode_07` is unsafe as a remote record: `cur_hp/cur_mp = 0` (:2391-2392, "cur_hp=0 renders the remote player dead"), IP octets written into input-state bytes `+0x8B3..+0x8B6` (:2378-2387), and non-idle motion constants (:2367-2372). This blocks reusing it for M4. | wrong_behavior |
| B5 | Session teardown (:578-581) only deletes the session. A disconnected member would stay in other clients' frames, pending invites would leak, and no 0x06 despawn is sent. | wrong_behavior |
| B6 | Refuse flags are ignored. `_handle_enter_world` treats 0x2B payload[0..4] as padding (:688-705; spec 0x2B: bytes 0-4 are refuse_whisper/exchange/party/talk/friend). C2S 0x40 is not dispatched. | wrong_behavior |
| B7 | HP/MP/level changes are never reflected to party frames: `_handle_use_item` sends only 0x28/0x44 to self (:1228-1231), the `_handle_change_map` refill likewise (:1312-1315), and `_send_exp` sends 0x22 only to the owner (:2010-2013). No 0x55 builder exists. | cosmetic |
| B8 | The `_admin_listener` injector sends every packet to **all** sessions with a socket, including ones not in world (:459-461). A two-client test cannot target one client. Party S2C such as 0x51 read `scene+0x220`, so injecting before the scene exists is unsafe. | wrong_behavior |
| B9 | Combat is single-client: `_find_client_pid` matches only `windslayer_patched.exe` (:1673, so WindSlayer_p2.exe is never attached), the driver finds the player by uid==1 (:1774-1783), and `_memory_melee` applies a swing to **every** session's monster list (:1681-1698). With two clients in world, client A's swing damages session B's mobs and sends B 0x29/0x21/0x18. This blocks party hunting tests. | wrong_behavior |
| B10 | Names are not length-capped (create char :2590), and `wsproto` str encode does not force a NUL (wsproto.py:285). A 17-byte name in 0x4E/0x4F overflows the client's strcpy (0x4E/0x4F hazard 4). | crash_or_desync (latent) |
| B11 | `session['max_hp']/['max_mp']` are taken from the saved character hp/mp (:779-780), not the client-computed max, so 0x4F/0x54/0x55 would show the wrong max. `session['level']` is only set by `_send_exp` (:1998-2000). | cosmetic |

---

## 5. Implementation plan

| id | Title | Pri | Effort | Depends on | Opcodes |
|---|---|---|---|---|---|
| party-mp-identity | Per-account persistent uid (= 0x02 account_id), single login per account | P1 | M | none | 0x02, 0x07, 0x2B |
| party-mp-registry | In-world session registry, `no_enc` per session, `_send_to`, `world_lock`, disconnect hook | P1 | S | party-mp-identity | none |
| party-spec-codec | Load protocol_spec.json grammars in the server; `_pkt`/`_grammar` helpers; 16-byte name cap | P1 | S | none | 0x4E, 0x4F, 0x51, 0x54, 0x55, 0x90 |
| party-mp-remote-record | Grammar-built 0x05/0x07 player record with real HP/MP and idle motion defaults (fixes B4) | P1 | M | party-spec-codec, party-mp-identity | 0x04, 0x05, 0x07 |
| party-mp-presence | Same-map presence: 0x07 list on enter/map change, 0x05 to others, 0x06 on leave/disconnect | P1 | M | party-mp-registry, party-mp-remote-record | 0x05, 0x06, 0x07 |
| party-test-harness | Admin injector `target` (uid/name) field; wsview `rclick`/`dblclick` + `--pid`; wsdev in-world probe by own uid; p2 login | P1 | S | party-mp-identity | none |
| party-core-state | Party model, constants, `_party_of`, HUD mirror helpers `_hud_add/_hud_remove/_hud_rebuild` | P1 | S | party-mp-registry, party-spec-codec | 0x4F, 0x51 |
| party-invite | C2S 0x27 handler: validations → 0x4E / 0x50 / 0x56 / 0x15; pending invites with TTL | P1 | M | party-core-state, party-privacy | 0x27, 0x4E, 0x50, 0x56, 0x15 |
| party-accept-join | C2S 0x28 handler: invite match, re-validation, create/extend party, 0x4F fan-out, 0x53 when the inviter is offline | P1 | M | party-invite | 0x28, 0x4F, 0x53, 0x56 |
| party-leave | C2S 0x29 + disconnect: 0x51 self/other, frame compaction rebuild, dissolve at 1 member | P1 | M | party-accept-join | 0x29, 0x51, 0x4F |
| party-chat | C2S 0x6A → S2C 0x90 to all members incl. sender; name rebuild, 87-byte cap, 700 ms throttle | P2 | S | party-accept-join | 0x6A, 0x90 |
| party-privacy | Parse refuse flags from 0x2B, dispatch C2S 0x40 | P2 | S | party-mp-registry | 0x2B, 0x40 |
| party-vitals-sync | 0x54/0x55 fan-out on HP/MP changes (use item, map refill, level-up); 0x22 to same-map members; 250 ms coalescing | P2 | S | party-accept-join | 0x54, 0x55, 0x22 |
| party-map-change-hud | Verify HUD persistence across 0x08/0x03; rebuild the HUD after the 0x07 replay if needed | P2 | S | party-accept-join, party-mp-presence | 0x08, 0x03, 0x07, 0x4F, 0x51 |
| party-dep-combat-multiclient | Per-client combat attach (pid ↔ session), hits only on the owning session; shared per-map monsters | P2 | M | party-mp-identity | 0x29, 0x21, 0x18 |
| party-exp-share | Optional EXP split among same-map members (0x21 per member; level-ups via 0x22) | P3 | M | party-accept-join, party-dep-combat-multiclient | 0x21, 0x22 |
| party-skill-hooks | Expose party membership to the skill system for 0x92 party heal and 0x3B aura fan-out | P3 | M | party-accept-join | 0x92, 0x3B, 0x25 |

Suggested order: party-spec-codec → party-mp-identity → party-mp-registry → party-test-harness → party-core-state → party-privacy → party-invite → party-accept-join → party-leave → party-chat → party-vitals-sync. Next come party-mp-remote-record → party-mp-presence, which the UI path needs but which can be developed in parallel because the handlers are testable with injected packets. Then the map-change HUD check, then P3.

---

## 6. Live test plan

Rules (feedback_experiments.md): one change at a time, reproduce the baseline first, and run each experiment at least 3 times before concluding anything. After any injected dummy entity, clean up with `sendspec 06 '{"uid":2}'` or a portal.

### 6.1 Single-client injection tests (possible today, before any server code)
Setup: `python wsdev.py up` (TestHero in map 101). Read the player position with `python wsview.py state`.

| Test | Action | Expected | Risk |
|---|---|---|---|
| T-DUMMY (prereq, S2C 0x05) | `wsdev.py sendspec 05 '{"name":"Dummy","uid":2,"job1":1,"level":1,"rank_emblem":99,"repeat[14]":[{"appearance":0},{"appearance":1},{"appearance":1},{"appearance":0},{"appearance":0},{"appearance":0},{"appearance":0},{"appearance":0},{"appearance":0},{"appearance":0},{"appearance":0},{"appearance":0},{"appearance":0},{"appearance":0}],"attack_dir_8cf":8,"motion_id":8,"facing_dir":2,"pos_x":<px+60>,"pos_y":<py>,"input_dir_8b3":8,"cur_hp":100,"cur_mp":50}'` (367 B) | `wsview state` lists a uid 2 entity with state 3 near the player. The body may be unclothed or invisible (known bug 8), but the entity exists. | state_change |
| T-0x27 | Right-click or double-click Dummy's body (screen position from `wsview shot`; needs wsview rclick/dblclick, or two fast clicks) → menu "Make Party" → click it with `wsdev.py cap 3 click <x> <y>` | Chat shows "You requested <Dummy> to join the party."; capture **C2S 0x27 `02 00 00 00`**; server logs Unhandled 0x27 (today) | safe |
| T-0x4F | `wsdev.py sendspec 4F '{"member_name":"Dummy","member_uid":2,"hp_max":100,"hp_cur":80,"mp_max":50,"mp_cur":25}'` (29 B) | Party frame 1 (0x75) opens: "Dummy", HP 80/100, MP 25/50 gauges | state_change |
| T-0x4F-label | Right-click Dummy again | Menu control 5 now reads "Break Party" | safe |
| T-0x29 | Click "Break Party" (or click frame 1 → menu → Break Party) under `cap 3` | Capture **C2S 0x29** with 0-byte payload; frame stays open (the client waits for the server) | safe |
| T-0x54 | `sendspec 54 '{"uid":2,"max_hp":100,"cur_hp":30}'` | Frame 1 HP text 30/100, gauge 30%. Also: with uid 999 nothing happens; with max_hp 0 nothing happens | safe |
| T-0x55 | `sendspec 55 '{"uid":2,"max_mp":50,"cur_mp":5}'` | Frame 1 MP text 5/50, gauge 10% | safe |
| T-0x6A | With frame 1 open: click the chat bar, type `/p hello`, Enter, under `cap 3` | Capture **C2S 0x6A `10 54 65 73 74 48 65 72 6F 20 3A 20 68 65 6C 6C 6F`** (len 16, "TestHero : hello"); no local line. Without frame 1: popup "Only possible when you are in a party.", no packet | safe |
| T-CHAT (S2C 0x90) | `sendspec 90 '{"text_len":13,"text":"Dummy : hello"}'`; compare with `sendspec 91 '{"text_len":13,"text":"Dummy : hello"}'` and `sendspec 16 '{"sender_name":"Dummy","text_len":5,"text":"hello"}'` | 0x90 gives an orange "Dummy : hello" line in the chat log. Screenshot all three and pick the party-chat display. Check whether the party chat tab filters it | safe |
| T-0x4E | `sendspec 4E '{"inviter_name":"Dummy"}'` (17 B) | Invite dialog 0x71 opens with "Dummy". Screenshot to find the Accept button | state_change |
| T-0x28 | `cap 3 click <accept x> <y>` | Capture **C2S 0x28 `44 75 6D 6D 79` + 12×`00`** (17 B); dialog closes. Also test the other button: expect no packet | safe |
| T-0x50 | `sendspec 50` | System line "The player is already in a party." (salmon color) | safe |
| T-0x56 | `sendspec 56` | System line "The level difference between party members must be within 10." | safe |
| T-0x51-other | With frame 1 bound to uid 2: `sendspec 51 '{"member_uid":2}'` | Frame 1 closes; right-click Dummy shows "Make Party" again | state_change |
| T-0x51-gap (proves the F4 compaction rule) | Inject 0x4F uid 2 "Dummy" then 0x4F uid 3 "Dummy2" (frames 1, 2), then `sendspec 51 '{"member_uid":2}'`, then type `/p hi` | Frame 1 closes and frame 2 stays (no compaction). `/p hi` gives "Only possible when you are in a party." and no 0x6A. If it sends 0x6A anyway, the compaction rule can be dropped | state_change |
| T-0x51-self | With 2 frames bound: `sendspec 51 '{"member_uid":1}'` | All frames close; a following 0x4F uid 2 goes into frame 1 again | state_change |
| T-FULL | Inject 0x4F for uids 2, 3, 4, 5 (4 frames), inject a second dummy (0x05 uid 6), right-click it → Make Party | "The party is full", no 0x27. A 5th injected 0x4F (uid 6) changes nothing (0 bytes read) | state_change |
| T-MAP | With frame 1 bound (uid 2): take the map-101 portal (Down on the sparkle at x≈1404) | After 0x08/0x03/0x07: record whether frame 1 is still visible with the same values and whether `/p hi` still sends 0x6A. This decides party-map-change-hud | disruptive |
| T-SELF (optional) | `sendspec 4F '{"member_name":"TestHero","member_uid":1,"hp_max":100,"hp_cur":100,"mp_max":50,"mp_cur":50}'` | Checks the frames-hold-others assumption (a frame showing self is harmless but wrong). Clean up with 0x51 uid 1 | state_change |
| T-HAZ (do not run by default) | 0x4F with `hp_max:0` | Frame bound but hidden and not freed by 0x51 (hazard 1). Only to confirm the hazard | crash_risk |

### 6.2 Two-client end-to-end tests (after M1-M4 and party handlers)
Setup: server up; client A = `WindSlayer_patched.exe` test/test TestHero (uid 1); client B = `WindSlayer_p2.exe` admin/admin "test" (uid 2); both on map 101 near each other. Use the admin injector `target` and wsview `--pid`.
1. **Presence:** each client's `wsview state` shows the other uid with state 3 and cur_hp > 0; no duplicate local player.
2. **Invite:** A right-clicks B → Make Party → server log `[PARTY] invite 1->2`; B shows dialog 0x71 "TestHero".
3. **Accept:** B clicks Accept → A gets frame "test" (0x4F uid 2) and B gets frame "TestHero" (0x4F uid 1), each with nonzero HP/MP.
4. **Refusals:**
   - A invites B again → 0x50 "already in a party" on A.
   - Set B `level` to 20 in accounts.json → relog → invite → 0x56 on A.
   - B ticks Options "RefuseParty" → A's invite gives the 0x15 refusal line and no dialog on B.
5. **Chat:** A types `/p hi` → both clients show "TestHero : hi" (0x90). Spoof check: a hand-crafted 0x6A with "Other : x" shows "TestHero : x".
6. **Vitals:** B uses an HP potion → A's frame HP updates (0x54). B levels up → A's frame max updates.
7. **Leave:** B → Break Party → B frames cleared (0x51 uid 2 self), A frame cleared (0x51 uid 2 → dissolve → 0x51 uid 1 self).
8. **Compaction:** 3 clients (or 2 clients + injected dummy) → middle member leaves → the remaining clients' frame 1 stays open and /p still works.
9. **Disconnect:** kill client B → A's frame clears and B's entity despawns (0x06).
10. **Map change:** A portals to map 102 → frames persist (or are rebuilt), and B, when it follows, is re-flagged (`entity+0x13A0 == 1` via a memory read).

---

## 7. Open questions

1. **Party chat display opcode.** No EN receive handler formats party chat. 0x90 (orange, u8 len + text, 88-byte buffer) matches C2S 0x6A's 88-byte "Name : msg" format exactly, and 0x91 green is the friend-chat colour. This is unproven; resolve with T-CHAT or a deeper trace of chat tab filtering (window 0x0B line lists).
2. **Maximum party size, 5 or 4?** The design takes the frames as other members only (0x51 self form clears all frames, and 0x4F never flags the local entity), which gives 5. What is the 5th scene slot (`scene+0x26C`) for? T-SELF gives partial evidence.
3. **Do 0x08/0x03 map loads close frames 0x75..0x78 or zero `scene+0x25C`?** The FUN_00483470 batch is untraced. Resolve with T-MAP.
4. **Does 0x29 mean leave or disband?** The client cannot tell them apart. The design uses "sender leaves; dissolve at 1 member". The original server's rule (e.g. whether the first member was a leader) is unknown.
5. **Level-gap rule.** Is it inviter vs invitee only, or across all members? Is it checked on 0x27, on 0x28, or both? Which side received 0x56 in retail?
6. **Response to a refused (RefuseParty) or offline invite.** EN has no string for it (KR 0x56 subtype 3 is gone). Did retail send nothing? The design uses a server-worded 0x15.
7. **Does the Decline button of window 0x71 send anything?** Nothing found in FUN_00446300 case 0x71 (only control 3). T-0x28 checks the other button.
8. **Movement relay opcode** (M6). The C2S 0x0D spec says relay as S2C 0x1B; the 0x2A spec says 0x2A/0x9F. The movement group owns this, and it affects how party members appear to move.
9. **Position of other players for 0x07/0x05 records.** C2S 0x0D carries no x/y except in the interact tail, so the server only knows spawn or portal positions unless it simulates movement.
10. **EXP sharing or party bonus.** No EN packet or string implies one (0x7F is mentor-only). What is `scene+0x96D` (party average level) used for on the client?
11. **Party-skill interplay.** The 0x92 spec asks whether the local uid can appear in `scene+0x25C..`, which would double-heal. With this design the local uid never goes into that array.
12. **Party state after relog.** The design drops membership on disconnect. Retail behaviour (rejoin on reconnect) is unknown and not client-visible.
