#!/usr/bin/env python3
"""
damage.py - the client's own combat numbers, ported (config DAMAGE_FORMULA 'client')
===================================================================================
The field client computes a damage estimate for every hit it detects and draws it as the
digit over the victim, but never writes HP (0x41A6C2): the server is authoritative, so it
must compute the same number. Two functions of the client do it, the same math in both EN
builds (2009 Build 14 VAs first, 2008 in brackets):

    FUN_0041b830 [FUN_0041ace0]  stat derivation: Strong_Atk, Weak_Atk, Def, element attack
                                 and resists of an entity, from its class/level/stats/
                                 equipment/buffs (players) or its hni template (monsters)
    FUN_004194f0 [FUN_00418ac0]  one hit: physical term B (level scaled), the skill term, the
                                 element term E, clamp >= 1, victim buffs, cap at victim HP

Reference: the reconciled spec (re_tools/docs, "Reconciled damage spec for the EN 2009
client"), whose worked examples were run on the original exe bytes under Unicorn. The 2008
functions were re-read for this port: the class tables (0x6F1464.. vs 0x525F44..), the
weapon-class switch (jump table 0x41CA30: 0 STR/10, 1 STR/7, 2 STR/6, 3-4 DEX/8, 5-6 INT/10),
the level scale, the event table and the p5 skill switch are identical. One build difference
is modelled: the 2009 derivation sends equipment Kind 3 / 7 to FUN_0041d640 (option bytes,
no Def), the 2008 one counts their Def like any armour. The regular equipment loop covers 16
grid slots in 2008 (+0x13C) and 15 in 2009 (+0x150); cash/costume slots add nothing.

    import damage as DMG
    hero = DMG.player_stats(char, session)                 # derived at PC_53
    mob = DMG.monster_stats(monster)                       # hni template, no scaling
    hit = DMG.damage(hero, mob, DMG.EV_WEAK, cap=monster.hp)
    hit.total, hit.b, hit.e                                # 8, 8, 0 for TestHero -> Monkey Soldier

No randomness: the client's grade roll (0.75/1/1.25/1.5) is on its DISPLAYED digit only, so
the server takes the base value off HP. Nothing here sends a packet or takes a lock.

Precision (x87 control word): derivations triggered by S2C packets run on Fireway's socket
thread at the default PC_53 (plain Python doubles, FSTP float stores rounded to float32);
the damage itself runs on the main thread after D3D9 CreateDevice at PC_24 (every operation
rounded to float32 - exact here, operands are <= 24 bits). The two differ by at most 1 in
under 1% of builds, and the digit carries the grade roll anyway.

Not modelled (spec section 6): option-stone words and the Kind 3/7 option bytes; the arena
fixed stats; the thrown-item (arg5) path; follow-up hits (Double Kick / Double Arrow); the
fairy branch; summon damage pools. The server has no p5 (the client's key slot): it resolves
the skill term from the fresh cast id (cast_skill); a NODMG cell has no family, so it can't
be reached from a cast and such a hit counts as a plain strong hit.
"""
import math
import struct
from collections import namedtuple

import en_content as EC
import progression
import skills as SK

# ---- constants read from the exes (identical in 2008 and 2009) -----------------------------
# class:     0    1    2    3    4    5    6                   2009 VA / 2008 VA
K_STR = (1.0, 1.0, 1.0, 1.4, 1.4, 1.4, 1.3)          # f32 @0x525F44 / 0x6F1464
B_STR = (10.0, 20.0, 25.0, 15.0, 20.0, 15.0, 20.0)   # f32 @0x525EAC / 0x6F13CC
K_DEX = (1.0, 1.4, 1.3, 1.0, 1.0, 1.2, 1.2)          # f32 @0x525F60 / 0x6F1480
B_DEX = (10.0, 10.0, 15.0, 25.0, 20.0, 20.0, 15.0)   # f32 @0x525EF0 / 0x6F1410
K_INT = (1.0, 1.3, 1.4, 1.3, 1.2, 1.0, 1.0)          # f32 @0x525F7C / 0x6F149C
B_INT = (10.0, 15.0, 10.0, 15.0, 15.0, 25.0, 25.0)   # f32 @0x525F0C / 0x6F142C
K_SPR = (1.0, 1.2, 1.2, 1.2, 1.3, 1.3, 1.4)          # f32 @0x525F98 / 0x6F14B8
B_SPR = (10.0, 25.0, 20.0, 15.0, 15.0, 10.0, 10.0)   # f32 @0x525F28 / 0x6F1448
# Element opposites (table 0x41AA64): 1 Fire <-> 2 Ice/Water, 3 Earth <-> 4 Wind.
OPPOSITE = {1: 2, 2: 1, 3: 4, 4: 3}
ELEM_FIRE, ELEM_WIND = 1, 4
MAX_CLASS = 6

