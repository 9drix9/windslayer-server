#!/usr/bin/env python3
"""
combat.py - skill hitboxes, damage, the death penalty and revive points
======================================================================
(cs-skill-damage, cs-traps, cs-player-death; combat_skill.md F3a / F7 / F8, 3.4 / 3.5)

Pure functions over plain values - nothing here sends a packet or takes a lock; the
GameServer methods of P3 stage 4 do that. Every function takes its inputs explicitly so the
offline tests can pin them:

    import combat as C
    box = C.hitbox(sd.family)                                  # Hitbox(front=150, back=0, half_height=60)
    C.targets(mobs.values(), (1300, 714), C.FACING_RIGHT, box) # alive mobs in front, nearest first
    C.skill_damage(sd, C.attack_power(char), mob.defense, buff_p_a)   # DAMAGE_FORMULA 'placeholder'
    C.death_penalty(char['exp'])                               # FUN_0041a230's exp loss
    C.revive_point(102, EC.portals(), start=(101, 1411.0, 714.0))   # (101, 1405.0, 714.0)

WHO RESOLVES A SKILL HIT. The EN basic attack and every skill cast carry no target (C2S 0x15
is just the id, combat_skill.md finding 3), and in field mode the client's own hit math
(FUN_00418ac0) never writes HP. So an attack skill is resolved here, from the caster's last
known position and facing (the dev combat driver's +0x11F8/+0x1288/+0x8BD reads, C2S 0x0D
interact tails and state bits) against SKILL_HITBOX. The box numbers are the design's
placeholders (combat_skill.md 3.4, open question 8) until the .hsi collision rects are ported.

THE DAMAGE NUMBERS are the client's own formula since the damage-formula port: damage.py
(FUN_0041b830 / FUN_004194f0 [2008 FUN_0041ace0 / FUN_00418ac0]), config DAMAGE_FORMULA
'client'. attack_power / defense_power / skill_damage / body_damage below are the old
placeholder rules, kept for DAMAGE_FORMULA 'placeholder' (rollback); dot_damage serves both
(DoT ticks are not part of FUN_004194f0).
"""
from collections import deque, namedtuple

import en_content as EC
import progression

FACING_LEFT, FACING_RIGHT = -1, 1

Hitbox = namedtuple('Hitbox', 'front back half_height')
# combat_skill.md 3.4: default 150 px in front, nothing behind, +-60 px vertically; the wide
# sweeps reach both sides. Keyed by family base id (skills.SkillDef.family).
DEFAULT_HITBOX = Hitbox(150, 0, 60)
SKILL_HITBOX = {
    0x8AF: Hitbox(300, 300, 120),     # Crescent Slash (family 2223)
    0x8DB: Hitbox(300, 300, 120),     # Heaven Strike (family 2267)
}

# Booby Trap trigger (F7 step 4). The client's own test is GetColRect(sprite 0xE1, x, y, 0x1E,
# 2) against the victim's body box (FUN_00416380 @0x417BC0); the server only has points, so a
# reported victim must stand within TRAP_VICTIM_RADIUS of the trap (anti-forge), and with no
# report the victim is the nearest live monster inside +-40 x +-30 px of the trap point.
TRAP_VICTIM_RADIUS = 120.0
TRAP_FALLBACK_HALF_W = 40.0
TRAP_FALLBACK_HALF_H = 30.0

# FUN_0041a230 (client-detected death, asm 0x41A29C..0x41A3CB): for 10 <= level < 99 and no
# "Waive EXP Penalty" (item 1891 in the +0x1364 list, FUN_00426d10) the client subtracts
#     min(exp into the level (FUN_00427d10), _ftol2(FILD [0x6F0EAC + 4*level] * rate))
# where [0x6F0EAC + 4*L] is EXP_INC[L-1] (the span of the current level) and the rate is a
# double picked by level bracket. Values read from WindSlayer.exe (they are float32 constants
# widened to double, hence the tails). combat_skill.md open question 12 answered.
PENALTY_LEVEL_MIN = 10                  # CMP BL,0xA / JC
PENALTY_LEVEL_END = 99                  # CMP BL,0x63 / JNC (level 99 has no penalty)
PENALTY_RATES = (
    (20, 0.05000000074505806),          # [0x6F8C98]  level < 20
    (30, 0.05999999865889549),          # [0x6F8D98]  level < 30
    (40, 0.07000000029802322),          # [0x6F8D90]  level < 40
    (50, 0.07999999821186066),          # [0x6F8D88]  level < 50
    (60, 0.09000000357627869),          # [0x6F8D80]  level < 60
    (None, 0.10000000149011612),        # [0x6F8D78]  60..98
)
WAIVE_EXP_PENALTY_ITEM = 1891           # 0x763 (not modelled: the +0x1364 list is cash state)

