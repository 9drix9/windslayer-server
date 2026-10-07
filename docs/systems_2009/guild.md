# Guild system: server design (EN 2009 client, Outspark v1.04 Build 14)

Group: `guild` (plus the Guild Battle sub-system, which depends on the PvP room phases P9/P10).
Client: `WindSlayer2009\WindSlayer.exe` (corpus copy `re_tools/corpus_2009/WindSlayer_2009.exe`, image base 0x400000, no ASLR).
Sources: `corpus_2009/decomp/*.c`, `corpus_2009/asm/*.asm`, `corpus_2009/protocol_spec_2009.json` (keys `0xB3`..`0xBB`, `0x21`, `0x04/0x05/0x07/0x2B/0x2C/0x2E/0x52`, C2S `0x48451D/0x87` .. `0x485FF4/0x9C`), `corpus_2009/dispatch_2009.json`, EN 2009 client data (`hs/windslayer.hii`, `hs/windslayer.hni`, `hs/stage97_02.hmi`, `hs/UILngKo.lng`, decoded in memory only), PySlayer `doc/Windslayers_Full_Packet.c` (KR), `RETAIL_VIDEO_CATALOG_2026-09-24.json`.
Scratch and evidence: `re_tools/docs/systems_2009/_work/guild/` (all `.asm` files below were produced by `gdis.py`, a capstone disassembler that annotates every Fireway read/write with its type; `ui_guild.txt` is the window/control dump with UILngKo text ids).

**Marking convention.** Every claim carries **[V]** (VERIFIED: read in the 2009 asm/decomp, or read from the EN 2009 data files, in this pass) or **[I]** (INFERRED: deduced, taken from retail video, from KR, or a server design choice). "(spec)" means the claim also appears in `protocol_spec_2009.json` and was re-checked.

**Strings.** The EN 2009 client has no numeric string ids for game messages: every message is an immediate `.rdata` literal (for example `MOV EAX,0x5292E4` then `CALL FUN_0049ebc0`). A pointer array at `.data` 0x543FF0..0x54421C lists the same literals, but no code references it [V] (byte scan of `.text` for 0x543E00..0x544400). Messages are therefore quoted as `0xVA "text"`. UI control captions do have ids: the `text` column of `ui_guild.txt` (UILngKo.lng ids, for example 9102 "Expulsion").

**Common helpers:**
- `FUN_0049ebc0(ui, 3, 3, x, 0)` with the text in EAX is a message box. Window 0x16 is the generic "Message" + OK box (`ui_guild` / hui) [V]. `x = 3` is the "Waiting for the server to respond." variant (0x485D99, 0x483965) [V]; that it has no OK button is [I].
- `FUN_00495e30(ui, text, color, 0)` and `FUN_00447bb0(game, text, color)` add chat-log lines [V].
- `FUN_00498050(win)` returns window +0x3C, the state, or 1 when the window is missing [V].
- `FUN_00497af0` / `FUN_00497b20` set / get window +0x134 [V].
- `FUN_00497b50` / `FUN_00497b80` set / get window +0x40 [V].
- `FUN_00497c70(win, 3, 1)` = show and `FUN_00497c70(win, 1, 1)` = hide [I, from the call sites, consistent with state 1 = closed].
- `FUN_0049e070(win, ctrl, state)` sets a control state; 0 = normal and 3 = hidden/disabled [I, from the call sites].

## Opcode summary