# Victim action events (+0x9DC, C2S 0x0D state_lo bits 12-15 for a monster hitting the
# player; the attacker's +0x9E0 in bits 16-19 for the player's own hit report): 6 contact
# (Body_Atk), 7 weak (Weak_Atk), 9 strong or skill (Strong_Atk); 1 / 4 the same into a guard
# (+ the victim's guard Def); 2 / 3 a weak swing into a guard (no damage, no digit).
EV_CONTACT, EV_WEAK, EV_STRONG = 6, 7, 9
EVENT_REMAP = {5: 4, 8: 7, 10: 9}                    # 0x419549: guarded / airborne variants

# Victim buff ranges read by FUN_004194f0 after the clamp (+0xF24, 21 slots).
HOLY_PROTECTION = (0x913, 0x91D)                     # the hit does nothing, no digit
MAGIC_SHIELD = (0x1D4, 0x1DE)                        # the hit goes to MP (capped) while MP > 0
WICKED_PROTECTION = (0xB1A, 0xB24)                   # reflects (0.3 + 0.05 x rank) of B
VAMPIRIC_ATTACK = (0xB25, 0xB2F)                     # event 9, job 2: total = 2 x rec.HP
BOOBY_TRAP_FAMILY = 0xA31                            # attacker +0x960 != 0: the trap's family
# The 720 ms window a fire (Attribute 1) event-9 skill hit opens on its attacker (+0xE14 /
# +0xE1C, FUN_00416ab0 0x417D56..0x418243): the first victim takes the full hit, every later
# fire hit inside it gets B = 0 and E / 3 (0x41A33A; a splash - the formula is certain, the
# reading as a splash is inferred).
FIRE_WINDOW_SECS = 0.72

# FUN_0041b830's buff loop (+0xF24) skips these ids for the Skill_P_A/P_D sums; the 0x9D9..
# 0x9E3 summon only when the holder cast it himself. A fairy's (0xA9F..0xACA) A_A / A_D count
# once per attribute.
BUFF_SKIP_RANGES = ((0x9CE, 0x9D8), (0x9E4, 0x9EE), (0x9EF, 0x9F9), (0x8E7, 0x8F1), (0x8FD, 0x907))
BUFF_SKIP_OWN_RANGE = (0x9D9, 0x9E3)
FAIRY_RANGE = (0xA9F, 0xACA)
# Class 3 / 4 Evasion passive: FUN_004281b0's lowest learned rank adds its Skill_P_D.
EVASION_FAMILY = {3: 0x171, 4: 0x19D}

# Equipment Kinds (hii def+0x1CC) the derivation switch treats specially.
KIND_WEAPON, KIND_SHIELD = 11, 12
KINDS_OPTION_BYTE_2009 = frozenset((3, 7))           # FUN_0041d640: no Def in 2009
# Regular equipment slots the derivation loops over (the grid slot = the server's slot key).
REGULAR_SLOTS = {'2008': 16, '2009': 15}


def f32(x):
    return struct.unpack('<f', struct.pack('<f', x))[0]


K_STR, B_STR, K_DEX, B_DEX, K_INT, B_INT, K_SPR, B_SPR = (
    tuple(f32(v) for v in t) for t in (K_STR, B_STR, K_DEX, B_DEX, K_INT, B_INT, K_SPR, B_SPR))
RATIO_DEFAULT = (f32(0.7), f32(0.3))                 # 0x52EEB8 strong, 0x52EEB4 weak
REFLECT_BASE, REFLECT_STEP = 0.30000001192092896, 0.05000000074505806   # float-valued doubles


