#!/usr/bin/env python3
"""
combo_hud_2009.py - retail grade words + combo counter for the EN 2009 client (Outspark v1.04 Build 14)
======================================================================================================
v2 (2026-09-24). It follows the retail rules in
re_tools/docs/GRADE_WORDS_COMBO_RULES_2026-09-24.md and applies that document's checker fixes A-D.
v1 was a count-based Good!/Great!/Wow! driven from the hit event at 0x412D70. v2 drops it:
retail footage rejects a count-based word (0 of 57 first-hit words fit). Build 14 knows the damage
of a hit only inside FUN_004194f0, so both the word and the count are driven from there.

WHAT IT DOES
  1. A grade word is a per-hit damage roll. It applies to every hit the local player lands on a
     monster, outside PvP rooms, and only where the server owns HP ([scene+0xF40] == 0):
         r = xorshift32 % 1000 ->  CRITICAL! x1.5 (5%) | BAD x0.75 (13%) | GOOD x1.25 (13%) | none x1.0
         damage = trunc(damage * M * J / 10^6), J = 900..1100 per mille (uniform jitter)
     Only BAD may land on 0 (no digit, no count, the word still shows). none, GOOD and CRITICAL!
     stay >= 1 (checker fix C).
     - A stored hit (the deferred skills of 0x41A564: stored to victim +0xE20, replayed later by
       FUN_00426b60) is rolled once, at the replay, so its word appears with its digit.
     - The word pops above the attacker for every graded hit, the first one included. A new word
       never removes an older one; they may overlap.
     - At most one word is spawned per frame, by priority CRITICAL! > GOOD > BAD. A multi-target
       swing therefore shows one word.
     - CRITICAL! can also play the shipped critical burst basic.hsi 0x14B on its victim.
       This is the CRIT_BURST knob; the evidence for it is a single video moment.
  2. The combo counter adds +1 for each damage call with damage > 0. A multi-victim swing adds one
     per victim. A 0-damage hit neither adds, refreshes nor resets.
     - The window is CHAIN_MS = 2000 ms from the last damaging hit. The counter shows while
       2 <= count and now < last + SHOW_MS (2000), then disappears in one frame (hard cut, no fade).
     - "N Combo!" pops on every increment: digits 1.45x ("Combo !" 1.4x) for 100 ms, 0.9x for
       60 ms, then 1.0x (rule doc (b); combo_art v2.1 pop sprites, t = now - last_ms).
     - Colour tier: green 2..19, orange from ORANGE_AT = 20. Red from RED_AT is opt-in: 0 = off.
       The evidence for red is Korean 2011 footage only.
  Rates, multipliers, jitter, window and tiers are named knobs below. They are estimates for the
  Outspark era: the ordering BAD <= none <= GOOD < CRITICAL! is robust; the exact values are a fit.

WHICH HITS ARE TOUCHED (cave A, at the damage clamp of FUN_004194f0)
  A hit is touched only if all of these hold:
    - attacker EBP == [scene+0x988] (local player)
    - victim byte +0x9C == 4 (monster)
    - [scene+0xF40] == 0
    - room [scene+0xFB4] (== game+0x4FC) is not PvP (+0x7C != 1)
  There is no event whitelist (checker fix B). The damage function's "event" is the victim's
  reaction code and says nothing about the attack kind.
  Among those hits, in this order:
    - return address [ESP+0x68] == 0x426BCD: FUN_00426b60 (CALL at 0x426BC8) replays a stored hit
      with param_7 = victim +0xE20 (its only caller of 0x4194F0). Rolled, graded, counted - the
      one roll of that hit (param_7 is ignored here; it may even be 0, see below).
    - a hit that 0x41A564 is about to store: param_5 byte [ESP+0x78] == 9 with attacker
      +0x118/+0x119 == 2/2, or == 6 with 4/2 (the exact test at 0x41A564..0x41A5C1). Untouched:
      no roll, no word, no PEND. 0x41A5C3 exits without reaching B and 0x41A5D0 stores EBX into
      victim +0xE20, so the stored value stays the game's own clamped damage (>= 1). Rolling here
      instead would show the word at the cast, not with the digit, and a BAD 0 stored there is
      read by the replay as param_7 == 0 = "no precomputed damage" (0x4195A4), which recomputes
      a full hit.
    - param_7 != 0 ([ESP+0x80]): a precomputed damage from the two recursive calls: the
      secondary hit at 0x41A313 (same attacker and victim) and the reflect at 0x41A46B (a
      0xB1A..0xB24 buff on the victim returns part of the damage, attacker and victim swapped:
      it passes the filters only when the local player's reflect hits a monster). It is counted
      but not rolled, so it gets no second word (checker fix A).
    - param_6 != 0 ([ESP+0x7C]): a damage-over-time tick (0x41889E, 0x430D66). GRADE_DOT = True
      (default): rolled and graded, never counted. False: untouched.
    - otherwise: rolled, graded, counted.
  The six branches into 0x41A55A include the life-drain path (0x41A512, damage = 2 x heal). It is
  rolled too; the effect is cosmetic only.
  The server needs no changes. When [scene+0xF40] == 0 and the room is not PvP, the damage only
  feeds the cap at victim +0xA0 and the digit accumulators +0x11B4..+0x11C8. There is no HP or
  aggro write, and the C2S hit report carries no damage.

INTEGRATION WITH patch_2009.py (unchanged contract)
  patch_2009.py imports this module and calls apply(data, off, put, iat, pe) after its smooth and
  aggro-rule blocks (on by default, --no-combo-hud skips it). Before writing anything, apply()
  checks every original byte, that the cave is all zero, that no hook or cave overlaps
  RESERVED (every other patch_2009 site), and the IAT slots. Only then does it write the cave and
  the four hooks and raise the section VirtualSize fields.

WHAT IS PATCHED (pristine exe = image base 0x400000, file offset = VA - 0x400000)
  0x41A55A  83FB017D05BB01000000 -> E9 <caveA> 90x5
            FUN_004194f0: the "damage >= 1" clamp. Cave A re-runs the clamp, then rolls or flags the
            hit. It returns to 0x41A564. The only branches into the range are the six that land on
            its first byte and the internal JGE to 0x41A564.
            Stack there (frame 0x58 + 4 pushes): EBX = damage, EBP = attacker, [ESP+0x20] victim,
            [ESP+0x28] scene, [ESP+0x68] return address, [ESP+0x74] event, [ESP+0x78] param_5,
            [ESP+0x7C] param_6, [ESP+0x80] param_7. Add 0x20 after PUSHAD.
  0x41A6AA  3BDA0F8422F6FFFF -> E9 <caveB> 909090
            the "damage == 0 -> return" test (EDX is 0 on every path here). Cave B counts a pending
            hit whose final damage is > 0, then runs CMP EBX,EDX / JZ 0x419CD4 / JMP 0x41A6B2.
            There is no call between A and B. The one early exit between them (0x41A5C3, the
            store to victim +0xE20) skips B; cave A leaves exactly those hits untouched (see
            WHICH HITS), so they set no PEND and store the game's own value.
  0x43E57D  E8BE220500 -> E8 <hud>
            frame render root FUN_0043e480: the CALL of the UI window manager FUN_00490840. The HUD
            spawns the queued word, draws the counter, then tail-jumps to 0x490840.
  0x43F6C9  8B86F4040000 -> E9 <init> 90
            FUN_0043f140 (runs once, from WinMain), right after ./hs/basic.hsi loaded. It seeds the
            RNG (rdtsc | 1), loads ./hs/combo001.hsi into the basic sprite control and probes the
            art with GetExtent.
  0x412D70  NOT patched any more (v1's hit hook). Nothing needs restoring: every build starts from
            the pristine exe.
  cave      0x4C6220..CAVE_END, in the zero .text tail 0x4C61F7..0x4C6FFF. The smooth cave of
            patch_2009 is 0x4C6200..0x4C6214, so there is no overlap. The catch-up knock fix
            cave of patch_2009 is 0x4C6F00..0x4C6F3B (in RESERVED), so the combo cave may grow
            to 0x4C6F00 at most.
            Layout: strings, then the art probe table (16 B per entry), then A, B, HUD and init,
            each 16-byte aligned with 0xCC padding.
  state     0x551680..0x55169F, in the zero .data slack past VirtualSize (0x551668). It is BSS:
            zeroed by the loader, not in the file, and nothing in the exe references
            0x551668..0x551FFF.
  headers   .text VirtualSize >= CAVE_END - 0x401000 and .data VirtualSize >= 0x1A6A0. max() is
            used, so the result does not depend on the order of the patches.

STATE (little-endian; read it live with ReadProcessMemory at 0x551680)
  +0x00 u32 count         damaging hits in the current chain (the HUD only hides it, it does not
                          zero it; cave B restarts at 1 when now - last >= CHAIN_MS)
  +0x04 u32 last_ms       clock of the last damaging hit ([0x54EF34] = game+0x364, the per-frame
                          timeGetTime stamped at 0x40E670)
  +0x08 u32 show_until    last_ms + SHOW_MS
  +0x0C u32 art_base      first id of combo001.hsi (342 when loaded right after basic.hsi); 0 = no art
  +0x10 u32 rng           xorshift32 state
  +0x14 u16 last_word     last grade-word sprite spawned (debug)
  +0x16 u8  grade         word queued for the next HUD frame: 0 none, 1 BAD, 2 GOOD, 3 CRITICAL!
                          (a higher value wins inside one frame)
  +0x17 u8  pend          set by A, consumed by B: count this hit if its final damage is > 0
  +0x18 u32 art_flags     1 base (v1 counter art), 2 grade words, 4 orange set, 8 red set
  +0x1C u32 crit_uid      victim uid of the queued CRITICAL! (target of the 0x14B burst)

GRADE WORDS (spawned from the HUD hook, not from cave A)
  Cave A may run inside FUN_0042e790's walk of the effect lists, so the spawn is deferred to the
  HUD hook:
      FUN_0042cd90(0x54EBD0, sprite, [scene+0x224], 0, 0, 0, 1, 10, 0)
  This is kind 10 in the front list: the effect follows the attacker by uid and is freed after the
  sprite's total frame time. It is the same call shape the game uses at 0x44FB34.
  Sprites (art flag 2): GOOD art+32, BAD art+33, CRITICAL! art+34 (3 frames, 860 ms, ink centre
  (0,-120) from the draw point).
  Without that art: GOOD -> 0x14C Good!, CRITICAL! -> 0x14E Wow!, BAD -> no word.
  With CRIT_BURST, a CRITICAL! also spawns FUN_0042cd90(0x54EBD0, 0x14B, crit_uid, 0,0,0, 1, 10, 0):
  the 10-frame 580 ms burst on the victim.

THE COUNTER (HUD hook, every frame)
  It draws only if all of these hold:
    - count >= 2 and 0 < show_until - now <= SHOW_MS (the hard cut)
    - scene exists and scene mode is 4..6
    - the local player exists, the room is not PvP, and the player is not blinded (game+0x1700)
  Colour tier from the (uncapped) count: green, orange if ORANGE_AT <= count, red if 0 < RED_AT <= count.
  With the art (art_base != 0 and, for orange/red, their art flag):
    - set = art_base + {0 green, 35 orange, 57 red}
    - the digits are the pop sprites set+11+d, centred at y 433; the rightmost digit is centred at
      x 684, each further digit 31 px to its left
    - the word is the pop sprite set+21, centred at (743, 442)
    - t = now - last_ms, so the pop restarts on every increment
    - the pop sprites hold their last frame (repeat 0)
  Fallback:
    - the shipped damage digits, scaled 4/3 about their top-left at x 673 - 27*k, y 415:
      green 0x5F+d for the green tier, red 0x06+d for the orange and red tiers. SetScaling is
      restored to 1.0 after each digit, because the damage popups share these sprites.
    - CIME::Draw_Text "Combo!" at (724, 450), bold with a 4-way outline, in the tier's colour.
  The count shown is capped at 999. No SetModulate is used, so the counter is always fully opaque.

ART (optional; the patch runs without it)
  The four files live in .\\combo_art\\ next to this module. build_combo_art.py builds them; the
  manifest and README there list the ids. install_assets(dst_hs_dir) copies them into the game's
  hs\\ folder, and nothing is copied unless you call it yourself. Copy order:
      combo001_a8.hsc  combo001_a8.hsc1  combo001_a8.hsc2  and LAST  combo001.hsi
  Each file goes through '<name>.tmp' + os.replace. An interrupted install leaves no .hsi, and the
  fallback is used.
  Before writing, install_assets checks that hs\\ has fx003_a8.hsc/.hsc1/.hsc2 (image 2 of
  combo001.hsi, the shipped damage digits). No shipped file is replaced.
  At startup the init hook calls
      OpenFromFile([0x54E844], "./hs/combo001.hsi", 0, 1, 11 x NULL, 1)
  That is the map loader's call shape; param_2 = 0 means no RemoveAllImage.
    - If the call returns 0 (file missing, or it fails its SHA-1 check), base 342 is tried
      instead: "variant A", a basic.hsi with the art appended.
    - Every probe in ART_PROBES must return its exact GetExtent size before its flag bit is set.
      art_base is accepted only if both counter probes (+0 = 40x52, +10 = 76x26) pass.
  v1 art (32 sprites) sets only flag 1, so v2 still runs with it: v1 counter art, fallback words,
  red-digit fallback from 20 on.
  Upgrading hs\\ from the v1 files: delete hs\\combo001.hsi first, then the three textures, then run
  --install-assets. It refuses to overwrite files that differ. From a v2.0 install only
  combo001.hsi differs (v2.1 retimed the pop frames; the textures are byte-identical): delete it
  and run --install-assets.
  The art was rendered from Windows fonts: Agency FB Bold (AGENCYB.TTF, GOOD and CRITICAL!),
  Franklin Gothic Heavy Italic (FRAHVIT.TTF, BAD and the counter digits) and Eras Bold ITC
  (ERASBD.TTF). Check their licence before publishing combo_art\\.

LIVE-TEST PLAN (not done yet: everything above is static RE plus Unicorn emulation of the caves)
  Rules for every run:
    - use a patched copy, run non-elevated
    - run only while the user is not playing
    - make one change per run
    - Lv1 Novice against Ssiyo
  0. Prerequisite, unrelated to this patch: hs\\fps_low.hsi must pass its SHA-1 footer check, or
     FUN_0043f140 stops at 0x43F6D8 "Could not load FPS_low Sprite." before the world loads.
     Read [scene+0xF40] once in the field: it must be 0, or no hit is ever graded.
  1. Record 60 swings at 30 fps. Expect:
     - a word on about 30% of hits, first hits included
     - BAD at Lv1 often shows no digit, and those hits do not count
     - CRITICAL! shows the largest digit
     - "2 Combo!" on the 2nd damaging hit
     - a hard cut 2.0 s +-1 frame after the last damaging hit
  2. Window: swings every 1.8 s continue the chain; swings every 2.3 s restart it.
  3. Multiple victims: stack two Ssiyos; one swing that damages both adds 2.
  4. Resets:
     - being hit changes nothing
     - kill one Ssiyo and hit another within 2 s: the chain continues
     - a 0-damage (BAD) hit neither adds nor refreshes
  5. Colour tiers:
     - first a dev build with ORANGE_AT = 3 and RED_AT = 5
     - then the real values, reaching 20+ with a pack or a multi-target skill
  6. Regressions:
     - red digits for monster-on-player hits keep their Run 0 values
     - PvP room: no word, no counter, damage unchanged
     - server [DAMAGE] lines and kills unchanged
     - read 0x551680..0x55169F through ReadProcessMemory: count, rng != 0, art_base 342, art_flags 15
  7. Fallbacks: run with the v1 combo001 art (flags 1), then with no art at all (art_base 0).
  8. Damage over time (GRADE_DOT = True): ticks vary and get words, but do not count.
  9. Stored hits (needs a character with +0x118/+0x119 = 2/2 and its param_5-9 skill, or 4/2
     and its param_5-6 skill): no word at the cast; the word, the digit and +1 appear together
     when the stored hit lands (effect timer FUN_004185b0 0x4188EC or the removal packet
     FUN_00451960 0x45BB9B). A BAD there on a 1-damage hit shows the word and no digit, and
     the full damage must never appear instead.

usage (no file is written unless you ask for it):
  python combo_hud_2009.py --check           re-assemble with keystone, compare with PREBUILT, list
  python combo_hud_2009.py --install-assets  copy the four art files into .\\hs (explicit only)
"""
import hashlib
import os
import struct
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
B = 0x400000

