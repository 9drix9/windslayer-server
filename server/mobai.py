#!/usr/bin/env python3
"""
mobai.py - retail monster aggro: the hate list and the chase decisions, run by the server
=========================================================================================
(re_tools/docs/MONSTER_AGGRO_RE_2026-09-23.md; addresses EN 2009 first, 2008 in [brackets])

The field client never gives a monster a target. Both writers of the aggro target
+0xE44 [+0xDB4] - the hit apply FUN_004194f0 @0x41A815 [FUN_00418ac0 @0x419DE1] and the 2009
proximity scan @0x41930C - sit behind the host flag scene+0xF40 [+0xF24], which only the
CGameMain constructor ever stores (0 at 0x4128B3 [0x411FC3]), and no S2C handler writes it.
Retail ran that half of the monster engine on the SERVER and drove each mob with S2C
0x2A/0x9F command nodes, handing it back with 0x9E. This module is the engine's rules as pure
functions over plain values - nothing here sends a packet or takes a lock; GameServer does
both, under the session's combat lock:

    import mobai as AI
    flags = AI.Flags.of(tpl.ai)                        # hni AI: line -> the entity flags
    AI.add_hate(mob, uid, dmg, alive)                  # hate list + target rule 0x41A737..0x41A815
    d = AI.decide((mob.x, mob.y), player_xy, mob.ai_dir, flags, rng_bit=1)
    AI.lo(d.direction, d.motion)                       # the lo word of a 0x2A / 0x1B command

THE COMMAND WORD. S2C 0x2A/0x9E/0x9F share handler 0x459C3D [0x454E6D]; it decodes the 8-byte
blob into a 0x58-byte queue node: lo bits 0-1 the direction (node+0x4A: 1 -> 2 left, 2 -> 6
right, else 8), lo bits 2-4 the motion (node+0x4B), which the consumer 0x4129F0 [0x412100]
pops into +0x93F and +0x940 - the same two inputs the client's own chase branch writes. So
lo = direction | motion << 2 (walk left 01 / right 02, jump 0D/0E, drop 11/12, attack A
05/06, attack B 15/16, dash 19/1A, stop 00). Bits 12-19 (action, reaction) are never set and
hi stays 0: with hi = 0 the 0x2A self-form holds exactly 960 ms (0x459F04 [0x455136]).
"""
from collections import namedtuple

# ---------------------------------------------------------------- the input word ---
DIR_LEFT, DIR_RIGHT = 1, 2                  # lo bits 0-1 (node+0x4A -> +0x93F = 2 / 6)
MOTION_WALK = 0                             # +0x940 = 0 with a direction: walk that way
MOTION_ATTACK_A = 1                         # 0x41916E: +0x940 = 1 (attack A, state 4)
MOTION_JUMP = 3                             # 0x418F4D / 0x418FAE
MOTION_DROP = 4                             # 0x418F88 / 0x418E73 (down through the floor)
MOTION_ATTACK_B = 5                         # 0x41916E: +0x940 = 5 (attack B, state 1)
MOTION_SKILL = 6                            # 0x41919A: +0xE5C set -> +0x940 = 6 (dash)
STOP = 0                                    # direction 8, motion 0: a fully idle node
ATTACK_MOTIONS = (MOTION_ATTACK_A, MOTION_ATTACK_B)
# Motions that carry the mob sideways at its walk speed (dead reckoning without the driver).
MOVING_MOTIONS = (MOTION_WALK, MOTION_JUMP, MOTION_DROP, MOTION_SKILL)
LO_MASK = 0x1F                              # bits 0-4 only: never action/reaction (12-19)