def tdiv(a, b):
    """C integer division (IMUL magic / SAR / sign fix): truncates toward zero."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def trunc(x):
    """_ftol2 (0x4C1EF0): truncation toward zero."""
    return int(math.trunc(x))


def _r(pc):
    return f32 if pc == 24 else (lambda x: x)


def _in(value, rng):
    return rng[0] <= value <= rng[1]


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _cls(value):
    c = _int(value) & 0xFF
    return c if 0 <= c <= MAX_CLASS else 0


# ---- FUN_0041b830: monsters -----------------------------------------------------------------
def derive_monster(t):
    """t: the hni template as {lv, type, hp, body_atk, weak_atk, strong_atk, def, attri_atk,
    attri_def}. Copied with no scaling; hni type 4..7 is element type-3 (table 0x41D064) with
    Attri_Atk as its attack and Attri_Def as the resist of that element."""
    typ = _int(t.get('type'))
    e = typ - 3 if 4 <= typ <= 7 else 0
    res = [0, 0, 0, 0, 0]
    if e:
        res[e] = _int(t.get('attri_def'))
    w, s = _int(t.get('weak_atk')), _int(t.get('strong_atk'))
    rs, rw = (f32(s / f32(s + w)), f32(w / f32(s + w))) if s + w else RATIO_DEFAULT
    hp = t.get('hp')
    return dict(level=_int(t.get('lv')) & 0xFF, cls=0, job=0, I=0, body=_int(t.get('body_atk')),
                weak=w, strong=s, defense=_int(t.get('def')), guard_def=0, elem=e,
                elem_atk=_int(t.get('attri_atk')) if e else 0, res=res, shield_elem=0,
                shield_res=0, r_strong=rs, r_weak=rw, elem_pct=[0] * 5,
                hp=None if hp is None else _int(hp))


def monster_template(mob):
    """The derive_monster input of a server Monster (or an hni NpcTemplate)."""
    def g(*names):
        return next((getattr(mob, n) for n in names if getattr(mob, n, None) is not None), 0)

    return {'lv': g('level', 'lv'), 'type': g('elem_type', 'type'), 'hp': g('hp'),
            'body_atk': g('body_atk'), 'weak_atk': g('weak_atk'), 'strong_atk': g('strong_atk'),
            'def': g('defense'), 'attri_atk': g('attri_atk'), 'attri_def': g('attri_def')}


def monster_stats(mob):
    return derive_monster(monster_template(mob))


# ---- FUN_0041b830: players ------------------------------------------------------------------
def item_record(item):
    """The derivation's view of an hii record (en_content.ItemDef or a dict)."""
    if isinstance(item, dict):
        return item
    return dict(kind=item.kind, job=list(item.job or []), w_att=item.w_att, s_att=item.s_att,
                defense=item.defense, attribute=item.attribute, attri_atk=item.attri_atk,
                attri_def=item.attri_def, str=item.stat_str, dex=item.stat_dex,
                int=item.stat_int, tol=item.stat_tol, skill_p_a=item.skill_p_a,
                skill_p_d=item.skill_p_d, skill_a_a=item.skill_a_a, skill_a_d=item.skill_a_d)


def _add_pct(table, attr, value):
    if value and 0 <= attr < 5:
        for e in ((1, 2, 3, 4) if attr == 0 else (attr,)):
            table[e] += value


