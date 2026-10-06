#!/usr/bin/env python3
"""
test_hitstun.py - hitstun.py, the per-kind hurt / stun / slide of a client-caught hit
======================================================================================
(re_tools/docs/HIT_STUN_PER_SWING_RE_2026-09-28.md; desync fix M1 per-swing hurt, M3)

Pure functions only, no server:
- the three values live session 2 measured on the attacker's copy of a Monkey Soldier come
  out of the attacker's own C2S 0x0D words: basic swing 490 (610 - 120), dash attack 760
  (2140 - 1380), Ice Spear 1020 (1290 - 270; stun 2020 with the ice element);
- E = E0 + the logic ms after the action's start packet up to the report - 30; L is the
  stage's, the combo goes on only while the attack key is down inside the stage's window (E
  420..600 / 1080..1230, 0x41588x..0x41592D) - a press let go before it ends the stage at
  its L - a press after it ended starts a new swing, and so does the key held past it;
- the dash attack only right after motion-6 words and <= 580 ms into the dash; bow / staff
  single shots; the dash strong attack's E0;
- FUN_00422710 state-1 lengths, FUN_0044f070 variants, FUN_004194f0 elements (the report's
  section 5 ice / wind rows against both builds' hii when the client data is there);
- stun: hurt - 60 (tae 7/8), hurt + 1000 ice (9/10), >= 600 wind; slide 2 / 4 / 19 x 1.0 or
  2.5 ticks, 0 airborne;
- the fallback (MOB_HIT_RELAY_HURT_MS) for an unknown kind, a stale action or per-swing off,
  and the guesses that err high: event 9 the state-1 default (1020 for a Warrior), a held
  key past an end the tracker does not know the stage-2 default 760; a cast / strong attack
  just past its L is hurt L (the client's clamp);
- state 1: the attack key held or mashed through a cast or a strong attack sends motion-1 /
  5 words the client ignores - they start nothing while E < L; a fresh cast wins over a
  combo anchor for event 9; the Vampiric Attack relay flag is 0;
- live session 2b (A's own words from the live log, LIVE_2B: k2, k3, k4, i4, c1, f1 at 77 /
  84 / 85.19 / 103): the attacker's own hurt (action nibble 6..10, hi its ms) ends his
  action; the input held through it starts at its end (hurt_len: 270 for contact 250, 750
  for a swing 810) at stage 0 / E 0, E by the timeline from there (the default only below
  0; f1 10.34: E 150, 460 on A's copy, the old cap gave 490) - 490 / 500 / 760 / 1020 on A's
  copy where 2e2f05e said 280 / 290 / 250 / 410 / 700 / 780 or 760; a report in the hurt's
  own packet is still the old action; the dash end (state 7) holds an attack the same way,
  and a dash attack waits for the wind-up; an airborne hurt (8 / 10, state 0x17) turns him
  and starts no ground action;
- his facing +0x949 from his words (report_facing): a key in a hurt, a dash or a swing, and
  the report's own key, turn nothing (the held key and the tail's side knocked k2, k3, k4,
  c1 and f1 the wrong way); a dash attack, a dash strong attack or a cast out of the dash
  starts in state 6, so its first tick turns nothing either (the arrow reversed on the
  attack word still knocks the dash's way);
- live session 2c (LIVE_2C: t2, t3, t5, f1 at 65.79 / 66.42): an action ends ON the tick
  whose E, after that tick's += 30, reaches its L (_end_tick: E 630 for stage 0, 2160 for a
  dash attack, L for a cast), and the attack key down on the next tick starts a stage-0
  swing there - a tap on E 630 of stage 0 (600 the tick before, inside the window only by
  the old test) and a key held through a dash attack's end: 490 / 500 on A's copy where
  fe746fa said 470 (stage 1 read from E 780) and the 'held' 760; L is the stage the tracker
  reached (FUN_00422710 reads +0x979), E past it a stale anchor; live session 3 (LIVE_3,
  3d_free1 d@63): a press on the tick a dash attack ends, 500 where fe746fa said 400. A
  replay of every hit report of sessions 2b / 2c / 3 (A's and B's own C2S streams from the
  live log against their copies' +0x9E4 / +0x95B) matches 278 / 281; the three left are B's
  air attacks (state 0xE, not modelled: taken for a ground swing, 30 ms low).
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import config as cfgmod  # noqa: E402
import en_content as EC  # noqa: E402
import hitstun as H  # noqa: E402

B8, B9 = cfgmod.BUILD_2008, cfgmod.BUILD_2009
DIRS = {B8: cfgmod.DEFAULTS['CLIENT_DIR'], B9: cfgmod.DEFAULTS['CLIENT_DIR_2009']}
HAVE = {b: os.path.exists(os.path.join(HERE, d, 'hs', 'windslayer.hii')) for b, d in DIRS.items()}

# C2S 0x0D words (live session 2): the swing press (motion 1, +0x8D0 bit 21), idle, a dash
# right (motion 6 + right), the dash attack (motion 1 + right), the strong attack (motion 5),
# Ice Spear's cast words (motion 7, variant 1).
SWING, IDLE, DASH_R, DASH_ATTACK_R, STRONG = 0x200004, 0x200000, 0x1A, 0x06, 0x200014
CAST_ICE = 0x20003C
REPORT_7, REPORT_9 = 7 << 16, 9 << 16
ATTR = {0x104: 2, 0x10F: 1, 0x13A: 4, 0xA73: 4}.get     # Ice Spear, Fire Beat, Wind Cutter, Nova


def run(words, wt=1):
    """A tracker fed (lo, logic_elapsed_ms) packets in order."""
    t = H.new_tracker()
    for lo, ms in words:
        H.observe(t, lo, ms, weapon=wt)
    return t


def held_tracker(wt=1):
    """A tracker whose first word is the attack key's re-send (it began with the key down: a
    map change's fresh tracker): the 'held' anchor, no action whose end it knows."""
    t = H.new_tracker()
    t['lo'] = SWING
    H.observe(t, SWING, 210, weapon=wt)
    return t


class LiveValues(unittest.TestCase):
    def test_a_basic_swing_from_standing_is_490(self):
        """c01: the swing words, idle 60 ms later, the report 90 ms after that: E = 120."""
        t = run([(SWING, 400), (IDLE, 60), (REPORT_7, 90)])
        h = H.classify(t, 7, 7, cls=1, weapon=1, attribute_of=ATTR)
        self.assertEqual((h.kind, h.L, h.E, h.hurt, h.stun, h.ticks, h.variant, h.element),
                         ('swing', 610, 120, 490, 430, 2.0, 0, 0))
        self.assertEqual(h.why, 'swing: L 610 - E 120 (weapon 1)')

    def test_a_dash_attack_is_760(self):
        """d-series: the dash words, the attack 120 ms into the dash (state 6 -> 4 with +0xE9C
        = 0x564, stage 2), the report one tick later: E = 1380."""
        t = run([(DASH_R, 500), (DASH_ATTACK_R, 120), (REPORT_7, 30)])
        h = H.classify(t, 7, 7, cls=1, weapon=1, attribute_of=ATTR)
        self.assertEqual((h.kind, h.L, h.E, h.hurt, h.stun, h.ticks), ('dash', 2140, 1380, 760, 700, 2.0))
        self.assertEqual(h.why, 'dash attack: L 2140 - E 1380 (weapon 1)')

    def test_ice_spear_is_1020_and_2020_in_state_3(self):
        """s02: the cast words (variant 1), idle 60 ms later, the report 240 ms after that:
        E = 270; hurt 1290 - 270; the ice element adds 1000 ms; the relay flag is 1."""
        t = run([(CAST_ICE, 40), (IDLE, 60), (REPORT_9, 240)])
        h = H.classify(t, 9, 9, cls=1, weapon=1, cast_skill=260, attribute_of=ATTR)
        self.assertEqual((h.kind, h.L, h.E, h.hurt, h.stun, h.ticks, h.variant, h.element),
                         ('skill', 1290, 270, 1020, 2020, 4.0, 1, H.ELEMENT_ICE))
        self.assertEqual(h.why, 'skill v1: L 1290 - E 270 (weapon 1)')