# ------------------------------------------------ the chase branch's constants ---
# 0x418C86..0x4191A6 [0x4184A4..0x4187B7]; every value read from the exe's .rdata.
FACE_DX = 65.0                  # @0x52EFD8: |dx| > 65 turns toward the target, else keep
DROP_FAR_DY = 140.0             # @0x52EFD0: target > 140 px lower ...
DROP_FAR_DX = 200.0             # @0x52EE08: ... and |dx| < 200 (|dx| > 65 branch)
DROP_NEAR_DY = 20.0             # @0x52ED20: target > 20 px lower ...
DROP_NEAR_DX = 40.0             # @0x52EFC8: ... and |dx| < 40 (|dx| <= 65 branch)
LEASH_PX = 600.0                # @0x52EFC0: the client wander's own home leash
PROXIMITY_PX = 200.0            # @0x52EE08 again: the 2009 AI[5] scan box (0x4192A6..0x4192F0)
# The client compares the two f64 y's with no margin (0x418F0E / 0x418F91); the server's
# positions are 0.25 s memory samples or interact tails, so "higher" needs this much.
JUMP_MIN_DY = 10.0
# The attack test is FUN_0041dc70's attack rect against the target's body rect (0x419114 /
# 0x41912C); the .hsi rects are not ported, so a box in front of the mob stands in for it.
ATTACK_REACH_X = 60.0
ATTACK_REACH_Y = 40.0
# Hysteresis of that box (server-side only; the client re-tests its rects once per 990 ms
# decision, the server every 0.3 s tick on positions it partly dead-reckons): a swing that
# started at ATTACK_REACH_X keeps going until the target is more than this much further in
# front. One 0.3 s tick of the commanded walk is 82.5 px/s x 0.3 s ~ 25 px, which is how far
# the server's x of a mob that walked to the reach edge swings per tick (P7 live L2 "Related":
# 36 walk/attack flips in 21 s); 30 px covers that plus sample noise.
ATTACK_HOLD_X = 30.0
# ... and it holds for about one swing only: the client re-decides every 990 ms (MONSTER_AGGRO_RE
# T4), so a swing lasts one decision. The hold is spatial AND timed: a commanded attack does
# not move the mob (only MOVING_MOTIONS are dead-reckoned) and nothing fixes its position
# while nothing hits, so a hold without a time limit parked the mob swinging at air for good
# once its target stood 1..30 px beyond reach (P7 live L2 review). After ATTACK_HOLD_SECS the
# plain box decides again: the mob walks in and starts a new swing at reach - at the edge at
# most one flip pair per ~1 s instead of one flip per 0.3 s tick.
ATTACK_HOLD_SECS = 1.0
# Hold of the 0x2A self-form node (0x459F04: 960 - min(+0xE3C, hi12), hi = 0).
HOLD_2A_SECS = 0.96

# ----------------------------------------------------- the template AI flags ---
# hni `AI:` line: 13 ints in 2009, 10 in 2008. FUN_00409150 stores them at template+0x254
# [+0x250]; the 0x1A handler copies them onto the entity (REP MOVSD, 13 dwords to +0xE50 at
# 0x456C99 [10 dwords to +0xDC0 at 0x451F67]).
AI_INTS = 13
AI_ATTACK_A = 0                 # +0xE50 [+0xDC0]
AI_SKILL = 3                    # +0xE5C: dash
AI_PROXIMITY = 5                # +0xE64: aggro on a player within 200 x 200 px (2009 only)
AI_COUNTER_JUMP = 6             # +0xE68: jump when the target swings toward it (not modelled)
AI_NO_JUMP = 7                  # +0xE6C [+0xDDC]
AI_ATTACK_B = 8                 # +0xE70
AI_STATIONARY = 9               # +0xE74 [+0xDE4]: never chases (0x418C86 wanders instead)


def pad_ai(ai):
    """The AI ints padded (or cut) to the 2009 entity's 13 dwords."""
    ints = [int(v) for v in (ai or ())][:AI_INTS]
    return tuple(ints + [0] * (AI_INTS - len(ints)))


class Flags(namedtuple('Flags', 'attack_a skill proximity counter_jump no_jump attack_b stationary')):
    """The entity flags the chase branch reads, from a template's AI ints."""
    __slots__ = ()

    @classmethod
    def of(cls, ai):
        a = pad_ai(ai)
        return cls(bool(a[AI_ATTACK_A]), bool(a[AI_SKILL]), bool(a[AI_PROXIMITY]),
                   bool(a[AI_COUNTER_JUMP]), bool(a[AI_NO_JUMP]), bool(a[AI_ATTACK_B]),
                   bool(a[AI_STATIONARY]))

    @property
    def attacks(self):
        return self.attack_a or self.attack_b