# ================================================================ knobs (behaviour)
# ---- grade roll: one roll per ordinary damaging hit of the local player on a monster ----
# Rates are per mille of rolled hits. Retail estimate (Outspark era, 356 hits): CRITICAL! 5.3%
# (CI 3.4-8.2), BAD 13.5% (10-17), GOOD 12.6% (10-16). A4tc (2011) had BAD:GOOD = 17:4, so rates
# may differ between builds. Their sum must not exceed 1000; the remainder gets no word.
RATE_CRIT_PM = 50
RATE_BAD_PM = 130
RATE_GOOD_PM = 130
# Damage multipliers per mille (fit: BAD 0.64-0.83, GOOD 1.12-1.34, CRITICAL! 1.44-1.68 measured).
MULT_BAD_PM = 750
MULT_NONE_PM = 1000
MULT_GOOD_PM = 1250
MULT_CRIT_PM = 1500
# Uniform jitter per mille applied to every rolled hit: J in [JITTER_LO_PM, JITTER_HI_PM]
# (fit: +-10%; yal's basic hits needed +-15%). MULT_x * JITTER_HI_PM must stay <= 2,000,000.
JITTER_LO_PM = 900
JITTER_HI_PM = 1100
# Damage-over-time ticks (param_6 != 0): True = rolled and graded (word + varied digit), never
# counted - the rule doc's retail evidence (four graded ticks with no input) and its live-test
# step 8. False = untouched (no roll, no word, no count), the strict "param_6 == 0" reading.
GRADE_DOT = True
# CRITICAL! also plays the shipped critical burst (basic.hsi 0x14B, 10 frames) on the victim.
# Inferred from one retail moment (y-ONQ 2:08); set False to show the word only.
CRIT_BURST = True
# ---- combo counter ----
CHAIN_MS = 2000               # a damaging hit within CHAIN_MS of the previous one continues the chain
SHOW_MS = CHAIN_MS            # counter visible until last + SHOW_MS, then a hard cut (no fade)
ORANGE_AT = 20                # orange digits + word from this count (exact in the 2012 EN client); 0 = off
RED_AT = 0                    # red from this count, e.g. 50 (2011 KR footage only): 0 = off (opt-in)
MAX_SHOWN = 999               # the number drawn is capped here (the count itself is not)

