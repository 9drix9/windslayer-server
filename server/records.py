#!/usr/bin/env python3
"""
records.py - the per-player record and the character list, from the store (roadmap P1)
======================================================================================
Three packets carry the same player row and one carries the select screen. Both are built
here, from the store record plus the live session, so no builder invents values again:

    world-player-record (+party-mp-remote-record)  S2C 0x07 / 0x04 / 0x05
    lc-spawn-fields                                the own 0x07 at enter world / map change
    lc-charlist                                    S2C 0x02 LoginResultCharacterList

    import packets as P, records as R
    rec = R.player_record(session, char, account)          # 0x07 / 0x04 field names
    P.send(server, sock, session, '0x07', R.player_list(rec))
    P.send(server, sock, peer, '0x05', R.to_0x05(rec))     # same row, 0x05 field names
    P.send(server, sock, session, '0x02', R.character_list(uid, account))

0x07 and 0x04 share one grammar; 0x05 is the same row with different field names and
without the shop and buff-point blocks, so `to_0x05` renames instead of rebuilding
(world_movement_npc.md 3.4 "keep a key map").

What the values are (login_character.md F5/3.2, world_movement_npc.md 3.4)
--------------------------------------------------------------------------
- `uid` is the ACCOUNT uid (F3/D1). The client wrote it to scene+0x220 from S2C 0x02
  before character select, and the registration gate 0x4221A2 drops an own 0x07 whose
  uid differs.
- `karma` / `manner_points` is the account's manner (the same i32 0x02 sends), not 1.
- `level` is always derived from `char['exp']` with the client's own table
  (progression.level_for_exp); nothing stores a level (S1-06, B1/B3).
- `appearance` is `char['look']` verbatim (+ `look_ext` in 2009): one sprite file number per
  draw layer, which every one of 0x02 / 0x07 / 0x04 / 0x05 copies into the entity as it is -
  the client never derives a look from the equipped ids. It starts as the words the client
  composes after S2C 0x1C, `[0, s1, 0, 0, s10, s5, s6, 0, 0, s9, s10, 0, 0, s10]`, and the
  server then applies the client's own composition routine (inventory.compose, 2009
  FUN_004282c0 / 2008 FUN_00426d50) at every 0x1D / 0x1E / 0x24, so the stored words are
  always the ones on screen: the worn hat, clothes, gloves, shoes and weapon (the old
  builder sent the creation outfit with only a weapon merged into slot 11, so every portal
  and relog put the character back in its creation clothes). B8 / S2-25: the older builder
  put a constant 123 in slot 1 and face/top/bottom/shoes in 2/4/5/6 ("renders unclothed").
- `gender` is `record_gender`: the account flag in 2008, the character's own bool in 2009.
  The receiving client composes every later 0x1D / 0x1E of this player with it, so the
  server composes the stored look with the very same value.
- stats are the character's STR/DEX/INT/SPR (+0xE6..+0xEC), not 0 (B9).
- the motion block is the idle state RegisterLocalPlayer writes: +0x954 = 0, +0x8D9 = 0,
  +0x8CF = 8, +0x904 = 8, +0xE00 = 0, +0x8BD = 2, +0x8B3 = 8, +0x8B4..6 = 0. The old
  builder sent 32/1/0/501 and the four bytes of an IP address at +0x8B3 (S2-27), which is
  a walking, mid-attack entity.
- `cur_hp` / `cur_mp` are the live session values: a record with 0 renders a corpse (B9).
- `rank_icon` is the fame rank the client itself computes (progression.rank_for_fame, table
  0x6F11D8; login_character Q7): 0 = 'Trainee' with the red fist emblem on every new
  character's name tag, as in the retail footage. It used to be 99 ("no emblem"), which
  the name tag skips but the Player Info window (Char. Info on a player of the same map,
  filled from entity +0x9A / 2009 +0x9E) indexes past its name table (lc-player-info).

EN 2009 build (client-2009-login, protocol_spec_2009.json)
---------------------------------------------------------
`client_build='2009'` gives the 2009 field sets:
- 0x02: header + u8 account_flag_152 (0) + u32 session_key; 82-byte records with the
  character's own `gender` bool and 17 appearance words (`look` + `look_ext`); no str[33].
- 0x07 (own record at enter world): gm_or_guild_id instead of gm_level (the GM-or-guild block,
  arch09-roster-record below), 17 appearance words, 15 equip slots, 10 cash slots of (id, attr0),
  the motion block under its 2009 names (same values; spec_2009 0x07 order puts pos_x/pos_y
  after the skill list) and the per-receiver pet block (pet_block: has_pet = a pet worn AND
  awake, + its level and name - P15 pet-s1; absent on the receiver's own row:
  packets.DEFAULT_ASSUME_2009; presence.py records in the receiver's clientview mirror which
  owners' pet_info each client got).
- client-2009-world: the equip grid keeps its 25 positions; 2009 sends slots 0..14 with
  their option blocks and slots 15..24 as (id, attr0) rows, slot 15 being the pet slot:
  the worn pet record's item (pet_slot_2009, ROADMAP_2009_ADDENDUM C2), the same record every
  0x6F carries first. 0x04 = player_list of 2009 rows built per receiver (the pet block
  exists only on rows that are not the receiver's own); 0x05 = to_0x05(rec, '2009') with
  the pet block always present.

arch09-roster-record (P14; guild.md 1.2 + 8, ROADMAP_2009_ADDENDUM X10)
----------------------------------------------------------------------
ONE rule for the 2009 GM-or-guild block of every player record, `guild_block`, in three forms:
  FORM_FIELD   0x04 / 0x05 / 0x07  u16 gm_or_guild_id; == 1: bool gm_hidden; > 1: str[17] name,
                                   u16 emblem fg, u16 emblem bg (0x4536A5..0x45371A [V])
  FORM_ROSTER  0x2B / 0x2C / 0x2E  u16 guild_id; > 1: the same 21 bytes - NO gm_hidden [V spec]
                                   (no builder yet: the P9 room model calls it)
  FORM_INFO    0x52                u16 guild_id; > 1: str[17] name (label 4, else "N/A") [V]
The value is the receiver's entity+0x12 for this player, whose renderer FUN_0043c300 draws
"Game Master" for 1 and the guild name + emblem for > 1 - ONE value, so a GM in a guild shows
one tag (guild.md 8.1 [I]: the visible GM tag wins; `gm_tag_visible`). In order:
  1. a visible GM (gm on, not in shadow, the tag not switched off: `!guild gmtag off`, the P14
     test aid that lets a GM create / apply / show his guild - a visible GM's client refuses
     both, "Wind master can't register as guild.", guild.md 1.2)  -> 1, gm_hidden 0;
  2. a GM in shadow (gm_hidden; tag not off), guild or not        -> 1, gm_hidden 1 (FORM_FIELD
     only: the roster / 0x52 forms have no gm_hidden and fall through to 3 / 4);
  3. a guild member                                               -> the guild id + block;
  4. anyone else                                                  -> 0.
Rule 2 before 3 (P14 review): the GM clients that hold a shadowed GM (presence.visible_to:
non-GMs get no record at all) render him by gm_hidden - dimmed, silent; a shadowed GM in a
guild must not come back as a plain member there. His own 0x07 carries it too; his 0x8A sub 3
then sets his own entity+0x12 to the guild (the window), as for any member, and no sub 37.
The guild comes from the caller as a GuildTag (guild.Guilds.tag_of(char)); None = no guild,
which keeps every record byte-identical to the pre-P14 one. 2008 records keep gm_level.
"""
import time
from collections import namedtuple