def lo(direction, motion):
    """The 0x2A / 0x1B lo word: direction | motion << 2 (bits 0-4 only)."""
    return ((int(direction) & 0x3) | (int(motion) & 0x7) << 2) & LO_MASK


def direction_of(word):
    return int(word) & 0x3


def motion_of(word):
    return (int(word) >> 2) & 0x7


def is_attack(word):
    return direction_of(word) != 0 and motion_of(word) in ATTACK_MOTIONS


MOTION_NAMES = {MOTION_WALK: 'walk', MOTION_ATTACK_A: 'attack A', MOTION_JUMP: 'jump',
                MOTION_DROP: 'drop', MOTION_ATTACK_B: 'attack B', MOTION_SKILL: 'dash'}


def describe(word):
    """'walk left', 'attack B right', 'stop' ... for the log."""
    side = {DIR_LEFT: 'left', DIR_RIGHT: 'right'}.get(direction_of(word))
    if side is None:
        return 'stop' if motion_of(word) == MOTION_WALK else f'motion {motion_of(word)} in place'
    return f'{MOTION_NAMES.get(motion_of(word), f"motion {motion_of(word)}")} {side}'


def facing_toward(mob_x, target_x):
    return DIR_RIGHT if float(mob_x) <= float(target_x) else DIR_LEFT


# ------------------------------------------------------------------ hate list ---
def add_hate(mob, uid, dmg, alive):
    """FUN_004194f0 0x41A737..0x41A815 [0x419CEB..0x419DE1] on the server: the attacker's
    entry in the damage list +0x1410 grows by `dmg` (a new attacker is appended), the top
    damager +0x1420 is the FIRST entry with the greatest total (the scan only replaces on a
    strictly greater one), and aggro +0xE44 = attacker when he is that top damager or the top
    damager is gone or dead (FUN_00419450 finds nothing, or state 0x10/0x16). `alive(uid)`
    answers the last question. Returns True when the target changed."""
    uid = int(uid)
    mob.hate[uid] = mob.hate.get(uid, 0) + max(0, int(dmg))
    top = max(mob.hate, key=mob.hate.__getitem__)
    if top == uid or not alive(top):
        changed = mob.aggro_uid != uid
        if not mob.aggro_uid:
            mob.aggro_x = getattr(mob, 'x', 0.0)   # the chase leash counts from here
        mob.aggro_uid = uid
        return changed
    return False


# -------------------------------------------------------------- the decisions ---
Decision = namedtuple('Decision', 'direction motion')