# ================================================================ addresses (pristine Build 14)
A_SITE, A_ORIG, A_RET = 0x41A55A, bytes.fromhex('83FB017D05BB01000000'), 0x41A564
REPLAY_RET = 0x426BCD         # return address of FUN_00426b60's CALL 0x4194F0 (0x426BC8), the stored-hit replay
B_SITE, B_ORIG = 0x41A6AA, bytes.fromhex('3BDA0F8422F6FFFF')
B_ZERO, B_NEXT = 0x419CD4, 0x41A6B2        # damage == 0 -> epilogue | else continue
HUD_SITE, HUD_ORIG, HUD_NEXT = 0x43E57D, bytes.fromhex('E8BE220500'), 0x490840
INIT_SITE, INIT_ORIG, INIT_RET = 0x43F6C9, bytes.fromhex('8B86F4040000'), 0x43F6CF

CAVE_VA = 0x4C6220            # zero .text tail 0x4C61F7..0x4C6FFF; smooth cave = 0x4C6200..0x4C6214
CAVE_LIMIT = 0x4C7000         # end of .text raw data
STATE_VA = 0x551680           # zero .data slack; .data VirtualSize ends at 0x551668
ST_COUNT, ST_LAST, ST_SHOW, ST_BASE = STATE_VA, STATE_VA + 4, STATE_VA + 8, STATE_VA + 12
ST_RNG, ST_WORD, ST_GRADE, ST_PEND = STATE_VA + 0x10, STATE_VA + 0x14, STATE_VA + 0x16, STATE_VA + 0x17
ST_FLAGS, ST_CRIT_UID = STATE_VA + 0x18, STATE_VA + 0x1C
STATE_END = STATE_VA + 0x20
DATA_VSIZE_MIN = STATE_END - 0x537000       # 0x1A6A0

# every other edit of patch_2009.py (va, length): hooks and cave must not touch these
RESERVED = (
    (0x4C6200, 0x15),                           # smooth cave
    (0x40E7A0, 6), (0x40E732, 6), (0x40E76B, 6),  # smooth call sites
    (0x416BF2, 52),                             # aggro contact gate
    (0x419272, 1),                              # aggro: no wander swing
    (0x43C3CC, 41), (0x43C416, 4),              # name box (level rule 41 B; kr rule 15 B)
    (0x43F0FC, 4),                              # no-response timer jump-table entry
    (0x452610, 15),                             # LAN probe NOP
    (0x4409C8, 5),                              # version push (long address)
    (0x49F420, 1), (0x49F440, 1), (0x49F460, 1), (0x49F480, 1),   # X-Trap stubs
    (0x4405CF, 4), (0x4407FA, 4), (0x45F19A, 4), (0x488C42, 4),   # --p2 port immediates
    (0x52DD54, 16), (0x52DFD7, 53),             # version server address slots
    (0x4138C6, 5), (0x4C6F00, 0x3C),            # catch-up knock fix F1: hook + cave (patch_2009 KNOCK_*)
)

GAME = 0x54EBD0               # CMyD3DApplication (game_state)
CLOCK = 0x54EF34              # game+0x364 = timeGetTime() of this frame
SCENE_PTR = 0x54F0C0          # game+0x4F0
ROOM_PTR = 0x54F0CC           # game+0x4FC, room mode at +0x7C (1 = PvP); == [scene+0xFB4]
BLIND = GAME + 0x1700         # set by the entity renderer for the 'blind' buffs
SPRITES = 0x54E844            # basic sprite control (basic.hsi)
CIME = 0x54E840
FX_SPAWN = 0x42CD90           # FUN_0042cd90, stdcall 9 args, RET 0x24

IMPORTS = {                   # name -> IAT slot VA expected in Build 14
    'DrawSprite': ('?DrawSprite@CSpriteControl@@QAEHKHHKHHHHH@Z', 0x4C7360),
    'SetScaling': ('?SetScaling@CSpriteControl@@QAEHKMMH@Z', 0x4C7390),
    'GetExtent': ('?GetExtent@CSpriteControl@@QAEHKAAI0@Z', 0x4C7398),
    'OpenFromFile': ('?OpenFromFile@CSpriteControl@@QAEKPBDHHPAD1111111111H@Z', 0x4C735C),
    'Draw_Text': ('?Draw_Text@CIME@@QAEXPADJJKKKHHIHHHH@Z', 0x4C7564),
}

# ================================================================ art ids and layout
G_BAD, G_GOOD, G_CRIT = 1, 2, 3                        # grade codes = queue priority
ART_HSI = './hs/combo001.hsi'
ART_VARIANT_A_BASE = 342      # art appended to basic.hsi (variant A)
FLAG_BASE, FLAG_GRADES, FLAG_ORANGE, FLAG_RED = 1, 2, 4, 8
# (offset from art_base, GetExtent w, h, flag bit); a bit is set only if all its probes match
ART_PROBES = ((0, 40, 52, FLAG_BASE), (10, 76, 26, FLAG_BASE),
              (32, 80, 28, FLAG_GRADES), (33, 68, 28, FLAG_GRADES), (34, 138, 32, FLAG_GRADES),
              (35, 40, 52, FLAG_ORANGE), (45, 76, 26, FLAG_ORANGE),
              (57, 40, 52, FLAG_RED), (67, 76, 26, FLAG_RED))