import buffs as B
import cash as cashmod
import progression
import store as storemod

# Wire sizes of the fixed arrays in the 0x07/0x04/0x05 grammar.
LOOK_SLOTS = 14
EQUIP_SLOTS = 16              # equip grid +0x13C, each with 6 option words at +0x16E
EQUIP_OPTION_WORDS = 6
CASH_EQUIP_SLOTS = 9          # +0x15C = grid slots 16..24 (the costume half)
# Client-side caps: the entity buff array is 21 entries (RegisterLocalPlayer walks
# +0xE84 x 21) and the skill list 30 (combat_skill.md 1).
MAX_BUFFS = 21
MAX_SKILLS = 30
# Buff ids whose 0x07/0x04 entry carries a ground point (grammar gate 0x0A31..0x0A3B).
BUFF_POINT_IDS = range(0x0A31, 0x0A3C)
# rank_icon / 0x52 rank >= 99 = no emblem and no rank name (the client tables have 99 rows).
RANK_ICON_NONE = 99
MAX_CHARACTERS = storemod.MAX_CHARACTERS
# S2C 0x52 PlayerInfoView (lc-player-info): the equipment list is read into u16[25] and
# 25 x 12-byte stack arrays of the handler frame (2008 0x456DC7, 2009 0x45C2B4): never more
# than 25 entries (the entity grid: 16 regular + 9 cash in 2008, 15 regular + the pet slot
# + 9 cash in 2009) and never more than 5 leading option words (packets.LIST_CAPS).
INFO_EQUIP_ENTRIES = 25
INFO_OPTION_WORDS = 5
# spec_2009 0x52 guild_id: 0/1 = no guild, label 4 "N/A" (2008 hard-codes " Not in the
# Guild"); >= 2 carries the guild name (guild_block FORM_INFO).
INFO_NO_GUILD = 0
# S2C 0x04 payload limit: 2038 B / 368 B per record (world_movement_npc.md F1 step 5).
MAX_RECORDS_PER_PACKET = 5
# Always 3 ("no legacy Yahoo id to transfer"), which hides select-screen controls 5/6/7
# (login_character.md F11; lc-id-transfer-stub answers a 0x65/0x66 that arrives anyway).
TRANSFER_STATUS_NONE = 3

# EN 2009 build (spec_2009 0x02 / 0x07): wider arrays.
BUILD_2009 = '2009'
LOOK_SLOTS_2009 = LOOK_SLOTS + storemod.LOOK_EXT_SLOTS      # 17
EQUIP_SLOTS_2009 = 15
CASH_EQUIP_SLOTS_2009 = 10
# spec_2009 0x02 account_flag_152 (scene+0x152): echoed as the last byte of C2S 0x2B; when
# non-zero, returning to the login screen also calls FUN_0049DDF0(6,0). Meaning unknown.
ACCOUNT_FLAG_152 = 0
# 0x07 motion block, 2008 name -> 2009 name (same entity fields; the 2009 entity moved them
# by +0x8C..+0x9C, spec_2009 0x07 diff_vs_2008). Values are the idle ones (IDLE_MOTION).
MOTION_NAMES_2009 = {
    'action_timer_end_954': 'action_timer_end_9e4',
    'state_flag_8d9': 'state_flag_965',
    'anim_substate_8cf': 'anim_substate_95b',
    'action_state_904': 'action_state_994',
    'action_timer_e00': 'action_timer_e9c',
    'direction_8bd': 'direction_949',
    'vertical_velocity_e50': 'vertical_velocity_eec',
    'input_state_8b3': 'input_state_93f',
    'input_state_8b4': 'input_state_940',
    'input_state_8b5': 'input_state_941',
    'input_state_8b6': 'input_state_942',
    'move_flag_8da': 'move_flag_966',
    'hit_window_8df': 'hit_window_96b',
    'move_flag_8dc': 'move_flag_968',
    'move_flag_8dd': 'move_flag_969',
    'move_flag_8de': 'move_flag_96a',
    'timer_d94': 'timer_e24',
    'combo_stage_8ec': 'combo_stage_979',
}