class Tracker(unittest.TestCase):
    def test_e_counts_the_packets_after_the_start_minus_one_tick(self):
        t = run([(SWING, 5000), (IDLE, 30), (IDLE, 210), (REPORT_7, 30)])
        self.assertEqual(H.anchor_e(t['anchor']), 30 + 210 + 30 - 30)
        self.assertEqual(H.classify(t, 7, 7, weapon=1).hurt, 610 - 240)

    def test_the_combo_stage_comes_from_e(self):
        """Held key: the builder re-sends every 210 ms; the combo goes on through its stages
        (windows 0..610, 640..1250, 1280..2140)."""
        words = [(SWING, 400)] + [(SWING, 210)] * 3                          # E 180, 390, 600
        h = H.classify(run(words + [(REPORT_7, 150)]), 7, 7, weapon=1)       # E 750: stage 1
        self.assertEqual((h.L, h.E, h.hurt), (1250, 750, 500))
        h = H.classify(run(words + [(SWING, 210)] * 3 + [(REPORT_7, 150)]), 7, 7, weapon=1)
        self.assertEqual((h.L, h.E, h.hurt), (2140, 1380, 760))              # stage 2

    def test_a_press_inside_the_window_carries_the_combo_on(self):
        """State 4 goes on only on a tick the attack key is down at E 420..600 (1080..1230 for
        stage 1; 0x41588x..0x41592D): a press at E 450 does, a key held from E 300 into the
        window does, a press at E 300 let go at 390 does not (stage 0 then ends at 610)."""
        t = run([(SWING, 400), (IDLE, 60), (SWING, 390)])                    # E 450 on its tick
        self.assertEqual((t['anchor']['kind'], t['anchor']['stage']), ('swing', 1))
        H.observe(t, IDLE, 90, weapon=1)
        H.observe(t, REPORT_7, 270, weapon=1)                                 # E 780
        self.assertEqual(H.classify(t, 7, 7, weapon=1).hurt, 1250 - 780)
        held = run([(SWING, 400), (IDLE, 60), (SWING, 240), (SWING, 210)])   # down from E 300 to 510
        self.assertEqual(held['anchor']['stage'], 1)
        early = run([(SWING, 400), (IDLE, 60), (SWING, 240), (IDLE, 90)])    # down at E 300..390
        self.assertEqual((early['anchor']['kind'], early['anchor']['stage']), ('swing', 0))
        self.assertEqual(H._action_end(early['anchor']), 610)
        # stage 1 -> 2 the same way: pressed at E 1110 (its window), not at E 900
        t = run([(SWING, 400), (IDLE, 60), (SWING, 390), (IDLE, 90), (SWING, 660)])
        self.assertEqual(t['anchor']['stage'], 2)
        t = run([(SWING, 400), (IDLE, 60), (SWING, 390), (IDLE, 90), (SWING, 450), (IDLE, 60)])
        self.assertEqual((t['anchor']['stage'], H._action_end(t['anchor'])), (1, 1250))

    def test_a_press_after_the_swing_ended_starts_a_new_one(self):
        t = run([(SWING, 400), (IDLE, 60), (SWING, 700), (IDLE, 60), (REPORT_7, 90)])
        h = H.classify(t, 7, 7, weapon=1)
        self.assertEqual((h.kind, h.E, h.hurt), ('swing', 120, 490))

    def test_a_held_key_past_the_combo_starts_stage_0_again(self):
        """Held through all three stages (410 < E < 610 on 420, 1050 < E < 1250 on 1080), the
        combo ends on the tick E reaches 2160 (t 2560) and the key down on the next one starts
        a stage-0 swing there (t 2590; was the 'held' anchor, E unknown, 760)."""
        words = [(SWING, 400)] + [(SWING, 210)] * 11                         # t 2710: E 2310
        t = run(words)
        self.assertEqual((t['anchor']['kind'], t['anchor']['t0'], t['anchor']['stage']), ('swing', 2590, 0))
        H.observe(t, REPORT_7 | SWING, 30, weapon=1)
        h = H.classify(t, 7, 7, weapon=1)
        self.assertEqual((h.kind, h.L, h.E, h.hurt), ('swing', 610, 120, 490))
        self.assertEqual(h.why, 'swing: L 610 - E 120 (weapon 1), started at the end of the swing (stage 2)')

    def test_a_held_key_past_an_end_not_known_is_the_stage_2_default(self):
        """E unknown (the key already down when the tracker began, e.g. a map change's fresh
        tracker, or out of an airborne hurt): the largest stage default, 760 (gate 0.82 s) -
        not 490, which let the chase in ~270 ms early on a stage-2 hit."""
        t = held_tracker()
        H.observe(t, REPORT_7, 90, weapon=1)
        self.assertEqual(t['anchor']['kind'], 'held')
        h = H.classify(t, 7, 7, weapon=1, fallback_ms=490)
        self.assertEqual((h.kind, h.hurt, h.stun, h.L, h.E), ('fallback', 760, 700, None, None))
        self.assertEqual(h.why, 'a held attack key past its action (E unknown): the stage-2 default 760')
        h = H.classify(t, 7, 7, weapon=1, fallback_ms=900)                   # never below the key
        self.assertEqual((h.hurt, h.why), (900, 'a held attack key past its action (E unknown): '
                                                 'MOB_HIT_RELAY_HURT_MS 900'))

    def test_a_stale_swing_is_the_fallback(self):
        t = run([(SWING, 400), (IDLE, 60), (IDLE, 3000), (REPORT_7, 90)])
        h = H.classify(t, 7, 7, weapon=1, fallback_ms=333)
        self.assertEqual((h.kind, h.hurt), ('fallback', 333))
        self.assertEqual(h.why, 'swing words 3150 ms old: E 3120 is past the action (L 610); '
                                'MOB_HIT_RELAY_HURT_MS 333')

    def test_no_words_at_all_is_the_fallback(self):
        for track in (None, H.new_tracker()):
            h = H.classify(track, 7, 7, weapon=1, fallback_ms=490)
            self.assertEqual((h.kind, h.hurt, h.tae, h.ticks), ('fallback', 490, 7, 2.0))
            self.assertEqual(h.why, 'no swing words counted (last action: none): MOB_HIT_RELAY_HURT_MS 490')

    def test_per_swing_off_is_the_flat_value(self):
        t = run([(SWING, 400), (IDLE, 60), (REPORT_7, 90)])
        h = H.classify(t, 7, 7, weapon=1, fallback_ms=360, per_swing=False)
        self.assertEqual((h.kind, h.hurt, h.stun), ('fallback', 360, 300))
        self.assertEqual(h.why, 'MOB_HIT_HURT_PER_SWING off: MOB_HIT_RELAY_HURT_MS 360')

    def test_the_dash_window(self):
        """Motion 1 right after motion-6 words is the dash attack only <= 580 ms into the
        dash. Later, or after the dash words stopped, he is in the dash end (state 7, 150 ms
        from the release or the 580 ms mark): the attack starts nothing there, and the key
        still held starts a swing from standing one tick after it (a virtual anchor)."""
        late = run([(DASH_R, 500), (DASH_R, 300), (DASH_ATTACK_R, 300)])     # state 7 from 1080
        self.assertEqual((late['anchor'], late['busy']['why'], late['busy']['until']), (None, 'dash end', 1260))
        self.assertEqual(late['busy']['pending']['kind'], 'swing')
        H.observe(late, DASH_ATTACK_R, 210, weapon=1)                          # t 1310: out at 1260
        self.assertEqual((late['anchor']['kind'], late['anchor']['virtual'], late['anchor']['t0']),
                         ('swing', True, 1260))
        H.observe(late, REPORT_7 | DASH_ATTACK_R, 90, weapon=1)
        h = H.classify(late, 7, 7, weapon=1)
        self.assertEqual((h.kind, h.E, h.hurt), ('swing', 110, 500))
        self.assertEqual(h.why, 'swing: L 610 - E 110 (weapon 1), started at the end of the dash')
        # released, then pressed inside state 7, reported before its end by the tracker: the
        # swing began earlier on his client - E at most the time since the press
        idle_between = run([(DASH_R, 500), (IDLE, 30), (DASH_ATTACK_R, 30), (REPORT_7 | DASH_ATTACK_R, 120)])
        self.assertEqual((idle_between['anchor'], idle_between['busy']['until']), (None, 710))
        h = H.classify(idle_between, 7, 7, weapon=1)
        self.assertEqual((h.kind, h.E, h.hurt), ('swing', 90, 520))
        self.assertEqual(h.why, 'swing: L 610 - E 90 (weapon 1), the dash not over by his words yet: the '
                                'action his held input starts')
        # reported on the very tick the tracker lets him out (710): caught a tick before it
        h = H.classify(run([(DASH_R, 500), (IDLE, 30), (DASH_ATTACK_R, 30), (REPORT_7 | DASH_ATTACK_R, 150)]),
                       7, 7, weapon=1)
        self.assertEqual((h.E, h.hurt), (120, 490))
        self.assertTrue(h.why.endswith('started at the end of the dash (E -30 by his words, taken as the '
                                       'default 120)'), h.why)
        edge = run([(DASH_R, 500), (DASH_R, 290), (DASH_ATTACK_R, 290), (REPORT_7, 30)])
        self.assertEqual(edge['anchor']['kind'], 'dash')

    def test_the_dash_attack_waits_for_the_wind_up(self):
        """Motion case 1 / 5 acts in state 6, and the wind-up (state 5) moves on to it on the
        dash's third tick: an attack word 30 ms into the dash starts the dash attack at the
        dash + 90 ms (live 2b f1 84.05: E 1380 on A's copy, 760; the word's tick gave 1440)."""
        t = run([(DASH_R, 500), (DASH_ATTACK_R, 30)])
        self.assertEqual((t['anchor']['kind'], t['anchor']['t0'], t['anchor']['ms']), ('dash', 590, -60))
        H.observe(t, REPORT_7 | DASH_ATTACK_R, 90, weapon=1)                  # caught on its first tick
        self.assertEqual(H.classify(t, 7, 7, weapon=1).hurt, 760)
        t = run([(DASH_R, 500), (0x16, 30), (REPORT_9, 180)])                  # the dash strong attack
        h = H.classify(t, 9, 9, weapon=1)
        self.assertEqual((h.kind, h.E), ('strong', 150 + 120 - 30))

    def test_bow_and_staff_are_single_shots(self):
        for wt, length, e0 in ((3, 720, 180), (5, 740, 200), (6, 740, 200)):
            with self.subTest(wt=wt):
                t = run([(SWING, 400), (IDLE, 60), (REPORT_7, 90)], wt=wt)
                self.assertEqual(H.classify(t, 7, 7, weapon=wt).hurt, length - 120)
                t = run([(DASH_R, 500), (DASH_ATTACK_R, 120), (REPORT_7, 30)], wt=wt)
                self.assertEqual(H.classify(t, 7, 7, weapon=wt).hurt, length - e0)
                # a press on E 750: the bow's 720 ended on E 720, a new shot from the press's
                # tick; the staff's 740 ends on this very tick (E 750), and the key still down
                # 30 ms later starts the next shot there (_end_tick)
                t = run([(SWING, 400), (IDLE, 60), (SWING, 690), (REPORT_7, 90)], wt=wt)
                self.assertEqual(H.anchor_e(t['anchor']), 60 if wt == 3 else 30)
                t = run([(SWING, 400), (IDLE, 60), (SWING, 800), (REPORT_7, 90)], wt=wt)
                self.assertEqual(H.anchor_e(t['anchor']), 60)                # a new shot at E 830

    def test_the_strong_attack(self):
        """Motion 5: state 1 with variant 0 (no element, flag 0); from the dash its E0."""
        t = run([(STRONG, 400), (IDLE, 60), (REPORT_9, 240)])
        h = H.classify(t, 9, 9, cls=1, weapon=1, attribute_of=ATTR)
        self.assertEqual((h.kind, h.L, h.E, h.hurt, h.stun, h.ticks, h.variant, h.element),
                         ('strong', 1290, 270, 1020, 1020, 4.0, 0, 0))
        t = run([(DASH_R, 500), (0x16, 120), (REPORT_9, 30)])                # strong + right
        h = H.classify(t, 9, 9, weapon=1)
        self.assertEqual((h.kind, h.E, h.hurt), ('strong', 150, 1140))
        t = run([(STRONG, 400), (IDLE, 60), (REPORT_9, 240)], wt=5)
        self.assertEqual(H.classify(t, 9, 9, weapon=5).L, 1490)

    def test_a_skill_with_no_cast_words_takes_the_default_e(self):
        h = H.classify(run([(IDLE, 60), (REPORT_9, 240)]), 9, 9, cls=1, weapon=1, cast_skill=260,
                       attribute_of=ATTR)
        self.assertEqual((h.kind, h.E, h.hurt, h.variant, h.stun), ('skill', 270, 1020, 1, 2020))
        self.assertEqual(h.why, 'no cast words counted: the default E; skill v1: L 1290 - E 270 (weapon 1)')
        # a stale cast and no fresh one: the event-9 fallback, variant 0 (no longer inside its
        # L): the state-1 default L - 270, never below MOB_HIT_RELAY_HURT_MS
        t = run([(CAST_ICE, 40), (IDLE, 60), (IDLE, 2000), (REPORT_9, 240)])
        h = H.classify(t, 9, 9, cls=1, weapon=1, fallback_ms=490, attribute_of=ATTR)
        self.assertEqual((h.kind, h.hurt, h.variant, h.flag, h.stun), ('fallback', 1020, 0, 0, 1020))
        self.assertEqual(h.why, 'skill words 2300 ms old: E 2270 is past the action (L 1290); '
                                'the state-1 default L 1290 - E 270 (weapon 1)')

    def test_a_new_variant_is_a_new_cast(self):
        t = run([(CAST_ICE, 40), (IDLE, 60), (0x20005C, 300), (REPORT_9, 240)])   # variant 2
        self.assertEqual((t['anchor']['kind'], t['anchor']['variant'], t['anchor']['ms']), ('cast', 2, 240))
        h = H.classify(t, 9, 9, cls=1, weapon=1, attribute_of=ATTR)
        self.assertEqual((h.variant, h.element, h.hurt), (2, H.ELEMENT_FIRE, 1290 - 210))

    def test_a_swing_promoted_to_event_9(self):
        t = run([(SWING, 400), (IDLE, 60), (REPORT_9, 90)])
        h = H.classify(t, 9, 9, weapon=1)
        self.assertEqual((h.kind, h.hurt, h.stun, h.ticks, h.variant), ('swing (event 9)', 490, 490, 4.0, 0))

    def test_the_event_9_fallback_is_the_state_1_default(self):
        """No cast / strong words and no cast: every event-9 hit on the attacker's copy runs
        a state-1 length (report 1.4 default L - 270), never below MOB_HIT_RELAY_HURT_MS."""
        for wt, hurt in ((0, 1020), (1, 1020), (2, 970), (3, 1120), (4, 970), (5, 1220), (6, 1220)):
            with self.subTest(wt=wt):
                h = H.classify(None, 9, 9, weapon=wt, fallback_ms=490)
                self.assertEqual((h.kind, h.hurt, h.stun, h.variant, h.flag), ('fallback', hurt, hurt, 0, 0))
        h = H.classify(H.new_tracker(), 9, 9, weapon=1, fallback_ms=490)
        self.assertEqual(h.why, 'no cast / strong attack words counted (last action: none): '
                                'the state-1 default L 1290 - E 270 (weapon 1)')
        h = H.classify(None, 9, 9, weapon=1, fallback_ms=1500)
        self.assertEqual((h.hurt, h.why), (1500, 'no cast / strong attack words counted (last action: '
                                                 'none): MOB_HIT_RELAY_HURT_MS 1500'))
        # a held key past an end not known, event 9: the state-1 default wins over the stage-2 one
        t = held_tracker()
        H.observe(t, REPORT_9, 90, weapon=1)
        self.assertEqual(H.classify(t, 9, 9, weapon=1).hurt, 1020)

    def test_just_past_l_a_state_1_action_is_hurt_l(self):
        """The client clamps E > L to L (0x412F34): a cast / strong attack E past its L by
        less than STATE1_E_SLACK_MS is still that action, hurt L; from L + 300 it is stale."""
        t = run([(CAST_ICE, 40), (IDLE, 60), (IDLE, 1200), (REPORT_9, 90)])      # E 1320
        h = H.classify(t, 9, 9, cls=1, weapon=1, attribute_of=ATTR)
        self.assertEqual((h.kind, h.L, h.E, h.hurt, h.stun, h.variant, h.flag),
                         ('skill', 1290, 1320, 1290, 2290, 1, 1))
        self.assertEqual(h.why, 'skill v1: E 1320 past L 1290 by < 300: hurt L (weapon 1)')
        t = run([(STRONG, 400), (IDLE, 60), (IDLE, 1500), (REPORT_9, 29)])      # E 1559
        h = H.classify(t, 9, 9, weapon=1)
        self.assertEqual((h.kind, h.E, h.hurt), ('strong', 1559, 1290))
        t = run([(STRONG, 400), (IDLE, 60), (IDLE, 1500), (REPORT_9, 60)])      # E 1590 = L + 300
        self.assertEqual(H.classify(t, 9, 9, weapon=1).kind, 'fallback')

    def test_the_tail_tae(self):
        t = run([(SWING, 400), (IDLE, 60), (REPORT_7, 90)])
        air = H.classify(t, 7, 8, weapon=1)
        self.assertEqual((air.tae, air.stun, air.ticks, air.airborne), (8, 430, 0.0, True))
        self.assertEqual(H.classify(t, 7, 9, weapon=1).tae, 7)                # not a swing's: 7
        self.assertEqual(H.classify(t, 7, 0, weapon=1).tae, 7)
        self.assertEqual(H.classify(t, 9, 10, weapon=1).tae, 10)
        self.assertEqual(H.classify(t, 9, 7, weapon=1).tae, 9)