ART_GOOD, ART_BAD, ART_CRIT = 32, 33, 34               # grade-word offsets
SET_GREEN, SET_ORANGE, SET_RED = 0, 35, 57             # counter sets: digit +d, word +10, pops +11+d / +21
FB_GOOD, FB_CRIT, FB_BURST = 0x14C, 0x14E, 0x14B       # shipped basic.hsi: Good!, Wow!, critical burst
ART_DIGIT_Y, ART_DIGIT_X0, ART_DIGIT_ADV = 433, 684, 31   # sprite centre; x of the rightmost digit
ART_WORD_X, ART_WORD_Y = 743, 442
# fallback: shipped damage digits (40x38 / 38x38 cells, top-left anchor) + CIME text
FB_DIGIT_GREEN, FB_DIGIT_RED = 0x5F, 0x06
FB_DIGIT_Y, FB_DIGIT_X0, FB_DIGIT_ADV = 415, 673, 27
FB_SCALE = 0x3FAAAAAB         # 4/3 as float
FB_WORD_X, FB_WORD_Y = 724, 450
FB_RGB_GREEN, FB_RGB_ORANGE, FB_RGB_RED = 0x32FF00, 0xFF9A10, 0xFF3018
SCREEN = '0x80000000'         # camX/camY sentinel = raw screen space; map W/H = 0x640/0x4B0
RNG_FALLBACK_SEED = 0x2545F491  # used only if the init hook never ran (rng == 0)

STRINGS = ((0x4C6220, b'Combo!\0'), (0x4C6228, ART_HSI.encode() + b'\0'))
STR_COMBO, STR_HSI = STRINGS[0][0], STRINGS[1][0]
PROBE_VA = 0x4C6240
PROBE_BLOB = b''.join(struct.pack('<4I', *p) for p in ART_PROBES)
PROBE_END = PROBE_VA + len(PROBE_BLOB)
CODE_VA = (PROBE_END + 15) & ~15


def _knob_check():
    """Refuse knob values the caves cannot handle."""
    t = RATE_CRIT_PM + RATE_BAD_PM + RATE_GOOD_PM
    assert all(r >= 0 for r in (RATE_CRIT_PM, RATE_BAD_PM, RATE_GOOD_PM)) and t <= 1000, 'rates: 0..1000 in total'
    assert 0 < JITTER_LO_PM <= JITTER_HI_PM, 'jitter range'
    for m in (MULT_BAD_PM, MULT_NONE_PM, MULT_GOOD_PM, MULT_CRIT_PM):
        # damage (< 2^31) * M * J must stay below 2^32 * 10^6 so the DIV cannot fault
        assert 0 < m and m * JITTER_HI_PM <= 2_000_000, 'multiplier x jitter too large'
    assert 0 < CHAIN_MS < 0x7FFFFFFF and 0 < SHOW_MS < 0x7FFFFFFF
    assert ORANGE_AT == 0 or ORANGE_AT >= 2
    assert RED_AT == 0 or (RED_AT >= 2 and (ORANGE_AT == 0 or RED_AT > ORANGE_AT)), 'RED_AT must be above ORANGE_AT'
    assert 1 <= MAX_SHOWN <= 999999


def _xorshift():
    # xorshift32 (13, 17, 5) on EAX, EDX scratch
    return """
    mov  edx, eax
    shl  edx, 13
    xor  eax, edx
    mov  edx, eax
    shr  edx, 17
    xor  eax, edx
    mov  edx, eax
    shl  edx, 5
    xor  eax, edx"""


def _asm_a(imp):
    t_crit = RATE_CRIT_PM
    t_bad = t_crit + RATE_BAD_PM
    t_good = t_bad + RATE_GOOD_PM
    dot = ("""
    xor  ebx, ebx                           ; DoT tick: graded, never counted
    jmp  L_roll""" if GRADE_DOT else """
    jmp  L_aout                             ; DoT tick: untouched""")
    return f"""
; ---- cave A: jumped to from 0x41A55A (replaces CMP EBX,1 / JGE / MOV EBX,1) ----
    cmp  ebx, 1                             ; the replaced clamp: damage >= 1
    jge  L_a0
    mov  ebx, 1
L_a0:
    pushad                                  ; saved EBX at [esp+0x10]
    mov  byte ptr [{ST_PEND:#x}], 0
    mov  esi, dword ptr [esp+0x48]          ; scene ([esp+0x28] before pushad)
    cmp  ebp, dword ptr [esi+0x988]         ; attacker is the local player
    jne  L_aout
    mov  edi, dword ptr [esp+0x40]          ; victim ([esp+0x20])
    cmp  byte ptr [edi+0x9c], 4             ; monsters only
    jne  L_aout
    cmp  dword ptr [esi+0xf40], 0           ; client-owned HP: never touch
    jne  L_aout
    mov  eax, dword ptr [esi+0xfb4]         ; room (= game+0x4FC)
    test eax, eax
    jz   L_a1
    cmp  dword ptr [eax+0x7c], 1            ; PvP room
    je   L_aout
L_a1:
    cmp  dword ptr [esp+0x88], {REPLAY_RET:#x}  ; return address: FUN_00426b60 replays a stored hit
    je   L_replay
    movzx eax, byte ptr [esp+0x98]          ; param_5 ([esp+0x78]): the store test of 0x41A564
    movzx ecx, word ptr [ebp+0x118]         ; attacker +0x118 | +0x119 << 8
    cmp  eax, 9
    jne  L_s6
    cmp  ecx, 0x0202
    je   L_aout                             ; stored to +0xE20 at 0x41A5D0: rolled at the replay
L_s6:
    cmp  eax, 6
    jne  L_a2
    cmp  ecx, 0x0204
    je   L_aout                             ; stored (param_5 6, 4/2)
L_a2:
    cmp  dword ptr [esp+0xa0], 0            ; param_7: precomputed damage (secondary hit)
    jne  L_apend                            ; count it, no second roll or word
    mov  ebx, 1                             ; ebx = count this hit
    cmp  dword ptr [esp+0x9c], 0            ; param_6: damage-over-time effect id
    je   L_roll{dot}
L_replay:
    mov  ebx, 1                             ; the stored hit: rolled once, here, with its digit
L_roll:
    mov  eax, dword ptr [{ST_RNG:#x}]
    test eax, eax
    jnz  L_r0
    mov  eax, {RNG_FALLBACK_SEED:#x}
L_r0:{_xorshift()}
    mov  dword ptr [{ST_RNG:#x}], eax
    xor  edx, edx
    mov  ecx, 1000
    div  ecx                                ; edx = r = 0..999
    xor  esi, esi                           ; grade 0 = no word
    mov  ecx, {MULT_NONE_PM}
    cmp  edx, {t_crit}
    jb   L_gcrit
    cmp  edx, {t_bad}
    jb   L_gbad
    cmp  edx, {t_good}
    jae  L_jit
    mov  esi, {G_GOOD}
    mov  ecx, {MULT_GOOD_PM}
    jmp  L_jit
L_gcrit:
    mov  esi, {G_CRIT}
    mov  ecx, {MULT_CRIT_PM}
    jmp  L_jit
L_gbad:
    mov  esi, {G_BAD}
    mov  ecx, {MULT_BAD_PM}
L_jit:
    mov  eax, dword ptr [{ST_RNG:#x}]{_xorshift()}
    mov  dword ptr [{ST_RNG:#x}], eax
    push ecx
    xor  edx, edx
    mov  ecx, {JITTER_HI_PM - JITTER_LO_PM + 1}
    div  ecx
    pop  ecx
    add  edx, {JITTER_LO_PM}                ; J per mille
    imul ecx, edx                           ; M * J (<= 2,000,000)
    mov  eax, dword ptr [esp+0x10]          ; clamped damage (>= 1)
    mul  ecx
    mov  ecx, 1000000
    div  ecx                                ; trunc(damage * M * J / 10^6)
    test eax, eax
    jns  L_r1
    mov  eax, 0x7fffffff
L_r1:
    cmp  esi, {G_BAD}
    je   L_r2                               ; only BAD may reach 0
    test eax, eax
    jnz  L_r2
    inc  eax
L_r2:
    mov  dword ptr [esp+0x10], eax          ; becomes EBX at popad
    test esi, esi
    jz   L_rq
    movzx eax, byte ptr [{ST_GRADE:#x}]
    cmp  esi, eax
    jbe  L_rq                               ; one word per frame: CRITICAL! > GOOD > BAD
    mov  eax, esi
    mov  byte ptr [{ST_GRADE:#x}], al
    cmp  esi, {G_CRIT}
    jne  L_rq
    mov  eax, dword ptr [edi+0x88]          ; victim uid (burst target)
    mov  dword ptr [{ST_CRIT_UID:#x}], eax
L_rq:
    test ebx, ebx
    jz   L_aout
L_apend:
    mov  byte ptr [{ST_PEND:#x}], 1
L_aout:
    popad
    jmp  {A_RET:#x}
"""