# The idle motion block (RegisterLocalPlayer @0x422120 writes exactly these) in 0x07/0x04
# field names. A remote record may override them from session['motion']; world-presence
# leaves that unset on purpose (spec correction C12: remote records need the idle block,
# and the 0x1B stream that follows the spawn carries the mover's live input, presence.py).
IDLE_MOTION = {
    'action_timer_end_954': 0,
    'state_flag_8d9': 0,
    'anim_substate_8cf': 8,
    'action_state_904': 8,
    'action_timer_e00': 0,
    'direction_8bd': 2,
    'vertical_velocity_e50': 0,
    'input_state_8b3': 8,
    'input_state_8b4': 0,
    'input_state_8b5': 0,
    'input_state_8b6': 0,
    'move_flag_8da': 0,
    'hit_window_8df': 0,
    'move_flag_8dc': 0,
    'move_flag_8dd': 0,
    'move_flag_8de': 0,
    'timer_d94': 0,
    'combo_stage_8ec': 0,
}

# 0x07/0x04 field name -> 0x05 field name (the same entity offsets under other names).
KEY_MAP_05 = {
    'karma': 'manner_points',
    'room_id': 'room_no',
    'gm_level': 'gm_flag',
    'job': 'job1',
    'rank_icon': 'rank_emblem',
    'action_timer_end_954': 'motion_unk_954',
    'state_flag_8d9': 'motion_flag_8d9',
    'anim_substate_8cf': 'attack_dir_8cf',
    'action_state_904': 'motion_id',
    'action_timer_e00': 'motion_timer',
    'direction_8bd': 'facing_dir',
    'vertical_velocity_e50': 'motion_timer2_e50',
    'input_state_8b3': 'input_dir_8b3',
    'input_state_8b4': 'action_state_8b4',
    'input_state_8b5': 'action_sub_8b5',
    'input_state_8b6': 'action_sub_8b6',
    'move_flag_8da': 'state_flag_8da',
    'hit_window_8df': 'state_flag_8df',
    'move_flag_8dc': 'state_flag_8dc',
    'move_flag_8dd': 'state_flag_8dd',
    'move_flag_8de': 'state_flag_8de',
    'timer_d94': 'unk_d94',
}
# 0x05 has no stall block and its buff entries carry no ground point, so these are dropped.
DROPPED_IN_05 = ('shop_open', 'shop_sign_sprite', 'shop_title')


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ------------------------------------------- the GM-or-guild block (arch09-roster-record) ---
# A player's guild as the records carry it (guild.Guilds.tag_of): id >= 2 (0 none, 1 = the GM
# marker, 0xFFFF = load failure - never a guild, guild.md 1.1), name <= 16 bytes, the two
# emblem frames (sprite 0x148 drawn on top = fg, 0x149 first = bg).
GuildTag = namedtuple('GuildTag', 'id name emblem_fg emblem_bg')
GM_TAG = 1
NO_TAG = 0
FORM_FIELD, FORM_ROSTER, FORM_INFO = 'field', 'roster', 'info'


def gm_tag_visible(gm, gm_hidden=False, tag_off=False):
    """True for a GM whose nameplate says "Game Master" (entity+0x12 == 1): the flag on, not in
    shadow (gm_hidden) and the tag not switched off for the session (session['gm_tag_off'],
    `!guild gmtag off`). The one rule of the records and the 0x8A reply's sub 37."""
    return bool(gm) and not gm_hidden and not tag_off


def _guild_ok(guild):
    return guild is not None and 2 <= _int(getattr(guild, 'id', 0)) <= 0xFFFE


def guild_block(char, guild=None, session=None, form=FORM_FIELD):
    """The 2009 GM-or-guild fields of `char`'s record in `form` (module docstring "arch09-
    roster-record"). FORM_FIELD -> {'gm_or_guild_id', ['gm_hidden' | 'guild_name',
    'guild_emblem_fg', 'guild_emblem_bg']}; FORM_ROSTER -> {'guild_id', [the three guild
    fields]}; FORM_INFO -> {'guild_id', ['guild_name']}."""
    char, session = char or {}, session or {}
    gm, hidden, off = char.get('gm'), char.get('gm_hidden'), session.get('gm_tag_off')
    in_guild = _guild_ok(guild)
    if form == FORM_INFO:
        if in_guild:
            return {'guild_id': _int(guild.id), 'guild_name': guild.name}
        return {'guild_id': INFO_NO_GUILD}
    if gm_tag_visible(gm, hidden, off):
        return {'gm_or_guild_id': GM_TAG, 'gm_hidden': 0} if form == FORM_FIELD else {'guild_id': GM_TAG}
    if form == FORM_FIELD and gm and hidden and not off:
        return {'gm_or_guild_id': GM_TAG, 'gm_hidden': 1}          # the shadow wins over the guild
    if in_guild:
        out = {'gm_or_guild_id' if form == FORM_FIELD else 'guild_id': _int(guild.id),
               'guild_name': guild.name, 'guild_emblem_fg': _int(guild.emblem_fg) & 0xFFFF,
               'guild_emblem_bg': _int(guild.emblem_bg) & 0xFFFF}
        return out
    return {'gm_or_guild_id' if form == FORM_FIELD else 'guild_id': NO_TAG}


def tag_value(block):
    """The entity+0x12 value a guild_block (any form) gives its player on the receiver."""
    return _int(block.get('gm_or_guild_id', block.get('guild_id', NO_TAG)))


