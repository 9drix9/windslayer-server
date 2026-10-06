#!/usr/bin/env python3
"""
cashuse.py - using cash items (P8 stage 3), both client builds
=============================================================
premium_cash-use-generic, -expiry, -rename (+ lc-rename), -stat-reset, -megaphone,
-region-warp and -friend-warp (docs/systems/premium_cash.md F8-F11, F13-F15; roadmap P8
order 3). cash.py holds the model (records, serials, find / consume), mall.py the mall; this
module runs what a double-click on a record of the Spark Items / cash bag tab asks for.

    use = CashUse(server)                       # GameServer.cashuse
    use.use(sock, session, rec)                 # C2S 0x48 -> 0x72 (owner + holders), or 0x72 {uid, 0, 0}
    use.rename(sock, session, rec)              # C2S 0x49 -> 0x73 {1, serial} + 0x74 (self + holders) | 0x73 {0}
    use.stat_reset(sock, session, rec)          # C2S 0x4A -> 0x76 18 B owner / 12 B holders
    use.megaphone(sock, session, rec)           # C2S 0x4C -> orange 0x90 to everyone, 0x72 to the owner
    use.region_warp(sock, session, rec)         # C2S 0x70 -> 0x9A {1, serial} + the map load | 0x9A {0}
    use.friend_warp(sock, session, rec)         # C2S 0x71 -> 0x9B {1, serial} + the map load | 0x9B {0}
    use.tick_expiry()                           # every EXPIRY_SCAN_SECS: 0x93 per expired period record
    use.boost_exp(session, amount)              # the EXP items' multiplier on kill exp (award_exp stage 2)

Every opcode here is wire-identical in the two builds (spec_2009 0x469072/0x48, /0x49,
0x469382/0x4A, 0x468F05/0x4C, 0x469C54/0x70, 0x468F05/0x71; S2C 0x72 / 0x73 / 0x74 / 0x76 /
0x90 / 0x93 / 0x9A / 0x9B: "identical" or "identical wire format"); only the client's struct
offsets moved (2009 entity look +0x12E, gender +0x11B, uid +0x88). Nothing is built by hand:
packets.py resolves the receiver-dependent forms (0x72 / 0x76 owner vs observer) per receiver.

What the client does on each reply (so the server mirrors it exactly)
--------------------------------------------------------------------
Every success names the record's serial and the client runs its own consume-by-serial on it
(2008 FUN_00464380, 2009 FUN_0046df70): a counted record (kind 1) loses `count` and is freed
at 0, a period record (kind 2) gets the 0x72 expiry copied in, anything else is untouched.
cash.CashInventory.consume is the same rule, so both lists stay equal without a 0x6F.
  0x72 CashItemUsed {uid, item [, hair_code for 0xD39/0xD3A/0xD50] [, serial [, expiry]]}:
      every client recomposes that entity's look (below), plays effect / sound 0x140; the
      owner also closes the waiting box 0x16 and consumes (1, or activates a period record).
      The refusal {uid, 0, 0} (10 B) closes the box and changes nothing (live T-72c).
  0x73 {1, serial}: "Your character name has successively changed." + consume; 0x73 {0}: "The
      name is already being used." (any non-1). 0x73 does NOT rename (live T-73b): 0x74 does.
  0x74 {uid, new_name}: the entity's name in place; for the local uid also scene+0x206 and the
      status panel (live T-74). Never the short 4-byte form (C45).
  0x76 {uid, str, dex, int, spi [, serial, count]}: absolute base stats, max HP/MP recomputed
      (FUN_00427d40 / f40), the party frame's MP text; the owner consumes `count`.
  0x93 {serial}: "[name] is expired." and the record leaves the owned list - ONLY when the
      client holds that serial (2009 case 0x93 walks +0x04; nothing is printed otherwise).
  0x9A / 0x9B {1, serial}: consume; the map does not change by itself (live T-9A): the
      server's map load follows. {0}: "You are unable to transfer to the area." / "The
      character doesn't exist...".

Look changes (the hair dye / hairstyle / eyes: premium_cash-use-generic)
-----------------------------------------------------------------------
The client never takes a look from the server here: it recomposes its own copy of the
entity's look (2008 entity+0x120, 2009 +0x12E) - 2008 FUN_00427430 = 2009 FUN_004289a0, a
switch on the item def's Cash_Cls (+0x1F4), each case ending in the ONE composition routine
(2008 FUN_00426d50 = 2009 FUN_004282c0 = inventory.compose, the cash-over-regular rule):
  Cash_Cls 0 (the eyes 3117..3124, Kind 1): compose(Kind, Cash, Spr_Num);
  Cash_Cls 1 (hairstyles, Kind 4): Spr_Num with its tens digit replaced by the OLD hair's
      colour digit (FUN_00427080 with EAX = Spr_Num, restyle());
  Cash_Cls 2 (hair colours 3409..3415): the old hair with its tens digit replaced by
      (u8)(item - 0x50) = 1..7 (0x427587, dye());
  Cash_Cls 14: only a period flag (returns 2), no look; anything else and Cash 0: nothing.
The three RANDOM items 0xD39 / 0xD3A (hairstyle F / M) and 0xD50 (colour) skip that switch:
the 0x72 carries the hair_code the SERVER chose, applied as compose(4, cash 1, code, gender,
job 1) - which is exactly the item's own Kind 4 / Cash 1 / Job[0] 1. The server picks it with
the client's own rule (FUN_004270f0: random_style / random_dye), so every client and the
store land on the same words. The hair code is decimal: hundreds = the set (0 / 1 creation
F / M, 4 / 5 the new F / M styles), tens = colour (0 = the style's default colour from the
table at 2008 0x6F103C = 2009 0x525B64: HAIR_DEFAULT_COLOR), ones = style.
The server applies the same composition to the stored look (inventory.Inventory.recompose
with the gender bool the records send) BEFORE the 0x72 goes out and touches the presence
revision, so a peer whose spawn record is still in flight rebuilds it with the new look and
every later 0x07 / 0x04 / 0x02 carries it (persisted: `look` + 2009 `look_ext`). The holders
get the observer form of the same 0x72 and recompose their copy themselves.

Generic use (C2S 0x48, the one dispatcher; premium_cash-use-generic)
-------------------------------------------------------------------
Checks in order, the first failure answers 0x72 {uid, 0, 0}: in the world and not in the
mall; a cash item of this client (hii); not an item whose window sends its own request
(OWN_REQUEST: 1895 -> 0x49, stat resets -> 0x4A, megaphones -> 0x4C, stones -> 0x70 / 0x71,
Notes -> 0x4B, element separators -> 0x72, slot extensions: a 0x48 for one is a modified
client); the hii Gender of a look item matches the character's gender bool; a usable record
(the client's own lookup FUN_0045E760: quantity > 0, not activated, not worn); a period item
not already running; the gate another group registered for the item (CashUse.gates: the pet
stub's "a pet is worn" for EN pet food, and its refusal of the pet name ticket - pets.py,
ROADMAP_2009_ADDENDUM C6). Then by what the item does: a look change and / or a period
activation (below), or an effect another group registered (CashUse.effects, C6: the pet
stub's feed). An item with none of them is refused and KEPT: the design's "anything else:
quantity - 1, 0x72" would use it up for nothing (the 2009 mall also sells a guild billboard,
not modelled yet). A success consumes (counted) or activates (period), sends the owner's
0x72 and the holders' observer form, then runs the effect.

Period items (Cash_T 2: +50% / +100% EXP 3321..3326, Waive EXP Penalty 1891)
---------------------------------------------------------------------------
Use = activation: the record gets expire = now + Cash_V days (the client copies the 0x72's
SYSTEMTIME in; window 5 prints it as "<m>month <d>day HH:MM", C36) and stays quantity 1; it
is no longer usable or bagged. The client refuses a second activation of an effect it lists
already; the server mirrors that (the same item active -> refusal). Every later 0x6F carries
it activated, and the 0x6F tail prints "[x] will expire in N day(s)." (2009 FUN_00465110) -
the item "shows its expiry". The effects are the server's: boost_exp() multiplies kill exp
(killer and party shares) by the best active EXP item - stage 2 of the one exp path
GameServer.award_exp (card bonus -> EXP item -> event multiplier -> 0x21), for
EXP_BOOST_SOURCES only; 1891 skips the server's death exp penalty (the client applies no
bonus of its own to a 0x21).
Expiry (premium_cash-expiry, F15): tick_expiry() every EXPIRY_SCAN_SECS finds activated
records whose date passed, removes them (the boost ends with them), persists and sends S2C
0x93 {serial} to an in-world owner, whose client holds the record (every map load's 0x6F put
it there): "[x] is expired." A record that expired while its owner was offline, in the mall
or mid map load is removed by the next map load's owned-list sync instead
(take_expired from GameServer._send_owned_cash), which leaves it out of the 0x6F and prints
the client's own words as an S2C 0x15 line: the design's "send it in the 0x6F, then 0x93"
would make the 0x6F tail print "The remain time for [x] is -H hrs. -M min. ..." first
(FUN_00465110 prints the remaining time of every activated record it is sent, negative
for a past date).

Rename (premium_cash-rename + lc-rename, D8; ROADMAP_2009_ADDENDUM C4 / C5)
------------------------------------------------------------------------
C2S 0x49 carries only the new name; the item is 1895 Change Nickname, owned and usable.
Rules = lc-create's (names.check: 1..16 bytes, no space, no reserved word, [A-Za-z0-9]) and
unique across every account (a case change of one's own name is allowed). The store renames
the record (its stable `cid` stays), rewrites every stored reference to it through its
rename_rewriters (other characters' friends / mentor / mentees first: social.rename_references;
P12 / P14 add theirs), rebuilds its name index and saves at once - before the reply, because
enter world matches the name. The session, the world index and the messenger identity follow.
Then 0x73 {1, serial} and 0x74 to self and to the holders, and the world hook ON_RENAME (the
C4 hook other groups subscribe to: fn(server, session, old=, new=, cid=)) pushes the rest: the
messenger re-sends the 0x0B friend list to every online friend who lists the character (the
0x60 presence update matches by name, so an old row could never be renamed in place) and a
0x7B to an online mentor (0x7B refills the mentee row found by uid in place), the party
rebuilds its members' frames (0x51 self + 0x4F per member: 0x4F is the only packet that
writes a frame's name). Refused mid trade or with a stall open (both keyed by the live
session name on the other side).

S2C 0x73 NameChangeResult has two users (ROADMAP_2009_ADDENDUM C5 / X17): this rename and the
planned refusal of the 2009 C2S 0x4D PetRename (pet F9: {0} closes its "Waiting for the server
to respond." box). name_change_fields() is the one builder of both forms.

Stat reset (premium_cash-stat-reset; C13 / C15)
----------------------------------------------
C2S 0x4A {item, str_removed, dex_removed, int_removed, tol_removed}. The client's allowance is
the RECORD's quantity (C13: a "Reset 5" record of quantity 5 lets 5 points go), so the server
checks 0 < sum <= quantity, each stat - removed >= STAT_MIN (0: the window's '-' stops at 0,
and creation allows 0), applies it, consumes `sum` (the 0x76 consume_count, which the client
takes off the same record) and recomputes max HP / MP as the client does. Free points are
derived by the client as total(level) - sum(stats) (spec 0x14), so a removal can only raise
them (C15 holds). Failure: the owner form with the unchanged stats, serial 0, count 0 - it
closes the waiting box and consumes nothing (registry.MUST_REPLY 0x4A is the same bytes).

Megaphone (premium_cash-megaphone, F11; design Q7)
-------------------------------------------------
C2S 0x4C {item, str[61] text}, no waiting box. 3377..3379 Super Megaphone -> every in-world
player, 3380 / 3381 Megaphone -> every in-world player on the sender's channel (one channel
per server today, so the same set). The line is S2C 0x90 (orange, 87 bytes max): "[Megaphone]
<name> : <text>". The client already ran its Curse_Engine filter (C46: a blocked word is
never sent); the server cuts the text at the NUL, drops control bytes and limits a character
to one megaphone per MEGAPHONE_RATE_SECS (a refused line gets one 0x15 to its sender). The
count goes down through the client's own consume: 0x72 {uid, item, serial} to the owner only
(Cash_Cls 15 has no look case, so it only plays effect 0x140) - config
MEGAPHONE_CONSUME_PACKET '0x6F' re-sends the whole owned list instead, should the sparkle
look wrong live (the original packet is unknown, design Q7).

Warp stones (premium_cash-region-warp / -friend-warp, F13 / F14)
---------------------------------------------------------------
0x70 {item 3430/3432/3434, i32 dest = region * 100 + suffix (live T-70c: 704 Balderan
Entrance)}: the destination must be a map the running build's client has a stage .hmi for
(not merely listed in data.map_codes) and not a PvP room map; the landing point is where a
portal into it lands (GameServer._warp_point). 0x71 {item 3429/3431/3433, str[17] name}: the target is an online
character in the world (not oneself, not in the mall, not in a room map, visible to the user)
- the user moves to the target (the design's reading of "Friend Teleport"), landing beside it
like the GM /go (GameServer._go_point). Both: consume, 0x9A / 0x9B {1, serial}, then the one
map load (GameServer._map_transfer: 0x08 0x03 0x07 0x28 0x44 0x6F ...). The user must be in
the world, alive and not in the mall; a trade or stall is closed by the map load's own hooks.

Locks: CashUse.lock (the megaphone rate book) -> store.lock -> send_lock. Every record change
is made under store.lock and persisted with mark_dirty (the rename: an immediate save once
store.lock is let go, GameServer._save_store_now); packets go out after it is released. Nothing here takes a combat or world lock except through
GameServer._map_transfer / _refresh_vitals, which are called with no lock held.
"""
import datetime
import logging
import random
import threading
import time