# Live session 2b (livefix 2e2f05e, 2026-09-28): A's own C2S 0x0D words, decoded from the
# live server log - (lo, logic_elapsed_ms, hi[, victim uid & 0xFFFF, target_dx, tae]) - and
# ('cast', id) for his C2S 0x15, each with its time in the capture. `facing`: A's +0x949 at
# the window's start (the capture's sample). `want`: A's copy of the mob after each report
# (+0x9E4, +0x95B), `old`: what 2e2f05e relayed (hi, knock) - the failures of the session.
LIVE_2B = {
    'k2': dict(facing=2, want=[(490, 2), (500, 2), (490, 2)], old=[(280, 2), (290, 2), (760, 6)], words=[
        (0x00200000, 210, 810),                  # -0.217
        (0x00200000, 210, 810),                  # -0.006
        (0x00206000, 360, 250),                  #  0.353 ae 6: the contact, 250 ms
        (0x00200004, 60, 250),                   #  0.413 the key pressed in the hurt
        (0x00200004, 210, 250),                  #  0.624 held: out of state 3 at 0.353 + 270
        (0x00270004, 150, 250, 1, -60.0, 7),     #  0.773 E 120
        (0x00200004, 210, 250),                  #  0.984
        (0x00200004, 210, 250),                  #  1.194
        (0x00276004, 210, 250, 1, -60.0, 7),     #  1.404 E 750, and hurt again (ae 6)
        (0x00200004, 210, 250),                  #  1.614
        (0x00270004, 210, 250, 0, 26.25, 7),     #  1.823 stage 0 again; the mob BEHIND him
    ]),
    'k3': dict(facing=2, want=[(760, 6), (490, 6), (490, 6)], old=[(760, 2), (760, 6), (760, 2)], words=[
        (0x00100000, 210, 250),                  # -0.403
        (0x00106000, 180, 250),                  # -0.223 ae 6
        (0x00100000, 210, 250),                  # -0.014
        (0x00100000, 210, 250),                  #  0.197
        (0x00106000, 120, 250),                  #  0.317 ae 6
        (0x00100002, 90, 250),                   #  0.408 right, in the hurt: no turn
        (0x00100000, 90, 250),                   #  0.498
        (0x0010001a, 120, 250),                  #  0.617 dash right: turns him
        (0x00100004, 210, 250),                  #  0.828 the dash attack
        (0x00170004, 30, 250, 0, -11.25, 7),     #  0.858 the mob overlaps him (dx -11)
        (0x00100004, 210, 250),                  #  1.066
        (0x00100004, 210, 250),                  #  1.276
        (0x00100004, 210, 250),                  #  1.487
        (0x00106004, 90, 250),                   #  1.578 ae 6: the held combo ends
        (0x00100004, 210, 250),                  #  1.788
        (0x00170004, 210, 250, 1, -1.0, 7),      #  1.997
        (0x00206004, 120, 250),                  #  2.118 ae 6
        (0x00200004, 210, 250),                  #  2.328
        (0x00270004, 210, 250, 1, -23.5, 7),     #  2.538 behind him
    ]),
    'k4': dict(facing=6, want=[(490, 2)], old=[(250, 6)], words=[
        (0x00200000, 120, 250),                  #  0.526
        (0x00200019, 120, 250),                  #  0.646 dash left
        (0x00200019, 210, 250),                  #  0.856
        (0x00200002, 60, 250),                   #  0.917 right tapped as the dash ends: no turn
        (0x00200000, 30, 250),                   #  0.946
        (0x00200004, 30, 250),                   #  0.976 the attack in state 7
        (0x00200004, 210, 250),                  #  1.187
        (0x00200004, 210, 250),                  #  1.397
        (0x00200004, 210, 250),                  #  1.606
        (0x00200004, 210, 250),                  #  1.817
        (0x00107004, 180, 810),                  #  1.996 ae 7: the monkey's swing, 810 ms
        (0x00100004, 210, 810),                  #  2.205
        (0x00100004, 210, 810),                  #  2.415
        (0x00100004, 210, 810),                  #  2.627
        (0x00100004, 210, 810),                  #  2.836 out at 1.996 + 750
        (0x00170004, 60, 810, 1, 50.75, 7),      #  2.897 E 120 (was 1890)
    ]),
    'i4': dict(facing=2, want=[(1020, 6)], old=[(780, 6)], words=[
        (0x00200001, 210, 250),                  #  1.829 walking left
        (0x00200000, 30, 250),                   #  1.860
        (0x00200000, 210, 250),                  #  2.070
        (0x00200002, 90, 250),                   #  2.160 right tapped standing: turns him
        (0x00200000, 30, 250),                   #  2.190
        ('cast', 260),                           #  2.205 C2S 0x15 Ice Spear
        (0x00106000, 30, 250),                   #  2.221 ae 6
        (0x0010003c, 30, 250),                   #  2.250 the cast words, in the hurt
        (0x0010003c, 210, 250),                  #  2.460
        (0x00100000, 90, 250),                   #  2.549 the cast began at 2.221 + 270
        (0x00100000, 210, 250),                  #  2.760
        (0x00196000, 30, 250, 0, 56.25, 9),      #  2.790 E 270 (was 510)
    ]),
    'c1': dict(facing=2, want=[(490, 2)], old=[(410, 6)], words=[
        (0x00100000, 210, 250),                  #  0.054
        (0x00106000, 120, 250),                  #  0.384 ae 6
        (0x00100002, 30, 250),                   #  0.414 right, in the hurt: no turn
        (0x00100000, 30, 250),                   #  0.444
        (0x00100004, 30, 250),                   #  0.475
        (0x00100000, 60, 250),                   #  0.534 released in the hurt: nothing
        (0x00100000, 210, 250),                  #  0.743
        (0x00100004, 90, 250),                   #  0.833 a swing
        (0x00100000, 60, 250),                   #  0.893
        (0x00106000, 30, 250),                   #  0.924 ae 6: it ends
        (0x00100000, 210, 250),                  #  1.135
        (0x00100004, 60, 250),                   #  1.194 a new press after the hurt
        (0x00100000, 30, 250),                   #  1.225
        (0x00170000, 120, 250, 0, 55.25, 7),     #  1.344 E 120 (was 840); the mob behind him
    ]),
    'f1@84': dict(facing=6, want=[(760, 6), (490, 6)], old=[(700, 6), (760, 2)], words=[
        (0x00100002, 210, 250),                  # 83.238
        (0x00100000, 60, 250),                   # 83.298
        (0x0010001a, 120, 250),                  # 83.418 dash right
        (0x0010001a, 210, 250),                  # 83.628
        (0x0010601a, 30, 250),                   # 83.659 ae 6 in the dash
        (0x0010001a, 210, 250),                  # 83.868 the dash key held: it dashes again at 83.929
        (0x00100004, 90, 250),                   # 83.958 30 ms into it: out of the wind-up at +90
        (0x00170004, 90, 250, 1, 20.5, 7),       # 84.048 E 1380 (was 1440)
        (0x00100004, 210, 250),                  # 84.257
        (0x00100004, 210, 250),                  # 84.468
        (0x00100004, 210, 250),                  # 84.677
        (0x00106004, 90, 250),                   # 84.768 ae 6
        (0x00100004, 210, 250),                  # 84.978
        (0x00170004, 210, 250, 1, -9.5, 7),      # 85.188 E 120 (was 'held', 760); the mob behind
    ]),
    'f1@77': dict(facing=6, want=[(760, 6), (490, 6)], old=[(760, 6), (340, 6)], words=[
        (0x0020001a, 90, 810),                   # 76.158 dash right
        (0x00200004, 90, 810),                   # 76.248
        (0x00270004, 30, 810, 1, 4.0, 7),        # 76.277 the dash attack
        (0x00200000, 30, 810),                   # 76.308
        (0x00200004, 90, 810),                   # 76.398
        (0x00200000, 60, 810),                   # 76.458
        (0x00200000, 210, 810),                  # 76.668
        (0x00200000, 210, 810),                  # 76.878
        (0x00106000, 120, 250),                  # 76.999 ae 6
        (0x00100001, 60, 250),                   # 77.058 left, in the hurt: no turn (held said left)
        (0x00100000, 30, 250),                   # 77.089
        (0x00100004, 30, 250),                   # 77.118
        (0x00100004, 210, 250),                  # 77.327
        (0x00170004, 90, 250, 1, 11.5, 7),       # 77.418
    ]),
    'f1@9': dict(facing=2, want=[(490, 2), (500, 2), (460, 2)], old=[(490, 2), (500, 2), (50, 2)], words=[
        (0x00206000, 120, 250),                  #   8.837 ae 6
        (0x00200000, 210, 250),                  #   9.047
        (0x00200004, 60, 250),                   #   9.108
        (0x00270004, 150, 250, 0, -45.75, 7),    #   9.258 E 120
        (0x00200004, 210, 250),                  #   9.468
        (0x00200004, 210, 250),                  #   9.678
        (0x00276004, 210, 250, 0, -53.25, 7),    #   9.888 E 750, and hurt again (ae 6)
        (0x00200004, 210, 250),                  #  10.097
        (0x00200004, 210, 250),                  #  10.308 out at 9.888 + 270
        (0x00270004, 30, 250, 0, -90.75, 7),     #  10.340 E 150 by the timeline: 460 (the
    ]),                                          #         default 120 cap gave 490)
    'f1@103': dict(facing=2, want=[(490, 2)], old=[(250, 2)], words=[
        (0x00100004, 210, 250),                  # 102.738
        (0x00206004, 30, 250),                   # 102.768 ae 6
        (0x00200004, 210, 250),                  # 102.978
        (0x00270002, 210, 250, 1, -8.0, 7),      # 103.188 the report's own key (right) turns
    ]),                                          #         him only after pass 1
}