def level_of(char):
    """The level the client itself shows for this character (exp is the only truth)."""
    return progression.level_for_exp(_int((char or {}).get('exp', 0)))


def rank_of(char):
    """The fame rank (0 'Trainee' .. 98) of this character, exactly as the owner's client
    derives it from the 0x03 fame_points (progression.rank_for_fame): the value its own
    entity gets at +0x9A, so every other client must be sent the same."""
    return progression.rank_for_fame(_int((char or {}).get('fame', 0)))


def appearance(char, gender=0):
    """The 14 look words for 0x02 / 0x07 / 0x04 / 0x05: `char['look']` verbatim - the words
    the client shows, kept composed by inventory.compose at every equip / unequip. Nothing
    is merged in: a worn costume weapon, a Clear Hat or an unequipped shirt all live in the
    stored words exactly as the client composed them."""
    look = list((char or {}).get('look') or [])
    if len(look) != LOOK_SLOTS:
        # A record that never went through the store migration (or a hand-edited file):
        # fall back to the creation defaults of the record's gender instead of sending
        # zeros (= invisible body).
        look = (look + storemod.default_look(gender)[len(look):])[:LOOK_SLOTS]
    return [_int(v) & 0xFFFF for v in look]


def look_ext(char):
    """Appearance words 14..16 of the 2009 array (store `look_ext`, client-2009-login): the
    pet layers (pet / pet_helm / pet_wear), 0 = not drawn."""
    ext = [_int(v) & 0xFFFF for v in ((char or {}).get('look_ext') or [])][:storemod.LOOK_EXT_SLOTS]
    return ext + [0] * (storemod.LOOK_EXT_SLOTS - len(ext))


def appearance_2009(char, gender=0):
    """The 17 look words of the 2009 0x02 / 0x07 records: indices 0..13 keep their 2008
    meaning (S2C 0x1C composes the same 7 creation words, spec_2009 0x02 `appearance`)."""
    return appearance(char, gender) + look_ext(char)


def char_gender(char, account=None):
    """0/1 gender of a character: the 2009 per-character value (spec_2009 0x02 record
    `gender`, set by C2S 0x0E), else the account's 2008 flag it was migrated from."""
    value = (char or {}).get('gender')
    if value is None:
        value = (account or {}).get('gender')
    return 1 if _int(value) else 0


def record_gender(char, account=None, client_build=None):
    """The gender bool this build's records send for a character, which is the value every
    client stores in the entity (2009 +0x11B, 2008 +0x113) and composes that entity's look
    with (inventory.compose: the underwear an unequip leaves). 2009: the character's own
    (char_gender, the 0x02 row / 0x07 `gender`); 2008: the account's flag, as the 2008
    0x07 / 0x04 / 0x05 `gender` carries it. The equip gender gate reads the same value."""
    if client_build == BUILD_2009:
        return char_gender(char, account)
    return 1 if (account or {}).get('gender') else 0


def equip_grid(session, char):
    """The 16 equip slots as [{item_id, [6 option words]}].

    Accepts both the shape the equip handler keeps today (`session['equip_grid']` =
    {slot: item_id}) and the persisted one item_inventory-model-persist writes
    (`char['equipped']` = {slot: {'id': int, 'w': [6 words]}}); the stored block wins.
    Zero option words were the reinforcement duplication bug (S1-16), so whatever the
    model holds is echoed verbatim."""
    live = (session or {}).get('equip_grid') or {}
    stored = (char or {}).get('equipped') or {}
    rows = []
    for slot in range(EQUIP_SLOTS):
        entry = stored.get(slot, stored.get(str(slot), live.get(slot, live.get(str(slot), 0))))
        if isinstance(entry, dict):
            item_id = _int(entry.get('id'))
            words = [_int(w) & 0xFFFF for w in (entry.get('w') or [])][:EQUIP_OPTION_WORDS]
        else:
            item_id, words = _int(entry), []
        words += [0] * (EQUIP_OPTION_WORDS - len(words))
        rows.append({'equip_item_id': item_id & 0xFFFF,
                     f'repeat[{EQUIP_OPTION_WORDS}]': [{'equip_item_attr': w} for w in words]})
    return rows


def _legacy_cash_ids(session, char):
    """The 9 ids of the old `cash_equip` list (+0x15C), 0-padded: the costume ids a record
    carried before they were grid entries (premium_cash-owned-list-sync)."""
    values = (char or {}).get('cash_equip') or (session or {}).get('cash_equip') or []
    values = [_int(v) & 0xFFFF for v in values][:CASH_EQUIP_SLOTS]
    return values + [0] * (CASH_EQUIP_SLOTS - len(values))


def cash_equip(session, char):
    """The 9 cash-equip words of the 2008 record (+0x15C = grid slots 16..24, right after
    the 16 regular ones at +0x13C): the worn costume of each slot, else the legacy
    `cash_equip` list. The receiving client composes this player's later 0x1D / 0x1E from
    ITS copy of these ids (the costume beats the regular item), so a costume the model
    holds must be here - the same ids cash_rows_2009 sends to a 2009 client. An id the 2008
    client has no hii row for (cashmod.foreign_to_client: a character that wore 2009 pet gear
    in grid 23 / 24 on a 2009 server sharing this store) goes out as 0."""
    legacy = _legacy_cash_ids(session, char)
    rows = []
    for i, slot in enumerate(range(EQUIP_SLOTS, EQUIP_SLOTS + CASH_EQUIP_SLOTS)):
        item_id, _words = _grid_entry(session, char, slot)
        item_id = item_id or legacy[i]
        if item_id and cashmod.foreign_to_client(item_id, '2008'):
            item_id = 0
        rows.append({'cash_equip_item_id': item_id})
    return rows


