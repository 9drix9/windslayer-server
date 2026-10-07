#!/usr/bin/env python3
"""
pets.py - the 2009 pets (P15 pet-s1..pet-s6; the P8 seams C4 / C5 / C6; re_tools/docs/systems_2009/
pet.md, ROADMAP_2009_ADDENDUM P15; the mall half of pet-s7 is mall.py "Pets")
================================================================================================
    gs.pets = Pets(gs).install(gs.cashuse)       # GameServer.__init__ (after the cash use stage)
    register(gs.world.hooks, gs.pets)            # the C4 hook ON_RENAME (2009 only)
    gs.pets.equip(session, item_id)              # C2S 0x82 PetEquip   -> S2C 0xAB (F1)
    gs.pets.unequip(session, item_id)            # C2S 0x83 PetUnequip -> S2C 0xAC (F2)
    gs.pets.set_awake(session, awake)            # S2C 0xAD (F6; GM !pet set awake, a feed / bell wake)
    gs.pets.tick(now)                            # 'pet-tick' (GameServer.start): 0xAE / 0xAF / 0xAD(0) (F4)
    gs.pets.emote(session, action)               # C2S 0x86 PetEmote  -> S2C 0xB2 owner + viewers (F8)
    gs.pets.feed(session, food_id)               # C2S 0x85 PetFeed   -> S2C 0xB1 (+ 0xAD 1) (F5)
    gs.pets.use_bell(sock, session, item_id)     # C2S 0x15 Pet Bell  -> S2C 0x25 + 0xAD 1 (F7)
    gs.pets.equip_gear(sock, session, ...)       # C2S 0x0F pet gear  -> S2C 0x1D (F10, Kind 15 / 16)
    gs.pets.unequip_gear(sock, session, ...)     # C2S 0x11 pet gear  -> S2C 0x1E
    gs.pets.rename(sock, session, rec)           # C2S 0x4D -> S2C 0xC0 + 0xB0 | 0x73 {0} (F9, C5)
    gs.pets.owner_renamed(session, old, new)     # C4: world.ON_RENAME of a pet owner
    pets.sync_look(char)                         # appearance word 14 = the worn pet's Spr_Num

Only the EN 2009 client has pets (no 0xAB..0xC0 handler, no Type 6 item in 2008): every entry
point refuses on a 2008 server (`supported`), and nothing here ever builds a 2008 packet.

The model (pet.md 2.2, 6; cash.py "Pet records")
-----------------------------------------------
A pet is a kind-3 record in char['cash_items'] (cash.py, P8 C1) with {exp, awake, level, gauge,
name}; the WORN one (`equipped`) is grid slot 15 (records.pet_slot_2009) and is in every 0x6F,
first (C2 / H5). Its gear sits in grid slots 23 (Kind 15 headgear) / 24 (Kind 16 apparel) of
char['equipped'] (inventory.KIND_TO_CASH_SLOT_2009; worn through C2S 0x0F in pet-s5). The
character's appearance word 14 (store look_ext[0]) is the worn pet's Spr_Num (FUN_004282c0
look[kind 14] = spr: the owner's draw call takes the pet body layer from it, FUN_004332f0
:394-427) - sync_look, at every equip / unequip and every login (GameServer._compose_looks).

What each client holds (arch09-receiver-mirror; pet.md 4, 7 H1-H5)
-----------------------------------------------------------------
- The OWNER's client: pet_info IS its cash record (+0x1628), bound by the 0x6F (session
  'pet_bound' = the serial it bound) or by the local 0xAB; the local forms of 0xAB / 0xAD carry
  no tail (H3: exactly 6 / 5 bytes - the assume 'uid == local_player_uid' True).
- A VIEWER's client: a 0x1C-byte pet_info per owner entity, allocated by a record with has_pet 1
  (records.pet_block), a remote 0xAB with has_pet_info 1 or an 0xAD(1), freed with the owner's
  entity (0x06). Its clientview pet_info_seen is kept by presence.py (records, 0x06) and here
  (pet packets), always under the viewer's presence lock (presence.holders_apply), so the
  mirror changes in byte order.
- H1: 0xAC and 0xAD(0) go only to viewers in pet_info_seen (the remote paths write pet_info+0xA
  with no NULL check, 0x457B3C / 0x457C37). H2: 0xB0 / 0xC0 go out only for an AWAKE pet whose
  item the hii gives a sprite template (pet_drawn: CardNpc != 0, cp-1) - 0xC0 only to an owner
  whose client bound the record, 0xB0 only to viewers in pet_info_seen (pet-s6 below). H4:
  every name is cut inside its field (packets: str[N] -> N-1 bytes + NUL).
- presence.touch(owner) after every change: a record of the owner built before it and still in
  flight is rebuilt (presence "Observer broadcasts"), so no viewer ends up with a stale block.

Equip / unequip (F1 / F2) - no reply on a refusal (the client takes no lock, pet.md 5); the
refusals a player can cause are told with the client's own words as a 0x15 line:
- 0x82 {id}: a bagged pet record of that item (def Type 6) on a character in world (not in the
  mall), pet gear off (grid 23 / 24 empty: "Take off the equipment of your pet first."); a pet
  worn already is swapped - S2C 0xAC for it first (owner + its pet_info holders), which needs a
  free equipment-tab slot. Then the record is worn (bound at level 1 if it was a box pet, F13),
  look word 14 is its Spr_Num, persisted; S2C 0xAB local (6 B) to the owner, the remote form
  (21 B awake / 7 B asleep) to every viewer that holds the owner (pet_info_seen += uid when
  awake).
- 0x83 {id}: the worn pet's item, gear off, a free equipment-tab slot; the record goes back to
  the bag, word 14 = 0, persisted; S2C 0xAC to the owner and to the viewers in pet_info_seen
  (then dropped from it). Viewers without it get nothing: their stale slot-15 word is replaced
  by the owner's next record.

The tick (pet-s3; pet.md 2.7, F4; config PET_* rates, all [I])
-------------------------------------------------------------
The client's own hunger / EXP code (FUN_00425d90) is host-only and dead in EN, so the server
owns gauge, EXP and level. The 'pet-tick' scan (SCAN_SECS) adds the owner's FIELD time - in
world, not on a room / arena map (hpmp.ROOM_MAPS: the host gate map_mode == 0), not in the
mall, not dead - to a per-session accumulator (session TICK_KEY; never persisted, nothing
ticks offline), and each full period is one step():
  - awake, gauge < 2: the pet falls asleep - set_awake(False): gauge 0, S2C 0xAD {uid, 0} to
    the owner and ONLY to the viewers holding its pet_info (H1);
  - awake: gauge -= PET_HUNGER_STEP, EXP += PET_EXP_STEP (cap 30600); a level the EXP now gives
    (cash.pet_level_for) is +1 -> S2C 0xAF {uid, level} to the owner and the pet_info holders
    (the client's emote 4 / 5 and the growth phase at 5 / 9); then S2C 0xAE {gauge, exp} to the
    owner (local, no uid). An awake 0xAE at <= 10 % is the client's auto-feed trigger: it sends
    C2S 0x85 for the first food it owns (H8), answered below - once per step, never a loop;
  - asleep: gauge += PET_SLEEP_REGEN_STEP (cap 100) -> S2C 0xAE, so the client's own bell gate
    (gauge >= 10) sees what the server sees.
The local packets (0xAE / 0xB1 / the 5-byte 0xAD) go only to an owner whose client bound the
record (pet_bound == its serial): without its pet_info the client reads nothing.

Emotes (pet-s3, F8): C2S 0x86 {0x12 smile | 0x14 warning (Lv >= 5) | 0x15 trick (Lv >= 9)}
for an awake worn pet, PET_EMOTE_MIN_SECS apart -> S2C 0xB2 {uid, action} to the owner (its
client plays nothing until then) and to every client holding the owner (0xB2 is gated on the
receiver's pet_info and sprite, so it is always safe). "Insufficient pet level." is the
client's own refusal: nothing reaches the server.

Feeding and waking (pet-s4, F5 / F7; the 10 % rule)
---------------------------------------------------
- C2S 0x85 {food} (the cp-2 exe's auto-feed and bag feeding): the EN food id 4286..4289 as
  sent (pet F5 step 1). The stock exe's auto-feed asks for its KR 4282..4285, which are EN
  NON-food rows (ADDENDUM X8: what it found in the owned list was e.g. the Premium Guild
  Billboard), so those are dropped and logged, never mapped onto the player's Pet Food; that
  exe feeds through C2S 0x48 (C6). The server consumes one unit of the owned record the
  client's own lookup picks (cash.find = FUN_00464c10), sets the gauge to at least
  PET_FOOD_GAUGE and sends S2C 0xB1 {gauge, that serial} - the client consumes its copy BY
  SERIAL, so no 0x72 / 0x6F follows. A sleeping pet wakes: 0xB1 then 0xAD {uid, 1} (0xB1
  never touches awake). A full gauge (100) is refused as the client refuses a manual feed
  then - on both paths (the 0x48 food_gate too).
- The stock exe's fallback (CLIENT_ITEM_IDS 'kr'; C6 below): EN food used from the bag goes
  through dialog 0x3F4 as C2S 0x48 -> cashuse answers S2C 0x72 (its consume-by-serial, closes
  the waiting box), then fed() sends S2C 0xB1 {gauge, serial 0} (nothing consumed twice) and
  the wake's 0xAD.
- The Pet Bell (EN 4285, a Type-0 bag item) through C2S 0x15: the worn pet must be ASLEEP with
  gauge >= 10 ("Pet has to have at least over 10% HP to wake up.", 0x52B4AC: the cp-2 exe's
  own gate at 0x44FD72; the stock exe sends the bell ungated, so the server tells the line).
  One bell leaves the bag, S2C 0x25 {4285} (the client removes it and plays the use effect),
  then the wake's 0xAD. Feeding wakes a pet at any gauge; only the bell has the 10 % rule.

Pet gear (pet-s5, F10; inventory.is_pet_gear)
---------------------------------------------
Type 1 cash, Kind 15 (headgear -> grid 23) / 16 (apparel -> grid 24). Like every cash item it
is a record of char['cash_items'] (cash.CashInventory._grant_gear; the 0x6F purges cash items
from every bag and files a Type 1 record in the equipment tab), worn through the NORMAL
C2S 0x0F / 0x11, which GameServer hands over here: 0x0F needs an AWAKE worn pet ("Register the
pet first.", 0x52B5C4: FUN_0044fe20 tests grid 15 and +0x127C, zeroed with the sprite) of the
gear's species ("This is not the equipment of your pet.", 0x52B59C: template+0x154 == the
gear's CardNpc - cp-1 data; on an unpatched hii, where both are 0, the EN rule that a pet's
four gear rows are the four ids below it). The record becomes `equipped`, the grid slot holds
it, look word 15 / 16 is recomposed (inventory.compose) and S2C 0x1D echoes the request to the
owner (its client moves the record owned -> equipped, FUN_00462b70) and goes to the viewers
(their grid + look: the gear is drawn in the owner's frame). 0x11 is the reverse (0x1E; room
in the equipment tab). A pet with gear on can be neither swapped nor taken off ("Take off the
equipment of your pet first.", F1 / F2 above). Gear has no stat columns, so no vitals change.

Auto-loot (T14, F12): the pet's pickup is C2S 0x1F, the W key's wire form; the client checks
the range from the PET (<= 25 px), the owner / drop_time rule and, for equipment, pet level
>= 5 - so the server, which never sees the pet's position, keeps its own checks (loot
protection, bag room incl. the bagged pet / gear slots) and no range gate
(GameServer._handle_ground_pickup).

C6 - C2S 0x48 on an UNPATCHED exe (pet.md B2, F5 step 5; config CLIENT_ITEM_IDS 'kr')
------------------------------------------------------------------------------------
The stock exe hard-codes the KR item ids (4 lower than EN above 4248: cp-2 fixes them), so the
pet-food case of its cash-use switch (FUN_0046d6f0, KR 0x10BA..0x10BD) and the rename dialog
(KR 0x10DE -> window 0x4CB) never match the EN items. Using EN Pet Food 4286..4289 or the pet
name ticket 4322 from the bag therefore falls into the generic dialog 0x3F4, which sends C2S
0x48 {id} behind "Waiting for the server to respond." - the cash-use dispatcher
(cashuse.CashUse.use) answers S2C 0x72 and calls this hook:
  - food: the gate refuses (0x72 {uid, 0, 0}: the box closes, the food is kept) unless the
    character wears a pet (cash.equipped_pet); with one, the use consumes one (0x72 {uid, id,
    serial} - the client's own consume-by-serial) and fed() applies pet F5 step 2 to the model
    (gauge up to PET_FOOD_GAUGE), sends S2C 0xB1 {gauge, serial 0} (the gauge redraw and the
    "I feel new power." bubble; serial 0 consumes nothing a second time) and wakes a sleeping
    pet (0xAD to the owner and the viewers). On the cp-2 exe (CLIENT_ITEM_IDS 'en', the live
    default) the food goes out as C2S 0x85 instead and never reaches 0x48; one that does means
    the exe is the stock one - logged once, and fed the same way.
  - the name ticket: always refused and kept. The rename window 0x4CB (-> C2S 0x4D) opens only
    on a cp-2 patched exe; through 0x3F4 the ticket would be spent for nothing.
Every other 0x48 is the generic dispatcher's own business.

Rename (pet-s6, F9; C5) - C2S 0x4D PetRename (2009 only; pet.md 3, 5, 7 H2)
--------------------------------------------------------------------------
{u32 pet_serial, str[13] new_name}, sent from window 0x4CC after the cp-2 exe's ticket case
(FUN_0046d6f0 EN 4322 -> window 0x4CB, only with a worn pet record), then the client shows
"Waiting for the server to respond." (0x52CF5C): a MUST-REPLY (registry BUILD_MUST_REPLY 0x4D,
the exception backstop). Exactly one answer per request, under the owner's pet lock:
  - success: the serial is the WORN pet record's (FUN_00462c80(+0x16E)), the pet is AWAKE, the
    owner's client bound the record (pet_bound) and its hii row has a sprite template
    (pet_drawn) - H2: 0xC0 / 0xB0 strcpy the name into *(entity+0x1638) with no NULL check
    (0x46BD38 / 0x45D49F), and that sprite exists only then -; the name passes FUN_0043e1b0's
    character-name rules (names.check) inside str[13] (<= 12 bytes) and is not the current
    one; the character owns a usable "Pet name making" record (EN 4322, cash.find). One ticket
    is consumed, the name stored; S2C 0xC0 {ticket serial, name} to the owner - its client
    consumes the ticket BY SERIAL (FUN_0046df70 on the owned list), closes box 0x16 and shows
    "Pet name has been changed." - and S2C 0xB0 {uid, name} to every viewer in pet_info_seen
    (each holds a sprite of the awake pet); presence.touch first, so a record of the owner in
    flight is rebuilt with the new name (records.pet_block).
  - anything else (asleep: T16): S2C 0x73 {0} through the one 0x73 builder
    (cashuse.name_change_result, C5) - it closes box 0x16 and shows the client's "The name is
    already being used." text; nothing is consumed. 0xC0 has no failure form.
On the stock exe ('kr') 0x4D cannot be sent (no 0x4CB); a forged one is answered the same way.

The owner's own rename (C4; world.ON_RENAME, cashuse.CashUse.rename -> owner_renamed)
------------------------------------------------------------------------------------
Nothing of a pet is keyed by its owner's NAME: the records are the character's own (serials;
the store renames the record in place), the session state is per session (pet_bound, the tick,
the emote throttle), each viewer's pet_info hangs off the owner's ENTITY by uid (+0x163C /
pet_info_seen), and the pet's tag is the pet's own name (pet_info+0xD). The 0x74 the rename
sends rewrites only the owner entity's name (entity+0x00) - the sprite, pet_info and +0x1638
stay - so the subscriber sends no pet packet (an 0xB0 would only repeat the name, and is the
H2 hazard while the pet sleeps): it checks that the worn record, the owner's pet_bound and the
viewers' pet_info mirror still line up and logs where the pet went, for the live check.

Locks: one pet lock per owner session (session['pet_lock']) serializes [change, owner packet,
viewer packets] of that owner's pet; under it the owner's combat lock (only the bell's bag
change, taken with no store lock held: the GameServer ladder combat_lock -> store.lock), the
store lock (let go before any packet), then a viewer's presence lock (holders_apply) ->
clientview lock -> send_lock. Nothing takes the pet lock under any of those, so the 'pet-tick'
(world_lock -> pet lock) and the handlers (pet lock first) cannot cross. A rename's 0x73
refusal is sent after the pet lock is let go (cashuse's own send).
"""
import logging
import threading
import time