# Live session 2c (livefix fe746fa, 2026-09-28), the same form: the end of an action with the
# attack key down. t2 / t3 / t5: timed taps at 0 / 480 / 1120 ms - the third lands on the
# tick E 630 of the swing the second began (600 on the tick before), which ends that swing;
# the key still down 30 ms later starts a new stage 0 (A: state 4 -> 8 -> 4). f1@65: a key
# pressed in a dash attack and held past its end (E 2160) starts a stage-0 swing 30 ms after
# it (A: 4 -> 8 -> 4 at 17:40:42.186 / .211), carried into stage 1 by the held key.
LIVE_2C = {
    't2': dict(facing=2, want=[(490, 2), (490, 2)], old=[(490, 2), (470, 2)], words=[
        (0x00200001, 180, 810),                  # 17:28:12.300 walking left
        (0x00200001, 210, 810),                  # 12.510
        (0x00200000, 90, 810),                   # 12.599
        (0x00200004, 120, 810),                  # 12.720 tap 1 (0 ms): a swing
        (0x00200000, 90, 810),                   # 12.810
        (0x00206000, 30, 250),                   # 12.839 ae 6: it ends
        (0x00200000, 210, 250),                  # 13.050
        (0x00200004, 150, 250),                  # 13.199 tap 2 (480 ms): a swing
        (0x00200000, 90, 250),                   # 13.291
        (0x00270000, 60, 250, 1, -71.5, 7),      # 13.350 E 120
        (0x00200000, 210, 250),                  # 13.559
        (0x00200000, 210, 250),                  # 13.769
        (0x00200004, 60, 250),                   # 13.829 tap 3 (1110 ms): on E 630, the end tick
        (0x00200000, 90, 250),                   # 13.920 down on the next tick: a new stage 0
        (0x00270000, 90, 250, 1, -79.0, 7),      # 14.010 E 120 (was E 780, stage 1: 470)
    ]),
    't3': dict(facing=2, want=[(490, 2), (490, 2)], old=[(490, 2), (470, 2)], words=[
        (0x00200001, 120, 250),                  # 17:28:21.540 walking left
        (0x00206001, 120, 250),                  # 21.660 ae 6
        (0x00200000, 120, 250),                  # 21.780
        (0x00200001, 120, 250),                  # 21.900
        (0x00200000, 210, 250),                  # 22.109
        (0x00206000, 90, 250),                   # 22.199 ae 6
        (0x00200004, 60, 250),                   # 22.260 tap 1, in the hurt
        (0x00200000, 60, 250),                   # 22.319 let go in it: nothing
        (0x00200000, 210, 250),                  # 22.530
        (0x00200004, 210, 250),                  # 22.740 tap 2: a swing
        (0x00200000, 90, 250),                   # 22.829
        (0x00270000, 60, 250, 1, -71.5, 7),      # 22.891 E 120
        (0x00200000, 210, 250),                  # 23.100
        (0x00200000, 210, 250),                  # 23.309
        (0x00200004, 60, 250),                   # 23.370 tap 3: on E 630, the end tick
        (0x00200000, 90, 250),                   # 23.460
        (0x00270000, 90, 250, 1, -71.5, 7),      # 23.550 E 120 (was 470)
    ]),
    't5': dict(facing=2, want=[(490, 2), (490, 2), (490, 2)], old=[(490, 2), (490, 2), (470, 2)], words=[
        (0x00206000, 120, 250),                  # 17:28:45.539 ae 6
        (0x00200000, 210, 250),                  # 45.750
        (0x00200004, 180, 250),                  # 45.930 tap 1: a swing
        (0x00200000, 90, 250),                   # 46.020
        (0x00276000, 60, 250, 1, -34.0, 7),      # 46.079 E 120, and hurt again (ae 6)
        (0x00200000, 210, 250),                  # 46.290
        (0x00200004, 120, 250),                  # 46.409 tap 2: a swing
        (0x00200000, 90, 250),                   # 46.501
        (0x00270000, 60, 250, 1, -41.5, 7),      # 46.560 E 120
        (0x00200000, 210, 250),                  # 46.771
        (0x00200000, 210, 250),                  # 46.979
        (0x00200004, 60, 250),                   # 47.040 tap 3: on E 630, the end tick
        (0x00200000, 90, 250),                   # 47.129
        (0x00276000, 90, 250, 1, -49.0, 7),      # 47.219 E 120 (was 470), and hurt again
    ]),
    'f1@65': dict(facing=6, want=[(760, 2), (490, 2), (500, 2)], old=[(760, 2), (760, 2), (760, 2)], words=[
        (0x00206000, 270, 250),                  # 17:40:41.040 ae 6 (64.47)
        (0x00200001, 30, 250),                   # 41.070 left, in the hurt: no turn
        (0x00200000, 150, 250),                  # 41.221
        (0x00200019, 90, 250),                   # 41.311 dash left, out of the hurt: turns him
        (0x00200004, 60, 250),                   # 41.370 the dash attack (out of the wind-up)
        (0x00270000, 60, 250, 1, -71.25, 7),     # 41.429 E 1380 (64.86)
        (0x00200000, 210, 250),                  # 41.641
        (0x00200000, 210, 250),                  # 41.849
        (0x00200004, 90, 250),                   # 41.940 pressed in it (E 1920)
        (0x00200004, 210, 250),                  # 42.151 held: it ends on E 2160 (42.18)
        (0x00270004, 210, 250, 1, -48.75, 7),    # 42.359 E 120 (65.79; was 'held', 760)
        (0x00200004, 210, 250),                  # 42.570 held: stage 1 on E 420
        (0x00200004, 210, 250),                  # 42.779
        (0x00270004, 210, 250, 1, -56.25, 7),    # 42.989 E 750 (66.42; was 'held', 760)
    ]),
}