def _asm_b(imp):
    return f"""
; ---- cave B: jumped to from 0x41A6AA (replaces CMP EBX,EDX / JZ 0x419CD4); EDX == 0 here ----
    cmp  byte ptr [{ST_PEND:#x}], 0
    je   L_bgo
    mov  byte ptr [{ST_PEND:#x}], 0
    test ebx, ebx
    jz   L_bgo                              ; 0 damage: no digit, no count, no refresh
    push eax
    push ecx
    mov  eax, dword ptr [{CLOCK:#x}]
    mov  ecx, eax
    sub  ecx, dword ptr [{ST_LAST:#x}]      ; now - last (unsigned, wrap-safe)
    cmp  ecx, {CHAIN_MS}
    mov  ecx, dword ptr [{ST_COUNT:#x}]
    jb   L_b1
    xor  ecx, ecx                           ; window passed: restart at 1
L_b1:
    inc  ecx
    mov  dword ptr [{ST_COUNT:#x}], ecx
    mov  dword ptr [{ST_LAST:#x}], eax
    add  eax, {SHOW_MS}
    mov  dword ptr [{ST_SHOW:#x}], eax
    pop  ecx
    pop  eax
L_bgo:
    cmp  ebx, edx                           ; the replaced test
    je   {B_ZERO:#x}
    jmp  {B_NEXT:#x}
"""


def _asm_hud(imp):
    burst = f"""
    cmp  ebx, {G_CRIT}
    jne  L_cnt
    mov  eax, dword ptr [{ST_CRIT_UID:#x}]
    mov  dword ptr [{ST_CRIT_UID:#x}], 0
    test eax, eax
    jz   L_cnt
    push 0
    push 10
    push 1
    push 0
    push 0
    push 0
    push eax                                ; victim uid: the burst follows the victim
    push {FB_BURST:#x}
    push {GAME:#x}
    call {FX_SPAWN:#x}""" if CRIT_BURST else ''
    tiers = ''
    if ORANGE_AT:
        tiers += f"""
    cmp  eax, {ORANGE_AT}
    jb   L_tier
    mov  ecx, {SET_ORANGE}
    mov  edx, {FLAG_ORANGE}
    mov  esi, {FB_DIGIT_RED:#x}
    mov  edi, {FB_RGB_ORANGE:#x}"""
    if RED_AT:
        tiers += f"""
    cmp  eax, {RED_AT}
    jb   L_tier
    mov  ecx, {SET_RED}
    mov  edx, {FLAG_RED}
    mov  esi, {FB_DIGIT_RED:#x}
    mov  edi, {FB_RGB_RED:#x}"""
    return f"""
; ---- HUD: called from 0x43E57D (was CALL 0x490840); ends by tail-jumping to 0x490840 ----
    pushad
; -- 1. spawn the grade word queued by cave A (outside FUN_0042e790's list walk) --
    movzx ebx, byte ptr [{ST_GRADE:#x}]
    test ebx, ebx
    jz   L_cnt
    mov  byte ptr [{ST_GRADE:#x}], 0
    mov  esi, dword ptr [{SCENE_PTR:#x}]
    test esi, esi
    jz   L_cnt
    mov  ecx, dword ptr [esi+0xf18]         ; scene mode 4..6 only
    sub  ecx, 4
    cmp  ecx, 2
    ja   L_cnt
    test byte ptr [{ST_FLAGS:#x}], {FLAG_GRADES}
    jz   L_wfb
    mov  ecx, dword ptr [{ST_BASE:#x}]
    lea  eax, [ecx+{ART_GOOD}]
    cmp  ebx, {G_GOOD}
    je   L_wsp
    lea  eax, [ecx+{ART_BAD}]
    cmp  ebx, {G_BAD}
    je   L_wsp
    lea  eax, [ecx+{ART_CRIT}]
    jmp  L_wsp
L_wfb:
    mov  eax, {FB_GOOD:#x}                  ; Good!
    cmp  ebx, {G_GOOD}
    je   L_wsp
    cmp  ebx, {G_BAD}
    je   L_cnt                              ; no BAD word without the art
    mov  eax, {FB_CRIT:#x}                  ; Wow! for CRITICAL!
L_wsp:
    mov  word ptr [{ST_WORD:#x}], ax
    push 0                                  ; a9
    push 10                                 ; kind 10: follows the entity with uid a3
    push 1                                  ; front list game+0x440
    push 0                                  ; y offset
    push 0                                  ; x offset
    push 0
    push dword ptr [esi+0x224]              ; local player uid
    push eax                                ; sprite
    push {GAME:#x}
    call {FX_SPAWN:#x}                      ; stdcall, RET 0x24{burst}
L_cnt:
; -- 2. the counter --
    mov  ebx, dword ptr [{ST_COUNT:#x}]
    cmp  ebx, 2
    jb   L_done                             ; hidden at 0 and 1
    mov  eax, dword ptr [{CLOCK:#x}]
    mov  ecx, dword ptr [{ST_SHOW:#x}]
    sub  ecx, eax                           ; remaining = show_until - now
    test ecx, ecx                           ; sign of the difference (wrap-safe)
    jle  L_done                             ; hard cut
    cmp  ecx, {SHOW_MS}
    ja   L_done
    mov  ebp, eax
    sub  ebp, dword ptr [{ST_LAST:#x}]      ; t = ms since the last increment (pop timing)
    mov  esi, dword ptr [{SCENE_PTR:#x}]
    test esi, esi
    jz   L_done
    mov  ecx, dword ptr [esi+0xf18]         ; scene mode 4..6 only
    sub  ecx, 4
    cmp  ecx, 2
    ja   L_done
    cmp  dword ptr [esi+0x988], 0           ; local player
    je   L_done
    mov  ecx, dword ptr [{ROOM_PTR:#x}]
    test ecx, ecx
    jz   L_room
    cmp  dword ptr [ecx+0x7c], 1            ; PvP room
    je   L_done
L_room:
    cmp  dword ptr [{BLIND:#x}], 0          ; blinded: the world overlay is black
    jne  L_done
    cmp  ebx, {MAX_SHOWN}
    jbe  L_capped
    mov  ebx, {MAX_SHOWN}
L_capped:
; tier: ecx = art set offset, edx = art flag it needs (0 = none), esi = fallback digit, edi = fallback rgb
    mov  eax, dword ptr [{ST_COUNT:#x}]
    mov  ecx, {SET_GREEN}
    xor  edx, edx
    mov  esi, {FB_DIGIT_GREEN:#x}
    mov  edi, {FB_RGB_GREEN:#x}{tiers}
L_tier:
    mov  eax, dword ptr [{ST_BASE:#x}]
    test eax, eax
    jz   L_fb
    test edx, edx
    jz   L_art
    test dword ptr [{ST_FLAGS:#x}], edx
    jz   L_fb
L_art:
; ---- combo art: pop sprites, t = ms since the increment ----
    lea  esi, [eax+ecx]                     ; set base
    mov  edi, {ART_DIGIT_X0}
L_adig:
    mov  eax, ebx
    xor  edx, edx
    mov  ecx, 10
    div  ecx
    mov  ebx, eax
    lea  eax, [esi+edx+11]                  ; digit pop sprite
    push 0x4b0
    push 0x640
    push {SCREEN}
    push {SCREEN}
    push -1
    push ebp
    push {ART_DIGIT_Y}
    push edi
    push eax
    mov  ecx, dword ptr [{SPRITES:#x}]
    call dword ptr [{imp['DrawSprite']:#x}]
    sub  edi, {ART_DIGIT_ADV}
    test ebx, ebx
    jnz  L_adig
    lea  eax, [esi+21]                      ; "Combo !" pop
    push 0x4b0
    push 0x640
    push {SCREEN}
    push {SCREEN}
    push -1
    push ebp
    push {ART_WORD_Y}
    push {ART_WORD_X}
    push eax
    mov  ecx, dword ptr [{SPRITES:#x}]
    call dword ptr [{imp['DrawSprite']:#x}]
    jmp  L_done
; ---- fallback: shipped damage digits x4/3 + CIME text in the tier colour ----
L_fb:
    push edi                                ; [esp] = rgb
    mov  ebp, esi                           ; digit sprite base
    mov  edi, {FB_DIGIT_X0}
L_fdig:
    mov  eax, ebx
    xor  edx, edx
    mov  ecx, 10
    div  ecx
    mov  ebx, eax
    lea  esi, [ebp+edx]
    push 0
    push {FB_SCALE:#x}
    push {FB_SCALE:#x}
    push esi
    mov  ecx, dword ptr [{SPRITES:#x}]
    call dword ptr [{imp['SetScaling']:#x}]
    push 0x4b0
    push 0x640
    push {SCREEN}
    push {SCREEN}
    push -1
    push 0
    push {FB_DIGIT_Y}
    push edi
    push esi
    mov  ecx, dword ptr [{SPRITES:#x}]
    call dword ptr [{imp['DrawSprite']:#x}]
    push 0                                  ; restore: shared with the damage digits
    push 0x3f800000
    push 0x3f800000
    push esi
    mov  ecx, dword ptr [{SPRITES:#x}]
    call dword ptr [{imp['SetScaling']:#x}]
    sub  edi, {FB_DIGIT_ADV}
    test ebx, ebx
    jnz  L_fdig
    pop  eax                                ; rgb
    mov  ecx, dword ptr [{CIME:#x}]
    test ecx, ecx
    jz   L_done
    push 0x4b0
    push 0x640
    push -1
    push -1
    push 0                                  ; align left
    push {SCREEN}
    push {SCREEN}
    push 0xff000000                         ; outline colour
    push 9                                  ; bold + 4-way outline
    or   eax, 0xff000000
    push eax
    push {FB_WORD_Y}
    push {FB_WORD_X}
    push {STR_COMBO:#x}
    call dword ptr [{imp['Draw_Text']:#x}]
L_done:
    popad
    jmp  {HUD_NEXT:#x}
"""