import cash as CASH
import cashuse as CU
import clientview as cview
import config as cfgmod
import en_content as EC
import gm
import hpmp
import inventory as invmod
import names
import packets as P
import presence
import records as R
import world as worldmod

log = logging.getLogger('WS')

BUILD = '2009'                     # the only client with pets
# pet F5 step 2 [I]: "stamina will be recovered up to 90%" (hii text of 4286..4289); the live
# value is config PET_FOOD_GAUGE (this is its default).
FOOD_GAUGE = cfgmod.DEFAULTS['PET_FOOD_GAUGE']
# ---- P15 stage 2 (pet-s3 / pet-s4 / pet-s5) ----
# The tick scan: GameServer.start runs tick() this often; the periods themselves are config
# PET_HUNGER_SECS / PET_SLEEP_REGEN_SECS (>= 1 s, so one step per scan at most).
SCAN_SECS = 1.0
TICK_KEY = 'pet_tick'              # session: {serial, last, awake, asleep} - runtime only
# A scan gap longer than this (a stalled scheduler) counts only this much field time.
MAX_GAP_SECS = 5.0
# FUN_00425d90 (the dead host code): an awake pet whose gauge is below 2 falls asleep.
SLEEP_BELOW = 2
# C2S 0x86 / S2C 0xB2 actions (FUN_004462e0: "/Pet smile" 0x12 any level, "/Pet warning" 0x14
# level >= 5, "/Pet trick" 0x15 level >= 9; "Insufficient pet level." 0x528068 is client-side).
EMOTE_LEVEL = {0x12: 1, 0x14: 5, 0x15: 9}
# The server's emote throttle (PET_EMOTE_MIN_SECS, the client's own 700 ms) less this much
# latency slack, as the skill cooldowns allow (GameServer.SKILL_CT_TOLERANCE_MS).
EMOTE_TOLERANCE_SECS = 0.15
# An awake S2C 0xAE at or below this gauge is the cp-2 client's auto-feed trigger (H8): it sends
# C2S 0x85 for each one - `!pet tick n` stops its burst after the first.
AUTO_FEED_AT = 10
# The Pet Bell gate (FUN_0044f070 @0x44FD72 on the cp-2 exe): asleep and gauge >= 10.
BELL_MIN_GAUGE = 10
BELL_TEXT = 'Pet has to have at least over 10% HP to wake up.'      # 0x52B4AC (one line here)
REGISTER_TEXT = 'Register the pet first.'                           # 0x52B5C4 (FUN_0044fe20)
NOT_YOURS_TEXT = 'This is not the equipment of your pet.'           # 0x52B59C (FUN_0044fe20)
# The EN 2009 hii puts a pet's four gear rows right below it (pet.md 2.1: Picky 4294 <- its
# gear 4290..4293, Ulie 4299 <- 4295..4298, ...): the species rule when CardNpc is 0 (no cp-1).
GEAR_ROWS = 4
# The receiver gate of the local-only grammars 0xAE / 0xB1 (spec_2009): the owner's client
# holds the pet_info - stated only for an owner whose client bound the record (pet_bound).
LOCAL_INFO_GATE = 'local_player_pet_info == null'
# Grid slots (FUN_00427af0, inventory.KIND_TO_CASH_SLOT_2009): 15 = the pet (Kind 14 cash),
# 23 / 24 = its headgear (Kind 15) / apparel (Kind 16).
PET_SLOT = 15
GEAR_SLOTS = (23, 24)
# The client's own texts (2009 exe strings, pet.md 2.4 / FUN_0044fe20 / FUN_00477000).
GEAR_ON_TEXT = 'Take off the equipment of your pet first.'          # 0x52B570
BAG_FULL_TEXT = "There isn't empty space in the inventory."
# Receiver-state conditions of the pet grammars (spec_2009 0xAB / 0xAD / 0xAF): every pet
# packet is built for one receiver with these stated, never guessed (packets.MissingAssume).
AB_GATE = 'entity_lookup(uid) == null || pet_item_valid(pet_item_id) == 0'
AD_GATE = 'entity_lookup(uid) == null'
AF_GATE = 'entity_lookup(uid) == null || pet_info_of(uid) == null'
B0_GATE = AF_GATE                  # spec_2009 0xB0: the same lookup + pet_info gate as 0xAF
C0_GATE = LOCAL_INFO_GATE          # spec_2009 0xC0: nothing is read without a local pet_info
LOCAL = 'uid == local_player_uid'
# P15 pet-s6 (F9): what the owner's client shows on S2C 0xC0 itself (for the log line; the
# ticket C2S 0x4D spends is EN 4322 "Pet name making", cash.PET_NAME_TICKET).
RENAMED_TEXT = 'Pet name has been changed.'                         # 0x529480 (the 0xC0 popup)
# `!pet give` by species (EN 2009 hii 4294 / 4299 / 4304 / 4309, pet.md 2.1).
SPECIES = {'picky': 4294, 'ulie': 4299, 'chikapuka': 4304, 'guriguri': 4309}
SET_FIELDS = ('exp', 'level', 'gauge', 'awake', 'name')


