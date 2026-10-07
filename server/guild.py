#!/usr/bin/env python3
"""
guild.py - the 2009 guilds (P14 guild-g0, g1 and - stage 4 - g2, g3, g5, g4; re_tools/docs/
systems_2009/guild.md, ROADMAP_2009_ADDENDUM P14)
=========================================================================================
    gs.guilds = Guilds(gs)                        # GameServer.__init__ (after the resync bundle)
    gs.guilds.tag_of(char)                        # records' GuildTag (arch09-roster-record) | None
    gs.guilds.send_sub(sock, s, sub3(..), reply_to=0x8A)   # THE guarded 0xB3 sender
    gs.guilds.points_for(guild_base)              # the 0x21 tail value (F12)
    gs.guilds.create / apply / accept / reject / member_action / chat / change_grade /
        change_master / change_notice / increase_capacity (sock, session, rec)   # C2S 0x87..0x92

Client (EN 2009 Build 14 only; the 2008 client has no guild opcode, no 0xB3..0xBB handler and
no tail on 0x21, so nothing here runs on a 2008 server - `supported()` - and no guilds.json is
opened there): the guild lives on CMessenger M (M+0x44 id, +0x78 members, +0xE0 applications,
+0x114 boards, guild.md 1.1) and EVERY S2C 0x03 resets it; the client then sends C2S 0x2F, 0x63
and 0x8A (0x453581), and the 0x8A reply must restore it (F0). The nameplate is entity+0x12:
1 = "Game Master", > 1 = the guild name + emblem (FUN_0043c300, guild.md 1.2).

guild-g0: the codec (section 11 G0)
-----------------------------------
- protocol_spec_2009.json: 0xB3 sub 3 member record = grade, JOB1, JOB2 and sub 13 = ..., level,
  JOB2, JOB1 (the names were swapped; record +0x1A = job1 / class, +0x1B = job2 / tier [V]
  0x484484..0x4844AA, class icon 0x53E4C0[7*job2 + job1] at 0x491D77); C2S 0x88 is 87 bytes.
- One builder per 0xB3 sub-code (`sub1` .. `sub37`, `sub185`) and per 0xB4..0xBB (`entity_set`,
  `chat_line`, `entity_clear`, `board_remove`, `board_add`, `board_list`): each returns the
  (spec key, fields) pair packets.build / send takes, with the client's limits checked (sub 26's
  stack cookie, the u8 counts, the one-frame payload, the 130-byte application record).
- Guards: S2C 0xB9 has no handler anywhere (primary default 0x45DE1A, SubHandler3/4/5 [V]) -
  packets.FORBIDDEN_S2C refuses it for every send, raw ones included; 0xB3 sub 19 makes the
  client send C2S 0x8A again (0x48456D), so as a REPLY to 0x8A it is an endless loop [V] -
  packets.FORBIDDEN_REPLIES refuses it in GameServer._send_encrypted while a 0x8A is being
  dispatched, and send_sub(reply_to=0x8A) refuses it before that.

guild-g1: store, the 0x8A reply, the records, the 0x21 tail, the soft-lock fallbacks
------------------------------------------------------------------------------------
- Store: GUILDS_FILE (default guilds.json) next to accounts.json, one JSON with every guild:
      {"version": 1, "last_id": n, "guilds": [{id, name, points, max_members, notice,
        emblem_fg, emblem_bg, wins, losses, draws, created_at,
        members: [{cid, name, grade, joined_at}]   (join order; ONE grade 5 = the master),
        applications: [{cid, name, level, job1, job2, at}]   (FIFO: the client pops its FIRST)}]}
  Members are keyed by the character's stable `cid` (ROADMAP_2009_ADDENDUM C4 / X14): the name,
  the account uid, level and class are read from the account store at send time, so a rename
  needs no rewrite and a deleted character's row is skipped on the wire and dropped at the next
  load (store.retired_cid: a cid is never reused). accounts.json is NOT changed. The load
  normalizes the file (migrate: types, ranges, one master, unique ids / names / members) and
  resolves every cid; when that changes an existing file, its original bytes go to the one-time
  guilds.json.bak-pre-p12+p14 first, and a LATER load that drops rows (a member / application /
  guild the account store does not know) keeps its own guilds.json.bak-<time> (`census`).
  Writes are atomic (store.atomic_write) from the tick thread, like the boss ledger. Ids
  2..0xFFFE, never reused (`last_id`).
- C2S 0x8A -> resync.STAGE_GUILD (arch09-resync-bundle, guild F0): step 'guild' = sub 3 with the
  whole member list (uid = account uid, grade, job1 = class, job2, level from exp, online = the
  member's channel or 0, name) for a member, else sub 15 {0}; step 'guild_apps' = sub 4 with the
  pending applications (FIFO, <= 15 per packet) to the master - the 0x03 emptied the client's
  list; step 'gm_tag' = sub 37 {own uid} for a visible GM (records.gm_tag_visible), last. 0xBB
  (the 9702 boards) is P15 guild-g6's step 'guild_boards' between them (boards.py).
- Records (records.guild_block): the member's tag in 0x04 / 0x05 / 0x07 and the name in 0x52.
- 0x21 (GameServer._exp_delta_packet): the u32 guild_points tail exactly when the receiver's
  client-side guild id (clientview, arch09-receiver-mirror) is > 1 and the delta > 0. Its value
  is GUILD_POINTS_PCT of the PRE-multiplier exp of an earned grant (award_exp's guild_base, X9;
  [I] the one retail sample 41 exp -> 20 GP), 0 for a GM / penalty grant (the tail must still be
  there). The client only prints it; the amount accrues in session['guild_pending_gp'], which
  guild-g5 credits at logout (sub 21) - g1 credits nothing.
- Soft-lock fallbacks (F14): C2S 0x96 (Guild Battle register: "Waiting for the server to
  respond." box 0x16) -> sub 23 {0, 0}; C2S 0x9C (challenge accept, the same box) -> sub 34 {0}
  (both hide box 0x16 first; registry BUILD_MUST_REPLY keeps the same bytes as the backstop).
- Admin seeding: `!guild seed <name> <master> [member ...]` and the other `!guild` test aids
  (dev_guild). Moiba's 0x0B billboard sale is P15 guild-g6 (GameServer._buy_guild_board, boards.py).

guild-g2 .. g5, g4: the flows (P14 stage 4; guild.md F1-F12, section 11 G2-G5)
--------------------------------------------------------------------------------
Every request is answered with the sub-code its client waits for (F14), on every path:
- g2 create C2S 0x87 (Moiba, map 9702 only) -> sub 1 {result[, id]}. Gates in F1's order: in a
  guild 5, visible GM 4, not on 9702 3, name (names.check, unique case-insensitive) 2, gold <
  50,000 7, Lv < 30 8, no 2nd class 9, emblem outside fg 0..999 / bg 0..599 3 [V]
  (EMBLEM_FG_MAX / EMBLEM_BG_MAX = the sprite sheets; only an empty picker cell gives more).
  Success: 50,000 gold debited (the
  client subtracts it itself on sub 1 {1}), sub 1 {1, id}, sub 3 with member_count 0 (name /
  points / max / notice / emblem, the welcome line and the own plate - NOT the pre-filled own
  record twice), 0xB4 to the clients that hold the creator. Disband 0x8E action 3 -> sub 8:
  0xA with members left, 0xB under GUILD_DISBAND_MIN_DAYS, else 1 + 0xB7 + 0xB8 {gid} to 9702.
- g3 apply 0x89 (u32 or u16 guild id: the registry tells the two send sites by length) -> sub
  2: 1 (master online; he gets sub 2 0x10 + the 130-byte record when his list is synced),
  0x11 (master offline [I]: sub 4 at his next map load), 6 (full / unknown guild), 0 (what the
  client refuses itself). Applications: FIFO, one per applicant and guild; a stale one (the
  applicant joined a guild) stays until it is popped or the master's next sub 4 sync, so the
  client list and the server list pop in step. An application keeps the name the master's
  client was given (a rename of the applicant does not rewrite it: the client's record still
  shows the old one) until that sync refreshes it. Accept 0x8B {name} (found by that name) ->
  sub 5 {1} + sub 13 to the members' windows + sub 3 (the whole list) to the new member + 0xB4;
  {5} it is in a guild now; {0} full / unknown. Reject 0x8C pops applications[0], no reply (none
  while the master's list is stale: see master change). Leave 0x8E
  action 2 -> sub 6 {1} + sub 14 to the others + 0xB7 (the master gets {0}; {0xC} under
  GUILD_LEAVE_MIN_HOURS). Kick action 1 (master only) -> sub 18 {1} + sub 14 to the remaining
  members + sub 6 {1} to the kicked client + 0xB7; {0} otherwise. A visible GM member gets sub
  37 after its own sub 3 / 6 / 8 (they overwrite entity+0x12 = 1). Login: sub 20 {name,
  channel} to the others on the connection's first entry; logout (world.ON_LOGOUT): the login's
  guild points credited, sub 21 {name, total}. A 2009 channel hop (arch09-session-continuity)
  sends neither, and the points earned before the hop wait by cid (departed) for the real
  logout.
- g5 chat 0x8D -> S2C 0xB5 "<real name> : msg" (<= 87 B) to every OTHER member in world, any
  channel; dropped: not that guild, within GUILD_CHAT_MIN_SECS (0.7) of the sender's last line,
  empty; a member who blacklisted the sender is skipped. Guild points: the 0x21 tail of
  award_exp (GUILD_POINTS_PCT of the GUILD_POINTS_BASE exp, 'pre' = before the cash item and
  the event multiplier, X9), credited at logout. Leaving / kicked / disbanded forfeits the
  uncredited points [I] (a silent credit would show up in the next sub 21 under another name).
- g4 grade 0x92 -> exactly one sub 16 ({1}, 0xD at 4 Guardians, 0xE at 10 Vanguards, {0}) + sub
  17 {name, grade} to the windows; who: GUILD_GRADE_MIN_GRADE (default the master). Master
  change 0x91 -> sub 11 {1} + sub 12 (old master grade 1, as the client's FUN_00485200) + the
  pending applications (sub 4) to the new master; {8} not a member / not eligible
  (GUILD_MASTER_NEEDS_CREATE_RULES). The OLD master's client keeps its application list (sub 12
  does not clear M+0xE0) but no longer in step with the FIFO: clientview apps_stale until its
  next map load - no push, no sub 4 on a re-promotion (it would append), its 0x8C pops nothing.
  Notice 0x8F -> sub 7 {1} (the others read it at their next
  map load; no push sub-code [V]); {0} non-master / empty. Capacity 0x90 -> the server recomputes
  the client's tier (next_tier: level from the points, the add, the cost) and the gold: sub 9
  {1, cost} + sub 10 {max} to every window incl. the master's; else sub 9 {0}. Rename
  (world.ON_RENAME, C4) -> sub 22 {old, new} to every window (the renamed one's too).
Member DELTAS (sub 10 / 12 / 13 / 14 / 17 / 20 / 21 / 22) go only to a client that holds that
guild's window (clientview guild_list), application pushes only to a master whose list is synced
(apps_held): a client in a map load gets the state from its coming sub 3 / sub 4 instead, so
nothing shows twice. Wire layouts are the 2009 spec's (0xB3 subs, 0xB4..0xB8); the 2008 client
has no guild: no route, no hook, nothing sent.

EN Build 14 has NO "join guild" entry in the player menu [V] (P12+P14 live triage G4; answers
guild.md open question 11): the EN windslayer.hui window 0x50 (the right-click popup) has 11
controls - End, Character name, Char. Info, Trade, Make Party, Whisper, Add as Friend, Converse,
Copy Name, Praise, Report - while the KR 2025 hui has a 12th (Text 8038 = "Guild join request"
in the KR UILngKo.lng, Pos 20 210, Type 18), and the EN UILngKo.lng has no id 8038.
FUN_00450130 walks to the 12th node to enable / grey it and finds none, so the 0x4B6 join dialog
(C2S 0x89 u32) is unreachable from the menu. On a stock EN client a player can apply ONLY
through a Guild Plaza board (0x89 u16; the boards are guild-g6, P15 - server-seeded ones are the
planned stock-client path) or a patched hui (cp-4, user-installed). Until then `!guild apply <guild> <char>` is the GM aid: for an online
character it runs this very 0x89 path (Guilds.apply: sub 2 to the applicant, the 0x10 push to a
synced master), for an offline one it only queues the application.

The receiver mirror and the hard hazards (guild.md 1.1 / 2 / 3, ADDENDUM 4.2 "Never send")
-----------------------------------------------------------------------------------------
send_sub / send_entity are the only way guild packets reach a client. Under the receiver's
clientview lock they refuse: anything before the connection's first 0x03 (SubHandler3 drops it);
subs 1 / 3 / 6 / 8 / 15 before the receiver's own 0x07 of this map load (NULL entity write);
a second sub 3 WITH members since the last 0x03 (it appends: every member twice) - a sub 3 with
none (the F1 create trick) is allowed; sub 19 as a 0x8A reply. Each send then updates the
mirror the client's own handler would: sub 1 {1, id} / sub 3 -> id, sub 6 (left) / sub 8 {1} ->
0, sub 15 -> 0 (0xFFFF for result 1, which nothing sends), sub 37 / 0xB4 / 0xB6 / 0xB7 on the
receiver's own uid -> 1 / id / 0.

Locks: Guilds.lock guards the guild dict and is a LEAF for everything but its own file write
(no store lock, no send, no clientview lock under it): mutations validate against the account
store first and take Guilds.lock only to check and change; readers copy under it and resolve
outside. `tag_of` takes no lock at all: it reads an immutable {cid: GuildTag} map that every
mutation replaces, so the record builders (presence, map load, 0x52) never meet this lock.
Guilds.flow_lock (guild-g3) serializes [validate, change, fan out] of every membership /
management flow and the 0x8A reply's [build sub 3, send]: each client sees each change exactly
once. Order: flow_lock -> store.lock / Guilds.lock (each let go again) -> a receiver's presence
lock / clientview lock -> send_lock. Never a combat lock under it (the gold of a create /
capacity is debited before and refunded after it), and nothing that holds a clientview lock
takes it (the 0x21 path only adds to session['guild_pending_gp']). The world hooks may fire
under world_lock (the 'channel-hop' tick): nothing under flow_lock takes world_lock. Nothing
writes a socket under flow_lock either: it is taken through Guilds._flow(), whose senders only
queue (flush=False; the writer threads are woken) and which flushes the requester's own
connection after letting flow_lock go - a requester whose TCP send buffer is full cannot stall
every other guild flow / 0x8A reply.
"""
import contextlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field

import chat as chatmod
import clientview as cview
import config as cfgmod
import en_content as EC
import gm
import names
import packets as P
import presence
import records as R
import store as storemod
import world as worldmod

log = logging.getLogger('WS')

BUILD_2009 = '2009'

# ----------------------------------------------------------- client constants ---
# guild.md 1.1 / 1.3 / 1.4 / 7 [V] (VAs there).
GUILD_ID_MIN, GUILD_ID_MAX = 2, 0xFFFE             # 0 none, 1 = the GM marker, 0xFFFF = load failure
GUILD_ID_GM, GUILD_ID_LOAD_FAILED = 1, 0xFFFF
CREATE_GOLD = 50_000                               # 0xC350 / 0xFFFF3CB0 (sub 1 deducts it locally)
CREATE_LEVEL = 30
BASE_MAX_MEMBERS = 15                              # the create pre-fill M+0x58 (0x4843D7)
MAX_MEMBERS_CAP = 50
# A stored max below the create's 15 is kept (`!guild max`: the full-guild negative case of the
# P14 exit criteria with two clients); the client just shows "(n/max)" and offers the 20 tier.
MIN_MAX_MEMBERS = 1
LEVEL_INC = (405450, 1013625, 1925887, 3294280)    # 0x5250B8, non-cumulative
CAP_BY_LEVEL = {2: 20, 3: 30, 4: 40, 5: 50}        # 0x5250C0
TIERS = ((20, 6500), (30, 20000), (40, 60000), (50, 100000))   # 0x5250C8 / 0x5250D8
MAX_GUARDIANS, MAX_VANGUARDS = 4, 10
GRADE_TRAINEE, GRADE_SOLDIER, GRADE_VANGUARD, GRADE_GUARDIAN, GRADE_MASTER = 1, 2, 3, 4, 5
GRADE_NAMES = {1: 'Trainee', 2: 'Soldier', 3: 'Vanguard', 4: 'Guardian', 5: 'Master'}   # 0x53E541
GB_ROSTER = 6                                      # "(%u/%u)" with 6; "at least 6 members online"
NAME_MAX = 16                                      # str[17]
NOTICE_MAX = 60                                    # str[61]
EMBLEM_DEFAULT = 0x12                              # the 0x4C3 picker default
JOB1_MAX, JOB2_MAX = 6, 2                          # the 3 x 7 class icon table 0x53E4C0
# The application record (sub 2 result 0x10, sub 4): str[130] read as raw bytes; the client uses
# +1 name[17] and the u16 at +0x6E = level*100 + job2*10 + job1 (FUN_00484580) [V].
APP_RECORD_BYTES = 130
APP_NAME_AT = 1
APP_CLASS_AT = 0x6E
APPS_PER_PACKET = 15                               # (2038 - 2) // 130
MAX_APPLICATIONS = 100
# One frame holds 2038 payload bytes (GameServer.MAX_S2C_PAYLOAD): sub 3 = 98 + 26 n.
SUB3_BASE_BYTES, SUB3_ROW_BYTES = 98, 26
SUB3_MAX_ROWS = (2038 - SUB3_BASE_BYTES) // SUB3_ROW_BYTES      # 74
SUB26_MAX = GB_ROSTER                              # stack cookie at esp+0x220: last <= 24; use <= 5
SUB29_MAX = 8                                      # the 0x4D9 list's rows
# The board item of S2C 0xBA / 0xBB (board+0x42) and 0xB3 sub 185: the exe draws sprite 0x145 /
# 0x146 only for its own two hard-coded ids (0x45DBC3..0x45DD4A), which are the KR ones on the
# stock exe and the EN ones on the cp-2 exe (CLIENT_PATCH_SET_RE 8.5) - config CLIENT_ITEM_IDS
# (en_content.exe_item_id). (board, premium board) per value; board_items() picks one.
BOARD_ITEMS_EN = (0x10BC, 0x10BB)                  # EN 4284 Guild Billboard / 4283 Premium (cp-2 exe)
BOARD_ITEMS_KR = (0x10B8, 0x10B7)                  # the stock exe's KR 4280 / 4279 (guild.md 1.6)
CHAT_LINE_MAX = 87                                 # 0xB5: 88-byte buffer (packets.TEXT_FIELDS)