def derive_player(cls, level, base, items, pc=53, p_a=0, p_d=0, mods=(), build='2009'):
    """cls = +0x118 (0..6), level = +0x9D, base = (STR, DEX, INT, SPR) u16 +0xF0..+0xF6.
    items: the equipped hii records of the regular slots (item_record dicts). p_a / p_d: extra
    Skill_P_A / Skill_P_D; mods: the buff records (and the Evasion passive) whose Skill_P_A /
    P_D / A_A / A_D add like an item's. build '2008' counts the Def of Kind 3 / 7 items."""
    r = _r(pc)
    bonus = [0, 0, 0, 0]
    sa = wa = dsum = gdef = ea = 0
    wc = 0
    welem = selem = 0
    sres = 0
    res = [0, 0, 0, 0, 0]
    apct = [0] * 5                                   # +0x1240 + 4*e  (Skill_A_A)
    dpct = [0] * 5                                   # +0x1258 + 4*e  (Skill_A_D)
    for it in items:
        bonus[0] += _int(it.get('str')); bonus[1] += _int(it.get('dex'))
        bonus[2] += _int(it.get('int')); bonus[3] += _int(it.get('tol'))
        p_a += _int(it.get('skill_p_a')); p_d += _int(it.get('skill_p_d'))
        attr, kind = _int(it.get('attribute')), _int(it.get('kind'), -1)
        _add_pct(apct, attr, _int(it.get('skill_a_a')))
        _add_pct(dpct, attr, _int(it.get('skill_a_d')))
        sa += _int(it.get('s_att'))                  # every Kind (0x41BF36)
        wa += _int(it.get('w_att'))
        ev = 0
        if 0 <= attr <= 4 and (it.get('attri_atk') or it.get('attri_def')):
            ev = _int(it.get('attri_atk')) if kind == KIND_WEAPON else _int(it.get('attri_def'))
        ev = max(0, ev)
        if kind == KIND_SHIELD:                      # raw guard Def, shield element
            gdef += _int(it.get('defense'))
            if ev:
                selem, sres = attr, sres + ev
        elif kind == KIND_WEAPON:                    # weapon class: the LAST non-zero Job flag
            for j, flag in enumerate(list(it.get('job') or [])[:7]):
                if flag:
                    wc = j
            if ev:
                welem, ea = attr, ea + ev
        elif kind in KINDS_OPTION_BYTE_2009 and build == '2009':
            pass                                     # FUN_0041d640 option bytes: not modelled
        else:
            dsum += _int(it.get('defense'))
            if ev and 1 <= attr <= 4:
                res[attr] += ev
    for m in mods:                                   # +0xF24 buffs and the +0x111C slots
        p_a += _int(m.get('skill_p_a')); p_d += _int(m.get('skill_p_d'))
        attr = _int(m.get('attribute'))
        _add_pct(apct, attr, _int(m.get('skill_a_a')))
        _add_pct(dpct, attr, _int(m.get('skill_a_d')))
    S, D, I, T = (f32(max(0, _int(b) + x)) for b, x in zip(base, bonus))
    c, L = _cls(cls), _int(level) & 0xFF
    l10 = r(L / 10.0)
    if wc == 0:
        M = r(r(r(r(K_STR[c] * S) + B_STR[c]) / 10.0) + l10)
    elif wc == 1:
        M = r(r(r(r(K_STR[c] * S) + B_STR[c]) / 7.0) + l10)
    elif wc == 2:
        M = r(r(r(r(K_STR[c] * S) + B_STR[c]) / 6.0) + l10)
    elif wc in (3, 4):
        M = r(r(r(r(K_DEX[c] * D) + B_DEX[c]) * 0.125) + l10)
    else:
        M = r(r(r(r(K_INT[c] * I) + B_INT[c]) / 10.0) + l10)
    M = f32(r(M + 1.0))
    # STR even for staves and wands; DEX for bows and daggers
    base_k = r(r(K_DEX[c] * D) + B_DEX[c]) if wc in (3, 4) else r(r(K_STR[c] * S) + B_STR[c])
    z = r(base_k + 100.0)
    atk = lambda v: trunc(r(r(r(z * v) / 100.0) + M)) if v else trunc(M)  # noqa: E731
    strong, weak = atk(sa), atk(wa)
    defense = trunc(r(r(r(r(r(K_SPR[c] * T) + B_SPR[c]) + 100.0) * dsum) / 100.0)) if dsum else 0
    if ea:
        ea = trunc(r(r(r(r(r(K_INT[c] * I) + B_INT[c]) + 100.0) * ea) / 100.0))
    zr = r(r(r(K_DEX[c] * D) + B_DEX[c]) + 100.0)
    res = [0] + [trunc(r(r(zr * v) / 100.0)) if v else 0 for v in res[1:]]
    tot = strong + weak                              # ratios BEFORE the % adds (0x41CA45)
    rs, rw = (f32(strong / f32(tot)), f32(weak / f32(tot))) if tot else RATIO_DEFAULT
    if p_a:                                          # 0x41CF30
        if strong:
            q = tdiv(p_a * strong, 100)
            strong += q if q else 1
        if weak:
            weak += tdiv(p_a * weak, 100)
    if p_d and defense:
        defense += tdiv(p_d * defense, 100)
    if 1 <= welem <= 4 and apct[welem] and ea:
        q = tdiv(ea * apct[welem], 100)
        ea += q if q else 1
    for e in range(1, 5):
        if res[e] and (dpct[0] or dpct[e]):
            q = tdiv((dpct[e] + dpct[0]) * res[e], 100)
            res[e] += q if q else 1
    return dict(level=L, cls=c, job=0, I=int(I), body=0, weak=weak, strong=strong,
                defense=defense, guard_def=gdef, elem=welem, elem_atk=ea, res=res,
                shield_elem=selem, shield_res=sres, r_strong=rs, r_weak=rw, elem_pct=apct,
                M=M, wc=wc, hp=None)


