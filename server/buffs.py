#!/usr/bin/env python3
"""
buffs.py - buff state, the client's slot-replacement rules and server-driven expiry (cs-buffs)
============================================================================================
combat_skill.md F3d/F3f/F3g/F3i, F5, F2; spec corrections C4 (live combat_skill#08,
item_inventory#10/#11).

THE CLIENT NEVER EXPIRES A BUFF (C4). In field mode it counts slot +0xC down past zero, the
icon blinks, the stat bonus stays, and nothing removes the slot until the server sends
S2C 0x43 (remove + stat recompute) or S2C 0x3C (remove, icon only). So every slot the
server lets the client insert (S2C 0x3B self buff / trap / summon, S2C 0x42 item buff, the
0x07 buff list) is recorded here with its deadline, and a 50 ms ticker on the server sends
the removal when the deadline passes (GameServer._tick_buffs).

    import buffs as B
    buff, displaced = B.apply(session, 292, now)       # after sending 0x3B {292, uid}
    for b in B.pop_expired(session, now): send(B.removal_opcode(b['id']), ...)
    B.rows(session, now)                               # 0x07 buff list, remaining ms
    char['buffs'] = B.persist(session, now)            # relog keeps the remaining time
    B.restore(session, char['buffs'], now)

One record per replacement GROUP, mirroring FUN_00424e20's own rule for inserting a slot
into the entity's 21-slot table (+0xE84, decomp read for this stage):

    group                         client on a new slot of the same group
    f<family base>  skills        OVERWRITES the slot in place (same family: `(old - |lv|) <
                                  new <= (old - |lv|) + rec+0x142`), duration restarted - so a
                                  recast is a refresh and needs no removal packet
    aura 0x8E7..0x912             any aura overwrites any other aura (cross-range rule)
    shield 0x1D4..0x1DE/0xB1A..   Magic Shield and 0xB1A..0xB24 overwrite each other
    summon 0x9C3..0x9F9           a summon overwrites any summon (0x3B passes source 0)
    i<item id>  item buffs        STACK: a second 0x42 for item 148 adds a second record and
                                  icon (live item_inventory#11)
    fairy 0xA9F..0xAC9            STACK (no family check, up to 3)

For the two stacking kinds the server sends the removal of the old record FIRST and then
the new insert, so a refreshed item buff is still one icon and one stat bonus - the
"identical item buffs not stacking unintentionally" rule. `apply` returns that displaced
record for the caller to remove.

Persistence: `char['buffs'] = [{id, remaining_ms, x, y, src}]`. The remaining time is frozen
while the character is offline (retail behaviour unknown, combat_skill.md 3.2; the P3 exit
criterion asks for "the buff with its remaining time" after a relog). Traps are dropped on
every map load and never persisted (F2 step 2: the trap sits on the map it was placed on).

Nothing here sends a packet; every function takes an explicit monotonic `now` so tests can
drive the clock.
"""
from collections import namedtuple

import en_content as EC
import skills as SK

MAX_SLOTS = SK.MAX_BUFF_SLOTS                  # +0xE84, 21 x 0x18
# +0x107C: the second slot table FUN_004259c0 copies aura / group-buff slots into (6 x 0x18);
# FUN_00427d40 adds the mHP of the ids in it to max HP (hpmp.slot_mhp).
MAX_PARTY_SLOTS = 6
REMOVE_RECALC = '0x43'                         # slot removal + stat recompute + HUD refresh
REMOVE_ICON = '0x3C'                           # slot removal only (and the trap event 0xE2)
# Booby Trap Lv1-10: the record carries its ground point (C2S 0x15 / S2C 0x3B / 0x07 x,y).
TRAP_RANGES = SK.TRAP_RANGES
# FUN_00424e20's replacement exceptions (see the table above).
AURA_REPLACE_RANGES = SK._ranges((0x8E7, 0x912))
SHIELD_REPLACE_RANGES = SK._ranges((0x1D4, 0x1DE), (0xB1A, 0xB24))
FAIRY_STACK_RANGES = SK._ranges((0xA9F, 0xAC9))
# Berserk's self-damage: FUN_00417e10 runs FUN_00418ac0(holder, holder, 0, 0, id) every
# 990 ms of the slot (host-only, so the field client never does it). The hit formula of
# FUN_00418ac0 is not ported; the amount is the record's own HP column (Lv1: 2), and it
# never takes the last point of HP (0x28 hp=0 leaves a corpse with no dialog, F8 step 1;
# the death flow is cs-player-death).
BERSERK_PERIOD_SECS = SK.DOT_PERIOD_MS / 1000.0
# Healing Aura 0x8F2..0x8FB (cs-party-skills, combat_skill.md F3f step 3): FUN_00417e10 heals
# the holder and its party by the record's HP every 5010 ms of the slot - host-only, so the
# field client never does it and the server ticks it (due_aura_heals).
AURA_HEAL_PERIOD_SECS = SK.AURA_HEAL_PERIOD_MS / 1000.0