def buff_rows(session, char=None, now=None):
    """Active buffs as 0x07/0x04 entries {buff_skill_id, buff_duration = remaining ms}; ids
    0x0A31..0x0A3B also carry their ground point.

    cs-buffs owns both lists: `session['buffs']` holds the live records (buffs.py, each with
    its monotonic deadline) and `char['buffs']` the persisted ones ({id, remaining_ms}). A
    session that has a buff model is authoritative even when it is EMPTY - falling back to
    the stored list there would resurrect buffs that already expired and were removed with
    S2C 0x43/0x3C. Only a session without one (a bare record) reads the stored list.
    Records whose time is up are never sent: the client would insert a slot it counts
    further below zero and never removes (spec correction C4)."""
    session = session or {}
    source = session['buffs'] if isinstance(session.get('buffs'), list) else (char or {}).get('buffs')
    now = time.monotonic() if now is None else now
    rows = []
    for buff in B.rows(source, now):
        buff_id = buff['id']
        row = {'buff_skill_id': buff_id, 'buff_duration': buff['remaining_ms']}
        if buff_id in BUFF_POINT_IDS:
            row['buff_param_a'] = _int(buff.get('x')) & 0xFFFF
            row['buff_param_b'] = _int(buff.get('y')) & 0xFFFF
        rows.append(row)
    return rows


def skill_rows(char):
    """Learned skill ids (cs-skill-learn owns `char['skills']`)."""
    return [{'skill_id': _int(s) & 0xFFFF} for s in ((char or {}).get('skills') or [])[:MAX_SKILLS]]


def position(session, char, pos=None):
    """(x, y) for the record: the caller's explicit point (arrival point of a transfer),
    else the character's stored position, else the session's last known one."""
    if pos is not None:
        return float(pos[0]), float(pos[1])
    if char and char.get('x') is not None and char.get('y') is not None:
        return float(char['x']), float(char['y'])
    live = (session or {}).get('pos')
    if live:
        return float(live[0]), float(live[1])
    return 0.0, 0.0


def vitals(session, char):
    """(cur_hp, cur_mp) as the client must see them. 0 renders the entity dead, so the
    live session values win and the stored ones are the fallback (B9, S2-35)."""
    session, char = session or {}, char or {}
    hp = session.get('hp') if session.get('hp') is not None else char.get('hp', storemod.DEFAULT_HP)
    mp = session.get('mp') if session.get('mp') is not None else char.get('mp', storemod.DEFAULT_MP)
    return max(0, _int(hp)) & 0xFFFF, max(0, _int(mp)) & 0xFFFF


def player_record(session, char, account=None, *, remote=False, pos=None, uid=None,
                  client_build=None, guild=None):
    """One player row in S2C 0x07 / 0x04 field names (use `to_0x05` for 0x05).

    session: the record owner's session (uid, live hp/mp, equip grid, buffs).
    char:    that player's store record (name, class, exp, look, stats, position).
    account: the owner's store account (uid, gender, manner). Defaults to the session.
    remote:  build it for another client: the motion block comes from session['motion'] when
             a caller set one, else the idle block (world-presence sends idle, C12).
    guild:   the player's GuildTag (2009 only: the GM-or-guild block, guild_block), None = none.
    """
    session = session or {}
    char = char or {}
    account = account or {}
    if uid is None:
        uid = session.get('uid') or account.get('uid') or 0
    motion = dict(IDLE_MOTION)
    if remote:
        relayed = session.get('motion')
        if isinstance(relayed, dict):
            motion.update({k: v for k, v in relayed.items() if k in IDLE_MOTION})
    x, y = position(session, char, pos)
    cur_hp, cur_mp = vitals(session, char)
    buffs, skills = buff_rows(session, char), skill_rows(char)
    # GM presence: gm_level 1 is the only value that makes the client read gm_hidden
    # (handler gate 0x44F093). chat_mail_gm-gm-flag-manner owns char['gm'].
    gm_level = 1 if char.get('gm') else 0
    gender = record_gender(char, account)                   # the 2008 row: the account flag
    rec = {
        'name': char.get('name', ''),
        'uid': _int(uid),
        'karma': _int(account.get('manner')),
        # No room and no cash-shop balloon yet (pvp-room-balloon / premium_cash-presence);
        # a non-zero room_id makes the client read room_type + room_title as well.
        'room_id': 0,
        'gm_level': gm_level,
        'job': _int(char.get('class')) & 0xFF,
        'job2': _int(char.get('job2')) & 0xFF,
        'level': level_of(char) & 0xFF,
        'rank_icon': rank_of(char),
        'gender': gender,
        f'repeat[{LOOK_SLOTS}]': [{'appearance_part': v} for v in appearance(char, gender)],
        'stat_str': _int(char.get('str')) & 0xFFFF,
        'stat_dex': _int(char.get('dex')) & 0xFFFF,
        'stat_int': _int(char.get('int')) & 0xFFFF,
        'stat_tol': _int(char.get('spr')) & 0xFFFF,
        f'repeat[{EQUIP_SLOTS}]': equip_grid(session, char),
        f'repeat[{CASH_EQUIP_SLOTS}]': cash_equip(session, char),
        'buff_count': len(buffs),
        'repeat[buff_count]': buffs,
        'skill_count': len(skills),
        'repeat[skill_count]': skills,
        'pos_x': x,
        'pos_y': y,
        'cur_hp': cur_hp,
        'cur_mp': cur_mp,
        # No open stall (shop_storage-stall-presence adds sprite + title when there is one).
        'shop_open': 0,
        **motion,
    }
    if gm_level == 1:
        rec['gm_hidden'] = 1 if char.get('gm_hidden') else 0
    if client_build == BUILD_2009:
        return _record_2009(rec, session, char, account, guild)
    return rec