# ---- FUN_004194f0 ---------------------------------------------------------------------------
def level_scale(A, D, la, lv, pc=24):
    """trunc(a * (((a / (A+D)) * 2.0) * La / (La+Lv))), a = f32(A); 0 if A == 0 or La == 0.
    ~ 2 A^2 La / ((A+D)(La+Lv))."""
    if A == 0 or la == 0:
        return 0
    r = _r(pc)
    a = f32(A)
    t = r(a / (A + D))                               # FIDIV int
    t = r(t * 2.0)                                   # FMUL ST2, double 2.0 @0x52EEE8
    t = r(t * la)                                    # FIMUL
    t = r(t / (la + lv))                             # FIDIV
    return trunc(r(a * t))


# One resolved hit. total: what comes off the victim (HP, or MP when to_mp); 0 = no digit.
# b / e: the physical and element terms; fire: an event-9 fire skill hit (it opens the
# attacker's FIRE_WINDOW_SECS window); reflect: B the victim's Wicked Protection sent back.
Hit = namedtuple('Hit', 'total b e fire reflect to_mp note')


def _no_hit(note):
    return Hit(0, 0, 0, False, 0, False, note)


def damage(att, vic, event, skill=None, *, pc=24, cap=None, direct=0, splash=False,
           vic_buffs=(), vic_mp=None):
    """One hit of `att` on `vic` (derive_player / derive_monster dicts).

    event   the victim's +0x9DC (1..10); 5/8/10 are remapped, 2/3 do nothing
    skill   events 4/9: the skill term of the attacker's key slot - a dict {attribute,
            attri_atk, skill_p_a, div (3/6 or None), nodmg, nova, vampiric_hp} (cast_skill)
    cap     the victim's current HP (the server's own; monsters: mob.hp)
    direct  an arg6 hit: B = direct, no event switch and no skill term (the delayed release
            of Time Bomb / Cartilage Smash, a Wicked Protection reflection)
    splash  the attacker's fire window is open (a fire event-9 hit then gets B = 0, E / 3)
    vic_buffs / vic_mp   the victim's buff ids and MP (Holy Protection, Magic Shield, Wicked
            Protection)
    """
    r = _r(pc)
    ev = EVENT_REMAP.get(event, event)
    la, lv = _int(att.get('level')), _int(vic.get('level'))
    elem = [0] * 5
    skill = skill or {}
    fire = False
    if direct:
        B = int(direct)
    else:
        if ev in (1, 6):
            A, D = att['body'], vic['defense'] + (vic['guard_def'] if ev == 1 else 0)
        elif ev == 7:
            A, D = att['weak'], vic['defense']
        elif ev in (4, 9):
            A, D = att['strong'], vic['defense'] + (vic['guard_def'] if ev == 4 else 0)
        else:
            return _no_hit(f'event {event}: no damage')
        B = level_scale(A, D, la, lv, pc)
        if ev in (4, 9) and skill:
            if skill.get('nodmg'):
                return _no_hit('a no-damage skill slot')
            if skill.get('div') and B:
                B = tdiv(B, skill['div'])
            e = _int(skill.get('attribute'))
            if 0 < e < 5 and skill.get('attri_atk'):
                elem[e] += _int(skill['attri_atk'])
            if skill.get('skill_p_a'):
                B += tdiv(_int(skill['skill_p_a']) * B, 100)
            if ev == 9 and e == ELEM_WIND and skill.get('nova'):
                B = 0                                # Nova: 2.5 at +0x13E8 and no physical term
            fire = ev == 9 and e == ELEM_FIRE
    for i in range(1, 5):                            # 0x419FD6: attacker element % and INT scale
        v = elem[i]
        if not v:
            continue
        pct = att['elem_pct'][i]
        if pct:
            v = tdiv(pct * v, 100) + v
        z = r(r(r(K_INT[att['cls']] * max(0, att['I'])) + B_INT[att['cls']]) + 100.0)
        elem[i] = trunc(r(r(z * v) / 100.0))
    w = att['elem']                                  # 0x41A070: weapon / monster element
    if 1 <= w <= 4 and att['elem_atk']:
        if not any(elem[1:]):
            ratio = att['r_strong'] if ev in (4, 9) else att['r_weak']
            elem[w] += trunc(r(att['elem_atk'] * ratio))
        else:
            elem[w] += att['elem_atk']
            elem[OPPOSITE[w]] = max(0, elem[OPPOSITE[w]] - att['elem_atk'])
    E = 0
    if la:
        for i in range(1, 5):                        # 0x41A139: per-element level scale
            if elem[i]:
                R = vic['res'][i] + (vic['shield_res'] if ev == 4 and vic['shield_elem'] == i else 0)
                E += level_scale(elem[i], R, la, lv, pc)
    note = []
    if fire and splash:                              # 0x41A33A: a later hit of the fire window
        B, E = 0, tdiv(E, 3)
        note.append('fire splash')
    reflect = 0
    if not direct and not skill.get('delayed'):
        wicked = [b for b in vic_buffs if _in(b, WICKED_PROTECTION)]
        if wicked and B:
            f = f32((wicked[-1] - WICKED_PROTECTION[0]) * REFLECT_STEP + REFLECT_BASE)
            reflect = trunc(r(f * B))
            B -= reflect
            if reflect:
                note.append(f'{reflect} reflected')
    total = B + E                                    # 0x41A487
    if ev == 9 and skill.get('vampiric_hp') and not direct:
        total = 2 * _int(skill['vampiric_hp'])
        note.append('vampiric')
    total = max(1, total)                            # 0x41A55A
    to_mp = False
    if any(_in(b, HOLY_PROTECTION) for b in vic_buffs):
        return Hit(0, B, E, fire, reflect, False, 'holy protection')
    if any(_in(b, MAGIC_SHIELD) for b in vic_buffs) and vic_mp:
        to_mp = True
        total = min(total, _int(vic_mp))
        note.append('magic shield')
    hp = vic.get('hp') if cap is None else cap
    if not to_mp and hp is not None:
        total = min(total, max(0, _int(hp)))         # 0x41A689
    return Hit(total, B, E, fire, reflect, to_mp, ', '.join(note))


