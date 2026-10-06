#!/usr/bin/env python3
"""
hitstun.py - the hurt, stun and slide of a client-caught hit on a monster, per hit kind
=======================================================================================
(re_tools/docs/HIT_STUN_PER_SWING_RE_2026-09-28.md; EN 2009 client, static RE; desync fix
M1 per-swing hurt, M3 skill-hit knockback. A 2008 server sends the same numbers UNVERIFIED:
its 0x2A grammar matches (protocol_spec, action_flag for 9 / 10), but nobody has read how the
2008 pass 2 uses +0x98C, its body001.hsi lengths or its variant table, and its cast pose
comes from the S2C 0x25 accept, not from sending 0x15. The live server runs the 2009 build.)

When the attacker's own client catches a hit, FUN_00412c60 pass 1 sets the monster's hurt
+0x9E4 to what is LEFT of the attacker's current action at the hit tick:

    hurt = L - E        L = FUN_00422710(attacker), the total ms of his action
                        E = his elapsed time +0xE9C at the hit (the client uses L if E > L)

and pass 2 / state 3 turn it into the stun and the slide by the report tail's
target_action_event (tae, the monster's +0x9DC):

    tae 7 / 8 (swing)          +0xE9C starts at 0x3C: slide 2 ticks, stun hurt - 60 ms
    tae 9 / 10 (skill, strong) +0xE9C starts at 0:    slide 4 ticks, stun hurt ms
    element +0x95D (tae 9/10 only, FUN_004194f0 from the attacker's learned skill):
        2 ice  +1000 ms of stun (the frozen look)
        4 wind the slide runs while +0xE9C < 600: 19 ticks (x 2.5 for Mage tier-1 Nova)
    tae 8 / 10: the monster was airborne and is launched (state 0x17) - same stun, the
        slide is not modelled (the server keeps the fix at the hit point).

L (FUN_00422710, the jump tables at 0x422E78..; hs/body001.hsi sprite n = state n, its
length the sum of its frame delays):

    combo (state 4), weapon type 0/1/2/4: 610 / 1250 / 2140 for stage 0 / 1 / 2 (+0x979,
        which goes on only while the attack key is down at E 420..600 / 1080..1230, else the
        stage ends at its L; FUN_00422710 reads the stage, not E: at a hit L is the stage
        the tracker reached); weapon 3: 720, 5/6: 740
    skill / strong attack (state 1): skill_len(weapon type, cast variant, tier); the strong
        attack is variant 0

E, from the attacker's own C2S 0x0D stream (tick order FUN_0042c920: the builder sends
before the state machine adds its 30 ms, and a hit caught on tick N is reported on N+1):

    E = E0 + sum(logic_elapsed_ms of the packets after the action's start packet P, up to
        and including the hit report H) - 30

    E0 = 0 from standing; a dash attack (motion 1 right after motion-6 words, <= 580 ms into
    the dash) enters state 4 with +0xE9C = 1380 (weapon 0/1/2/4; 180 bow, 200 staff); a dash
    strong attack (motion 5 after motion 6) 150 / 120 / 210 / 270.

While the attacker is in state 1 (a cast or strong attack, E < L), FUN_00414210 ignores
motion 1 / 5 / 6 (its motion cases act only in states 8 / 0xC / 6), but his client still
sends them: FUN_0042c310 writes scene+0x259 = 1 on every tick the attack key is down (its
only guard, +0x259 != 7, covers the 60 ms cast pose). So those words do not start a new
action here either (in_state_1). A new cast's words always do: FUN_0044f070 lets a skill
start only in states 8 / 0xC / 6.

A monster's hit on the attacker himself (his own 0x0D action nibble 6..10, the hi12 of that
packet its ms) puts him in state 3: it ends his action and its combo stage, and the words he
sends in that hurt start nothing. The input still held when it ends (hurt_len) starts its
action there, at stage 0 / E 0, E then by the timeline from that start (the kind's
first-hit default only when the hit came before it). The dash end (state 7) holds an attack
the same way. An airborne hurt (8 / 10, state 0x17) turns him and starts no ground action.
Live 2b: the old anchor gave 280 / 290 / 250 / 410 / 780 or the 760 'held' default where A's
copy had 490 / 500 / 1020 (and 460: f1 10.34, E 150).

The end of an action works like the end of that hurt (live 2c). The state machine's tick
(FUN_00414210) adds its 30 ms to +0xE9C first (0x4142DB), then runs the motion case (motion
1 starts state 4 only in 8 / 0xC / 6), then the state's own case: state 4's combo check
(0x415880..0x41592D) and its end test (E >= L -> state 8, E 0, stage 0: 0x4159E2..0x415A02)
both read E after that tick's += 30, and so does state 1's (0x415AD8). So an action ends
ON the tick whose E reaches L (_end_tick), an attack key down on that tick meets the motion
case while he is still in state 4 / 1 (it starts nothing), and the key down on the NEXT
tick starts a fresh stage-0 swing there (E 0) - after any action: a swing at any stage, a
dash attack, a cast, a strong attack, a bow / staff shot (_roll_over). Live 2c: taps at
0 / 480 / 1120 ms put the third on the tick E 630 of the stage-0 swing the second began
(E 600 before the += 30): that swing ended there and the key still down 30 ms later began
a new one (t2 / t3 / t5: 490 on A's copy where the old anchor ran on to E 780 and read
stage 1 from it, 470); an attack key pressed in a dash attack and held past its end (E
2160) began a stage-0 swing 30 ms after it (f1 65.79 / 66.42: 490 / 500 where the old
tracker had 'held', 760).

The tracker also follows his facing +0x949 (the knockback direction): a direction key
turns him only on the ticks he is in 8 / 0xC / the air (report_facing), and his combo
stage: a stage goes on only when the attack key is down inside its window (COMBO_ADVANCE).

Live session 2 (livefix 9ee51aa) measured the attacker's copy of a Monkey Soldier:
basic swing 490 = 610 - 120, dash attack 760 = 2140 - 1380, Ice Spear 1020 = 1290 - 270
(+1000 ice: 2020 ms in state 3). Those are also the defaults when E is not known, and a
guess errs HIGH: a low hurt lets the chase in before the attacker's copy stands (the early
chase offset), a high one only holds the chase a little longer on every copy.

Everything here is pure: the server keeps one tracker dict per session (observe() on every
accepted C2S 0x0D) and asks classify() at a hit report. Nothing sends or locks.
"""
from dataclasses import dataclass

# --- C2S 0x0D state words (presence.py has the same decoding) -----------------------------
LO_MASK = 0x003FFFFF
MOTION_ATTACK, MOTION_STRONG, MOTION_DASH, MOTION_CAST = 1, 5, 6, 7
MAX_STEP_MS = 5000          # a longer logic_elapsed_ms is a stall, not an action
MS_CAP = 60000              # an anchor older than this is dead anyway

# --- interact events / target action events -----------------------------------------------
SWING_EVENT, SKILL_EVENT = 7, 9
TAES = {SWING_EVENT: (7, 8), SKILL_EVENT: (9, 10)}
AIRBORNE_TAES = (8, 10)