# The nine towns are the maps with a portal to the flea market 9701 (en_maps.py: "the nine
# towns -> 9701"); the death dialog reads "Please click the option to resurrect in town."
FLEA_MARKET_MAP = 9701


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ facing ---
def facing_from_state(state_lo):
    """C2S 0x0D state_lo bits 0-1 = +0x8B3 (01 = value 2 = left, 10 = value 6 = right, 00 =
    none; spec 0x42CE94/0x0D). None when no direction is held (keep the last one)."""
    bits = _int(state_lo) & 0x3
    return FACING_LEFT if bits == 1 else FACING_RIGHT if bits == 2 else None


def facing_from_entity(value_8bd):
    """Entity +0x8BD, the facing the client keeps after the key is released (2 = left, 6 =
    right; RegisterLocalPlayer seeds 2, FUN_00416380 compares it between attacker and victim).
    None for any other value."""
    value = _int(value_8bd) & 0xFF
    return FACING_LEFT if value == 2 else FACING_RIGHT if value == 6 else None


# ------------------------------------------------------------------ hitbox ---
def hitbox(family):
    return SKILL_HITBOX.get(_int(family), DEFAULT_HITBOX)


def in_hitbox(origin, facing, point, box):
    """True when `point` is inside `box` laid in front of `origin` facing `facing`."""
    ox, oy = _num(origin[0]), _num(origin[1])
    px, py = _num(point[0]), _num(point[1])
    ahead = (px - ox) * (FACING_LEFT if facing == FACING_LEFT else FACING_RIGHT)
    return -box.back <= ahead <= box.front and abs(py - oy) <= box.half_height


def targets(mobs, origin, facing, box):
    """The live monsters inside the box, nearest first (ties by uid, so the order is
    deterministic)."""
    ox, oy = _num(origin[0]), _num(origin[1])
    hit = [m for m in mobs if m.alive and in_hitbox(origin, facing, (m.x, m.y), box)]
    return sorted(hit, key=lambda m: (abs(m.x - ox) + abs(m.y - oy), m.uid))


def nearest(mobs, point, half_w=None, half_h=None):
    """The live monster nearest `point`, optionally only inside +-half_w x +-half_h."""
    px, py = _num(point[0]), _num(point[1])
    best, best_d = None, None
    for m in mobs:
        if not m.alive:
            continue
        dx, dy = abs(m.x - px), abs(m.y - py)
        if (half_w is not None and dx > half_w) or (half_h is not None and dy > half_h):
            continue
        d = dx + dy
        if best is None or d < best_d or (d == best_d and m.uid < best.uid):
            best, best_d = m, d
    return best


def distance(a, b):
    return ((_num(a[0]) - _num(b[0])) ** 2 + (_num(a[1]) - _num(b[1])) ** 2) ** 0.5


# ------------------------------------------------------------------ damage ---
def _equipped_items(char, catalog):
    for entry in ((char or {}).get('equipped') or {}).values():
        item_id = entry.get('id') if isinstance(entry, dict) else entry
        item = catalog.get(item_id) if item_id else None
        if item is not None:
            yield item


def attack_power(char, catalog=None):
    """STR + the W_Att of every equipped item: the basic-attack placeholder (DAMAGE_FORMULA
    'placeholder'; the client's own attack is damage.derive_player)."""
    catalog = EC.items() if catalog is None else catalog
    return _int((char or {}).get('str')) + sum(_int(i.w_att) for i in _equipped_items(char, catalog))


def defense_power(char, catalog=None):
    """Sum of the equipped items' Def column (the placeholder's player defense; the client
    scales it by SPR and class, damage.derive_player)."""
    catalog = EC.items() if catalog is None else catalog
    return sum(_int(i.defense) for i in _equipped_items(char, catalog))


def skill_damage(sd, attack, defense, buff_p_a=0):
    """combat_skill.md 3.5 placeholder: the caster's attack plus the running buffs' Skill_P_A,
    scaled by the skill's own Skill_P_A (the damage bonus of Con-0 attack skills, e.g. Double
    Attack 15) and its element power Attri_Atk, minus the target's Def; never below 1."""
    atk = _int(attack) + _int(buff_p_a)
    raw = int(atk * (1.0 + (_int(sd.skill_p_a) + _int(sd.attri_atk)) / 100.0))
    return max(1, raw - _int(defense))