# ---- the skill term: (p5, class, job) -> family (switch 0x41980E, tables 0x41A978..) ---------
# value: family | 'NODMG' | (family, div) | {job: value, None: value for any other job}.
# div: B = trunc(B / div) when B != 0 (class 2 job 1: Triple Kick / 3, Merciless Strike / 6).
SKILL_SLOT = {
    1: {1: 0x104, 2: 0x22C, 3: 0x150, 5: 0x200},
    2: {1: 0x10F, 2: 'NODMG', 3: 0x15B, 4: 'NODMG', 5: 0x20B},
    3: {1: 0x13A, 3: 0x166, 4: 0x7A1, 5: 0x216, 6: 0x1DF},
    4: {1: 0x76A, 2: 0x7D8, 4: 0x796, 5: 0x7C2},
    5: {1: 0x775, 2: 'NODMG', 3: 'NODMG', 4: {1: 0x9FA}},
    6: {1: 0x8B0, 2: {1: (0x91E, 3), 2: 0x955}, 3: {1: 0x98C}, 4: {2: 0xA47}, 5: {1: 0xA68}, 6: 0x7AC},
    7: {2: {1: 0x929, 2: 0x960}, 3: {2: 0x9CE}, 4: {1: 0xA1B}, 5: {1: 0xA73, None: 0xACB}, 6: 0x7B7},
    8: {1: 0x8D1, 2: {1: (0x93F, 6), 2: 0x976}, 3: {1: 0x9A2}, 4: {1: 0xA26}, 5: {1: 0xA7E}, 6: {1: 0xB04}},
    9: {1: 0x8DC, 2: {1: 0x94A, 2: 0x981}, 3: {1: 0x9AD, None: 0x9E4}, 5: {1: 0xA89}, 6: {1: 'NODMG'}},
    10: {3: {1: 0x9B8, None: 0x9EF}, 5: {1: 0xA94}, 6: {2: 'NODMG'}},
    11: {6: {2: 'NODMG'}},
}
# The two cells whose event-9 hit is stored and released later as an arg6 hit (0x419D44 ..):
# (p5 9, class 2, job 2) Cartilage Smash and (p5 6, class 4, job 2) Time Bomb.
DELAYED_CELLS = frozenset(((9, 2, 2), (6, 4, 2)))
NOVA_CELL = (7, 5, 1)