# ------------------------------------------------------------- pure helpers ---
def pet_spr(item_id, catalog=None):
    """The pet item's Spr_Num (hii def+0x1D0): the appearance word 14 it is drawn with."""
    catalog = catalog if catalog is not None else EC.items()
    d = catalog.get(int(item_id)) if item_id else None
    return int(getattr(d, 'spr_num', 0) or 0) & 0xFFFF if d is not None else 0


def sync_look(char, catalog=None):
    """Store look_ext[0] (appearance word 14) = the worn pet's Spr_Num, else 0 - what the client
    composes on 0xAB / 0xAC (FUN_004282c0 look[14] = spr / 0, pet.md 2.3). Words 15 / 16 (the pet
    gear) are inventory.compose's like any grid item. A new list when it changes (a save may be
    serializing the old one). Returns True when it changed. Caller holds the store lock."""
    pet = CASH.equipped_pet(char)
    want = pet_spr(pet['item_id'], catalog) if pet is not None else 0
    ext = [int(v) & 0xFFFF for v in list((char or {}).get('look_ext') or [])][:3]
    ext += [0] * (3 - len(ext))
    if ext[0] == want and char.get('look_ext') == ext:
        return False
    ext[0] = want
    char['look_ext'] = ext
    return True


def remote_info(pet_rec):
    """(has_pet_info, level, name) of the remote 0xAB / 0xAD forms: the info goes out only for
    an awake pet (pet.md 3 0xAB "server rules", F6)."""
    pet = CASH.normalize_pet((pet_rec or {}).get('pet'))
    return bool(pet['awake']), max(1, pet['level']), pet['name']


def ab_fields(owner_uid, item_id, local, pet_rec=None):
    """S2C 0xAB PetEquip for one receiver: the local form is the 6-byte {uid, item} (H3); the
    remote one adds has_pet_info (+ level, str[13] name when the pet is awake). Returns
    (fields, assume) for packets.send / GameServer._push."""
    fields = {'uid': int(owner_uid) & 0xFFFFFFFF, 'pet_item_id': int(item_id) & 0xFFFF}
    if not local:
        has, level, name = remote_info(pet_rec)
        fields['has_pet_info'] = int(has)
        if has:
            fields.update(pet_level=level, pet_name=name)
    return fields, {AB_GATE: False, LOCAL: bool(local)}


def ad_fields(owner_uid, awake, local, pet_rec=None):
    """S2C 0xAD PetAwakeState for one receiver: {uid, awake} (5 B) for the owner and for a sleep;
    a remote wake adds u8 level + str[13] name (19 B)."""
    fields = {'uid': int(owner_uid) & 0xFFFFFFFF, 'awake': int(bool(awake))}
    if awake and not local:
        _has, level, name = remote_info(pet_rec)
        fields.update(pet_level=level, pet_name=name)
    return fields, {AD_GATE: False, LOCAL: bool(local)}


def af_fields(owner_uid, level):
    """S2C 0xAF PetLevelUp {uid, u8 level} for a receiver that holds the owner's pet_info (the
    gated read: without one the client reads the uid only)."""
    return ({'uid': int(owner_uid) & 0xFFFFFFFF, 'pet_level': max(1, min(CASH.PET_LEVEL_MAX, int(level)))},
            {AF_GATE: False})


def ae_fields(gauge, exp):
    """S2C 0xAE PetStatus {u8 gauge, u16 exp} (3 B, local, no uid) for an owner whose client
    bound the pet record - the only receiver whose local pet_info exists."""
    return ({'pet_hp': max(0, min(CASH.PET_GAUGE_MAX, int(gauge))),
             'pet_exp': max(0, min(CASH.PET_EXP_MAX, int(exp)))}, {LOCAL_INFO_GATE: False})


def b1_fields(gauge, food_serial):
    """S2C 0xB1 PetFeedResult {u8 gauge, u32 food serial} (5 B, local): the client consumes one
    unit of the record with that serial (FUN_0046df70; 0 / unknown = nothing) and redraws."""
    return ({'pet_hp': max(0, min(CASH.PET_GAUGE_MAX, int(gauge))),
             'food_item_serial': int(food_serial or 0) & 0xFFFFFFFF}, {LOCAL_INFO_GATE: False})


def pet_drawn(item_id, catalog=None):
    """Whether the clients build a SPRITE for pet item `item_id`: every sprite builder
    (FUN_00447f40, FUN_00448350, the 0x6F finalize) takes the NPC template from the item's hii
    CardNpc (def+0x150) and returns silently when it is 0 (pet.md B1; the stock EN hii has 0,
    cp-1 sets 182..185). Without one, entity+0x1638 stays NULL while pet_info exists, so an
    0xB0 / 0xC0 would strcpy through NULL (H2, 0x45D49F / 0x46BD38). The server reads the
    client's own hii (CLIENT_DIR_2009), so this is the installed data's answer."""
    catalog = catalog if catalog is not None else EC.items()
    d = catalog.get(int(item_id)) if item_id else None
    return bool(d is not None and getattr(d, 'card_npc', 0))


def pet_name_refusal(name):
    """Why `name` (str / bytes, cut at its NUL) cannot be a pet name, or None: the client's own
    validator FUN_0043e1b0(name, 1) runs the character-name rules (names.check: no space, no
    reserved word such as "gamemaster" / "wind", the server's [A-Za-z0-9] policy), and every
    pet packet carries the name in str[13] (<= 12 bytes + its NUL, H4)."""
    text = names.clean(name)
    bad = names.check(text)
    if bad is not None:
        return bad
    size = len(P.to_bytes(text))
    if size > CASH.PET_NAME_MAX:
        return f'{size} bytes, the pet name fields hold {CASH.PET_NAME_MAX}'
    return None


def c0_fields(ticket_serial, name):
    """S2C 0xC0 PetRenameResult {u32 ticket serial, str[13] name} (17 B) for an owner whose
    client bound the pet record (the only receiver that reads the body). Its client consumes one
    unit of the record with that serial from the owned list (FUN_0046df70) and copies the name
    into pet_info+0xD and the sprite."""
    return ({'cash_item_serial': int(ticket_serial or 0) & 0xFFFFFFFF, 'pet_name': CASH.clip_pet_name(name)},
            {C0_GATE: False})


def b0_fields(owner_uid, name):
    """S2C 0xB0 PetRename {u32 uid, str[13] name} (17 B) for a viewer holding the owner's
    pet_info (the gated read: without one only the uid is read)."""
    return ({'uid': int(owner_uid) & 0xFFFFFFFF, 'pet_name': CASH.clip_pet_name(name)}, {B0_GATE: False})


def gear_species_ok(gear_id, pet_item_id, catalog=None):
    """Whether pet gear `gear_id` fits the pet `pet_item_id` (FUN_0044fe20: the worn pet's NPC
    template index template+0x154 must equal the gear's hii CardNpc; the template comes from the
    PET item's CardNpc, so with the cp-1 data both are 182..185). On an unpatched hii both are 0
    (and the client shows no pet at all): the EN row rule GEAR_ROWS."""
    catalog = catalog if catalog is not None else EC.items()
    gear, pet = catalog.get(int(gear_id)), catalog.get(int(pet_item_id))
    if gear is None or pet is None:
        return False
    if getattr(gear, 'card_npc', 0) and getattr(pet, 'card_npc', 0):
        return gear.card_npc == pet.card_npc
    return int(pet_item_id) - GEAR_ROWS <= int(gear_id) < int(pet_item_id)


class PetRefused(Exception):
    """A pet request the server drops (the client takes no lock: pet.md 5). `tell`: the
    client's own text for the 0x15 line, None = only logged (a forged or stale request)."""

    def __init__(self, why, tell=None):
        super().__init__(why)
        self.tell = tell