def _grid_entry(session, char, slot):
    """(item_id, [6 words]) of one grid slot: the persisted block wins over the live map
    (same precedence as equip_grid)."""
    live = (session or {}).get('equip_grid') or {}
    stored = (char or {}).get('equipped') or {}
    entry = stored.get(slot, stored.get(str(slot), live.get(slot, live.get(str(slot), 0))))
    if isinstance(entry, dict):
        words = [_int(w) & 0xFFFF for w in (entry.get('w') or [])][:EQUIP_OPTION_WORDS]
        return _int(entry.get('id')) & 0xFFFF, words + [0] * (EQUIP_OPTION_WORDS - len(words))
    return _int(entry) & 0xFFFF, [0] * EQUIP_OPTION_WORDS


def grid_item(session, char, slot):
    """The item id of one equip grid slot (the persisted block wins over the live map, as in
    equip_grid). The 2009 pet gear slots 23 (Kind 15 headgear) / 24 (Kind 16 apparel) are read
    through here by the pet equip gate (pets.py, FUN_0044fe20 / FUN_00477000)."""
    return _grid_entry(session, char, slot)[0]


def pet_block(char):
    """The 2009 pet block of a REMOTE player row (0x04 / 0x07 when uid != the receiver, 0x05
    always; asm 0x453D80..0x453E58, pet.md 3 "Pet block", F3): has_pet = the character wears a
    pet AND it is awake (also stored as the receiver's pet_info+0xA = awake), then u8 level and
    str[13] name (NUL-terminated inside 13 bytes, H4: packets cuts every str[N] at N-1). A
    sleeping pet goes out as has_pet 0: its grid slot 15 and look[14] are in the row anyway,
    and the receiver allocates the pet_info on its 0xAD(1) (F6). Its level is at least 1 (a
    worn pet is bound, F13)."""
    pet = cashmod.equipped_pet(char) if isinstance(char, dict) else None
    if pet is None:
        return {'has_pet': 0}
    fields = cashmod.normalize_pet(pet.get('pet'))
    if not fields['awake']:
        return {'has_pet': 0}
    return {'has_pet': 1, 'pet_level': max(1, fields['level']), 'pet_name': fields['name']}


def pet_slot_2009(char):
    """The 2009 grid slot 15 (the PET slot, entity+0x16E): the item id of the character's worn
    pet record (cash.equipped_pet), else 0. The record, not the stored grid, is the source: a
    carried-over 2008 slot-15 item (Kind 18) is no pet and must never be looked up as one, and
    one source for this slot and the 0x6F's is_equipped pet row keeps the two in step
    (ROADMAP_2009_ADDENDUM C2): the own 0x07 binds +0x1628 = FUN_00462c80(slot 15) from the
    equipped list and every later 0x6F frees that record and must re-bind it."""
    pet = cashmod.equipped_pet(char) if isinstance(char, dict) else None
    return _int(pet['item_id']) & 0xFFFF if pet is not None else 0


def cash_rows_2009(session, char):
    """The ten 2009 (id, attr0) rows of grid slots 15..24 (spec_2009 0x07 cash_equip_item_id
    x10 + cash_equip_item_attr0 = word 0 of the slot's 12-byte record, entity+0x236+12*i).

    Slot 15 is the 2009 PET slot (entity+0x16E; FUN_00462c80 binds the local pet from that
    item id and FUN_00447f40 spawns it): the worn pet record's item (pet_slot_2009), no
    option word. Slots 16..24 are the cash half as in 2008 (16..22 costumes; 2009 adds the pet
    hat / glasses in 23 / 24, inventory.KIND_TO_CASH_SLOT_2009): a grid entry there, else the
    2008 +0x15C cash list (`cash_equip`) those ids used to travel in."""
    legacy = _legacy_cash_ids(session, char)
    rows = [{'cash_equip_item_id': pet_slot_2009(char), 'cash_equip_item_attr0': 0}]   # slot 15: pet
    for i, slot in enumerate(range(EQUIP_SLOTS, EQUIP_SLOTS + CASH_EQUIP_SLOTS)):  # 16..24
        item_id, words = _grid_entry(session, char, slot)
        if not item_id:
            item_id, words = legacy[i], [0]
        rows.append({'cash_equip_item_id': item_id, 'cash_equip_item_attr0': words[0]})
    return rows


def _record_2009(rec, session, char, account, guild=None):
    """The 2009 0x07 / 0x04 row from the 2008 one (client-2009-login/-world; spec_2009 0x07).

    - the GM-or-guild block (arch09-roster-record, guild_block FORM_FIELD): 1 + gm_hidden for
      a GM, else the guild id + name + emblem words for a member, else 0.
    - 17 appearance words (`look` + `look_ext`) and the character's own gender.
    - 15 regular equip slots 0..14 with their option blocks: the grid positions are the same
      in both builds (FUN_00427af0 writes entity+0x150 + 2*slot for the 2009 Kinds, 2008
      FUN_00426680 entity+0x13C); the 2008 slot 15 left the regular half and is the pet slot.
    - 10 (id, attr0) rows for slots 15..24 (cash_rows_2009).
    - the motion block under its 2009 names (MOTION_NAMES_2009; same idle values).
    - the pet block (pet_block): read only when uid != the receiver's uid (packets.
      DEFAULT_ASSUME_2009), so the owner's own 0x07 carries the fields but not the bytes."""
    out = {MOTION_NAMES_2009.get(k, k): v for k, v in rec.items()
           if k not in ('gm_level', 'gm_hidden', f'repeat[{LOOK_SLOTS}]', f'repeat[{EQUIP_SLOTS}]',
                        f'repeat[{CASH_EQUIP_SLOTS}]')}
    out.update(guild_block(char, guild, session, FORM_FIELD))
    out['gender'] = gender = record_gender(char, account, BUILD_2009)
    out[f'repeat[{LOOK_SLOTS_2009}]'] = [{'appearance_part': v} for v in appearance_2009(char, gender)]
    out[f'repeat[{EQUIP_SLOTS_2009}]'] = rec[f'repeat[{EQUIP_SLOTS}]'][:EQUIP_SLOTS_2009]
    out[f'repeat[{CASH_EQUIP_SLOTS_2009}]'] = cash_rows_2009(session, char)
    # P15 pet-s1 (pet F3): the same block for every receiver; which receivers then hold the
    # owner's pet_info is presence.py's book (clientview pet_info_seen, H1).
    out.update(pet_block(char))
    return out


