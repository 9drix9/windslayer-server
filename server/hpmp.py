#!/usr/bin/env python3
"""
hpmp.py - the authoritative HP/MP model and natural regeneration (cs-hp-mp-model, cs-regen)
==========================================================================================
combat_skill.md 1.3 / 1.5 / 3.5 / F2 / F11, roadmap S2-35 and B9/B19.

In field mode the server owns every HP/MP value (the client writes +0x9C only as a room
host), but the client computes the MAXIMA itself - from the job, level, stats and equipment
of its own 0x07 record - and clamps every 0x28/0x44 against them. So the server must compute
exactly the same maxima, or its clamps disagree with the globe the player sees:

    import hpmp
    d = hpmp.derive(session, char)          # Derived(max_hp=140, max_mp=87, regen_hp=5, ...)
    hpmp.refresh(session, char)             # session max_hp/max_mp + clamp of the current
    hpmp.refresh(session, char, full=True)  # level-up: the client's own full heal

The LIVE BUG this replaces: `max_hp = session.get('max_hp', char.get('hp', 100))` made the
maximum whatever the record last stored as CURRENT HP, so a potion above it was clamped
away, a relog after a heal lost it, and the model had no room for damage at all. Nothing
here ever reads a maximum from the store: `char['hp']` / `char['mp']` are the current values
(world-persistence), and max is derived every time, like the level is from the exp.

Max HP - FUN_00427d40 (asm 0x427D40, constants read from WindSlayer.exe)
------------------------------------------------------------------------
    stat   = u16 SPR (+0xEC, the 0x07 `stat_tol`) + i32 bonus (+0xFC, equipment Tol)
    raw    = f32( (stat * MUL[c] + ADD[c]) * 10.0 + BASE[c] + f32sum((level-1) x PER[c]) )
    raw    = f32( raw + i32 flat bonus (+0x100, option stones with Attribute 1) )
    max_hp = trunc(raw) + sum(mHP of the ids in the second slot table +0x107C)
             (0 when level is 0 or > 99, or the class is > 6)
    c     : 0      1      2      3      4      5      6
    MUL   : 1.0    1.2    1.2    1.2    1.3    1.3    1.4     (float32 0x6F14B8)
    ADD   : 10     25     20     15     15     10     10      (float32 0x6F1448)
    BASE  : 10     20     20     15     15     10     10      (switch 0x427D89)
    PER   : 2      6      5      4      3      2      2

Max MP - FUN_00427f40 (asm 0x427F40)
-----------------------------------
    stat   = u16 INT (+0xEA, `stat_int`) + i32 bonus (+0xF8, equipment Int)
    max_mp = trunc(f32(f32((stat * MUL[c] + ADD[c]) * 7.0 + BASE[c] + level_sum) + i32 +0x104))
             (0 when level > 99 or the class is > 6; no slot-table term)
    MUL   : 1.0    1.3    1.4    1.3    1.2    1.0    1.0     (float32 0x6F149C)
    ADD   : 10     15     10     15     15     25     25      (float32 0x6F142C)
    BASE  : 10     10     10     15     15     25     20      (switch 0x427F7E)
    PER   : 2      1      1      1.5    4      6      5

The intermediate stores are float32 (FSTP float) and the final conversion is _ftol2
(truncation); `_f32` reproduces both, so a 1.3 * stat that lands a hair under an integer
rounds the way the client's does. Every live reading so far matches (test_hpmp.py):
TestHero Lv1 STR3/DEX2/INT1/SPR3 140/87, Lv2 142/89; stats 0 at Lv1 110/80, Lv2 112/82;
SPR 30 at Lv1 410, Lv5 418/88, Lv12 432/102, Lv13 434/104; a Warrior with stats 0 270/115,
Lv3 282/117 (LIVE_TEST_LOG / live_verify combat_skill#05-#07, login_character).

Bonuses - FUN_0041ace0, field branch (scene+0xF6C == 0)
-------------------------------------------------------
For each of the 16 equip-grid items (+0x13C, the 0x07 equip rows): Str/Dex/Int/Tol columns
(def+0x1A8..+0x1B4) go to +0xF0..+0xFC. For Kinds 5, 6, 8, 9, 10, 11, 12 the option words
(+0x16E, words 0..4 until the first 0) are item ids too: Kind 10 also adds their stat columns,
and by the option's Attribute (def+0x190) 1 adds its HP to +0x100 (max HP), 2 its MP to +0x104
(max MP), 3 its HP to +0x108 (HP regen), 4 its MP to +0x10C (MP regen). Word 5 (the block's
trailing word, +0x178) always adds its record's stat columns. Room mode (stats forced to 55,
level 49) is the pvp group's and is not modelled.

Regeneration - FUN_00417e10 (only as a room host on the client) + FUN_0041ace0
-------------------------------------------------------------------------------
    regen_mp = 4 + MP of the learned Improve Mana Recovery (490..499) + option Attribute 4
    regen_hp = 5 + HP of the learned Improve Rejuvenation (111..120)  + option Attribute 3
Every 15000 ms (+0xE80, +30 per 30 ms frame) MP += regen_mp when below max, clamped; the HP
timer (+0xE7C) only runs while the action is idle (+0x904 == 8) and restarts from 0 whenever
it is not, so HP rises 15 s after the player stopped and every 15 s after that. Both timers
live on the entity, so a map load (which recreates the local player) restarts them. The
field client runs none of this (live combat_skill#07: HP 10 / MP 1 unchanged after 62 s),
so the server ticks it and sends plain S2C 0x28 / 0x44 absolutes, no 0x3D visuals (F11).
"""
import struct
from collections import namedtuple