def decide(mob_xy, target_xy, ai_dir, flags, *, rng_bit=0, prev_motion=None,
           reach_x=ATTACK_REACH_X, reach_y=ATTACK_REACH_Y, jump_dy=JUMP_MIN_DY, hold_x=0.0,
           swing_secs=None, hold_secs=ATTACK_HOLD_SECS):
    """One pass of the client's chase branch 0x418EA0..0x41919A [0x4184BE..0x4187AB] for a
    mob at `mob_xy` chasing a target at `target_xy` (screen y grows downward). Returns the
    (direction, motion) it would write into +0x93F / +0x940:

    - |dx| > 65: face the target (0x418EFE/0x418F07: right when mob.x < target.x); jump when
      the target is higher and AI[7] is 0 (0x418F4D); drop when it is more than 140 px lower
      with |dx| < 200 (0x418F88).
    - |dx| <= 65: keep the old direction (`ai_dir`) - a contact-only mob walks through the
      target and turns back once past 65 px; jump when higher (0x418FAE), else drop when more
      than 20 px lower with |dx| < 40 (0x418FD0 -> 0x418F88).
    - attack (AI[0] / AI[8], 0x419097..0x41916E): A when AI[0] and (rng bit or no AI[8]),
      else B; it overrides jump/drop when the target stands in the reach box in front of the
      mob (FUN_0041dc70's rect overlap). An attack already running keeps its kind, so the rng
      does not flip A/B every tick.
    - hysteresis (server-side, `hold_x`, ATTACK_HOLD_X): an attack STARTS with the target
      within `reach_x`, and one already running (`prev_motion` an attack, no turn this
      decision - a turn aims a new swing) keeps going while the target stays within
      `reach_x + hold_x` in front - but only for `hold_secs` (ATTACK_HOLD_SECS, about one
      client decision) after that attack word was first commanded: `swing_secs` is its age
      (None: unknown - no hold). Past that the plain box decides, so a target parked just
      beyond reach gets walked after instead of swung at forever. The facing, jump, drop and
      dash rules above are the client's, unchanged. hold_x 0 = the plain box every tick.
    - dash (AI[3], 0x419180): +0x940 = 6 when nothing else was decided.
    The counter-jump (AI[6], 0x418FDA) reads the target's swing state and is not modelled."""
    mx, my = float(mob_xy[0]), float(mob_xy[1])
    tx, ty = float(target_xy[0]), float(target_xy[1])
    adx = abs(tx - mx)
    higher = my - ty > jump_dy
    motion = MOTION_WALK
    if adx > FACE_DX:
        direction = DIR_RIGHT if mx < tx else DIR_LEFT
        if higher and not flags.no_jump:
            motion = MOTION_JUMP
        if ty - my > DROP_FAR_DY and adx < DROP_FAR_DX:
            motion = MOTION_DROP
    else:
        direction = ai_dir if ai_dir in (DIR_LEFT, DIR_RIGHT) else facing_toward(mx, tx)
        if higher:
            if not flags.no_jump:
                motion = MOTION_JUMP
        elif ty - my > DROP_NEAR_DY and adx < DROP_NEAR_DX:
            motion = MOTION_DROP
    if flags.attacks:
        if prev_motion == MOTION_ATTACK_A and flags.attack_a:
            use_a = True
        elif prev_motion == MOTION_ATTACK_B and flags.attack_b:
            use_a = False
        else:
            use_a = flags.attack_a and (bool(rng_bit) or not flags.attack_b)
        ahead = (tx - mx) * (1 if direction == DIR_RIGHT else -1) >= 0
        swinging = (prev_motion in ATTACK_MOTIONS and direction == ai_dir
                    and swing_secs is not None and float(swing_secs) < float(hold_secs))
        reach = reach_x + (max(0.0, float(hold_x)) if swinging else 0.0)
        if ahead and adx <= reach and abs(ty - my) <= reach_y:
            motion = MOTION_ATTACK_A if use_a else MOTION_ATTACK_B
    if flags.skill and motion == MOTION_WALK:
        motion = MOTION_SKILL
    return Decision(direction, motion)


def in_proximity(mob_xy, target_xy, box=PROXIMITY_PX):
    """The 2009 AI[5] scan 0x4192AC..0x4192F0: |dx| < 200 and |dy| < 200 (strict)."""
    return (abs(float(target_xy[0]) - float(mob_xy[0])) < box
            and abs(float(target_xy[1]) - float(mob_xy[1])) < box)


def clear(mob):
    """Forget the aggro state without a packet (kill, respawn, release)."""
    mob.aggro_uid = 0
    mob.hate = {}
    mob.ai_lo = -1
    mob.ai_sent_t = 0.0
    mob.ai_hit_t = 0.0
    mob.ai_hold_end = 0.0
    mob.ai_attack_t = 0.0
    mob.ai_attack_a_t = 0.0         # P13 boss-b3: the last attack A / attack B commanded, so a
    mob.ai_attack_b_t = 0.0         # swing event hurts only after its own kind (bosses.swing_age)
    mob.ai_attack_start_t = 0.0
    mob.ai_recover_until = 0.0      # desync fix M1: no hurt gate left over
    mob.ai_stun_until = 0.0         # ... and no stun end for the dead reckoning
    mob.ai_ice_until = 0.0
    mob.ai_ice_chase_t = 0.0
    mob.ai_stun_t = 0.0             # ... nor a stun whose rest the next word starts
    mob.ai_stun_secs = 0.0
    mob.ai_owned = False
    mob.ai_takers = set()           # 0x9E / 0x29 / 0x1A cleared +0x971 on every client
    mob.aggro_x = 0.0