def player_list(*records):
    """S2C 0x07 / 0x04 fields for one or more records (0x04 takes at most 5 per packet)."""
    rows = [r for group in records for r in (group if isinstance(group, (list, tuple)) else [group])]
    if len(rows) > MAX_RECORDS_PER_PACKET:
        raise ValueError(f'{len(rows)} player records in one packet: the client payload limit is '
                         f'{MAX_RECORDS_PER_PACKET} (world_movement_npc.md F1 step 5)')
    return {'player_count': len(rows), 'repeat[player_count]': rows}


def to_0x05(rec, client_build=None):
    """The same row in S2C 0x05 RemotePlayerAppear field names (no count byte, no stall
    block, and buff entries without the ground point).

    2009 (spec_2009 0x05, client-2009-world): 0x05 uses the 0x07 row's own 2009 names, so
    only the stall block and the buff ground points go, and the trailing pet block is read
    UNCONDITIONALLY (no receiver gate, unlike 0x07/0x04): has_pet is always there."""
    if client_build == BUILD_2009:
        out = {k: v for k, v in rec.items() if k not in DROPPED_IN_05}
        out['repeat[buff_count]'] = [{'buff_skill_id': b['buff_skill_id'], 'buff_duration': b['buff_duration']}
                                     for b in rec['repeat[buff_count]']]
        out['has_pet'] = _int(rec.get('has_pet'))
        if not out['has_pet']:
            out.pop('pet_level', None)
            out.pop('pet_name', None)
        return out
    out = {}
    for key, value in rec.items():
        if key in DROPPED_IN_05:
            continue
        out[KEY_MAP_05.get(key, key)] = value
    out[f'repeat[{LOOK_SLOTS}]'] = [{'appearance': e['appearance_part']}
                                    for e in rec[f'repeat[{LOOK_SLOTS}]']]
    out[f'repeat[{EQUIP_SLOTS}]'] = [
        {'equip_item_id': e['equip_item_id'],
         f'repeat[{EQUIP_OPTION_WORDS}]': [{'equip_option': w['equip_item_attr']}
                                           for w in e[f'repeat[{EQUIP_OPTION_WORDS}]']]}
        for e in rec[f'repeat[{EQUIP_SLOTS}]']]
    out[f'repeat[{CASH_EQUIP_SLOTS}]'] = [{'extra_equip_item_id': e['cash_equip_item_id']}
                                          for e in rec[f'repeat[{CASH_EQUIP_SLOTS}]']]
    out['repeat[buff_count]'] = [{'buff_id': b['buff_skill_id'], 'buff_time_ms': b['buff_duration']}
                                 for b in rec['repeat[buff_count]']]
    return out


# --------------------------------------------- 0x52 remote player info (lc-player-info) ---
def info_equipment(session, char, client_build=None):
    """The 25 (item_id, [6 option words]) of the entity equip grid the Player Info window
    lists (2008 entity +0x13C u16[25] with the +0x16E 12-byte blocks, 2009 +0x150 / +0x182;
    the local Char. Info path FUN_00446300 0x44A01C / 2009 FUN_00448730 walks the same 25),
    in grid order - the window appends them in entry order and skips item_id 0.

    Regular slots are the grid with its stored option words (same precedence as
    equip_grid). 2009 slot 15 is the pet slot: the worn pet's item (pet_slot_2009, as in
    cash_rows_2009). Slots 16..24 are the cash half: a grid entry there, else the legacy
    `cash_equip` list, exactly what the owner's 0x07 / 0x04 / 0x05 row carries (cash_equip /
    cash_rows_2009)."""
    regular = EQUIP_SLOTS_2009 if client_build == BUILD_2009 else EQUIP_SLOTS
    legacy = _legacy_cash_ids(session, char)
    rows = []
    for slot in range(INFO_EQUIP_ENTRIES):
        if slot < regular:
            rows.append(_grid_entry(session, char, slot))
        elif slot < EQUIP_SLOTS:                               # 2009 slot 15: the pet
            rows.append((pet_slot_2009(char), [0] * EQUIP_OPTION_WORDS))
        else:
            item_id, words = _grid_entry(session, char, slot)
            if not item_id:
                item_id, words = legacy[slot - EQUIP_SLOTS], [0] * EQUIP_OPTION_WORDS
            rows.append((item_id, words))
    return rows