# 0xB3 sub-codes (guild.md 2).
SUB_CREATE, SUB_APPLY, SUB_INFO, SUB_APPS, SUB_ACCEPT, SUB_LEFT = 1, 2, 3, 4, 5, 6
SUB_NOTICE, SUB_DISBAND, SUB_CAP_RESULT, SUB_CAP, SUB_MASTER_RESULT, SUB_MASTER = 7, 8, 9, 10, 11, 12
SUB_MEMBER_ADD, SUB_MEMBER_DEL, SUB_NOT_IN_GUILD, SUB_GRADE_RESULT, SUB_GRADE = 13, 14, 15, 16, 17
SUB_KICK_RESULT, SUB_REREQUEST, SUB_LOGIN, SUB_LOGOUT, SUB_RENAME = 18, 19, 20, 21, 22
SUB_GB_REGISTER, SUB_GB_INVITE, SUB_GB_ROSTER_ADD, SUB_GB_ROSTER, SUB_GB_NO_PLACE = 23, 24, 25, 26, 27
SUB_GB_ROOMS_FIRST, SUB_GB_ROOMS, SUB_GB_STOP, SUB_GB_LEFT, SUB_GB_ROOM_DELTA = 28, 29, 30, 31, 32
SUB_GB_CHALLENGE, SUB_GB_RESULT, SUB_GB_OUTCOME, SUB_GM_TAG, SUB_BOARD_ITEM = 33, 34, 35, 37, 185
SUB_CODES = tuple(range(1, 36)) + (SUB_GM_TAG, SUB_BOARD_ITEM)
# [V] these write [scene+0x988]+0x12 (the local entity) with no NULL check (0x47DB55, 0x47DA58).
NEEDS_OWN_07 = frozenset({SUB_CREATE, SUB_INFO, SUB_LEFT, SUB_DISBAND, SUB_NOT_IN_GUILD})
REPLY_8A = 0x8A
NOT_IN_GUILD = 0                                   # sub 15: anything but 1 = "not in a guild"

# ---------------------------------------------------- guild-g2 .. g5, g4 ---
GUILD_PLAZA_MAP = 9702                             # Moiba (hni idx 181, UI 0x4BB) stands only here (1.5)
# The emblem range [V] (guild.md Q9, answered by the P12+P14 live triage): basic.hsi sprite 0x148
# (the emblem) has 1000 frames = 50 shapes x 20 colours, sprite 0x149 (the background) 600 frames
# = 30 patterns x 20 colours, and the 0x4C3 picker writes fg = shape*20 + colour, bg = pattern*20 +
# colour (FUN_00480430 case 0x4C3, LAB_00482702 / LAB_00482828; C2S 0x87 sends both as u16 from
# FUN_00484200). The picker's EMPTY cells are still clickable (pattern rows 4-5 give bg 600..999,
# bg-colour row 3 on pattern 29 gives 600..609): no frame exists for them - the 0x4B4 preview
# shows no background - so they stay refused (sub 1 result 3): an 0xB4 / record would push a
# frame index past the sheet to every client on the map (SpriteControl.dll's bounds handling
# is unverified). EN hui window 0x4C3 also places controls 24 / 29 on row-0 cells 14 / 19 (the
# later control wins the click), so index 1 and 6 of each picker cannot be chosen on EN Build 14:
# cosmetic, nothing server-side.
EMBLEM_COLOURS = 20
EMBLEM_SHAPES, EMBLEM_PATTERNS = 50, 30
EMBLEM_FG_MAX = EMBLEM_SHAPES * EMBLEM_COLOURS - 1     # 999 (sprite 0x148: 1000 frames)
EMBLEM_BG_MAX = EMBLEM_PATTERNS * EMBLEM_COLOURS - 1   # 599 (sprite 0x149: 600 frames)
ACT_KICK, ACT_LEAVE, ACT_DISBAND = 1, 2, 3         # C2S 0x8E action (0x4C2 +0x134, FUN_00484bd0)
# Result codes the client tells apart (guild.md 2 / 6). sub 1: 2 name, 3 "Fail to register the
# guild.", 4 GM, 5 other guild, 7/8/9 the prerequisites (the client shows the Wind-master text
# for them - a client bug, harmless; its own gates catch them first).
CREATE_OK, CREATE_BAD_NAME, CREATE_FAILED, CREATE_GM, CREATE_IN_GUILD = 1, 2, 3, 4, 5
CREATE_NO_GOLD, CREATE_LEVEL_LOW, CREATE_NO_JOB2 = 7, 8, 9
APPLY_SILENT, APPLY_OK, APPLY_FULL, APPLY_PUSH, APPLY_DELAYED = 0, 1, 6, 0x10, 0x11      # sub 2
ADMIT_FAILED, ADMIT_OK, ADMIT_IN_GUILD = 0, 1, 5                                        # sub 5 (other: full)
LEFT_FAILED, LEFT_OK, LEFT_TOO_SOON = 0, 1, 0x0C                                        # sub 6
NOTICE_FAILED, NOTICE_OK = 0, 1                                                         # sub 7
DISBAND_SILENT, DISBAND_OK, DISBAND_KICK_FIRST, DISBAND_TOO_YOUNG = 0, 1, 0x0A, 0x0B     # sub 8
MASTER_FAILED, MASTER_OK, MASTER_NOT_ELIGIBLE = 0, 1, 8                                 # sub 11
GRADE_FAILED, GRADE_OK, GRADE_GUARDIANS_FULL, GRADE_VANGUARDS_FULL = 0, 1, 0x0D, 0x0E   # sub 16
KICK_FAILED, KICK_OK = 0, 1                                                             # sub 18
# session key: the clientview map load (ClientView.loads) whose list an admission delivered.
ADMIT_LOAD_KEY = 'guild_admit_load'


def next_tier(max_members):
    """(new max, gold, guild level needed) of the capacity tier the client offers above
    `max_members` (FUN_00484f40: the first of 20 / 30 / 40 / 50 above it, for 6,500 / 20,000 /
    60,000 / 100,000 gold; a tier of N members needs the guild level whose cap it is: 20 -> Lv 2,
    30 -> Lv 3, 40 -> Lv 4, 50 -> Lv 5, CAP_BY_LEVEL), or None at 50."""
    for i, (cap, gold) in enumerate(TIERS):
        if cap > _int(max_members):
            return cap, gold, i + 2
    return None


# Fake online members (`!guild fake`, the Guild Battle "6 online" gate test aid): uids outside
# every server id space (ids.PLAYER 1..0xEFFFF, ids.MONSTER ..0x1FFFFF).
FAKE_UID_BASE = 0x00F00000
STORE_VERSION = 1
BACKUP_SUFFIX = '.bak-pre-p12+p14'
RETRY_SECS = 1.0


def supported(client_build):
    """The 2009 client is the only one with guilds."""
    return str(client_build) == BUILD_2009


def _int(value, default=0):
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value, lo, hi):
    return max(lo, min(hi, _int(value, lo)))


def text(value, limit):
    """A stored / compared text: NUL-cut, <= limit bytes (cp949, never a split character), str."""
    return P.cut_text(value, limit).decode('latin-1')


def key(name):
    return str(name or '').strip().lower()


def level_for_points(points):
    """The guild level the client shows for `points` (FUN_00484ef0): 1..5."""
    total, points = 0, max(0, _int(points))
    for i, inc in enumerate(LEVEL_INC):
        total += inc
        if points < total:
            return i + 1
    return 5


def emblem_ok(fg, bg):
    """True for an emblem the client can draw: fg 0..EMBLEM_FG_MAX, bg 0..EMBLEM_BG_MAX."""
    return (not isinstance(fg, bool) and not isinstance(bg, bool)
            and 0 <= _int(fg, -1) <= EMBLEM_FG_MAX and 0 <= _int(bg, -1) <= EMBLEM_BG_MAX)


def class_word(level, job1, job2):
    """The application record's u16 at +0x6E: level*100 + job2*10 + job1."""
    return (_clamp(level, 0, 99) * 100 + _clamp(job2, 0, JOB2_MAX) * 10 + _clamp(job1, 0, JOB1_MAX)) & 0xFFFF


def application_record(name, level, job1, job2):
    """The 130-byte str[130] of sub 2 (result 0x10) / sub 4: byte 0 = 0 [I], +1 name[17], +0x6E
    the class word, the rest zero. RawChars: the client reads it as a record, not a C string
    (byte 0 is a NUL), so packets.build must not NUL-cut it."""
    rec = bytearray(APP_RECORD_BYTES)
    rec[APP_NAME_AT:APP_NAME_AT + 17] = P.name17(name)
    word = class_word(level, job1, job2)
    rec[APP_CLASS_AT:APP_CLASS_AT + 2] = bytes((word & 0xFF, word >> 8))
    return P.RawChars(bytes(rec))


def parse_application(raw):
    """(name, level, job1, job2) of a 130-byte application record (tests, `!guild info`)."""
    raw = P.to_bytes(raw).ljust(APP_RECORD_BYTES, b'\x00')
    name = raw[APP_NAME_AT:APP_NAME_AT + 17].split(b'\x00', 1)[0].decode('latin-1')
    word = raw[APP_CLASS_AT] | (raw[APP_CLASS_AT + 1] << 8)
    return name, word // 100, word % 100 % 10, word % 100 // 10


# ============================================================ guild-g0 codec ===
# Every builder returns (spec key, fields) for packets.build / send (client build 2009).
def _b3(sub, **fields):
    return '0xB3', {'sub': int(sub), **fields}


def sub1(result, guild_id=0):
    """Create result: 1 + the new id (the client deducts 50,000 gold), 2 name, 4 GM, 5 other guild,
    7/8/9 prerequisites (wrong text), else "Fail to register the guild."."""
    fields = {'s1_result': _int(result) & 0xFF}
    if fields['s1_result'] == 1:
        if not GUILD_ID_MIN <= _int(guild_id) <= GUILD_ID_MAX:
            raise ValueError(f'sub 1: guild id {guild_id!r} is not 2..0xFFFE')
        fields['s1_guild_id'] = _int(guild_id)
    return _b3(SUB_CREATE, **fields)


def sub2(result, record=None):
    """Apply result: 1 applied, 6 full, 0x11 delayed; 0x10 = the application to the master (record)."""
    fields = {'s2_result': _int(result) & 0xFF}
    if fields['s2_result'] == 0x10:
        if record is None or len(P.to_bytes(record)) != APP_RECORD_BYTES:
            raise ValueError('sub 2 result 0x10 needs a 130-byte application_record()')
        fields['s2_notice'] = P.RawChars(P.to_bytes(record))
    return _b3(SUB_APPLY, **fields)


def member_row(uid, name, grade, job1, job2, level, online):
    """One sub 3 member record (wire order: uid, grade, job1, job2, level, online, name). job1 /
    job2 index the 3 x 7 class icon table 0x53E4C0[7*job2 + job1] (0x491D77) with no bounds
    check, so they are clamped to JOB1_MAX / JOB2_MAX like the application record."""
    return {'member_id': _int(uid) & 0xFFFFFFFF, 'member_grade': _clamp(grade, 1, GRADE_MASTER),
            'member_job1': _clamp(job1, 0, JOB1_MAX), 'member_job2': _clamp(job2, 0, JOB2_MAX),
            'member_level': _clamp(level, 0, 0xFF), 'member_online': _clamp(online, 0, 0xFF),
            'member_name': text(name, NAME_MAX)}


def sub3(guild_id, name, points, max_members, notice, emblem_fg, emblem_bg, wins=0, losses=0,
         draws=0, members=()):
    """The full guild info (98 + 26 n bytes). The client APPENDS the members (never twice per
    0x03: Guilds.send_sub) and prints the welcome line / the notice on every one."""
    if not GUILD_ID_MIN <= _int(guild_id) <= GUILD_ID_MAX:
        raise ValueError(f'sub 3: guild id {guild_id!r} is not 2..0xFFFE')
    rows = list(members)
    if len(rows) > SUB3_MAX_ROWS:
        raise ValueError(f'sub 3: {len(rows)} members do not fit one frame (max {SUB3_MAX_ROWS})')
    masters = [r for r in rows if r['member_grade'] == GRADE_MASTER]
    if len(masters) > 1:
        raise ValueError('sub 3: more than one grade-5 record (the client keeps the last as master)')
    return _b3(SUB_INFO, guild_id=_int(guild_id), guild_name=text(name, NAME_MAX),
               guild_points=_clamp(points, 0, 0xFFFFFFFF), max_members=_clamp(max_members, 0, 0xFFFF),
               guild_notice=text(notice, NOTICE_MAX), emblem_fg=_int(emblem_fg) & 0xFFFF,
               emblem_bg=_int(emblem_bg) & 0xFFFF, battle_wins=_int(wins) & 0xFFFF,
               battle_losses=_int(losses) & 0xFFFF, battle_draws=_int(draws) & 0xFFFF,
               member_count=len(rows), **{'repeat[member_count]': rows})


def sub4(records):
    """Pending applications (appended to M+0xE0, the HUD icon): <= APPS_PER_PACKET per packet."""
    recs = [P.RawChars(P.to_bytes(r)) for r in records]
    if len(recs) > APPS_PER_PACKET or any(len(r) != APP_RECORD_BYTES for r in recs):
        raise ValueError(f'sub 4: at most {APPS_PER_PACKET} 130-byte application records per packet')
    return _b3(SUB_APPS, s4_count=len(recs), **{'repeat[s4_count]': [{'s4_notice': r} for r in recs]})


def sub5(result):
    return _b3(SUB_ACCEPT, s5_result=_int(result) & 0xFF)


def sub6(result):
    """Left: 0 failed, 0xC "after 1 day", else out of the guild (clears the local tag and M)."""
    return _b3(SUB_LEFT, s6_result=_int(result) & 0xFF)


def sub7(result):
    return _b3(SUB_NOTICE, s7_result=_int(result) & 0xFF)


def sub8(result):
    """Disband: 1 deleted (clears the local tag and M), 0xA kick first, 0xB 7 days."""
    return _b3(SUB_DISBAND, s8_result=_int(result) & 0xFF)


def sub9(ok, cost=0):
    fields = {'s9_ok': 1 if ok else 0}
    if ok:
        fields['s9_cost'] = _clamp(cost, 0, 0xFFFFFFFF)
    return _b3(SUB_CAP_RESULT, **fields)


def sub10(max_members):
    return _b3(SUB_CAP, s10_max_members=_clamp(max_members, 0, 0xFFFF))


def sub11(result):
    return _b3(SUB_MASTER_RESULT, s11_result=_int(result) & 0xFF)


def sub12(new_master):
    return _b3(SUB_MASTER, s12_new_master=text(new_master, NAME_MAX))


def sub13(uid, name, online, level, job1, job2, grade):
    """A member added (27 B): wire order uid, name, online, level, JOB2, JOB1, grade (job1 / job2
    clamped to the class icon table, as member_row)."""
    return _b3(SUB_MEMBER_ADD, s13_member_id=_int(uid) & 0xFFFFFFFF, s13_member_name=text(name, NAME_MAX),
               s13_online=_clamp(online, 0, 0xFF), s13_level=_clamp(level, 0, 0xFF),
               s13_job2=_clamp(job2, 0, JOB2_MAX), s13_job1=_clamp(job1, 0, JOB1_MAX),
               s13_grade=_clamp(grade, 1, GRADE_MASTER))


def sub14(name):
    return _b3(SUB_MEMBER_DEL, s14_member_name=text(name, NAME_MAX))


def sub15(result=NOT_IN_GUILD):
    """Not in a guild (local +0x12 = 0). Result 1 = "Failed to get guild info" with +0x12 = 0xFFFF,
    which makes the client expect the 0x21 tail: never sent (guild.md 3)."""
    return _b3(SUB_NOT_IN_GUILD, s15_result=_int(result) & 0xFF)


def sub16(result):
    return _b3(SUB_GRADE_RESULT, s16_result=_int(result) & 0xFF)


def sub17(name, grade):
    return _b3(SUB_GRADE, s17_member_name=text(name, NAME_MAX), s17_grade=_clamp(grade, 1, GRADE_MASTER))


def sub18(result):
    return _b3(SUB_KICK_RESULT, s18_result=_int(result) & 0xFF)


def sub19():
    """Makes the client send C2S 0x8A (0x48456D). NEVER a reply to 0x8A (endless loop): kept for
    injection tests only; Guilds.send_sub and packets.FORBIDDEN_REPLIES refuse it there."""
    return _b3(SUB_REREQUEST)


def sub20(name, online):
    return _b3(SUB_LOGIN, s20_member_name=text(name, NAME_MAX), s20_online=_clamp(online, 0, 0xFF))


def sub21(name, points_total):
    return _b3(SUB_LOGOUT, s21_member_name=text(name, NAME_MAX), s21_guild_points=_clamp(points_total, 0, 0xFFFFFFFF))


def sub22(old, new):
    return _b3(SUB_RENAME, s22_old_name=text(old, NAME_MAX), s22_new_name=text(new, NAME_MAX))


def sub23(result, value=0):
    """Guild Battle registration result; hides box 0x16 first. 0 = failed (the 0x96 fallback)."""
    return _b3(SUB_GB_REGISTER, s23_result=_int(result) & 0xFF, s23_value=_int(value) & 0xFFFF)


def sub24(gb_id, guild_name, value):
    return _b3(SUB_GB_INVITE, s24_id=_int(gb_id) & 0xFFFFFFFF, s24_guild_name=text(guild_name, NAME_MAX),
               s24_value=_int(value) & 0xFFFF)


def sub25(gb_id, name, reason):
    return _b3(SUB_GB_ROSTER_ADD, s25_id=_int(gb_id) & 0xFFFFFFFF, s25_name=text(name, NAME_MAX),
               s25_reason=_int(reason) & 0xFF)


def sub26(entries=None):
    """The Guild Battle roster [(member_id, name)], or {0} for "no place" (entries None). STACK
    HAZARD [V]: last <= 24 or the cookie at esp+0x220 is overwritten; capped at GB_ROSTER."""
    if entries is None:
        return _b3(SUB_GB_ROSTER, s26_ok=0)
    rows = list(entries)
    if not 1 <= len(rows) <= SUB26_MAX:
        raise ValueError(f'sub 26: 1..{SUB26_MAX} roster entries (stack hazard), got {len(rows)}')
    return _b3(SUB_GB_ROSTER, s26_ok=1, s26_last_index=len(rows) - 1, **{'repeat[s26_last_index + 1]': [
        {'s26_member_id': _int(uid) & 0xFFFFFFFF, 's26_member_name': text(name, NAME_MAX)} for uid, name in rows]})