# ------------------------------------------------------------------ runtime ---
class Pets:
    """The pets of one GameServer (GameServer.pets)."""

    def __init__(self, server):
        self.server = server

    @property
    def supported(self):
        return str(getattr(self.server, 'client_build', None)) == BUILD

    @property
    def client_item_ids(self):
        cfg = getattr(self.server, 'config', None)
        return cfg.get('CLIENT_ITEM_IDS', 'en') if cfg is not None else 'en'

    def cfg(self, key):
        """A PET_* config value (config.py holds the defaults and the [I] notes)."""
        cfg = getattr(self.server, 'config', None)
        default = cfgmod.DEFAULTS[key]
        return cfg.get(key, default) if cfg is not None else default

    # --------------------------------------------------------------- plumbing ---
    @staticmethod
    def lock(session):
        lock = session.get('pet_lock')
        if lock is None:
            lock = session.setdefault('pet_lock', threading.RLock())
        return lock

    def _char(self, session):
        return self.server._session_char(session) if session.get('char_name') else None

    @staticmethod
    def _who(session):
        return repr(session.get('char_name') or session.get('username'))

    def _push(self, target, key, fields_assume):
        fields, assume = fields_assume
        return bool(self.server._push(target, key, fields, 'PET', assume=assume))

    def _tell(self, session, text):
        sock = session.get('sock')
        if sock is not None and text:
            self.server._notice(sock, session, text, 'warn')

    def gear_on(self, session, char):
        """The pet gear slots (23 / 24) that hold an item: the client refuses 0x82 / 0x83 then."""
        return [slot for slot in GEAR_SLOTS if R.grid_item(session, char, slot)]

    def _in_world(self, session):
        if not self.supported:
            raise PetRefused('the 2008 client has no pets')
        char = self._char(session)
        if char is None or not session.get('in_world'):
            raise PetRefused('no character in world')
        if session.get('in_cash_shop'):
            raise PetRefused('in the Item Mall')
        return char

    def _gender(self, session, char):
        return R.record_gender(char, self.server._session_account(session), BUILD)

    # ----------------------------------------------------------- F1: equip ---
    def equip(self, session, item_id, what='C2S 0x82'):
        """C2S 0x82 PetEquip {u16 pet_item_id} (pet F1). Returns the worn record (a copy), or
        None when refused (logged; told with the client's own words when the player can act)."""
        item_id = int(item_id) & 0xFFFF
        try:
            return self._equip(session, item_id, what)
        except PetRefused as e:
            log.info(f'[PET] {self._who(session)} {what} equip {item_id} refused: {e}')
            self._tell(session, e.tell)
            return None

    def _equip(self, session, item_id, what):
        char = self._in_world(session)
        uid = P.session_uid(session) or 0
        store = self.server.store
        with self.lock(session):
            with store.lock:
                d = CASH.cash_def(item_id)
                if d is None or not d.is_pet:
                    raise PetRefused(f'{item_id} is no pet item (hii Type 6)')
                new = next((r for r in CASH.ensure(char) if CASH.is_pet_record(r) and r['item_id'] == item_id
                            and not r.get('equipped')), None)
                if new is None:
                    raise PetRefused(f'no bagged {d.name or item_id} record (the 0xAB local form needs it in the '
                                     f'owned list and the equipment tab, H7)')
                gear = self.gear_on(session, char)
                if gear:
                    raise PetRefused(f'pet gear in grid slot(s) {gear}', GEAR_ON_TEXT)
                old = CASH.equipped_pet(char)
                if old is not None and invmod.Inventory(char).free_slots('equip') < 1:
                    # the swap's 0xAC puts the old pet back in the tab BEFORE the 0xAB takes the
                    # new one out of it
                    raise PetRefused('the equipment tab has no slot for the pet worn now', BAG_FULL_TEXT)
                CASH.bind_pet(new)                       # a level-0 box pet: bound (F13); else a no-op
                old_copy = dict(old) if old is not None else None
                if old is not None:
                    old['equipped'] = False
                new['equipped'] = True
                sync_look(char)
                worn = dict(new, pet=dict(new['pet']))
            store.mark_dirty(f'pet equip {session.get("char_name")}')
            presence.touch(session)
            if old_copy is not None:
                self._send_unequip(session, uid, old_copy['item_id'])
            self._push(session, '0xAB', ab_fields(uid, item_id, True))
            session['pet_bound'] = worn['serial']          # the local 0xAB bound +0x1628 (C2)
            sent = presence.holders_apply(self.server, session, lambda peer: self._remote_equip(peer, uid, worn),
                                          touch_first=False)
        log.info(f'[PET] {self._who(session)} {what}: wears {worn["pet"]["name"]!r} {d.name or item_id} '
                 f'({item_id}, serial {worn["serial"]:#x}, Lv{worn["pet"]["level"]} '
                 f'{"awake" if worn["pet"]["awake"] else "asleep"})'
                 + (f' in place of {old_copy["item_id"]} ({old_copy["serial"]:#x})' if old_copy else '')
                 + f'; 0xAB local + remote to {sent} viewer(s)')
        return worn

    def _remote_equip(self, peer, uid, worn):
        """Under the viewer's presence lock: the remote 0xAB, and its pet_info when awake."""
        fields_assume = ab_fields(uid, worn['item_id'], False, worn)
        if not self._push(peer, '0xAB', fields_assume):
            return 0
        if fields_assume[0]['has_pet_info']:
            cview.note_pet_info(peer, uid)
        return 1

    # --------------------------------------------------------- F2: unequip ---
    def unequip(self, session, item_id, what='C2S 0x83'):
        """C2S 0x83 PetUnequip {u16 pet_item_id} (pet F2). Returns the record taken off (a
        copy), or None when refused."""
        item_id = int(item_id) & 0xFFFF
        try:
            return self._unequip(session, item_id, what)
        except PetRefused as e:
            log.info(f'[PET] {self._who(session)} {what} unequip {item_id} refused: {e}')
            self._tell(session, e.tell)
            return None

    def _unequip(self, session, item_id, what):
        char = self._in_world(session)
        uid = P.session_uid(session) or 0
        store = self.server.store
        with self.lock(session):
            with store.lock:
                pet = CASH.equipped_pet(char)
                if pet is None or pet['item_id'] != item_id:
                    raise PetRefused(f'the worn pet is {pet["item_id"] if pet else "none"}, not {item_id}')
                gear = self.gear_on(session, char)
                if gear:
                    raise PetRefused(f'pet gear in grid slot(s) {gear}', GEAR_ON_TEXT)
                if invmod.Inventory(char).free_slots('equip') < 1:
                    raise PetRefused('the equipment tab is full', BAG_FULL_TEXT)
                pet['equipped'] = False
                sync_look(char)
                off = dict(pet, pet=dict(pet['pet']))
            store.mark_dirty(f'pet unequip {session.get("char_name")}')
            presence.touch(session)
            sent = self._send_unequip(session, uid, item_id)
        log.info(f'[PET] {self._who(session)} {what}: took off {off["pet"]["name"]!r} ({item_id}, serial '
                 f'{off["serial"]:#x}); 0xAC local + to {sent} viewer(s) holding its pet_info')
        return off

    def _send_unequip(self, session, uid, item_id):
        """S2C 0xAC to the owner (it clears +0x1628: pet_bound None) and to every viewer in
        pet_info_seen, which drops the uid (F2 steps 3-4, H1). Caller holds the pet lock and
        changed the model first. Returns how many viewers got it."""
        self._push(session, '0xAC', ({'uid': uid, 'pet_item_id': item_id}, None))
        session['pet_bound'] = None

        def one(peer):
            if not cview.has_pet_info(peer, uid):
                return 0                                   # H1: no pet_info there - nothing
            sent = self._push(peer, '0xAC', ({'uid': uid, 'pet_item_id': item_id}, None))
            cview.forget_pet_info(peer, uid)
            return int(sent)
        return presence.holders_apply(self.server, session, one, touch_first=False)

    # ------------------------------------------------------ F6: sleep / wake ---
    def set_awake(self, session, awake, gauge=None, what='set'):
        """Put the worn pet to sleep or wake it (pet F6): the model (asleep: gauge 0, as the
        client zeroes +0xC on the local 0xAD(0)), then S2C 0xAD - the 5-byte local form to the
        owner when its client bound the record (+0x1628, pet_bound), a wake's 19-byte remote form
        to every viewer that holds the owner (pet_info_seen += uid: 0xAD(1) allocates it), a
        sleep's 5-byte form ONLY to viewers in pet_info_seen (H1; they keep the block). Returns
        the worn record (a copy) or None when no pet is worn."""
        char = self._char(session)
        if not self.supported or char is None:
            return None
        uid = P.session_uid(session) or 0
        awake = bool(awake)
        with self.lock(session):
            with self.server.store.lock:
                rec = CASH.equipped_pet(char)
                if rec is None:
                    return None
                pet = CASH.normalize_pet(rec.get('pet'))
                pet['awake'] = awake
                if gauge is not None:
                    pet['gauge'] = max(0, min(CASH.PET_GAUGE_MAX, int(gauge)))
                if not awake:
                    pet['gauge'] = 0
                rec['pet'] = pet
                worn = dict(rec, pet=dict(pet))
            self.server.store.mark_dirty(f'pet {"wake" if awake else "sleep"} {session.get("char_name")}')
            self._restart_tick(session)
            if not session.get('in_world'):
                return worn                     # the next map load's 0x07 / 0x6F carry it
            sent = self._announce_awake(session, uid, worn, awake)
        log.info(f'[PET] {self._who(session)} {what}: {worn["pet"]["name"]!r} {"wakes" if awake else "falls asleep"} '
                 f'(gauge {worn["pet"]["gauge"]}%); 0xAD to the owner and {sent} viewer(s)')
        return worn

    def _announce_awake(self, session, uid, worn, awake):
        """S2C 0xAD for a model change already made (caller holds the pet lock): the 5-byte
        local form to the owner when its client bound the record, a wake's 19-byte remote form
        to every viewer holding the owner (it allocates the pet_info: pet_info_seen += uid), a
        sleep's 5-byte form only to the viewers in pet_info_seen (H1). Returns the viewers sent."""
        presence.touch(session)
        if session.get('pet_bound') == worn['serial']:
            self._push(session, '0xAD', ad_fields(uid, awake, True))

        def one(peer):
            if not awake and not cview.has_pet_info(peer, uid):
                return 0                               # H1
            if not self._push(peer, '0xAD', ad_fields(uid, awake, False, worn)):
                return 0
            if awake:
                cview.note_pet_info(peer, uid)
            return 1
        return presence.holders_apply(self.server, session, one, touch_first=False)

    def _send_level(self, session, uid, worn):
        """S2C 0xAF {uid, level} to the owner (bound) and the viewers holding its pet_info (the
        client reads the level only with one; every effect inside is gated on the sprite)."""
        level = worn['pet']['level']
        if session.get('pet_bound') == worn['serial']:
            self._push(session, '0xAF', af_fields(uid, level))

        def one(peer):
            return int(cview.has_pet_info(peer, uid) and self._push(peer, '0xAF', af_fields(uid, level)))
        return presence.holders_apply(self.server, session, one, touch_first=False)

    # ------------------------------------------------------- pet-s3: the tick ---
    def _ticking(self, session):
        """Field time (pet F4, the host gate `map_mode == 0`): in world on a map, not a room /
        arena map (hpmp.ROOM_MAPS), not in the mall, not dead."""
        return bool(session.get('in_world') and session.get('current_map') is not None
                    and session.get('current_map') not in hpmp.ROOM_MAPS
                    and not session.get('in_cash_shop') and not session.get('dead'))

    @staticmethod
    def _restart_tick(session):
        """A sleep / wake starts both periods over (the host keeps one accumulator per state)."""
        st = session.get(TICK_KEY)
        if st is not None:
            st['awake'] = st['asleep'] = 0.0

    def tick(self, now=None):
        """'pet-tick' (GameServer.start, every SCAN_SECS on the tick scheduler, world_lock held):
        add each owner's field time since the last scan to its worn pet's accumulator and run a
        step() for each full PET_HUNGER_SECS (awake) / PET_SLEEP_REGEN_SECS (asleep). Time off
        the field does not count (its `last` is dropped), a scan gap counts at most MAX_GAP_SECS.
        Offline tests call it with an explicit clock. Returns the number of steps run."""
        if not self.supported:
            return 0
        now = time.monotonic() if now is None else float(now)
        steps = 0
        for session in list(getattr(self.server, 'sessions', {}).values()):
            try:
                steps += self._tick_one(session, now)
            except OSError as e:
                # a socket that died between the checks and the send: its own connection
                # thread cleans the session up
                log.debug(f'[PET] tick of {self._who(session)}: send failed ({e})')
            except Exception:                       # noqa: BLE001 - one owner's error must not
                # stop the scan: every later session's pet would miss this second (and every
                # second while the error persists)
                log.exception(f'[PET] tick of {self._who(session)} failed; the scan goes on')
        return steps

    def _tick_one(self, session, now):
        char = self._char(session)
        rec = CASH.equipped_pet(char) if char is not None else None
        st = session.get(TICK_KEY)
        if rec is None or not self._ticking(session):
            if st is not None:
                st['last'] = None
            return 0
        if st is None or st.get('serial') != rec['serial']:
            session[TICK_KEY] = {'serial': rec['serial'], 'last': now, 'awake': 0.0, 'asleep': 0.0}
            return 0
        last, st['last'] = st.get('last'), now
        if last is None:
            return 0
        dt = max(0.0, min(now - last, MAX_GAP_SECS))
        awake = bool(CASH.normalize_pet(rec.get('pet'))['awake'])
        key, period = (('awake', float(self.cfg('PET_HUNGER_SECS'))) if awake
                       else ('asleep', float(self.cfg('PET_SLEEP_REGEN_SECS'))))
        st[key] += dt
        if st[key] < period:
            return 0
        st[key] = min(st[key] - period, period)          # one step per scan, no backlog burst
        return int(self.step(session, why='tick') is not None)

    def step(self, session, why='tick'):
        """One tick step on the worn pet (pet F4 [I], rates from config): an awake pet below 2 %
        falls asleep (set_awake: 0xAD 0); else it gets hungrier and gains EXP (0xAF on a level,
        then 0xAE to the owner), an asleep one recovers (0xAE). Returns the record after the
        step (a copy), or None when no pet is worn. Also `!pet tick`."""
        char = self._char(session)
        if not self.supported or char is None:
            return None
        uid = P.session_uid(session) or 0
        hunger, exp_step = int(self.cfg('PET_HUNGER_STEP')), int(self.cfg('PET_EXP_STEP'))
        regen = int(self.cfg('PET_SLEEP_REGEN_STEP'))
        with self.lock(session):
            with self.server.store.lock:
                rec = CASH.equipped_pet(char)
                if rec is None:
                    return None
                pet = CASH.normalize_pet(rec.get('pet'))
                before = dict(pet)
                fall_asleep = pet['awake'] and pet['gauge'] < SLEEP_BELOW
                if not fall_asleep:
                    if pet['awake']:
                        pet['gauge'] = max(0, pet['gauge'] - hunger)
                        pet['exp'] = min(CASH.PET_EXP_MAX, pet['exp'] + exp_step)
                        if CASH.pet_level_for(pet['exp']) > pet['level']:
                            pet['level'] = min(CASH.PET_LEVEL_MAX, pet['level'] + 1)
                    else:
                        pet['gauge'] = min(CASH.PET_GAUGE_MAX, pet['gauge'] + regen)
                    rec['pet'] = pet
                worn = dict(rec, pet=dict(pet))
            if fall_asleep:
                return self.set_awake(session, False, what=f'{why} (gauge {before["gauge"]}% < {SLEEP_BELOW})')
            if pet == before:
                return worn                                  # rates 0, or capped: nothing to send
            self.server.store.mark_dirty(f'pet {why} {session.get("char_name")}')
            levelled = pet['level'] != before['level']
            viewers = 0
            if session.get('in_world'):
                if levelled:
                    presence.touch(session)                  # the records carry pet_level
                    viewers = self._send_level(session, uid, worn)
                if session.get('pet_bound') == worn['serial']:
                    self._push(session, '0xAE', ae_fields(pet['gauge'], pet['exp']))
        log.info(f'[PET] {self._who(session)} {why}: {pet["name"]!r} '
                 f'{"awake" if pet["awake"] else "asleep"} gauge {before["gauge"]}->{pet["gauge"]}% '
                 f'exp {before["exp"]}->{pet["exp"]}'
                 + (f', level {before["level"]}->{pet["level"]} (0xAF to the owner + {viewers} viewer(s))'
                    if levelled else '') + '; 0xAE')
        return worn

    # ----------------------------------------------------- pet-s3: emotes ---
    def emote(self, session, action, what='C2S 0x86'):
        """C2S 0x86 PetEmote {u8 action} (pet F8) -> S2C 0xB2 {uid, action} to the owner and to
        every client holding the owner. Returns how many 0xB2 went out (0 = refused, no reply:
        the client takes no lock and plays nothing on its own)."""
        try:
            return self._emote(session, int(action) & 0xFF, what)
        except PetRefused as e:
            log.info(f'[PET] {self._who(session)} {what} emote {int(action) & 0xFF:#x} refused: {e}')
            self._tell(session, e.tell)
            return 0

    def _emote(self, session, action, what):
        char = self._in_world(session)
        need = EMOTE_LEVEL.get(action)
        if need is None:
            raise PetRefused(f'{action:#x} is no pet action (0x12 / 0x14 / 0x15; spec_2009 0xB2: no other '
                             f'motion is verified)')
        uid = P.session_uid(session) or 0
        with self.lock(session):
            with self.server.store.lock:
                rec = CASH.equipped_pet(char)
                pet = CASH.normalize_pet(rec.get('pet')) if rec is not None else None
            if pet is None:
                raise PetRefused('no pet worn')
            if not pet['awake']:
                raise PetRefused('the pet sleeps (the client sends 0x86 only for an awake pet)')
            if pet['level'] < need:
                raise PetRefused(f'level {pet["level"]} < {need} (the client refuses it itself: '
                                 f'"Insufficient pet level.")')
            now = time.monotonic()
            last = session.get('pet_emote_at')
            gap = float(self.cfg('PET_EMOTE_MIN_SECS'))
            # the client's own 700 ms anti-spam measures at ITS send: two emotes 700 ms apart
            # there can arrive closer, so the server allows the network some slack
            if last is not None and now - last < gap - EMOTE_TOLERANCE_SECS:
                raise PetRefused(f'{(now - last) * 1000:.0f} ms after the last one (PET_EMOTE_MIN_SECS {gap} '
                                 f'- {EMOTE_TOLERANCE_SECS} s tolerance)')
            session['pet_emote_at'] = now
            fields = {'uid': uid, 'pet_action': action}
            sent = int(self._push(session, '0xB2', (dict(fields), None)))
            sent += presence.holders_apply(self.server, session,
                                           lambda peer: int(self._push(peer, '0xB2', (dict(fields), None))),
                                           touch_first=False)
        log.info(f'[PET] {self._who(session)} {what}: {pet["name"]!r} action {action:#x} -> 0xB2 to the owner and '
                 f'{max(0, sent - 1)} viewer(s)')
        return sent

    # ------------------------------------------------ pet-s4: feed / bell ---
    def feed(self, session, food_id, what='C2S 0x85'):
        """C2S 0x85 PetFeed {u16 food} (pet F5): the cp-2 exe's auto-feed (on an awake 0xAE at
        <= 10 %) and bag feeding. Returns the record after the feed (a copy), or None when
        refused - no reply (no client lock; the auto-feed asks again on the next low 0xAE)."""
        raw = int(food_id) & 0xFFFF
        try:
            return self._feed_request(session, raw, what)
        except PetRefused as e:
            log.info(f'[PET] {self._who(session)} {what} feed {raw} refused: {e}')
            self._tell(session, e.tell)
            return None

    def _feed_request(self, session, raw, what):
        char = self._in_world(session)
        ids_ = self.client_item_ids
        # pet F5 step 1: an owned record WITH THAT ITEM ID - the EN food 4286..4289 as sent. The
        # stock exe's auto-feed asks for KR 4282..4285, which are EN non-food rows (the Cruiser
        # Sword, the two billboards, the Pet Bell: ADDENDUM X8): it found one of those in its
        # owned list (in practice the Premium Guild Billboard record 4283), not a food - so it is
        # dropped instead of spending the player's Pet Food (that exe feeds through C2S 0x48).
        food = raw
        if food not in CASH.PET_FOOD:
            kr = EC.en_item_from_exe(raw, 'kr')
            misroute = (f'; the stock exe\'s KR food id {raw} = EN {EC.item_name(raw)} ({raw}), not food '
                        f'(ADDENDUM X8 misroute)' if kr in CASH.PET_FOOD else '')
            raise PetRefused(f'{raw} is no EN pet food id {sorted(CASH.PET_FOOD)} (CLIENT_ITEM_IDS {ids_!r})'
                             + misroute)
        record = self.server.cash.find(char, food)
        if record is None:
            raise PetRefused(f'no {EC.item_name(food)} ({food}) record with a count (FUN_00464c10)')
        return self._fed(session, char, record['serial'], consume=True, what=f'{what} {EC.item_name(food)}')

    def _fed(self, session, char, serial, consume, what):
        """Pet F5 steps 2-4 on the worn pet: (consume one unit of `serial` when the server has
        not yet), gauge = max(gauge, PET_FOOD_GAUGE), awake; S2C 0xB1 {gauge, serial} to the
        bound owner - `serial` is the record its client consumes (0 when the 0x72 did already) -
        then, for a sleeping pet, the wake's 0xAD. Returns the record (a copy)."""
        uid = P.session_uid(session) or 0
        resync = False
        with self.lock(session):
            with self.server.store.lock:
                rec = CASH.equipped_pet(char)
                if rec is None:
                    raise PetRefused('no pet worn')
                pet = CASH.normalize_pet(rec.get('pet'))
                if consume:
                    if pet['gauge'] >= CASH.PET_GAUGE_MAX:
                        raise PetRefused('the gauge is 100 % (the client refuses a manual feed then)')
                    if self.server.cash.consume(char, serial, 1, what='pet feed') is None:
                        raise PetRefused(f'food record {serial:#x} is gone')
                woke = not pet['awake']
                before = pet['gauge']
                pet.update(gauge=max(pet['gauge'], int(self.cfg('PET_FOOD_GAUGE'))), awake=True)
                rec['pet'] = pet
                worn = dict(rec, pet=dict(pet))
            self.server.store.mark_dirty(f'pet feed {session.get("char_name")}')
            if woke:
                self._restart_tick(session)
            viewers = 0
            if session.get('in_world'):
                if session.get('pet_bound') == worn['serial']:
                    self._push(session, '0xB1', b1_fields(pet['gauge'], serial if consume else 0))
                else:
                    # no local pet_info took the 0xB1 (nothing consumed there): re-list instead
                    resync = consume
                if woke:
                    viewers = self._announce_awake(session, uid, worn, True)
        if resync:
            self._owner_resync(session, 'pet feed')
        log.info(f'[PET] {self._who(session)} {what}: {pet["name"]!r} gauge {before}->{pet["gauge"]}%'
                 + (f', wakes (0xAD to the owner and {viewers} viewer(s))' if woke else '')
                 + f'; 0xB1 serial {serial if consume else 0:#x}')
        return worn

    def use_bell(self, sock, session, item, what='C2S 0x15'):
        """C2S 0x15 {Pet Bell} (pet F7): wake the worn pet - asleep, gauge >= 10 - for one bell:
        S2C 0x25 {bell} (the client removes it, plays the use effect), then the wake's 0xAD.
        Returns the woken record (a copy) or None when refused (no 0x25: the bell stays and the
        client, which takes no lock, keeps no cooldown)."""
        item = int(item) & 0xFFFF
        try:
            return self._bell(sock, session, item, what)
        except PetRefused as e:
            log.info(f'[PET] {self._who(session)} {what} Pet Bell {item} refused: {e}')
            self._tell(session, e.tell)
            return None

    def _bell(self, sock, session, item, what):
        char = self._in_world(session)
        if item != CASH.PET_BELL:
            raise PetRefused(f'{item} is not the Pet Bell ({CASH.PET_BELL})')
        if session.get('dead'):
            raise PetRefused('dead (the client takes no input)')
        server = self.server
        with self.lock(session):
            with server.store.lock:
                rec = CASH.equipped_pet(char)
                pet = CASH.normalize_pet(rec.get('pet')) if rec is not None else None
            if pet is None:
                raise PetRefused('no pet worn (the cp-2 client sends nothing without a pet_info)')
            if pet['awake']:
                raise PetRefused('the pet is awake (the cp-2 client sends nothing then)')
            if pet['gauge'] < BELL_MIN_GAUGE:
                raise PetRefused(f'gauge {pet["gauge"]}% < {BELL_MIN_GAUGE}', BELL_TEXT)
            # The bag change under the owner's combat lock like every C2S 0x15 use (no store
            # lock held: combat_lock comes before it on the GameServer ladder).
            with server._combat_lock(session):
                bag = invmod.Inventory(char)
                if not bag.has(item, 1):
                    server._send_phantom_remove(sock, session, item, why='Pet Bell use the bag lacks')
                    raise PetRefused('no Pet Bell in the bag', "You don't have that item.")
                escrow = server._escrow_refusal(session, item, 1)
                if escrow is not None:
                    raise PetRefused(escrow[0], escrow[1])
                bag.remove(item, 1)
                server._stamp_use_cooldown(session, item)
                server.store.mark_dirty(f'use {item}')
                left = bag.count(item)
                P.send(server, sock, session, '0x25', {'item_or_skill_id': item})
            worn = self.set_awake(session, True, what=f'{what} Pet Bell ({left} left)')
        return worn

    # --------------------------------------------------------- pet-s5: gear ---
    @staticmethod
    def _gear_record(char, item, worn):
        """The pet gear record of `item` that is (worn=True) / is not (False) equipped, or None."""
        return next((r for r in CASH.ensure(char) if r['item_id'] == item and not CASH.is_pet_record(r)
                     and bool(r.get('equipped')) == worn), None)

    def _adopt_gear(self, char, item):
        """A record for a worn pet gear grid entry that has none (store data from before
        pet-s5, a GM edit): minted so the 0x6F lists the item where the client puts it. Caller
        holds the store lock."""
        d = CASH.cash_def(item)
        rec = CASH.new_record(self.server.cash.next_serial(), d, CASH.ORIGIN_EVENT, equipped=True)
        CASH.ensure(char).append(rec)
        log.warning(f'[PET] {char.get("name")!r}: worn pet gear {item} had no cash record - minted '
                    f'{rec["serial"]:#x}')
        return rec

    def equip_gear(self, sock, session, item, stones=(), extra=0, what='C2S 0x0F'):
        """C2S 0x0F EquipItem of pet gear (pet F10) -> S2C 0x1D. Returns the grid slot, or None
        when refused (no reply: 0x0F takes no client lock)."""
        item = int(item) & 0xFFFF
        try:
            return self._equip_gear(sock, session, item, list(stones), int(extra), what)
        except PetRefused as e:
            log.info(f'[PET] {self._who(session)} {what} pet gear {item} refused: {e}')
            self._tell(session, e.tell)
            return None

    def _equip_gear(self, sock, session, item, stones, extra, what):
        char = self._in_world(session)
        catalog = EC.items()
        d = catalog.get(item)
        if not invmod.is_pet_gear(item, catalog):
            raise PetRefused(f'{item} is no pet gear (Type 1 cash, Kind 15 / 16)')
        why = self.server._wearer_gates(session, char, d)
        if why is not None:
            # FUN_0044fe20's gender / level / job gates run before the pet ones: never by hand
            raise PetRefused(f'wearer gate: {why}')
        uid = P.session_uid(session) or 0
        words = invmod.block_from_wire(stones, extra)
        gender = self.server._record_gender(session, char)
        stale = False
        with self.lock(session):
            with self.server.store.lock:
                pet_rec = CASH.equipped_pet(char)
                if pet_rec is None or not CASH.normalize_pet(pet_rec.get('pet'))['awake']:
                    raise PetRefused('no awake pet worn (FUN_0044fe20: grid 15 / +0x127C)', REGISTER_TEXT)
                if not gear_species_ok(item, pet_rec['item_id'], catalog):
                    raise PetRefused(f'CardNpc {d.card_npc} is not the template of '
                                     f'{EC.item_name(pet_rec["item_id"])} ({pet_rec["item_id"]})', NOT_YOURS_TEXT)
                rec = self._gear_record(char, item, worn=False)
                if rec is None:
                    stale = True
                else:
                    bag = invmod.Inventory(char, catalog)
                    slot, displaced = bag.equip(item, words)
                    if slot is None:
                        raise PetRefused(f'Kind {d.kind} has no grid slot (FUN_00427af0)')
                    if displaced is not None:
                        # the 0x1D puts it back in the client's bag and its record back in the
                        # owned list (FUN_00462c00: a cash item)
                        old = (self._gear_record(char, displaced['id'], worn=True)
                               or self._adopt_gear(char, displaced['id']))
                        old['equipped'] = False
                    rec['equipped'] = True
                    bag.recompose(item, d.spr_num, gender)
            if not stale:
                self.server.store.mark_dirty(f'pet gear on {session.get("char_name")}')
                P.send(self.server, sock, session, '0x1D', invmod.item_fields(
                    '0x1D', item, uid=uid, block=invmod.echo_block_fields('0x1D', stones, extra)))
                seen = presence.to_holders(self.server, session, '0x1D',
                                           invmod.item_fields('0x1D', item, words=words, uid=uid), 'PET')
        if stale:
            self._owner_resync(session, 'pet gear')        # the 0x6F re-files the client's bag
            raise PetRefused('no bagged record of it: the client showed a stale bag entry (0x6F re-sent)')
        log.info(f'[PET] {self._who(session)} {what}: {EC.item_name(item)} ({item}) on '
                 f'{EC.item_name(pet_rec["item_id"])}, grid {slot}'
                 + (f' in place of {displaced["id"]}' if displaced is not None else '')
                 + f'; look_ext {char.get("look_ext")}; 0x1D (+{seen} viewer(s))')
        return slot

    def unequip_gear(self, sock, session, item, stones=(), extra=0, what='C2S 0x11'):
        """C2S 0x11 UnequipItem of pet gear -> S2C 0x1E (the record back in the bag). Returns
        the grid slot, or None when refused (no reply)."""
        item = int(item) & 0xFFFF
        try:
            return self._unequip_gear(sock, session, item, list(stones), int(extra), what)
        except PetRefused as e:
            log.info(f'[PET] {self._who(session)} {what} pet gear {item} off refused: {e}')
            self._tell(session, e.tell)
            return None

    def _unequip_gear(self, sock, session, item, stones, extra, what):
        char = self._in_world(session)
        catalog = EC.items()
        if not invmod.is_pet_gear(item, catalog):
            raise PetRefused(f'{item} is no pet gear')
        uid = P.session_uid(session) or 0
        words = invmod.block_from_wire(stones, extra)
        gender = self.server._record_gender(session, char)
        with self.lock(session):
            with self.server.store.lock:
                bag = invmod.Inventory(char, catalog)
                slot, entry = bag.worn(item, words)
                if entry is None:
                    raise PetRefused(f'block {words} is not worn (a repeated double-click)')
                if bag.free_slots('equip') < 1:
                    # the client refuses to send then ("You have no more slots for the item.")
                    raise PetRefused('the equipment tab is full')
                rec = self._gear_record(char, item, worn=True) or self._adopt_gear(char, item)
                bag.unequip_item(item, words)
                rec['equipped'] = False
                bag.recompose(item, 0, gender)
            self.server.store.mark_dirty(f'pet gear off {session.get("char_name")}')
            P.send(self.server, sock, session, '0x1E', invmod.item_fields(
                '0x1E', item, uid=uid, block=invmod.echo_block_fields('0x1E', stones, extra)))
            seen = presence.to_holders(self.server, session, '0x1E',
                                       invmod.item_fields('0x1E', item, words=entry['w'], uid=uid), 'PET')
        log.info(f'[PET] {self._who(session)} {what}: {EC.item_name(item)} ({item}) off, grid {slot}; 0x1E '
                 f'(+{seen} viewer(s)), back in the bag')
        return slot

    def give_gear(self, session, item, count=1, what='!pet gear'):
        """GM: `count` records of pet gear `item` (cash.CashInventory._grant_gear, one per piece)
        and one 0x6F. Returns the records."""
        char = self._need_char(session)
        out = []
        try:
            for _ in range(max(1, int(count))):
                out.append(self.server.cash.grant(char, item, origin=CASH.ORIGIN_EVENT, what=what))
        except CASH.CashError as e:
            if not out:
                raise gm.DevCommandError(str(e)) from None
            self.server._gm_reply(session, f'Only {len(out)} given: {e}')
        if session.get('in_world'):
            self._owner_resync(session, what)
        return out

    # ---------------------------------------------------------- C6 (0x48) ---
    def install(self, cashuse):
        """Register the pet items with the generic C2S 0x48 dispatcher (CashUse.gates /
        effects). Harmless on a 2008 server: its item table has no such ids, so the dispatcher
        refuses them before any gate runs."""
        for item in CASH.PET_FOOD:
            cashuse.gates[item] = self.food_gate
            cashuse.effects[item] = self.fed
        cashuse.gates[CASH.PET_NAME_TICKET] = self.ticket_gate
        cashuse.effects[CASH.PET_NAME_TICKET] = self.ticket_used
        return self

    @staticmethod
    def food_gate(server, session, d, record):
        """Why this food use is refused (the food kept), or None. Caller holds store.lock."""
        char = server._session_char(session) if session.get('char_name') else None
        pets = getattr(server, 'pets', None)
        if pets is not None and pets.client_item_ids == 'en' and not session.get('pet_0x48_hint'):
            session['pet_0x48_hint'] = True
            log.warning(f'[PET] {session.get("char_name")!r}: C2S 0x48 with pet food {d.id} under CLIENT_ITEM_IDS '
                        f"'en' - a cp-2 exe feeds through C2S 0x85; this exe looks like the stock one "
                        f"(CLIENT_ITEM_IDS 'kr'?)")
        worn = CASH.equipped_pet(char)
        if worn is None:
            return f'{d.name or d.id}: no pet worn to feed (the food is kept)'
        if CASH.normalize_pet(worn.get('pet'))['gauge'] >= CASH.PET_GAUGE_MAX:
            # as the 0x85 path and the client's own manual feed: a full pet eats nothing
            return f'{d.name or d.id}: the gauge is 100 % (the food is kept)'
        return None

    @staticmethod
    def fed(server, session, d, record):
        """After cashuse's consume + 0x72 (the stock exe's feed, pet F5 step 5): the model (gauge
        up to PET_FOOD_GAUGE, awake), S2C 0xB1 {gauge, serial 0} - the redraw and the "I feel
        new power." bubble, consuming nothing a second time - and a sleeping pet's wake (0xAD to
        the owner and the viewers). Returns the pet fields after it, None without a pet."""
        char = server._session_char(session) if session.get('char_name') else None
        pets = getattr(server, 'pets', None)
        if pets is None or char is None:
            return None
        try:
            worn = pets._fed(session, char, record['serial'], consume=False,
                             what=f'C2S 0x48 {d.name or d.id} (serial {record["serial"]:#x}, 0x72 consumed it)')
        except PetRefused as e:
            # the gate checked a worn pet under the same store lock hold as the consume; one
            # taken off since is the only way here - the food is spent like any 0x72 use
            log.warning(f'[PET] {session.get("char_name")!r} C2S 0x48 {d.name or d.id}: used, but not fed ({e})')
            return None
        return worn['pet']

    @staticmethod
    def ticket_gate(server, session, d, record):
        return (f'{d.name or d.id}: the pet name ticket renames through window 0x4CB (C2S 0x4D, a cp-2 '
                f'patched exe); used from the bag it would be spent for nothing (kept)')

    @staticmethod
    def ticket_used(server, session, d, record):     # never reached while ticket_gate refuses
        log.warning(f'[PET] {session.get("char_name")!r}: pet name ticket {record.get("serial")} used '
                    f'without a rename')

    # ------------------------------------------------- pet-s6: rename (F9, C5) ---
    def rename(self, sock, session, rec, what='C2S 0x4D'):
        """2009 C2S 0x4D PetRename {u32 pet_serial, str[13] new_name} (pet F9; module docstring
        "Rename"): a MUST-REPLY - S2C 0xC0 to the owner (+ 0xB0 to the viewers holding its
        pet_info), or the refusal S2C 0x73 {0} (C5). Returns the renamed record (a copy), or
        None when refused."""
        serial = int(rec.get('pet_serial', 0) or 0) & 0xFFFFFFFF
        new = names.clean(rec.get('new_name', b''))
        try:
            return self._rename(session, serial, new, what)
        except PetRefused as e:
            # after the pet lock: cashuse's one 0x73 builder closes box 0x16 (nothing consumed)
            self.server.cashuse.name_change_result(sock, session, False, why=f'pet {serial:#x} -> {new!r}: {e}',
                                                   what='pet rename')
            return None

    def _rename(self, session, serial, new, what):
        char = self._in_world(session)
        bad = pet_name_refusal(new)
        if bad is not None:
            raise PetRefused(f'invalid name ({bad}; FUN_0043e1b0 rules)')
        server = self.server
        uid = P.session_uid(session) or 0
        with self.lock(session):
            with server.store.lock:
                rec = CASH.equipped_pet(char)
                if rec is None:
                    raise PetRefused('no pet worn (the client sends the worn record\'s serial)')
                if rec['serial'] != serial:
                    raise PetRefused(f'not the worn pet record ({rec["serial"]:#x} is worn)')
                pet = CASH.normalize_pet(rec.get('pet'))
                if not pet['awake']:
                    raise PetRefused('the pet sleeps: no sprite, so 0xC0 / 0xB0 would write through NULL '
                                     '(H2, 0x46BD38 / 0x45D49F)')
                if session.get('pet_bound') != serial:
                    raise PetRefused(f'the client has not bound the record (pet_bound {session.get("pet_bound")}): '
                                     f'its 0xC0 would read nothing')
                if not pet_drawn(rec['item_id']):
                    raise PetRefused(f'hii CardNpc 0 for {rec["item_id"]}: no client builds its sprite (B1 / H2; '
                                     f'the cp-1 data patch sets it)')
                if pet['name'] == new:
                    raise PetRefused('the same name')
                ticket = server.cash.find(char, CASH.PET_NAME_TICKET)
                if ticket is None:
                    raise PetRefused(f'no usable {EC.item_name(CASH.PET_NAME_TICKET)} ({CASH.PET_NAME_TICKET}) record')
                left = server.cash.consume(char, ticket['serial'], 1, what='pet rename')
                old = pet['name']
                pet['name'] = CASH.clip_pet_name(new)
                rec['pet'] = pet
                worn = dict(rec, pet=dict(pet))
            server.store.mark_dirty(f'pet rename {session.get("char_name")}')
            presence.touch(session)                      # the records carry pet_name
            self._push(session, '0xC0', c0_fields(ticket['serial'], worn['pet']['name']))
            viewers = self._send_name(session, uid, worn)
        log.info(f'[PET] {self._who(session)} {what}: {old!r} -> {worn["pet"]["name"]!r} ({EC.item_name(rec["item_id"])}, '
                 f'serial {serial:#x}); ticket {ticket["serial"]:#x} ({left} left); 0xC0 ("{RENAMED_TEXT}") + 0xB0 '
                 f'to {viewers} viewer(s)')
        return worn

    def _send_name(self, session, uid, worn):
        """S2C 0xB0 {uid, name} to every viewer holding the owner's pet_info. The caller checked
        H2 (an awake pet with a sprite template: each such viewer holds its sprite - a wake's
        0xAD(1) reaches every holder) and holds the pet lock. Returns how many got it."""
        fields, assume = b0_fields(uid, worn['pet']['name'])

        def one(peer):
            return int(cview.has_pet_info(peer, uid) and self._push(peer, '0xB0', (dict(fields), assume)))
        return presence.holders_apply(self.server, session, one, touch_first=False)

    # ------------------------------------------------------ C4: owner rename ---
    def owner_renamed(self, session, old, new):
        """world.ON_RENAME for a renamed character (ROADMAP_2009_ADDENDUM C4; module docstring
        "The owner's own rename"): no pet packet - nothing of a pet is keyed by the owner's name
        and the 0x74 left the sprite / pet_info alone. It checks that the worn record, the
        owner's pet_bound and the viewers' pet_info mirror still line up, and logs it. Returns
        {'worn': serial | None, 'bound': bool, 'holders': [names]}."""
        if not self.supported:
            return None
        char = self._char(session)
        with self.server.store.lock:
            worn = CASH.equipped_pet(char)
            pets = [r['serial'] for r in CASH.ensure(char) if CASH.is_pet_record(r)] if char is not None else []
        uid = P.session_uid(session)
        holders = [p.get('char_name') for p in self.server.world.peers(session) if cview.has_pet_info(p, uid)]
        out = {'worn': worn['serial'] if worn is not None else None,
               'bound': worn is not None and session.get('pet_bound') == worn['serial'], 'holders': holders}
        if worn is not None and session.get('in_world') and not out['bound']:
            # a worn pet the owner's client has not bound: its next map load's 0x6F binds it
            log.warning(f'[PET] {old!r} -> {new!r}: worn pet {worn["serial"]:#x} not bound on the client '
                        f'(pet_bound {session.get("pet_bound")})')
        if pets:
            log.info(f'[PET] owner {old!r} is now {new!r}: pet record(s) {[hex(s) for s in pets]} stay the '
                     f'character\'s (worn {out["worn"] and hex(out["worn"])}, bound {out["bound"]}); pet_info held by '
                     f'{holders or "nobody"} (by uid {uid}); no pet packet (the tag is the pet\'s own name)')
        return out

    # ------------------------------------------------------------ GM `!pet` ---
    def command(self, session, args):
        """`!pet ...` (GameServer._dev_pet; the usage line is the gm.register row below)."""
        if not self.supported:
            raise gm.DevCommandError('the 2008 client has no pets (CLIENT_BUILD 2009 only)')
        parts = args.split()
        sub = parts[0].lower() if parts else ''
        if not sub:
            for line in self.describe(session):
                self.server._gm_reply(session, line)
            return
        if sub == 'give':
            return self._cmd_give(session, parts[1:])
        if sub in ('wear', 'equip', 'on'):
            return self._cmd_wear(session, parts[1:])
        if sub in ('off', 'unequip'):
            char = self._need_char(session)
            pet = CASH.equipped_pet(char)
            if pet is None:
                raise gm.DevCommandError('no pet worn')
            if self.unequip(session, pet['item_id'], what='!pet off') is None:
                raise gm.DevCommandError('refused (see the line above / the log)')
            self.server._gm_reply(session, f'Took off {pet["pet"].get("name")!r}.')
            return
        if sub == 'set':
            return self._cmd_set(session, parts[1:])
        if sub == 'bell':
            n = gm.parse_int(parts[1], 'count', lo=1, hi=99) if len(parts) > 1 else 1
            # cp-3: the Pet Bell is a plain Type-0 bag item (no cash record): the !give path
            self.server._dev_give(session, f'{CASH.PET_BELL} {n}')
            return
        if sub == 'ticket':
            # pet-s6 live check T15: "Pet name making" records (EN 4322, counted) into the cash bag
            n = gm.parse_int(parts[1], 'count', lo=1, hi=CASH.STACK_MAX) if len(parts) > 1 else None
            rec = self.server._dev_cash_grant(session, CASH.PET_NAME_TICKET, n, '!pet ticket')
            self.server._gm_reply(session, f'{EC.item_name(CASH.PET_NAME_TICKET)} ({CASH.PET_NAME_TICKET}) in your '
                                           f'cash bag: serial {rec["serial"]:#x}, x{rec["qty"]}; use it to rename '
                                           f'the worn, awake pet (C2S 0x4D).')
            return
        if sub == 'food':
            n = gm.parse_int(parts[1], 'count', lo=1, hi=CASH.STACK_MAX) if len(parts) > 1 else None
            item = gm.parse_int(parts[2], 'food item', lo=min(CASH.PET_FOOD), hi=max(CASH.PET_FOOD)) \
                if len(parts) > 2 else max(CASH.PET_FOOD)
            rec = self.server._dev_cash_grant(session, item, n, '!pet food')
            self.server._gm_reply(session, f'{EC.item_name(item)} ({item}) in your cash bag: serial '
                                           f'{rec["serial"]:#x}, x{rec["qty"]}.')
            return
        if sub == 'seen':
            for line in self.seen_lines(session):
                self.server._gm_reply(session, line)
            return
        if sub == 'tick':
            return self._cmd_tick(session, parts[1:])
        if sub == 'gear':
            return self._cmd_gear(session, parts[1:])
        raise gm.DevCommandError(f'unknown sub-command {sub!r}')

    def _cmd_tick(self, session, parts):
        """`!pet tick [n]`: n tick steps now (the 60 s / 300 s periods skipped; pet-s3 live
        checks T6 / T7: `!pet set gauge 11`, then `!pet tick` -> 0xAE 10 % -> the auto-feed)."""
        n = gm.parse_int(parts[0], 'steps', lo=1, hi=200) if parts else 1
        self._need_char(session)
        worn, done, stop = None, 0, ''
        for _ in range(n):
            worn = self.step(session, why='!pet tick')
            if worn is None:
                raise gm.DevCommandError('no pet worn')
            done += 1
            p = CASH.normalize_pet(worn.get('pet'))
            if done < n and p['awake'] and p['gauge'] <= AUTO_FEED_AT:
                # each awake 0xAE at <= 10 % makes the cp-2 client auto-feed (H8): one GM
                # command must not use up n foods
                stop = f' (stopped: {p["gauge"]}% <= {AUTO_FEED_AT}%, the client auto-feeds)'
                break
        p = worn['pet']
        self.server._gm_reply(session, f'{p["name"]!r} after {done} step(s): Lv{p["level"]} exp {p["exp"]} '
                                       f'{p["gauge"]}% {"awake" if p["awake"] else "asleep"}.{stop}')

    def _cmd_gear(self, session, parts):
        """`!pet gear <id> [n | wear]`: pet gear records into your bag (S2C 0x6F); wear = then
        the C2S 0x0F path (S2C 0x1D)."""
        if not parts:
            raise gm.DevCommandError('no pet gear id (Picky 4290..4293, Ulie 4295..4298, ...: pet.md 2.1)')
        item = gm.parse_int(parts[0], 'pet gear id', lo=1, hi=0xFFFF)
        if not invmod.is_pet_gear(item):
            raise gm.DevCommandError(f'{item} is no pet gear (hii Type 1 cash, Kind 15 / 16)')
        wear = len(parts) > 1 and parts[1].lower() in ('wear', 'equip', 'on')
        n = 1 if wear or len(parts) < 2 else gm.parse_int(parts[1], 'count', lo=1, hi=10)
        recs = self.give_gear(session, item, n)
        self.server._gm_reply(session, f'{EC.item_name(item)} ({item}) x{len(recs)} in your bag: serial(s) '
                                       f'{", ".join(format(r["serial"], "#x") for r in recs)}.')
        if wear:
            if self.equip_gear(session.get('sock'), session, item, (), 0, what='!pet gear wear') is None:
                raise gm.DevCommandError('given, but not worn (see the line above / the log)')
            self.server._gm_reply(session, f'Wearing {EC.item_name(item)}.')

    def _need_char(self, session):
        char = self._char(session)
        if char is None:
            raise gm.DevCommandError('no character in this session')
        return char

    @staticmethod
    def _item_arg(text):
        word = str(text).lower()
        if word in SPECIES:
            return SPECIES[word]
        return gm.parse_int(word, 'pet item id', lo=1, hi=0xFFFF)

    def _cmd_give(self, session, parts):
        if not parts:
            raise gm.DevCommandError('no pet (an item id or picky / ulie / chikapuka / guriguri)')
        item = self._item_arg(parts[0])
        d = CASH.cash_def(item)
        if d is None or not d.is_pet:
            raise gm.DevCommandError(f'{item} is no pet item (hii Type 6: {sorted(SPECIES.values())})')
        rec = self.server._dev_cash_grant(session, item, None, '!pet give')   # a bound record + 0x6F
        self.server._gm_reply(session, f'{d.name} ({item}) in your bag: serial {rec["serial"]:#x}; double-click it '
                                       f'to wear it (C2S 0x82).')
        if len(parts) > 1 and parts[1].lower() in ('wear', 'equip', 'on'):
            if self.equip(session, item, what='!pet give wear') is None:
                raise gm.DevCommandError('given, but not worn (see the line above / the log)')
            self.server._gm_reply(session, f'Wearing {d.name}.')

    def _cmd_wear(self, session, parts):
        char = self._need_char(session)
        with self.server.store.lock:
            bagged = [r for r in CASH.ensure(char) if CASH.is_pet_record(r) and not r.get('equipped')]
        if parts:
            want = self._item_arg(parts[0])
            bagged = [r for r in bagged if want in (r['item_id'], r['serial'])]
        if not bagged:
            raise gm.DevCommandError('no bagged pet (to wear: !pet give <id>)')
        if self.equip(session, bagged[0]['item_id'], what='!pet wear') is None:
            raise gm.DevCommandError('refused (see the line above / the log)')
        self.server._gm_reply(session, f'Wearing {bagged[0]["pet"].get("name")!r}.')

    def _cmd_set(self, session, parts):
        """`!pet set <field> <value>` on the worn pet (else the first bagged one): the model,
        then the packets that keep every client in step - 0xAD for awake (set_awake), 0xAF for a
        level change (owner + pet_info holders), 0xB0 for a name change of an awake, drawn pet
        (the pet_info holders, pet-s6; asleep they see it at the owner's next appearance, H2) -
        and the owner's 0x6F, which re-binds +0x1628 to the new values (gauge, EXP, name: the
        finalize copies the name into the sprite). No ticket: the GM path, not C2S 0x4D."""
        if len(parts) < 2 or parts[0].lower() not in SET_FIELDS:
            raise gm.DevCommandError(f'!pet set <{"|".join(SET_FIELDS)}> <value>')
        field, text = parts[0].lower(), ' '.join(parts[1:])
        char = self._need_char(session)
        uid = P.session_uid(session) or 0
        if field == 'awake':
            value = gm.parse_int(text, 'awake', lo=0, hi=1)
            worn = self.set_awake(session, bool(value), what='!pet set awake')
            if worn is None:
                raise gm.DevCommandError('no pet worn (awake is the worn pet\'s state)')
            self._owner_resync(session, '!pet set')
            self.server._gm_reply(session, f'{worn["pet"]["name"]!r} {"awake" if value else "asleep"}.')
            return
        with self.lock(session):
            with self.server.store.lock:
                rec = CASH.equipped_pet(char) or next(
                    (r for r in CASH.ensure(char) if CASH.is_pet_record(r)), None)
                if rec is None:
                    raise gm.DevCommandError('no pet (!pet give <id>)')
                pet = CASH.normalize_pet(rec.get('pet'))
                before = dict(pet)
                if field == 'exp':
                    pet['exp'] = gm.parse_int(text, 'exp', lo=0, hi=CASH.PET_EXP_MAX)
                    pet['level'] = max(pet['level'], CASH.pet_level_for(pet['exp']))
                elif field == 'level':
                    pet['level'] = gm.parse_int(text, 'level', lo=1, hi=CASH.PET_LEVEL_MAX)
                    if CASH.pet_level_for(pet['exp']) != pet['level']:
                        pet['exp'] = CASH.PET_EXP_TABLE[pet['level'] - 1]
                elif field == 'gauge':
                    pet['gauge'] = gm.parse_int(text, 'gauge', lo=0, hi=CASH.PET_GAUGE_MAX)
                elif field == 'name':
                    name = CASH.clip_pet_name(text)
                    if not name:
                        raise gm.DevCommandError('an empty name')
                    pet['name'] = name
                rec['pet'] = pet
                worn = dict(rec, pet=dict(pet)) if rec.get('equipped') else None
            self.server.store.mark_dirty(f'!pet set {field} {session.get("char_name")}')
            if worn is not None and session.get('in_world'):
                presence.touch(session)
                if pet['level'] != before['level']:
                    self._send_level(session, uid, worn)
                if pet['name'] != before['name'] and pet['awake'] and pet_drawn(worn['item_id']):
                    self._send_name(session, uid, worn)          # H2: awake and drawn only
        self._owner_resync(session, '!pet set')
        self.server._gm_reply(session, f'{pet["name"]!r}: {field} {before[field]} -> {pet[field]} '
                                       f'(Lv{pet["level"]} exp {pet["exp"]} {pet["gauge"]}%).')

    def _owner_resync(self, session, what):
        if session.get('in_world') and session.get('sock') is not None:
            self.server._send_owned_cash(session['sock'], session, reason=what, force=True)

    def seen_lines(self, session):
        """Which clients on the caller's map hold the caller's pet_info (the H1 mirror)."""
        uid = P.session_uid(session)
        out = []
        for peer in self.server.world.peers(session):
            out.append(f'{peer.get("char_name")}: {"holds" if cview.has_pet_info(peer, uid) else "no"} pet_info '
                       f'of you; it holds {sorted(cview.view(peer).snapshot()["pet_info_seen"])}')
        mine = cview.view(session).snapshot()['pet_info_seen']
        out.append(f'you hold the pet_info of {mine or "nobody"}; pet_bound {session.get("pet_bound")}')
        return out

    def describe(self, session):
        """Lines for `!pet`: the worn pet, the bagged ones, the look / gear words, the switch."""
        char = self._need_char(session)
        with self.server.store.lock:
            pets = [dict(r, pet=CASH.normalize_pet(r.get('pet'))) for r in CASH.ensure(char) if CASH.is_pet_record(r)]
            ext = list(char.get('look_ext') or [])
        lines = [f'CLIENT_ITEM_IDS {self.client_item_ids!r}; look_ext {ext}; gear 23/24 '
                 f'{[R.grid_item(session, char, s) for s in GEAR_SLOTS]}; pet_bound {session.get("pet_bound")}']
        for r in pets:
            p = r['pet']
            lines.append(f'{r["serial"]:#x} {EC.item_name(r["item_id"])} ({r["item_id"]}) {p["name"]!r} Lv{p["level"]} '
                         f'exp {p["exp"]} {p["gauge"]}% {"awake" if p["awake"] else "asleep"}'
                         f'{" WORN" if r.get("equipped") else ""}')
        if not pets:
            lines.append('no pets (!pet give picky [wear])')
        with self.server.store.lock:
            gear = [dict(r) for r in CASH.ensure(char)
                    if not CASH.is_pet_record(r) and invmod.is_pet_gear(r['item_id'])]
        if gear:
            lines.append('gear: ' + ', '.join(f'{EC.item_name(r["item_id"])} ({r["item_id"]})'
                                              f'{" WORN" if r.get("equipped") else ""}' for r in gear))
        st = session.get(TICK_KEY) or {}
        lines.append(f'tick: awake {st.get("awake", 0.0):.0f}/{self.cfg("PET_HUNGER_SECS"):g} s, asleep '
                     f'{st.get("asleep", 0.0):.0f}/{self.cfg("PET_SLEEP_REGEN_SECS"):g} s '
                     f'({"counting" if st.get("last") is not None else "paused"})')
        return lines