def info_entry(item_id, words):
    """One S2C 0x52 equipment entry: u16 item_id, u8 option_count, option_count x u16, u16
    option_last. The handler reads the counted words into words 0..count-1 of the entry's
    6-word block and option_last into word 5 (2008 0x456DC7 loop), so sending the words up
    to the last non-zero one of 0..4 reproduces the stored block exactly, zeros between
    included (canonical_options would stop at the first zero and lose the words after it)."""
    words = [_int(w) & 0xFFFF for w in (words or [])][:EQUIP_OPTION_WORDS]
    words += [0] * (EQUIP_OPTION_WORDS - len(words))
    head = words[:INFO_OPTION_WORDS]
    count = max((i + 1 for i, w in enumerate(head) if w), default=0)
    return {'item_id': _int(item_id) & 0xFFFF, 'option_count': count,
            'repeat[option_count]': [{'option': w} for w in head[:count]],
            'option_last': words[INFO_OPTION_WORDS]}


def player_info(session, char, account=None, client_build=None, guild=None):
    """S2C 0x52 PlayerInfoView fields (window 0x72 "Player Info.") for a character: the
    record the other clients draw, from the same sources as player_record so Char. Info
    shows the same whether the client fills the window itself (target on its map) or from
    this reply (C2S 0x2A, target elsewhere): manner = the account's i32 (karma), level from
    exp, fame rank, class / tier, STR/DEX/INT/SPR, the 25 grid entries.

    2009 (spec_2009 0x52 diff_vs_2008): a u16 guild_id between level and rank, 0 = "N/A",
    > 1 followed by the guild name (`guild`: the target's GuildTag; guild_block FORM_INFO)."""
    session, char, account = session or {}, char or {}, account or {}
    entries = [info_entry(item_id, words) for item_id, words in info_equipment(session, char, client_build)]
    fields = {
        'char_name': char.get('name', ''),
        'manner_points': _int(account.get('manner')),
        'level': level_of(char) & 0xFF,
        'rank': rank_of(char),
        'job1': _int(char.get('class')) & 0xFF,
        'job2': _int(char.get('job2')) & 0xFF,
        'stat_str': _int(char.get('str')) & 0xFFFF,
        'stat_dex': _int(char.get('dex')) & 0xFFFF,
        'stat_int': _int(char.get('int')) & 0xFFFF,
        'stat_spr': _int(char.get('spr')) & 0xFFFF,
        'equip_count': len(entries),
        'repeat[equip_count]': entries,
    }
    if client_build == BUILD_2009:
        fields.update(guild_block(char, guild, session, FORM_INFO))
    return fields


# ------------------------------------------------------- 0x02 character list ---
def character_row(char, client_build=None, account=None):
    """One select-screen entity in S2C 0x02 (lc-charlist).

    class_id (+0x110) is the class and job_branch (+0x111) the tier: the server used to
    put the LEVEL in job_branch, which labels a level-2 character with the tier-2 class
    name and reads past the 21-entry name table from level 3 (B5, S2-23). The level itself
    is not in the packet at all: the client derives it from total_exp (B6). The three ranks
    and their changes are 0 until the fame -> rank table is known (Q7).

    2009 (spec_2009 0x02): + bool `gender` after the name (entity+0x11B, the character's own)
    and 17 appearance words, 82 bytes per record."""
    row = {
        'name': char.get('name', ''),
        'class_id': _int(char.get('class')) & 0xFF,
        'job_branch': _int(char.get('job2')) & 0xFF,
        'total_exp': progression.clamp_exp(_int(char.get('exp'))),
        'rank_1': 0, 'rank_2': 0, 'rank_3': 0,
        'rank_change_1': 0, 'rank_change_2': 0, 'rank_change_3': 0,
    }
    if client_build == BUILD_2009:
        row['gender'] = gender = record_gender(char, account, BUILD_2009)
        row[f'repeat[{LOOK_SLOTS_2009}]'] = [{'appearance': v} for v in appearance_2009(char, gender)]
    else:
        gender = record_gender(char, account)
        row[f'repeat[{LOOK_SLOTS}]'] = [{'appearance': v} for v in appearance(char, gender)]
    return row


def character_list(uid, account, *, result=1, transfer_status=TRANSFER_STATUS_NONE,
                   client_build=None, session_key=0):
    """S2C 0x02 LoginResultCharacterList fields for a successful login (lc-charlist).

    account_id is the account uid the client keeps in scene+0x220 for the whole session,
    account_gender_flag gates "(M)"/"(F)" items (B15) and manner_points is the account's
    manner i32 - the three were a hard-coded 0 / 0 / 0 under the names unknown_1 /
    has_premium / premium_flags (S2-46). The reply ends after transfer_status: the 16
    trailing bytes the old builder appended were never read.

    client_build '2009' (spec_2009 0x02): the account gender byte is `account_gender_byte`
    (the 2009 client overwrites it with 1, gender is per character), then u8
    account_flag_152 and the u32 `session_key` the client echoes in its next C2S 0x01
    (relogin after a channel change); records per character_row(..., '2009')."""
    account = account or {}
    chars = list(account.get('characters') or [])[:MAX_CHARACTERS]
    fields = {
        'result': result,
        'account_id': _int(uid) & 0xFFFFFFFF,
        # premium_cash-wallet-model (F1 step 2): the popup owed after a first purchase (the
        # account's first_purchase_notice, cash.ensure_account); mall+0x00 shows it at the next
        # 0x6A and clears it.
        'cash_first_purchase_flag': 1 if account.get('first_purchase_notice') else 0,
        'account_gender_flag': 1 if account.get('gender') else 0,
        'manner_points': _int(account.get('manner')),
        'char_count': len(chars),
        'repeat[char_count]': [character_row(c, client_build, account) for c in chars],
        'transfer_status': _int(transfer_status),
    }
    if client_build == BUILD_2009:
        fields['account_gender_byte'] = fields.pop('account_gender_flag')
        fields['account_flag_152'] = ACCOUNT_FLAG_152
        fields['session_key'] = _int(session_key) & 0xFFFFFFFF
    return fields