Mods = namedtuple('Mods', 'p_a p_d a_a a_d')


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _record(item_id):
    return EC.items().get(item_id)


# ------------------------------------------------------------ classification ---
def group_of(item_id):
    """The replacement group of a slot id (module docstring)."""
    item_id = _int(item_id) & 0xFFFF
    if SK._in(item_id, AURA_REPLACE_RANGES):
        return 'aura'
    if SK._in(item_id, SHIELD_REPLACE_RANGES):
        return 'shield'
    if SK._in(item_id, SK.SUMMON_RANGES):
        return 'summon'
    sd = SK.skill_def(item_id)
    if sd is None:
        return f'i{item_id}'
    if SK._in(item_id, FAIRY_STACK_RANGES):
        return f'fairy{sd.family}'
    return f'f{sd.family}'


def client_replaces(item_id):
    """True when FUN_00424e20 overwrites the holder's slot of the same group in place; False
    when it adds a second record next to it (item buffs, the fairy summons)."""
    return SK.skill_def(item_id) is not None and not SK._in(item_id, FAIRY_STACK_RANGES)


def stat_mod(item_id):
    """The slot changes the holder's stats, so its removal must recompute them (0x43, not
    0x3C): any Skill_P_A/P_D/A_A/A_D (the 0x43/FUN_0041ace0 gate), mHP/mMP, or an aura /
    group-buff id (FUN_00425af0 re-evaluates those). combat_skill.md F5 step 2."""
    rec = _record(item_id)
    if rec is None:
        return False
    return bool(rec.skill_p_a or rec.skill_p_d or rec.skill_a_a or rec.skill_a_d
                or rec.mhp or rec.mmp or SK.party_buff(item_id))


def removal_opcode(item_id):
    """S2C 0x43 for a slot with stat modifiers, S2C 0x3C for everything else (traps, summons,
    icon-only item buffs). Live combat_skill#09/#10: 0x3C leaves the stat bonus behind."""
    return REMOVE_RECALC if stat_mod(item_id) else REMOVE_ICON


def duration_ms(item_id):
    """The slot duration the client itself gives the id: a skill's Con (summons forced to
    60000 ms, FUN_00424e20 @0x425124), an item's Con (def+0x1C8, item_inventory#10)."""
    sd = SK.skill_def(item_id)
    if sd is not None:
        return sd.duration_ms
    rec = _record(item_id)
    return 0 if rec is None else max(0, _int(rec.con))


def is_trap(item_id):
    return SK._in(item_id, TRAP_RANGES)


def is_berserk(item_id):
    return SK._in(item_id, SK.BERSERK_RANGES)


def berserk_damage(item_id):
    sd = SK.skill_def(item_id)
    return 0 if sd is None else abs(sd.hp)


def is_healing_aura(item_id):
    sd = SK.skill_def(item_id)
    return sd is not None and sd.kind == 'aura' and SK._in(item_id, SK.AURA_HEAL_RANGES)