def _asm_init(imp):
    zeros = '\n'.join(['    push 0'] * 11)
    return f"""
; ---- init: jumped to from 0x43F6C9 (replaces MOV EAX,[ESI+0x4F4]) after basic.hsi loaded ----
    pushad
    rdtsc
    or   eax, 1                             ; never 0 (xorshift32 would stick at 0)
    mov  dword ptr [{ST_RNG:#x}], eax
    mov  ecx, dword ptr [{SPRITES:#x}]
    test ecx, ecx
    jz   L_iout
    push 1
{zeros}
    push 1                                  ; reuse if already loaded (map loader shape)
    push 0                                  ; 0 = no RemoveAllImage
    push {STR_HSI:#x}
    call dword ptr [{imp['OpenFromFile']:#x}]
    test eax, eax
    jnz  L_i1
    mov  eax, {ART_VARIANT_A_BASE}          ; variant A: art appended to basic.hsi
L_i1:
    mov  esi, eax                           ; candidate art_base
    mov  ebx, {FLAG_BASE | FLAG_GRADES | FLAG_ORANGE | FLAG_RED}
    mov  edi, {PROBE_VA:#x}                 ; (offset, w, h, flag) x {len(ART_PROBES)}
L_pl:
    mov  eax, dword ptr [edi]
    add  eax, esi
    call L_ext
    cmp  eax, dword ptr [edi+4]
    jne  L_pbad
    cmp  edx, dword ptr [edi+8]
    je   L_pnext
L_pbad:
    mov  eax, dword ptr [edi+12]
    not  eax
    and  ebx, eax                           ; one failed probe clears its flag
L_pnext:
    add  edi, 16
    cmp  edi, {PROBE_END:#x}
    jb   L_pl
    test ebx, {FLAG_BASE}
    jz   L_iout                             ; no counter art -> built-in fallback everywhere
    mov  dword ptr [{ST_FLAGS:#x}], ebx
    mov  dword ptr [{ST_BASE:#x}], esi
L_iout:
    popad
    mov  eax, dword ptr [esi+0x4f4]         ; the replaced instruction
    jmp  {INIT_RET:#x}
; ---- L_ext: eax = sprite id -> eax = w, edx = h (0 when GetExtent rejects the id); ecx clobbered ----
L_ext:
    push 0                                  ; h
    push 0                                  ; w
    mov  ecx, esp
    lea  edx, [esp+4]
    push edx
    push ecx
    push eax
    mov  ecx, dword ptr [{SPRITES:#x}]
    call dword ptr [{imp['GetExtent']:#x}]
    pop  eax
    pop  edx
    ret
"""


ROUTINES = (('a', _asm_a), ('b', _asm_b), ('hud', _asm_hud), ('init', _asm_init))


def _clean(src):
    # keystone treats ';' as a statement separator: drop comments first
    return '\n'.join(l.split(';')[0].rstrip() for l in src.splitlines() if l.split(';')[0].strip())


def source_text(imp=None):
    imp = imp or {k: v[1] for k, v in IMPORTS.items()}
    data = ''.join(f'#data {va:#x} {s.hex()}\n' for va, s in STRINGS) + f'#data {PROBE_VA:#x} {PROBE_BLOB.hex()}\n'
    return data + '\n'.join(f'#{name}\n' + _clean(fn(imp)) for name, fn in ROUTINES)


def build(imp=None):
    """Assemble with keystone. Returns (blob, entries{name: (va, code length)}, cave_end)."""
    import keystone
    _knob_check()
    imp = imp or {k: v[1] for k, v in IMPORTS.items()}
    ks = keystone.Ks(keystone.KS_ARCH_X86, keystone.KS_MODE_32)
    blob = bytearray()
    for va, s in STRINGS + ((PROBE_VA, PROBE_BLOB),):
        assert va >= CAVE_VA + len(blob)
        blob += b'\0' * (va - CAVE_VA - len(blob)) + s
    blob += b'\0' * (CODE_VA - CAVE_VA - len(blob))
    entries = {}
    va = CODE_VA
    for name, fn in ROUTINES:
        enc, _ = ks.asm(_clean(fn(imp)), va)
        if not enc:
            raise SystemExit(f'combo hud: keystone produced no code for {name}')
        entries[name] = (va, len(enc))
        blob += bytes(enc)
        va += len(enc)
        pad = (-va) % 16
        blob += b'\xCC' * pad
        va += pad
    end = CAVE_VA + len(blob)
    assert end <= CAVE_LIMIT, 'cave does not fit in the .text tail'
    return bytes(blob), entries, end