def skill_cell(p5, cls, job):
    """-> (family or None, div or None, nodmg) for events 4/9."""
    v = SKILL_SLOT.get(p5, {}).get(cls)
    if isinstance(v, dict):
        v = v.get(job, v.get(None))
    if v == 'NODMG':
        return None, None, True
    if isinstance(v, tuple):
        return v[0], v[1], False
    return v, None, False


def cell_for_skill(skill_id, cls, job):
    """Server side (no p5 on the wire): (p5, family, div) of the slot cell whose family range
    family..family+9 holds the cast skill, or None."""
    for p5 in SKILL_SLOT:
        fam, div, _nodmg = skill_cell(p5, cls, job)
        if fam is not None and fam <= skill_id <= fam + 9:
            return p5, fam, div
    return None


def learned_rank(family, learned_ids):
    """FUN_004281b0: the LOWEST rank family+r (r = 0..9) in the learned list, or 0."""
    return SK.learned_in_family(learned_ids, family, 10)


def _learned(char):
    """char['skills'] read as it is (skills.learned() would normalize - write - the record
    from a combat thread; the store migration already did that)."""
    skills = (char or {}).get('skills')
    return list(skills) if isinstance(skills, list) else []


def _skill_term(rec, **extra):
    term = {'id': rec.id if rec is not None else 0,
            'attribute': rec.attribute if rec is not None else 0,
            'attri_atk': rec.attri_atk if rec is not None else 0,
            'skill_p_a': rec.skill_p_a if rec is not None else 0}
    term.update(extra)
    return term


def cast_skill(char, cast_id, catalog=None):
    """The skill term of a hit that belongs to the fresh cast `cast_id` (events 4/9), as the
    client resolves it from its key slot: the cell of the caster's class/job whose family
    holds the cast, the lowest learned rank of that family (normally the cast itself), its
    Attribute / Attri_Atk / Skill_P_A. None: no cell (a plain strong hit). Vampiric Attack
    (job 2, family 0xB25) has no cell but replaces the total with 2 x the record's HP."""
    catalog = EC.items() if catalog is None else catalog
    char = char or {}
    cls, job = _cls(char.get('class')), _int(char.get('job2')) & 0xFF
    cast_id = _int(cast_id)
    learned = _learned(char)
    if _in(cast_id, VAMPIRIC_ATTACK):
        rid = learned_rank(VAMPIRIC_ATTACK[0], learned) if job == 2 else 0
        rec = catalog.get(rid) if rid else None
        return {'id': rid, 'p5': 9, 'vampiric_hp': rec.hp} if rec is not None else None
    cell = cell_for_skill(cast_id, cls, job)
    if cell is None:
        return None
    p5, fam, div = cell
    rid = learned_rank(fam, learned)
    rec = catalog.get(rid) if rid else None
    return _skill_term(rec, p5=p5, family=fam, div=div,
                       nova=(p5, cls, job) == NOVA_CELL,
                       delayed=(p5, cls, job) in DELAYED_CELLS)


def trap_skill(char, catalog=None):
    """The skill term of a Booby Trap catch (attacker +0x960 set: family 0xA31)."""
    catalog = EC.items() if catalog is None else catalog
    rid = learned_rank(BOOBY_TRAP_FAMILY, _learned(char))
    rec = catalog.get(rid) if rid else None
    return _skill_term(rec, family=BOOBY_TRAP_FAMILY) if rec is not None else None


# ---- the server's player: char record + session -> derive_player ---------------------------
def equipped_items(char, catalog=None, build=None):
    """The hii records of the regular equipment slots the derivation loops over (2008: grid
    slots 0..15, 2009: 0..14; the cash/costume slots add nothing)."""
    catalog = EC.items() if catalog is None else catalog
    build = build or getattr(catalog, 'client_build', None) or '2008'
    top = REGULAR_SLOTS.get(build, 16)
    out = []
    # one C call copies the live dict before the walk (an equip change on another thread must
    # not raise "dictionary changed size during iteration"; inventory.py's rule)
    for slot, entry in list(((char or {}).get('equipped') or {}).items()):
        if not 0 <= _int(slot, -1) < top:
            continue
        item_id = entry.get('id') if isinstance(entry, dict) else entry
        item = catalog.get(item_id) if _int(item_id) > 0 else None
        if item is not None:
            out.append(item_record(item))
    return out