import en_content as EC
import progression
import records as R
import skills as SK

# ------------------------------------------------------------ client tables ---
CLASS_COUNT = 7                   # +0x110 >= 7 makes both functions return 0
LEVEL_MAX = progression.LEVEL_MAX
HP_STAT_MUL = (1.0, 1.2, 1.2, 1.2, 1.3, 1.3, 1.4)          # 0x6F14B8
HP_STAT_ADD = (10.0, 25.0, 20.0, 15.0, 15.0, 10.0, 10.0)    # 0x6F1448
HP_SCALE = 10.0                                             # double 0x6F8C68
HP_BASE = (10.0, 20.0, 20.0, 15.0, 15.0, 10.0, 10.0)        # jump table 0x427F20
HP_PER_LEVEL = (2.0, 6.0, 5.0, 4.0, 3.0, 2.0, 2.0)
MP_STAT_MUL = (1.0, 1.3, 1.4, 1.3, 1.2, 1.0, 1.0)           # 0x6F149C
MP_STAT_ADD = (10.0, 15.0, 10.0, 15.0, 15.0, 25.0, 25.0)    # 0x6F142C
MP_SCALE = 7.0                                              # double 0x6F8A68
MP_BASE = (10.0, 10.0, 10.0, 15.0, 15.0, 25.0, 20.0)        # jump table 0x428070
MP_PER_LEVEL = (2.0, 1.0, 1.0, 1.5, 4.0, 6.0, 5.0)

# FUN_0041ace0 regen bases (+0x108 = 5, +0x10C = 4 before any bonus).
REGEN_HP_BASE = 5
REGEN_MP_BASE = 4
# The client's regen period: +0xE7C/+0xE80 compared against 15000 ms (FUN_00417e10).
REGEN_PERIOD_SECS = 15.0
# Equipment Kinds whose option words FUN_0041ace0 reads as bonus items.
OPTION_BONUS_KINDS = frozenset((5, 6, 8, 9, 10, 11, 12))
KIND_STAT_OPTIONS = 10            # only this Kind also takes the options' stat columns
OPTION_WORDS_BONUS = 5            # words 0..4 (stop at the first 0)
OPTION_WORD_STATS = 5             # word 5: stat columns only
ATTR_MAX_HP, ATTR_MAX_MP, ATTR_REGEN_HP, ATTR_REGEN_MP = 1, 2, 3, 4
# Mode-1 room maps (roadmap 1.11a): the UDP host owns HP there (pvp-udp-host), not this
# ticker - FUN_00417e10's regen block also requires map+0x70 == 0.
ROOM_MAPS = frozenset((9801, 9802, 9803, 9804, 9902, 9903))
U16 = 0xFFFF