# PREBUILT = the output of build() for the constants above (python combo_hud_2009.py --check
# re-assembles and compares). apply() uses it when keystone is not installed.
PREBUILT_SRC_SHA256 = '30527556410a23f1bd8d762bf3d5400a4966add4c35a5c21a4d8330d3b6d3ce6'
PREBUILT_ENTRIES = {'a': (0x4C62D0, 437), 'b': (0x4C6490, 88), 'hud': (0x4C64F0, 751), 'init': (0x4C67E0, 178)}
PREBUILT_HEX = (
    '436F6D626F2100002E2F68732F636F6D626F3030312E6873690000000000000000000000280000003400000001000000'
    '0A0000004C0000001A0000000100000020000000500000001C0000000200000021000000440000001C00000002000000'
    '220000008A0000002000000002000000230000002800000034000000040000002D0000004C0000001A00000004000000'
    '39000000280000003400000008000000430000004C0000001A0000000800000083FB017D05BB0100000060C605971655'
    '00008B7424483BAE880900000F858D0100008B7C244080BF9C000000040F857C01000083BE400F0000000F856F010000'
    '8B86B40F000085C0740A83787C010F845B01000081BC2488000000CD6B420074520FB68424980000000FB78D18010000'
    '83F809750C81F9020200000F842E01000083F806750C81F9040200000F841D01000083BC24A0000000000F8508010000'
    'BB0100000083BC249C00000000740931DBEB05BB01000000A19016550085C07505B891F4452589C2C1E20D31D089C2C1'
    'EA1131D089C2C1E20531D0A39016550031D2B9E8030000F7F131F6B9E803000083FA32721C81FAB4000000722081FA36'
    '0100007322BE02000000B9E2040000EB16BE03000000B9DC050000EB0ABE01000000B9EE020000A19016550089C2C1E2'
    '0D31D089C2C1EA1131D089C2C1E20531D0A3901655005131D2B9C9000000F7F15981C2840300000FAFCA8B442410F7E1'
    'B940420F00F7F185C07905B8FFFFFF7F83FE01740585C07501408944241085F674220FB6059616550039C6761789F0A2'
    '9616550083FE03750B8B8788000000A39C16550085DB7407C605971655000161E9DF40F5FFCCCCCCCCCCCCCCCCCCCCCC'
    '803D97165500007442C605971655000085DB74375051A134EF540089C12B0D8416550081F9D00700008B0D8016550072'
    '0231C941890D80165500A38416550005D0070000A388165500595839D30F84F137F5FFE9CA41F5FFCCCCCCCCCCCCCCCC'
    '600FB61D9616550085DB0F84B6000000C60596165500008B35C0F0540085F60F84A10000008B8E180F000083E90483F9'
    '020F878F000000F6059816550002741B8B0D8C1655008D412083FB0274218D412183FB0174198D4122EB14B84C010000'
    '83FB02740A83FB01745CB84E01000066A3941655006A006A0A6A016A006A006A00FFB6240200005068D0EB5400E80E68'
    'F6FF83FB03752FA19C165500C7059C1655000000000085C0741C6A006A0A6A016A006A006A0050684B01000068D0EB54'
    '00E8DA67F6FF8B1D8016550083FB020F8214020000A134EF54008B0D8816550029C185C90F8EFF01000081F9D0070000'
    '0F87F301000089C52B2D841655008B35C0F0540085F60F84DD0100008B8E180F000083E90483F9020F87CB01000083BE'
    '88090000000F84BE0100008B0DCCF0540085C9740A83797C010F84AA010000833DD0025500000F859D01000081FBE703'
    '00007605BBE7030000A180165500B90000000031D2BE5F000000BF00FF320083F8147214B923000000BA04000000BE06'
    '000000BF109AFF00A18C16550085C00F849000000085D2740C8515981655000F84800000008D3408BFAC02000089D831'
    'D2B90A000000F7F189C38D44160B68B00400006840060000680000008068000000806AFF5568B101000057508B0D44E8'
    '5400FF1560734C0083EF1F85DB75BE8D461568B00400006840060000680000008068000000806AFF5568BA01000068E7'
    '020000508B0D44E85400FF1560734C00E9C40000005789F5BFA102000089D831D2B90A000000F7F189C38D7415006A00'
    '68ABAAAA3F68ABAAAA3F568B0D44E85400FF1590734C0068B00400006840060000680000008068000000806AFF6A0068'
    '9F01000057568B0D44E85400FF1560734C006A00680000803F680000803F568B0D44E85400FF1590734C0083EF1B85DB'
    '758B588B0D40E8540085C9743C68B004000068400600006AFF6AFF6A006800000080680000008068000000FF6A090D00'
    '0000FF5068C201000068D40200006820624C00FF1564754C0061E961A0FCFFCC600F3183C801A3901655008B0D44E854'
    '0085C974756A016A006A006A006A006A006A006A006A006A006A006A006A016A006828624C00FF155C734C0085C07505'
    'B85601000089C6BB0F000000BF40624C008B0701F0E83C0000003B470475053B570874078B470CF7D021C383C71081FF'
    'D0624C0072DBF7C301000000740C891D9816550089358C165500618B86F4040000E9598EF7FF6A006A0089E18D542404'
    '5251508B0D44E85400FF1598734C00585AC3CCCCCCCCCCCCCCCCCCCCCCCCCCCC'
)


def _blob():
    src_sha = hashlib.sha256(source_text().encode()).hexdigest()
    try:
        blob, entries, end = build()
    except ImportError:
        if PREBUILT_HEX is None or src_sha != PREBUILT_SRC_SHA256:
            raise SystemExit('combo hud: keystone is not installed and the prebuilt cave does not '
                             'match the source constants: python -m pip install --user keystone-engine')
        _knob_check()
        blob = bytes.fromhex(PREBUILT_HEX)
        return blob, {k: tuple(v) for k, v in PREBUILT_ENTRIES.items()}, CAVE_VA + len(blob)
    if PREBUILT_HEX is not None and src_sha == PREBUILT_SRC_SHA256 and blob.hex().upper() != PREBUILT_HEX.upper():
        raise SystemExit('combo hud: keystone output differs from the prebuilt cave for the same source')
    return blob, entries, end


def hooks(entries):
    """[(va, orig, new, what)] for the four hook sites."""
    rel = lambda src, dst: struct.pack('<i', dst - (src + 5))
    return [
        (A_SITE, A_ORIG, b'\xE9' + rel(A_SITE, entries['a'][0]) + b'\x90' * 5, 'combo grade roll'),
        (B_SITE, B_ORIG, b'\xE9' + rel(B_SITE, entries['b'][0]) + b'\x90' * 3, 'combo count'),
        (HUD_SITE, HUD_ORIG, b'\xE8' + rel(HUD_SITE, entries['hud'][0]), 'combo HUD draw'),
        (INIT_SITE, INIT_ORIG, b'\xE9' + rel(INIT_SITE, entries['init'][0]) + b'\x90', 'combo art load'),
    ]


def _overlaps(a, alen, b, blen):
    return a < b + blen and b < a + alen


def apply(data, off, put, iat, pe):
    """Patch `data` (bytearray of the 2009 exe) in place. Conventions of patch_2009.py:
    off(va) -> file offset, put(va, orig, new, what) checks orig then writes, iat{name: slot va},
    pe = pefile object (only section headers are used; current sizes are re-read from data)."""
    for key, (name, want) in IMPORTS.items():
        got = iat.get(name)
        if got != want:
            raise SystemExit(f'combo hud: IAT slot of {key} is {got and hex(got)}, expected {want:#x} - not Build 14?')
    blob, entries, end = _blob()
    hk = hooks(entries)
    for va, ln in RESERVED:
        if _overlaps(CAVE_VA, len(blob), va, ln):
            raise SystemExit(f'combo hud: cave overlaps reserved range {va:#x}+{ln}')
        for hva, orig, _new, _w in hk:
            if _overlaps(hva, len(orig), va, ln):
                raise SystemExit(f'combo hud: hook {hva:#x} overlaps reserved range {va:#x}+{ln}')
    for i, (hva, orig, new, _w) in enumerate(hk):
        assert len(new) == len(orig)
        for hvb, origb, _n, _w2 in hk[i + 1:]:
            if _overlaps(hva, len(orig), hvb, len(origb)):
                raise SystemExit(f'combo hud: hooks {hva:#x} and {hvb:#x} overlap')
    secs = {s.Name.rstrip(b'\0'): s for s in pe.sections}
    text, dat = secs[b'.text'], secs[b'.data']
    if not (text.VirtualAddress + B <= CAVE_VA and end <= text.VirtualAddress + B + text.SizeOfRawData):
        raise SystemExit('combo hud: cave is outside the raw .text data')
    if not (dat.VirtualAddress + B + dat.SizeOfRawData <= STATE_VA
            and STATE_END <= dat.VirtualAddress + B + ((dat.Misc_VirtualSize + 0xFFF) & ~0xFFF)
            and dat.Misc_VirtualSize <= STATE_VA - B - dat.VirtualAddress):
        raise SystemExit('combo hud: the .data state block is not in the zero slack of .data')
    # verify everything before writing anything (put() re-checks the hooks)
    for va, orig, _new, what in hk:
        o = off(va)
        if bytes(data[o:o + len(orig)]) != orig:
            raise SystemExit(f'{what}: unexpected bytes at 0x{va:X}: {bytes(data[o:o + len(orig)]).hex()} '
                             f'(want {orig.hex()}) - not the pristine 2009 exe, or already patched?')
    # cave: every byte must still be zero (the same check put() does, without printing 3 KB of hex)
    co = off(CAVE_VA)
    if bytes(data[co:co + len(blob)]) != b'\0' * len(blob):
        raise SystemExit(f'combo cave: 0x{CAVE_VA:X}..0x{end - 1:X} is not all zero - already patched?')
    data[co:co + len(blob)] = blob
    print(f'  {"combo cave":22} 0x{CAVE_VA:X}  {len(blob)} zero bytes -> code/data (ends 0x{end:X}, '
          f'sha256 {hashlib.sha256(blob).hexdigest()[:16]})')
    for va, orig, new, what in hk:
        put(va, orig, new, what)
    # section VirtualSize fields: max() so the patch order does not matter
    for sec, need, what in ((text, end - B - text.VirtualAddress, '.text'),
                            (dat, DATA_VSIZE_MIN, '.data')):
        fo = sec.get_file_offset() + 8
        cur = struct.unpack_from('<I', data, fo)[0]
        new = max(cur, need)
        if new != cur:
            struct.pack_into('<I', data, fo, new)
        print(f'  {"combo " + what + " VSize":22} 0x{cur:X} -> 0x{new:X}')
    return entries, end