# Live session 3 (livefix fe746fa, 2026-09-28, 3d_free1, A), the same form: a press on the tick a
# dash attack ends (E 2130 on the tick before, 2160 on its own) starts nothing there, the key
# still down 30 ms later a stage-0 swing (A: 4 -> 8 -> 4 at 18:14:29.125 / .187); fe746fa ran
# the dash attack on (E 2310: the 490 fallback by luck) and then read a new press as stage 0.
LIVE_3 = {
    'd@63': dict(facing=2, want=[(760, 2), (490, 2), (500, 2)], old=[(760, 2), (490, 2), (400, 2)], words=[
        (0x00200001, 2070, 780),                 # 18:14:28.106 walking left
        (0x00200000, 60, 780),                   # 28.167
        (0x00200019, 60, 780),                   # 28.226 dash left
        (0x00200004, 120, 780),                  # 28.346 the dash attack
        (0x00270000, 30, 780, 8, -61.0, 7),      # 28.377 E 1380
        (0x00200004, 120, 780),                  # 28.496 a press in it (E 1500)
        (0x00200000, 30, 780),                   # 28.525
        (0x00200000, 210, 780),                  # 28.737
        (0x00200000, 210, 780),                  # 28.946
        (0x00200004, 180, 780),                  # 29.125 a press on E 2160, the end tick
        (0x00200000, 60, 780),                   # 29.187 down on the next tick: a new stage 0
        (0x00270000, 120, 780, 8, -38.5, 7),     # 29.305 E 120 (was E 2310, the fallback)
        (0x00200000, 210, 780),                  # 29.516
        (0x00200004, 180, 780),                  # 29.695 a press at E 510: stage 1
        (0x00200000, 60, 780),                   # 29.756
        (0x00270000, 180, 780, 8, -46.0, 7),     # 29.935 E 750 (was a new stage 0, E 210: 400)
    ]),
}


def replay(words, facing=None, wt=1, cls=1, tier=0):
    """Feed a LIVE_2B word list to a fresh tracker (his +0x949 seeded) and classify each
    report: [(Hit, report_facing), ...]. A cast's age is the logic time since its marker."""
    t = H.new_tracker()
    t['facing'] = facing
    cast, clock, out = None, 0, []
    for w in words:
        if w[0] == 'cast':
            cast = (w[1], clock)
            continue
        lo, el, hi = w[:3]
        clock += el
        H.observe(t, lo, el, weapon=wt, tier=tier, hi=hi)
        event = (lo >> 16) & 0xF
        if event in (7, 9):
            h = H.classify(t, event, w[5], cls=cls, tier=tier, weapon=wt,
                           cast_skill=cast[0] if cast else None,
                           cast_age_ms=clock - cast[1] if cast else None, attribute_of=ATTR)
            out.append((h, H.report_facing(t)))
    return out


class AttackerHurt(unittest.TestCase):
    """Live session 2b: a monster's hit on the ATTACKER (his own 0x0D action nibble 6..10,
    hi its ms) ends his action on his client; the words he sends in that hurt start nothing,
    and the input still held at its end starts its action there at stage 0 / E 0. His copy
    of the mob had 490 / 500 / 1020 where the old anchor said 280 / 290 / 250 / 410 / 780 or
    the 760 'held' default. And the knockback goes in his +0x949, which a key pressed in a
    hurt, a dash or a swing does not turn."""

    def test_the_live_2b_hits_match_the_attackers_copy(self):
        for name, case in LIVE_2B.items():
            with self.subTest(name):
                got = replay(case['words'], case['facing'])
                self.assertEqual([(h.hurt, f) for h, f in got], case['want'])
                self.assertNotEqual(case['want'], case['old'])

    def test_hurt_len(self):
        """Pass 2 starts +0xE9C at 0x3C for 7 / 8, else 0, and the state machine adds 30 on
        the same tick; state 3 exits on the tick it reaches hi, he acts on the next one
        (live 2b: contact 250 -> 240 ms in state 3, 810 -> 720, 750 -> 660)."""
        self.assertEqual([H.hurt_len(6, 250), H.hurt_len(7, 810), H.hurt_len(7, 750), H.hurt_len(8, 810),
                          H.hurt_len(9, 1020), H.hurt_len(10, 250), H.hurt_len(6, 0), H.hurt_len(7, 30),
                          H.hurt_len(6, 0x7000 | 250)],
                         [270, 750, 690, 750, 1020, 270, 30, 30, 270])

    def test_the_k2_log_lines(self):
        """k2: the first hit after the contact is a swing from standing started at the hurt's
        end; its report in the same packet as the next contact is still that combo (stage 1,
        500); the next swing starts over at stage 0."""
        got = replay(LIVE_2B['k2']['words'], 2)
        self.assertEqual([h.why for h, _ in got], [
            'swing: L 610 - E 120 (weapon 1), started at the end of his own hurt (contact, hi 250)',
            'swing: L 1250 - E 750 (weapon 1), started at the end of his own hurt (contact, hi 250)',
            'swing: L 610 - E 120 (weapon 1), started at the end of his own hurt (contact, hi 250)'])
        self.assertEqual([h.stun for h, _ in got], [430, 440, 430])

    def test_the_i4_ice_spear_restarts_at_the_hurts_end(self):
        """The cast words came 30 ms into the contact's hurt: the cast began at its end (E 270,
        1020 + the ice 1000 on A's copy); the old anchor at the words gave E 510, 780."""
        (h, f), = replay(LIVE_2B['i4']['words'], 2)
        self.assertEqual((h.kind, h.L, h.E, h.hurt, h.stun, h.variant, h.flag, h.element, f),
                         ('skill', 1290, 270, 1020, 2020, 1, 1, H.ELEMENT_ICE, 6))
        self.assertEqual(h.why, 'skill v1: L 1290 - E 270 (weapon 1), started at the end of his own hurt '
                                '(contact, hi 250)')

    def test_a_report_in_the_hurts_own_packet_is_the_old_action(self):
        """The builder sends the hit caught on the last tick and the hurt caught on it in one
        packet; pass 1 still used the running combo (k2 1.404: 500 on A's copy)."""
        t = run([(SWING, 400), (IDLE, 60), (SWING, 240)])                    # stage 1 reached
        H.observe(t, REPORT_7 | (6 << 12) | SWING, 480, weapon=1, hi=250)     # E 750 + ae 6
        self.assertEqual((t['anchor']['kind'], t['busy'], t['hurt_next']['hi']), ('swing', None, 250))
        self.assertEqual(H.classify(t, 7, 7, weapon=1).hurt, 1250 - 750)
        H.observe(t, SWING, 210, weapon=1, hi=250)                            # the hurt begins
        self.assertEqual((t['anchor'], t['busy']['until'] - t['busy']['start']), (None, 270))

    def test_a_key_released_in_the_hurt_starts_nothing(self):
        t = run([(SWING, 400), (IDLE, 60)])
        H.observe(t, 6 << 12, 60, weapon=1, hi=250)
        for lo, ms in ((SWING, 60), (IDLE, 60), (IDLE, 210)):              # pressed and let go
            H.observe(t, lo, ms, weapon=1, hi=250)
        self.assertEqual((t['anchor'], t['busy']), (None, None))
        h = H.classify(t, 7, 7, weapon=1, fallback_ms=490)
        self.assertEqual((h.kind, h.hurt), ('fallback', 490))

    def test_a_guarded_hit_is_no_hurt(self):
        """Pass 2 cases 1..5 only reset +0xE9C: no state 3, the action is not cancelled."""
        t = run([(SWING, 400), (IDLE, 60)])
        H.observe(t, (4 << 12) | SWING, 60, weapon=1, hi=250)
        H.observe(t, IDLE, 30, weapon=1, hi=250)
        self.assertEqual((t['anchor']['kind'], t['busy'], t.get('hurt_next')), ('swing', None, None))

    def test_a_hit_while_the_tracker_still_has_him_hurt(self):
        """His client let him out earlier than hurt_len says (a hurt the tracker cannot time):
        the report is the action his held input starts, E at most the time since that input
        and at most the kind's default - a lower E only raises the hurt."""
        t = run([(IDLE, 400)])
        H.observe(t, 7 << 12, 30, weapon=1, hi=810)                           # the swing: 750 ms
        H.observe(t, SWING, 60, weapon=1, hi=810)
        H.observe(t, REPORT_7 | SWING, 300, weapon=1, hi=810)
        h = H.classify(t, 7, 7, weapon=1)
        self.assertEqual((h.kind, h.E, h.hurt), ('swing', 120, 490))
        self.assertEqual(h.why, 'swing: L 610 - E 120 (weapon 1), his own hurt (action 7, hi 810) not over by '
                                'his words yet: the action his held input starts (E 270 by his words, at most '
                                'the default 120)')
        t = run([(IDLE, 400)])
        H.observe(t, 7 << 12, 30, weapon=1, hi=810)
        H.observe(t, CAST_ICE, 60, weapon=1, hi=810)
        H.observe(t, (9 << 16) | CAST_ICE, 150, weapon=1, hi=810)
        h = H.classify(t, 9, 9, cls=1, weapon=1, attribute_of=ATTR)
        self.assertEqual((h.kind, h.variant, h.E, h.hurt, h.stun), ('skill', 1, 120, 1170, 2170))

    def test_an_airborne_hurt_turns_him_and_starts_no_ground_action(self):
        """ae 8 / 10: state 0x17 (0x413C2A), where the turn check runs (0x4144B2), and out to
        state 9 in the air (0x4154C2), where a held attack is an air attack (0xE): a key in
        the window turns him, nothing is pending, a hit after it is the fallback."""
        t = run([(IDLE | 1, 400)])
        H.observe(t, 8 << 12, 30, weapon=1, hi=810)
        H.observe(t, SWING | 2, 60, weapon=1, hi=810)                         # right, attack held
        self.assertEqual(t['busy']['note'], 'his own hurt (action 8, airborne, hi 810)')
        self.assertEqual((t['busy']['pending'], t['facing']), (None, 6))
        H.observe(t, SWING | 2, 210, weapon=1, hi=810)
        H.observe(t, REPORT_7 | SWING, 210, weapon=1, hi=810)                  # still in the window
        h = H.classify(t, 7, 7, weapon=1, fallback_ms=490)
        self.assertEqual((h.kind, h.hurt, H.report_facing(t)), ('fallback', 490, 6))
        H.observe(t, SWING | 1, 210, weapon=1, hi=810)                         # left, in the window
        self.assertEqual((t['busy']['until'] - t['t'], t['anchor'], t['facing']), (60, None, 2))
        H.observe(t, SWING | 1, 210, weapon=1, hi=810)                         # out: no ground swing
        self.assertEqual((t['busy'], t['anchor']['kind']), (None, 'held'))
        H.observe(t, REPORT_7 | SWING, 90, weapon=1, hi=810)
        h = H.classify(t, 7, 7, weapon=1, fallback_ms=490)
        self.assertEqual((h.kind, h.hurt), ('fallback', H.HELD_SWING_HURT_MS))
        # a ground hurt (ae 7) of the same length holds the key and starts the swing
        t = run([(IDLE | 1, 400)])
        H.observe(t, 7 << 12, 30, weapon=1, hi=810)
        H.observe(t, SWING | 2, 60, weapon=1, hi=810)
        self.assertEqual((t['busy']['pending']['kind'], t['facing']), ('swing', 2))