# --- the action length L (FUN_00422710; body001.hsi totals) -------------------------------
COMBO_ENDS = (610, 1250, 2140)          # state 4 stage 0 / 1 (fixed in code), 2 (T[4])
# State 4 goes on to the next stage on a tick the attack key is down while +0xE9C is
# strictly inside (0x19A, 0x262) for stage 0, (0x41A, 0x4E2) for stage 1 (0x41588x..0x41592D).
COMBO_ADVANCE = ((0x19A, 0x262), (0x41A, 0x4E2))
SHOT_LEN = {3: 720, 5: 740, 6: 740}     # T[39] bow, T[35] staff: one shot, no stage
# Tentative (report 1.5, not verified): the effect-sprite lengths of Monk Weakness Detection
# (weapon 2, variant 5: basic.hsi 0xA4) and Mage tier-1 Nova (weapon 5, variant 7: 0xFF).
FX_WEAKNESS_DETECTION_MS = 710
FX_NOVA_MS = 1490


def skill_len(wt, variant, tier):
    """L of state 1 (a skill cast or, with variant 0, the strong attack) for weapon type
    `wt` (+0x11A: 0 none, 1 Warrior .. 6 Priest), the cast variant (+0x98B) and the tier
    (+0x119), at the hit (+0x965 is set just before, 0x41385D)."""
    wt, v, t = int(wt or 0), int(variant or 0), int(tier or 0)
    if wt == 1:
        return 1040 if v == 5 else 1290                    # T[23] / T[1]
    if wt == 2:
        if v == 5:
            return FX_WEAKNESS_DETECTION_MS
        if v == 6:
            return 1640 if t == 1 else 1200                # 0x56E + 0xFA / 0x4B0
        if v == 8 and t == 1:
            return 3300                                    # 0xBEA + 0xFA
        if v == 9 and t == 2:
            return 600
        return 1240                                        # T[30]
    if wt == 3:
        if v == 8 and t == 1:
            return 1740                                    # T[38] + 350
        if v == 10 and t == 1:
            return 2090                                    # T[38] + 700
        return 1390                                        # T[38]
    if wt == 4:
        return 700 if (v == 5 and t == 1) else 1240        # T[26]
    if wt == 5:
        return FX_NOVA_MS if (v == 7 and t == 1) else 1490  # T[34]
    if wt == 6:
        return 1490                                        # T[34]
    return 1290                                            # T[1]


def swing_len(wt, e):
    """The smallest state-4 L above elapsed `e`: the stage end E alone allows for the combo
    weapons, the single shot for bow / staff; None when `e` is past the whole action. Only a
    bound: FUN_00422710 reads the stage +0x979, and classify takes the stage the tracker
    reached (_action_end) - E past that stage's L is a stale anchor, not the next stage."""
    wt = int(wt or 0)
    if wt in SHOT_LEN:
        return SHOT_LEN[wt] if e < SHOT_LEN[wt] else None
    for end in COMBO_ENDS:
        if e < end:
            return end
    return None


# E0 of an action entered from the dash (FUN_00414210 motion case 1 / 5 in state 6).
DASH_SWING_E0 = {3: 180, 5: 200, 6: 200}                  # else 1380 (0x564), stage 2
DASH_SWING_E0_DEFAULT = 1380
DASH_STRONG_E0 = {0: 150, 1: 150, 2: 120, 4: 120, 3: 210, 5: 270, 6: 270}
# The dash: wind-up T[5] 50 ms + at most 500 ms in state 6, + one tick of slack.
DASH_WINDOW_MS = 580
# The dash attack / strong attack comes out of state 6 only (FUN_00414210 motion case 1 / 5):
# the wind-up (state 5, entered with +0xE9C 0) moves on to 6 on the tick +0xE9C reaches
# 50, the third, so the earliest start is the dash's tick + 90 (live 2b f1 84.05: the
# attack word 30 ms into the dash, state 4 about 90 ms after its state 5: E 1380, not 1440).
DASH_ATTACK_EARLIEST_MS = 90
# The hit is caught on tick N and reported by the builder on tick N+1.
REPORT_TICK_MS = 30
# E when no anchor says (the hit on the swing's second frame, report 1.4): swing stage 0,
# skill / strong attack on T[1].
DEFAULT_SKILL_E = 270
# The anchors of an action in state 1 (a skill cast; the strong attack, variant 0).
STATE1_KINDS = ('cast', 'strong')
# A cast / strong-attack anchor whose E is past its L by less than this at a hit is still
# that action (the server's E runs a packet late): the client's hurt is then L (E > L,
# 0x412F34..0x412F4A; report 0 / 6.2). A fresh cast (C2S 0x15) this young is still running.
STATE1_E_SLACK_MS = 300
# The hurt of a held attack key the server lost count of (E unknown: no action it knows the
# end of, e.g. out of an airborne hurt): the largest stage default, stage 2 / the dash attack
# 2140 - 1380 (report 1.4; basic 490, stage 1 500). A key held through the end of an action
# the tracker knows starts a stage-0 swing on the tick after it (_roll_over), E known.
HELD_SWING_HURT_MS = COMBO_ENDS[-1] - DASH_SWING_E0_DEFAULT

# --- FUN_0044f070: skill id -> cast variant (scene+0x25A -> attacker +0x941 -> +0x98B) ----
VARIANT_RANGES = {
    1: ((0x104, 0x10E), (0x150, 0x15A), (0x187, 0x191), (0x1BE, 0x1C8), (0x200, 0x20A),
        (0x22C, 0x236)),
    2: ((0x10F, 0x119), (0x15B, 0x165), (0x1B3, 0x1BD), (0x1C9, 0x1D3), (0x20B, 0x215),
        (0x242, 0x24C)),
    3: ((0x13A, 0x144), (0x166, 0x170), (0x1DF, 0x1E9), (0x216, 0x220), (0x237, 0x241),
        (0x7A1, 0x7AB)),
    4: ((0x24D, 0x257), (0x76A, 0x774), (0x7D8, 0x7E2), (0x780, 0x78A), (0x796, 0x7A0),
        (0x7C2, 0x7CC)),
    5: ((0x258, 0x262), (0x775, 0x77F), (0x7E3, 0x7ED), (0x78B, 0x795), (0x7CD, 0x7D7),
        (0x9FA, 0xA04), (0xA3C, 0xA46)),
    6: ((0x8B0, 0x8BA), (0x7AC, 0x7B6), (0x91E, 0x928), (0x955, 0x95F), (0xA05, 0xA0F),
        (0xA47, 0xA51), (0x98C, 0x996), (0xA68, 0xA72)),
    7: ((0x8BB, 0x8C5), (0x7B7, 0x7C1), (0x929, 0x933), (0x960, 0x96A), (0xA1B, 0xA25),
        (0xA52, 0xA5C), (0x997, 0x9A1), (0xA73, 0xA7D), (0xACB, 0xAD5)),
    8: ((0x8D1, 0x8DB), (0x93F, 0x949), (0x976, 0x980), (0xA26, 0xA30), (0xA5D, 0xA67),
        (0x9A2, 0x9AC), (0xA7E, 0xA88), (0xB04, 0xB0E), (0xB0F, 0xB19)),
    9: ((0x8DC, 0x8E6), (0x94A, 0x954), (0x981, 0x98B), (0x9AD, 0x9B7), (0xA89, 0xA93),
        (0xAF9, 0xB03), (0xB25, 0xB2F)),
    10: ((0x9B8, 0x9C2), (0xA94, 0xA9E), (0xB30, 0xB3A)),
    11: ((0xB3B, 0xB45),),
}