Bonus = namedtuple('Bonus', 'str dex int tol hp mp regen_hp regen_mp')
Derived = namedtuple('Derived', 'max_hp max_mp regen_hp regen_mp bonus level job')


def _f32(x):
    """Round to float32, as FSTP float does."""
    return struct.unpack('<f', struct.pack('<f', float(x)))[0]


def _ftol(x):
    """_ftol2 (FUN_0049fb80): truncation toward zero."""
    return int(x)


def _level_sum(level, per):
    """FLDZ, then (level - 1) x `FADD; FSTP float` of the per-level constant."""
    total = 0.0
    for _ in range(max(0, int(level) - 1)):
        total = _f32(total + per)
    return total


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ maxima ---
def max_hp(job, level, stat_tol, bonus_tol=0, flat=0, slot_mhp=0):
    """FUN_00427d40 for one entity. `slot_mhp` is the summed mHP of the second slot table."""
    job, level = _as_int(job), _as_int(level)
    if level == 0 or level > LEVEL_MAX or not 0 <= job < CLASS_COUNT:
        return 0
    stat = (_as_int(stat_tol) & U16) + _as_int(bonus_tol)
    raw = _f32((stat * _f32(HP_STAT_MUL[job]) + _f32(HP_STAT_ADD[job])) * HP_SCALE
               + HP_BASE[job] + _level_sum(level, HP_PER_LEVEL[job]))
    raw = _f32(raw + _as_int(flat))
    return _ftol(raw) + _as_int(slot_mhp)


def max_mp(job, level, stat_int, bonus_int=0, flat=0):
    """FUN_00427f40 for one entity (it has no slot-table term)."""
    job, level = _as_int(job), _as_int(level)
    if level > LEVEL_MAX or not 0 <= job < CLASS_COUNT:
        return 0
    stat = (_as_int(stat_int) & U16) + _as_int(bonus_int)
    raw = _f32((stat * _f32(MP_STAT_MUL[job]) + _f32(MP_STAT_ADD[job])) * MP_SCALE
               + MP_BASE[job] + _level_sum(level, MP_PER_LEVEL[job]))
    raw = _f32(raw + _as_int(flat))
    return _ftol(raw)


# ------------------------------------------------------------------ bonuses ---
def _stat_cols(d):
    return (d.stat_str, d.stat_dex, d.stat_int, d.stat_tol)


def equipment_bonus(equip_rows, catalog=None):
    """The equipment half of FUN_0041ace0 (field branch). `equip_rows` is what the 0x07
    record carries (records.equip_grid): [{'equip_item_id', 'repeat[6]': [{'equip_item_attr'}]}]
    or plain (item_id, [6 words]) pairs."""
    catalog = EC.items() if catalog is None else catalog
    s = [0, 0, 0, 0]
    hp = mp = regen_hp = regen_mp = 0
    for row in equip_rows or ():
        if isinstance(row, dict):
            item_id = _as_int(row.get('equip_item_id'))
            words = [_as_int(w.get('equip_item_attr')) for w in row.get(f'repeat[{R.EQUIP_OPTION_WORDS}]', [])]
        else:
            item_id, words = _as_int(row[0]), [_as_int(w) for w in (row[1] or [])]
        words = (words + [0] * R.EQUIP_OPTION_WORDS)[:R.EQUIP_OPTION_WORDS]
        if not item_id:
            continue
        item = catalog.get(item_id)
        if item is None:
            continue
        for i, v in enumerate(_stat_cols(item)):
            s[i] += v
        if item.kind not in OPTION_BONUS_KINDS:
            continue
        for w in words[:OPTION_WORDS_BONUS]:
            if w == 0:
                break
            opt = catalog.get(w)
            if opt is None:
                continue
            if item.kind == KIND_STAT_OPTIONS:
                for i, v in enumerate(_stat_cols(opt)):
                    s[i] += v
            if opt.attribute == ATTR_MAX_HP:
                hp += opt.hp
            elif opt.attribute == ATTR_MAX_MP:
                mp += opt.mp
            elif opt.attribute == ATTR_REGEN_HP:
                regen_hp += opt.hp
            elif opt.attribute == ATTR_REGEN_MP:
                regen_mp += opt.mp
        last = words[OPTION_WORD_STATS]
        opt = catalog.get(last) if last else None
        if opt is not None:
            for i, v in enumerate(_stat_cols(opt)):
                s[i] += v
    return Bonus(*s, hp, mp, regen_hp, regen_mp)