import cash as CASH
import chat as chatmod
import en_content as EC
import hpmp
import inventory as invmod
import mall as mallmod
import names
import packets as P
import presence
import social
import world as worldmod

log = logging.getLogger('WS')

# ---- item ids (premium_cash.md 3.3; hii Cash_Cls / Cash_T, both builds) ----
NAME_CHANGE = CASH.NAME_CHANGE               # 1895 Change Nickname (C2S 0x49)
STAT_RESET = CASH.STAT_RESET                 # 3214 / 3215 / 3234 / 3235 (C2S 0x4A)
MEGA_SUPER, MEGA = CASH.MEGA_SUPER, CASH.MEGA  # 3377..3379 / 3380, 3381 (C2S 0x4C)
REGION_STONE = CASH.REGION_STONE             # 3430 / 3432 / 3434 (C2S 0x70)
FRIEND_STONE = CASH.FRIEND_STONE             # 3429 / 3431 / 3433 (C2S 0x71)
RANDOM_LOOK = CASH.RANDOM_LOOK               # 0xD39 / 0xD3A hairstyle F / M, 0xD50 colour
RANDOM_DYE = 0xD50
ELEMENT_SEPARATORS = frozenset({3436, 3437, 3951, 3952})    # C2S 0x72 (item_inventory stage 4)
# Items whose use window sends another request than C2S 0x48: a 0x48 naming one comes from a
# modified client and is refused (the real one would never consume them this way).
OWN_REQUEST = (frozenset({NAME_CHANGE}) | STAT_RESET | MEGA_SUPER | MEGA | REGION_STONE | FRIEND_STONE
               | CASH.NOTES | ELEMENT_SEPARATORS | frozenset(CASH.SLOT_EXT))