def variant_of(skill_id):
    """The cast variant FUN_0044f070 gives `skill_id` (0: none, e.g. a passive)."""
    i = int(skill_id or 0)
    for v, ranges in VARIANT_RANGES.items():
        for a, b in ranges:
            if a <= i <= b:
                return v
    return 0


# --- FUN_004194f0: the element byte +0x95D ------------------------------------------------
ELEMENT_FIRE, ELEMENT_ICE, ELEMENT_LIGHTNING, ELEMENT_WIND = 1, 2, 3, 4
ELEMENT_NAMES = {0: 'none', 1: 'fire', 2: 'ice', 3: 'lightning', 4: 'wind'}
ICE_EXTRA_STUN_MS = 1000                # state 3 holds +0x9E4 + 1000 (0x4152AC)
WIND_SLIDE_TICKS = 19                   # while +0xE9C < 600, from 0
WIND_MIN_STUN_MS = 600
NOVA_WIND_FACTOR = 2.5                  # +0x13E8 for Mage tier 1 variant 7
SLIDE_TICKS = {7: 2, 9: 4}              # while +0xE9C < 0x96: from 0x3C / from 0
SKIP = 'skip'
# (class, tier, variant) whose element FUN_004194f0 defers to the debuff's detonation
# (+0x95E, +0x95D = 0): Monk tier 2 Cartilage Smash, Rogue tier 2 Time Bomb.
DEFERRED = frozenset({(2, 2, 9), (4, 2, 6)})
# (class, tier, variant) whose case-9 relay carries action_flag 0 although the attacker's
# +0x98B is not 0 (report 2.2): FUN_004194f0 applies a tier-2 variant-9 attacker's Vampiric
# Attack (0xB25, a Priest skill) HP drain to the ATTACKER entity, on a watcher's copy too, so
# every watcher's client would add HP to its copy of him. Vampiric Attack has no element and
# no FUN_00426840 debuff; flag 0 costs only pass 2's expiry of a Rogue Stun (0x1B3..0x1BD)
# on the watchers' copies (+0x98C != 0). The slide and the stun still come from action 9.
NO_RELAY_FLAG = frozenset({(6, 2, 9)})


def relay_flag(variant, cls, tier):
    """The u8 action_flag of the case-9 relay for an attacker of (cls, tier) whose cast
    variant is `variant`: the variant, 0 for NO_RELAY_FLAG."""
    v = int(variant or 0)
    return 0 if (int(cls or 0), int(tier or 0), v) in NO_RELAY_FLAG else v & 0xFF


def element_base(variant, cls, tier):
    """The skill family FUN_004194f0 looks up in the attacker's learned list for (variant,
    attacker class +0x118, tier +0x119): its first id, SKIP when the function returns
    before any element (variant 2 Monk / Rogue, ...), None when there is no family."""
    v, c, t = int(variant or 0), int(cls or 0), int(tier or 0)
    m = {1: {1: 0x104, 2: 0x22C, 3: 0x150, 5: 0x200},
         2: {1: 0x10F, 2: SKIP, 3: 0x15B, 4: SKIP, 5: 0x20B},
         3: {1: 0x13A, 3: 0x166, 4: 0x7A1, 5: 0x216, 6: 0x1DF},
         4: {1: 0x76A, 2: 0x7D8, 4: 0x796, 5: 0x7C2}}
    if v in m:
        return m[v].get(c)
    if v == 5:
        if c in (2, 3):
            return SKIP
        if c == 1:
            return 0x775
        return 0x9FA if (c == 4 and t == 1) else None
    if v == 6:
        if c == 1:
            return 0x8B0
        if c == 6:
            return 0x7AC
        return {(2, 1): 0x91E, (2, 2): 0x955, (3, 1): 0x98C, (4, 2): 0xA47, (5, 1): 0xA68}.get((c, t))
    if v == 7:
        if c == 5:
            return 0xA73 if t == 1 else 0xACB
        if c == 6:
            return 0x7B7
        return {(2, 1): 0x929, (2, 2): 0x960, (3, 2): 0x9CE, (4, 1): 0xA1B}.get((c, t))
    if v == 8:
        if c == 1:
            return 0x8D1
        return {(2, 1): 0x93F, (2, 2): 0x976, (3, 1): 0x9A2, (4, 1): 0xA26, (5, 1): 0xA7E,
                (6, 1): 0xB04}.get((c, t))
    if v == 9:
        if c == 1:
            return 0x8DC
        if c == 3:
            return 0x9AD if t == 1 else 0x9E4
        if c == 6 and t == 1:
            return SKIP
        return {(2, 1): 0x94A, (2, 2): 0x981, (5, 1): 0xA89}.get((c, t))
    if v == 10:
        if c == 3:
            return 0x9B8 if t == 1 else 0x9EF
        if c == 5 and t == 1:
            return 0xA94
        if c == 6 and t == 2:
            return SKIP
    if v == 11 and c == 6 and t == 2:
        return SKIP
    return None


def element_of(variant, cls, tier, attribute_of):
    """(element, wind factor) a tae-9/10 hit with cast `variant` by an attacker of (cls,
    tier) gives the monster: the hii Attribute (record +0x190, `attribute_of(id)`) of the
    family element_base names when it is 1..4; (0, 1.0) for variant 0 (a strong attack),
    no family, an early return or a deferred element."""
    v, c, t = int(variant or 0), int(cls or 0), int(tier or 0)
    if not v or (c, t, v) in DEFERRED:
        return 0, 1.0
    base = element_base(v, c, t)
    if not isinstance(base, int):
        return 0, 1.0
    try:
        attr = int(attribute_of(base) or 0)
    except (TypeError, ValueError):
        attr = 0
    if not 1 <= attr <= 4:
        return 0, 1.0
    factor = NOVA_WIND_FACTOR if (attr == ELEMENT_WIND and (c, t, v) == (5, 1, 7)) else 1.0
    return attr, factor


