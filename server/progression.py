#!/usr/bin/env python3
"""
progression.py - client-computed level rules (login_character.md 3.5; lc-exp-table)
===================================================================================
The EN client derives the level from the total exp it was sent (S2C 0x02 total_exp,
S2C 0x03 exp_total, then S2C 0x21 deltas) and levels itself when a threshold is crossed
(FUN_00440DF0). The server must use exactly the same table, or it forces a different
level with 0x22 / 0x07 (roadmap S1-06).

EXP_INC is the 99 u32 per-level INCREMENTS at exe VA 0x6F0C28 (identical copy at 0x6F0EB0),
read from WindSlayer.exe: 56, 121, 238, ... 77319382, 82635627, 1 (sum 1,200,382,231).
The old server EXP_TABLE held the first 98 of them but treated them as cumulative
thresholds, so 176 exp was Lv3 on the server and Lv2 on the client.

    level_for_exp(55) == 1, level_for_exp(56) == 2, exp_for_level(3) == 177
    exp_for_level(13) == 30485, exp_for_level(99) == EXP_MAX == 1_200_382_230
    stat_total(1) == 9, stat_total(2) == 13, stat_total(29) == 121, stat_total(99) == 471
    exp_bar(60) == (4, 121)        # the client's "%u/%u" EXP tooltip at Lv2

The three exe tables the client indexes by level (test_records.py re-reads them with
pefile and fails if a byte differs):
  0x6F0C28[i]   the increment of level i+1. FUN_00440DF0 accumulates it until the total is
                exceeded and returns that level: EXP_CUM / level_for_exp below.
  0x6F0C24[L]   the same table indexed by the CURRENT level (= EXP_INC[L-1]): the "%u/%u"
                denominator of the exp bar (FUN_00442AA0), 0 at level 99.
  0x6F1044[L]   10 + (L-1)//10 for L = 1..99. Not an exp value: RegisterLocalPlayer writes
                it to entity+0xB4 (10 flat in room/arena mode). No packet carries it, so
                the server has nothing to mirror - noted here so it is not mistaken for a
                third exp table.

lc-data-model needs exp_for_level for the level -> exp migration; lc-exp-persist wires
grant_exp (owner 0x21 only) and lc-stats wires stat_total.
"""
from itertools import accumulate

EXP_INC = (
    56, 121, 238, 425, 700, 1128, 1708, 2464, 3420, 4800, 6556, 8869, 11718, 15163,
    19264, 24081, 29748, 36261, 44285, 53599, 64214, 76323, 90034, 105455, 122815, 142240,
    163856, 187928, 216080, 247401, 281920, 320139, 362268, 408517, 459284, 514800, 575498,
    641839, 717402, 799875, 889594, 987377, 1093608, 1208928, 1333745, 1469013, 1615188,
    1773304, 1950410, 2141939, 2348512, 2571400, 2811612, 3070536, 3349280, 3648988, 3971210,
    4317189, 4700676, 5112786, 5555038, 6029829, 6538818, 7084582, 7669369, 8295507, 8965873,
    9682985, 10471300, 11315085, 12217856, 13183261, 14215080, 15317770, 16495408, 17753337,
    19095964, 20528417, 22093361, 23764171, 25546875, 27448330, 29476282, 31638765, 33943428,
    36399530, 39015969, 41802640, 44831868, 48059445, 51497556, 55158802, 59056974, 63206331,
    67622400, 72321228, 77319382, 82635627, 1,
)
assert len(EXP_INC) == 99 and sum(EXP_INC) == 1_200_382_231

# EXP_CUM[i] = total exp at which level i+2 starts.
EXP_CUM = tuple(accumulate(EXP_INC))
LEVEL_MAX = 99
# Clamp: at >= EXP_CUM[98] the client's loop runs off the table and returns low byte 1 (Lv1).
EXP_MAX = EXP_CUM[97]


def clamp_exp(exp):
    return max(0, min(int(exp), EXP_MAX))


def level_for_exp(exp):
    """Level the client shows for `exp` total (first i with exp < EXP_CUM[i], plus 1)."""
    exp = clamp_exp(exp)
    for i, threshold in enumerate(EXP_CUM):
        if exp < threshold:
            return i + 1
    return LEVEL_MAX                      # unreachable after the clamp