| Opcode | Dir | Name | Grammar (short) | Verification | Server today |
|---|---|---|---|---|---|
| 0xB3 | S2C | GuildMessage (36 real sub-codes 1..35, 37, plus 185) | `u8 sub` + per-sub body (section 2) | [V] every sub-code target disassembled | sub 15 result 0 and sub 37 only (`_handle_guild_info`, windslayer_server.py:7766-7790) |
| 0xB4 | S2C | EntityGuildSet | `u32 uid; u16 guild_id; str[17] name; u16 emblem_fg; u16 emblem_bg` (27 B) | [V] (spec) | never sent |
| 0xB5 | S2C | ChatLineGuildColor (guild chat relay) | `u8 len; str[len] text` | [V] handler; use as the relay [I] | never sent |
| 0xB6 | S2C | EntityGuildClear (keeps name bytes) | `u32 uid` | [V] (spec) | never sent |
| 0xB7 | S2C | EntityGuildClear (also blanks the name) | `u32 uid` | [V] (spec) | never sent |
| 0xB8 | S2C | GuildAdBoardRemove | `u16 guild_id` | [V] (spec) | never sent |
| 0xB9 | S2C | **no handler** | - | [V] primary default 0x45DE1A, SubHandler3 default 0x47F973, SubHandler4 default 0x46C57E, SubHandler5 handles only 0x8A/0x8B | must never be sent |
| 0xBA | S2C | GuildAdBoardAdd | 87 B, same field order as C2S 0x88 | [V] (spec) | never sent |
| 0xBB | S2C | GuildAdBoardList | `u8 n` + n x 87 B, **different field order** from 0xBA | [V] (spec) | never sent |
| 0x21 | S2C | ExpDelta, guild tail | `i32 exp_delta; [u32 guild_points]` (tail only if delta > 0 and the receiver's own entity+0x12 > 1) | [V] 0x4591F6..0x459236 | gate table packets.py:695-699; hook `_receiver_guild_id` (windslayer_server.py:7761) returns 0 |
| 0x04/0x05/0x07, 0x2B/0x2C/0x2E, 0x52 | S2C | player records: guild block | `u16 gm_or_guild_id; [bool gm_hidden if ==1]; [str[17] name; u16 fg; u16 bg if >1]` (0x52: name only) | [V] 0x4536A5..0x45371A | always the GM value (records.py:456-468, 567-586) |
| 0x87 | C2S | GuildCreate | `str[17] name; u32 own_uid; u16 fg; u16 bg` (25 B) | [V] 0x4844C1..0x48451D | consumed, no reply |
| 0x88 | C2S | GuildAdBoardPlace | 87 B (spec says 83; the ADD list sums to 87) | [V] 0x482B67..0x482C4D | consumed |
| 0x89 | C2S | GuildJoinRequest | `u32 guild_id` (context menu) **or** `u16 guild_id` (plaza board) | [V] 0x482D35 / 0x482DA4 | consumed |
| 0x8A | C2S | GuildInfoRequest | empty; sent after **every** S2C 0x03 and after sub 19 | [V] 0x453559..0x453581, 0x484550 | replied (sub 15) |
| 0x8B | C2S | GuildApplicationAccept | `str[17] applicant` | [V] 0x484730 | consumed |
| 0x8C | C2S | GuildApplicationReject | empty | [V] 0x4846D0 | consumed |
| 0x8D | C2S | GuildChat | `u16 guild_id; u8 len; bytes[len] "Name : msg"` | [V] 0x47AD00 | consumed |
| 0x8E | C2S | GuildMemberAction | `u16 guild_id; u32 uid; str[17] name; u8 action (1 kick, 2 leave, 3 disband)` (24 B) | [V] 0x484BD0 | consumed |
| 0x8F | C2S | GuildNoticeChange | `str[61] notice` | [V] 0x484C90 | consumed |
| 0x90 | C2S | GuildMaxMemberIncrease | `u16 guild_id; u8 add; u32 gold_cost` (7 B) | [V] 0x483025..0x48306B | consumed |
| 0x91 | C2S | GuildMasterChange | `u16 guild_id; str[17] new_master` | [V] 0x484C40 | consumed |
| 0x92 | C2S | GuildMemberGradeChange | `u16 guild_id; u32 uid; str[17] name; u8 grade` (24 B) | [V] 0x485350 | consumed |
| 0x96..0x9D | C2S | Guild Battle family | section 9 | [V] builders 0x485A90..0x485FF4 | consumed (0x96 would soft-lock, see F14) |
| 0xBC | C2S | RoomHost_GuildBattleResult | dead code | [V] (spec) | warn only |
| 0x0B | C2S | NpcShopBuy from Moiba (menu 0x4BB control 5, send site 0x474327) | `u16 item; u16 qty; u16 npc`; **npc is always 0 on the wire** (1.5, control 5) | [V] asm + live (P15 G1a) | GameServer._buy_guild_board: npc 0 on map 9702 with a board item id means Moiba (`_board_sale_npc`). The cp-2 exe gets EN 4284 for 1,000 gold; the stock exe is refused |
| 0x15 | C2S | UseItem sent by the client on sub 185 | `u16 item_id` (always 0x10B8; **after cp-2: 0x10BC = 4284**) | [V] 0x47E853..0x47E87A | normal use-item path |
| 0x33 | C2S | Messenger conversation invite ("Converse" in the guild tab) | `u32 uid; str[17] name` | [V] FUN_0047b130 | handled (`_handle_room_invite`) |

---

## 1. How the client implements guilds

### 1.1 The guild state lives in the messenger object M

`M = *(game_state+0x508)` [V] (CMessenger, constructor FUN_00479e90). M+0x124 = game_state, M+0x128 = `scene+0x48` ("charinfo"), M+0x12C = UI root, M+0x130 = the game socket copy [V] (FUN_00479e90 sets `+0x128 = [param+0x4F0]+0x48`). So charinfo+0x1C4 = scene+0x20C (own name), charinfo+0x1DC = scene+0x224 (the **local uid** from S2C 0x02), charinfo+0x230 = scene+0x278 (gold, u64), charinfo+0x238 = scene+0x280 (total exp) [V by offset arithmetic]. **Consequence: every "member id" / "master id" on the guild wire is a player uid** (the same value as entity uid; after F3 it is the account uid).

Guild fields in M [V] (sub 3 reader 0x47D77B, reset FUN_0047a8c0, create FUN_00484200):

| Offset | Type | Meaning |
|---|---|---|
| +0x44 | u16 | guild id. 0 = none, **1 is never a guild** (1 is the GM marker on entities), 0xFFFF = "load failed". Everything tests `< 2` for "not in a guild". |
| +0x46 | char[17] | guild name |
| +0x57 | u8 | guild level 1..5, recomputed from points by FUN_00484ef0 |
| +0x58 | u16 | max members |
| +0x5A | u16 | member count (after sub 3 = list count) |
| +0x5C | u32 | guild points |
| +0x60 / +0x64 | u32 / char[17] | master uid / master name. **The client decides "am I the master" by `strcmp(own name, M+0x64)`** (FUN_00484d70, FUN_004847b0, window 0x4BB/0x4BC handlers). |
| +0x78 | list | member records, 0x20 B each: +0 u8 online (0 = offline), +4 u32 uid, +8 char[17] name, +0x19 u8 level, **+0x1A u8 job1 (class 0..6)**, **+0x1B u8 job2 (0..2)**, +0x1C u8 grade (1..5) |
| +0x88 | char[61] | notice |
| +0xC6 / +0xC8 | u16 / u16 | emblem foreground (sprite 0x148) / background (sprite 0x149) |
| +0xCA/+0xCC/+0xCE | u16 x3 | Guild Battle wins / losses / draws |
| +0xE0 | list | pending applications (heap str[130] records), FIFO |
| +0x114 | list | Guild Plaza advertisement boards (0x70-byte records) |
| +0x190 | ptr | the board the player clicked (join dialog) |

**Correction to protocol_spec_2009 (0xB3):** the record's +0x1A is job1 and +0x1B is job2, not the reverse [V]. Proof: the create pre-fill writes `+0x1A = entity+0x118` (job) and `+0x1B = entity+0x119` (job2) (0x484484..0x4844AA), and the class icon index is `7*[+0x1B] + [+0x1A]` into the 3x7 table 0x53E4C0 (rows = job2 0..2, columns = class 0..6) (0x491D77..0x491D99). The wire order is unchanged; only the field names in the spec grammar are swapped (sub 3: 3rd byte = job1, 4th = job2; sub 13: 5th byte = job2, 6th = job1). The Guild Battle gate "own +0x1A != 0" (0x485D58) therefore means "has a first class", not "2nd class".

**Lifecycle** [V]:
- Before the first S2C 0x03, M+0x130 is NULL, so the client sends **no** guild C2S at all (every builder tests M+0x130).
- The S2C 0x03 world-info handler calls `FUN_0047a1a0` at 0x452C01, mid-parse. This copies the socket into M+0x130 and resets everything: FUN_0047a8c0 (guild fields and member list), FUN_0047aaa0 (**application list M+0xE0**), FUN_0047a980 (**board list M+0x114**), FUN_0047a500 (guild window controls), and others.
- At the end of the same handler the client sends C2S 0x2F, C2S 0x63, then **C2S 0x8A** (0x453559..0x453581).
- **So after every map load the client has forgotten its guild, its pending applications and the plaza boards.** The 0x8A reply must restore all three.
- **NULL hazard [V]:** subs 1, 3, 6, 8 and 15 write `[scene+0x988]+0x12` (the local player entity) with no NULL check (for example 0x47DB55, 0x47DA58). Send them only after the local 0x07 record has been processed. The current server order (0x03, then 0x07, then the 0x8A reply) satisfies this.

### 1.2 Nameplates and entity+0x12

Entity fields [V]: +0x12 u16 `gm_or_guild_id`, +0x16A1 char[17] guild name, +0x16B2 u16 emblem fg, +0x16B4 u16 emblem bg, +0x18 bool gm_hidden (GM only).

Renderer FUN_0043c300 [V] (0x43C6E0..0x43C930):
- +0x12 == 1 draws a green (0xFF00FF00) `0x52D3F8 "Game Master"` line.
- +0x12 > 1 draws the guild name in yellow (0xFFFFFF00) plus emblem sprite 0x149 (frame +0x16B4), then sprite 0x148 on top (frame +0x16B2).
- 0 draws nothing.

Writers of entity+0x12:

| Writer | Effect | V/I |
|---|---|---|
| S2C 0x04/0x05/0x07 record `u16 gm_or_guild_id` (0x4536A5) | ==1 is followed by `bool gm_hidden`; >1 is followed by `str[17] name, u16 fg, u16 bg` | [V] |
| S2C 0x2B/0x2C/0x2E (arena rosters) | `u16 guild_id`; >1 is followed by the same 21 bytes; **no gm_hidden byte** | [V] (spec) |
| S2C 0x52 info view | `u16 guild_id`; >1 is followed by `str[17]` name (label 4, otherwise "N/A") | [V] (spec) |
| S2C 0xB4 | sets all four fields on any entity | [V] |
| S2C 0xB6 | +0x12 = 0 and emblems = 0 | [V] |
| S2C 0xB7 | 0xB6 plus the first DWORD of the name | [V] |
| 0xB3 sub 1 (success) | local +0x12 = new guild id **only** (name and emblem are not set) | [V] 0x47D5C0 |
| 0xB3 sub 3 | local +0x12, name, emblems | [V] 0x47DA5E..0x47DAC7 |
| 0xB3 sub 6 / sub 8 | clear local fields | [V] |
| 0xB3 sub 15 | local +0x12 = 0 (result != 1) or 0xFFFF (result 1) | [V] |
| 0xB3 sub 37 | entity(uid)+0x12 = 1 | [V] 0x47F951 |

**GM interaction** [V]:
- Sub 15 answers every 0x8A and zeroes the local +0x12, so a GM's "Game Master" tag disappears after the first map change.
- The server already re-sends **sub 37 {own uid}** after the sub 15 (windslayer_server.py:7784-7790; the comment there records the live observation).
- A GM (local +0x12 == 1) cannot create a guild (menu 0x4B6: `0x528FD8 "Wind master can't register \r\nas guild."`) or apply to one.
- 0x21 never carries the guild tail for a GM, because the gate is `> 1`.

### 1.3 Guild level, points and capacity (client tables) [V]

- **Level.** FUN_00484ef0 uses the non-cumulative u32 increments at 0x5250B8: `405450, 1013625, 1925887, 3294280`. The level is the first i (1-based) with `points < sum(inc[0..i-1])`, and 5 when `points >= 6,639,242`. Cumulative thresholds: Lv1 < 405,450 <= Lv2 < 1,419,075 <= Lv3 < 3,344,962 <= Lv4 < 6,639,242 <= Lv5.
- **Progress bar** (window 0x4B3 control 0x39 gauge, control 0x3A text): `(points - start_of_level) / inc[level-1]`, formatted `0x52E314 "(%.2f%%)"`, or `0x52E30C "(100%%)"` at level 5 (FUN_004847b0 0x484930..0x484A29). Retail "Lv.2 ... (2.91%)" therefore means about 434,950 points [I].
- **Capacity** (FUN_00484f40, guild NPC menu control 4):
  - Level 1: window 0x4BE shows "Guild Level Requirement: 2" (`0x52E320 "2"`) and "Required Gold: 6500". No increase is possible.
  - Level 5 with max 50: `0x528E58 "You can't add max No. of guild any more."`
  - Otherwise, if `max != cap[level]` (cap table 0x5250C0+4*L: L2 = 20, L3 = 30, L4 = 40, L5 = 50), the client picks the first tier `T[i]` in `{20, 30, 40, 50}` (0x5250C8) that is greater than max. It opens window 0x4BD with `0x528E84 "You can add max No. of guild by %u."` (T[i] - max) and "Cost to increase: `cost[i]` Gold", where cost = `{6500, 20000, 60000, 100000}` (0x5250D8).
  - If `max == cap[level]`, window 0x4BE shows "Guild Level Requirement: L+1" and "Required Gold: cost[L-1]".
- **Starting capacity 15.** The create pre-fill sets M+0x58 = 15 (0x4843D7) [V]. Retail "(4/20)" at Lv.2 fits this table [I].

### 1.4 Grades [V]

The display table is char[15] at 0x53E541 + 15*grade (drawn only when grade > 0; 0x491F43..0x491F58): **1 `Trainee`, 2 `Soldier`, 3 `Vanguard`, 4 `Guardian`, 5 `Master`**. Retail shows "Master / Soldier / Trainee" [I video, consistent].
- The grade buttons in window 0x4B3 carry UI captions 9105 "Apprentice", 9106 "Junior", 9107 "Senior", 9108 "Submaster" (= grades 1..4). The captions differ from the drawn names; this is cosmetic.
- Client caps (FUN_00485350) [V]: at most 4 Guardians (grade 4, `0x528D1C "You can only set 4\r\nGuardians."`) and 10 Vanguards (grade 3, `0x528D00 "You can set 10 \r\nVanguards."`). Grade 5 is never settable through 0x92. A click on the member's current grade sends nothing.
- Guild Battle registration requires own grade >= 3 (`0x528AE4 "Only Guardian and Vanguard can\r\nregister for the Guild Battle."`) [V].
- The grade-editor toggle (row buttons 0x1D..0x21, FUN_00485510) has **no master check** in the client [V]. The server must enforce who may change grades [I].

### 1.5 Windows and controls

Control numbers are the 1-based ids of `ui_guild.txt`, and they match the `param_3` values in FUN_00480430 [V] (for example control 8 is relabelled by FUN_004847b0, and control 0x3E = 62 is "Guild Battle").

**Guild Info tab 0x4B3** (a tab of the Community window 0x176; tabs Friend 0xB / Mento 0xD / Blacklist 0x3B switch back to 0x176) [V]:

| Control | Content |
|---|---|
| 9 | `"%s   Lv.%u"` (name, M+0x57) |
| 0xA | `"(%u/%u)"` (M+0x5A, M+0x58) |
| 0x39 / 0x3A | progress gauge / `"(%.2f%%)"` |
| 0x3D | `0x528584 "%uRound %uWin %uLose %uDraw"` (W+L+D, W, L, D) |
| member list | online non-masters first, then offline non-masters; the master is inserted with flag 1 (listed first [I]). Rows show name, "Lv.%u", a class icon (sprite 0x147) and the grade name. |
| 7 "Converse" | offline member: `"<%s>%s"` with `0x52B1E4 " is\r\n\r\nnot on line."`; otherwise FUN_0047b130 sends **C2S 0x33 {uid, name}** ("Inviting <name> for a conversation.", messenger system) |
| 8 | caption `0x5290EC "Kick"` when own name == M+0x64, else `0x5290E4 "Leave"` (UI default 9102 "Expulsion") |
| 0x1D..0x21 | per-row grade editor |
| 0x22..0x35 | grade buttons (5 rows x 4); column 1..4 = grade 1..4, which sends 0x92 |
| 0x3E | 9335 "Guild Battle": FUN_00485cb0 sends 0x96 |

- **Kick (master)** needs a selected row with grade != 5. It opens confirm 0x4C2 with `0x529118 "Do you want to kick this member out?"`, control 4 = member name, action 1. With no row selected: `0x52B12C "Please select a character name. "`.
- **Leave (non-master)** opens 0x4C2 with `0x5290F4 "Do you want to leave this guild?"`, control 4 = guild name, action 2.

**Guild NPC menu 0x4BB** (NPC **Moiba**, hni idx 181, `UI` 1211 = 0x4BB). It is placed only in **map 9702 "Guild Plaza"** (stage97_02, tile 2200,1300) [V: EN 2009 hni + hmi]. Map 9702 is entered from maps 801, 1001 and 1101 (portals `801_75`, `1001_64`, `1101_72`, server portals_en_2009.json) [V]. Opening the menu: the master gets control 3 enabled and control 2 disabled; others get the reverse (0x448A0C..0x448A7A) [V].

| Control | Caption (UI id) | Client behaviour | V/I |
|---|---|---|---|
| 2 | "Guild make" (9082) | M+0x44 < 2 opens creation window 0x4B4, otherwise `0x5291AC "Already in the other guild."` | [V] |
| 3 | "Break Guild" (9125) | confirm 0x4C2 (`"Do you want to leave this guild?"`, guild name) with **action 3**. On OK: member count > 1 gives `0x528FA0 "To do this,\r\nyou have to kick someone in your guild."` and nothing is sent; otherwise 0x8E action 3 | [V] |
| 4 | "Increase Capacity" (9126) | master only (else "Guild master can only do this.") runs FUN_00484f40 (section 1.3), then window 0x4BD OK sends 0x90 | [V] |
| 5 | "Purchase advertisement" (9127) | quantity dialog 0x10 `0x528B90 "How many do you want to buy?(%uGold)"` for template item +0x288 (dialog +0x134 = item, +0x11C = the NPC id copied at 0x482331, +0x40 = 5), then C2S 0x0B {item, qty, npc}. **npc is always 0 on the wire**: the OK control (hui window 16 control 2) has Event 1 (close), so FUN_00448730 closes dialog 0x10 before it dispatches the control, and FUN_00497e00 (mode 1) zeroes +0x120 / +0x11C (0x497E9F / 0x497EA5) before FUN_00472f40 reads +0x11C (0x474315). The item survives (+0x134 is not cleared). The server must infer Moiba (map 9702 + a board item id) | [V] asm; live P15 G1a (raw `0B BB 10 01 00 00 00`) |
| 6 | "Change Guild Master" (9128) | master only, window 0x4BF (name field + "Member list" picker 0x433), OK sends 0x91 (empty name: message, nothing sent) | [V] |
| 7 | "Edit Guild News" (9129) | master only, window 0x4C1, OK sends 0x8F if the text is non-empty and differs from M+0x88 | [V] |

**Creation windows** [V]:
- 0x4B4 has name input 1, "Regist" 2, "Create" 7 (opens emblem picker 0x4C3).
- 0x4C3 has Shape/Color (emblem) and Pattern/Color (background) pickers. Window +0xDC becomes 3 when both are chosen. Picker value defaults are 0x12.
- 0x4B5 confirm reads "Registration Cost: 50,000 Gold" (UI 9122/9077). Its control 2 runs FUN_00484200.

**FUN_00484200 gates, in order** [V]:
1. Emblem chosen (0x4B4 +0xDC == 3), else `0x529030 "You can't meet the guild prerequisite.\r\nLook for guild help.\r\nCause: (%s)"` with `0x529000 "Guild Mark"`.
2. Name check FUN_0043e1b0(name, 1), else `0x529238 "This name is not allowed for guild name.\r\nPlease, try other name."`.
3. Gold (u64) >= 50,000, else Cause `"Register Gold"`.
4. Level >= 30 (from exp), else Cause `"Level"`.
5. Local entity+0x119 (job2) != 0, else Cause `"2nd Class Change"`.

Then the client **pre-fills M optimistically**: +0x60 own uid, +0x57 = 1, +0x46 name, +0x5C = 0, +0x58 = 15, +0x5A = 1, emblem, and its own member record (online = scene+0x21D, level, job1, job2, grade 5). Then it sends 0x87.

**Application window 0x4BC** (opened from HUD window 3 control 8, below) [V FUN_00484580]:
- Control 1 = record+1 (name), control 2 `"%s : %u"` "Level" = u16@+0x6E / 100, control 3 `"%s : %s"` "Class" = class-name table 0x544A18[(v%100)/10*7 + (v%100)%10], control 4 `0x5291C8 "applied for this\r\nguild."`.
- Accept (control 5): the master sends 0x8B. **A non-master pressing Accept actually rejects** (0x8C).
- Deny (control 6) sends 0x8C.

**HUD window 3 control 8** ("Receive a message", UI 61) is the shared notification icon [V]:
- sub 2 (0x10) and sub 4 set its state to 0 (shown).
- It is re-hidden (state 3) when the application list becomes empty (FUN_004846d0 / FUN_00484730).
- Clicking it opens a pending memo first (M+0xD4, window 0x3FE), otherwise window 0x4BC (FUN_00448730 case 8).

**Join dialog 0x4B6** [V]:
- Opened by a plaza board click (mode 0) or by player context menu 0x50 **control 12** (mode 2).
- Control 12 is enabled (white) only when the target entity's +0x12 > 1 and the local +0x12 == 0; otherwise it is greyed (0x450452..0x4504BD). The menu then pushes the target's guild id and guild name.
- Its caption is unknown: the decoded `windslayer.hui` lists only 11 controls for window 0x50 [I "Join Guild"].
- OK: M+0x44 >= 2 gives "Already in the other guild."; local +0x12 == 1 gives "Wind master can't register as guild."; otherwise it sends 0x89.

**Chat mode** [V]: window 0x6D control 3 "To Guild" requires local +0x12 >= 2, else `"You can only do this while you are in some guild."`. The typed prefix `/g ` is not gated.

### 1.6 Guild Plaza boards and the EN item-id mismatch

- Board rendering (FUN_004330c0) runs only when `scene+0xF18 == 6` and the map code is 0x25E6 (9702) [V] (spec).
- The client hardcodes the **KR** item ids: **0x10B8 (4280) = Guild Billboard**, a Type-0 item that opens dialog 0x4B7 in map 9702 (FUN_0044f070 0x44FCB8) and draws sprite 0x145; **0x10B7 (4279) = premium board**, from the cash bag (FUN_0046d6f0 case 0x10B7), sprite 0x146 [V]. Sub 185 also tests 0x10B8.
- **The EN 2009 `windslayer.hii` is shifted by +4 in this range** [V]: 4279 "Sky Bow", 4280 "Rider's Bow" (Type 1 weapons), **4283 "Premium Guild Billboard"** (cash, Type 5), **4284 "Guild Billboard"** (Buy 1000, Type 0). KR gamedef has the boards at 4279/4280.
- Moiba's EN template sells **4283** (hni `item:` 4283; KR NPC 181 sells 4280) [V].
- Consequence [I, must be live-tested (T-BOARD-ITEM)]: in Build 14 no EN item reaches the billboard code. 4284 is Type 0 but not 0x10B8, so it takes the generic use-item path and sends C2S 0x15 {4284}. 4283 = 0x10BB lands on the **pet-food** case of FUN_0046d6f0 (0x10BA..0x10BD). **The C2S 0x88 path is probably unreachable with stock EN data.** The S2C side (0xBA/0xBB) still renders if the server uses board_item_id 4279/4280.
- **After cp-2** (G-CP exe patch, `patch_2009.py` default; CLIENT_PATCH_SET_RE_2026-10-06.md 8.5): all six board constants are +4. Then EN 4284 opens dialog 0x4B7 in 9702 and EN 4283 takes the premium-board case, so C2S 0x88 becomes reachable. The server must then send **4284 / 4283** in 0xBA / 0xBB / sub 185; KR 4280 / 4279 would draw no sprite. Everything above describes the stock exe. The server setting that follows the installed exe is `CLIENT_ITEM_IDS` (`"en"` = cp-2 exe, `"kr"` = stock exe; CLIENT_PATCH_SET_RE_2026-10-06.md 8.5).

---

## 2. S2C 0xB3 sub-command reference

Dispatch [V]: SubHandler3 FUN_0047bd60, case 0xB3 at 0x47D506.
- Gate: M+0x124 and M+0x12C non-NULL, otherwise the whole packet goes unhandled.
- `u8 sub` read at 0x47D51C. `sub-1 > 0xB8` exits. Index table 0x47FBA8 into target table 0x47FB14.
- 36 targets for 1..35 and 37, plus 185 (0xB9). Sub 36 and every other value read nothing more and return.
- Every sub-code below is [V] (disassembled in `_work/guild/b3_handler.asm`) unless marked [I].
- Colour 0xFFFFC800 (orange-yellow) = "guild colour".

| Sub | Target VA | Wire body after `u8 sub` | Client effect and strings |
|---|---|---|---|
| 1 | 0x47D543 | `u8 result; if result==1: u16 guild_id` | **1:** M+0x44 = id; M+0x64 = own name; local +0x12 = id; **gold -= 50,000 locally** (0x47D5D0 adds 0xFFFF3CB0 to scene+0x278, u64); gold label refresh; box `0x5292E4 "Guild has been registered."`; hide 0x4B3; FUN_004431a0. **Every failure resets M** (FUN_0047a8c0, clears the pre-fill). **2** `0x52927C "You can't use this guild name.\r\nPlease, try other name."`. **4** `0x528FD8 "Wind master can't register \r\nas guild."`. **5** `0x5291AC "Already in the other guild."`. **7/8/9** build "Cause: (Register Gold / Level / 2nd Class Change)" but the box shows the Wind-master text (client bug). **Other** `0x5292B4 "Fail to register the guild.\r\nPlease, try later."` |
| 2 | 0x47DB7D | `u8 result; if result==0x10: str[130] record` | **1** `0x529210 "You have applied for this guild."`. **6** `0x5291E4 "This guild can't recruit any more\r\nmembers."`. **0x11** `0x528C18 "Guild admission is being\r\ndelayed."`. **0x10** appends a heap copy to M+0xE0 and shows the HUD icon. Other values do nothing. |
| 3 | 0x47D77B | `u16 guild_id; str[17] name; u32 points; u16 max_members; str[61] notice; u16 emblem_fg; u16 emblem_bg; u16 wins; u16 losses; u16 draws; u8 n; n x {u32 uid; u8 grade; u8 job1; u8 job2; u8 level; u8 online; str[17] name}` (98 + 26n B) | Writes all fields into M. Member records are **appended** (no clear). A grade-5 record sets M+0x60/+0x64. M+0x5A = list count; level recomputed. Chat line (colour 0xFFFFC800): notice empty gives `0x528CA4 "[Guild Master]:Hello. Welcome to %s guild."`, otherwise `0x52907C "[Guild Master]:%s"` (notice). Then local +0x12/+0x16A1/+0x16B2/+0x16B4 = id/name/fg/bg; FUN_004431a0. Printed **on every sub 3**, which means on every map load. |
| 4 | 0x47DCF1 | `u8 n; n x str[130] record` | appends to M+0xE0 and shows the HUD icon |
| 5 | 0x47DDFF | `u8 result` | **1** `0x52918C "Guild admission has completed."`. **5** "Already in the other guild.". **Other** "This guild can't recruit any more members." |
| 6 | 0x47DE6A | `u8 result` | **0** `0x528D3C "Procedure failed.\r\nPlease, try later."`. **0xC** `0x528B24 "You can leave the guild after\r\n1 day."`. **Other:** hide 0x4B3, reset M, refresh window, local +0x12/+0x16B2/+0x16B4 = 0 (name bytes kept), chat `0x5290C8 "You are out of this guild."` (colour 0xFFFFC800). |
| 7 | 0x47DFD3 | `u8 result` | **0** `0x529090 "Fail to change the notice."` and the notice is blanked (DWORD M+0x88 = 0). **1** `0x5290AC "Notice has been changed."`. Other: nothing. |
| 8 | 0x47E073 | `u8 result` | **0xA** "To do this, you have to kick someone in your guild.". **0xB** `0x528F60 "You can delete your guild\r\nafter 7 days from it's starting day."`. **1:** local +0x12 = 0, name DWORD = 0, emblems = 0; hide 0x4B3; reset M; box `0x528F3C "Your guild has been deleted."`. Other: nothing. |
| 9 | 0x47E18A | `u8 ok; if ok: u32 cost` | **ok:** gold -= cost locally, then `0x528E30 "Max No. of guild has been increased."`. **0:** `0x528DF0 "Fail to increase the No. of guild member.\r\nPlease, try later."`. M+0x58 is **not** updated (see sub 10). |
| 10 | 0x47E255 | `u16 max_members` | M+0x58 = value; window refresh |
| 11 | 0x47E29A | `u8 result` | **1** `0x528DAC "Guild master has been changed."`. **8** `0x528D64 "This member can't be\r\nthe guild master."`. **Other** `0x528D8C "Fail to change guild master."` |
| 12 | 0x47E30D | `str[17] new_master` | hide 0x4B3. FUN_00485200: if both the current grade-5 record and the named record exist and differ, then M+0x60/+0x64 = named uid/name, old master grade = **1**, new = 5. Refresh. |
| 13 | 0x47E6C9 | `u32 uid; str[17] name; u8 online; u8 level; u8 job2; u8 job1; u8 grade` (27 B; **different order from sub 3**) | new record appended, M+0x5A += 1, hide 0x4B3, refresh |
| 14 | 0x47E378 | `str[17] name` | hide 0x4B3, remove the record by name (FUN_004852c0), M+0x5A -= 1, refresh |
| 15 | 0x47DAF5 | `u8 result` | **1:** local +0x12 = **0xFFFF** and box `0x5285A0 "Failed to get guild info.\r\nPlease, log in again."`. **Other:** local +0x12 = 0. (The "not in a guild" answer.) |
| 16 | 0x47E3F1 | `u8 result` | **1** `0x528CD0 "The grade of this member \r\nhas been changed."`. **0xD** "You can only set 4 Guardians.". **0xE** "You can set 10 Vanguards.". **Other** "Procedure failed...". Always hides grade buttons 0x22..0x31 afterwards. |
| 17 | 0x47E4E7 | `str[17] name; u8 grade` | record(name).grade = grade; refresh (unknown name: nothing) |
| 18 | 0x47DF6A | `u8 result` | **0** "Procedure failed...". **Other** `0x528BFC "Kicked that guild member.\r\n"` |
| 19 | 0x47E7E2 | (none) | the client **sends C2S 0x8A** (FUN_00484550) |
| 20 | 0x47E56C | `str[17] name; u8 online` | record(name)+0 = online; chat `0x528BE8 "[%s] has logged in."` (0xFFFFC800). Unknown name: nothing. |
| 21 | 0x47E56C | `str[17] name; u32 points_total` | Only if the member is found: delta = new - M+0x5C, M+0x5C = new, level recomputed, record+0 = 0. Chat `0x528BD0 "[%s] has logged out."` and `0x528B4C "Guild point(+%u : [%s]) increased."` (both 0xFFFFC800). |
| 22 | 0x47E8D6 | `str[17] old; str[17] new` | ignored unless M+0x44 > 1. record(old)+8 = new; if that record is grade 5, M+0x64 = new. **No window refresh.** |
| 23 | 0x47E9DC | `u8 result; u16 value` | Guild Battle registration result (section 9). Always hides box 0x16 first. |
| 24 | 0x47EB48 | `u32 id; str[17] name; u16 value` | GB invitation window 0x4D8 (needs M+0x44 > 1) |
| 25 | 0x47EC25 | `u32 id; str[17] name; u8 reason` | GB roster add/refusal (section 9) |
| 26 | 0x47EDAF | `i32 ok; if ok: i32 last; if last >= 0: (last+1) x {u32 id; str[17] name}` | GB roster list 0x4D1. **Stack hazard: last <= 24** (cookie at esp+0x220); use <= 5. |
| 27 | 0x47EED6 | `i32 value` | 0: `0x528920 "You can't play Guild Battle due to\r\nlack of empty place."` |
| 28 | 0x47EF35 | same as 29 | hides 0x4D0/0x4D2, clears the 0x4D9 list, sets 0x4D0 +0x134 = 1, then falls into 29 |
| 29 | 0x47EF91 | `u8 n; u8 in_progress; n x {u16 room_id; str[17] a (+2); str[17] b (+0x13)}` | room list 0x4D9. Label `"%u (%u %s)"` (count, in_progress, "In progress"). **Sleep(500)** inside the network handler (0x47F10D). |
| 30 | 0x47F1B7 | `u8 kind; if kind==1: u32 member_id` | kind 2: hides all GB windows, `0x528864 "Guild Battle registration has been stopped."`. Other kinds: remove a roster entry (section 9). |
| 31 | 0x47F143 | same as 30 | if 0x4D9 is open: `0x5286CC "One member left. Regroup\r\nthe team."`, clear 0x4D9. Then acts as 30. |
| 32 | 0x47F4C6 | `u8 kind; u8 in_progress; u16 room_id; kind 1: str[17] b; str[17] a; kind 2: u16 room_id2` | room-list delta on 0x4D9 (kind 1 adds a record; the wire order is **b before a**) |
| 33 | 0x47F6C7 | `u16 room_id; str[17] name` | challenge prompt 0x4D6 `"[%d] %s"` (only if 0x4D6 is closed and 0x4D9 is open) |
| 34 | 0x47F891 | `u8 result` | hides box 0x16. **1** `0x528828 "You are either already on the Battle\r\nor not registered."`. **2** `0x5286F0 "Same guild can't play Guild Battle."`. **Other: nothing**, so `sub 34 result 0` is a silent "close the waiting box". |
| 35 | 0x47F7E9 | `u8 outcome` | 1 wins+1, 2 losses+1, 3 draws+1; refresh |
| 36 | - | (none) | no case (default) |
| 37 | 0x47F918 | `u32 uid` | entity(uid)+0x12 = 1 ("Game Master" tag). Unknown uid: nothing. |
| 185 | 0x47E805 | `u8 flag; if flag==1: u16 item_id` | flag 1 and item 0x10B8: the client sends **C2S 0x15 {u16 0x10B8}** (UseItem). flag 1 with another item: nothing. flag != 1: `0x528EA8 "Guild master can only do this."` After cp-2 the compare is 0x10BC (4284), and the 0x15 echoes the packet's own field, so `{1, 4284}` gives C2S 0x15 {4284}. |

**Sub 3 hazards** [V]:
- A second sub 3 without an intervening 0x03 duplicates every member, so send sub 3 at most once per map load. The only exception is the "member_count = 0" trick in F1.
- Every string is copied with a C string copy: keep names NUL-terminated within 17 bytes and the notice within 61.
- Sub 3 prints the welcome line every time.

---

## 3. Other S2C: 0xB4..0xBB, 0x21 tail, broadcasts

- **0xB4 EntityGuildSet** [V] (spec, 0x45D840..0x45D96E). Send it to every player on the same map (including the member) when that player's guild state changes: create, join, emblem change. Unknown uid: the bytes are consumed and nothing happens. It also works as the fix-up for the local nameplate after sub 1 (sub 1 does not set the name or emblem).
- **0xB6 / 0xB7** [V] (spec). 0xB6 clears id and emblem. 0xB7 additionally zeroes the first 4 name bytes. They look the same on screen. Use **0xB7** for leave, kick and disband [I] (retail choice unknown). They do not touch M (the guild window); the sub-codes do that.
- **0xB5 ChatLineGuildColor** [V] (0x45D07F..0x45D0F3): reads `u8 len` then `len` bytes into a zeroed 88-byte buffer and calls FUN_00447bb0 with colour 0xFFFFC800. This is **the same function and colour as the client's own /g echo** (0x447761..0x44776E) and the guild system lines. KR uses 0xB5 with the same colour (PySlayer case 0xB5, colour -14336 = 0xFFFFC800). **Use 0xB5 as the guild-chat relay** [I, strong]. Keep len <= 87.
- **0xB8 / 0xBA / 0xBB** [V] (spec): boards. 0xBA uses the C2S 0x88 field order (item id before guild id). **0xBB records use a different order** (guild id after master name, item id after ad text). The board list is cleared by every 0x03 (FUN_0047a980), so a player entering 9702 needs a **0xBB after that map's 0x03**.
  - For the client's "double billboard" check, `master_char_id` must equal the placing master's uid (FUN_00484d70 compares board+0 with scene+0x224) [V].
  - Placement gates in the client [V] (FUN_00484d70):
    - master only;
    - `0x528EC8 "You can't make\r\ndouble billboard."` when a board with board+0 == own uid exists;
    - `0x528EF0 "You are too close from other billboard.\r\nPlease, move to other location."` when another board is within sqrt(25000) (about 158 px).
- **0x21 guild tail** [V] (0x4591EA..0x459236). After "You've received (+%d) experience points.", the client tests `word [[scene+0x988]+0x12] > 1`. If so it reads **u32 guild_points** and prints `0x528B70 "(+%u) guild points are gained."` (colour -1). The value is only displayed, never stored.
  - **The server must mirror the receiver's client-side +0x12**: the last value set by the 0x07 own record, sub 1/3/6/8/15/37, or 0xB4/0xB6/0xB7 on self. Otherwise the packet is 4 bytes short or long.
  - Sub 15 result 1 (0xFFFF) also makes the client expect the tail, so never use result 1.
  - 0x7F (mentor) never has the tail.
  - Retail (2010 client) printed "(20) guild points are gained." for a 41-exp kill [I video, different build].
- **Guild Battle broadcasts.** KR has 0x15 subtypes 3/4/5 with two names (`"[%s]길드가 [%s]길드와의 경기에서 승리 하였습니다."` etc., PySlayer 4353-4363). **The EN 2009 0x15 has no such subtypes** (wire `u8 type; u8 len; str`), and Build 14 contains **no** "[%s] guild has won the game against [%s] guild." format string [V] (strings.tsv). The retail line (2010 video) must have come pre-formatted. Recommended: S2C 0x15 `msg_type 0` (chat line) or `2` (chat line plus centre notice) with the full text [I]. `0x528790 "Draw. Guild Battle is closing."` is printed locally by **S2C 0x36** when room mode (scene+0xF90) == 7 and the round result is a draw [V] (0x45B4D6..0x45B4EB).

---

## 4. C2S send sites (all builders verified by asm)

| Opcode | Builder VA (Send) | Trigger / UI | Client gates before sending | Required server answer |
|---|---|---|---|---|
| 0x87 | FUN_00484200 (0x48451D) | 0x4B5 control 2 | see 1.5 (emblem, name, 50k gold, Lv30, job2, not in a guild) | **sub 1** (any result; failures reset the optimistic pre-fill) |
| 0x88 | FUN_00480430 (0x482C4D) | dialog 0x4B7 OK, after using board 0x10B8/0x10B7 in 9702 | non-empty text + CheckString, master, no own board, >= 158 px from others, a bag item selected | 0xBA broadcast; sub 185 [I] |
| 0x89 (4 B) | 0x482D64 | 0x4B6 mode 2 (context menu control 12) | M+0x44 < 2, not GM | **sub 2** |
| 0x89 (2 B) | 0x482DCD | 0x4B6 mode 0 (board click in 9702) | same, board guild id > 1 | **sub 2** |
| 0x8A | 0x453581 / 0x48456D | end of every S2C 0x03; sub 19 | M+0x130 set | **sub 3** or **sub 15 result 0** (+ sub 4, + sub 37) |
| 0x8B | FUN_00484730 (0x484772) | 0x4BC Accept, master | pops the FIRST application locally | sub 5 (+ fan-out) |
| 0x8C | FUN_004846d0 (0x484707) | 0x4BC Deny (or Accept by a non-master) | pops the FIRST application locally (even with no socket) | none |
| 0x8D | FUN_0047ad00 (0x47AD4F) | `/g msg`, CP949 IME prefix, or chat mode 3 | 700 ms anti-spam (`"Do not Spam."`), curse filter; **no guild check for /g**; local echo first | relay 0xB5 to the others |
| 0x8E | FUN_00484bd0 (0x484C32) | 0x4C2 OK: action 1 (kick selected), 2 (leave, own uid/name), 3 (disband, only when count <= 1) | see 1.5 | action 1 sub 18; 2 sub 6; 3 sub 8 |
| 0x8F | FUN_00484c90 (0x484CD8) | 0x4C1 OK | non-empty, differs from M+0x88; the client copies it into M+0x88 **before** sending | **sub 7** |
| 0x90 | FUN_00480430 (0x48306B) | 0x4BD OK | client tier/cost from its tables | **sub 9** (+ sub 10) |
| 0x91 | FUN_00484c40 (0x484C87) | 0x4BF OK | name non-empty | **sub 11** (+ sub 12) |
| 0x92 | FUN_00485350 (0x485447) | 0x4B3 grade buttons | 4 Guardians / 10 Vanguards caps; not the same grade | **sub 16** (+ sub 17) |
| 0x96 | FUN_00485cb0 (0x485D93) | 0x4B3 control 0x3E; a player's GB sign (+0xE9 == 7) | FUN_00445c00(0) room gates; own grade >= 3; >= 6 online members (`0x528AB4 "You have to have at least\r\n6 members online."`); own record job1 != 0 | **MUST** answer sub 23 or sub 34: a "Waiting for the server to respond." box stays up |
| 0x97..0x9D | section 9 | Guild Battle windows | - | section 9 |

---

## 5. Request/response flows

Conventions:
- `G` = guild record, `m(X)` = X's membership, `uid(X)` = X's session uid (the value of scene+0x224 on X's client), `name(X)` = character name (at most 16 bytes, NUL-padded to 17).
- "Online members" = sessions in world whose character is in G.
- "Same map" = in-world sessions on the same map code.
- All sends use the target session's encoding (`_send_to`, as in party F1).

### F0. Map load / login (0x8A) — the backbone
1. The server sends 0x03, then 0x07 (the own record carries `gm_or_guild_id = G.id` and the guild block when in a guild) [V wire].
2. The client resets M and sends 0x2F, 0x63, 0x8A.
3. The server answers 0x8A:
   - **Not in a guild:** sub 15 result 0, then, if GM and not hidden, sub 37 {own uid} (as today).
   - **In G:** send one sub 3 with all members. For each member: uid = account uid, grade, job1 = class, job2, level from exp, online = channel number (>= 1) if online else 0 [I], name. Then:
     - if the receiver is the master and G has pending applications, **sub 4** with them in FIFO order (the 0x03 reset emptied the client's list [V]);
     - if the map is 9702, **0xBB** with the live boards (the 0x03 reset cleared them [V]).
   - Never answer 0x8A with sub 19 (endless loop) [V].
4. Mirror `session.client_guild_id = G.id` (or 0) for the 0x21 tail.

On login (the first world entry of the session), additionally send **sub 20 {name, channel}** to every other online member (`"[X] has logged in."`) [V client text; I retail].

### F1. Create (0x87, NPC Moiba in 9702)
1. Client gates (1.5), then optimistic pre-fill, then **C2S 0x87** `{name, own_uid, fg, bg}`.
2. Server checks, in order:
   - already in a guild: sub 1 result 5;
   - GM: result 4;
   - name illegal or duplicate: result 2 [I: retail used 2 for "can't use this name"];
   - gold < 50,000: result 7; level < 30: result 8; job2 == 0: result 9 (these show the wrong text in the client, which is harmless);
   - emblem out of range: result 3 (shows "Fail to register") [I].
3. On success:
   - allocate an id in 2..0xFFFE; create G {master = X, points 0, max 15, notice "", emblem, W/L/D 0, members [X grade 5], created_at};
   - **deduct 50,000 gold** and persist (the client already subtracted it locally);
   - send **sub 1 {1, id}**;
   - then **sub 3 with member_count = 0**. This fills name/points/max/notice/emblem and the local nameplate and prints the welcome line without duplicating the pre-filled self record [V behaviour of sub 3; design I];
   - then **0xB4** {uid(X), id, name, fg, bg} to the same-map sessions **other than X** (X's nameplate is set by sub 3).
4. Log `[GUILD] create id=.. name=.. master=..`.

Without step 3's sub 1 the client keeps a phantom guild in the window until the next 0x03. There is no modal and no soft-lock [V].

### F2. Apply (0x89) and deliver the application
1. X → **C2S 0x89** (u32 or u16; distinguish by the payload length 4 vs 2) [V].
2. Server:
   - G missing: sub 2 result 0 (nothing shown; choose `6` to give feedback) [I];
   - X already in a guild: result 5 [I] (not a sub-2 text; use sub 2 with 0, or sub 5 5);
   - G full (`len(members) >= max`): **sub 2 result 6**;
   - otherwise append the application {name(X), uid(X), level, job1, job2, time} to G.applications (drop duplicates).
3. If G's master is online: send the master **sub 2 result 0x10 + str[130]** record = `byte0 = 0` [I], `+1 name[17]`, `+0x6E u16 = level*100 + job2*10 + job1` [V layout the client reads], rest zero. Send X **sub 2 result 1** ("You have applied...").
4. If the master is offline: X gets **sub 2 result 0x11** ("Guild admission is being delayed.") [I]. The master receives it through sub 4 on the next F0.

### F3. Accept / reject (0x8B / 0x8C)
The client always pops its **first** application before sending, so the server must pop G.applications[0] (FIFO), and must have delivered them in that order [V].
1. **0x8B {name}.** The sender must be the master and name must match applications[0] (otherwise drop the stale entry and sub 5 other).
   - Applicant now in another guild: master gets **sub 5 result 5**.
   - G full: **sub 5 other** ("can't recruit").
   - Otherwise add the member (grade 1 Trainee [I]) and persist.
2. Fan-out on success:
   - master: **sub 5 result 1**;
   - every online member (including the master): **sub 13** {uid, name, online, level, job2, job1, grade} (27 B, note the order);
   - the applicant, if online: **sub 3** (full list, its own M is empty after the last 0x03), or **sub 19** so that the client re-requests [V both work; sub 3 saves a round trip]. Set `client_guild_id`;
   - same-map sessions of the applicant: **0xB4**.
3. **0x8C** (empty): pop applications[0], no reply. Optionally notify the applicant with a 0x15 line [I].

### F4. Leave (0x8E action 2)
1. X (not the master) → 0x8E {gid, uid(X), name(X), 2}. Server checks gid == G.id.
   - The master cannot leave [I]: sub 6 result 0.
   - Optional retail rule: joined less than 24 h ago gives **sub 6 result 0xC** [V text; enforcement I].
2. Success:
   - remove, persist;
   - X: **sub 6 result 1** (`"You are out of this guild."`, clears the local state);
   - the other online members: **sub 14 {name}**;
   - same map: **0xB7 {uid(X)}**;
   - `client_guild_id = 0`.

### F5. Kick (0x8E action 1)
1. The master → 0x8E {gid, target uid, target name, 1}. The sender must be the master [I policy; the client shows Kick to the master only]. The target must not be the master.
2. Failure: master gets **sub 18 result 0**.
3. Success:
   - master: **sub 18 result 1** ("Kicked that guild member.");
   - remaining online members (including the master): **sub 14 {name}**;
   - the target, if online: **sub 6 result 1** (the only sub-code that clears a member's own state and prints "You are out of this guild.") [I reuse];
   - target's map: **0xB7**.

### F6. Disband (0x8E action 3)
1. The master (count == 1 on the client) → 0x8E action 3.
2. Server:
   - member count > 1: **sub 8 result 0xA**;
   - optional: guild younger than 7 days gives **sub 8 result 0xB** [V text; enforcement I];
   - success: delete G, clear boards (0xB8 {gid} to everyone in 9702), master **sub 8 result 1**, **0xB7** to the same map, `client_guild_id = 0`.

### F7. Grade change (0x92)
1. Sender policy [I]: master only (the client does not gate). Validate grade 1..4 and the target in G. Guardians (4) <= 4, else **sub 16 0xD**. Vanguards (3) <= 10, else **sub 16 0xE**.
2. Success: persist, requester **sub 16 result 1**, every online member **sub 17 {name, grade}**.
3. Always send exactly one sub 16: the client resets the grade buttons only on sub 16 [V].

### F8. Master change (0x91)
1. The master → {gid, name}. The target must be a member [I]; conditions for result 8 are unknown (for example offline, level < 30 or no job2) [I].
2. Success: new master = grade 5, old master = **grade 1** (this matches the client's FUN_00485200 [V]; the server must mirror it or the lists diverge). Requester **sub 11 result 1**, every online member **sub 12 {new name}**.

### F9. Notice (0x8F)
1. The master → str[61]. Store at most 60 bytes (cut at the first NUL), curse filter [I]. Requester **sub 7 result 1** (on failure sub 7 0, which blanks the requester's copy [V]).
2. Other members see it at their next F0 (there is no push sub-code [V]).

### F10. Capacity (0x90)
1. The master → {gid, add, cost}. **Recompute on the server**: level from G.points (1.3), the tier as the client does, and check that `add == T[i] - max` and `cost == cost[i]`, gold >= cost, and level >= the tier's level (L2 for 20, L3 for 30, and so on).
2. Failure: **sub 9 ok = 0**.
3. Success: deduct gold, max = T[i], persist. Requester **sub 9 {1, cost}** (the client subtracts gold locally, so the server total must match), and **sub 10 {max}** to every online member including the requester (sub 9 does not update M+0x58 [V]).

### F11. Guild chat (0x8D → 0xB5)
1. X → {gid, len, "Name : msg"}. Drop it if X is not in G or gid != G.id (the client sends M+0x44 even when it is 0 or 0xFFFF [V]).
2. Server-side 700 ms throttle. Rebuild the line as `name(X) + " : " + msg` (anti-spoof, as party F8), cut to 87 bytes.
3. Send **0xB5 {len, line}** to every **other** online member on any map. The sender already echoed it locally [V], so sending it to X duplicates the line.

### F12. Guild points
1. On every kill exp gain `d > 0` for X in G (and not GM), compute `gp = guild_points(d)` [I; the single retail sample is 41 exp → 20 GP, so start with `floor(d/2)` behind a config knob]. Send it as the 0x21 tail. Add it to `session.pending_gp`.
2. On logout of X: `G.points += pending_gp`, persist, and send **sub 21 {name(X), G.points}** to every other online member. The client prints "[X] has logged out." and "Guild point(+pending : [X]) increased.", which matches retail [V client; I retail policy].
3. There is no sub-code that pushes points to online members during play [V]. Their windows update at the next F0 or sub 21.
4. Level-ups of G are shown client-side from points. The server needs the level only for F10.

### F13. Guild Plaza boards (0x88 → 0xBA; expiry → 0xB8)
Blocked in Build 14 by the item-id mismatch (1.6) [I]. The server side for when it becomes reachable (or for GM-seeded boards):
1. X (master) in 9702 → 0x88 (87 B).
2. Server checks: X is the master of gid, X is in 9702, X owns a board item (4280 normal or 4279 premium in the exe's numbering, or 4284/4283 in EN data; see open question 1; after cp-2 the exe numbering *is* 4284/4283), no other active board of X, distance >= 158 px from other boards (as the client).
3. Store the board {expires = now + 1 h (normal) or 24 h (premium)} [I from the KR item text "사용기간은 1시간/24시간"].
4. Broadcast **0xBA** (the same body) to everyone in 9702.
5. Reply **sub 185 {1, item}**: for 0x10B8 the client then sends C2S 0x15 {0x10B8}, which consumes the item through the normal use path [I flow]. After cp-2 the item is 4284 (0x10BC), echoed as C2S 0x15 {4284}.
6. On expiry or disband send **0xB8 {gid}** to 9702.
7. A player entering 9702 gets **0xBB** (all live boards) after the 0x8A reply.

### F14. Must-reply summary and soft-lock rules
| C2S | Left without a reply | Minimal fallback |
|---|---|---|
| 0x8A | guild window empty, local tag stale, apps/boards lost | sub 15 result 0 (current) |
| 0x87 | phantom pre-filled guild until the next map load; the user sees nothing | sub 1 result 3 ("Fail to register the guild") |
| 0x96 | **modal "Waiting for the server to respond." (window 0x16) stays up** [V box; soft-lock I] | sub 23 result 0 ("Fail to register the Guild Battle") or sub 34 result 0 (silent close) |
| 0x9C | **same waiting box** [V] | sub 34 result 0/1 |
| 0x92 | grade buttons stay visible | sub 16 result 0 |
| 0x89, 0x8B, 0x8E, 0x8F, 0x90, 0x91 | no feedback (no modal) | sub 2 0 / sub 5 other / sub 18 0, sub 6 0, sub 8 0 / sub 7 0 / sub 9 0 / sub 11 other |
| 0x8C, 0x8D, 0x9B, 0x9D | nothing expected | none |

Today 0x96 cannot be produced: it needs an own member record with grade >= 3, which needs sub 3. **The first stage that sends sub 3 must also add the 0x96/0x9C fallbacks** [V gate; I plan].

---

## 6. Error-code index (by sub-code)

| Sub | Values the client distinguishes |
|---|---|
| 1 | 1 ok (+u16), 2 name, 4 GM, 5 other guild, 7/8/9 prerequisite (wrong text), anything else "Fail to register" |
| 2 | 1 applied, 6 full, 0x10 master push (+str[130]), 0x11 delayed, others silent |
| 5 | 1 completed, 5 other guild, others "can't recruit" |
| 6 | 0 failed, 0xC one day, others = left |
| 7 | 0 failed (blanks notice), 1 ok, others silent |
| 8 | 0xA kick first, 0xB seven days, 1 deleted, others silent |
| 9 | 0 failed, non-zero ok (+u32 cost) |
| 11 | 1 ok, 8 not eligible, others failed |
| 15 | 1 load failure (0xFFFF), others not in a guild |
| 16 | 1 ok, 0xD Guardians, 0xE Vanguards, others failed |
| 18 | 0 failed, others ok |
| 23 | 0 failed, 2 manner, 3 class-change text **then success**, others success |
| 25 | 2 manner, 3 class change, 4 Flea Market, 6 cash shop, 7 other GB, 8 party, 0xF0 added, others "other service" |
| 26/27 | 0 = no empty place |
| 30 | kind 2 = stopped; kind 1 carries a uid |
| 34 | 1 already/not registered, 2 same guild, others silent |
| 35 | 1 win, 2 loss, 3 draw |
| 185 | flag 1 (+item), others "Guild master can only do this." |

Sub-25 message VAs [V]: 2 `0x5289F8`, 3 `0x5289A8`, 4 `0x528620`, 6 `0x528694`, 7 `0x528960`, 8 `0x52865C`, default `0x528A30` (jump table 0x47FC88/0x47FC64 decoded).

---

## 7. Persistence and server state

Guilds are account-independent, so they need their own store. Either a `guilds` top-level section written by `store.py` with the same atomic-write and debounce guarantees, or a separate `guilds.json` behind the same `Store` pattern [I].

```python
GUILD_ID_MIN, GUILD_ID_MAX = 2, 0xFFFE       # 0 none, 1 = GM marker, 0xFFFF = load failure [V]
GUILD_CREATE_GOLD = 50_000                    # [V] 0xC350 / 0xFFFF3CB0
GUILD_CREATE_LEVEL, GUILD_BASE_MAX = 30, 15   # [V]
GUILD_LEVEL_INC = (405450, 1013625, 1925887, 3294280)   # [V] 0x5250B8
GUILD_CAP_BY_LEVEL = {2: 20, 3: 30, 4: 40, 5: 50}       # [V] 0x5250C0
GUILD_TIERS = ((20, 6500), (30, 20000), (40, 60000), (50, 100000))  # [V] 0x5250C8/0x5250D8
MAX_GUARDIANS, MAX_VANGUARDS = 4, 10          # [V]
GRADE = {1: 'Trainee', 2: 'Soldier', 3: 'Vanguard', 4: 'Guardian', 5: 'Master'}  # [V]
GB_ROSTER = 6                                 # [V] "(%u/%u)" with 6

guild = {
  'id': int, 'name': str(<=16 B), 'created_at': ts,
  'master': char_name, 'points': u32, 'max_members': 15..50, 'notice': str(<=60 B),
  'emblem_fg': u16, 'emblem_bg': u16, 'wins': u16, 'losses': u16, 'draws': u16,
  'members': [{'name', 'uid', 'grade', 'joined_at'}],        # join order
  'applications': [{'name', 'uid', 'level', 'job1', 'job2', 'at'}],  # FIFO [V order matters]
}
character += {'guild_id': 0}                  # authoritative back-reference
session   += {'client_guild_id': 0,           # what THIS client holds at entity+0x12 (0x21 tail)
              'pending_gp': 0,                # credited at logout (sub 21)
              'last_guild_chat': 0.0}
world     += {'boards': {gid: {...0xBA fields, 'expires_at'}}}   # in memory, 9702 only
```

- The member `online` byte is the channel number when online and 0 when offline [I]. The client's own record at creation uses scene+0x21D, which FUN_00448730 case 9 also passes as the channel [V read; meaning I].
- Level and job come from the character store at send time. There is no push when a member levels up [V: no such sub-code].
- The name index must be unique. The case rule is unknown [I: case-insensitive].
- A character rename (P8 cash rename item) must update `members[].name`, `master` and `applications`, then send **sub 22 {old, new}** to online members [V client handler; I trigger].

---

## 8. Guild ids in records and sends (server work outside the guild module)

1. `records.py` 0x04/0x05/0x07 builders: `gm_or_guild_id = 1` for a visible GM, otherwise `G.id`, followed by the 21-byte block. Both must use the same rule. For a GM who is also in a guild the GM tag wins [I]: the client cannot show both.
2. 0x2B/0x2C/0x2E arena rosters: `guild_id` and the block (no gm_hidden) [V].
3. 0x52 info view: `guild_id` + name, label 4 [V].
4. 0x21 builder: `local_player_guild_id > 1` = `session.client_guild_id > 1` (the existing `_receiver_guild_id` hook).
5. Disconnect: F12 step 2 (sub 21) plus the same bookkeeping as party F5.

---

## 9. Guild Battle (depends on P9 room model and P10 matches)

All UI windows, builders and sub-codes below are [V]. The overall choreography is [I] (the KR server was not observed).
- Windows: 0x4D0 "register/roster" (room name input 4, members 8..13, "(0/6)" 7, "Invite Player" 20, "Apply Battle" 21, "Exit" 22, maps 26 "Popola" / 27 "Mt.Marble" / 28 "Amakusa" / 29 "Balderan"), 0x4D1 roster list (joiner view, "waiting", Cancel), 0x4D2 member picker ("Invite" 18), 0x4D3 create confirm, 0x4D4 cancel confirm, 0x4D6 "Guild Battle challenge", 0x4D8 invitation (Accept 3 / Refuse 4), 0x4D9 room list ("Waiting Guilds" `"%u (%u %s)"`, 8 rows, "Member change" 13).
- Local state: entity +0xE9 (room type) = 7 and +0xEC = registration/room id while registered (sub 23). **0x9B clears both** (0x485F2D) [V]. Clicking another player whose room sign is set (+0xEA != 0) and whose +0xE9 == 7 calls FUN_00485cb0, the same as the Guild Battle button (FUN_00450130 room-sign path; spec 0x4506A6) [V].

| Step | C2S | Server → client | Notes |
|---|---|---|---|
| Register | 0x96 (empty) | **sub 23 {result, value}** (hides the waiting box). Success (any value other than 0/2/3; 3 also succeeds after showing a message): +0xE9 = 7, +0xEC = value, show 0x4D0 (self added to the roster), hide 0x4B3, fill and show picker 0x4D2 with online members except self | value = registration id [I]; broadcast the 0x07-style room sign? [I] |
| Invite a member | 0x97 {uid, name} (then Sleep(500) and box `0x52876C "That guild member has been invited."`) | invitee: **sub 24 {id, name, value}** → 0x4D8 (needs to be in a guild). Leader: **sub 25 {id, name, reason}** | reason 0xF0 adds the name to 0x4D0; others are refusal texts (section 6) |
| Accept invitation | 0x98 {id, name, value} (echo of sub 24) | joiner: **sub 26 {1, last, pairs}** → 0x4D1 roster; others with 0x4D0/0x4D1 open: **sub 25 0xF0**; no place: **sub 26 {0}** or **sub 27 {0}** | FUN_00485b50 adds only names that are in the receiver's own member list, at most 6 [V] |
| Leave / remove | (0x9B / disconnect) | **sub 30/31 {1, uid}** removes the entry; **{2}** stops everything | sub 31 also clears 0x4D9 ("One member left...") |
| Create room | 0x99 {i32 map 9801..9804, str[17] title} | roster: **sub 28 {n, in_progress, rooms}** → 0x4D9 | **Sleep(500) per 28/29 packet** [V] |
| Room list updates | - | **sub 32** add (kind 1: b, a) or remove (kind 2 / room_id) | record +2 = room title, +0x13 = guild name [I from 0x9A's check `+0x13 != own guild`] |
| Challenge | 0x9A {room_id, guild name} (then Sleep(500) and `0x528A7C "Guild Battle invitation has been sent\r\nto other guild."`) | target room leader: **sub 33 {room_id, challenger}**. Errors to the challenger: **sub 34** 1/2 | the client refuses its own room and its own guild name [V] |
| Accept challenge | 0x9C {room_id} + waiting box | both rosters: **S2C 0x2F** RoomGameEnter `room_mode 7` (P9); on error **sub 34** | P9 must decide which packet closes box 0x16 on success [I] |
| Leave room list | 0x9D (empty) | none traced | the client returns to 0x4D0 on its own |
| Match | (P10 UDP / room host) | 0x36 results; draw prints "Draw. Guild Battle is closing." [V]; final: **sub 35 {1/2/3}** to all online members of both guilds, persist W/L/D, broadcast the pre-formatted win line via 0x15 [I] | C2S 0xBC (room-host result) is dead code [V] |

Other gates [V]: FUN_00445c00 (manner < -19, level, party frame 0x75 open, "Only able during stop motion.", "Not during hunting/battle.", open room windows) and FUN_00445ee0 (`0x5285D8 "You can't play while you are registering/waiting for the Guild Battle."`). These block other PvP entry while registered.

---

## 10. Live test plan

Rules (feedback_experiments.md): one change at a time, baseline first, at least 3 runs.
- **Never kill a monster while an injected guild state (sub 3 / 0xB4 on self with id >= 2) is active**: the server's 0x21 builder still assumes no guild, and the client would read 4 bytes too many.
- Clean up with `sendspec B3 '{"sub":15,"s15_result":0}'` or a portal.
- Clients: `WindSlayer2009\WindSlayer_patched.exe` (A, test/test) and `WindSlayer2009\WindSlayer_p2.exe` (B, admin/admin).
- The field names below are the protocol_spec_2009 grammar keys (sub 3 `member_job2` is really job1, see 1.1).

### 10.1 Single-client injection (possible today)
| Test | Action | Expected |
|---|---|---|
| T-8A | take any portal with `wsdev cap 3` | capture C2S 0x8A (0 B) after 0x2F/0x63; the server log shows sub 15 |
| T-B3-3 | `sendspec B3 '{"sub":3,"guild_id":7,"guild_name":"Testers","guild_points":434950,"max_members":20,"guild_notice":"","emblem_fg":18,"emblem_bg":18,"battle_wins":0,"battle_losses":4,"battle_draws":1,"member_count":2,"repeat[member_count]":[{"member_id":1,"member_grade":5,"member_job2":1,"member_job1":0,"member_level":46,"member_online":1,"member_name":"TestHero"},{"member_id":2,"member_grade":1,"member_job2":2,"member_job1":0,"member_level":18,"member_online":0,"member_name":"Ghost"}]}'` (150 B) | yellow chat line "[Guild Master]:Hello. Welcome to Testers guild."; own nameplate shows "Testers" + emblem; Community → Guild tab: "Testers   Lv.2", "(2/20)", about "(2.91%)", "5Round 0Win 4Lose 1Draw", TestHero Master (Kick button visible), Ghost Trainee offline |
| T-B3-3dup | repeat T-B3-3 without a portal | every member listed twice, "(4/20)" (proves "append, no clear") |
| T-CONVERSE | select Ghost → Converse | box "<Ghost> is not on line." |
| T-B3-20/21 | `{"sub":20,"s20_member_name":"Ghost","s20_online":1}` then `{"sub":21,"s21_member_name":"Ghost","s21_guild_points":435000}` | "[Ghost] has logged in." / "[Ghost] has logged out." + "Guild point(+50 : [Ghost]) increased." |
| T-B3-13/14/17/12 | add "Newbie" (sub 13), set grade 4 (sub 17), make master (sub 12), remove (sub 14) | list updates; after sub 12 TestHero shows Trainee and the Kick button reads "Leave" |
| T-B3-16 | click a grade button under `cap 3` | capture C2S 0x92 (24 B); then inject sub 16 {13} | box "You can only set 4 Guardians."; buttons hide |
| T-B3-19 | `{"sub":19}` | the client sends 0x8A once; the server answers sub 15 (no loop); the guild window is empty afterwards |
| T-B3-37 | `{"sub":37,"s37_uid":1}` | own nameplate "Game Master" |
| T-B3-2/4 | `{"sub":2,"s2_result":16,"s2_notice":"<\x00 + 'Alice' + pad + level/job u16 at +0x6E>"}` | HUD notification icon appears; clicking it opens 0x4BC "Alice / Level : 20 / Class : ..."; Accept sends **0x8B "Alice"**, Deny sends **0x8C** |
| T-B3-6 | `{"sub":6,"s6_result":1}` | window closes, tag gone, "You are out of this guild." |
| T-B4/B6/B7 | with a dummy remote player (party T-DUMMY 0x05 uid 2): `sendspec B4 '{"uid":2,"guild_id":9,"guild_name":"Rivals","emblem_fg":18,"emblem_bg":18}'`, then B6, then B7 | yellow "Rivals" + emblem under Dummy; right-click Dummy shows menu entry 12 enabled (A not in a guild); B6/B7 remove it |
| T-JOIN-MENU | after T-B4, menu entry 12 → OK under `cap 3` | capture **0x89 `09 00 00 00`** (u32) |
| T-B5 | `sendspec B5 '{"text_len":12,"text":"Ghost : hiya"}'` | yellow chat line; check which chat tab shows it (Guild?) |
| T-GCHAT | after T-B3-3, type `/g hello` | local yellow echo "TestHero : hello"; capture **0x8D `07 00 10 'TestHero : hello'`** |
| T-0x21 | after T-B3-3, kill one mob with a server build that sets `client_guild_id` = 7 | "(+N) guild points are gained." with no desync |
| T-GB-WAIT | after T-B3-3 (with grade 5 and 6 fake online members via sub 3): Guild Battle button | capture **0x96**; the "Waiting for the server to respond." box stays; `sendspec B3 '{"sub":34,"s34_result":0}'` closes it |
| T-BA/BB | stand in 9702: `sendspec BB` with 2 boards (item 4280 and 4279; **4284 and 4283 on a cp-2 exe**) | two boards (sprites 0x145/0x146) with white text and a green guild name; clicking one opens 0x4B6 and OK sends **0x89 u16** |
| T-B8 | `sendspec B8 '{"guild_id":<id>}'` | that guild's boards disappear |
| T-BOARD-ITEM | GM-grant 4284 "Guild Billboard" and 4280; use each in 9702 | predicted [I]: 4284 → C2S 0x15 {4284} (no dialog); 4280 (Rider's Bow) → equip path. This decides open question 1. On a cp-2 exe: 4284 → dialog 0x4B7 |
| T-B3-185 | `{"sub":185,"s185_flag":1,"s185_item_id":4280}` (stock exe); `4284` on a cp-2 exe | capture C2S 0x15 `B8 10` (stock) / `BC 10` (cp-2) |
| T-MOIBA | walk to 9702 (portal from 801/1001/1101), click Moiba | menu 0x4BB; "Guild make" (or "Break Guild" when the master); control 5 → quantity dialog for Moiba's hni row: 4283 "(0Gold)" on the stock hni, 4284 "(1000Gold)" after cp-5. OK sends C2S 0x0B {item, qty, **npc 0**} (the close zeroes +0x11C at 0x497EA5 before the read at 0x474315; 1.5 control 5). No longer refused (P15 guild-g6): on 9702 the server names Moiba itself (`_board_sale_npc`) and sells the Guild Billboard **4284** for 1,000 gold each on the cp-2 exe (0x18 {gold, victy, 4284, qty}; a 0x15 price line only when the dialog named 4283). Off 9702, npc 0 is refused ("That item is not for sale."); on the stock exe (`CLIENT_ITEM_IDS "kr"`) "Guild billboards are not available." |

### 10.2 Two-client end-to-end (after stages G1..G5)
Setup:
- A = TestHero: Lv >= 30, job2 != 0, >= 60,000 gold (GM `/mony`, `/expexp`, or edit accounts.json).
- B = the admin char: Lv >= 1, no guild.
- Both on map 9702.

1. **Create**: A → Moiba → Guild make → name "Testers", emblem → Regist → Create.
   - A: "Guild has been registered.", gold -50,000, welcome line, window "(1/15)".
   - B sees "Testers" under A (0xB4).
   - Relog A: still in the guild (persistence).
2. **Apply**: B right-clicks A → entry 12 → OK: B gets "You have applied for this guild."; A's HUD icon appears.
3. **Accept**: A clicks the icon → Accept. A gets "Guild admission has completed." and the list "(2/15)". B gets the welcome line and a tag. A sees B's tag, and B sees A's.
4. **Chat**: B `/g hi`. A sees the yellow "admin : hi" once. B sees only its local echo (no duplicate).
5. **Points**: B kills a mob and sees "(+N) guild points are gained.". B logs out: A sees "[B] has logged out." + "Guild point(+N : [B]) increased.". B logs in: A sees "[B] has logged in.".
6. **Grades**: A sets B to Guardian: A gets the box, and both lists show "Guardian" (sub 17).
7. **Notice**: A edits the notice. After a portal, both get "[Guild Master]:<notice>".
8. **Capacity**: with points seeded to Lv2 (admin), Increase Capacity shows "add by 5", cost 6,500. OK: gold -6,500 on both server and client, "(2/20)".
9. **Master change**: A → B: both lists swap; A becomes Trainee; the Kick/Leave captions swap after refresh.
10. **Kick**: the new master B kicks A. A gets "You are out of this guild." and loses the tag; B sees "Kicked that guild member." and "(1/20)".
11. **Leave**: re-join A, then A leaves: "You can leave the guild after 1 day." if the rule is enabled, otherwise success.
12. **Disband**: B (alone) → Break Guild: "Your guild has been deleted.", tag gone for both viewers.
13. **Negative cases**: A below Lv30 gets "Cause: (Level)" (client-side, no packet); duplicate name gets "You can't use this guild name."; applying to a full guild gets "This guild can't recruit any more members.".

---

## 11. Implementation plan (stages, each testable)

| Stage | Title | Depends on | Opcodes | Exit test |
|---|---|---|---|---|
| G0 | Spec/codec fixes: rename the sub 3/13 job fields in the server's protocol_spec_2009 copy, fix the 0x88 length (87), add builders for 0xB3 subs, 0xB4..0xBB, and a guard that forbids S2C 0xB9 and sub 19-as-reply | none | 0xB3, 0xB4..0xBB | unit tests: grammar round-trip of every sub; sub 3 with 2 members = 150 B, sub 13 = 27 B, 0xBA = 87 B |
| G1 | Guild store + read path: `guilds` persistence, `character.guild_id`, `session.client_guild_id`; 0x8A → sub 3 (+ sub 4 for masters) or sub 15 (+ sub 37 for GM); guild block in 0x04/0x05/0x07/0x2B/0x2C/0x2E/0x52; 0x21 tail from `client_guild_id`; **0x96/0x9C fallbacks (sub 23 0 / sub 34 0)**; admin command to seed a guild | F3/F4/F5 (done in P1..P5) | 0x8A, 0xB3/3/4/15/37, 0x04/0x05/0x07, 0x21, 0x52 | seed "Testers" with A+B: both tags visible to each other, windows populated, welcome line on every portal, kills print guild points with no desync, T-GB-WAIT closes |
| G2 | Create and disband at Moiba: 0x87 → sub 1 + sub 3(n=0) + 0xB4; 0x8E action 3 → sub 8 + 0xB7 + 0xB8; name/emblem validation, 50k gold debit | G1 | 0x87, 0x8E(3), 0xB3/1/3/8, 0xB4, 0xB7 | 10.2 steps 1 and 12 |
| G3 | Membership: 0x89 (both lengths) → sub 2 (+0x10 master push / 0x11), FIFO applications, 0x8B/0x8C → sub 5/13/3(or 19), leave/kick → sub 6/14/18 + 0xB7, login/logout sub 20/21 | G2 | 0x89, 0x8B, 0x8C, 0x8E(1,2), subs 2/4/5/6/13/14/18/19/20/21 | 10.2 steps 2, 3, 10, 11 |
| G4 | Management: 0x92 → sub 16/17 (caps), 0x91 → sub 11/12 (old master → grade 1), 0x8F → sub 7, 0x90 → sub 9/10 with server-side tier/level/gold checks, rename hook → sub 22 | G3 | 0x8F, 0x90, 0x91, 0x92, subs 7/9/10/11/12/16/17/22 | 10.2 steps 6..9 |
| G5 | Chat and points: 0x8D → 0xB5 relay to others (name rebuild, 87 B cap, 700 ms), guild points accrual + logout credit, level derivation | G3 | 0x8D, 0xB5, 0x21, sub 21 | 10.2 steps 4..5 |
| G6 | Guild Plaza: 0xBB after the 0x8A reply in 9702, board store with expiry → 0xB8, 0x88 → 0xBA + sub 185, Moiba purchase via 0x0B (decide the item id: EN 4283/4284 vs exe 4279/4280; with cp-2 they are the same), board ids from the `CLIENT_ITEM_IDS` setting (CLIENT_PATCH_SET_RE_2026-10-06.md 8.5), GM command to place a board | G2; T-BOARD-ITEM result | 0x88, 0xBA, 0xBB, 0xB8, sub 185, 0x0B | T-BA/BB live; boards survive re-entry; expire on schedule |
| G7 | Guild Battle: registration/roster/room list/challenge (subs 23..35), room mode 7 via the P9 room model, match results via P10, W/L/D persistence, sub 35, win/draw broadcast (0x15, pre-formatted) | P9, P10, G4 | 0x96..0x9D, subs 23..35, 0x2F(7), 0x36, 0x15 | two guilds of 6 (or a reduced `GB_ROSTER` debug setting) register, challenge, fight, and get W/L updated in both windows |

Suggested order: G0 → G1 → G2 → G3 → G5 → G4 → G6. G7 comes after P10. G1 alone already fixes the retail-visible parts: tags, window and guild points.

---

## 12. Open questions

1. **EN item-id shift.** The exe hardcodes billboards 4279/4280 (and pet items 0x10B9..0x10BD). The EN 2009 hii has them at 4283/4284 (and Moiba sells 4283). Is the billboard feature dead in Build 14 (T-BOARD-ITEM)? If so, the server should seed boards itself (GM/admin) or the item table needs a client-side data patch. This is shared with the pet group (`_work/pet/align_kr_en.txt`). **Answered by G-CP cp-2** (an exe patch, not a data patch): the six board constants move to 4284/4283 (CLIENT_PATCH_SET_RE_2026-10-06.md 8). It is built but not installed yet, so the server must follow whichever exe is installed.
2. **Guild point formula and sources.** There is one sample: 41 exp → 20 GP (2010 client). Do quests or PvP give points? Was the credit really deferred to logout (the client text suggests yes), and what happens on a crash?
3. **Permissions.** Who may change grades, invite, or place boards besides the master? The client gates only Kick/Break/Capacity/Master/Notice/boards on "own name == M+0x64".
4. **Member `online` byte.** Is it the channel number or a boolean? What does the client do with values > 1 beyond the icon?
5. **0xB6 vs 0xB7.** Which event used which (leave/kick/disband)?
6. **Application record.** 130 bytes, but only +1 (name) and +0x6E (level*100 + job2*10 + job1) are read. Is byte 0 or +0x12..+0x6D a message?
7. **Sub 2 result 0x11 "admission is being delayed".** Is it "master offline" or a cooldown?
8. **Application delivery.** Was sub 4 sent after every map load (the client clears M+0xE0 on each 0x03) or only at login?
9. **Retail rules.** The 1-day leave and 7-day delete rules; the conditions for master-change result 8; the guild name charset and case rules; emblem value ranges (frame counts of sprites 0x148/0x149).
10. **Chat-tab filtering.** Does the "Guild" chat tab filter by colour 0xFFFFC800 (so 0xB5, sub 3/6/20/21 lines all land there)?
11. **Window 0x50 control 12.** It exists at runtime (enabled/greyed by FUN_00450130) but not in the decoded hui (11 controls). What is its caption?
12. **Guild Battle.** The meaning of s23_value (+0xEC), s24 id/value, and the room record names. Which packet closes the 0x9C waiting box on success? Sub 30's 0x4D0 branch compares the window id with 0x15 and seems never to update the "(n/6)" label on 0x4D0 (client bug?). How did the Build 14 server announce wins (no format string in this exe)?
13. **Level/job changes of online members.** No sub-code carries them. Are they only refreshed by the next map load (sub 3)?
14. **GM who is also a guild member.** entity+0x12 can hold only one value. The design shows the GM tag.