# --- the attacker's own hurt (live session 2b: a monster hit HIM mid-action) --------------
# His C2S 0x0D action nibble ae (lo bits 12-15, +0x94C, with an event_source_uid tail): a
# monster's (or player's) hit his own client caught on him. FUN_00412c60 pass 2 (the switch
# on +0x9DC, 0x413998): cases 6..10 (0x413A73..0x413C47) set +0xE9C = 0x3C for 7 / 8, else 0,
# and state 0x17 (airborne) for 8 / 10, else state 3 - which ends whatever he was doing (the
# combo and its stage, a cast); cases 1..5 (a guarded hit) only reset +0xE9C (0x413A17), no
# state change, so they are no hurt here. State 3 exits once +0xE9C >= +0x9E4 (0x41528A..
# 0x4152C2), and his +0x9E4 is the hi12 of the same packet (+0x9E8: contact 250, a Monkey
# Soldier swing 810 / 750). Live 2b, 468 hurts: state 3 for 240 / 720 / 660 ms, then one tick
# of state 8 (the turn check), then an input still held starts its action at stage 0 / E 0
# (k2: 490, 500, 490 on A's copy where the old anchor gave 280, 290, 760; i4: the Ice Spear
# cast word came 30 ms into the hurt and the cast began at its end, E 270 = 1020, not 510).
HURT_EVENTS = frozenset(range(6, 11))
HURT_E9C_START = {7: 0x3C, 8: 0x3C}
AIRBORNE_HURTS = (8, 10)            # state 0x17: the same stun, then the landing (not modelled)
# The dash end (state 7, T[7] = 150 ms) holds an attack pressed after the dash words stopped
# (FUN_00414210 motion case 1 acts in 8 / 0xC / 6 only): it starts one tick after it (k4:
# the attack word 60 ms into state 7, state 4 180 ms after the release).
DASH_END_MS = 150
# The first-hit-frame E of each combo stage (report 1.4: sprite 4 frames @100 / 740 / 1380):
# the E of a hit on an action re-started at the end of a hurt or the dash end that the
# timeline puts before its start (E < 0), and the most E of a hit while the tracker still
# has him busy (the time since the input overestimates it). Not a cap on the timeline E
# otherwise: an ice hit's chase word goes out inside the stun, so a hurt error is a lasting
# offset of both copies (live 2b f1 10.34: E 150 by the timeline, 460 on A's copy).
STAGE_DEFAULT_E = (120, 750, 1380)
MOTION_GUARD = 2
# lo bits 0-1 (the direction key, +0x93F) -> +0x949: 2 left, 6 right.
FACING_OF_DIR = {1: 2, 2: 6}