class ActionEnd(unittest.TestCase):
    """Live session 2c: FUN_00414210 adds 30 to +0xE9C first, then runs the motion case, then
    state 4's combo check and end test (0x415880..0x41592D, 0x4159E2) or state 1's (0x415AD8)
    on that E - an action ends ON the tick its E reaches L, a key down on that tick starts
    nothing (state 4 / 1 at the motion case), the key down on the next tick starts a stage-0
    swing (_end_tick, _roll_over). fe746fa kept the old anchor past its end and read stage 1
    from E (t2 / t3 / t5: 470), or had no E at all ('held', f1 65.79 / 66.42: 760)."""

    def test_the_live_2c_and_3_hits_match_the_attackers_copy(self):
        for name, case in {**LIVE_2C, **LIVE_3}.items():
            with self.subTest(name):
                got = replay(case['words'], case['facing'])
                self.assertEqual([(h.hurt, f) for h, f in got], case['want'])
                self.assertNotEqual(case['want'], case['old'])

    def test_the_2c_log_lines(self):
        (_, _), (h, _) = replay(LIVE_2C['t2']['words'], 2)
        self.assertEqual((h.kind, h.L, h.E, h.stun), ('swing', 610, 120, 430))
        self.assertEqual(h.why, 'swing: L 610 - E 120 (weapon 1), started at the end of the swing (stage 0)')
        got = replay(LIVE_2C['f1@65']['words'], 6)
        self.assertEqual([h.why for h, _ in got], [
            'dash attack: L 2140 - E 1380 (weapon 1)',
            'swing: L 610 - E 120 (weapon 1), started at the end of the dash attack',
            'swing: L 1250 - E 750 (weapon 1), started at the end of the dash attack'])

    def test_end_tick(self):
        """The first tick (t0 + 30k) whose E = e0 + 30k reaches the action's L."""
        t = run([(SWING, 400)])
        self.assertEqual(H._end_tick(t['anchor']), 400 + 630)                 # 610: E 630
        t['anchor']['stage'] = 1
        self.assertEqual(H._end_tick(t['anchor']), 400 + 1260)                # 1250: E 1260
        t['anchor']['stage'] = 2
        self.assertEqual(H._end_tick(t['anchor']), 400 + 2160)                # 2140: E 2160
        t = run([(DASH_R, 500), (DASH_ATTACK_R, 120)])
        self.assertEqual(H._end_tick(t['anchor']), 620 + 780)                 # 1380 + 780 = 2160
        self.assertEqual(H._end_tick(run([(SWING, 400)], wt=3)['anchor']), 400 + 720)
        self.assertEqual(H._end_tick(run([(SWING, 400)], wt=5)['anchor']), 400 + 750)
        self.assertEqual(H._end_tick(run([(CAST_ICE, 40)])['anchor']), 40 + 1290)
        t = run([(DASH_R, 500), (0x16, 120)])                                  # dash strong: E0 150
        self.assertEqual(H._end_tick(t['anchor']), 620 + 1140)
        self.assertIsNone(H._end_tick(held_tracker()['anchor']))
        self.assertIsNone(H._end_tick(None))

    def test_a_press_on_the_end_tick_starts_stage_0_on_the_next(self):
        """Stage 0 from t 400: a press on E 600's tick (t 1000) is inside (410, 610) and goes
        on to stage 1; a press on E 630's tick (t 1030) ends the swing there, and the key down
        on the next tick starts a new swing (t 1060); let go by then, nothing starts."""
        on = run([(SWING, 400), (IDLE, 90), (IDLE, 510), (SWING, 30)])        # t 1030: E 630
        self.assertEqual((on['anchor']['t0'], on['anchor']['stage']), (400, 0))   # still his
        H.observe(on, IDLE, 90, weapon=1)                                     # down on 1060
        self.assertEqual((on['anchor']['kind'], on['anchor']['t0']), ('swing', 1060))
        H.observe(on, REPORT_7, 90, weapon=1)
        h = H.classify(on, 7, 7, weapon=1)
        self.assertEqual((h.kind, h.L, h.E, h.hurt), ('swing', 610, 120, 490))
        early = run([(SWING, 400), (IDLE, 90), (IDLE, 480), (SWING, 30), (IDLE, 90), (REPORT_7, 120)])
        h = H.classify(early, 7, 7, weapon=1)                                 # t 1000: E 600
        self.assertEqual((early['anchor']['t0'], h.L, h.E, h.hurt), (400, 1250, 780, 470))
        gone = run([(SWING, 400), (IDLE, 90), (IDLE, 510), (SWING, 30), (IDLE, 30)])
        self.assertEqual(gone['anchor']['t0'], 400)                           # up on 1060
        H.observe(gone, REPORT_7, 150, weapon=1)
        h = H.classify(gone, 7, 7, weapon=1, fallback_ms=490)
        self.assertEqual((h.kind, h.hurt), ('fallback', 490))
        self.assertEqual(h.why, 'swing words 810 ms old: E 780 is past the action (L 610); '
                                'MOB_HIT_RELAY_HURT_MS 490')

    def test_a_key_held_through_a_dash_attacks_end(self):
        """The dash attack from t 620 (E 1380) ends on E 2160 (t 1400); the key held from its
        E 1920 starts a stage-0 swing on t 1430 and carries it into stage 1 on its E 420."""
        t = run([(DASH_R, 500), (DASH_ATTACK_R, 120), (REPORT_7 | DASH_ATTACK_R, 30), (IDLE, 210),
                 (IDLE, 210), (SWING, 90), (SWING, 210)])                     # t 1370: E 2130
        self.assertEqual((t['anchor']['kind'], t['anchor']['t0']), ('dash', 620))
        H.observe(t, REPORT_7 | SWING, 210, weapon=1)                          # t 1580
        self.assertEqual((t['anchor']['kind'], t['anchor']['t0'], t['anchor']['virtual']), ('swing', 1430, True))
        self.assertEqual(H.classify(t, 7, 7, weapon=1).hurt, 490)
        for lo in (SWING, SWING, REPORT_7 | SWING):
            H.observe(t, lo, 210, weapon=1)                                    # t 2210: E 750
        h = H.classify(t, 7, 7, weapon=1)
        self.assertEqual((t['anchor']['stage'], h.L, h.E, h.hurt), (1, 1250, 750, 500))

    def test_the_report_on_the_end_tick_is_the_old_actions(self):
        """A hit caught on the swing's last tick (E 600) is reported on its end tick: that
        packet is still the old swing's (pass 1 runs before the state machine), even with the
        key down; the next word starts the new swing."""
        t = run([(SWING, 400), (IDLE, 90), (IDLE, 510), (REPORT_7 | SWING, 30)])   # t 1030
        h = H.classify(t, 7, 7, weapon=1)
        self.assertEqual((h.kind, h.L, h.E, h.hurt), ('swing', 610, 600, 10))
        H.observe(t, SWING, 210, weapon=1)
        self.assertEqual((t['anchor']['t0'], t['anchor']['note']), (1060, 'started at the end of the swing (stage 0)'))

    def test_a_press_after_the_end_tick_is_a_plain_swing(self):
        """The key up on the end tick and pressed on the next: a swing from standing (no
        note); held from before the end: the same tick, 'started at the end'."""
        t = run([(SWING, 400), (IDLE, 90), (IDLE, 540), (SWING, 30)])         # t 1060
        self.assertEqual((t['anchor']['t0'], t['anchor']['virtual']), (1060, False))
        t = run([(SWING, 400), (IDLE, 90), (IDLE, 510), (SWING, 30), (SWING, 30)])
        self.assertEqual((t['anchor']['t0'], t['anchor']['virtual']), (1060, True))