def aura_heal(item_id):
    """HP one Healing Aura tick gives each member it covers: the record's own HP column
    (Lv1 11 .. Lv10 25; FUN_004258d0 = 0, so the cast itself heals nobody)."""
    sd = SK.skill_def(item_id)
    return 0 if sd is None else max(0, sd.hp)


# ------------------------------------------------------------------ state ---
def state(session):
    """session['buffs']: the holder's slots in the client's own order. A list, never None,
    once a session has a model - records.buff_rows then never falls back to the persisted
    list for it."""
    buffs = session.get('buffs')
    if not isinstance(buffs, list):
        buffs = session['buffs'] = []
    return buffs


def _new(item_id, now, dur_ms, x=0, y=0, src=0):
    item_id = _int(item_id) & 0xFFFF
    buff = {'id': item_id, 'group': group_of(item_id), 'expires': now + dur_ms / 1000.0,
            'x': _int(x) & 0xFFFF, 'y': _int(y) & 0xFFFF, 'src': _int(src) & 0xFFFFFFFF}
    if is_berserk(item_id):
        buff['next_tick'] = now + BERSERK_PERIOD_SECS
    if is_healing_aura(item_id):
        buff['next_heal'] = now + AURA_HEAL_PERIOD_SECS
    return buff


def sync_party_slots(session):
    """session['slot_buffs']: the aura / group-buff slots FUN_004259c0 copies into +0x107C:
    the holder's own (the local caster, FUN_00424e20 field branch) and, in a party, the ones
    its same-map members' auras fan out to it (session['aura_cover'], cs-party-skills: kept
    by GameServer._tick_party_auras; combat_skill.md F3f step 2). hpmp.derive adds their mHP
    to max HP, so Breath of Vitality raises the maximum of the whole party while it lasts."""
    own = [{'id': b['id']} for b in _live(session) if SK.party_buff(b['id'])]
    cover = [{'id': int(c['id']) & 0xFFFF} for c in (session.get('aura_cover') or ())]
    session['slot_buffs'] = (own + cover)[:MAX_PARTY_SLOTS]
    return session['slot_buffs']


def party_auras(session, now):
    """The running aura / group-buff records of `session` another party member can be
    covered by: [{id, src (the holder's uid), group}], in slot order."""
    uid = _int(session.get('uid'))
    return [{'id': b['id'], 'src': uid, 'group': b['group']} for b in _live(session)
            if SK.party_buff(b['id']) and remaining_ms(b, now) > 0]


def cover_of(own, others):
    """cs-party-skills: the aura cover a holder gets from its same-map party members, from
    party_auras() snapshots - `own` the holder's, `others` one list per member in join order
    (never the holder). One record per aura GROUP: the holder's own slot of a group first,
    then the first member that runs it (FUN_00424e20's cross-range rule: any aura overwrites
    any other aura in one slot table, so a group is one slot); with the holder's own at most
    MAX_PARTY_SLOTS (+0x107C holds 6)."""
    groups = {a['group'] for a in own}
    out = []
    for auras in others:
        for a in auras:
            if a['group'] in groups:
                continue
            groups.add(a['group'])
            out.append({'id': a['id'], 'src': a['src']})
    return out[:max(0, MAX_PARTY_SLOTS - len(own))]


def apply(session, item_id, now, *, x=0, y=0, src=0, dur_ms=None):
    """Record the slot the client inserts for `item_id` right now. Returns (buff, displaced):

        buff       the new record, or None when the client inserts nothing (duration 0, or a
                   full 21-slot table: FUN_00424e20 returns 0 and drops it)
        displaced  the previous record of the same group when the client would STACK the
                   new one next to it: the caller sends removal_opcode(displaced['id'])
                   BEFORE the insert packet. None when there was none or the client
                   overwrites it in place.
    """
    dur = duration_ms(item_id) if dur_ms is None else max(0, _int(dur_ms))
    if dur <= 0:
        return None, None
    buffs = state(session)
    new = _new(item_id, now, dur, x, y, src)
    displaced = None
    for i, old in enumerate(buffs):
        if old['group'] != new['group']:
            continue
        if client_replaces(new['id']):
            buffs[i] = new
            sync_party_slots(session)
            return new, None
        displaced = buffs.pop(i)
        break
    if len(buffs) >= MAX_SLOTS:
        if displaced is not None:
            buffs.append(displaced)          # nothing is sent, so nothing changes
        return None, None
    buffs.append(new)
    sync_party_slots(session)
    return new, displaced