def slot_mhp(slot_buffs, catalog=None):
    """Summed mHP (def+0x160) of the ids in the entity's second slot table (+0x107C, 6 x
    0x18), which FUN_00427d40 adds after the truncation. Breath of Vitality is the only EN
    record with an mHP; cs-buffs / cs-party-skills put its id in session['slot_buffs']."""
    catalog = EC.items() if catalog is None else catalog
    total = 0
    for b in list(slot_buffs or ())[:6]:
        bid = _as_int(b.get('id') if isinstance(b, dict) else b)
        item = catalog.get(bid) if bid else None
        if item is not None:
            total += item.mhp
    return total


def regen_amounts(learned, bonus, catalog=None):
    """(regen_hp, regen_mp) per 15 s: the FUN_0041ace0 bases, the learned regen passives and
    the option stones with Attribute 3 / 4."""
    catalog = EC.items() if catalog is None else catalog
    hp, mp = REGEN_HP_BASE, REGEN_MP_BASE
    mp_id = SK.learned_in_family(learned, SK.REGEN_MP_FAMILY_FIRST)
    if mp_id and catalog.get(mp_id) is not None:
        mp += catalog.get(mp_id).mp
    hp_id = SK.learned_in_family(learned, SK.REGEN_HP_FAMILY_FIRST)
    if hp_id and catalog.get(hp_id) is not None:
        hp += catalog.get(hp_id).hp
    return hp + bonus.regen_hp, mp + bonus.regen_mp


def derive(session, char, catalog=None):
    """Everything the client derives for this character's own record: the maxima and the
    regen amounts, from the same job / level / stats / equip grid / skill list the 0x07
    record sends (records.player_record)."""
    session, char = session or {}, char or {}
    catalog = EC.items() if catalog is None else catalog
    job = _as_int(char.get('class')) & 0xFF
    level = R.level_of(char) & 0xFF
    bonus = equipment_bonus(R.equip_grid(session, char), catalog)
    learned = char.get('skills') or []
    mh = max_hp(job, level, char.get('spr'), bonus.tol, bonus.hp,
                slot_mhp(session.get('slot_buffs'), catalog))
    mm = max_mp(job, level, char.get('int'), bonus.int, bonus.mp)
    rh, rm = regen_amounts(learned, bonus, catalog)
    return Derived(mh, mm, rh, rm, bonus, level, job)


def new_character_vitals(char, catalog=None):
    """(hp, mp) a freshly created character starts with: its full maxima (no equipment, no
    skills yet). The old creation default 100/50 spawned a Novice at 100/140."""
    d = derive({}, char, catalog)
    return d.max_hp, d.max_mp


# ----------------------------------------------------------- session model ---
def current(session, char):
    """(hp, mp) the model holds: the live session values, else the stored current ones."""
    return R.vitals(session, char)


def refresh(session, char, *, full=False, catalog=None):
    """Recompute the maxima into session['max_hp'] / ['max_mp'] and clamp the current values
    the way the client does after the same event (FUN_00427d40/FUN_00427f40 param):

        full=False  cur = min(cur, max)  - equip/unequip 0x1D/0x1E, stat 0x14, spawn, level down
        full=True   cur = max            - level-up (S2C 0x21 / 0x22 call both with 1)

    A maximum of 0 (class > 6, level 0) is never used to clamp: the client would show x/0
    and the server would kill the player. Returns the Derived values. Callers hold the
    session's combat lock (cs-combat-lock)."""
    d = derive(session, char, catalog)
    session['max_hp'], session['max_mp'] = d.max_hp, d.max_mp
    hp, mp = current(session, char)
    if full:
        hp = d.max_hp or hp
        mp = d.max_mp or mp
    else:
        if d.max_hp:
            hp = min(hp, d.max_hp)
        if d.max_mp:
            mp = min(mp, d.max_mp)
    session['hp'], session['mp'] = hp, mp
    return d