# P8 compatibility: the stub's name (GameServer.pets was a PetStub).
PetStub = Pets


def name_change_refusal():
    """The 0x73 fields a refused 0x4D gets (registry BUILD_MUST_REPLY's backstop is the same)."""
    return CU.name_change_fields(False)


def register(hooks, pets):
    """The pets' world hook (2009 only; GameServer.__init__): an owner's rename (ON_RENAME, the
    C4 hook cashuse.CashUse.rename fires after its 0x73 / 0x74) -> Pets.owner_renamed."""
    def on_rename(server, session, old=None, new=None, **_):
        pets.owner_renamed(session, old, new)

    hooks.register(worldmod.ON_RENAME, on_rename)
    return on_rename


# The '!' command (gm.register: no edit of GameServer.DEV_COMMANDS); its handler is the thin
# GameServer._dev_pet, which hands over to server.pets.command.
if 'pet' not in gm.COMMANDS:
    gm.register('pet', gm.DevCommand(
        '_dev_pet', '!pet [give <id|picky|ulie|chikapuka|guriguri> [wear] | wear [id] | off | '
                    'set <exp|level|gauge|awake|name> <v> | bell [n] | food [n] [4286-4289] | ticket [n] | '
                    'seen | tick [n] | gear <id> [n|wear]]',
        'the 2009 pets: your pets; give = a bound pet record into your bag (S2C 0x6F; wear: then the '
        'C2S 0x82 path, S2C 0xAB); wear / off = the 0x82 / 0x83 paths; set = the worn pet\'s field (0xAD / '
        '0xAF / 0xB0 to the clients, the owner\'s 0x6F); bell = Pet Bells into your bag (cp-3, a Type-0 item); '
        'food = pet food into your cash bag; ticket = pet name tickets (4322) into your cash bag (rename: '
        'C2S 0x4D -> 0xC0 / 0xB0); seen = who holds your pet_info (the H1 mirror); tick = n '
        'hunger / sleep steps now (0xAE / 0xAF / 0xAD); gear = pet gear records into your bag (wear: then '
        'the C2S 0x0F path, S2C 0x1D)',
        owner='pet-s1..pet-s7 (P15 stages 1-3)', aliases=('pets',)))