def exp_for_level(level):
    """Minimum total exp of `level` (1..99; out-of-range levels are clamped)."""
    level = max(1, min(int(level), LEVEL_MAX))
    return 0 if level <= 1 else EXP_CUM[level - 2]


def exp_bar(exp):
    """(exp into the current level, exp the level needs) - the client's "%u/%u" exp tooltip
    (FUN_00442AA0: numerator from the 0x6F0EB0 accumulation, denominator 0x6F0C24[level]).
    The last level has no next threshold, so its denominator is 0."""
    exp = clamp_exp(exp)
    level = level_for_exp(exp)
    if level >= LEVEL_MAX:
        # Last row (i == 0x62): the client skips the subtraction and shows the raw total
        # over a 0 denominator.
        return exp, 0
    return exp - exp_for_level(level), EXP_INC[level - 1]


def stat_total(level):
    """Stat points a character has at `level` (FUN_00440E10): 9 + 4 per level to 29, then 5."""
    level = int(level)
    if not 1 <= level <= LEVEL_MAX:
        return 0
    return 9 + 4 * min(level - 1, 28) + 5 * max(0, level - 29)


# ---------------------------------------------------------------- fame rank ---
# lc-player-info (P6 stage 1): the rank a character's fame (reputation, S2C 0x03
# fame_points -> scene+0x27C) maps to. FUN_00442C90 (2008, called by the S2C 0x03 handler;
# 2009 FUN_00443970) walks this u32[99] table at exe VA 0x6F11D8 (2009 0x525B70, the same
# bytes): the rank is the first index i with fame < FAME_RANK[i], or 98 (0x62) at the last
# row, which is 0. It writes the rank to the local entity +0x9A (2009 +0x9E) - the value the
# 0x07/0x04/0x05 `rank_icon` carries for remote players and S2C 0x52 `rank` for the Player
# Info window. Index -> name is the 20-byte table at 0x70BDC8 (2009 0x54A848): 0 'Trainee'
# (the red fist of the retail name tags, RETAIL_VIDEO_CATALOG lSAXdVlJd8Y 0:58), 1..9
# 'Trainee Lv.1..9', 10 'Battler Lv.1' (2009 'Gladiator Lv.1'), ... 96 'Archangel',
# 97 'Monarch', 98 'Windslayer'. A value >= 99 has no name (garbage label) and no emblem.
FAME_RANK = (
    30, 100, 220, 400, 670, 1050, 1550, 2190, 2990, 3970, 5150, 6550,
    8190, 10090, 12280, 14780, 17610, 20790, 24350, 28310, 32700, 37540, 42850, 48660,
    55010, 61950, 69520, 77760, 86830, 96790, 107710, 119650, 132670, 146870, 162330, 179120,
    197310, 216980, 238400, 261670, 286890, 314160, 343580, 375300, 409440, 446110, 485420, 527490,
    572760, 621420, 673670, 729700, 789700, 853920, 922570, 995860, 1074000, 1157190, 1246130, 1341120,
    1442460, 1550450, 1665390, 1787660, 1917580, 2055470, 2201650, 2356440, 2520850, 2697400, 2886580, 3088870,
    3304750, 3534700, 3779580, 4039900, 4316180, 4608930, 4919580, 5249410, 5599290, 5970090, 6362670, 6777880,
    7217100, 7681220, 8171130, 8687720, 9231880, 9807240, 10416730, 11063150, 11752390, 12490450, 13288220, 14158140,
    15115540, 16172930, 0,
)
assert len(FAME_RANK) == 99
RANK_MAX = 98                 # 'Windslayer'; the client loop stops at index 0x62


def rank_for_fame(fame):
    """The rank index (0 'Trainee' .. 98 'Windslayer') the client computes for `fame`
    (FUN_00442C90: fame is a u32 there, so a negative value reads as a huge one)."""
    fame = int(fame or 0) & 0xFFFFFFFF
    for i, threshold in enumerate(FAME_RANK):
        if fame < threshold or i == RANK_MAX:
            return i
    return 0                      # unreachable: row 98 always stops the loop