def clamp_hp(session, value):
    """A new current HP inside [0, max] (max 0 = unknown: only the lower bound)."""
    value = max(0, _as_int(value))
    top = _as_int(session.get('max_hp'))
    return min(value, top) if top > 0 else value


def clamp_mp(session, value):
    value = max(0, _as_int(value))
    top = _as_int(session.get('max_mp'))
    return min(value, top) if top > 0 else value


# ------------------------------------------------------------------- regen ---
def regen_reset(session, now, period=REGEN_PERIOD_SECS):
    """A map load recreated the local player: both entity timers restart at 0 and the new
    entity is idle (RegisterLocalPlayer writes action 8)."""
    session['regen'] = {'mp_due': now + period, 'idle_since': now, 'hp_due': now + period}


def regen_activity(session, moving, now, period=REGEN_PERIOD_SECS):
    """Follow the idle state from C2S 0x0D. `moving` = the packet carries directional,
    jump/attack or sub-state input. While moving the HP timer is stopped; the first idle
    packet after that restarts it from 0 (the client resets +0xE7C whenever +0x904 != 8).
    A repeated idle packet changes nothing."""
    st = session.get('regen')
    if st is None:
        return
    if moving:
        st['idle_since'] = st['hp_due'] = None
    elif st.get('idle_since') is None:
        st['idle_since'] = now
        st['hp_due'] = now + period


def regen_interrupt(session, now, period=REGEN_PERIOD_SECS):
    """A one-off non-idle action (hit reaction, cast animation): the HP timer restarts."""
    st = session.get('regen')
    if st is not None and st.get('idle_since') is not None:
        st['idle_since'] = now
        st['hp_due'] = now + period


def _advance(due, now, period):
    """Next deadline after firing at `due`: fixed rate, but a tick that fell a whole period
    behind skips ahead instead of firing a burst (ticks.py rule)."""
    nxt = due + period
    return nxt if nxt > now else now + period


def regen_step(session, derived, now, period=REGEN_PERIOD_SECS):
    """Run the due regen timers of one session. Returns (new_hp, new_mp); an element is None
    when that value did not change (nothing to send). The caller holds the combat lock and
    has already skipped dead / out-of-world / room sessions."""
    st = session.get('regen')
    if st is None:
        return None, None
    hp, mp = _as_int(session.get('hp')), _as_int(session.get('mp'))
    new_hp = new_mp = None
    if now >= st['mp_due']:
        st['mp_due'] = _advance(st['mp_due'], now, period)
        if derived.max_mp > 0 and mp < derived.max_mp:
            value = max(0, min(derived.max_mp, mp + derived.regen_mp))
            if value != mp:
                session['mp'] = new_mp = value
    if st.get('hp_due') is not None and now >= st['hp_due']:
        st['hp_due'] = _advance(st['hp_due'], now, period)
        if derived.max_hp > 0 and 0 < hp < derived.max_hp:
            value = max(1, min(derived.max_hp, hp + derived.regen_hp))
            if value != hp:
                session['hp'] = new_hp = value
    return new_hp, new_mp


def moving_state(state_lo):
    """C2S 0x0D state_lo bits 0-8 (+0x8B3 direction, +0x8B4 jump/attack/down, +0x8B5
    sub-state) are the input that takes the player out of idle. Bits 9-11 (+0x8B6) are left
    out: they stay 1 for ~1.5 s after a map change on a standing player (C12), and bits
    22-31 are stale scratch bytes (spec 0x42CE94/0x0D)."""
    return bool(_as_int(state_lo) & 0x1FF)