def hurt_len(ae, hi):
    """Logic ms from the tick of his hurt packet to the first tick he acts again: pass 2 sets
    +0xE9C to 0x3C (ae 7 / 8) or 0 and the state machine adds 30 on that same tick; state 3
    exits on the tick it reaches hi (& 0xFFF), the next tick is state 8. Contact 250: 270;
    a Monkey Soldier swing 810: 750, 750: 690 (live 2b)."""
    need = (int(hi or 0) & 0xFFF) - HURT_E9C_START.get(int(ae or 0), 0)
    return REPORT_TICK_MS * max(1, -(-need // REPORT_TICK_MS))


# --- the tracker: the attacker's current action from his C2S 0x0D stream ------------------
def motion(lo):
    return (int(lo) >> 2) & 0x7


def cast_variant(lo):
    return (int(lo) >> 5) & 0xF


COMBO_KINDS = ('swing', 'dash')


def new_tracker(facing=None):
    """t: his logic clock (the sum of logic_elapsed_ms, ms); anchor: his running action;
    busy: a window his client acts on no input (a hurt, the dash end) with the input still
    held (pending); facing: the model of his +0x949 (2 / 6), report_facing its value at the
    last packet's tick; hurt_next: a hurt the last packet carried (begun by the next one).
    `facing`: his +0x949 when his entity was (re)created - the direction byte of the S2C
    0x07 spawn record the server sent (read into +0x949 at 0x453B80); facing_from / report_
    from say where the model's value came from ('spawn' until a word turns him: 'words')."""
    f = facing if facing in FACING_OF_DIR.values() else None
    src = 'spawn' if f is not None else None
    return {'lo': 0, 'dash_ms': 0, 'anchor': None, 't': 0, 'busy': None, 'hurt_next': None,
            'dash_t0': None, 'facing': f, 'report_facing': f, 'facing_from': src, 'report_from': src}


def _stage_of(e):
    for i, end in enumerate(COMBO_ENDS):
        if e < end:
            return i
    return len(COMBO_ENDS)


def _value(x):
    """An int argument that may be given as a callable (only called when needed)."""
    return int((x() if callable(x) else x) or 0)


def _start(track, kind, weapon, *, variant=0, from_dash=False, tier=0, t0=None, note=None):
    """A new anchor starting on tick t0 (default: the tick of this packet). `note`: it was
    started by the tracker at the end of a busy window (virtual: its start is an estimate)."""
    wt = _value(weapon)
    e0, stage = 0, 0
    if kind == 'dash':
        e0 = DASH_SWING_E0.get(wt, DASH_SWING_E0_DEFAULT)
        stage = _stage_of(e0) if wt not in SHOT_LEN else 0
    elif kind == 'strong' and from_dash:
        e0 = DASH_STRONG_E0.get(wt, DASH_STRONG_E0[0])
    length = skill_len(wt, variant if kind == 'cast' else 0, _value(tier)) if kind in STATE1_KINDS else None
    t = int(track.get('t') or 0)
    t0 = t if t0 is None else int(t0)
    track['anchor'] = {'kind': kind, 'ms': t - t0, 'e0': e0, 'wt': wt, 'stage': stage,
                       'variant': int(variant), 'from_dash': bool(from_dash), 'L': length, 't0': t0,
                       'key_t': t0, 'virtual': note is not None, 'note': note}
    return track['anchor']


def _turn(track, lo):
    """The turn check (0x414498..0x41450F) on a tick he is in state 8 / 0xC / the air: +0x949
    = the direction key of `lo` when one is held."""
    f = FACING_OF_DIR.get(int(lo) & 3)
    if f is not None:
        track['facing'] = f
        track['facing_from'] = 'words'


def _action_end(anchor):
    """The E at which the anchor's action ends (the state switch sets state 8), None when
    not known ('held')."""
    kind = anchor['kind']
    if kind in STATE1_KINDS:
        return anchor.get('L')
    if kind in COMBO_KINDS:
        if anchor['wt'] in SHOT_LEN:
            return SHOT_LEN[anchor['wt']]
        return COMBO_ENDS[min(int(anchor['stage']), len(COMBO_ENDS) - 1)]
    return None


# The actions whose end the tracker knows (_action_end): an attack key down on the tick after
# that end starts a stage-0 swing there (_roll_over).
ROLL_KINDS = COMBO_KINDS + STATE1_KINDS
END_LABELS = {'swing': 'swing', 'dash': 'dash attack', 'cast': 'cast', 'strong': 'strong attack'}


def _end_tick(anchor):
    """The tick the anchor's action ends on: the first tick whose +0xE9C, after that tick's
    += 30, reaches _action_end - state 4's end test (0x4159E2) and state 1's (0x415AD8) read E
    after the += 30 and set state 8 on that same tick (E 630 for stage 0's 610, 2160 for a dash
    attack's 2140). None when not known ('held'). The stage is the one reached so far: a
    later advance moves it."""
    end = _action_end(anchor) if anchor is not None and anchor['kind'] in ROLL_KINDS else None
    if end is None or anchor.get('t0') is None:
        return None
    need = int(end) - int(anchor['e0'])
    return int(anchor['t0']) + REPORT_TICK_MS * max(0, -(-need // REPORT_TICK_MS))


def _end_note(anchor):
    """'started at the end of the <action>' for a swing an attack key began at its end."""
    what = END_LABELS.get(anchor['kind'], anchor['kind'])
    if anchor['kind'] == 'swing' and int(anchor['wt']) not in SHOT_LEN:
        what += f' (stage {int(anchor["stage"])})'
    return f'started at the end of the {what}'


def _roll_over(track, prev, t_prev, t, weapon):
    """The attack key down (`prev`, motion 1) on the ticks t_prev + 30 .. t - 30: an action
    the tracker knows that ended on one of them before t - 30 (_end_tick) is followed on the
    next tick by a stage-0 swing - state 8's turn check, then motion case 1 (state 4, E 0) -
    which the key carries on (_combo_check), and which can end the same way. An end on tick
    t - 30 is this packet's own tick's (observe: `lo` decides), an end on t the next one's.
    Returns the running anchor."""
    anchor = track.get('anchor')
    while True:
        end = _end_tick(anchor)
        if end is None or not t_prev < end + REPORT_TICK_MS <= t - REPORT_TICK_MS:
            return anchor
        s = end + REPORT_TICK_MS
        _turn(track, prev)
        anchor = _start(track, 'swing', weapon, t0=s, note=_end_note(anchor))
        _combo_check(anchor, s + REPORT_TICK_MS, t - REPORT_TICK_MS)


def _busy_at(track, tau, m_input=None):
    """True when the tracker has him in a state the turn check skips at tick `tau` (it turns
    him only in 8 / 0xC / 9 / 0x13 / 0x17 / 0x11, 0x41449E): a busy window (hurt, dash end;
    not an airborne hurt, state 0x17 turns him), the dash (5 / 6), a running action (1 / 4;
    a 'held' key: within a stage of its last word) or the guard (2, `m_input` the input of
    that tick)."""
    busy = track.get('busy')
    if busy is not None and tau < busy['until']:
        return not busy.get('airborne')
    if track.get('dash_t0') is not None and tau > track['dash_t0']:
        return True
    if m_input == MOTION_GUARD:
        return True
    a = track.get('anchor')
    if a is None or a.get('t0') is None:
        return False
    if tau == a['t0']:
        # Its first tick: from 8 / 0xC the turn check came before the motion case that
        # started it; a dash attack, a dash strong attack or a cast out of the dash starts in
        # state 6 (motion case 1 / 5 / 7), where the turn check does not run.
        return a['kind'] == 'dash' or bool(a.get('from_dash'))
    if tau < a['t0']:
        return True                                 # a dash attack still in the wind-up
    if a['kind'] == 'held':
        return tau - int(a.get('key_t') or a['t0']) < COMBO_ENDS[0] + REPORT_TICK_MS
    end = _action_end(a)
    return end is None or int(a['e0']) + (tau - REPORT_TICK_MS - int(a['t0'])) < end


PENDING_KINDS = {MOTION_ATTACK: 'swing', MOTION_STRONG: 'strong', MOTION_CAST: 'cast', MOTION_DASH: 'dash'}


def _pending_of(lo, pending, t):
    """The action an input held through a busy window starts at its end: 'swing' (motion
    1), 'strong' (5), 'cast' (7, its variant) or 'dash' (6); None for any other input (the
    key released). `t`: the tick the input was first seen (kept while it stays the same)."""
    m = motion(lo)
    kind = PENDING_KINDS.get(m)
    if kind is None:
        return None
    variant = cast_variant(lo) if m == MOTION_CAST else 0
    if pending is not None and pending['kind'] == kind and pending['variant'] == variant:
        return pending
    return {'kind': kind, 'variant': variant, 't': int(t)}


def _begin_hurt(track, hurt, prev_lo):
    """His hurt (ae 6..10) from the tick of the packet that carried it: the running action,
    its combo stage and the dash end; a busy window of hurt_len ms with the input of that
    tick pending. An airborne hurt (ae 8 / 10: state 0x17, 0x413C2A) turns him (the turn
    check runs in 0x17, 0x4144B2) and exits to state 9 in the air (0x4154C2), where a held
    attack is an air attack (0xE), not a ground swing: nothing is pending, a hit after it is
    the fallback."""
    ae, hi, t0 = int(hurt['ae']), int(hurt['hi']), int(hurt['t0'])
    track['anchor'] = None
    track['dash_t0'] = None
    track['dash_ms'] = MS_CAP                                  # no dash attack out of a hurt
    airborne = ae in AIRBORNE_HURTS
    what = 'contact' if ae == 6 else f'action {ae}' + (', airborne' if airborne else '')
    track['busy'] = {'why': 'hurt', 'start': t0, 'until': t0 + hurt_len(ae, hi), 'ae': ae, 'hi': hi,
                     'airborne': airborne, 'pending': None if airborne else _pending_of(prev_lo, None, t0),
                     'note': f'his own hurt ({what}, hi {hi})'}


def _start_pending(track, busy, weapon, tier, until):
    """The action his held input starts on tick `until`, the first tick after a busy window
    (a virtual anchor: its start is the tracker's estimate). Returns the anchor or None."""
    p = busy.get('pending')
    note = f'started at the end of {busy["note"]}'
    if p is None:
        return track.get('anchor')
    if p['kind'] == 'dash':
        track['dash_t0'] = int(until)
        track['dash_ms'] = max(0, int(track['t']) - int(until))
        return track.get('anchor')
    kind = p['kind']
    return _start(track, kind, weapon, variant=p['variant'] if kind == 'cast' else 0, tier=tier,
                  t0=until, note=note)


def anchor_e(anchor):
    """The attacker's +0xE9C at the last packet the anchor counted (report 1.4)."""
    return int(anchor['e0']) + int(anchor['ms']) - REPORT_TICK_MS


def in_state_1(anchor):
    """True while a cast / strong-attack anchor is inside its L at the last packet it
    counted: the attacker is still in state 1 and his client ignores motion 1 / 5 (a held or
    mashed attack key during the cast, see the module docstring)."""
    return (anchor is not None and anchor['kind'] in STATE1_KINDS and anchor.get('L') is not None
            and anchor_e(anchor) < int(anchor['L']))


def _combo_check(anchor, tau_from, tau_to):
    """State 4's combo check (0x415880..0x41592D) on the ticks tau_from..tau_to, the attack
    key down (+0x940 == 1) on all of them: stage n goes on to n + 1 on a tick whose +0xE9C
    (after its += 30) is strictly inside COMBO_ADVANCE[n]. A key pressed and let go before
    the window carries nothing on: the stage then ends at its L (FUN_00422710 reads the stage
    +0x979, not E). Bow / staff shots have no stages."""
    if anchor is None or anchor['kind'] not in COMBO_KINDS or int(anchor['wt']) in SHOT_LEN:
        return
    base = int(anchor['e0']) - int(anchor['t0'])               # E on tick tau = base + tau
    tau, tau_to = int(tau_from), int(tau_to)
    while tau <= tau_to and anchor['stage'] < len(COMBO_ADVANCE):
        lo, hi = COMBO_ADVANCE[anchor['stage']]
        e = base + tau
        if e <= lo:
            tau += REPORT_TICK_MS * ((lo - e) // REPORT_TICK_MS + 1)
            continue
        if e >= hi:
            return                                             # the stage ended (hi is its L)
        anchor['stage'] += 1
        tau += REPORT_TICK_MS


def _combo_goes_on(anchor, t):
    """A motion-1 packet (a press or the held key's re-send) on tick `t` inside a running
    swing / dash attack: True when it belongs to that action (its E on the tick before is
    inside the L of the stage reached, so tick `t`'s motion case still runs in state 4; the
    combo check of tick `t` then sees the key, and carries the combo on inside the stage's
    window) - False when the action ended before it (a new state 4 starts here). With E on
    tick `t` (after its += 30) at the stage's L the action ends ON tick `t` (_end_tick): the
    key down on the next tick starts a new swing there (_roll_over, observe) - live 2c, the
    tap on E 630 of stage 0 (600 on the tick before) was a new stage 0, not stage 1."""
    end = _action_end(anchor)
    if end is None or anchor_e(anchor) >= end:
        return False
    _combo_check(anchor, t, t)
    return True


def observe(track, lo, elapsed, weapon=0, tier=0, hi=0):
    """Follow one accepted C2S 0x0D of the attacker: count its logic_elapsed_ms into the
    running action and start a new one on the words that start an action:

      motion 1 after motion-6 words        a dash attack (<= DASH_WINDOW_MS into the dash);
                                           else the dash end (state 7) holds it: a swing at
                                           its end if the key is still down
      motion 1 otherwise                   a new swing unless the running combo goes on
                                           (_combo_goes_on)
      motion 1 on the tick after the       a stage-0 swing there (_roll_over; the key held
      running action's end (_end_tick)     or pressed on the end tick): any action the
                                           tracker knows; a held key past an end it does
                                           not know starts a 'held' anchor (E unknown)
      motion 5 (a new press)               the strong attack (from the dash: its E0)
      motion 7 (new, or a new variant)     a skill cast; the variant is lo bits 5-8
      motion 1 / 5 in state 1              nothing: a cast / strong attack runs on while
                                           its E < L (in_state_1)
      action nibble 6..10 (his hurt, `hi`  the running action ends (state 3, from the NEXT
      its ms)                              packet on, so this packet's own hit report is the
                                           old action's); words inside the hurt start
                                           nothing, the input still held at its end
                                           (hurt_len) starts its action there, at stage 0

    It also follows his facing +0x949 (_turn on the ticks he is not busy, _busy_at):
    report_facing(track) is its value at this packet's tick.
    `weapon` / `tier`: the attacker's weapon type (+0x11A) and tier (+0x119), or callables
    giving them (only called when an action starts; the tier sizes a state-1 action's L).
    Returns the running anchor (None before the first action, inside a busy window)."""
    lo = int(lo or 0) & LO_MASK
    elapsed = min(MAX_STEP_MS, max(0, int(elapsed or 0)))
    prev = int(track.get('lo') or 0)
    m, pm = motion(lo), motion(prev)
    track['lo'] = lo
    t_prev = int(track.get('t') or 0)
    t = track['t'] = t_prev + elapsed
    hurt = track.pop('hurt_next', None)
    if hurt is not None:
        _begin_hurt(track, hurt, prev)
    anchor = track.get('anchor')
    if anchor is not None:
        anchor['ms'] = min(MS_CAP, int(anchor['ms']) + elapsed)
        if pm == MOTION_ATTACK:                                # the key held on those ticks
            _combo_check(anchor, t_prev + REPORT_TICK_MS, t - REPORT_TICK_MS)
    if pm == MOTION_DASH:
        track['dash_ms'] = min(MS_CAP, int(track.get('dash_ms') or 0) + elapsed)
    # The ticks t_prev + 30 .. t - 30 ran the previous input.
    busy = track.get('busy')
    stood_up = busy is not None and busy['until'] < t
    if stood_up:
        track['busy'] = None                                   # he stood up in between:
        _turn(track, prev)                                     # the turn check, then the
        anchor = _start_pending(track, busy, weapon, tier, busy['until'])   # held input
        if pm == MOTION_ATTACK and (busy.get('pending') or {}).get('kind') == 'swing':
            _combo_check(anchor, int(busy['until']) + REPORT_TICK_MS, t - REPORT_TICK_MS)
    if pm == MOTION_ATTACK and track.get('busy') is None:
        anchor = _roll_over(track, prev, t_prev, t, weapon)    # the key down past an action's end
    if (not stood_up and (busy is None or busy.get('airborne')) and elapsed >= REPORT_TICK_MS
            and not _busy_at(track, t - REPORT_TICK_MS, pm)):
        _turn(track, prev)
    track['report_facing'] = track.get('facing')
    track['report_from'] = track.get('facing_from')
    # This packet's own tick.
    if ((lo >> 12) & 0xF) in HURT_EVENTS:
        # His pass 2 puts him in state 3 on this tick before the state machine runs: no
        # turn, no action; the hurt begins with the next packet (hurt_next).
        track['hurt_next'] = {'t0': t, 'ae': (lo >> 12) & 0xF, 'hi': int(hi or 0) & 0xFFF}
        return anchor
    busy = track.get('busy')
    if busy is not None:
        if busy['until'] > t:
            if busy.get('airborne'):
                _turn(track, lo)                               # state 0x17 turns him, no action
                return anchor
            if busy['why'] == 'hurt' or m != MOTION_CAST:
                busy['pending'] = _pending_of(lo, busy.get('pending'), t)
                return anchor
            track['busy'] = None                               # a cast word: see below
        else:
            track['busy'] = None                               # he stands on this very tick
            _turn(track, lo)
            if busy.get('airborne'):
                return anchor                                  # state 9, in the air
            return _start_pending(track, dict(busy, pending=_pending_of(lo, None, t)), weapon, tier, t)
    if not _busy_at(track, t):
        _turn(track, lo)                                       # the turn check of this tick
    dash_t0 = track.get('dash_t0')
    if m == MOTION_DASH and pm != MOTION_DASH:
        track['dash_ms'] = 0                                   # a dash starts: its clock
        track['dash_t0'] = t
    from_dash = pm == MOTION_DASH and int(track.get('dash_ms') or 0) <= DASH_WINDOW_MS
    # A dash attack / strong attack starts in state 6: not before the wind-up is over.
    out_of_dash = (max(t, int(dash_t0) + DASH_ATTACK_EARLIEST_MS)
                   if from_dash and dash_t0 is not None else None)
    if pm == MOTION_DASH and m != MOTION_DASH:
        track['dash_t0'] = None
        attack = from_dash and m in (MOTION_ATTACK, MOTION_STRONG)
        if not attack and m != MOTION_CAST and dash_t0 is not None:
            # The dash words stop when state 7 begins (or the dash ran out, DASH_WINDOW_MS):
            # an attack held from here starts once it is over.
            end = min(t, int(dash_t0) + DASH_WINDOW_MS) + DASH_END_MS + REPORT_TICK_MS
            if end > t:
                track['busy'] = {'why': 'dash end', 'start': t, 'until': end, 'note': 'the dash',
                                 'pending': _pending_of(lo, None, t)}
                return anchor
    busy1 = in_state_1(anchor)
    if m == MOTION_ATTACK:
        if anchor is not None and anchor['kind'] == 'held':
            anchor['key_t'] = t
        if busy1:
            pass                                               # state 1 runs on
        elif pm == MOTION_DASH:
            anchor = _start(track, 'dash' if from_dash else 'swing', weapon, t0=out_of_dash)
        elif anchor is not None and anchor['kind'] in COMBO_KINDS and _combo_goes_on(anchor, t):
            pass
        elif pm != MOTION_ATTACK:
            anchor = _start(track, 'swing', weapon)            # a press after the action ended
        elif _end_tick(anchor) == t - REPORT_TICK_MS:
            # The key held through the tick its action ended on: this tick's state 8 starts it.
            anchor = _start(track, 'swing', weapon, note=_end_note(anchor))
        else:
            anchor = _start(track, 'held', weapon)             # held past an end not known
    elif m == MOTION_STRONG and pm != MOTION_STRONG:
        if not busy1:
            anchor = _start(track, 'strong', weapon, from_dash=from_dash, tier=tier, t0=out_of_dash)
    elif m == MOTION_CAST and (pm != MOTION_CAST or cast_variant(lo) != cast_variant(prev)):
        anchor = _start(track, 'cast', weapon, variant=cast_variant(lo), tier=tier, from_dash=from_dash)
    return anchor


def report_facing(track):
    """His +0x949 at the tick of the last packet observe() saw, before that packet's own
    input turned him - what pass 1 of a hit reported in it copies to the monster's +0x95B:
    2 left, 6 right, None when neither a word nor his spawn record set it."""
    return (track or {}).get('report_facing')


def report_facing_from(track):
    """Where report_facing's value came from: 'words' (a turn his words made), 'spawn' (the
    S2C 0x07 record that created his entity, new_tracker), None."""
    return (track or {}).get('report_from')


# --- the classification of one hit report -------------------------------------------------
@dataclass(frozen=True)
class Hit:
    """What one client-caught hit does to the attacker's copy of the monster - and, relayed,
    to every other copy."""
    event: int          # the report's interact event: 7 swing, 9 skill / strong attack
    tae: int            # its tail target_action_event: the relay's action nibble
    kind: str           # 'swing', 'dash', 'skill', 'strong', 'swing (event 9)', or the fallback
    hurt: int           # ms, the relay's hi (12 bits) = the monster's +0x9E4
    stun: int           # ms the attacker's copy stays in state 3 (the chase gate)
    ticks: float        # slide ticks at the walk speed (0: airborne, not modelled)
    variant: int        # the attacker's +0x98B at the hit: his cast variant (0 none / strong)
    flag: int           # the relay's u8 action_flag (tae 9/10): the variant (relay_flag)
    element: int        # the server's view of +0x95D (0 none, 1..4)
    factor: float       # the wind slide's +0x13E8
    L: object           # the action length (None for the fallback)
    E: object           # the attacker's elapsed ms at the hit (None for the fallback)
    why: str            # for the log

    @property
    def airborne(self):
        return self.tae in AIRBORNE_TAES


def stun_ms(tae, hurt, element=0):
    """How long the monster stays in state 3 (or 0x17): hurt - 60 from 0x3C for tae 7/8,
    hurt (+1000 ice) from 0 for 9/10; a wind slide stands at least 600 ms."""
    if tae in TAES[SWING_EVENT]:
        stun = max(0, int(hurt) - 60)
    else:
        stun = int(hurt) + (ICE_EXTRA_STUN_MS if element == ELEMENT_ICE else 0)
    if element == ELEMENT_WIND:
        stun = max(stun, WIND_MIN_STUN_MS)
    return stun


def slide_ticks(tae, element=0, factor=1.0):
    """Ticks of the hurt slide at the monster's walk speed: 2 (tae 7), 4 (tae 9), 19 x
    +0x13E8 with the wind element; 0 for an airborne victim (8 / 10, not modelled)."""
    if tae in AIRBORNE_TAES:
        return 0.0
    if tae == 9 and element == ELEMENT_WIND:
        return WIND_SLIDE_TICKS * float(factor)
    return float(SLIDE_TICKS.get(tae, 0))


def _tae(event, tae):
    ok = TAES.get(event, ())
    tae = int(tae or 0)
    return tae if tae in ok else (ok[0] if ok else event)


PENDING_ACTIONS = ('swing', 'strong', 'cast')


def _default_e(kind, wt, e):
    """The E a hit of `kind` lands at by default (report 1.4: the second frame): the combo
    stage's first-hit frame (STAGE_DEFAULT_E, stage from `e`), DEFAULT_SKILL_E for state 1;
    None when not known (bow / staff shots)."""
    if kind in ('skill', 'strong'):
        return DEFAULT_SKILL_E
    if kind in ('swing', 'dash', 'swing (event 9)') and int(wt or 0) not in SHOT_LEN:
        stage = _stage_of(e)
        return STAGE_DEFAULT_E[stage] if stage < len(STAGE_DEFAULT_E) else None
    return None


def _pending_anchor(track, busy, weapon, tier):
    """A hit while the tracker still has him in a busy window: it ended earlier on his client
    and the input he held (busy['pending']) started its action somewhere after that input
    came - the anchor of that action as if it started with the input (its E then at most
    the time since, which overestimates it; classify caps it at the kind's default,
    'pending'). Not stored."""
    p = busy['pending']
    kind = p['kind']
    wt = _value(weapon)
    length = (skill_len(wt, p['variant'] if kind == 'cast' else 0, _value(tier))
              if kind in STATE1_KINDS else None)
    return {'kind': kind, 'ms': max(0, int(track.get('t') or 0) - int(p['t'])), 'e0': 0, 'wt': wt,
            'stage': 0, 'variant': int(p['variant']), 'from_dash': False, 'L': length, 't0': int(p['t']),
            'virtual': True, 'pending': True,
            'note': f'{busy["note"]} not over by his words yet: the action his held input starts'}


def classify(track, event, tae=None, *, cls=0, tier=0, weapon=0, cast_skill=None, cast_age_ms=None,
             fallback_ms=490, per_swing=True, attribute_of=lambda _id: 0):
    """The Hit of a client-caught hit report with interact `event` (7 or 9) and tail `tae`.

    track          the attacker's tracker (observe(); None = no stream seen)
    cls, tier      the attacker's class / tier (+0x118 / +0x119)
    weapon         his weapon type now (+0x11A), when no anchor recorded one
    cast_skill     the id of his attack-skill cast in the report window (C2S 0x15), or None
    cast_age_ms    how long before the report that cast came (None: not known, taken as
                   still running); the cast runs while it is younger than its L + slack
    fallback_ms    the hurt of a hit whose kind or E is unknown (config MOB_HIT_RELAY_HURT_MS)
    per_swing      False: every hit gets fallback_ms (config MOB_HIT_HURT_PER_SWING)
    attribute_of   id -> the hii Attribute (element_of)

    Kinds:
      event 7 after a swing / dash attack anchor      L of the stage it reached (E past
                                                      it: a stale anchor)
      event 9 after the cast words                    the skill: skill_len by their variant
      event 9 after a strong-attack press             the strong attack: variant 0
      a cast / strong attack with L <= E < L + STATE1_E_SLACK_MS: still that action, hurt L
                                                      (the client's clamp for E > L)
      event 9 after a swing anchor                    a swing FUN_0041aa80 promoted to 9
                                                      (combo L, variant 0) - unless a cast
                                                      is running: then the swing (or held)
                                                      words were the attack key held
                                                      through state 1, and it is the cast
      event 9, no usable anchor, a running cast       that skill with E = DEFAULT_SKILL_E
    An action the tracker started at the end of a busy window (his own hurt, the dash end)
    or of an action with the attack key down (a virtual anchor) takes its timeline E, or the
    kind's default (the stage's first-hit frame STAGE_DEFAULT_E, DEFAULT_SKILL_E) when that
    is below 0. A hit while the tracker still has him in such a window (it ended earlier on
    his client) is the action his held input starts, E = min(the time since that input, the
    kind's default).
    Anything else is the fallback, which errs high (a low hurt lets the chase in early):
    event 9 the state-1 default L(variant 0) - DEFAULT_SKILL_E (1020 for a Warrior), a
    held attack key past an end the tracker does not know HELD_SWING_HURT_MS (760), else
    fallback_ms - never below fallback_ms. The variant is 0 when not known; the relay's flag
    is relay_flag's."""
    tae = _tae(event, tae)
    anchor = (track or {}).get('anchor')
    busy = (track or {}).get('busy')
    if busy is not None and (busy.get('pending') or {}).get('kind') in PENDING_ACTIONS:
        anchor = _pending_anchor(track, busy, weapon, tier)     # (an anchor from before is over)
    kind_now = anchor['kind'] if anchor else None
    E = anchor_e(anchor) if anchor else None
    wt = anchor['wt'] if anchor else int(weapon or 0)
    cast_v = variant_of(cast_skill) if cast_skill is not None else 0
    cast_L = skill_len(weapon, cast_v, tier)
    cast_running = cast_skill is not None and (cast_age_ms is None
                                               or float(cast_age_ms) < cast_L + STATE1_E_SLACK_MS)
    variant, kind, L, why, clamped = 0, None, None, '', False
    if event == SWING_EVENT and kind_now in COMBO_KINDS:
        kind, L = kind_now, _action_end(anchor)
    elif event == SKILL_EVENT and kind_now == 'cast':
        variant = int(anchor['variant'])
        kind, L = 'skill', skill_len(wt, variant, tier)
    elif event == SKILL_EVENT and kind_now == 'strong':
        kind, L = 'strong', skill_len(wt, 0, tier)
    elif event == SKILL_EVENT and kind_now in COMBO_KINDS + ('held',):
        if cast_running:
            age = f'{int(cast_age_ms)} ms old' if cast_age_ms is not None else 'fresh'
            why = f'{kind_now} words inside the cast ({age}, L {cast_L}): the attack key held through state 1; '
        elif kind_now in COMBO_KINDS:
            kind, L = 'swing (event 9)', _action_end(anchor)
    note = ''
    if kind is not None and anchor.get('virtual') and E is not None:
        note = f', {anchor["note"]}'
        default = _default_e(kind, wt, max(0, E))
        if default is not None and (E < 0 or (anchor.get('pending') and E > default)):
            note += f' (E {E} by his words, {"at most" if E > 0 else "taken as"} the default {default})'
            E = default
    if kind in ('skill', 'strong') and E is not None and L <= E < L + STATE1_E_SLACK_MS:
        clamped = True
    elif kind is not None and (L is None or E is None or not 0 <= E < L):
        why = (f'{kind} words {anchor["ms"]} ms old: E {E} is past the action'
               + (f' (L {L})' if L is not None else '') + '; ')
        kind = None
        variant = 0                         # a cast no longer inside its L (report 6.1)
    if kind is None and event == SKILL_EVENT and cast_running:
        variant, wt = cast_v, int(weapon or 0)
        kind, L, E = 'skill', cast_L, DEFAULT_SKILL_E
        why += 'the default E; ' if why.endswith('state 1; ') else 'no cast words counted: the default E; '
    fb = max(0, min(0xFFF, int(fallback_ms)))
    if not per_swing:
        hurt, why = fb, f'MOB_HIT_HURT_PER_SWING off: MOB_HIT_RELAY_HURT_MS {fb}'
        kind, L, E = 'fallback', None, None
    elif kind is None:
        if event == SKILL_EVENT:
            base = skill_len(wt, 0, tier) - DEFAULT_SKILL_E
            what = f'the state-1 default L {base + DEFAULT_SKILL_E} - E {DEFAULT_SKILL_E} (weapon {wt})'
        elif kind_now == 'held':
            base, what = HELD_SWING_HURT_MS, f'the stage-2 default {HELD_SWING_HURT_MS}'
        else:
            base, what = 0, ''
        if kind_now == 'held' and not why:
            why = 'a held attack key past its action (E unknown): '
        elif not why:
            why = (f'no {"swing" if event == SWING_EVENT else "cast / strong attack"} words '
                   f'counted (last action: {kind_now or "none"}): ')
        hurt = max(0, min(0xFFF, max(fb, base)))
        why += what if base > fb else f'MOB_HIT_RELAY_HURT_MS {fb}'
        kind, L, E = 'fallback', None, None
    else:
        hurt = max(0, min(0xFFF, int(L) if clamped else int(L) - int(E)))
        label = {'dash': 'dash attack', 'strong': 'strong attack'}.get(kind, kind)
        if kind == 'skill':
            label += f' v{variant}'
        if clamped:
            why += f'{label}: E {E} past L {L} by < {STATE1_E_SLACK_MS}: hurt L (weapon {wt})'
        else:
            why += f'{label}: L {L} - E {E} (weapon {wt})'
        why += note
    flag = relay_flag(variant, cls, tier)
    if flag != (variant & 0xFF):
        why += f' (relay flag {flag}: NO_RELAY_FLAG, the Vampiric Attack drain)'
    element, factor = element_of(variant, cls, tier, attribute_of) if tae in TAES[SKILL_EVENT] else (0, 1.0)
    return Hit(event=event, tae=tae, kind=kind, hurt=hurt, stun=stun_ms(tae, hurt, element),
               ticks=slide_ticks(tae, element, factor), variant=variant & 0xFF, flag=flag,
               element=element, factor=factor, L=L, E=E, why=why)


def cast_len(skill_id, weapon, tier):
    """L of an attack-skill cast (the cast-time chase gate: the client can only report its
    hit while the caster is in state 1, i.e. within L of the cast)."""
    return skill_len(weapon, variant_of(skill_id), tier)