class Facing(unittest.TestCase):
    """His +0x949 from his own words: the turn check (0x414498..0x41450F) runs only in states
    8 / 0xC / the air, before the tick's motion - a key in a hurt, a dash (5 / 6 / 7), a swing
    or a cast turns nothing, and a report's own key turns him only after pass 1 used the old
    facing. Live 2b: 85 / 85 hits against A's +0x95B (the held key missed 5, the tail's side 9)."""

    def test_a_key_standing_turns_him_a_key_in_an_action_does_not(self):
        t = run([(IDLE | 1, 400)])                                             # left, standing
        self.assertEqual(t['facing'], 2)
        H.observe(t, SWING, 60, weapon=1)                                      # a swing (stage 0)
        H.observe(t, IDLE | 2, 60, weapon=1)                                   # right, mid-swing
        self.assertEqual(t['facing'], 2)
        H.observe(t, REPORT_7 | 2, 60, weapon=1)
        self.assertEqual(H.report_facing(t), 2)
        H.observe(t, IDLE | 2, 700, weapon=1)                                  # it ended at E 610, still right
        self.assertEqual(H.report_facing(t), 6)
        # the attack key held on carries the combo into stage 1 (to 1250): still no turn
        t = run([(IDLE | 1, 400), (SWING, 60), (SWING | 2, 60), (REPORT_7 | SWING | 2, 60),
                 (SWING | 2, 600)])
        self.assertEqual(H.report_facing(t), 2)

    def test_the_reports_own_key_is_after_pass_1(self):
        t = run([(IDLE | 1, 400), (IDLE, 60)])
        H.observe(t, REPORT_7 | 2, 60, weapon=1)                               # standing, right
        self.assertEqual((H.report_facing(t), t['facing']), (2, 6))

    def test_a_dash_attack_starts_in_state_6_where_the_turn_check_does_not_run(self):
        """Motion case 1 / 5 in state 6 copies +0x949 to +0x95B without turning him, and the
        turn check skips state 6: a dash right with the attack word's arrow reversed (left)
        still knocks right - on the attack word's tick (the report one tick later), with an
        early word (the dash attack starts at the dash + 90 ms), for the dash strong attack
        and for a cast out of the dash. A swing from standing turns him on its first tick."""
        words = [(IDLE | 2, 400), (IDLE, 60), (DASH_R, 60), (DASH_R, 120)]
        t = run(words + [(0x05, 120)])                                        # attack + left
        self.assertEqual((t['anchor']['kind'], t['facing']), ('dash', 6))
        H.observe(t, REPORT_7 | 0x05, 30, weapon=1)
        self.assertEqual((H.report_facing(t), H.classify(t, 7, 7, weapon=1).hurt), (6, 760))
        t = run([(IDLE | 2, 400), (IDLE, 60), (DASH_R, 60), (0x05, 30)])        # 30 ms into the dash
        self.assertEqual(t['anchor']['t0'] - t['t'], 60)
        H.observe(t, REPORT_7 | 0x05, 90, weapon=1)                            # on the tick it starts
        self.assertEqual((H.report_facing(t), H.classify(t, 7, 7, weapon=1).hurt), (6, 760))
        t = run(words + [(0x15, 120)])                                        # strong attack + left
        self.assertEqual((t['anchor']['kind'], t['anchor']['from_dash']), ('strong', True))
        H.observe(t, REPORT_9 | 0x15, 30, weapon=1)
        self.assertEqual((H.report_facing(t), H.classify(t, 9, 9, weapon=1).E), (6, 150))
        t = run(words + [(CAST_ICE | 1, 120)])                                # a cast + left
        self.assertEqual((t['anchor']['kind'], t['anchor']['from_dash']), ('cast', True))
        H.observe(t, REPORT_9 | CAST_ICE | 1, 30, weapon=1)
        self.assertEqual(H.report_facing(t), 6)
        t = run([(IDLE | 2, 400), (IDLE, 60), (SWING | 1, 60), (REPORT_7 | SWING | 1, 30)])
        self.assertEqual(H.report_facing(t), 2)                               # standing: turned

    def test_a_press_let_go_before_the_window_ends_the_combo_at_610(self):
        """The re-send of a held key 210 ms into the swing is before the stage-0 window (E
        420..600): let go there, the swing ends at 610 - he is in state 8 and a key turns him,
        and the next press is a new stage-0 swing, not stage 1 (it was 2 and 400: E 850)."""
        t = run([(IDLE | 1, 400), (IDLE, 60), (SWING, 60), (SWING, 210), (IDLE, 60), (IDLE | 2, 400),
                 (SWING | 2, 90), (REPORT_7 | SWING | 2, 120)])
        h = H.classify(t, 7, 7, weapon=1)
        self.assertEqual((H.report_facing(t), h.kind, h.L, h.E, h.hurt), (6, 'swing', 610, 90, 520))

    def test_no_key_yet_is_none(self):
        self.assertIsNone(H.report_facing(run([(SWING, 400), (REPORT_7 | SWING, 150)])))
        self.assertIsNone(H.report_facing(None))