def _live(session):
    """The records without creating the list: a read must not give a session a buff model
    (records.buff_rows would then stop falling back to the stored list)."""
    buffs = session.get('buffs')
    return buffs if isinstance(buffs, list) else []


def find(session, item_id):
    """The record holding exactly this slot id, or None."""
    item_id = _int(item_id) & 0xFFFF
    return next((b for b in _live(session) if b['id'] == item_id), None)


def remove(session, item_id):
    """Drop the record of this slot id (the client's removal shifts later slots down, so
    the list order keeps matching). Returns it, or None."""
    buffs = state(session)
    item_id = _int(item_id) & 0xFFFF
    for i, b in enumerate(buffs):
        if b['id'] == item_id:
            del buffs[i]
            sync_party_slots(session)
            return b
    return None


def remaining_ms(buff, now):
    if 'expires' in buff:
        return int(round((float(buff['expires']) - now) * 1000.0))
    return _int(buff.get('remaining_ms'))


def pop_expired(session, now):
    """Remove and return every record whose deadline has passed, in slot order. The caller
    sends one removal per record (one per slot id: the table never holds two)."""
    buffs = state(session)
    gone = [b for b in buffs if remaining_ms(b, now) <= 0]
    if gone:
        buffs[:] = [b for b in buffs if remaining_ms(b, now) > 0]
        sync_party_slots(session)
    return gone


def next_deadline(session):
    buffs = session.get('buffs') or []
    return min((float(b['expires']) for b in buffs if 'expires' in b), default=None)


def prune_for_map_load(session, now):
    """A map load recreates the local player from the 0x07 buff list (F2): traps stay on the
    map they were placed on and are forgotten (F2 step 2), expired records are dropped
    without a packet (the new entity never gets them). Returns what was dropped."""
    buffs = state(session)
    gone = [b for b in buffs if is_trap(b['id']) or remaining_ms(b, now) <= 0]
    if gone:
        buffs[:] = [b for b in buffs if b not in gone]
        sync_party_slots(session)
    return gone


def clear(session):
    """Forget every record without a packet: death (the client memsets both slot tables in
    action case 0xD) and disconnect. cs-player-death calls it. The party auras that
    covered the holder go too (the memset takes +0x107C with it); a living member's cover
    comes back with the next GameServer._tick_party_auras once it is alive again."""
    state(session)[:] = []
    session.pop('aura_cover', None)
    sync_party_slots(session)


def modifiers(session):
    """Summed Skill_P_A / P_D / A_A / A_D of the holder's active slots: the attack and
    defense modifiers FUN_0041ace0 applies while a slot is in the table (combat_skill.md
    1.1). The placeholder skill damage (DAMAGE_FORMULA 'placeholder') adds p_a to its attack
    term; the client formula takes the slots itself, with FUN_0041b830's exclusions
    (damage.buff_mods). A party member's aura that covers the holder counts too
    (session['aura_cover'], cs-party-skills: "Support / Advance auras: modifiers only",
    F3f step 3)."""
    p_a = p_d = a_a = a_d = 0
    for b in list(_live(session)) + list(session.get('aura_cover') or ()):
        rec = _record(b['id'])
        if rec is None:
            continue
        p_a += rec.skill_p_a
        p_d += rec.skill_p_d
        a_a += rec.skill_a_a
        a_d += rec.skill_a_d
    return Mods(p_a, p_d, a_a, a_d)