def sub27(value):
    return _b3(SUB_GB_NO_PLACE, s27_value=_int(value))


def sub29(rooms, in_progress=0, first=False):
    """The room list [(room_id, name_a, name_b)]; first=True is sub 28 (clears the list first).
    Each packet makes the client Sleep(500) in its network handler [V]."""
    rows = list(rooms)
    if len(rows) > SUB29_MAX:
        raise ValueError(f'sub 28/29: at most {SUB29_MAX} rooms per packet')
    return _b3(SUB_GB_ROOMS_FIRST if first else SUB_GB_ROOMS, s29_count=len(rows),
               s29_value=_int(in_progress) & 0xFF, **{'repeat[s29_count]': [
                   {'s29_room_id': _int(r) & 0xFFFF, 's29_name_a': text(a, NAME_MAX), 's29_name_b': text(b, NAME_MAX)}
                   for r, a, b in rows]})


def sub30(kind, member_id=None, left=False):
    """kind 2 stops everything; kind 1 removes member_id (left=True is sub 31: also clears 0x4D9)."""
    fields = {'s30_kind': _int(kind) & 0xFF}
    if fields['s30_kind'] == 1:
        fields['s30_member_id'] = _int(member_id) & 0xFFFFFFFF
    return _b3(SUB_GB_LEFT if left else SUB_GB_STOP, **fields)


def sub32(kind, in_progress, room_id, name_a='', name_b='', room_id2=0):
    """Room list delta: kind 1 adds (wire order name_b BEFORE name_a [V]), kind 2 removes."""
    fields = {'s32_kind': _int(kind) & 0xFF, 's32_value': _int(in_progress) & 0xFF,
              's32_room_id': _int(room_id) & 0xFFFF}
    if fields['s32_kind'] == 1:
        fields.update(s32_name_b=text(name_b, NAME_MAX), s32_name_a=text(name_a, NAME_MAX))
    elif fields['s32_kind'] == 2:
        fields['s32_room_id2'] = _int(room_id2) & 0xFFFF
    return _b3(SUB_GB_ROOM_DELTA, **fields)


def sub33(room_id, name):
    return _b3(SUB_GB_CHALLENGE, s33_room_id=_int(room_id) & 0xFFFF, s33_name=text(name, NAME_MAX))


def sub34(result=0):
    """Hides box 0x16; 1 / 2 add a message, anything else is a silent close (the 0x9C fallback)."""
    return _b3(SUB_GB_RESULT, s34_result=_int(result) & 0xFF)


def sub35(outcome):
    return _b3(SUB_GB_OUTCOME, s35_result=_int(outcome) & 0xFF)


def sub37(uid):
    """entity(uid)+0x12 = 1: the "Game Master" nameplate."""
    return _b3(SUB_GM_TAG, s37_uid=_int(uid) & 0xFFFFFFFF)


def board_items(client_item_ids='en'):
    """(board, premium board) item ids the installed exe draws a board for: BOARD_ITEMS_EN on the
    cp-2 exe (config CLIENT_ITEM_IDS 'en', the live default), BOARD_ITEMS_KR on the stock one
    ('kr'). Both come from en_content.exe_item_id, THE arch09-id-shift rule for exe constants."""
    return tuple(EC.exe_item_id(i, client_item_ids) for i in BOARD_ITEMS_EN)


def sub185(flag, item_id=None, client_item_ids='en'):
    """flag 1 + a board item: the client sends C2S 0x15 {that item} back when it equals ITS board
    constant (0x47E846: re-reads this field, CLIENT_PATCH_SET_RE 8.3) - so only the board id of
    the installed exe (board_items) is sent."""
    fields = {'s185_flag': _int(flag) & 0xFF}
    if fields['s185_flag'] == 1:
        fields['s185_item_id'] = _int(item_id) & 0xFFFF
        if fields['s185_item_id'] not in board_items(client_item_ids):
            raise ValueError(f'sub 185 item {fields["s185_item_id"]}: the board ids of the {client_item_ids} exe are '
                             f'{board_items(client_item_ids)} (config CLIENT_ITEM_IDS)')
    return _b3(SUB_BOARD_ITEM, **fields)


def entity_set(uid, guild_id, name, emblem_fg, emblem_bg):
    """S2C 0xB4: any entity's tag (27 B). guild_id 1 = "Game Master", 0 hides."""
    return '0xB4', {'uid': _int(uid) & 0xFFFFFFFF, 'guild_id': _int(guild_id) & 0xFFFF,
                    'guild_name': text(name, NAME_MAX), 'emblem_fg': _int(emblem_fg) & 0xFFFF,
                    'emblem_bg': _int(emblem_bg) & 0xFFFF}


def chat_line(line):
    """S2C 0xB5: one guild-coloured chat line (<= 87 bytes: packets.TEXT_FIELDS)."""
    return '0xB5', {'text': P.cut_text(line, CHAT_LINE_MAX)}


def entity_clear(uid, blank_name=True):
    """S2C 0xB7 (also blanks the name; leave / kick / disband, guild.md 3 [I]) or 0xB6."""
    return ('0xB7' if blank_name else '0xB6'), {'uid': _int(uid) & 0xFFFFFFFF}


def board_remove(guild_id):
    return '0xB8', {'guild_id': _int(guild_id) & 0xFFFF}


def _board_fields(b, client_item_ids='en'):
    item = _int(b.get('board_item_id'))
    if item not in board_items(client_item_ids):
        raise ValueError(f'board item {item}: only the {client_item_ids} exe ids {board_items(client_item_ids)} draw '
                         f'a sprite (guild.md 1.6, config CLIENT_ITEM_IDS)')
    if not emblem_ok(b.get('emblem_symbol'), b.get('emblem_bg')):
        # the create rule (EMBLEM_FG_MAX / EMBLEM_BG_MAX), for guild-g6's C2S 0x88 boards too
        raise ValueError(f'board emblem {b.get("emblem_symbol")}/{b.get("emblem_bg")}: outside '
                         f'0..{EMBLEM_FG_MAX} / 0..{EMBLEM_BG_MAX} (no sprite frame)')
    return {'master_char_id': _int(b.get('master_char_id')) & 0xFFFFFFFF,
            'master_name': text(b.get('master_name'), NAME_MAX), 'board_item_id': item,
            'guild_id': _int(b.get('guild_id')) & 0xFFFF, 'guild_name': text(b.get('guild_name'), NAME_MAX),
            'emblem_symbol': _int(b.get('emblem_symbol')) & 0xFFFF, 'emblem_bg': _int(b.get('emblem_bg')) & 0xFFFF,
            'ad_text': text(b.get('ad_text'), 24), 'pos_x': float(b.get('pos_x', 0.0)),
            'pos_y': float(b.get('pos_y', 0.0))}


def board_add(board, client_item_ids='en'):
    """S2C 0xBA (87 B, the C2S 0x88 field order). client_item_ids: config CLIENT_ITEM_IDS."""
    return '0xBA', _board_fields(board, client_item_ids)


def board_list(boards, client_item_ids='en'):
    """S2C 0xBB (u8 n + n x 87 B; the record order differs from 0xBA - the grammar's names keep it
    straight). n = 0 clears the plaza. client_item_ids: config CLIENT_ITEM_IDS."""
    rows = [_board_fields(b, client_item_ids) for b in boards]
    if len(rows) > (2038 - 1) // 87:
        raise ValueError('0xBB: too many boards for one frame')
    return '0xBB', {'board_count': len(rows), 'repeat[board_count]': rows}


# =============================================================== the store ===
@dataclass
class Guild:
    id: int
    name: str
    points: int = 0
    max_members: int = BASE_MAX_MEMBERS
    notice: str = ''
    emblem_fg: int = EMBLEM_DEFAULT
    emblem_bg: int = EMBLEM_DEFAULT
    wins: int = 0
    losses: int = 0
    draws: int = 0
    created_at: float = 0.0
    members: list = field(default_factory=list)        # [{cid, name, grade, joined_at}] join order
    applications: list = field(default_factory=list)   # [{cid, name, level, job1, job2, at}] FIFO
    fake_online: int = 0                               # `!guild fake` (memory only)

    @property
    def level(self):
        return level_for_points(self.points)

    def master(self):
        return next((m for m in self.members if m['grade'] == GRADE_MASTER), None)

    def member(self, cid):
        return next((m for m in self.members if m['cid'] == cid), None)

    def tag(self):
        return R.GuildTag(self.id, self.name, self.emblem_fg, self.emblem_bg)

    def to_json(self):
        return {'id': self.id, 'name': self.name, 'points': self.points, 'max_members': self.max_members,
                'notice': self.notice, 'emblem_fg': self.emblem_fg, 'emblem_bg': self.emblem_bg,
                'wins': self.wins, 'losses': self.losses, 'draws': self.draws,
                'created_at': self.created_at,
                'members': [dict(m) for m in self.members],
                'applications': [dict(a) for a in self.applications]}

    def copy(self):
        g = Guild(**{k: v for k, v in self.__dict__.items() if k not in ('members', 'applications')})
        g.members = [dict(m) for m in self.members]
        g.applications = [dict(a) for a in self.applications]
        return g


def _valid_cid(value):
    return storemod.valid_cid(value)


def normalize_guild(row, changes):
    """A Guild from one stored row (structural repair; returns None when the row is unusable).
    Appends what it changed to `changes`."""
    if not isinstance(row, dict):
        changes.append(f'dropped a non-object guild row {row!r:.40}')
        return None
    gid = row.get('id')
    if isinstance(gid, bool) or not isinstance(gid, int) or not GUILD_ID_MIN <= gid <= GUILD_ID_MAX:
        changes.append(f'dropped guild row with id {gid!r} (ids are 2..0xFFFE)')
        return None
    name = text(row.get('name'), NAME_MAX)
    if not name:
        changes.append(f'guild {gid}: dropped (no name)')
        return None
    if name != row.get('name'):
        changes.append(f'guild {gid}: name cut to {name!r}')
    g = Guild(id=gid, name=name)
    # the emblem is clamped to the sprite sheets (EMBLEM_FG_MAX / EMBLEM_BG_MAX): a hand-edited
    # file can then never make the records / 0xB4 push a frame index past them
    for attr, lo, hi, default in (('points', 0, 0xFFFFFFFF, 0), ('max_members', MIN_MAX_MEMBERS, MAX_MEMBERS_CAP, BASE_MAX_MEMBERS),
                                  ('emblem_fg', 0, EMBLEM_FG_MAX, EMBLEM_DEFAULT),
                                  ('emblem_bg', 0, EMBLEM_BG_MAX, EMBLEM_DEFAULT),
                                  ('wins', 0, 0xFFFF, 0), ('losses', 0, 0xFFFF, 0), ('draws', 0, 0xFFFF, 0)):
        raw = row.get(attr, default)
        value = _clamp(raw, lo, hi) if isinstance(raw, int) and not isinstance(raw, bool) else default
        if value != raw:
            changes.append(f'guild {gid} {name}: {attr} {raw!r} -> {value}')
        setattr(g, attr, value)
    notice = text(row.get('notice') or '', NOTICE_MAX)
    if notice != (row.get('notice') or ''):
        changes.append(f'guild {gid} {name}: notice normalized')
    g.notice = notice
    created = row.get('created_at', 0)
    g.created_at = float(created) if isinstance(created, (int, float)) and not isinstance(created, bool) else 0.0
    if g.created_at != created:
        changes.append(f'guild {gid} {name}: created_at normalized')
    seen = set()
    for m in row.get('members') if isinstance(row.get('members'), list) else []:
        cid = m.get('cid') if isinstance(m, dict) else None
        if not _valid_cid(cid) or cid in seen:
            changes.append(f'guild {gid} {name}: dropped member row {m!r:.60}')
            continue
        seen.add(cid)
        grade = m.get('grade')
        fixed = _clamp(grade, GRADE_TRAINEE, GRADE_MASTER) if isinstance(grade, int) and not isinstance(grade, bool) else GRADE_TRAINEE
        if fixed != grade:
            changes.append(f'guild {gid} {name}: member {cid} grade {grade!r} -> {fixed}')
        joined = m.get('joined_at', 0)
        g.members.append({'cid': cid, 'name': text(m.get('name') or '', NAME_MAX), 'grade': fixed,
                          'joined_at': float(joined) if isinstance(joined, (int, float)) and not isinstance(joined, bool) else 0.0})
    if not isinstance(row.get('members'), list):
        changes.append(f'guild {gid} {name}: members list created')
    _one_master(g, changes)
    apps, app_seen = [], set()
    for a in row.get('applications') if isinstance(row.get('applications'), list) else []:
        cid = a.get('cid') if isinstance(a, dict) else None
        if not _valid_cid(cid) or cid in app_seen or cid in seen:
            changes.append(f'guild {gid} {name}: dropped application {a!r:.60}')
            continue
        app_seen.add(cid)
        at = a.get('at', 0)
        apps.append({'cid': cid, 'name': text(a.get('name') or '', NAME_MAX), 'level': _clamp(a.get('level'), 0, 99),
                     'job1': _clamp(a.get('job1'), 0, JOB1_MAX), 'job2': _clamp(a.get('job2'), 0, JOB2_MAX),
                     'at': float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else 0.0})
    if len(apps) > MAX_APPLICATIONS:
        changes.append(f'guild {gid} {name}: {len(apps) - MAX_APPLICATIONS} oldest application(s) dropped')
        apps = apps[-MAX_APPLICATIONS:]
    if not isinstance(row.get('applications'), list):
        changes.append(f'guild {gid} {name}: applications list created')
    g.applications = apps
    if not g.members:
        changes.append(f'guild {gid} {name}: dropped (no members)')
        return None
    known = {'id', 'name', 'points', 'max_members', 'notice', 'emblem_fg', 'emblem_bg', 'wins', 'losses',
             'draws', 'created_at', 'members', 'applications'}
    extra = sorted(set(row) - known)
    if extra:
        changes.append(f'guild {gid} {name}: unknown key(s) {extra} dropped')
    return g


def _one_master(g, changes):
    """Exactly one grade-5 member (sub 3: the client keeps the LAST grade-5 record as M+0x60/+0x64):
    the first stays master, a later one becomes a Trainee; none -> the earliest member [I O7]."""
    masters = [m for m in g.members if m['grade'] == GRADE_MASTER]
    for extra in masters[1:]:
        extra['grade'] = GRADE_TRAINEE
        changes.append(f'guild {g.id} {g.name}: second master {extra["cid"]} -> Trainee')
    if g.members and not masters:
        g.members[0]['grade'] = GRADE_MASTER
        changes.append(f'guild {g.id} {g.name}: no master - member {g.members[0]["cid"]} promoted')


def migrate(data):
    """(guilds {id: Guild}, last_id, changes) of a loaded guilds.json document. Idempotent: a
    normalized file yields no change."""
    changes = []
    if not isinstance(data, dict):
        raise ValueError('the top level must be an object')
    if data.get('version') != STORE_VERSION:
        changes.append(f'version {data.get("version")!r} -> {STORE_VERSION}')
    rows = data.get('guilds')
    if not isinstance(rows, list):
        changes.append('guilds list created')
        rows = []
    guilds, names_seen, cids = {}, set(), set()
    for row in rows:
        g = normalize_guild(row, changes)
        if g is None:
            continue
        if g.id in guilds:
            changes.append(f'guild {g.id} {g.name}: duplicate id dropped')
            continue
        if key(g.name) in names_seen:
            changes.append(f'guild {g.id} {g.name}: duplicate name dropped')
            continue
        kept = []
        for m in g.members:
            if m['cid'] in cids:
                changes.append(f'guild {g.id} {g.name}: member {m["cid"]} is in another guild - dropped here')
                continue
            kept.append(m)
        g.members = kept
        if not g.members:
            changes.append(f'guild {g.id} {g.name}: dropped (no members left)')
            continue
        _one_master(g, changes)
        cids.update(m['cid'] for m in g.members)
        names_seen.add(key(g.name))
        guilds[g.id] = g
    last = data.get('last_id', 0)
    fixed_last = max([_int(last) if isinstance(last, int) and not isinstance(last, bool) else 0, GUILD_ID_MIN - 1]
                     + list(guilds))
    if fixed_last != last:
        changes.append(f'last_id {last!r} -> {fixed_last}')
    return guilds, min(fixed_last, GUILD_ID_MAX), changes


def resolve(guilds, store):
    """Drop the member / application rows whose cid no longer names a character (deleted), refresh
    the cached names; a guild left without members is removed. Returns the change list."""
    changes = []
    if store is None:
        return changes
    with store.lock:
        by_cid = {c.get('cid'): c for acc in store.accounts.values() for c in acc.get('characters') or []
                  if _valid_cid(c.get('cid'))}
    for gid in sorted(guilds):
        g = guilds[gid]
        kept = []
        for m in g.members:
            char = by_cid.get(m['cid'])
            if char is None:
                changes.append(f'guild {gid} {g.name}: member {m["name"] or m["cid"]} (cid {m["cid"]}) no longer exists - dropped')
                continue
            if m['name'] != char.get('name'):
                changes.append(f'guild {gid} {g.name}: member {m["cid"]} name {m["name"]!r} -> {char.get("name")!r}')
                m['name'] = text(char.get('name'), NAME_MAX)
            kept.append(m)
        g.members = kept
        apps = []
        for a in g.applications:
            char = by_cid.get(a['cid'])
            if char is None:
                changes.append(f'guild {gid} {g.name}: application of cid {a["cid"]} dropped (no such character)')
                continue
            a['name'] = text(char.get('name'), NAME_MAX)
            apps.append(a)
        g.applications = apps
        if not g.members:
            changes.append(f'guild {gid} {g.name}: removed (no members left)')
            del guilds[gid]
            continue
        _one_master(g, changes)
    return changes


def census(rows):
    """(guilds, member rows, application rows) of a raw guilds.json 'guilds' list or of a loaded
    {id: Guild} dict: GuildStore.load compares the two to see whether a load DROPS data."""
    if isinstance(rows, dict):
        gs = list(rows.values())
        return len(gs), sum(len(g.members) for g in gs), sum(len(g.applications) for g in gs)
    rows = rows if isinstance(rows, list) else []
    def n(row, key_):
        value = row.get(key_) if isinstance(row, dict) else None
        return len(value) if isinstance(value, list) else 0
    return len(rows), sum(n(r, 'members') for r in rows), sum(n(r, 'applications') for r in rows)


class GuildStoreError(Exception):
    pass