# ---------------------------------------------------------------- art install (explicit only)
# The art ships next to this module (combo_art\, built by build_combo_art.py; see its README.txt).
ART_SRC = os.path.join(HERE, 'combo_art')
# Copy order matters: the textures first, the .hsi LAST. A present combo001.hsi with a missing
# combo001_a8.hsc passes the init probe (GetExtent reads the .hsi, not the texture) and then draws
# the wrong texture; a missing .hsi just makes OpenFromFile return 0 -> built-in fallback.
ART_FILES = ('combo001_a8.hsc', 'combo001_a8.hsc1', 'combo001_a8.hsc2', 'combo001.hsi')
ART_HSI_FILE = 'combo001.hsi'
_HSI_KEY = (0xE9, 0xDE, 0xE0)  # CValidationCheck::Decode (ValidationCheck.dll 0x10001220)


def _hsi_images(raw):
    """Image paths listed at the top of a (SHA-1 checked) .hsi: ['./hs/combo001_a8.hsc', ...]."""
    body = raw[:-20]
    head = bytes((b + _HSI_KEY[i % 3]) & 0xFF for i, b in enumerate(body)).decode('latin1')
    lines = head.split('\r\n')
    if not lines[0].startswith('Number_of_images:'):
        raise SystemExit(f'{ART_HSI_FILE}: unexpected header {lines[0][:40]!r}')
    n = int(lines[0].split(':')[1])
    imgs = []
    for k in range(1, n + 1):
        idx, path = lines[k].split(None, 1)
        if int(idx) != k:
            raise SystemExit(f'{ART_HSI_FILE}: bad image line {lines[k]!r}')
        imgs.append(path.strip())
    return imgs


def install_assets(dst_dir, src_dir=ART_SRC):
    """Copy the four combo art files into dst_dir (the game's hs folder).
    Everything is checked before anything is written:
      - the sources exist, the .hsi passes the client's SHA-1 check, the three .hsc* are sound ZIPs;
      - every OTHER image the .hsi uses (./hs/fx003_a8.hsc) is present in dst_dir with its
        .hsc1/.hsc2 D2D fallbacks;
      - no destination exists with different content (never overwrites, never touches a shipped file).
    Then each file is written as '<name>.tmp' and os.replace()d into place, textures first and the
    .hsi last, so an interrupted install leaves no combo001.hsi and the client uses the fallback.
    Returns the destination paths in copy order."""
    import shutil
    if not os.path.isdir(dst_dir):
        raise SystemExit(f'{dst_dir} is not a directory')
    for n in ART_FILES:
        s = os.path.join(src_dir, n)
        if not os.path.isfile(s):
            raise SystemExit(f'missing art file {s}')
    raw = open(os.path.join(src_dir, ART_HSI_FILE), 'rb').read()
    if len(raw) <= 20 or hashlib.sha1(raw[:-20]).digest() != raw[-20:]:
        raise SystemExit(f'{ART_HSI_FILE} fails the client SHA-1 check (CValidationCheck::CheckValid)')
    for n in ART_FILES:
        if n != ART_HSI_FILE:
            try:
                with zipfile.ZipFile(os.path.join(src_dir, n)) as z:
                    if not z.namelist() or z.testzip() is not None:
                        raise zipfile.BadZipFile('empty or corrupt member')
            except zipfile.BadZipFile as e:
                raise SystemExit(f'{n}: bad zip ({e})')
    ours = {n.lower() for n in ART_FILES}
    for img in _hsi_images(raw):
        base = img.replace('\\', '/').rsplit('/', 1)[-1]
        if base.lower() in ours:
            continue
        for ext in ('', '1', '2'):
            p = os.path.join(dst_dir, base + ext)
            if not os.path.isfile(p):
                raise SystemExit(f'{ART_HSI_FILE} uses {img}{ext}, which is missing in {dst_dir}')
    for n in ART_FILES:
        s, d = os.path.join(src_dir, n), os.path.join(dst_dir, n)
        if os.path.exists(d) and open(d, 'rb').read() != open(s, 'rb').read():
            raise SystemExit(f'{d} exists and differs - remove it first')
    done = []
    for n in ART_FILES:
        s, d = os.path.join(src_dir, n), os.path.join(dst_dir, n)
        if not os.path.exists(d):
            tmp = d + '.tmp'
            try:
                shutil.copyfile(s, tmp)
                os.replace(tmp, d)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
        done.append(d)
    return done


def _check():
    import capstone
    blob, entries, end = build()
    src_sha = hashlib.sha256(source_text().encode()).hexdigest()
    print(f'cave 0x{CAVE_VA:X}..0x{end - 1:X} ({len(blob)} bytes)  ' +
          '  '.join(f'{k}=0x{v[0]:X}+{v[1]}' for k, v in entries.items()))
    print('source sha256', src_sha)
    if PREBUILT_HEX is None:
        print('PREBUILT not set')
    else:
        ok = (src_sha == PREBUILT_SRC_SHA256 and blob.hex().upper() == PREBUILT_HEX.upper()
              and {k: tuple(v) for k, v in PREBUILT_ENTRIES.items()} == entries)
        print('matches PREBUILT:', ok)
    for va, orig, new, what in hooks(entries):
        print(f'  {what:16} 0x{va:X}  {orig.hex().upper()} -> {new.hex().upper()}')
    for va, st in STRINGS:
        print(f'  string 0x{va:X} {st!r}')
    print(f'  probes 0x{PROBE_VA:X}..0x{PROBE_END - 1:X} ' +
          ' '.join(f'+{o}={w}x{h}/{f}' for o, w, h, f in ART_PROBES))
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    for name, (va, ln) in entries.items():
        o = va - CAVE_VA
        print(f'--- {name} 0x{va:X} ({ln} bytes)')
        n = 0
        for i in md.disasm(blob[o:o + ln], va):
            print(f'{i.address:08X}  {i.bytes.hex():<24} {i.mnemonic} {i.op_str}')
            n += i.size
        assert n == ln, f'{name}: capstone decoded {n} of {ln} bytes'
    return blob, entries, end


if __name__ == '__main__':
    if '--check' in sys.argv:
        _check()
    elif '--install-assets' in sys.argv:
        for p in install_assets(os.path.join(HERE, 'hs')):
            print('  ', p)
        print('installed. Remove all four files together to uninstall (combo001.hsi first).')
    else:
        print(__doc__)