def due_aura_heals(session, now):
    """[(buff, hp)] for every Healing Aura tick of this holder due at `now` (one per 5010 ms
    of the slot, never a burst - a late tick fires once and moves on, like Berserk's)."""
    out = []
    for b in _live(session):
        tick = b.get('next_heal')
        if tick is None or now < tick or remaining_ms(b, now) <= 0:
            continue
        nxt = tick + AURA_HEAL_PERIOD_SECS
        b['next_heal'] = nxt if nxt > now else now + AURA_HEAL_PERIOD_SECS
        out.append((b, aura_heal(b['id'])))
    return out


def due_berserk(session, now):
    """[(buff, damage)] for every Berserk tick due at `now` (one per 990 ms of the slot,
    never a burst: a late tick fires once and moves on)."""
    out = []
    for b in _live(session):
        tick = b.get('next_tick')
        if tick is None or now < tick or remaining_ms(b, now) <= 0:
            continue
        nxt = tick + BERSERK_PERIOD_SECS
        b['next_tick'] = nxt if nxt > now else now + BERSERK_PERIOD_SECS
        out.append((b, berserk_damage(b['id'])))
    return out


# ------------------------------------------------------------- the wire ---
def rows(buffs, now):
    """0x07/0x04 buff entries {id, remaining_ms, x, y} for the records that are still
    running (a 0 or negative duration would insert a slot the client counts further down
    and never removes). Accepts live records (with 'expires') and persisted ones."""
    out = []
    for b in list(buffs or [])[:MAX_SLOTS]:
        if not isinstance(b, dict):
            continue
        left = remaining_ms(b, now)
        if left <= 0:
            continue
        out.append({'id': _int(b.get('id')) & 0xFFFF, 'remaining_ms': left,
                    'x': _int(b.get('x')), 'y': _int(b.get('y'))})
    return out


# ------------------------------------------------------------- persistence ---
def persist(session, now):
    """The persisted form of the live records: [{id, remaining_ms, x, y, src}]. Traps are
    never persisted (F2 step 2)."""
    out = []
    for b in state(session):
        left = remaining_ms(b, now)
        if left <= 0 or is_trap(b['id']):
            continue
        out.append({'id': b['id'], 'remaining_ms': left, 'x': b.get('x', 0),
                    'y': b.get('y', 0), 'src': b.get('src', 0)})
    return out


def restore(session, stored, now):
    """Seed session['buffs'] from the persisted list at enter world: each record runs for
    its stored remaining time from `now`. Traps, unknown ids and spent records are
    dropped. Returns the live list."""
    buffs = state(session)
    buffs[:] = []
    for entry in list(stored or []):
        if not isinstance(entry, dict):
            continue
        item_id = _int(entry.get('id'))
        left = _int(entry.get('remaining_ms'))
        if left <= 0 or is_trap(item_id) or not EC.items().exists(item_id):
            continue
        if len(buffs) >= MAX_SLOTS or any(b['group'] == group_of(item_id) for b in buffs):
            continue
        buffs.append(_new(item_id, now, left, entry.get('x', 0), entry.get('y', 0),
                          entry.get('src', 0)))
    sync_party_slots(session)
    return buffs


def ensure(char):
    """Normalize (and create) char['buffs'] in place for the store migration: dict entries
    with an id in 1..4248 and a positive remaining_ms, at most 21. Structural only (no
    catalog lookups), and a conforming list is left untouched."""
    if not isinstance(char, dict):
        raise TypeError('character record must be a dict')
    raw = char.get('buffs')
    if not isinstance(raw, list):
        raw = char['buffs'] = []
    clean = []
    for entry in list(raw):
        if not isinstance(entry, dict):
            continue
        item_id, left = _int(entry.get('id')), _int(entry.get('remaining_ms'))
        if not 1 <= item_id <= EC.EN_ITEM_MAX_ID or left <= 0 or len(clean) >= MAX_SLOTS:
            continue
        clean.append({'id': item_id, 'remaining_ms': left, 'x': _int(entry.get('x')) & 0xFFFF,
                      'y': _int(entry.get('y')) & 0xFFFF,
                      'src': _int(entry.get('src')) & 0xFFFFFFFF})
    if clean != raw:
        raw[:] = clean
    return raw