class StateOne(unittest.TestCase):
    """The attack key held or mashed through a cast / strong attack (review 2026-09-28): the
    client (FUN_0042c310) sends motion-1 words every tick the key is down, but FUN_00414210
    acts on motion 1 / 5 only in states 8 / 0xC / 6 - so they start nothing while E < L."""

    def test_the_attack_key_held_through_an_ice_spear_cast(self):
        """Was: kind 'swing (event 9)', hurt 400, flag 0 - no ice on the watchers, a 0.52 s
        gate against the attacker's 2020 ms stun (the live +190 px)."""
        t = run([(CAST_ICE, 40), (SWING, 60), (REPORT_9 | SWING, 240)])
        self.assertEqual(t['anchor']['kind'], 'cast')
        h = H.classify(t, 9, 9, cls=1, weapon=1, cast_skill=260, attribute_of=ATTR)
        self.assertEqual((h.kind, h.L, h.E, h.hurt, h.stun, h.variant, h.flag, h.element),
                         ('skill', 1290, 270, 1020, 2020, 1, 1, H.ELEMENT_ICE))
        # the builder's 210 ms re-sends of the held key, all inside L: still the cast
        t = run([(CAST_ICE, 40), (SWING, 60)] + [(SWING, 210)] * 5 + [(REPORT_9 | SWING, 150)])
        self.assertEqual((t['anchor']['kind'], H.anchor_e(t['anchor'])), ('cast', 1230))
        self.assertEqual(H.classify(t, 9, 9, cls=1, weapon=1, attribute_of=ATTR).hurt, 60)

    def test_the_key_held_past_the_cast_starts_a_swing_at_its_end(self):
        """State 1 ends on the tick E reaches L (t 1330, E 1290: 0x415AD8 reads E after the
        += 30): the key down on the next tick starts a stage-0 swing there, whether its word
        comes on that tick or later (was the held combo, E unknown, 760); a new press after
        an idle word is a swing from standing."""
        for last in (210, 240):
            with self.subTest(last=last):
                t = run([(CAST_ICE, 40), (SWING, 60)] + [(SWING, 210)] * 5)  # E 1080 < 1290
                self.assertEqual((t['anchor']['kind'], H._end_tick(t['anchor'])), ('cast', 1330))
                H.observe(t, SWING, last, weapon=1)                          # t 1360 / 1390
                self.assertEqual((t['anchor']['kind'], t['anchor']['t0']), ('swing', 1360))
                H.observe(t, REPORT_7 | SWING, 360 - last, weapon=1)          # t 1510: E 120
                h = H.classify(t, 7, 7, weapon=1)
                self.assertEqual((h.kind, h.E, h.hurt), ('swing', 120, 490))
                self.assertEqual(h.why, 'swing: L 610 - E 120 (weapon 1), started at the end of the cast')
        # let go inside the cast: the press after its end is a swing from standing
        t = run([(CAST_ICE, 40), (SWING, 60), (IDLE, 60), (IDLE, 1240), (SWING, 30), (IDLE, 60), (REPORT_7, 90)])
        h = H.classify(t, 7, 7, weapon=1)
        self.assertEqual((h.kind, h.E, h.hurt, t['anchor']['virtual']), ('swing', 120, 490, False))
        # held to t 1370 (E 1330), past the end on t 1330: a swing began on 1360, and the
        # press on 1430 is inside it (E 70)
        t = run([(CAST_ICE, 40), (SWING, 60), (IDLE, 1300), (SWING, 30), (IDLE, 60), (REPORT_7, 90)])
        h = H.classify(t, 7, 7, weapon=1)
        self.assertEqual((t['anchor']['t0'], h.kind, h.E, h.hurt), (1360, 'swing', 190, 420))

    def test_the_tier_sizes_the_cast(self):
        """Monk tier 1 Merciless Strike (variant 8): L 3300 - motion-1 words 2 s in are still
        inside it; tier 0 (L 1240) they start a swing."""
        merciless = 0x200000 | (8 << 5) | (7 << 2)
        words = [(merciless, 40), (IDLE, 60), (IDLE, 2000), (SWING, 30)]
        t = H.new_tracker()
        for lo, ms in words:
            H.observe(t, lo, ms, weapon=2, tier=lambda: 1)
        self.assertEqual((t['anchor']['kind'], t['anchor']['L']), ('cast', 3300))
        self.assertEqual(run(words, wt=2)['anchor']['kind'], 'swing')

    def test_the_attack_key_held_through_a_strong_attack(self):
        """Was: a swing anchor, hurt 430 instead of 1020 (the dash-attack failure mode)."""
        t = run([(STRONG, 400), (SWING, 60), (REPORT_9 | SWING, 240)])
        h = H.classify(t, 9, 9, cls=1, weapon=1, attribute_of=ATTR)
        self.assertEqual((h.kind, h.E, h.hurt, h.variant, h.flag), ('strong', 270, 1020, 0, 0))
        # from the dash (E0 150), the attack key mashed at once
        t = run([(DASH_R, 500), (0x16, 120), (SWING, 30), (IDLE, 30), (SWING, 30), (REPORT_9 | SWING, 30)])
        h = H.classify(t, 9, 9, weapon=1)
        self.assertEqual((h.kind, h.E, h.hurt), ('strong', 240, 1050))
        # a strong-attack press inside a cast does not start one either
        t = run([(CAST_ICE, 40), (IDLE, 60), (STRONG, 60), (REPORT_9 | STRONG, 180)])
        self.assertEqual((t['anchor']['kind'], H.classify(t, 9, 9, weapon=1).hurt), ('cast', 1020))

    def test_a_running_cast_wins_over_a_combo_anchor_for_event_9(self):
        """The cast words were never counted (lost, a reset tracker) but the attack skill
        (C2S 0x15) is younger than its L + 300: an event-9 hit is the cast's, whatever
        swing or held anchor the tracker holds; older, it is the promoted swing again."""
        words = [(SWING, 400), (IDLE, 60), (REPORT_9, 90)]
        for age in (300, None, 1589):
            with self.subTest(age=age):
                h = H.classify(run(words), 9, 9, cls=1, weapon=1, cast_skill=260, cast_age_ms=age,
                               attribute_of=ATTR)
                self.assertEqual((h.kind, h.E, h.hurt, h.stun, h.variant, h.flag),
                                 ('skill', 270, 1020, 2020, 1, 1))
        h = H.classify(run(words), 9, 9, cls=1, weapon=1, cast_skill=260, cast_age_ms=300, attribute_of=ATTR)
        self.assertEqual(h.why, 'swing words inside the cast (300 ms old, L 1290): the attack key held '
                                'through state 1; the default E; skill v1: L 1290 - E 270 (weapon 1)')
        h = H.classify(run(words), 9, 9, cls=1, weapon=1, cast_skill=260, cast_age_ms=1590, attribute_of=ATTR)
        self.assertEqual((h.kind, h.hurt, h.variant), ('swing (event 9)', 490, 0))
        held = run([(SWING, 400)] + [(SWING, 210)] * 11 + [(REPORT_9, 90)])
        h = H.classify(held, 9, 9, cls=1, weapon=1, cast_skill=260, cast_age_ms=500, attribute_of=ATTR)
        self.assertEqual((h.kind, h.hurt, h.variant), ('skill', 1020, 1))
        # a no-anchor event 9 with a cast past its L + 300: not the cast's hit
        h = H.classify(None, 9, 9, cls=1, weapon=1, cast_skill=260, cast_age_ms=1700, attribute_of=ATTR)
        self.assertEqual((h.kind, h.hurt, h.variant), ('fallback', 1020, 0))

    def test_the_vampiric_attack_relay_flag_is_0(self):
        """Report 2.2: a tier-2 variant-9 attacker who knows Vampiric Attack (0xB25) gets its
        drain applied to his entity on a watcher's copy too - the relay flag is 0 for it."""
        vampiric = 0x200000 | (9 << 5) | (7 << 2)
        t = run([(vampiric, 40), (IDLE, 60), (REPORT_9, 240)], wt=6)
        h = H.classify(t, 9, 9, cls=6, tier=2, weapon=6, cast_skill=0xB25)
        self.assertEqual((h.kind, h.L, h.hurt, h.variant, h.flag, h.element), ('skill', 1490, 1220, 9, 0, 0))
        self.assertTrue(h.why.endswith('(relay flag 0: NO_RELAY_FLAG, the Vampiric Attack drain)'), h.why)
        t = run([(vampiric, 40), (IDLE, 60), (REPORT_9, 240)], wt=2)         # Monk tier 2: Cartilage Smash
        h = H.classify(t, 9, 9, cls=2, tier=2, weapon=2, cast_skill=0x981)
        self.assertEqual((h.variant, h.flag), (9, 9))
        self.assertEqual([H.relay_flag(9, 6, 2), H.relay_flag(9, 6, 1), H.relay_flag(1, 1, 0),
                          H.relay_flag(0, 6, 2)], [0, 9, 1, 0])


class Tables(unittest.TestCase):
    def test_state_1_lengths(self):
        cases = {(0, 5, 0): 1290, (1, 0, 0): 1290, (1, 5, 0): 1040, (2, 0, 0): 1240, (2, 5, 0): 710,
                 (2, 6, 1): 1640, (2, 6, 2): 1200, (2, 8, 1): 3300, (2, 9, 2): 600, (3, 1, 0): 1390,
                 (3, 8, 1): 1740, (3, 10, 1): 2090, (4, 2, 0): 1240, (4, 5, 1): 700, (5, 1, 0): 1490,
                 (5, 7, 1): 1490, (6, 9, 2): 1490}
        for (wt, v, t), length in cases.items():
            with self.subTest(wt=wt, v=v, t=t):
                self.assertEqual(H.skill_len(wt, v, t), length)
        self.assertEqual(H.cast_len(260, 1, 0), 1290)
        self.assertEqual(H.cast_len(1909, 1, 0), 1040)                        # Battle Charge

    def test_combo_lengths(self):
        self.assertEqual([H.swing_len(1, e) for e in (0, 609, 610, 1249, 1250, 2139, 2140)],
                         [610, 610, 1250, 1250, 2140, 2140, None])
        self.assertEqual((H.swing_len(3, 719), H.swing_len(3, 720), H.swing_len(6, 739)), (720, None, 740))

    def test_variants(self):
        cases = {260: 1, 270: 1, 271: 2, 314: 3, 1898: 4, 1909: 5, 2224: 6, 2235: 7, 2257: 8, 2268: 9,
                 2488: 10, 2675: 7, 2708: 10, 2875: 11, 435: 2, 80: 0, 94: 0, 0: 0}
        for sid, v in cases.items():
            with self.subTest(sid=sid):
                self.assertEqual(H.variant_of(sid), v)

    def test_elements(self):
        self.assertEqual(H.element_of(1, 1, 0, ATTR), (H.ELEMENT_ICE, 1.0))  # Ice Spear
        self.assertEqual(H.element_of(3, 1, 0, ATTR), (H.ELEMENT_WIND, 1.0))  # Wind Cutter
        self.assertEqual(H.element_of(7, 5, 1, ATTR), (H.ELEMENT_WIND, 2.5))  # Nova
        self.assertEqual(H.element_of(0, 1, 0, ATTR), (0, 1.0))               # strong attack
        self.assertEqual(H.element_of(2, 2, 0, ATTR), (0, 1.0))               # Monk v2: early return
        self.assertEqual(H.element_of(9, 2, 2, lambda i: 2), (0, 1.0))        # Cartilage Smash: deferred
        self.assertEqual(H.element_of(6, 4, 2, lambda i: 1), (0, 1.0))        # Time Bomb: deferred
        self.assertEqual(H.element_of(1, 1, 0, lambda i: 7), (0, 1.0))        # not 1..4

    def test_stun_and_slide(self):
        self.assertEqual([H.stun_ms(7, 490), H.stun_ms(8, 490), H.stun_ms(7, 30)], [430, 430, 0])
        self.assertEqual([H.stun_ms(9, 1020), H.stun_ms(9, 1020, H.ELEMENT_ICE), H.stun_ms(10, 1020, 2)],
                         [1020, 2020, 2020])
        self.assertEqual([H.stun_ms(9, 300, H.ELEMENT_WIND), H.stun_ms(9, 1020, H.ELEMENT_WIND)], [600, 1020])
        self.assertEqual([H.slide_ticks(7), H.slide_ticks(9), H.slide_ticks(9, H.ELEMENT_WIND),
                          H.slide_ticks(9, H.ELEMENT_WIND, 2.5), H.slide_ticks(8), H.slide_ticks(10, 4)],
                         [2.0, 4.0, 19.0, 47.5, 0.0, 0.0])


class ClientData(unittest.TestCase):
    """The report's section 5 elements against the client's own hii (record +0x190)."""
    ICE = [(1, 0, 1), (3, 0, 1), (5, 0, 1), (5, 1, 10)]
    WIND = [(1, 0, 3), (1, 0, 5), (1, 1, 6), (1, 1, 8), (2, 0, 4), (2, 1, 7), (2, 1, 9), (2, 2, 6),
            (2, 2, 8), (3, 0, 3), (4, 1, 5), (4, 1, 7), (5, 0, 3), (5, 1, 7), (6, 1, 8)]
    NONE = [(2, 2, 9), (4, 2, 6), (2, 0, 2), (1, 1, 7), (4, 0, 2), (6, 1, 9), (3, 1, 6)]

    def test_the_ice_and_wind_rows(self):
        for build in (B8, B9):
            if not HAVE[build]:
                continue
            EC.configure(DIRS[build], build)
            cat = EC.items()

            def attr(i):
                item = cat.get(i)
                return getattr(item, 'attribute', 0) if item is not None else 0
            for rows, want in ((self.ICE, H.ELEMENT_ICE), (self.WIND, H.ELEMENT_WIND), (self.NONE, 0)):
                for c, t, v in rows:
                    with self.subTest(build=build, cls=c, tier=t, variant=v):
                        self.assertEqual(H.element_of(v, c, t, attr)[0], want)
        EC.configure(DIRS[B8], B8)


if __name__ == '__main__':
    unittest.main(verbosity=2)