# Period EXP items: the percent their names promise ("+50% EXP/7 Days", "100% EXP/30 Days").
EXP_BOOST_PCT = {3321: 50, 3322: 50, 3323: 50, 3324: 100, 3325: 100, 3326: 100}
# The GameServer.award_exp sources the EXP item raises (its stage 2, per receiver): kill exp -
# the killer's part and each party member's share. A quest's exp and a mentor's 0x7F share (a
# percentage of what the mentee received, already raised by the mentee's own item) are not.
EXP_BOOST_SOURCES = frozenset({'kill', 'party'})
WAIVE_EXP_PENALTY = 1891                     # "Waive EXP Penalty" (Cash_T 2, 30 days)
# The 0x72 refusal {uid, 0, 0}: item 0 has no item def, so the grammar stops after the serial
# (10 B) - the same assume as registry._cash_item_use_refusal (MUST_REPLY 0x48).
REFUSAL_72_ASSUME = {'item_def(item_id) == null': True}

# ---- look (FUN_00427430 / FUN_004270f0 / FUN_00427080 / FUN_00427020; module docstring) ----
CLS_LOOK, CLS_HAIRSTYLE, CLS_HAIR_COLOUR = 0, 1, 2     # hii Cash_Cls (def+0x1F4) cases with a look
# The default colour digit of a creation hair code (tens digit 0), indexed 5 * gender + style
# (style 1..4): the 10 bytes at 2008 0x6F103C and 2009 0x525B64 (identical in both exes).
HAIR_DEFAULT_COLOR = (0, 5, 2, 1, 5, 0, 1, 4, 3, 6)
RANDOM_COLOURS = 7                           # rand() % 7 + 1
RANDOM_STYLES = {0: 7, 1: 6}                 # gender 0 (F): 7 styles, 1 (M): 6
RANDOM_STYLE_BASE = {0: 400, 1: 500}         # the new F / M hair sets (4xx / 5xx)

# ---- stat reset / megaphone / expiry ----
STAT_KEYS = ('str', 'dex', 'int', 'spr')
STAT_MIN = 0                                 # the 0x3FA window's '-' buttons stop at 0 (live #21)
MEGAPHONE_PREFIX = '[Megaphone] '
MEGAPHONE_TEXT_MAX = 60                      # C2S 0x4C str[61]
MEGAPHONE_RATE_SECS = 10.0                   # one megaphone per character per 10 s (F11)
EXPIRY_SCAN_SECS = 60.0                      # F15 / roadmap F8 ticker table: "60 s"
EXPIRED_TEXT = '[{}] is expired.'            # the client's own 0x93 words (2009 case 0x93)

# S2C results
OK, FAILED = 1, 0


def name_change_fields(ok, serial=None):
    """S2C 0x73 NameChangeResult (spec 0x73, SubHandler4 0x46BC08; ROADMAP_2009_ADDENDUM C5):
    the one builder of its two forms. Both first close message box 0x16 - the waiting box of
    C2S 0x49 (character rename) and of the 2009 C2S 0x4D (pet rename, pet F9). ok: {1, u32
    serial} (5 B) - "Your character name has successively changed." and the client consumes
    one unit of the record with that serial (the Change Nickname ticket); else {0} (1 B) -
    "The name is already being used. Please try another name." and nothing else is read."""
    if ok:
        return {'result': OK, 'item_serial': int(serial or 0) & 0xFFFFFFFF}
    return {'result': FAILED}


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ================================================================ pure rules ===
def hair_color(hair, gender):
    """2008 FUN_00427020 / 2009 FUN_00428590: the colour digit of a hair code - its tens digit,
    or for a creation code (tens 0, style 1..4) the style's default colour."""
    hair = _int(hair) & 0xFFFFFFFF
    tens = (hair % 100) // 10
    if tens:
        return tens
    style = hair % 10
    if style == 0 or style > 4:
        return 0
    return HAIR_DEFAULT_COLOR[5 * (1 if gender else 0) + style]