class GuildStore:
    """guilds.json: load (migrate + resolve + one-time backup), snapshot, atomic flush - the boss
    ledger's rules (bosses.BossLedger): one writer at a time, an older snapshot never replaces a
    newer one, a failed write stays dirty."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.guilds = {}
        self.last_id = GUILD_ID_MIN - 1
        self.dirty = False
        self.saves = 0
        self.migration_changes = []
        self._changes = 0
        self._gen = 0
        self._disk_gen = 0
        self._save_mutex = threading.Lock()

    @property
    def backup_path(self):
        return (self.path + BACKUP_SUFFIX) if self.path else ''

    def drop_backup_path(self, now=None):
        """<file>.bak-<YYYYmmdd-HHMMSS>[-n]: the copy a destructive load keeps (never overwritten)."""
        stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(time.time() if now is None else now))
        path, n = f'{self.path}.bak-{stamp}', 1
        while os.path.exists(path):
            n += 1
            path = f'{self.path}.bak-{stamp}-{n}'
        return path

    def load(self, store=None):
        """Read, migrate and resolve the file; a missing one is an empty store (nothing written
        until the first change). A file that cannot be read raises GuildStoreError (the server
        must not start over it); a malformed one is moved aside to <file>.bad-<time>."""
        raw, existed = None, bool(self.path) and os.path.exists(self.path)
        data = {'version': STORE_VERSION, 'last_id': GUILD_ID_MIN - 1, 'guilds': []}
        if existed:
            try:
                with open(self.path, 'rb') as f:
                    raw = f.read()
            except OSError as e:
                raise GuildStoreError(f'{self.path}: cannot be read ({e}); fix it or point GUILDS_FILE '
                                      f'elsewhere - the server will not replace it') from e
            try:
                data = json.loads(raw.decode('utf-8'))
                if not isinstance(data, dict):
                    raise ValueError('the top level is not an object')
            except ValueError as e:
                bad = f'{self.path}.bad-{int(time.time())}'
                try:
                    os.replace(self.path, bad)
                except OSError as moved:
                    raise GuildStoreError(f'{self.path}: malformed ({e}) and cannot be moved aside ({moved})') from e
                log.error(f'[GUILD] {self.path} is malformed ({e}); moved to {bad}, starting with no guilds')
                existed, raw = False, None
                data = {'version': STORE_VERSION, 'last_id': GUILD_ID_MIN - 1, 'guilds': []}
        before = census(data.get('guilds'))
        guilds, last_id, changes = migrate(data)
        changes += resolve(guilds, store)
        after = census(guilds)
        with self.lock:
            self.guilds, self.last_id = guilds, last_id
            self.migration_changes = changes
            if changes and existed:
                self._mark()
        if changes and existed:
            if self.backup_path and not os.path.exists(self.backup_path):
                storemod.atomic_write(self.backup_path, raw)
                log.info(f'[GUILD] one-time backup of the pre-migration file: {self.backup_path}')
            elif self.path and any(a < b for a, b in zip(after, before)):
                # P14 review: resolve() drops the rows of characters the account store does not
                # know (and guilds left empty) on EVERY load; once the one-time backup exists, a
                # later destructive load (another ACCOUNTS_FILE next to the same guilds.json, a
                # wiped character list) would lose them for good - so it keeps its own copy.
                path = self.drop_backup_path()
                storemod.atomic_write(path, raw)
                log.warning(f'[GUILD] this load drops data (guilds / members / applications {before} -> '
                            f'{after}); the file before it is kept as {path}')
            for line in changes:
                log.info(f'[GUILD] migrate {line}')
            self.flush()
        log.info(f'[GUILD] {len(guilds)} guild(s) from {self.path or "(memory)"}'
                 f'{f", {len(changes)} migration change(s)" if changes and existed else ""}')
        return len(guilds)

    def _mark(self):
        """Caller holds self.lock."""
        self.dirty = True
        self._changes += 1

    def snapshot(self):
        with self.lock:
            return {'version': STORE_VERSION, 'last_id': self.last_id,
                    'guilds': [self.guilds[g].to_json() for g in sorted(self.guilds)]}

    def flush(self, delays=None, wait=None):
        """Write the file if it changed. True when this snapshot or a newer one is on disk."""
        if not self.path:
            with self.lock:
                self.dirty = False
            return False
        with self.lock:
            if not self.dirty:
                return False
            self._gen += 1
            gen, covers = self._gen, self._changes
            snap = {'version': STORE_VERSION, 'last_id': self.last_id,
                    'guilds': [self.guilds[g].to_json() for g in sorted(self.guilds)]}
        data = json.dumps(snap, indent=1).encode('utf-8')
        wrote = False
        if wait is None:
            self._save_mutex.acquire()
        elif not self._save_mutex.acquire(timeout=max(0.0, float(wait))):
            raise TimeoutError(f'another save is writing {self.path}')
        try:
            if gen > self._disk_gen:
                storemod.atomic_write(self.path, data, delays)
                self._disk_gen = gen
                wrote = True
        finally:
            self._save_mutex.release()
        with self.lock:
            if wrote:
                self.saves += 1
            if self._changes == covers:
                self.dirty = False
        return True


def guilds_path(cfg, accounts_path):
    """GUILDS_FILE next to the accounts file ('' = memory only). Tests run on a temp accounts.json,
    so their guilds.json lands in the same temp directory."""
    name = str(cfg.get('GUILDS_FILE', cfgmod.DEFAULTS.get('GUILDS_FILE', 'guilds.json')) or '')
    if not name:
        return ''
    if os.path.isabs(name):
        return name
    return os.path.join(os.path.dirname(os.path.abspath(accounts_path)), name)


# ================================================================ runtime ===
class Guilds:
    """The guilds of one GameServer (server.guilds)."""

    def __init__(self, server):
        self.server = server
        self.supported = supported(getattr(server, 'client_build', None))
        path = guilds_path(server.config, server.store.path) if self.supported else ''
        self.db = GuildStore(path)
        self.lock = self.db.lock
        self._tags = {}                         # cid -> GuildTag: replaced whole (tag_of reads it lock-free)
        self._plock = threading.Lock()
        self._pending = None
        self._failures = 0
        # guild-g3 (module docstring "Locks"): one membership / management flow at a time,
        # [validate, change, fan the deltas out] - and the 0x8A reply's [build sub 3, send] -
        # so every client sees each change exactly once: in its sub 3 or as a delta after it.
        self.flow_lock = threading.RLock()
        # Per thread: the depth of _flow() and the sessions whose own-connection flush waits for
        # its release (P14 review: no socket write while flow_lock is held).
        self._flow_tls = threading.local()
        # guild-g5 (F12): guild points of characters whose connection closed while their login
        # may still go on (a 2009 channel hop holds the logout back): {cid: points}, credited to
        # the guild by the logout (sub 21). Memory only [I]: a server crash loses them.
        self._held_gp = {}
        if self.supported:
            self.db.load(server.store)
            self._reindex()

    # --------------------------------------------------------------- index ---
    def _reindex(self):
        """Rebuild the cid -> tag map (caller holds self.lock, or nothing else can run)."""
        with self.lock:
            self._tags = {m['cid']: g.tag() for g in self.db.guilds.values() for m in g.members}

    def tag_of(self, char):
        """The records' GuildTag of `char` (arch09-roster-record), None when it is in no guild or
        this is not a 2009 server. Lock-free (module docstring "Locks")."""
        if not self.supported or not isinstance(char, dict):
            return None
        return self._tags.get(char.get('cid'))

    def guild_id_of(self, char):
        tag = self.tag_of(char)
        return tag.id if tag is not None else 0

    def get(self, name_or_id):
        """A copy of the guild with this id or name (case-insensitive), or None."""
        with self.lock:
            if isinstance(name_or_id, int):
                g = self.db.guilds.get(name_or_id)
            else:
                g = next((x for x in self.db.guilds.values() if key(x.name) == key(name_or_id)), None)
            return g.copy() if g is not None else None

    def of_char(self, char):
        """A copy of `char`'s guild, or None."""
        tag = self.tag_of(char)
        return self.get(tag.id) if tag is not None else None

    def all(self):
        with self.lock:
            return [self.db.guilds[g].copy() for g in sorted(self.db.guilds)]

    # ------------------------------------------------------------ the wire ---
    def _resolve_chars(self, cids):
        """{cid: (account, char)} of these characters (one pass over the store)."""
        store = self.server.store
        wanted, out = set(cids), {}
        with store.lock:
            for acc in store.accounts.values():
                for char in acc.get('characters') or []:
                    if char.get('cid') in wanted:
                        out[char['cid']] = (acc, char)
        return out

    def _online_channel(self, name):
        """The channel number of the character's in-world session (guild.md 7 [I]: the member
        `online` byte is the channel), 0 when it is not in the world."""
        world = getattr(self.server, 'world', None)
        s = world.by_char_name(name) if world is not None and name else None
        if s is None or s.get('sock') is None or s.get('kicked'):
            return 0
        return max(1, int(self.server._channel_no(s)))

    def info_fields(self, g):
        """sub 3 for guild copy `g`: every member resolved from the store by cid (a deleted one is
        skipped), the master record included, then `fake_online` dummies."""
        found = self._resolve_chars(m['cid'] for m in g.members)
        rows = []
        for m in g.members:
            hit = found.get(m['cid'])
            if hit is None:
                log.warning(f'[GUILD] {g.name}: member cid {m["cid"]} ({m["name"]}) no longer exists - left out of sub 3')
                continue
            acc, char = hit
            rows.append(member_row(acc.get('uid', 0), char.get('name', ''), m['grade'], char.get('class', 0),
                                   char.get('job2', 0), R.level_of(char), self._online_channel(char.get('name'))))
        if not any(r['member_grade'] == GRADE_MASTER for r in rows):
            log.warning(f'[GUILD] {g.name}: the master is gone - sub 3 has no grade-5 record (nobody gets Kick)')
        channel = self._fake_channel()
        for i in range(max(0, min(g.fake_online, SUB3_MAX_ROWS - len(rows)))):
            rows.append(member_row(FAKE_UID_BASE + i, f'Dummy{i + 1}', GRADE_TRAINEE, 1, 0, 30, channel))
        return sub3(g.id, g.name, g.points, g.max_members, g.notice, g.emblem_fg, g.emblem_bg,
                    g.wins, g.losses, g.draws, rows)

    def _fake_channel(self):
        channels = self.server.config.channels()
        return int(channels[0][0]) if channels else 1

    def send_sub(self, sock, session, packet, *, reply_to=None, holds=None, apps=False, epoch=None):
        """THE 0xB3 sender (module docstring "The receiver mirror"). `packet` is a builder's
        (key, fields). Returns True when it went out, False when a guard held it back (logged).
        Raises packets.PacketError for sub 19 as a 0x8A reply.
        holds=gid: a member DELTA - sent only when this client holds guild gid's window
        (clientview guild_list; a client in a map load gets the state from its coming sub 3).
        apps=True: an application push - sent only when the client's application list mirrors
        the guild's FIFO (clientview apps_held; else the coming sub 4 carries it).
        epoch=n: sent only while the client is still in map load n (clientview loads) - the 0x8A
        reply's sub 4 belongs to the sub 3 of THAT load: after a newer 0x03 it would land with no
        sub 3 in its epoch and the next 0x8A would append the applications a second time."""
        key_, fields = packet
        if key_ != '0xB3':
            raise ValueError(f'send_sub sends 0xB3, not {key_}')
        sub = int(fields['sub'])
        who = session.get('char_name') or session.get('username')
        if sub == SUB_REREQUEST and reply_to == REPLY_8A:
            raise P.PacketError('0xB3 sub 19 as the reply to C2S 0x8A makes the client re-send 0x8A for ever '
                                '(guild.md 2 sub 19, F0 step 3)')
        if not self.supported or sock is None:
            return False
        v = cview.view(session)
        with v.lock:
            if not v.had_03:
                log.info(f'[GUILD] sub {sub} to {who!r} held back: no S2C 0x03 yet (SubHandler3 drops it)')
                return False
            if epoch is not None and v.loads != epoch:
                log.info(f'[GUILD] sub {sub} to {who!r} dropped: map load {v.loads} began after the sub 3 of '
                         f'load {epoch} (its own 0x8A gets the whole state again)')
                return False
            if holds is not None and v.guild_list != holds:
                log.debug(f'[GUILD] sub {sub} to {who!r} skipped: its client holds no window of guild {holds} '
                          f'(its next sub 3 carries the change)')
                return False
            if apps and not v.apps_held:
                log.debug(f'[GUILD] sub {sub} to {who!r} skipped: its application list is not synced yet '
                          f'(its coming sub 4 carries it)')
                return False
            if sub in NEEDS_OWN_07 and not v.own_07:
                log.error(f'[GUILD] sub {sub} to {who!r} REFUSED: before its own 0x07 (the handler writes '
                          f'the local entity with no NULL check, guild.md 1.1)')
                return False
            rows = sub == SUB_INFO and int(fields.get('member_count', 0)) > 0
            if rows and v.guild_rows:
                log.warning(f'[GUILD] second sub 3 to {who!r} without an S2C 0x03 in between held back: it '
                            f'would duplicate every member (guild.md 2 sub 3 hazard)')
                return False
            # flush=False: nothing is written under the view lock (clientview "Order")
            P.send(self.server, sock, session, key_, fields, flush=False)
            self._mirror_sub(session, v, sub, fields, rows)
        self._flush(session)
        return True

    def _flush(self, session):
        """Write what a flush=False send queued for `session` when this is its own connection
        thread (GameServer._flush_outbox) - or, inside _flow(), once flow_lock is let go."""
        tl = self._flow_tls
        if getattr(tl, 'depth', 0):
            if all(s is not session for s in tl.pending):
                tl.pending.append(session)
            return
        flush = getattr(self.server, '_flush_outbox', None)
        if flush is not None:
            flush(session)

    @contextlib.contextmanager
    def _flow(self):
        """flow_lock (module docstring "Locks") with the flushes deferred to its release (P14
        review): a flow answers its requester on the requester's own connection thread, where
        _flush_outbox writes the socket itself - under flow_lock a stalled requester (a full TCP
        send buffer) would hold every guild flow and every 0x8A reply server-wide for up to the
        socket timeout. Inside it the guild senders only queue (P.send flush=False: the writer
        thread is woken at once anyway); the outermost exit, flow_lock released, flushes the
        queued sessions in order. Re-entrant, like flow_lock."""
        tl = self._flow_tls
        pending = None
        with self.flow_lock:
            depth = getattr(tl, 'depth', 0)
            if depth == 0:
                tl.pending = []
            tl.depth = depth + 1
            try:
                yield
            finally:
                tl.depth = depth
                if depth == 0:
                    pending, tl.pending = tl.pending, None
        for s in pending or ():
            try:
                self._flush(s)
            except OSError as e:                   # the requester's connection just closed
                log.debug(f'[GUILD] deferred flush to {self._whois(s)!r} failed: {e}')

    @staticmethod
    def _mirror_sub(session, v, sub, fields, rows):
        """The client handler's own writes of local entity+0x12 and of the member list (caller
        holds v.lock, right after the send)."""
        if sub == SUB_CREATE:
            if fields.get('s1_result') == 1:
                v.guild_id = int(fields['s1_guild_id'])
            else:
                v.guild_rows, v.guild_list = False, 0      # every failure resets M (FUN_0047a8c0)
        elif sub == SUB_INFO:
            v.guild_id = v.guild_list = int(fields['guild_id'])
            if rows:
                v.guild_rows = True
        elif sub == SUB_LEFT and fields.get('s6_result') not in (0, 0x0C):
            v.guild_id, v.guild_rows, v.guild_list = 0, False, 0
        elif sub == SUB_DISBAND and fields.get('s8_result') == 1:
            v.guild_id, v.guild_rows, v.guild_list = 0, False, 0
        elif sub == SUB_NOT_IN_GUILD:
            v.guild_id = GUILD_ID_LOAD_FAILED if fields.get('s15_result') == 1 else 0
            v.guild_list = 0
        elif sub == SUB_GM_TAG and int(fields.get('s37_uid', 0)) == (P.session_uid(session) or 0):
            v.guild_id = GUILD_ID_GM

    def send_entity(self, sock, session, packet):
        """0xB4 / 0xB6 / 0xB7 to one receiver; on the receiver's own uid the mirror follows."""
        key_, fields = packet
        if key_ not in ('0xB4', '0xB6', '0xB7'):
            raise ValueError(f'send_entity sends 0xB4 / 0xB6 / 0xB7, not {key_}')
        if not self.supported or sock is None:
            return False
        v = cview.view(session)
        with v.lock:
            if not v.had_03:
                return False
            P.send(self.server, sock, session, key_, fields, flush=False)
            if int(fields['uid']) == (P.session_uid(session) or 0):
                v.guild_id = int(fields['guild_id']) if key_ == '0xB4' else 0
        self._flush(session)
        return True

    # ------------------------------------------- the 0x8A reply (resync steps) ---
    def reply_info(self, sock, session):
        """resync.STAGE_GUILD 'guild' (F0 step 3): sub 3 for a member, else sub 15 {0}. Returns
        the map-load epoch (clientview loads, >= 1) of a sub 3 with the member list that went
        out, else None: the 'guild_apps' step follows only then, and only within that same
        epoch (sub 4 appends to M+0xE0 exactly like sub 3 to the member list, so a second 0x8A
        without a 0x03 gets neither again, and a sub 4 after a newer 0x03 none at all).
        Built and sent under flow_lock (guild-g3): a membership change on another thread is
        either in this sub 3 or comes as a delta after it, never both (a sub 13 on top of a
        sub 3 that already lists the member shows it twice) and never neither. A client that
        got its whole list from an admission (the sub 3 to the new member) after this map
        load's 0x03 gets no second one here."""
        with self._flow():
            char = self.server._session_char(session)
            g = self.of_char(char)
            name = session.get('char_name')
            if g is None:
                self.send_sub(sock, session, sub15(NOT_IN_GUILD), reply_to=REPLY_8A)
                log.info(f'[GUILD] 0x8A guild info ({name!r}) -> 0xB3 sub 15 result 0 (not in a guild)')
                return None
            v = cview.view(session)
            with v.lock:
                held = v.guild_list == g.id and v.guild_rows and session.get(ADMIT_LOAD_KEY) == v.loads
            if held:
                log.info(f'[GUILD] 0x8A guild info ({name!r}): its client got the {g.name!r} list after this '
                         f'map load already (the admission) - no second sub 3')
                return None
            packet = self.info_fields(g)
            with v.lock:            # RLock: no map load between the send and reading its epoch
                epoch = v.loads if self.send_sub(sock, session, packet, reply_to=REPLY_8A) else None
            if epoch is None:
                return None
        log.info(f'[GUILD] 0x8A guild info ({name!r}) -> 0xB3 sub 3 {g.name!r} id {g.id} Lv.{g.level} '
                 f'({packet[1]["member_count"]}/{g.max_members}, {g.points} points)')
        return epoch

    def reply_applications(self, sock, session, epoch=None):
        """resync.STAGE_GUILD 'guild_apps' (F0 step 3): the master gets the pending applications
        again (the 0x03 emptied M+0xE0), oldest first - and from then on its list mirrors the
        guild's FIFO (apps_held), so a new application is pushed (sub 2 0x10) instead of
        waiting. Stale rows (the applicant joined a guild meanwhile, or no longer exists) are
        dropped right here, the one moment the client's list is rebuilt from the server's.
        epoch: the map load whose sub 3 reply_info sent - a map load that began since (a tick,
        a revive, a GM warp on another thread) gets no sub 4 from this 0x8A (P14 review)."""
        with self._flow():
            char = self.server._session_char(session)
            g = self.of_char(char)
            if g is None:
                return
            me = g.member(char.get('cid')) if isinstance(char, dict) else None
            if me is None or me['grade'] != GRADE_MASTER:
                return
            g = self._purge_stale_applications(g)
            if g is not None:
                self._send_applications(sock, session, g, reply_to=REPLY_8A, epoch=epoch)

    def _send_applications(self, sock, session, g, reply_to=None, epoch=None):
        """Caller holds flow_lock: every pending application of guild copy `g` as sub 4 (<= 15
        per packet; none at all for an empty list), then apps_held (and the list is no longer
        stale). epoch: send_sub's map-load guard."""
        recs = [application_record(a['name'], a['level'], a['job1'], a['job2']) for a in g.applications]
        for i in range(0, len(recs), APPS_PER_PACKET):
            if not self.send_sub(sock, session, sub4(recs[i:i + APPS_PER_PACKET]), reply_to=reply_to,
                                 holds=g.id, epoch=epoch):
                return False
        v = cview.view(session)
        with v.lock:
            if v.guild_list != g.id or (epoch is not None and v.loads != epoch):
                return False
            v.apps_held, v.apps_stale = True, False
        if recs:
            log.info(f'[GUILD] {g.name}: {len(recs)} pending application(s) -> sub 4 to the master '
                     f'{session.get("char_name")!r}')
        return True

    def send_gm_tag(self, sock, session):
        """resync.STAGE_GUILD 'gm_tag', last: sub 37 {own uid} puts back the "Game Master" tag the
        sub 15 / sub 3 just replaced, for a visible GM only (records.gm_tag_visible)."""
        if not R.gm_tag_visible(session.get('gm') == 1, session.get('gm_hidden'), session.get('gm_tag_off')):
            return False
        if self.send_sub(sock, session, sub37(P.session_uid(session) or 0), reply_to=REPLY_8A):
            log.info('[GUILD] GM nameplate restored: 0xB3 sub 37 after the guild answer')
            return True
        return False

    # ------------------------------------------------- the 0x21 tail (F12) ---
    def points_for(self, guild_base):
        """The tail value of a 0x21 whose tail is present: GUILD_POINTS_PCT of the pre-multiplier
        exp of an earned grant (award_exp guild_base; X9), 0 for a grant that earns none."""
        if guild_base is None or _int(guild_base) <= 0:
            return 0
        pct = max(0, _int(self.server.config.get('GUILD_POINTS_PCT', 50)))
        return min(0xFFFFFFFF, _int(guild_base) * pct // 100)

    @staticmethod
    def accrue(session, points):
        """A 0x21 tail went out with `points`: they wait for the logout credit (guild-g5, sub 21)."""
        if points > 0:
            session['guild_pending_gp'] = int(session.get('guild_pending_gp') or 0) + int(points)

    # ------------------------------------------------ soft-lock fallbacks (F14) ---
    def battle_register(self, sock, session, rec=None):
        """C2S 0x96: the client opened "Waiting for the server to respond."; sub 23 {0, 0} closes it
        ("failed"). Guild Battle itself is guild-g7 (P17)."""
        self._send_fallback(sock, session, sub23(0, 0), '0x96 Guild Battle register')

    def battle_accept(self, sock, session, rec=None):
        """C2S 0x9C {room}: the same waiting box; sub 34 {0} is its silent close."""
        self._send_fallback(sock, session, sub34(0), f'0x9C Guild Battle challenge accept {dict(rec or {})}')

    def _send_fallback(self, sock, session, packet, what):
        key_, fields = packet
        v = cview.view(session)
        with v.lock:
            P.send(self.server, sock, session, key_, fields, flush=False)
        self._flush(session)
        log.info(f'[GUILD] {what} from {session.get("char_name")!r} -> 0xB3 sub {fields["sub"]} '
                 f'{fields.get("s23_result", fields.get("s34_result"))} (no Guild Battle before guild-g7)')

    # ================================================ guild-g2 .. g5, g4: helpers ===
    def _cfg(self, name):
        return self.server.config.get(name, cfgmod.DEFAULTS[name])

    @staticmethod
    def _whois(session):
        return session.get('char_name') or session.get('username')

    def _me(self, session):
        """(char, guild copy or None, own member row or None) of the session's character."""
        char = self.server._session_char(session)
        g = self.of_char(char)
        me = g.member(char.get('cid')) if g is not None else None
        return char, g, me

    def _member_by_name(self, g, name):
        """(member row, account, char) of the member of guild copy `g` called `name` NOW (names
        are read from the store by cid, so a renamed member answers to its new name), or None."""
        wanted = key(text(name, NAME_MAX))
        if not wanted:
            return None
        found = self._resolve_chars(m['cid'] for m in g.members)
        for m in g.members:
            hit = found.get(m['cid'])
            if hit is not None and key(hit[1].get('name')) == wanted:
                return m, hit[0], hit[1]
        return None

    def _session_of(self, char_name):
        """The logged-in session of a character on any channel (in world or in a map load), or
        None. The guild is global (blch B.6 / B.7: one World sees every channel)."""
        if not char_name:
            return None
        s = self.server.world.by_char_name(char_name)
        if s is None or s.get('sock') is None or s.get('kicked') or s.get('closed'):
            return None
        return s

    def _member_sessions(self, g, exclude=None):
        """[(member row, char, session)] of the logged-in members of guild copy `g`."""
        found = self._resolve_chars(m['cid'] for m in g.members)
        out = []
        for m in g.members:
            hit = found.get(m['cid'])
            s = self._session_of(hit[1].get('name')) if hit is not None else None
            if s is not None and s is not exclude:
                out.append((m, hit[1], s))
        return out

    def _master_session(self, g):
        master = g.master()
        if master is None:
            return None
        hit = self._resolve_chars([master['cid']]).get(master['cid'])
        return self._session_of(hit[1].get('name')) if hit is not None else None

    def _fan(self, g, packet, exclude=None):
        """Caller holds flow_lock: a member delta to every logged-in member of `g` whose client
        holds g's window (send_sub holds=g.id). Returns the names told."""
        told = []
        for _m, char, s in self._member_sessions(g, exclude=exclude):
            try:
                if self.send_sub(s.get('sock'), s, packet, holds=g.id):
                    told.append(char.get('name'))
            except OSError as e:                       # a member whose connection just closed
                log.debug(f'[GUILD] sub {packet[1]["sub"]} to {char.get("name")!r} not sent: {e}')
        return told

    def _reply(self, sock, session, packet):
        """A sub-code to the requesting client itself (no window gate: it is the answer)."""
        try:
            return self.send_sub(sock, session, packet)
        except OSError as e:
            log.debug(f'[GUILD] sub {packet[1]["sub"]} to {self._whois(session)!r} not sent: {e}')
            return False

    def _gm_visible(self, session):
        return R.gm_tag_visible(session.get('gm') == 1, session.get('gm_hidden'), session.get('gm_tag_off'))

    def _show_tag(self, session, char, tag):
        """The member's nameplate on every client that holds its entity (guild.md 3: 0xB4 on a
        create / admission, 0xB7 on leave / kick / disband [I]) - the records' own rule
        (records.guild_block), so a visible GM keeps "Game Master" and gets nothing. Never the
        member's own client: its sub 1 / 3 / 6 / 8 set its local plate (and the mirror)."""
        if session is None or not session.get('in_world') or not isinstance(char, dict):
            return 0
        block = R.guild_block(char, tag, session)
        value = R.tag_value(block)
        if value == R.GM_TAG:
            return 0
        uid = P.session_uid(session) or 0
        if value >= GUILD_ID_MIN:
            key_, fields = entity_set(uid, value, block['guild_name'], block['guild_emblem_fg'],
                                      block['guild_emblem_bg'])
        else:
            key_, fields = entity_clear(uid)
        return presence.to_holders(self.server, session, key_, fields, 'GUILD')

    def _pay(self, session, gold, reason):
        """Debit `gold` from the session's wallet under its combat lock (the shop rule: the
        balance test and the debit are one step). Never called under flow_lock (no combat lock
        under it: module docstring "Locks")."""
        server = self.server
        wallet = server._wallet_of(session)
        if wallet is None:
            return False
        with server._combat_lock(session):
            if not wallet.pay(gold=gold):
                return False
            server._wallet_commit(session, wallet, reason)
        return True

    def _refund(self, session, gold, reason):
        server = self.server
        wallet = server._wallet_of(session)
        if wallet is None:
            return
        with server._combat_lock(session):
            wallet.earn(gold=gold)
            server._wallet_commit(session, wallet, reason)
        log.info(f'[GUILD] {gold} gold back to {self._whois(session)!r} ({reason})')

    def _purge_stale_applications(self, g):
        """Caller holds flow_lock, right before a sub 4 rebuilds a master's list from the FIFO:
        drop the applications of guild copy `g` whose applicant joined a guild meanwhile or no
        longer exists (kept until now so the master's client list and the FIFO agree), and
        refresh the names of renamed applicants (each row keeps the name the master's client
        was given until this moment - Guilds.renamed). Returns the fresh copy (None when the
        guild is gone)."""
        found = self._resolve_chars(a['cid'] for a in g.applications)
        renamed = []
        with self.lock:
            live = self.db.guilds.get(g.id)
            if live is None:
                return None
            stale = [a for a in live.applications if a['cid'] not in found or a['cid'] in self._tags]
            if stale:
                live.applications = [a for a in live.applications if a not in stale]
                self._commit(f'{live.name!r}: {len(stale)} stale application(s) dropped '
                             f'({[a["name"] for a in stale]}: in a guild now or gone)')
            for a in live.applications:
                now_name = text(found[a['cid']][1].get('name'), NAME_MAX)
                if now_name and a['name'] != now_name:
                    renamed.append(f'{a["name"]} -> {now_name}')
                    a['name'] = now_name
            if renamed:
                self._commit(f'{live.name!r}: application name(s) refreshed for the sub 4: {renamed}')
            out = live.copy()
        if stale or renamed:
            self.request_save()
        return out

    # ---------------------------------------------------- guild-g2: create ---
    def create(self, sock, session, rec):
        """C2S 0x87 GuildCreate {guild_name, master_char_id, emblem_symbol, emblem_bg} (guild F1:
        Moiba's 0x4B5 control 2, after the client's own gates and its optimistic pre-fill of M)
        -> exactly ONE sub 1 (a failure resets the pre-fill; with no answer at all the client
        keeps a phantom guild until its next map load). Success: 50,000 gold debited (the client
        subtracts it itself on sub 1 {1}), sub 1 {1, id}, sub 3 with member_count 0 (name,
        points, max, notice, emblem, the welcome line and the own plate - without duplicating
        the pre-filled own record), 0xB4 to the clients that hold the creator."""
        who = self._whois(session)
        gname = text(rec.get('guild_name'), NAME_MAX)
        fg, bg = _int(rec.get('emblem_symbol')), _int(rec.get('emblem_bg'))
        claimed, own_uid = _int(rec.get('master_char_id')), P.session_uid(session) or 0
        if claimed != own_uid:
            log.warning(f'[GUILD] create from {who!r}: master_char_id {claimed} is not its uid {own_uid} '
                        f'(the session decides)')
        char = self.server._session_char(session)
        result, why = self._create_refusal(session, char, gname, fg, bg)
        if result is None and not self._pay(session, CREATE_GOLD, f'guild create {gname}'):
            result, why = CREATE_NO_GOLD, f'gold < {CREATE_GOLD} at the debit'
        if result is not None:
            self._reply(sock, session, sub1(result))
            log.info(f'[GUILD] create {gname!r} by {who!r} refused: {why} -> 0xB3 sub 1 result {result}')
            return
        now, out, clash = time.time(), None, None
        with self._flow():
            with self.lock:
                clash = self._create_clash(char, gname)
                if clash is None:
                    try:
                        gid = self._next_id()
                    except ValueError as e:
                        clash = (CREATE_FAILED, str(e))
                if clash is None:
                    g = Guild(id=gid, name=gname, emblem_fg=fg, emblem_bg=bg, created_at=now)
                    g.members = [{'cid': char['cid'], 'name': text(char.get('name'), NAME_MAX),
                                  'grade': GRADE_MASTER, 'joined_at': now}]
                    self.db.guilds[gid] = g
                    self.db.last_id = gid
                    self._commit(f'create id={gid} name={gname!r} master={char.get("name")!r} '
                                 f'emblem {fg}/{bg} (50,000 gold)')
                    out = g.copy()
            if out is not None:
                self._reply(sock, session, sub1(CREATE_OK, out.id))
                self._reply(sock, session, sub3(out.id, out.name, out.points, out.max_members, out.notice,
                                                out.emblem_fg, out.emblem_bg))
                v = cview.view(session)
                with v.lock:
                    if v.guild_list == out.id:
                        v.apps_held = True             # a new guild: no application on either side
                self._show_tag(session, char, out.tag())
        if out is None:
            result, why = clash
            self._refund(session, CREATE_GOLD, f'guild create {gname} failed: {why}')
            self._reply(sock, session, sub1(result))
            log.info(f'[GUILD] create {gname!r} by {who!r} refused at the commit: {why} -> 0xB3 sub 1 result {result}')
            return
        self.request_save()

    def _create_refusal(self, session, char, gname, fg, bg):
        """(sub 1 result, why) of the first create gate that fails, in guild.md F1's order, or
        (None, None)."""
        if not isinstance(char, dict) or not _valid_cid(char.get('cid')) or not session.get('in_world'):
            return CREATE_FAILED, 'no character in world'
        if self.tag_of(char) is not None:
            return CREATE_IN_GUILD, f'already in {self.tag_of(char).name!r}'
        if self._gm_visible(session):
            return CREATE_GM, 'a visible GM ("Wind master can\'t register as guild.")'
        if _int(session.get('current_map')) != GUILD_PLAZA_MAP:
            return CREATE_FAILED, f'not on map {GUILD_PLAZA_MAP} (Moiba stands only in the Guild Plaza)'
        bad = names.check(gname) if gname else 'empty'
        if bad is not None:
            return CREATE_BAD_NAME, f'name {gname!r}: {bad}'
        with self.lock:
            taken = any(key(g.name) == key(gname) for g in self.db.guilds.values())
        if taken:
            return CREATE_BAD_NAME, f'a guild named {gname!r} exists'
        wallet = self.server._wallet_of(session)
        if wallet is None or wallet.gold < CREATE_GOLD:
            return CREATE_NO_GOLD, f'gold {wallet.gold if wallet else 0} < {CREATE_GOLD}'
        if R.level_of(char) < CREATE_LEVEL:
            return CREATE_LEVEL_LOW, f'Lv {R.level_of(char)} < {CREATE_LEVEL}'
        if not _int(char.get('job2')):
            return CREATE_NO_JOB2, 'no 2nd class (job2 0)'
        if not emblem_ok(fg, bg):
            return CREATE_FAILED, (f'emblem {fg}/{bg} outside 0..{EMBLEM_FG_MAX}/0..{EMBLEM_BG_MAX} '
                                   f'(empty picker cell)')
        return None, None

    def _create_clash(self, char, gname):
        """Caller holds self.lock: (sub 1 result, why) when the create cannot commit now (a race
        since the gates), else None."""
        if char['cid'] in self._tags:
            return CREATE_IN_GUILD, f'already in {self._tags[char["cid"]].name!r}'
        if any(key(g.name) == key(gname) for g in self.db.guilds.values()):
            return CREATE_BAD_NAME, f'the name {gname!r} was taken meanwhile'
        return None

    # ---------------------------------------------------- guild-g2: disband ---
    def _disband(self, sock, session, rec, char, g, me):
        """C2S 0x8E action 3 (Moiba "Break Guild", sent only when the client's member count is 1)
        -> sub 8 (guild F6): 0xA with members left, 0xB under GUILD_DISBAND_MIN_DAYS [I], else 1
        (clears the local plate and M) + 0xB7 to the clients that hold the master + 0xB8 {gid}
        to the Guild Plaza (its boards, guild-g6)."""
        who, now = self._whois(session), time.time()
        min_days = float(self._cfg('GUILD_DISBAND_MIN_DAYS'))
        result, out = DISBAND_SILENT, None
        with self._flow():
            with self.lock:
                live = self.db.guilds.get(g.id)
                mine = live.member(char['cid']) if live is not None else None
                if mine is None or mine['grade'] != GRADE_MASTER:
                    why = 'not its master'
                elif len(live.members) > 1:
                    result, why = DISBAND_KICK_FIRST, f'{len(live.members) - 1} member(s) left'
                elif min_days > 0 and now - live.created_at < min_days * 86400:
                    result, why = DISBAND_TOO_YOUNG, f'younger than {min_days:g} day(s) (GUILD_DISBAND_MIN_DAYS)'
                else:
                    del self.db.guilds[live.id]
                    self._held_gp.pop(char['cid'], None)
                    self._commit(f'disband id={live.id} {live.name!r} by its master {char.get("name")!r}')
                    result, why, out = DISBAND_OK, 'deleted', live.copy()
            self._reply(sock, session, sub8(result))
            if out is not None:
                self.send_gm_tag(sock, session)
                self._show_tag(session, char, None)
                # guild-g6 (boards.py): its board leaves the store and 0xB8 {gid} goes to 9702 -
                # always, as before g6 (queued: the master himself stands in 9702; _flow()
                # flushes it). flow_lock -> Boards.lock is the boards' lock order.
                self._drop_boards(out.id, 'disband', always=True)
        log.info(f'[GUILD] disband {g.name!r} by {who!r}: {why} -> 0xB3 sub 8 result {result}')
        if out is not None:
            self._forfeit_gp(session, char['cid'], 'its guild was deleted')
            self.request_save()

    # --------------------------------------------------- guild-g3: apply ---
    def apply(self, sock, session, rec):
        """C2S 0x89 GuildJoinRequest {u32 guild_id} (the 0x4B6 dialog behind popup 0x50 control
        12 - which exists only in the KR hui: EN Build 14's player menu has 11 controls, module
        docstring) or {u16 guild_id} (a Guild Plaza board, guild-g6) -> sub 2 (guild F2): 1 "You
        have applied for this guild." with the master online (and sub 2 0x10 + the 130-byte
        record to his client when its application list is synced), 0x11 "admission is being
        delayed" with the master offline [I] (he gets it with sub 4 at his next map load), 6
        "can't recruit any more" for a full / unknown guild, 0 (silent) for what the client
        itself refuses (a member of a guild, a visible GM). Applications are FIFO and one per
        applicant and guild. `!guild apply` runs it for an online character too (dev_apply).
        Returns (sub 2 result, why)."""
        who = self._whois(session)
        gid = _int(rec.get('guild_id'))
        char = self.server._session_char(session)
        result, why, out, added = None, '', None, False
        if not isinstance(char, dict) or not _valid_cid(char.get('cid')):
            result, why = APPLY_SILENT, 'no character'
        elif self.tag_of(char) is not None:
            result, why = APPLY_SILENT, f'already in {self.tag_of(char).name!r} (the client refuses it itself)'
        elif self._gm_visible(session):
            result, why = APPLY_SILENT, 'a visible GM (the client refuses it itself)'
        if result is not None:
            self._reply(sock, session, sub2(result))
            log.info(f'[GUILD] {who!r} applies to guild {gid}: refused, {why} -> 0xB3 sub 2 result {result}')
            return result, why
        cid = char['cid']
        with self._flow():
            with self.lock:
                live = self.db.guilds.get(gid) if GUILD_ID_MIN <= gid <= GUILD_ID_MAX else None
                if live is None:
                    result, why = APPLY_FULL, 'no such guild'
                elif len(live.members) >= live.max_members:
                    result, why = APPLY_FULL, f'full ({len(live.members)}/{live.max_members})'
                elif not any(a['cid'] == cid for a in live.applications):
                    if len(live.applications) >= MAX_APPLICATIONS:
                        result, why = APPLY_FULL, f'{MAX_APPLICATIONS} applications pending'
                    else:
                        live.applications.append({'cid': cid, 'name': text(char.get('name'), NAME_MAX),
                                                  'level': R.level_of(char),
                                                  'job1': _clamp(char.get('class'), 0, JOB1_MAX),
                                                  'job2': _clamp(char.get('job2'), 0, JOB2_MAX), 'at': time.time()})
                        added = True
                        self._commit(f'{live.name!r}: application of {char.get("name")!r} '
                                     f'({len(live.applications)} pending)')
                if live is not None:
                    out = live.copy()
            if result is None:
                master = self._master_session(out)
                if master is None:
                    result, why = APPLY_DELAYED, 'the master is offline: sub 4 at his next map load'
                else:
                    result, why = APPLY_OK, 'already pending' if not added else 'queued'
                    if added:
                        app = out.applications[-1]
                        record = application_record(app['name'], app['level'], app['job1'], app['job2'])
                        try:
                            pushed = self.send_sub(master.get('sock'), master, sub2(APPLY_PUSH, record),
                                                   holds=out.id, apps=True)
                        except OSError:
                            pushed = False
                        why += (f', pushed to the master {master.get("char_name")!r}' if pushed else
                                ', the master gets it with his next sub 4')
            self._reply(sock, session, sub2(result))
        log.info(f'[GUILD] {who!r} applies to guild {gid}{f" {out.name!r}" if out else ""}: {why} '
                 f'-> 0xB3 sub 2 result {result}')
        if added:
            self.request_save()
        return result, why

    # ---------------------------------------------- guild-g3: accept / reject ---
    def accept(self, sock, session, rec):
        """C2S 0x8B {applicant_name} (0x4BC Accept by the master; the client popped its FIRST
        application) -> sub 5 (guild F3): 1 + the admission (grade 1 Trainee [I]): sub 13 to the
        members' windows, sub 3 (the whole list) to the new member's client, 0xB4 to the clients
        that hold it; 5 when it joined another guild meanwhile; else "can't recruit". The row is
        found by the name the master's client holds (FIFO normally makes it the first; a rename
        of the applicant does not rewrite it - Guilds.renamed - so the old name still matches).
        A name the server does not know pops nothing: with renames paired, what is left are rows
        only the client holds (an admin `!guild apps clear` / seed, a forged name), and popping
        applications[0] there - guild.md F3 step 1 - would drop a live application the client
        still shows [deviation, P14 review]; the master's next map load rebuilds his list."""
        who = self._whois(session)
        name = text(rec.get('applicant_name'), NAME_MAX)
        char, g, me = self._me(session)
        if g is None or me is None or me['grade'] != GRADE_MASTER:
            self._reply(sock, session, sub5(ADMIT_FAILED))
            log.info(f'[GUILD] accept {name!r} by {who!r} refused: not a guild master -> 0xB3 sub 5 result 0')
            return
        result, why, out, hit = ADMIT_FAILED, '', None, None
        with self._flow():
            with self.lock:
                live = self.db.guilds.get(g.id)
                app = next((dict(a) for a in (live.applications if live else ()) if key(a['name']) == key(name)), None)
            if app is not None:
                hit = self._resolve_chars([app['cid']]).get(app['cid'])
            with self.lock:
                live = self.db.guilds.get(g.id)
                mine = live.member(char['cid']) if live is not None else None
                idx = next((i for i, a in enumerate(live.applications) if app and a['cid'] == app['cid']), None) \
                    if live is not None else None
                if mine is None or mine['grade'] != GRADE_MASTER:
                    why = 'no longer its master'
                elif idx is None:
                    why = 'no such application (a stale row on the client)'
                else:
                    del live.applications[idx]              # popped whatever the outcome (the client did)
                    if hit is None:
                        why = 'the applicant no longer exists'
                    elif app['cid'] in self._tags:
                        result, why = ADMIT_IN_GUILD, f'{hit[1].get("name")!r} is in {self._tags[app["cid"]].name!r} now'
                    elif len(live.members) >= live.max_members:
                        why = f'full ({len(live.members)}/{live.max_members})'
                    else:
                        live.members.append({'cid': app['cid'], 'name': text(hit[1].get('name'), NAME_MAX),
                                             'grade': GRADE_TRAINEE, 'joined_at': time.time()})
                        result, why = ADMIT_OK, 'admitted as Trainee'
                    self._commit(f'{live.name!r}: application of {app["name"]!r} {why}')
                    out = live.copy()
            self._reply(sock, session, sub5(result))
            if result == ADMIT_OK:
                acc, achar = hit
                anew = self._session_of(achar.get('name'))
                told = self._fan(out, sub13(acc.get('uid', 0), achar.get('name'), self._online_channel(achar.get('name')),
                                            R.level_of(achar), achar.get('class', 0), achar.get('job2', 0),
                                            GRADE_TRAINEE), exclude=anew)
                why += f'; sub 13 to {told}'
                if anew is not None:
                    why += '; sub 3 to the new member' if self._admit_client(anew, achar, out) else \
                        '; the new member gets its list at its next map load'
        log.info(f'[GUILD] accept {name!r} by {who!r}: {why} -> 0xB3 sub 5 result {result}')
        if out is not None:
            self.request_save()

    def _admit_client(self, session, char, g):
        """Caller holds flow_lock: the new member's own client gets the whole guild (its M is
        empty: it got sub 15 at its last map load), the GM tag back for a visible GM, and the
        clients that hold it its tag."""
        sent = False
        try:
            packet = self.info_fields(g)               # the store is read outside the view lock
            v = cview.view(session)
            with v.lock:                               # RLock: send_sub takes it again
                sent = self.send_sub(session.get('sock'), session, packet)
                if sent:
                    session[ADMIT_LOAD_KEY] = v.loads  # this map load's 0x8A sends no second one
            if sent:
                self.send_gm_tag(session.get('sock'), session)
        except OSError as e:
            log.debug(f'[GUILD] admission of {char.get("name")!r}: sub 3 not sent ({e})')
        self._show_tag(session, char, g.tag())
        return sent

    def reject(self, sock, session, rec=None):
        """C2S 0x8C (empty: 0x4BC Deny, or Accept pressed by a non-master): the client popped its
        FIRST application, so the server pops applications[0] (guild F3 step 3). No reply (none
        is traced); a non-master's request only cleared its own client's copy. A master whose
        list is stale (clientview apps_stale: he was master before, see change_master) popped a
        row that no longer pairs with the FIFO: nothing is popped until his next map load's
        sub 4 rebuilds that list."""
        who = self._whois(session)
        char, g, me = self._me(session)
        if g is None or me is None or me['grade'] != GRADE_MASTER:
            log.info(f'[GUILD] reject by {who!r}: not a guild master - nothing to pop server-side')
            return
        v = cview.view(session)
        with v.lock:
            stale = v.apps_stale
        if stale:
            log.info(f'[GUILD] reject by {who!r}: his application list is stale since a master change - '
                     f'nothing popped (his next map load rebuilds it)')
            return
        with self._flow():
            with self.lock:
                live = self.db.guilds.get(g.id)
                gone = live.applications.pop(0) if live is not None and live.applications else None
                if gone is not None:
                    self._commit(f'{live.name!r}: application of {gone["name"]!r} rejected by {who!r} '
                                 f'({len(live.applications)} left)')
        log.info(f'[GUILD] reject by {who!r}: ' + (f'{gone["name"]!r} dropped' if gone else 'no application pending'))
        if gone is not None:
            self.request_save()

    # ----------------------------------- guild-g3: 0x8E kick / leave / disband ---
    def member_action(self, sock, session, rec):
        """C2S 0x8E GuildMemberAction {guild_id, char_id, char_name, action} (confirm 0x4C2):
        1 kick (the master: sub 18), 2 leave (sub 6), 3 disband (sub 8)."""
        action = _int(rec.get('action'))
        char, g, me = self._me(session)
        gid = _int(rec.get('guild_id'))
        if g is not None and gid != g.id:
            log.warning(f'[GUILD] 0x8E action {action} from {self._whois(session)!r}: guild id {gid} is not '
                        f'its guild {g.id} - refused')
            g = me = None
        if action == ACT_KICK:
            self._kick(sock, session, rec, char, g, me)
        elif action == ACT_LEAVE:
            self._leave(sock, session, rec, char, g, me)
        elif action == ACT_DISBAND:
            if g is None or me is None:
                self._reply(sock, session, sub8(DISBAND_SILENT))
                log.info(f'[GUILD] disband by {self._whois(session)!r}: not in that guild -> 0xB3 sub 8 result 0')
                return
            self._disband(sock, session, rec, char, g, me)
        else:
            log.warning(f'[GUILD] 0x8E from {self._whois(session)!r}: unknown action {action} - dropped')

    def _leave(self, sock, session, rec, char, g, me):
        """Action 2 (a non-master's "Leave") -> sub 6 (guild F4): 1 = out (clears its plate and
        M, "You are out of this guild."), sub 14 {name} to the others' windows, 0xB7 to the
        clients that hold it; 0 for the master (he hands over or breaks the guild) [I]; 0xC
        "after 1 day" under GUILD_LEAVE_MIN_HOURS [I]."""
        who, now = self._whois(session), time.time()
        named = text(rec.get('char_name'), NAME_MAX)
        if isinstance(char, dict) and key(named) != key(char.get('name')):
            log.warning(f'[GUILD] leave from {who!r} names {named!r}: the session decides')
        hours = float(self._cfg('GUILD_LEAVE_MIN_HOURS'))
        result, out = LEFT_FAILED, None
        if g is None or me is None:
            why = 'not in that guild'
        elif me['grade'] == GRADE_MASTER:
            why = 'the master cannot leave (Change Guild Master or Break Guild)'
        elif hours > 0 and now - me['joined_at'] < hours * 3600:
            result, why = LEFT_TOO_SOON, f'joined less than {hours:g} h ago (GUILD_LEAVE_MIN_HOURS)'
        else:
            why = ''
        if why:
            self._reply(sock, session, sub6(result))
            log.info(f'[GUILD] leave by {who!r}: {why} -> 0xB3 sub 6 result {result}')
            return
        with self._flow():
            with self.lock:
                live = self.db.guilds.get(g.id)
                mine = live.member(char['cid']) if live is not None else None
                if mine is not None and mine['grade'] != GRADE_MASTER:
                    live.members.remove(mine)
                    self._commit(f'{live.name!r}: {char.get("name")!r} left ({len(live.members)} member(s))')
                    out = live.copy()
            if out is None:
                self._reply(sock, session, sub6(LEFT_FAILED))
                log.info(f'[GUILD] leave by {who!r}: no longer a member -> 0xB3 sub 6 result 0')
                return
            self._reply(sock, session, sub6(LEFT_OK))
            self.send_gm_tag(sock, session)
            told = self._fan(out, sub14(char.get('name')), exclude=session)
            self._show_tag(session, char, None)
        log.info(f'[GUILD] leave {out.name!r} by {who!r} -> 0xB3 sub 6 result 1, sub 14 to {told}')
        self._forfeit_gp(session, char['cid'], 'it left its guild')
        self.request_save()

    def _kick(self, sock, session, rec, char, g, me):
        """Action 1 (the master's "Kick" on a selected row) -> sub 18 (guild F5): 1 + sub 14 to
        the remaining members' windows (the master's included), sub 6 {1} to the kicked member's
        client ("You are out of this guild.", its plate and M cleared), 0xB7 to the clients that
        hold it; 0 for a non-master, the master himself or an unknown member."""
        who = self._whois(session)
        named = text(rec.get('char_name'), NAME_MAX)
        target = self._member_by_name(g, named) if g is not None and me is not None else None
        why = ''
        if g is None or me is None:
            why = 'not in that guild'
        elif me['grade'] != GRADE_MASTER:
            why = 'only the master kicks'
        elif target is None:
            why = f'{named!r} is no member'
        elif target[0]['grade'] == GRADE_MASTER or target[0]['cid'] == char['cid']:
            why = 'the master cannot be kicked'
        if why:
            self._reply(sock, session, sub18(KICK_FAILED))
            log.info(f'[GUILD] kick {named!r} by {who!r}: {why} -> 0xB3 sub 18 result 0')
            return
        t, tacc, tchar = target
        if _int(rec.get('char_id')) != _int(tacc.get('uid')):
            log.warning(f'[GUILD] kick {named!r}: char_id {rec.get("char_id")} is not its uid {tacc.get("uid")} '
                        f'(the name decides)')
        out = None
        with self._flow():
            with self.lock:
                live = self.db.guilds.get(g.id)
                mine = live.member(char['cid']) if live is not None else None
                gone = live.member(t['cid']) if live is not None else None
                if mine is not None and mine['grade'] == GRADE_MASTER and gone is not None and gone['grade'] != GRADE_MASTER:
                    live.members.remove(gone)
                    self._commit(f'{live.name!r}: {tchar.get("name")!r} kicked by {who!r} '
                                 f'({len(live.members)} member(s))')
                    out = live.copy()
            if out is None:
                self._reply(sock, session, sub18(KICK_FAILED))
                log.info(f'[GUILD] kick {named!r} by {who!r}: changed meanwhile -> 0xB3 sub 18 result 0')
                return
            self._reply(sock, session, sub18(KICK_OK))
            told = self._fan(out, sub14(tchar.get('name')))
            ts = self._session_of(tchar.get('name'))
            if ts is not None:
                try:
                    if self.send_sub(ts.get('sock'), ts, sub6(LEFT_OK)):
                        self.send_gm_tag(ts.get('sock'), ts)
                except OSError as e:
                    log.debug(f'[GUILD] kick: sub 6 to {tchar.get("name")!r} not sent ({e})')
                self._show_tag(ts, tchar, None)
        log.info(f'[GUILD] kick {tchar.get("name")!r} from {out.name!r} by {who!r} -> 0xB3 sub 18 result 1, '
                 f'sub 14 to {told}' + (', sub 6 to the kicked member' if ts is not None else ' (offline)'))
        self._forfeit_gp(ts, t['cid'], 'kicked from its guild')
        self.request_save()

    # ----------------------------------------- guild-g3: login / logout lines ---
    def logged_in(self, session, hop=None):
        """world.ON_ENTER_WORLD of the connection's FIRST entry (guild F0): sub 20 {name,
        channel} to the other members' windows ("[X] has logged in.", the row's online byte = the
        channel [I]). A 2009 channel hop continues the login (arch09-session-continuity): no
        line - unless its logout already went out (hop.logout_announced)."""
        if hop is not None and not hop.logout_announced:
            return
        char, g, me = self._me(session)
        if g is None or me is None:
            return
        channel = self.server._channel_no(session)
        with self._flow():
            g = self.of_char(char)
            if g is None:
                return
            told = self._fan(g, sub20(char.get('name'), channel), exclude=session)
        log.info(f'[GUILD] {char.get("name")!r} logged in (channel {channel}): 0xB3 sub 20 to {told}')

    def departed(self, session):
        """world.ON_LEAVE_WORLD / ON_DISCONNECT: the session's uncredited guild points wait by
        cid for the logout of its LOGIN - a channel hop continues it on a new connection, whose
        own points then add up (guild F12, arch09-session-continuity)."""
        with cview.lock(session):
            n = _int(session.pop('guild_pending_gp', 0))
        if n <= 0:
            return
        cid = self._cid_of(session.get('char_name'))
        if cid is None:
            log.info(f'[GUILD] {n} guild point(s) of {session.get("char_name")!r} dropped: no such character')
            return
        with self.lock:
            self._held_gp[cid] = self._held_gp.get(cid, 0) + n

    def _cid_of(self, name):
        hit = self.server.store.character_by_name(name) if name else None
        cid = hit[2].get('cid') if hit else None
        return cid if _valid_cid(cid) else None

    def _take_gp(self, session, cid):
        """Every guild point of the character not credited yet: its session's own (accrue) and
        what the earlier connections of the same login left (departed)."""
        n = 0
        if session is not None:
            with cview.lock(session):
                n += _int(session.pop('guild_pending_gp', 0))
        if cid is not None:
            with self.lock:
                n += self._held_gp.pop(cid, 0)
        return n

    def _forfeit_gp(self, session, cid, why):
        """Leave / kick / disband: the points the character earned and had not been credited
        are dropped [I]: crediting them silently would make the next sub 21's "+N" (new total
        minus the total each client holds) count them under another member's name."""
        n = self._take_gp(session, cid)
        if n:
            log.info(f'[GUILD] {n} uncredited guild point(s) of cid {cid} forfeited ({why})')

    def logged_out(self, session, char_name=None):
        """world.ON_LOGOUT (continuity.py: at once on a quit, held back while a 2009 channel hop
        may continue the login, never for the hop itself): the login's guild points go to the
        guild (guild F12 step 2) and the other members' windows get sub 21 {name, new total}
        ("[X] has logged out." + "Guild point(+N : [X]) increased.", N = the total minus the
        one each client holds)."""
        name = char_name or session.get('char_name')
        hit = self.server.store.character_by_name(name) if name else None
        char = hit[2] if hit else None
        cid = char.get('cid') if isinstance(char, dict) else None
        gp = self._take_gp(session, cid if _valid_cid(cid) else None)
        g = self.of_char(char)
        if g is None:
            if gp:
                log.info(f'[GUILD] {gp} guild point(s) of {name!r} dropped: in no guild at its logout')
            return
        out = None
        with self._flow():
            with self.lock:
                live = self.db.guilds.get(g.id)
                if live is not None and live.member(cid) is not None:
                    if gp:
                        live.points = min(0xFFFFFFFF, live.points + gp)
                        self._commit(f'{live.name!r}: +{gp} guild point(s) from {name!r} at its logout '
                                     f'(total {live.points}, Lv.{live.level})')
                    out = live.copy()
            if out is None:
                return
            told = self._fan(out, sub21(char.get('name'), out.points), exclude=self._session_of(char.get('name')))
        log.info(f'[GUILD] {char.get("name")!r} logged out: +{gp} -> {out.name!r} {out.points} points; '
                 f'0xB3 sub 21 to {told}')
        if gp:
            self.request_save()

    # ------------------------------------------------------- guild-g5: chat ---
    def chat(self, sock, session, rec):
        """C2S 0x8D GuildChat {guild_id, text_len, "Name : msg"} (/g, chat mode 3; the client
        already echoed it, so NOTHING goes back: registry BUILD_NEVER_REPLY) -> S2C 0xB5 {line}
        (the guild colour 0xFFFFC800, the client's own /g echo colour [V]) to every OTHER member
        in world on any channel (guild F11). The line is rebuilt with the real name
        (chat.friend_line: anti-spoof, <= 87 bytes); a sender in no guild or naming another
        guild id (the client sends M+0x44 even when it is 0 or 0xFFFF) is dropped, and so is a
        line within GUILD_CHAT_MIN_SECS (700 ms, the client's own anti-spam) of its last one.
        A member who blacklisted the sender does not get it (the 0x91 fan-out rule, bl-3)."""
        who = self._whois(session)
        char, g, me = self._me(session)
        gid = _int(rec.get('guild_id'))
        if g is None or me is None or gid != g.id:
            log.info(f'[GUILD] chat from {who!r} dropped: guild id {gid}, '
                     + (f'its guild is {g.id}' if g else 'in no guild'))
            return
        now = time.monotonic()
        last = session.get('last_guild_chat')
        gap = float(self._cfg('GUILD_CHAT_MIN_SECS'))
        if last is not None and now - last < gap:
            log.info(f'[GUILD] chat from {who!r} dropped: {now - last:.2f} s after its last line (< {gap:g} s)')
            return
        raw = P.to_bytes(rec.get('text', b''))
        msg = raw[:_int(rec.get('text_len'), len(raw))].split(b'\x00', 1)[0]
        line = chatmod.friend_line(char.get('name'), msg)
        if not line[len(chatmod.name_bytes(char.get('name')) + chatmod.FRIEND_LINE_SEP):].strip():
            log.info(f'[GUILD] chat from {who!r} dropped: empty')
            return
        session['last_guild_chat'] = now
        key_, fields = chat_line(line)
        heard, skipped = [], []
        blacklist = getattr(self.server, 'blacklist', None)
        for _m, mchar, s in self._member_sessions(g, exclude=session):
            if not worldmod.reachable(s):
                skipped.append(f'{mchar.get("name")} (map load)')
            elif blacklist is not None and blacklist.hides(s, session):
                skipped.append(f'{mchar.get("name")} (blacklisted the sender)')
            elif self.server._push(s, key_, fields, 'GUILD'):
                heard.append(mchar.get('name'))
        log.info(f'[GUILD] {line.decode("cp949", "replace")!r} ({g.name}) -> 0xB5 to {heard}'
                 + (f'; not: {skipped}' if skipped else ''))
        return heard

    # ------------------------------------------------- guild-g4: management ---
    def change_grade(self, sock, session, rec):
        """C2S 0x92 {guild_id, member_id, member_name, grade} (the 0x4B3 grade buttons) ->
        exactly ONE sub 16 (the client hides its grade buttons only on sub 16, guild F7 step 3):
        1 + sub 17 {name, grade} to the members' windows; 0xD at 4 Guardians, 0xE at 10
        Vanguards; 0 otherwise. Who may change grades: GUILD_GRADE_MIN_GRADE (default the master
        only; the client does not gate it [V], guild.md 1.4)."""
        who = self._whois(session)
        named, grade = text(rec.get('member_name'), NAME_MAX), _int(rec.get('grade'))
        char, g, me = self._me(session)
        min_grade = int(self._cfg('GUILD_GRADE_MIN_GRADE'))
        target = self._member_by_name(g, named) if g is not None and me is not None else None
        why = ''
        if g is None or me is None or _int(rec.get('guild_id')) != g.id:
            why = 'not in that guild'
        elif me['grade'] < min_grade:
            why = f'grade {me["grade"]} may not change grades (GUILD_GRADE_MIN_GRADE {min_grade})'
        elif not GRADE_TRAINEE <= grade <= GRADE_GUARDIAN:
            why = f'grade {grade} (0x92 sets 1..4; the master changes by 0x91)'
        elif target is None:
            why = f'{named!r} is no member'
        elif target[0]['grade'] == GRADE_MASTER:
            why = "the master's grade changes only with a master change"
        elif me['grade'] != GRADE_MASTER and (target[0]['grade'] >= me['grade'] or grade >= me['grade']):
            why = f'a grade-{me["grade"]} member changes only lower grades'
        if why:
            self._reply(sock, session, sub16(GRADE_FAILED))
            log.info(f'[GUILD] grade {named!r} -> {grade} by {who!r}: {why} -> 0xB3 sub 16 result 0')
            return
        t, _tacc, tchar = target
        result, out = GRADE_FAILED, None
        with self._flow():
            with self.lock:
                live = self.db.guilds.get(g.id)
                row = live.member(t['cid']) if live is not None else None
                if row is None or row['grade'] == GRADE_MASTER:
                    why = 'changed meanwhile'
                else:
                    others = sum(1 for m in live.members if m['grade'] == grade and m['cid'] != row['cid'])
                    if grade == GRADE_GUARDIAN and others >= MAX_GUARDIANS:
                        result, why = GRADE_GUARDIANS_FULL, f'{MAX_GUARDIANS} Guardians already'
                    elif grade == GRADE_VANGUARD and others >= MAX_VANGUARDS:
                        result, why = GRADE_VANGUARDS_FULL, f'{MAX_VANGUARDS} Vanguards already'
                    else:
                        old, row['grade'] = row['grade'], grade
                        result, why = GRADE_OK, f'{GRADE_NAMES[old]} -> {GRADE_NAMES[grade]}'
                        if old != grade:
                            self._commit(f'{live.name!r}: {tchar.get("name")!r} {why} by {who!r}')
                        out = live.copy()
            self._reply(sock, session, sub16(result))
            told = self._fan(out, sub17(tchar.get('name'), grade)) if out is not None else []
        log.info(f'[GUILD] grade {tchar.get("name")!r} -> {grade} by {who!r}: {why} -> 0xB3 sub 16 result {result}'
                 + (f', sub 17 to {told}' if out is not None else ''))
        if out is not None:
            self.request_save()

    def change_master(self, sock, session, rec):
        """C2S 0x91 {guild_id, new_master_name} (Moiba "Change Guild Master", master only) ->
        sub 11 (guild F8): 1 + sub 12 {new name} to the members' windows - the new master grade
        5, the old one grade 1 (the client's FUN_00485200 does exactly that, so the server
        mirrors it) - and the pending applications (sub 4) to the new master's client; 8 "This
        member can't be the guild master." for a non-member (or, with
        GUILD_MASTER_NEEDS_CREATE_RULES, one below Lv 30 / without a 2nd class); 0 otherwise.
        The old master's client keeps its application list (sub 12 does not clear M+0xE0), no
        longer in step with the FIFO: its view goes apps_held False / apps_stale True until its
        next map load (no push to it, its 0x8C pops nothing, and a re-promotion before that
        load sends it no sub 4 - it would append to the stale rows)."""
        who = self._whois(session)
        named = text(rec.get('new_master_name'), NAME_MAX)
        char, g, me = self._me(session)
        target = self._member_by_name(g, named) if g is not None and me is not None else None
        result, why = MASTER_FAILED, ''
        if g is None or me is None or _int(rec.get('guild_id')) != g.id:
            why = 'not in that guild'
        elif me['grade'] != GRADE_MASTER:
            why = 'not the master'
        elif target is None:
            result, why = MASTER_NOT_ELIGIBLE, f'{named!r} is no member'
        elif target[0]['cid'] == char['cid']:
            why = 'already the master'
        elif bool(self._cfg('GUILD_MASTER_NEEDS_CREATE_RULES')) and (
                R.level_of(target[2]) < CREATE_LEVEL or not _int(target[2].get('job2'))):
            result, why = MASTER_NOT_ELIGIBLE, 'below the create prerequisites (GUILD_MASTER_NEEDS_CREATE_RULES)'
        if why:
            self._reply(sock, session, sub11(result))
            log.info(f'[GUILD] master change to {named!r} by {who!r}: {why} -> 0xB3 sub 11 result {result}')
            return
        t, _tacc, tchar = target
        out = None
        with self._flow():
            with self.lock:
                live = self.db.guilds.get(g.id)
                mine = live.member(char['cid']) if live is not None else None
                row = live.member(t['cid']) if live is not None else None
                if mine is not None and mine['grade'] == GRADE_MASTER and row is not None:
                    mine['grade'], row['grade'] = GRADE_TRAINEE, GRADE_MASTER
                    self._commit(f'{live.name!r}: master {char.get("name")!r} -> {tchar.get("name")!r} '
                                 f'(the old master is a Trainee now)')
                    out = live.copy()
            self._reply(sock, session, sub11(MASTER_OK if out is not None else MASTER_FAILED))
            if out is None:
                log.info(f'[GUILD] master change to {named!r} by {who!r}: changed meanwhile -> 0xB3 sub 11 result 0')
                return
            old = cview.view(session)
            with old.lock:
                old.apps_stale = old.apps_stale or old.apps_held
                old.apps_held = False
            told = self._fan(out, sub12(tchar.get('name')))
            ts = self._session_of(tchar.get('name'))
            synced, note = False, ''
            if ts is not None:
                v = cview.view(ts)
                with v.lock:
                    held, stale = v.apps_held, v.apps_stale
                if stale:               # a master again before his next map load
                    note = ', no sub 4: the new master\'s list is stale until his next map load'
                elif not held:          # its M+0xE0 is empty (a non-master never gets sub 2 / 4)
                    out = self._purge_stale_applications(out) or out
                    synced = self._send_applications(ts.get('sock'), ts, out)
        log.info(f'[GUILD] master change {out.name!r}: {char.get("name")!r} -> {tchar.get("name")!r} -> 0xB3 '
                 f'sub 11 result 1, sub 12 to {told}' + (', sub 4 to the new master' if synced else note))
        self.request_save()

    def change_notice(self, sock, session, rec):
        """C2S 0x8F {notice str[61]} (Moiba "Edit Guild News", master only; the client copied it
        into M+0x88 first) -> sub 7: 1 "Notice has been changed." (the others see it at their
        next map load: no push sub-code exists [V], guild F9); 0 (which also blanks the
        requester's copy) for a non-master or an empty text. Stored <= 60 bytes."""
        who = self._whois(session)
        notice = text(rec.get('notice'), NOTICE_MAX)
        char, g, me = self._me(session)
        why, result = '', NOTICE_FAILED
        if g is None or me is None or me['grade'] != GRADE_MASTER:
            why = 'not a guild master'
        elif not notice.strip():
            why = 'empty'
        else:
            with self._flow():
                with self.lock:
                    live = self.db.guilds.get(g.id)
                    mine = live.member(char['cid']) if live is not None else None
                    if mine is not None and mine['grade'] == GRADE_MASTER:
                        live.notice = notice
                        self._commit(f'{live.name!r}: notice set by {who!r}: {notice!r}')
                        result = NOTICE_OK
                    else:
                        why = 'changed meanwhile'
        self._reply(sock, session, sub7(result))
        log.info(f'[GUILD] notice by {who!r}: {why or "changed"} -> 0xB3 sub 7 result {result}')
        if result == NOTICE_OK:
            self.request_save()

    def increase_capacity(self, sock, session, rec):
        """C2S 0x90 {guild_id, add_members, gold_cost} (Moiba "Increase Capacity" -> 0x4BD OK) ->
        sub 9 (guild F10): the server recomputes the client's tier (FUN_00484f40: the first of
        20 / 30 / 40 / 50 above the current max, for 6,500 / 20,000 / 60,000 / 100,000 gold at
        guild Lv 2 / 3 / 4 / 5 from the points) and refuses anything else. Success: the gold is
        debited (the client subtracts `cost` itself on sub 9 {1, cost}), sub 9 {1, cost} to the
        master and sub 10 {max} to every member's window, the master's included (sub 9 does
        not update M+0x58 [V])."""
        who = self._whois(session)
        add, cost = _int(rec.get('add_members')), _int(rec.get('gold_cost'))
        char, g, me = self._me(session)
        tier = next_tier(g.max_members) if g is not None else None
        why = ''
        if g is None or me is None or _int(rec.get('guild_id')) != g.id:
            why = 'not in that guild'
        elif me['grade'] != GRADE_MASTER:
            why = 'not the master'
        elif tier is None:
            why = f'max {g.max_members}: no tier above'
        elif g.level < tier[2]:
            why = f'guild Lv.{g.level} < {tier[2]} for {tier[0]} members'
        elif add != tier[0] - g.max_members or cost != tier[1]:
            why = f'asked +{add} for {cost} gold; the tier is +{tier[0] - g.max_members} for {tier[1]}'
        elif not self._pay(session, tier[1], f'guild capacity {g.name} -> {tier[0]}'):
            why = f'gold < {tier[1]}'
        if why:
            self._reply(sock, session, sub9(False))
            log.info(f'[GUILD] capacity +{add} ({cost} gold) by {who!r}: {why} -> 0xB3 sub 9 ok 0')
            return
        out = None
        with self._flow():
            with self.lock:
                live = self.db.guilds.get(g.id)
                mine = live.member(char['cid']) if live is not None else None
                if mine is not None and mine['grade'] == GRADE_MASTER and live.max_members == g.max_members:
                    live.max_members = tier[0]
                    self._commit(f'{live.name!r}: max members {g.max_members} -> {tier[0]} by {who!r} '
                                 f'({tier[1]} gold)')
                    out = live.copy()
            if out is not None:
                self._reply(sock, session, sub9(True, tier[1]))
                told = self._fan(out, sub10(out.max_members))
        if out is None:
            self._refund(session, tier[1], 'guild capacity changed meanwhile')
            self._reply(sock, session, sub9(False))
            log.info(f'[GUILD] capacity by {who!r}: changed meanwhile -> 0xB3 sub 9 ok 0')
            return
        log.info(f'[GUILD] capacity {out.name!r} -> {out.max_members} by {who!r} ({tier[1]} gold) -> 0xB3 sub 9 ok 1, '
                 f'sub 10 to {told}')
        self.request_save()

    def renamed(self, session, old, new, cid=None):
        """world.ON_RENAME (ROADMAP_2009_ADDENDUM C4, premium_cash-rename): members and
        applications are keyed by cid, so only the cached display names change; every member's
        window gets sub 22 {old, new} (the renamed one's too: when it is the master the client's
        own "am I the master" test compares its name with M+0x64, which sub 22 renames) [V
        handler, I trigger]. Its plate is the record's: nothing to do here.
        An APPLICATION keeps its name (P14 review): no sub-code renames a row of the master's
        M+0xE0, so his client still holds - and its Accept 0x8B sends - the old name; the FIFO
        and the client list stay paired by it until the master's next sub 4 sync refreshes it
        (_purge_stale_applications)."""
        if not _valid_cid(cid):
            cid = self._cid_of(new)
        if cid is None:
            return []
        changed, out = False, None
        with self._flow():
            with self.lock:
                for g in self.db.guilds.values():
                    for row in g.members:
                        if row['cid'] == cid and row['name'] != text(new, NAME_MAX):
                            row['name'] = text(new, NAME_MAX)
                            changed = True
                    if g.member(cid) is not None:
                        out = g.copy()
                if changed:
                    self._commit(f'cid {cid} renamed {old!r} -> {new!r}')
            told = self._fan(out, sub22(old, new)) if out is not None else []
        if out is not None:
            log.info(f'[GUILD] {out.name!r}: {old!r} is now {new!r} -> 0xB3 sub 22 to {told}')
        if changed:
            self.request_save()
        return told

    # ------------------------------------------------------------ mutation ---
    def _commit(self, why):
        """Caller holds self.lock: index, mark dirty, schedule the save."""
        self._reindex()
        self.db._mark()
        log.info(f'[GUILD] {why}')

    def seed(self, name, master, members=(), now=None):
        """Admin seeding: a guild `name` with `master` (grade 5) and `members` (Trainees), by
        character name. Returns the new Guild copy. Raises ValueError with the reason."""
        if not self.supported:
            raise ValueError('guilds need the 2009 client (CLIENT_BUILD 2009)')
        gname = text(name, NAME_MAX)
        bad = names.check(gname) if gname else 'empty'
        if bad is not None or gname != str(name):
            raise ValueError(f'guild name {name!r}: {bad or "longer than 16 bytes"}')
        wanted = [master] + [m for m in members]
        chars = []
        for who in wanted:
            hit = self.server.store.character_by_name(who)
            if hit is None:
                raise ValueError(f'no character named {who!r}')
            char = hit[2]
            if not _valid_cid(char.get('cid')):
                raise ValueError(f'{char.get("name")!r} has no character id (cid)')
            if any(c.get('cid') == char['cid'] for c in chars):
                raise ValueError(f'{char.get("name")!r} is named twice')
            chars.append(char)
        if len(chars) > BASE_MAX_MEMBERS:
            raise ValueError(f'{len(chars)} members: a new guild holds {BASE_MAX_MEMBERS}')
        now = time.time() if now is None else now
        with self.lock:
            if any(key(g.name) == key(gname) for g in self.db.guilds.values()):
                raise ValueError(f'a guild named {gname!r} exists')
            for char in chars:
                if char['cid'] in self._tags:
                    raise ValueError(f'{char.get("name")!r} is in {self._tags[char["cid"]].name!r} already')
            gid = self._next_id()
            g = Guild(id=gid, name=gname, created_at=now)
            g.members = [{'cid': c['cid'], 'name': text(c.get('name'), NAME_MAX),
                          'grade': GRADE_MASTER if i == 0 else GRADE_TRAINEE, 'joined_at': now}
                         for i, c in enumerate(chars)]
            for c in chars:                         # an application elsewhere is moot now
                for other in self.db.guilds.values():
                    other.applications = [a for a in other.applications if a['cid'] != c['cid']]
            self.db.guilds[gid] = g
            self.db.last_id = gid
            self._commit(f'seed {gid} {gname!r}: master {chars[0].get("name")!r}, '
                         f'members {[c.get("name") for c in chars[1:]]}')
            out = g.copy()
        self.request_save()
        return out

    def _next_id(self):
        """Caller holds self.lock: last_id + 1, never reused; wraps to the lowest free id."""
        nxt = self.db.last_id + 1
        if nxt <= GUILD_ID_MAX and nxt not in self.db.guilds:
            return nxt
        for gid in range(GUILD_ID_MIN, GUILD_ID_MAX + 1):
            if gid not in self.db.guilds:
                return gid
        raise ValueError('no free guild id (2..0xFFFE)')

    def disband(self, name):
        """Admin: delete a guild. The members' clients keep it until their next map load (their
        mirror too: the 0x21 tail follows what each client holds, not the store)."""
        with self.lock:
            g = next((x for x in self.db.guilds.values() if key(x.name) == key(name)), None)
            if g is None:
                raise ValueError(f'no guild named {name!r}')
            del self.db.guilds[g.id]
            self._commit(f'disband {g.id} {g.name!r} ({len(g.members)} member(s))')
            out = g.copy()
        self.request_save()
        self._drop_boards(out.id, 'admin disband')      # guild-g6: its live board, 0xB8 to 9702
        return out

    def _drop_boards(self, gid, why, always=False):
        """guild-g6: the Guild Plaza board(s) of a deleted guild (boards.Boards.remove_guild:
        store + S2C 0xB8 {gid} to 9702). Never under self.lock (Boards.lock reads guilds)."""
        boards = getattr(self.server, 'boards', None)
        if boards is not None:
            return boards.remove_guild(gid, why, always=always)
        if always:                                      # no board module (a bare test server)
            for s in self.server.world.map_sessions(GUILD_PLAZA_MAP):
                if self.server._push(s, *board_remove(gid), tag='GUILD', flush=False):
                    self._flush(s)
        return []

    def set_points(self, name, points):
        with self.lock:
            g = next((x for x in self.db.guilds.values() if key(x.name) == key(name)), None)
            if g is None:
                raise ValueError(f'no guild named {name!r}')
            g.points = _clamp(points, 0, 0xFFFFFFFF)
            self._commit(f'{g.name!r} points = {g.points} (Lv.{g.level})')
            out = g.copy()
        self.request_save()
        return out

    def set_max(self, name, count):
        """Admin / test aid (`!guild max`): the guild's member cap, 1..50 (the clients see it at
        their next sub 3; the capacity flow still offers the next client tier above it)."""
        with self.lock:
            g = next((x for x in self.db.guilds.values() if key(x.name) == key(name)), None)
            if g is None:
                raise ValueError(f'no guild named {name!r}')
            g.max_members = _clamp(count, MIN_MAX_MEMBERS, MAX_MEMBERS_CAP)
            self._commit(f'{g.name!r}: max members = {g.max_members} (admin)')
            out = g.copy()
        self.request_save()
        return out

    def set_fake_online(self, name, count):
        with self.lock:
            g = next((x for x in self.db.guilds.values() if key(x.name) == key(name)), None)
            if g is None:
                raise ValueError(f'no guild named {name!r}')
            g.fake_online = _clamp(count, 0, SUB3_MAX_ROWS - len(g.members))
            log.info(f'[GUILD] {g.name!r}: {g.fake_online} fake online member(s) in its sub 3 (memory only)')
            return g.copy()

    def add_application(self, name, who, now=None):
        """Admin: queue `who`'s application to guild `name` (FIFO; the real C2S 0x89 is guild-g3)."""
        hit = self.server.store.character_by_name(who)
        if hit is None:
            raise ValueError(f'no character named {who!r}')
        char = hit[2]
        cid = char.get('cid')
        if not _valid_cid(cid):
            raise ValueError(f'{char.get("name")!r} has no character id (cid)')
        now = time.time() if now is None else now
        with self._flow(), self.lock:       # never between a sub 4 sync's purge and its send
            g = next((x for x in self.db.guilds.values() if key(x.name) == key(name)), None)
            if g is None:
                raise ValueError(f'no guild named {name!r}')
            if cid in self._tags:
                raise ValueError(f'{char.get("name")!r} is in {self._tags[cid].name!r} already')
            if any(a['cid'] == cid for a in g.applications):
                raise ValueError(f'{char.get("name")!r} has applied to {g.name!r} already')
            if len(g.applications) >= MAX_APPLICATIONS:
                raise ValueError(f'{g.name!r} has {MAX_APPLICATIONS} applications')
            g.applications.append({'cid': cid, 'name': text(char.get('name'), NAME_MAX),
                                   'level': R.level_of(char), 'job1': _clamp(char.get('class'), 0, JOB1_MAX),
                                   'job2': _clamp(char.get('job2'), 0, JOB2_MAX), 'at': now})
            self._commit(f'{g.name!r}: application of {char.get("name")!r} queued ({len(g.applications)} pending)')
            out = g.copy()
        self.request_save()
        return out

    def clear_applications(self, name):
        with self.lock:
            g = next((x for x in self.db.guilds.values() if key(x.name) == key(name)), None)
            if g is None:
                raise ValueError(f'no guild named {name!r}')
            n, g.applications = len(g.applications), []
            self._commit(f'{g.name!r}: {n} application(s) cleared')
        self.request_save()
        return n

    # ---------------------------------------------------------------- save ---
    def request_save(self):
        """Write guilds.json from the tick thread (never under a caller's lock), as the boss
        ledger does; without a scheduler (unit tests) at once."""
        if not self.db.path:
            return
        ticks = getattr(self.server, 'ticks', None)
        if ticks is None:
            self.flush()
            return
        with self._plock:
            if self._pending is not None:
                return
            self._pending = ticks.call_later(0, self._scheduled_flush, name='guild-save')

    def _scheduled_flush(self):
        with self._plock:
            self._pending = None
        self.tick_flush()

    def tick_flush(self):
        return self.flush(delays=storemod.QUICK_REPLACE_DELAYS, wait=0)

    def flush(self, delays=None, wait=None):
        """Save now if anything changed (shutdown, tests). A failed write stays dirty and is
        retried RETRY_SECS later."""
        try:
            done = self.db.flush(delays=delays, wait=wait)
        except OSError as e:
            self._failures += 1
            if self._failures == 1:
                log.exception(f'[GUILD] saving {self.db.path} failed; kept dirty, retrying in {RETRY_SECS:g} s')
            else:
                log.warning(f'[GUILD] saving {self.db.path} failed again ({self._failures} in a row): {e}')
            ticks = getattr(self.server, 'ticks', None)
            if ticks is not None:
                with self._plock:
                    if self._pending is None:
                        self._pending = ticks.call_later(RETRY_SECS, self._scheduled_flush, name='guild-retry')
            return False
        self._failures = 0
        return done

    # ------------------------------------------------------------- GM aid ---
    def describe(self, g):
        """`!guild info` lines (each <= 80 bytes)."""
        master = g.master()
        lines = [f'{g.name} id {g.id} Lv.{g.level} {g.points} pts, {len(g.members)}/{g.max_members}, '
                 f'master {master["name"] if master else "-"}']
        rows = []
        for m in g.members:
            ch = self._online_channel(m['name'])
            rows.append(f'{m["name"]}({GRADE_NAMES[m["grade"]]}{f" ch{ch}" if ch else ""})')
        lines += gm.wrap_list(rows, 'Members: ', limit=80)
        if g.applications:
            lines += gm.wrap_list([a['name'] for a in g.applications], 'Applications: ', limit=80)
        if g.fake_online:
            lines.append(f'{g.fake_online} fake online member(s) (memory only)')
        if g.notice:
            lines.append(f'Notice: {g.notice}')
        return lines

    def dev_apply(self, gm_session, name, who):
        """`!guild apply <guild> <char>` (P12+P14 live triage G4): EN Build 14's player menu has
        no join entry (module docstring), so this is how a live test applies. An ONLINE
        character goes through the real C2S 0x89 path - Guilds.apply with the guild's id, as
        if its client sent it: sub 2 to the applicant, the sub 2 0x10 push to a synced master
        or 0x11 with the master offline, the refusals (0 / 6) - so those replies are exercised
        live; an offline one is only queued (add_application: the master's next sub 4).
        Returns the GM's 0x15 lines. Raises ValueError for an unknown guild / character."""
        g = self.get(name)
        if g is None:
            raise ValueError(f'no guild named {name!r}')
        hit = self.server.store.character_by_name(who)
        if hit is None:
            raise ValueError(f'no character named {who!r}')
        cname = hit[2].get('name')
        s = self._session_of(cname)
        if s is None or not s.get('in_world'):
            g = self.add_application(g.name, cname)
            log.info(f'[GUILD] !guild apply by {self._whois(gm_session)!r}: {cname!r} is offline - queued '
                     f'for {g.name!r} (no sub 2; the master gets sub 4 at his next map load)')
            return [f'{g.name}: {len(g.applications)} pending; {cname} is offline - the master gets '
                    f'sub 4 at his next map load.']
        log.info(f'[GUILD] !guild apply by {self._whois(gm_session)!r}: {cname!r} is online - the C2S 0x89 '
                 f'path (Guilds.apply, guild id {g.id})')
        result, why = self.apply(s.get('sock'), s, {'guild_id': g.id})
        now = self.get(g.id) or g
        return [f'{now.name}: {len(now.applications)} pending; {cname}: 0x89 path, sub 2 result {result}.',
                P.cut_text(f'({why})', 78).decode('latin-1')]

    def dev_guild(self, session, args):
        """`!guild ...` (module docstring; registered below). Every test aid of guild-g1."""
        reply = self.server._gm_reply
        if not self.supported:
            reply(session, 'Guilds need the 2009 client (CLIENT_BUILD 2009).', 'warn')
            return
        words = str(args or '').split()
        sub = words[0].lower() if words else ''
        try:
            if sub in ('', 'me'):
                g = self.of_char(self.server._session_char(session))
                reply(session, f'{len(self.db.guilds)} guild(s); you are in '
                               f'{g.name if g else "no guild"} (client tag {cview.guild_id(session)}).')
                if g is not None:
                    for line in self.describe(g):
                        reply(session, line)
            elif sub == 'list':
                rows = self.all()
                reply(session, f'{len(rows)} guild(s):')
                for g in rows:
                    m = g.master()
                    reply(session, f'{g.id} {g.name} Lv.{g.level} {len(g.members)}/{g.max_members} '
                                   f'master {m["name"] if m else "-"} {g.points} pts')
            elif sub == 'info':
                g = self.get(words[1]) if len(words) > 1 else self.of_char(self.server._session_char(session))
                if g is None:
                    raise gm.DevCommandError('no such guild')
                for line in self.describe(g):
                    reply(session, line)
            elif sub == 'seed':
                if len(words) < 3:
                    raise gm.DevCommandError('needs a guild name and its master')
                g = self.seed(words[1], words[2], words[3:])
                # one 0x15 line holds ~80 bytes: the member names wrap (P14 review)
                for line in gm.wrap_list([m['name'] for m in g.members], f'Guild {g.name} (id {g.id}) seeded: ',
                                         limit=80):
                    reply(session, line)
                reply(session, 'Each member sees it after a portal or relog.')
            elif sub == 'disband':
                if len(words) < 2:
                    raise gm.DevCommandError('needs a guild name')
                g = self.disband(words[1])
                reply(session, f'Guild {g.name} (id {g.id}) deleted; its members drop the tag at their next map load.')
            elif sub == 'points':
                if len(words) < 3:
                    raise gm.DevCommandError('needs a guild name and the points')
                g = self.set_points(words[1], gm.parse_int(words[2], 'points', 0, 0xFFFFFFFF))
                reply(session, f'{g.name}: {g.points} points = Lv.{g.level} (shown after a map load).')
            elif sub == 'max':
                if len(words) < 3:
                    raise gm.DevCommandError('needs a guild name and the member cap')
                g = self.set_max(words[1], gm.parse_int(words[2], 'max', MIN_MAX_MEMBERS, MAX_MEMBERS_CAP))
                reply(session, f'{g.name}: {len(g.members)}/{g.max_members} (windows show it after a map load; '
                               f'a full guild refuses applications).')
            elif sub == 'fake':
                if len(words) < 3:
                    raise gm.DevCommandError('needs a guild name and a count')
                g = self.set_fake_online(words[1], gm.parse_int(words[2], 'count', 0, SUB3_MAX_ROWS))
                reply(session, f'{g.name}: {g.fake_online} fake online member(s) from the next map load.')
            elif sub == 'apply':
                if len(words) < 3:
                    raise gm.DevCommandError('needs a guild name and a character')
                for line in self.dev_apply(session, words[1], words[2]):
                    reply(session, line)
            elif sub == 'apps':
                if len(words) < 3 or words[2].lower() != 'clear':
                    raise gm.DevCommandError('apps <guild> clear')
                n = self.clear_applications(words[1])
                reply(session, f'{n} application(s) cleared.')
            elif sub in ('board', 'boards'):
                # P15 guild-g6: the Guild Plaza boards (boards.Boards.dev)
                self.server.boards.dev(session, words[1:])
            elif sub == 'gmtag':
                if len(words) > 1 and words[1].lower() not in ('on', 'off'):
                    raise gm.DevCommandError('gmtag on|off')
                if len(words) > 1:
                    session['gm_tag_off'] = words[1].lower() == 'off'
                reply(session, f'Your GM nameplate is {"OFF" if session.get("gm_tag_off") else "on"} '
                               f'(records + sub 37; your next map load shows it).')
            else:
                raise gm.DevCommandError(f'unknown sub-command {sub!r}')
        except ValueError as e:
            if isinstance(e, gm.DevCommandError):
                raise
            raise gm.DevCommandError(str(e)) from None


def install(rs, guilds):
    """The P14 hook of the arch09-resync-bundle (resync.py): the 0x8A reply replaces the pre-P14
    'guild' step, the applications go right after it, 'gm_tag' (sub 37) stays last."""
    import resync as resyncmod
    only_2009 = {BUILD_2009}
    def info(server, sock, session, ctx):
        # the map-load epoch of the sub 3 that went out (None: no sub 3 with the list)
        ctx['guild_rows_epoch'] = guilds.reply_info(sock, session)

    def apps(server, sock, session, ctx):
        if ctx.get('guild_rows_epoch') is not None:
            guilds.reply_applications(sock, session, epoch=ctx['guild_rows_epoch'])

    rs.register(resyncmod.STAGE_GUILD, 'guild', info, builds=only_2009, replace=True,
                note='0xB3 sub 3 (member) / sub 15 {0} (guild-g1)')
    rs.register(resyncmod.STAGE_GUILD, 'guild_apps', apps, builds=only_2009, before='gm_tag',
                note='0xB3 sub 4 to a master with applications, after this 0x8A sub 3 (guild-g1)')
    if guilds.supported:
        register(guilds.server.world.hooks, guilds)
    return rs


def register(hooks, guilds):
    """The P14 world hooks (2009 only; world.py): the first entry of a connection sends the
    login line (sub 20) unless it continues a channel hop, a departure parks the session's
    uncredited guild points by cid, the end of the LOGIN (ON_LOGOUT, continuity.py) credits
    them with the logout line (sub 21), a rename sends sub 22 (ADDENDUM C4). Registered after
    the messenger's and continuity's, so on a plain disconnect the logout line follows the
    friends' 0x60."""
    def on_enter_world(server, session, first=False, hop=None, **_):
        if first:
            guilds.logged_in(session, hop=hop)

    def on_departure(server, session, **_):
        guilds.departed(session)

    def on_logout(server, session, char_name=None, **_):
        guilds.logged_out(session, char_name)

    def on_rename(server, session, old=None, new=None, cid=None, **_):
        guilds.renamed(session, old, new, cid)

    hooks.register(worldmod.ON_ENTER_WORLD, on_enter_world)
    hooks.register(worldmod.ON_LEAVE_WORLD, on_departure)
    hooks.register(worldmod.ON_DISCONNECT, on_departure)
    hooks.register(worldmod.ON_LOGOUT, on_logout)
    hooks.register(worldmod.ON_RENAME, on_rename)
    return on_enter_world, on_departure, on_logout, on_rename


# The '!' command (gm.register: no edit of GameServer.DEV_COMMANDS); its handler is the thin
# GameServer._dev_guild, which hands over to server.guilds.
if 'guild' not in gm.COMMANDS:
    gm.register('guild', gm.DevCommand(
        '_dev_guild', '!guild [list | info [name] | seed <name> <master> [member ...] | disband <name> | '
                      'points <name> <n> | max <name> <n> | fake <name> <n> | apply <name> <char> | '
                      'apps <name> clear | gmtag on|off | board [list | place|premium <name> <text> | '
                      'ttl <name> <secs> | expire <name>|all]]',
        'the 2009 guilds (guilds.json): your guild, every guild, one guild; seed = create with a master '
        '(grade 5) and Trainees; points sets the guild points (level); max sets the member cap 1..50 (a full '
        'guild for the apply refusal); fake = n dummy ONLINE members in '
        'the sub 3 (Guild Battle gate, memory only); apply = the C2S 0x89 path for an ONLINE character '
        '(sub 2 to it, the 0x10 push to a synced master; EN Build 14 has no join entry in the player '
        'menu), else queued (sub 4 at the master\'s next map load); '
        'gmtag off = your records show your guild / no "Game Master" tag (GM commands still work); '
        'board (P15 guild-g6, guild_boards.json) = the Guild Plaza boards: list; place / premium = a '
        'board of that guild at your spot on 9702 (else Moiba\'s), 0xBA to the plaza, replacing its '
        'board; ttl = it expires in n s; expire = removed now (0xB8)',
        owner='guild-g1 (P14 stage 3)', aliases=('guilds',)))