def dot_damage(sd, level):
    """One DoT tick (every 990 ms, FUN_00417e10 slot % 0x3DE): max(1, |HP| * (1 + level/20))."""
    return max(1, int(abs(_int(sd.hp)) * (1.0 + _int(level) / 20.0)))


def body_damage(body_atk, defense):
    """A monster's hit on the player (placeholder): its stat - defense, never below 1."""
    return max(1, _int(body_atk) - _int(defense))


# --------------------------------------------------------- death penalty ---
def penalty_rate(level):
    for top, rate in PENALTY_RATES:
        if top is None or level < top:
            return rate
    return PENALTY_RATES[-1][1]


def death_penalty(exp, waived=False):
    """The exp a death costs, as FUN_0041a230 computes it for the client-detected death:
    0 below level 10, at level 99 or when waived; else min(exp into the level, the level
    span x the bracket rate, truncated). The min means a death never takes a level away."""
    exp = progression.clamp_exp(exp)
    level = progression.level_for_exp(exp)
    if waived or not PENALTY_LEVEL_MIN <= level < PENALTY_LEVEL_END:
        return 0
    into = exp - progression.exp_for_level(level)
    span = progression.EXP_INC[level - 1]
    return max(0, min(into, int(span * penalty_rate(level))))


# ---------------------------------------------------------- revive points ---
def _edges(portals):
    """{source map: [(destination, x, y, key)]} from the portal table rows."""
    graph = {}
    for key, row in (portals or {}).items():
        try:
            src, idx = (int(p) for p in str(key).split('_', 1))
            dst, x, y = int(row[0]), float(row[1]), float(row[2])
        except (TypeError, ValueError, IndexError):
            continue
        graph.setdefault(src, []).append((dst, x, y, (src, idx)))
    for rows in graph.values():
        rows.sort(key=lambda r: (r[3][1], r[0]))
    return graph


def entry_point(map_code, portals):
    """(x, y) where the first portal from ANOTHER map into `map_code` lands (lowest source
    map, then index), or None when no portal leads there. A town revives here, and a dev
    `!warp` with no point arrives here (shop_storage-flea-warp: 9701 -> (1500, 2168) through
    201_178, the EN arrival rule on the market's exit-portal line, where every town's
    flea-market portal lands)."""
    map_code = _int(map_code)
    into = sorted((src, key[1], x, y) for src, rows in _edges(portals).items() if src != map_code
                  for dst, x, y, key in rows if dst == map_code)
    return (into[0][2], into[0][3]) if into else None


def towns(portals):
    """The town maps: every map with a portal to the flea market 9701."""
    return {src for src, rows in _edges(portals).items()
            if any(dst == FLEA_MARKET_MAP for dst, _x, _y, _k in rows)}


def revive_point(map_code, portals, *, start, overrides=None):
    """(map, x, y) a player who died on `map_code` revives at (REVIVE_POINT, F8 step 8).

    1. an override for the map (config REVIVE_POINTS {"<map>": [map, x, y]}) wins;
    2. a revive town is the start map (config START_MAP, the novice park 101) or one of the
       nine towns. Dying in one revives there: the start map at the start point, a town at
       the arrival point of its first portal from another map (lowest source map / index);
    3. otherwise the nearest revive town by portal hops (breadth first over the EN portal
       table, rows in index order, so the choice is deterministic), arriving where that
       portal lands - 102 -> 101 at (1405, 714) through 102_31;
    4. a map with no route (an instance, a map missing from the table) revives at the start.
    """
    map_code = _int(map_code)
    start = (int(start[0]), float(start[1]), float(start[2]))
    for key in (str(map_code), map_code):
        row = (overrides or {}).get(key)
        if row is not None and len(row) >= 3:
            return int(row[0]), float(row[1]), float(row[2])
    graph = _edges(portals)
    revive_towns = towns(portals) | {start[0]}

    def point_in(town):
        if town == start[0]:
            return start
        point = entry_point(town, portals)
        return (town, point[0], point[1]) if point is not None else start

    if map_code in revive_towns:
        return point_in(map_code)
    seen, queue = {map_code}, deque([map_code])
    while queue:
        cur = queue.popleft()
        for dst, x, y, _key in graph.get(cur, ()):
            if dst in seen:
                continue
            if dst in revive_towns:
                return dst, x, y
            seen.add(dst)
            queue.append(dst)
    return start