def set_color(hair, digit):
    """FUN_00427080 with EAX 0: `hair` with its tens digit replaced by `digit` (u32)."""
    hair = _int(hair) & 0xFFFFFFFF
    return (hair + ((hair // 100) * 10 - hair // 10 + _int(digit)) * 10) & 0xFFFFFFFF


def restyle(spr, old_hair, gender):
    """Cash_Cls 1 (a fixed hairstyle): FUN_00427080 with EAX = the item's Spr_Num - the
    style's sprite code with the OLD hair's colour digit."""
    return set_color(spr, hair_color(old_hair, gender))


def dye(old_hair, item_id):
    """Cash_Cls 2 (a fixed colour, 0x427587): the old hair with the colour digit
    (u8)(item_id - 0x50): 3409 Yellow -> 1 .. 3415 White -> 7."""
    return set_color(old_hair, (_int(item_id) - 0x50) & 0xFF)


def random_dye(old_hair, gender, rng):
    """0xD50 Random Hair Coloring: FUN_004270f0(gender, 1) - a colour 1..7 other than the
    current one, the style kept."""
    current = hair_color(old_hair, gender)
    while True:
        colour = rng.randrange(RANDOM_COLOURS) + 1
        if colour != current:
            return set_color(old_hair, colour)


def random_style(old_hair, gender, rng):
    """0xD39 / 0xD3A Random Hairstyle: FUN_004270f0(gender, 0) - a new-set style (1..7 F /
    1..6 M; not the current one when the old hair is already a new-set code, hundreds > 1),
    4xx / 5xx by gender, with the old hair's colour digit."""
    gender = 1 if gender else 0
    old_hair = _int(old_hair) & 0xFFFFFFFF
    current = old_hair % 10 if old_hair // 100 > 1 else 0
    while True:
        style = rng.randrange(RANDOM_STYLES[gender]) + 1
        if style != current:
            break
    return set_color(RANDOM_STYLE_BASE[gender] + style, hair_color(old_hair, gender))


def look_change(d, spr_num, old_hair, gender, rng):
    """(spr for the composition, hair_code for the wire or None) of using cash item `d`
    (cash.CashDef) whose hii Spr_Num is `spr_num`, or None when the item changes no look -
    FUN_00427430's switch plus the 0x72 random branch (module docstring "Look changes")."""
    if d is None or not d.cash:
        return None                                  # 0x427479: def+0x1F0 == 0 -> nothing
    if d.id in RANDOM_LOOK:
        code = (random_dye(old_hair, gender, rng) if d.id == RANDOM_DYE
                else random_style(old_hair, gender, rng)) & 0xFFFF
        return code, code
    if d.cls == CLS_LOOK:
        return _int(spr_num) & 0xFFFF, None
    if d.cls == CLS_HAIRSTYLE:
        return restyle(spr_num, old_hair, gender) & 0xFFFF, None
    if d.cls == CLS_HAIR_COLOUR:
        return dye(old_hair, d.id) & 0xFFFF, None
    return None


def boost_pct(char, now=None):
    """The best active EXP item's percent on this character (0 = none): activated period
    records of EXP_BOOST_PCT ids whose date has not passed. They do not stack."""
    best = 0
    for rec in list((char or {}).get('cash_items') or []):
        item = _int(rec.get('item_id'))
        if item in EXP_BOOST_PCT and CASH.is_activated(rec) and not CASH.is_expired(rec, now):
            best = max(best, EXP_BOOST_PCT[item])
    return best


def boosted(amount, pct):
    """Kill exp with a +pct % item: floor(amount * (100 + pct) / 100), never below amount."""
    amount = _int(amount)
    if amount <= 0 or pct <= 0:
        return amount
    return max(amount, amount * (100 + pct) // 100)


def waives_penalty(char, now=None):
    """An active 1891 Waive EXP Penalty on this character."""
    return any(_int(r.get('item_id')) == WAIVE_EXP_PENALTY and CASH.is_activated(r) and not CASH.is_expired(r, now)
               for r in list((char or {}).get('cash_items') or []))


def megaphone_text(sender, text):
    """The S2C 0x90 line of a megaphone: "[Megaphone] <sender> : <text>" as cp949 bytes, the
    text cut at its NUL and stripped of control bytes, the whole clamped to the client's 87
    (packets.cut_text keeps a cp949 pair whole). b'' when nothing is left to say."""
    body = P.cut_text(text or b'', MEGAPHONE_TEXT_MAX)
    body = bytes(b for b in body if b >= 0x20).strip()
    if not body:
        return b''
    line = P.to_bytes(MEGAPHONE_PREFIX) + P.to_bytes(sender or '') + b' : ' + body
    return P.cut_text(line, chatmod.FRIEND_LINE_MAX)


def rename_refusal(new_name, own_name, owner_of_new, own_username):
    """Why `new_name` cannot replace `own_name`, or None (names.check + uniqueness; a case
    change of one's own name is allowed). owner_of_new = store.name_owner(new_name)."""
    bad = names.check(new_name)
    if bad is not None:
        return f'invalid name: {bad}'
    if new_name == own_name:
        return 'the same name'
    if owner_of_new is not None and not (owner_of_new == own_username
                                         and new_name.lower() == str(own_name).lower()):
        return f'name already used on account {owner_of_new!r}'
    return None


def stat_reset_refusal(item_id, removed, stats, rec):
    """Why a C2S 0x4A cannot be applied, or None. removed / stats: 4 ints in STAT_KEYS order;
    rec: the owned record the client's lookup picks (None = not owned)."""
    if item_id not in STAT_RESET:
        return f'item {item_id} is no stat reset item'
    if rec is None:
        return f'no usable {item_id} record'
    total = sum(removed)
    if any(r < 0 for r in removed) or total <= 0:
        return f'nothing removed ({removed})'
    if total > _int(rec.get('qty')):
        return f'{total} points removed, the record allows {rec.get("qty")} (C13: its quantity)'
    for key, have, minus in zip(STAT_KEYS, stats, removed):
        if have - minus < STAT_MIN:
            return f'{key} {have} - {minus} < {STAT_MIN}'
    return None


# ================================================================== runtime ===
class CashUse:
    """The cash item uses of one GameServer (GameServer.cashuse). Session keys it owns:
      megaphone_t   monotonic() of the session's last megaphone (MEGAPHONE_RATE_SECS)
    A rename is told to the other groups through the world hook world.ON_RENAME (the C4 hook).
    effects: {item_id: fn(server, session, cash_def, record)} - the effect of an item another
      group owns, run after the generic C2S 0x48 consume + 0x72 (ROADMAP_2009_ADDENDUM C6: the
      pet stub, pets.py, registers the food 4286..4289 and the name ticket 4322 here). An
      item with no look, no period and no entry here is refused, never used up for nothing.
    gates: {item_id: fn(server, session, cash_def, record) -> why | None} - that group's
      precondition, checked with the record under the store lock BEFORE anything is used up;
      a reason refuses the use (0x72 {uid, 0, 0}, the item kept)."""

    def __init__(self, server, rng=None):
        self.server = server
        self.lock = threading.RLock()
        self.rng = rng if rng is not None else random.Random()
        self.effects = {}
        self.gates = {}

    # ---------------------------------------------------------------- helpers ---
    @property
    def store(self):
        return self.server.store

    @property
    def cash(self):
        return self.server.cash

    def _send(self, sock, session, key, fields, assume=None):
        return P.send(self.server, sock, session, key, fields, assume)

    def _holders(self, session, key, fields):
        """The observer form of a packet about `session`'s entity to the peers whose client
        holds it (presence.to_holders touches the revision first: a record in flight is
        rebuilt with the change instead)."""
        return presence.to_holders(self.server, session, key, fields, 'CASH')

    def _char(self, session):
        return self.server._session_char(session) if session.get('char_name') else None

    def _gender(self, session, char):
        return self.server._record_gender(session, char)

    def _in_world_refusal(self, session):
        if not session.get('char_name') or self._char(session) is None:
            return 'no character in this session'
        if session.get('in_cash_shop'):
            return 'in the mall'
        if not session.get('in_world') or session.get('current_map') is None:
            return 'not in the world (map load in flight)'
        return None

    @staticmethod
    def _now(now=None):
        return CASH.local_now(now)

    # ================================================================== F8 ===
    def use(self, sock, session, rec):
        """C2S 0x48 {u16 item_id} -> S2C 0x72 (the waiting box needs it). Returns the owner's
        0x72 fields (the refusal's too)."""
        item = _int(rec.get('item_id'))
        uid = P.session_uid(session) or 0

        def refuse(why):
            fields = {'player_uid': uid, 'item_id': 0, 'item_serial': 0}
            self._send(sock, session, '0x72', fields, REFUSAL_72_ASSUME)   # 10 B (item 0: no Cash_T)
            log.info(f'[CASHUSE] {session.get("char_name")!r} 0x48 item {item} refused (0x72 {{uid, 0, 0}}): {why}')
            return fields

        why = self._in_world_refusal(session)
        if why is not None:
            return refuse(why)
        char = self._char(session)
        d = CASH.cash_def(item)
        if d is None or not d.is_cash:
            return refuse('not a cash item of this client')
        if item in OWN_REQUEST:
            return refuse('this item is used through its own window / request, never C2S 0x48')
        gender = self._gender(session, char)
        why = mallmod.gender_refusal(d, gender) if d.cls in (CLS_LOOK, CLS_HAIRSTYLE, CLS_HAIR_COLOUR) else None
        if why is not None:
            return refuse(f'{d.name or item}: {why}')
        now = self._now()
        info = EC.items().get(item)
        effect, gate = self.effects.get(item), self.gates.get(item)
        change = left = None
        with self.store.lock:
            # Checked and applied under one hold of the store lock (the record, the look);
            # the packets go out after it.
            record = self.cash.find(char, item)
            period = record is not None and record['kind'] == CASH.KIND_PERIOD
            if record is None:
                why = f'no usable {d.name or item} record (FUN_0045E760)'
            elif period and any(r['item_id'] == item and CASH.is_activated(r) and not CASH.is_expired(r, now)
                                for r in CASH.ensure(char)):
                why = f'{d.name or item} is active already (the client refuses a second one)'
            elif gate is not None and (why := self._gate(gate, session, d, record)) is not None:
                pass
            else:
                bag = invmod.Inventory(char)
                change = look_change(d, getattr(info, 'spr_num', 0), bag.look_words(gender)[invmod.LAYER_HAIR],
                                     gender, self.rng)
                if change is None and not period and effect is None:
                    # The design's "anything else: consume + 0x72" would use the item up for
                    # nothing (2009 sells pet food / the pet name ticket / the guild billboard,
                    # none modelled): refused and kept until an owner registers its effect.
                    why = (f'{d.name or item} (Cash_Cls {d.cls}): no effect modelled - kept, not used up '
                           f'(CashUse.effects)')
                else:
                    if change is not None:
                        bag.recompose(item, change[0], gender)
                    serial = record['serial']
                    if period:
                        record['expire'] = CASH.format_expire(now + datetime.timedelta(days=max(1, d.value)))
                    else:
                        left = self.cash.consume(char, serial, 1, what='use')
                    used = dict(record)
        if why is not None:
            return refuse(why)
        self.store.mark_dirty(f'cash use {char.get("name")}')
        hair_code = change[1] if change is not None else None
        observer = {'player_uid': uid, 'item_id': item}
        if hair_code is not None:
            observer['hair_code'] = hair_code
        owner = {**observer, 'item_serial': serial}
        if period:
            owner['expire_time'] = CASH.expire_bytes(used)
        self._send(sock, session, '0x72', owner)
        seen = self._holders(session, '0x72', observer)
        look = ''
        if change is not None:
            words = invmod.Inventory(char).look_words(gender)
            look = (f'; look hair={words[invmod.LAYER_HAIR]} helm={words[invmod.LAYER_HELM]} '
                    f'back={words[invmod.LAYER_BACK_HAIR]} [1]={words[1]}'
                    + (f' (hair_code {hair_code})' if hair_code is not None else ''))
        what = (f'activated until {used["expire"]}' if period
                else f'{left} left' if left is not None else 'record kept (not counted)')
        log.info(f'[CASHUSE] {session.get("char_name")!r} used {d.name or item} ({item}, serial {serial:#x}): '
                 f'{what}{look}; 0x72 to {seen} holder(s)')
        if effect is not None:
            try:
                effect(self.server, session, d, used)
            except Exception:                            # noqa: BLE001 - the use itself stands
                log.exception(f'[CASHUSE] effect of {item} failed')
        return owner

    def _gate(self, gate, session, d, record):
        """A registered gate's refusal (None = go). A gate that raises refuses: nothing may be
        used up on an unchecked precondition."""
        try:
            return gate(self.server, session, d, record)
        except Exception:                                # noqa: BLE001 - the use is refused, the box closed
            log.exception(f'[CASHUSE] gate of {d.id} failed')
            return f'the gate of {d.id} failed'

    def name_change_result(self, sock, session, ok, serial=None, why=None, what='rename'):
        """Send S2C 0x73 (name_change_fields): the reply that closes the waiting box of a C2S
        0x49 rename or a 2009 C2S 0x4D pet rename. Returns the fields sent."""
        fields = name_change_fields(ok, serial)
        self._send(sock, session, '0x73', fields)
        if not ok:
            log.info(f'[CASHUSE] {session.get("char_name")!r} {what} refused (0x73 {{0}}): {why}')
        return fields

    # =============================================================== F15 ===
    def take_expired(self, char, now=None):
        """Remove the character's activated period records whose date has passed (their
        effect ends with them). Returns the removed records."""
        if char is None:
            return []
        # One clock for the whole pass: with now=None each is_expired() read the clock again,
        # so a record expiring between two passes left the list without being in `gone` (no
        # 0x93 / 0x15 for it) - P8 review.
        now = CASH.local_now(now)
        with self.store.lock:
            items = CASH.ensure(char)
            gone = [r for r in items if CASH.is_expired(r, now)]
            if gone:
                char['cash_items'] = [r for r in items if not any(r is g for g in gone)]
        if gone:
            self.store.mark_dirty(f'cash expiry {char.get("name")}')
        return gone

    def expired_notice(self, sock, session, records):
        """The client's own expiry words for records the client does NOT hold (left out of a
        0x6F): one S2C 0x15 line each (a 0x93 would print nothing, module docstring)."""
        for rec in records:
            text = EXPIRED_TEXT.format(EC.item_name(rec['item_id']) or rec['item_id'])
            self.server._notice(sock, session, text)
            log.info(f'[CASHUSE] {session.get("char_name")!r}: {rec["item_id"]} (serial {rec["serial"]:#x}) '
                     f'expired at {rec.get("expire")}: removed, 0x15 {text!r}')

    def expire(self, session, now=None):
        """Expire an in-world session's due records with S2C 0x93 {serial} (its client holds
        them: the map load's 0x6F put them there). Returns the serials."""
        if not session.get('in_world') or session.get('in_cash_shop') or session.get('sock') is None:
            return []
        char = self._char(session)
        gone = self.take_expired(char, now)
        for rec in gone:
            self.server._push(session, '0x93', {'item_serial': rec['serial']}, 'CASH')
            log.info(f'[CASHUSE] {session.get("char_name")!r}: {EC.item_name(rec["item_id"]) or rec["item_id"]} '
                     f'(serial {rec["serial"]:#x}) expired at {rec.get("expire")}: removed, 0x93')
        return [r['serial'] for r in gone]

    def tick_expiry(self, now=None):
        """The EXPIRY_SCAN_SECS ticker (F15). Returns the number of records expired."""
        n = 0
        for session in list(self.server._in_world_sessions()):
            try:
                n += len(self.expire(session, now))
            except Exception:                            # noqa: BLE001 - one session must not stop the scan
                log.exception(f'[CASHUSE] expiry scan of {session.get("char_name")!r} failed')
        return n

    def boost_exp(self, session, amount, now=None):
        """Kill exp through the best active EXP item of `session`'s character."""
        char = self._char(session)
        if char is None or _int(amount) <= 0:
            return _int(amount)
        with self.store.lock:
            pct = boost_pct(char, now)
        out = boosted(amount, pct)
        if out != amount:
            log.info(f'[CASHUSE] {session.get("char_name")!r}: +{pct}% EXP item: {amount} -> {out} exp')
        return out

    def waives_penalty(self, session, now=None):
        char = self._char(session)
        if char is None:
            return False
        with self.store.lock:
            return waives_penalty(char, now)

    # ================================================================== F9 ===
    def rename(self, sock, session, rec):
        """C2S 0x49 {str[17] new_name} -> 0x73 {1, serial} + 0x74 (self + holders), or 0x73 {0}.
        Returns the new name or None."""
        new = names.clean(rec.get('new_name', b''))

        def refuse(why):
            self.name_change_result(sock, session, False, why=why, what=f'rename to {new!r}')
            return None

        why = self._in_world_refusal(session)
        if why is not None:
            return refuse(why)
        srv = self.server
        if srv.trade.busy(session):
            return refuse('during a trade')
        if session.get('stall_client_selling') or srv.market.selling(session):
            return refuse('with a stall open')
        old, username = session.get('char_name'), session.get('username')
        char = self._char(session)
        touched = left = record = None
        with self.store.lock:
            # One hold of the store lock from the checks to the rename: the 1895 record is used
            # up and the name changed together in memory; the immediate save below writes both
            # (no free second rename after a crash, no half-renamed record).
            why = rename_refusal(new, old, self.store.name_owner(new), username)
            record = self.cash.find(char, NAME_CHANGE) if why is None else None
            if why is None and record is None:
                why = 'no usable 1895 Change Nickname record'
            if why is None:
                before = [dict(r) for r in CASH.ensure(char)]
                left = self.cash.consume(char, record['serial'], 1, what='rename')
                try:
                    # save=False: the write happens after this `with` (livetest bug 7: never
                    # under store.lock), so a failed write cannot split the state either - the
                    # rename is complete in memory and the store's retry persists it (P8 review).
                    touched = self.store.rename_character(username, old, new, save=False)
                except Exception as e:                   # noqa: BLE001 - the waiting box needs its 0x73
                    log.exception('[CASHUSE] rename failed in the store')
                    why = f'store: {e}'
                if touched is None:
                    char['cash_items'] = before          # nothing renamed: the ticket back
                    why = why or 'the name was taken meanwhile'
                else:
                    # Still under store.lock: GameServer._session_char looks the record up by
                    # session['char_name'] and falls back to chars[0], so a tick save / regen /
                    # peer handler waking on this lock must already find the new name (on a
                    # multi-character account chars[0] may be another character).
                    session['char_name'] = new
                    if session.get('msgr_name'):
                        session['msgr_name'] = new
        if why is not None:
            return refuse(why)
        # The immediate save (enter world and every name-addressed packet resolve through the
        # stored name). A failed write is logged and left dirty: the store retries with backoff.
        srv._save_store_now(f'rename {old!r} -> {new!r}')
        srv.world.enter(session, new)
        cooldowns = getattr(srv, 'skill_cooldowns', None)
        if isinstance(cooldowns, dict) and old.lower() in cooldowns:
            cooldowns[new.lower()] = cooldowns.pop(old.lower())
        uid = P.session_uid(session) or 0
        self.name_change_result(sock, session, True, record['serial'])
        fields = {'uid': uid, 'new_name': new}
        self._send(sock, session, '0x74', fields)
        seen = self._holders(session, '0x74', fields)
        cid = (char or {}).get('cid')
        log.info(f'[CASHUSE] {old!r} ({username}, cid {cid}) is now {new!r}: 1895 serial {record["serial"]:#x} '
                 f'({left} left); references rewritten in {touched} other record(s); 0x74 to {seen} holder(s)')
        # The C4 hook (world.ON_RENAME): each subscriber runs isolated (Hooks.fire logs and skips a
        # failing one), so the rename itself always stands.
        srv.world.hooks.fire(worldmod.ON_RENAME, srv, session, old=old, new=new, cid=cid)
        return new

    # ================================================================= F10 ===
    def stat_reset(self, sock, session, rec):
        """C2S 0x4A -> S2C 0x76 (owner 18 B; holders 12 B on success). Returns the owner's
        fields."""
        item = _int(rec.get('item_id'))
        removed = [_int(rec.get(k)) for k in ('str_removed', 'dex_removed', 'int_removed', 'tol_removed')]
        uid = P.session_uid(session) or 0
        char = self._char(session)
        stats = self.stats_of(char)

        def answer(serial=0, count=0, why=None):
            fields = {'target_uid': uid, **dict(zip(('str', 'dex', 'int', 'spi'), stats)),
                      'cash_item_serial': serial, 'consume_count': count}
            self._send(sock, session, '0x76', fields)
            if why:
                log.info(f'[CASHUSE] {session.get("char_name")!r} 0x4A {item} {removed} refused '
                         f'(0x76 unchanged, serial 0): {why}')
            return fields

        why = self._in_world_refusal(session)
        if why is not None or char is None:
            return answer(why=why or 'no character')
        with self.store.lock:
            stats = unchanged = self.stats_of(char)
            record = self.cash.find(char, item) if item in STAT_RESET else None
            why = stat_reset_refusal(item, removed, unchanged, record)
            if why is None:
                stats = [have - minus for have, minus in zip(unchanged, removed)]
                for key, value in zip(STAT_KEYS, stats):
                    char[key] = value
                serial, total = record['serial'], sum(removed)
                # the 0x76 consume_count: the client takes `total` off the same record (C13)
                left = self.cash.consume(char, serial, total, what='stat reset')
        if why is not None:
            return answer(why=why)
        self.store.mark_dirty(f'stat reset {char.get("name")}')
        self.server._refresh_vitals(session, char, reason='stat reset')
        fields = answer(serial, total)
        seen = self._holders(session, '0x76', fields)
        log.info(f'[CASHUSE] {session.get("char_name")!r} stat reset {EC.item_name(item) or item} (serial '
                 f'{serial:#x}): -{removed} -> STR/DEX/INT/SPR {stats}, {total} point(s) free again, '
                 f'{left} left; 0x76 to {seen} holder(s)')
        return fields

    @staticmethod
    def stats_of(char):
        """[str, dex, int, spr] of a character (the values the 0x07 / 0x03 send)."""
        return [max(0, min(0xFFFF, _int((char or {}).get(k)))) for k in STAT_KEYS]

    def refusal_76(self, session):
        """The owner form with unchanged stats, serial 0, count 0 (registry MUST_REPLY 0x4A)."""
        char = self._char(session) if session.get('char_name') else None
        return {'target_uid': P.session_uid(session) or 0,
                **dict(zip(('str', 'dex', 'int', 'spi'), self.stats_of(char))),
                'cash_item_serial': 0, 'consume_count': 0}

    # ================================================================= F11 ===
    def megaphone(self, sock, session, rec, now=None):
        """C2S 0x4C {item, str[61] text} -> S2C 0x90 to the audience + the owner's consume.
        Returns the number of players that got the line (0 = refused)."""
        item = _int(rec.get('item_id'))

        def refuse(why, tell=None):
            log.info(f'[CASHUSE] {session.get("char_name")!r} megaphone {item} refused: {why}')
            if tell and sock is not None and session.get('in_world'):
                self.server._notice(sock, session, tell, 'warn')
            return 0

        why = self._in_world_refusal(session)
        if why is not None:
            return refuse(why)
        if item not in MEGA_SUPER | MEGA:
            return refuse(f'item {item} is no megaphone')
        line = megaphone_text(session.get('char_name'), rec.get('message'))
        if not line:
            return refuse('empty text')
        char = self._char(session)
        if self.cash.find(char, item) is None:
            return refuse(f'no usable {item} record')
        mono = time.monotonic() if now is None else float(now)
        with self.lock:
            last = session.get('megaphone_t')
            if last is not None and mono - last < MEGAPHONE_RATE_SECS:
                return refuse(f'rate limit ({MEGAPHONE_RATE_SECS:g} s)', 'Please wait before using another megaphone.')
            session['megaphone_t'] = mono
        record, left = self._take_one(char, item, 'megaphone')
        if record is None:
            return refuse(f'the {item} record is gone')
        audience = self.audience(session, super_=item in MEGA_SUPER)
        heard = self.server._orange(audience, line)
        uid = P.session_uid(session) or 0
        if str(self.server.config.get('MEGAPHONE_CONSUME_PACKET', '0x72')).lower() == '0x6f':
            self.server._send_owned_cash(sock, session, reason='megaphone', force=True)
        else:
            self._send(sock, session, '0x72', {'player_uid': uid, 'item_id': item, 'item_serial': record['serial']})
        log.info(f'[CASHUSE] {session.get("char_name")!r} {EC.item_name(item) or item} (serial '
                 f'{record["serial"]:#x}, {left} left) -> {heard} player(s): {line!r}')
        return heard

    def audience(self, session, super_=True):
        """Who hears a megaphone: every in-world player (super), or those on the sender's
        channel (one channel per server today: the same set)."""
        sessions = list(self.server._in_world_sessions())
        if super_:
            return sessions
        channel = self.server._channel_no(session)
        return [s for s in sessions if self.server._channel_no(s) == channel]

    def _take_one(self, char, item, what):
        """find() and consume() of one `item` under ONE hold of the store lock, as use / rename /
        stat_reset do: (record, quantity left), or (None, None) when no usable record is left
        (P8 review: the separate holds let a line be broadcast, or a warp made, for a record
        that had gone in between)."""
        with self.store.lock:
            record = self.cash.find(char, item)
            left = None if record is None else self.cash.consume(char, record['serial'], 1, what=what)
        return (None, None) if left is None else (record, left)

    # ============================================================ F13 / F14 ===
    def _warp_refusal(self, session):
        why = self._in_world_refusal(session)
        if why is not None:
            return why
        if session.get('dead'):
            return 'dead'
        if session.get('current_map') in hpmp.ROOM_MAPS:
            return 'in a PvP room'
        return None

    def region_warp(self, sock, session, rec):
        """C2S 0x70 {item, i32 dest_map_id} -> 0x9A {1, serial} + the map load, or 0x9A {0}.
        Returns the destination map or None."""
        item, dest = _int(rec.get('item_id')), _int(rec.get('dest_map_id'))

        def refuse(why):
            self._send(sock, session, '0x9A', {'result': FAILED})
            log.info(f'[CASHUSE] {session.get("char_name")!r} region warp {item} -> {dest} refused (0x9A {{0}}): {why}')
            return None

        why = self._warp_refusal(session)
        if why is not None:
            return refuse(why)
        if item not in REGION_STONE:
            return refuse(f'item {item} is no General Teleport Stone')
        if not self.map_ok(dest):
            return refuse(f'map {dest} is not a field map of this client a portal leads into')
        char = self._char(session)
        srv = self.server
        x, y, how = srv._warp_point(dest)
        record, left = self._take_one(char, item, 'region warp')
        if record is None:
            return refuse(f'no usable {item} record')
        self._send(sock, session, '0x9A', {'result': OK, 'cash_item_serial': record['serial']})
        log.info(f'[CASHUSE] {session.get("char_name")!r} region warp {EC.item_name(item) or item} (serial '
                 f'{record["serial"]:#x}, {left} left): map {session.get("current_map")} -> {dest} at '
                 f'({x:.0f}, {y:.0f}), {how}')
        srv._map_transfer(sock, session, dest, x, y, reason='region warp', no_enc=session.get('no_enc', True))
        return dest

    # The arena / battlefield family (9801..9804 PvP rooms, 2009 9805 Seren plain Arena, 9901
    # Battlefield Lobby, 9902 / 9903): never a region-stone destination (design F13).
    ARENA_MAPS_FROM = 9800

    @staticmethod
    def map_ok(code):
        """A map a region stone may go to (design F13: a real destination, not an arena or
        instance map): one the running build's client has a stage file for (en_maps per
        CLIENT_DIR / CLIENT_DIR_2009), below the arena family (ARENA_MAPS_FROM, which also
        covers hpmp.ROOM_MAPS), and that a portal of the EN table leads into (combat.
        entry_point) - so the landing is that portal's arrival. That refuses 2009 9805 and
        9901 and 2008 1285 / 9901: stage files no portal reaches, where _warp_point fell back
        to map 101's start point on another map (P8 review). data.map_codes is a static EN
        table, not per build: it lists codes (219, 400, 929 ...) that neither client can
        load, so it never vouches for a destination."""
        if not 1 <= code < CashUse.ARENA_MAPS_FROM or code in hpmp.ROOM_MAPS:
            return False
        import combat                                   # late, like GameServer._client_has_map
        import en_maps
        if en_maps.load_map(code) is None:
            return False
        return combat.entry_point(code, EC.portals()) is not None

    def friend_warp(self, sock, session, rec):
        """C2S 0x71 {item, str[17] name} -> 0x9B {1, serial} + the map load beside the target,
        or 0x9B {0}. Returns the target session or None."""
        item = _int(rec.get('item_id'))
        wanted = social.name_text(rec.get('friend_name', b''))

        def refuse(why):
            self._send(sock, session, '0x9B', {'result': FAILED})
            log.info(f'[CASHUSE] {session.get("char_name")!r} friend warp {item} -> {wanted!r} refused '
                     f'(0x9B {{0}}): {why}')
            return None

        why = self._warp_refusal(session)
        if why is not None:
            return refuse(why)
        if item not in FRIEND_STONE:
            return refuse(f'item {item} is no Friend Teleport Stone')
        srv = self.server
        target = srv._go_target(wanted) if wanted else None
        if target is None or not presence.visible_to(target, session):
            return refuse('no such character in the world')
        if target is session or target.get('uid') == session.get('uid'):
            return refuse('oneself')
        if target.get('in_cash_shop') or not target.get('in_world'):
            return refuse(f'{target.get("char_name")!r} is in the mall / loading a map')
        code, x, y = srv._go_point(target)
        if code in hpmp.ROOM_MAPS:
            return refuse(f'{target.get("char_name")!r} is in a PvP room')
        char = self._char(session)
        record, left = self._take_one(char, item, 'friend warp')
        if record is None:
            return refuse(f'no usable {item} record')
        self._send(sock, session, '0x9B', {'result': OK, 'cash_item_serial': record['serial']})
        log.info(f'[CASHUSE] {session.get("char_name")!r} friend warp {EC.item_name(item) or item} (serial '
                 f'{record["serial"]:#x}, {left} left) -> {target.get("char_name")!r} on map {code} at '
                 f'({x:.0f}, {y:.0f})')
        srv._map_transfer(sock, session, code, x, y, reason='friend warp', no_enc=session.get('no_enc', True))
        return target

    # ----------------------------------------------------------------- views ---
    def describe(self, session, now=None):
        """Lines for `!cash use`: the active period items and their effects."""
        char = self._char(session)
        if char is None:
            return ['No character in this session.']
        with self.store.lock:
            active = [dict(r) for r in CASH.ensure(char) if CASH.is_activated(r)]
            pct, waive = boost_pct(char, now), waives_penalty(char, now)
        lines = [f'EXP bonus +{pct}%, death exp penalty {"waived" if waive else "normal"}; '
                 f'{len(active)} activated period item(s).']
        for r in active[:8]:
            state = 'EXPIRED' if CASH.is_expired(r, now) else 'until'
            lines.append(f'{r["serial"]:#x} {EC.item_name(r["item_id"]) or r["item_id"]} ({r["item_id"]}) '
                         f'{state} {r["expire"]}')
        return lines