def buff_mods(session, catalog=None):
    """The modifier records FUN_0041b830 adds from the holder's buffs: the 21 slots (+0xF24)
    minus the summon / aura ids it skips (0x9D9..0x9E3 only when self-cast), fairies once per
    attribute, then the six +0x111C slots (session['slot_buffs']) with no exclusion.

    Self-cast = slot +0x14 is the holder's uid. FUN_004262b0 (the S2C 0x3B insert, param_6 0)
    writes the holder's own uid there (0x426571), and the server keeps its own casts with
    src 0 (buffs.apply default), so src 0 is the holder too: Summon Lazy Sloth's Skill_P_D
    (-5..-23) never lowers its summoner's own Def."""
    catalog = EC.items() if catalog is None else catalog
    session = session or {}
    uid = session.get('uid') or session.get('account_id')
    out, fairy = [], set()
    for b in list(session.get('buffs') or [])[:SK.MAX_BUFF_SLOTS]:
        bid = _int(b.get('id') if isinstance(b, dict) else b)
        if not bid or not SK.slot_inserting(bid) or any(_in(bid, rg) for rg in BUFF_SKIP_RANGES):
            continue
        src = _int(b.get('src')) if isinstance(b, dict) else 0
        if _in(bid, BUFF_SKIP_OWN_RANGE) and (src == 0 or (uid is not None and src == _int(uid))):
            continue
        rec = catalog.get(bid)
        if rec is None:
            continue
        if _in(bid, FAIRY_RANGE) and 0 <= rec.attribute < 5:
            if rec.attribute in fairy:
                # a second fairy of one attribute: its P_A / P_D still add (0x41BFE9 adds
                # them before the attribute test), its A_A / A_D do not
                out.append({'skill_p_a': rec.skill_p_a, 'skill_p_d': rec.skill_p_d})
                continue
            fairy.add(rec.attribute)
        out.append(item_record(rec))
    for b in list(session.get('slot_buffs') or [])[:6]:
        rec = catalog.get(_int(b.get('id') if isinstance(b, dict) else b))
        if rec is not None:
            out.append(item_record(rec))
    return out


def evasion_mods(char, catalog=None):
    """Class 3 / 4: the learned Evasion rank (FUN_004281b0: the lowest one) adds its Skill_P_D
    (+2 at rank 1) and Skill_A_D - not its Skill_P_A (the derivation's tail reads only the
    record's +0x1BC and +0x1C4). [] for every other class or no Evasion learned."""
    catalog = EC.items() if catalog is None else catalog
    fam = EVASION_FAMILY.get(_cls((char or {}).get('class')))
    rid = learned_rank(fam, _learned(char)) if fam else 0
    rec = catalog.get(rid) if rid else None
    if rec is None:
        return []
    return [{'skill_p_d': rec.skill_p_d, 'skill_a_d': rec.skill_a_d, 'attribute': rec.attribute}]


def player_stats(char, session=None, catalog=None, build=None, pc=53):
    """derive_player for a server character (class, job2, level from exp, str/dex/int/spr,
    the regular equipment slots, the session's buffs, the Evasion passive)."""
    catalog = EC.items() if catalog is None else catalog
    build = build or getattr(catalog, 'client_build', None) or '2008'
    char = char or {}
    level = progression.level_for_exp(_int(char.get('exp')))
    base = tuple(_int(char.get(k)) & 0xFFFF for k in ('str', 'dex', 'int', 'spr'))
    st = derive_player(char.get('class'), level, base, equipped_items(char, catalog, build), pc=pc,
                       mods=buff_mods(session, catalog) + evasion_mods(char, catalog), build=build)
    st['job'] = _int(char.get('job2')) & 0xFF
    if session is not None and session.get('hp') is not None:
        st['hp'] = _int(session.get('hp'))
    return st


def buff_ids(session):
    """The victim's buff slot ids (Holy Protection / Magic Shield / Wicked Protection)."""
    return [_int(b.get('id') if isinstance(b, dict) else b) for b in list((session or {}).get('buffs') or [])]


def describe(st):
    """A short log text of derived stats."""
    return (f'Lv{st["level"]} Weak {st["weak"]} Strong {st["strong"]} Def {st["defense"]}'
            + (f' Body {st["body"]}' if st.get('body') else '')
            + (f' Guard {st["guard_def"]}' if st.get('guard_def') else '')
            + (f' elem {st["elem"]}:{st["elem_atk"]}' if st.get('elem') else ''))
