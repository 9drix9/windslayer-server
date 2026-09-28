#!/usr/bin/env python3
"""
debuffs.py - targeted debuffs on monsters: DoT, detonation, control (cs-debuffs)
================================================================================
combat_skill.md F3h, 1.2 (FUN_004257a0 = 0 ranges), spec corrections C4 / live combat_skill#12.

A targeted skill (Poison, Stun, Time Bomb, ...) is answered to the caster with S2C 0x25; its
S2C 0x3B would insert nothing (FUN_004257a0 = 0), so the effect is shown on the MONSTER with
S2C 0x41 {target_uid = mob, source_uid = caster, item_id}: the client inserts a slot with the
caster uid at +0x14 and draws its icon (live #12: a clock-faced bomb above the Pupu). Like
every slot in field mode, nothing on the client counts it down (FUN_00417e10 runs the DoT /
detonation block only as a room host), so the server owns all of it:

    kind       ids (families)                                   server does
    dot        Poison, Poison Arrow, 0x9C3.., Poison Cloud,     damage every 990 ms for Con
               Strike of Darkness (skills.DOT_RANGES)
    detonate   Cartilage Smash, Time Bomb (DETONATE_RANGES)     damage once at Con, then S2C
                                                                0x3C: its handler runs
                                                                FUN_004256c0, the knockdown /
                                                                explosion (live #12)
    control    Stun, Threaten, Arrow Grapple, Weapon Shackle,   `controlled(mob)` is true while
               Spider Web, Mutation                             it runs (the monster-AI hook;
                                                                today it stops contact damage)
    mark       every other targeted id (Thornbush, Blindfold,   the slot only
               Puppet, Frenzy)

and at Con the slot is removed with S2C 0x3C (0x43 if the record changes stats). One record
per family on a monster: FUN_00424e20 overwrites a same-family slot in place, so a recast is
a refresh and needs no removal. A dead or respawned monster forgets everything with no packet
(0x29 / the 0x06 despawn end the entity; the respawn 0x1A is a new one).

Records live on the Monster (`mob.debuffs = {family: record}`); nothing here sends a packet
and every function takes an explicit `now` (monotonic seconds).
"""
import skills as SK

# Control effects the monster AI must honour (combat_skill.md F3h step 4).
CONTROL_RANGES = SK._ranges(
    (0x1B3, 0x1BD),   # Stun
    (0x242, 0x24C),   # Threaten
    (0x78B, 0x795),   # Arrow Grapple
    (0x8BB, 0x8C5),   # Weapon Shackle
    (0xA52, 0xA5C),   # Spider Web
    (0xB3B, 0xB45),   # Mutation
)
DOT_PERIOD_SECS = SK.DOT_PERIOD_MS / 1000.0


def kind_of(sd):
    if sd.dot:
        return 'dot'
    if sd.detonates:
        return 'detonate'
    if SK._in(sd.id, CONTROL_RANGES):
        return 'control'
    return 'mark'


def state(mob):
    debuffs = getattr(mob, 'debuffs', None)
    if not isinstance(debuffs, dict):
        debuffs = mob.debuffs = {}
    return debuffs


def apply(mob, sd, now, *, src=0, tick_damage=0, hit_damage=0):
    """Record the slot S2C 0x41 inserts on `mob` for skill `sd` (duration = Con). Returns
    (record, replaced): replaced is the previous record of the same family, which the client
    overwrites in place (nothing to send for it).

    tick_damage: a DoT's per-990 ms damage; hit_damage: a detonation's damage. Both are
    computed by the caller at cast time (the caster's attack then), so the ticker needs no
    character."""
    dur = max(0, int(sd.duration_ms))
    if dur <= 0:
        return None, None
    kind = kind_of(sd)
    rec = {'id': sd.id, 'family': sd.family, 'kind': kind, 'expires': now + dur / 1000.0,
           'src': int(src) & 0xFFFFFFFF, 'tick_damage': int(tick_damage),
           'hit_damage': int(hit_damage)}
    if kind == 'dot':
        rec['next_tick'] = now + DOT_PERIOD_SECS
    debuffs = state(mob)
    replaced = debuffs.get(sd.family)
    debuffs[sd.family] = rec
    return rec, replaced


def remaining_ms(rec, now):
    return int(round((float(rec['expires']) - now) * 1000.0))


def _of(rec, src):
    return src is None or int(rec.get('src') or 0) == int(src)


def due_ticks(mob, now, src=None):
    """[record] for every DoT tick due at `now`, one per record per call (a late tick fires
    once and moves on, the ticks.py rule). A tick exactly at the end still fires. `src`: only
    the records of that caster uid (world-shared-monsters: the ticker runs each caster's
    slots under his own combat lock)."""
    out = []
    for rec in list(state(mob).values()):
        if not _of(rec, src):
            continue
        tick = rec.get('next_tick')
        if tick is None or now < tick or tick > rec['expires']:
            continue
        nxt = tick + DOT_PERIOD_SECS
        rec['next_tick'] = nxt if nxt > now else now + DOT_PERIOD_SECS
        out.append(rec)
    return out


def pop_expired(mob, now, src=None):
    """Remove and return the records whose Con has run out (only caster `src`'s if given)."""
    debuffs = state(mob)
    gone = [rec for rec in debuffs.values() if remaining_ms(rec, now) <= 0 and _of(rec, src)]
    for rec in gone:
        debuffs.pop(rec['family'], None)
    return gone


def controlled(mob, now):
    """True while a control effect (Stun, Spider Web, ...) runs on the monster."""
    return any(rec['kind'] == 'control' and remaining_ms(rec, now) > 0
               for rec in state(mob).values())


def clear(mob):
    """Forget every record without a packet (death, respawn, map change)."""
    state(mob).clear()
