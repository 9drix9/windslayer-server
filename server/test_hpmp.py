#!/usr/bin/env python3
"""
test_hpmp.py - the authoritative HP/MP model and natural regeneration
(cs-hp-mp-model, cs-regen; combat_skill.md 1.5 / 3.5 / F2 / F11, roadmap S2-35, B9, B19)

Three layers:
- the client formulas (FUN_00427d40 / FUN_00427f40 / FUN_0041ace0) against every live
  reading recorded so far, including float32 rounding and the exe's own constants;
- the regen state machine (hpmp.regen_*) on a fake clock;
- the server through fakeclient (no port, no game client, temp accounts.json): enter world
  with the stored CURRENT values and the formula maxima, regen ticks as S2C 0x28/0x44,
  no refill on portal or relog, the level-up heal, stat points, equip/unequip and the
  !hp / !mp / !vitals dev commands.

The live P2 finding this pins: `max_hp = session.get('max_hp', char.get('hp', 100))` made
the maximum equal the stored current HP, so a heal above it was lost on relog and nothing
could damage the player.
"""
import hashlib
import json
import logging
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import en_content as EC  # noqa: E402
import fakeclient as F  # noqa: E402
import hpmp  # noqa: E402
import packets as P  # noqa: E402
import progression  # noqa: E402
from wsproto import hexbytes  # noqa: E402

W = F.import_server()
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
EXE = r'C:\Users\ohdri\Desktop\Windslayer 2\re_tools\WindSlayer.exe'
HAVE_HII = os.path.exists(EC.hs_path('windslayer.hii'))
CAP_7E_101_TO_102 = hexbytes('17 00 00 00')
STICK = 179                                    # Wooden Stick, Kind 11 (option stones apply)
HAT = 1290                                     # Yellow Stripe Hat, Kind 10 (stat stones apply)
RING = 1343                                    # Old Gold Ring - Fire, Kind 17 (no option bonus)
WAND = 248                                     # Cleric Wand: Int +2 on the item itself
FIRE_5, WATER_5, EARTH_5, WIND_5 = 2959, 2964, 2969, 2974   # Perfect Elemental Stones (attr 1..4)
TOL_STONE, INT_STONE = 2949, 2944              # stat stones: Tol +5 / Int +5
# TestHero after the store migration: class 0, Lv1, STR 3 / DEX 2 / INT 1 / SPR 3.
HERO = {'class': 0, 'exp': 0, 'str': 3, 'dex': 2, 'int': 1, 'spr': 3}

GM_ACCOUNTS = {
    'test': {
        'password': 'test',
        'characters': [{'name': 'TestHero', 'level': 1, 'class': 0, 'map': 0,
                        'x': 100, 'y': 100, 'hp': 100, 'mp': 50, 'gm': 1}],
    },
    'admin': {'password': 'admin', 'characters': []},
}
_LIVE_HASH = None


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def setUpModule():
    global _LIVE_HASH
    _LIVE_HASH = _sha(LIVE_ACCOUNTS) if os.path.exists(LIVE_ACCOUNTS) else None


def tearDownModule():
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_hpmp.py (tests must only use temp copies)'


def hero(**kw):
    char = dict(HERO)
    char.update(kw)
    return char


# =========================================================================== formulas
class ClientFormulas(unittest.TestCase):
    """FUN_00427d40 / FUN_00427f40 against the values the real client showed."""

    def test_live_readings(self):
        # (job, level, SPR, INT) -> (max HP, max MP), each seen in the live client.
        vectors = [
            ((0, 1, 3, 1), (140, 87)),     # TestHero Lv1, the task's reference reading
            ((0, 2, 3, 1), (142, 89)),     # TestHero Lv2
            ((0, 1, 0, 0), (110, 80)),     # stats 0 (old 0x07): combat_skill#05/#06/#07
            ((0, 2, 0, 0), (112, 82)),     # 0x22 level 2 with stats 0 (login_character)
            ((0, 1, 30, 0), (410, 80)),    # SPR 30 via the stat window: "HP 100/410"
            ((0, 5, 30, 0), (418, 88)),    # 0x22 level 5: "HP 418/418, MP 88/88"
            ((0, 12, 30, 0), (432, 102)),  # level-down clamp "434/434 -> 432/432, 104 -> 102"
            ((0, 13, 30, 0), (434, 104)),  # "Lv 13, HP 434/434, MP 104/104"
            ((1, 1, 0, 0), (270, 115)),    # Warrior, stats 0: "HP 100/270, MP 50/115"
            ((1, 3, 0, 0), (282, 117)),    # class 1 level 3: "HP 100/282, MP 0/117"
        ]
        for (job, level, spr, int_), want in vectors:
            got = (hpmp.max_hp(job, level, spr), hpmp.max_mp(job, level, int_))
            self.assertEqual(got, want, f'job {job} Lv{level} SPR {spr} INT {int_}')

    def test_every_class_level_1_and_99(self):
        """The per-class tables of the switch at 0x427D89 / 0x427F7E, stats 0."""
        lv1 = [(hpmp.max_hp(c, 1, 0), hpmp.max_mp(c, 1, 0)) for c in range(7)]
        self.assertEqual(lv1, [(110, 80), (270, 115), (220, 80), (165, 120), (165, 120),
                               (110, 200), (110, 195)])
        lv99 = [(hpmp.max_hp(c, 99, 0), hpmp.max_mp(c, 99, 0)) for c in range(7)]
        self.assertEqual(lv99, [(306, 276), (858, 213), (710, 178), (557, 267), (459, 512),
                                (306, 788), (306, 685)])

    def test_float32_store_decides_the_truncation(self):
        """1.3 as float32 is 1.29999995: 10 x 1.3f + 15 = 27.9999995, x10 + 15 = 294.999995.
        The client stores that as float32 (295.0) before _ftol2, so it shows 295; a double
        truncation would say 294."""
        self.assertEqual(hpmp.max_hp(4, 1, 10), 295)
        naive = int((10 * hpmp._f32(1.3) + 15.0) * 10.0 + 15.0)
        self.assertEqual(naive, 294)

    def test_guards(self):
        self.assertEqual(hpmp.max_hp(0, 0, 3), 0)          # level 0 -> 0 (HP only)
        self.assertEqual(hpmp.max_mp(0, 0, 1), 87)         # MP has no level-0 guard
        self.assertEqual(hpmp.max_hp(0, 100, 3), 0)
        self.assertEqual(hpmp.max_mp(0, 100, 1), 0)
        self.assertEqual(hpmp.max_hp(7, 1, 3), 0)          # class >= 7
        self.assertEqual(hpmp.max_mp(7, 1, 1), 0)
        self.assertEqual(hpmp.max_hp(0, 1, 3, slot_mhp=50), 190)
        self.assertEqual(hpmp.max_hp(0, 1, 3, bonus_tol=5, flat=25), 215)
        self.assertEqual(hpmp.max_mp(0, 1, 1, bonus_int=2, flat=15), 116)

    def test_tables_are_the_exe_tables(self):
        """Re-read the float32 tables and the switch constants from WindSlayer.exe."""
        try:
            import pefile
        except ImportError:
            self.skipTest('pefile not installed')
        if not os.path.exists(EXE):
            self.skipTest(f'{EXE} not available')
        pe = pefile.PE(EXE, fast_load=True)
        base = pe.OPTIONAL_HEADER.ImageBase

        def f32s(va, n):
            return list(struct.unpack(f'<{n}f', pe.get_data(va - base, 4 * n)))

        def f64(va):
            return struct.unpack('<d', pe.get_data(va - base, 8))[0]

        close = self.assertAlmostEqual
        for got, want in ((f32s(0x6F14B8, 7), hpmp.HP_STAT_MUL), (f32s(0x6F1448, 7), hpmp.HP_STAT_ADD),
                          (f32s(0x6F149C, 7), hpmp.MP_STAT_MUL), (f32s(0x6F142C, 7), hpmp.MP_STAT_ADD)):
            for g, w in zip(got, want):
                close(g, w, places=5)
        self.assertEqual((f64(0x6F8C68), f64(0x6F8A68)), (hpmp.HP_SCALE, hpmp.MP_SCALE))
        # The case constants the two switches load (default 10/2; 20, 6, 5, 15, 4, 3, 25, 1.5).
        consts = {va: f32s(va, 1)[0] for va in (0x6F8A88, 0x6F0E18, 0x6F8A84, 0x6F8A70, 0x6F8A80,
                                                0x6F8A7C, 0x6F8A78, 0x6F8C70, 0x6F8A74, 0x6F0DCC)}
        self.assertEqual(sorted(set(consts.values())), [1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 10.0, 15.0, 20.0, 25.0])


# =========================================================================== bonuses
@unittest.skipUnless(HAVE_HII, 'needs the EN client hs/windslayer.hii (stat and option columns)')
class EquipmentBonus(unittest.TestCase):
    """FUN_0041ace0's equipment walk (field branch)."""

    def test_elemental_stones_on_a_weapon(self):
        """Kind 11 reads option words 0..4 as items and applies them by Attribute: Fire +HP
        (max), Water +MP (max), Earth HP regen, Wind MP regen."""
        b = hpmp.equipment_bonus([(STICK, [FIRE_5, WATER_5, EARTH_5, WIND_5, 0, 0])])
        self.assertEqual(b, hpmp.Bonus(0, 0, 0, 0, 25, 15, 5, 5))

    def test_option_scan_stops_at_the_first_zero(self):
        b = hpmp.equipment_bonus([(STICK, [FIRE_5, 0, WATER_5, 0, 0, 0])])
        self.assertEqual((b.hp, b.mp), (25, 0))

    def test_word_5_adds_stats_and_only_kind_10_takes_option_stats(self):
        self.assertEqual(hpmp.equipment_bonus([(STICK, [0, 0, 0, 0, 0, TOL_STONE])]).tol, 5)
        self.assertEqual(hpmp.equipment_bonus([(STICK, [TOL_STONE, 0, 0, 0, 0, 0])]).tol, 0)
        b = hpmp.equipment_bonus([(HAT, [TOL_STONE, INT_STONE, 0, 0, 0, 0])])
        self.assertEqual((b.int, b.tol), (5, 5))

    def test_other_kinds_ignore_their_words(self):
        self.assertEqual(hpmp.equipment_bonus([(RING, [FIRE_5, WATER_5, 0, 0, 0, TOL_STONE])]),
                         hpmp.Bonus(0, 0, 0, 0, 0, 0, 0, 0))

    def test_item_stat_columns(self):
        self.assertEqual(hpmp.equipment_bonus([(WAND, [0] * 6)]).int, 2)

    def test_derive_from_the_record(self):
        char = hero(equipped={5: {'id': STICK, 'w': [FIRE_5, WATER_5, EARTH_5, WIND_5, 0, 0]}})
        d = hpmp.derive({}, char)
        self.assertEqual((d.max_hp, d.max_mp, d.regen_hp, d.regen_mp), (165, 102, 10, 9))
        char = hero(equipped={5: {'id': STICK, 'w': [0, 0, 0, 0, 0, TOL_STONE]}})
        self.assertEqual(hpmp.derive({}, char).max_hp, 190)          # SPR 3 + 5

    def test_regen_passives(self):
        """Improve Rejuvenation 111..120 (HP column) and Improve Mana Recovery 490..499 (MP)."""
        self.assertEqual(hpmp.derive({}, hero())[2:4], (5, 4))
        self.assertEqual(hpmp.derive({}, hero(skills=[292, 111]))[2:4], (9, 4))
        self.assertEqual(hpmp.derive({}, hero(skills=[496]))[2:4], (5, 38))
        self.assertEqual(hpmp.derive({}, hero(skills=[0, 111]))[2:4], (5, 4))   # list ends at 0

    def test_slot_table_mhp(self):
        """Breath of Vitality Lv1 (mHP 50) in the +0x107C table raises max HP, not MP."""
        d = hpmp.derive({'slot_buffs': [{'id': 2798}]}, hero())
        self.assertEqual((d.max_hp, d.max_mp), (190, 87))

    def test_new_character_is_born_full(self):
        self.assertEqual(hpmp.new_character_vitals(hero()), (140, 87))


# =========================================================================== model
class Model(unittest.TestCase):
    def test_refresh_clamps_or_heals(self):
        s = {'hp': 500, 'mp': 10}
        d = hpmp.refresh(s, hero())
        self.assertEqual((s['hp'], s['mp'], s['max_hp'], s['max_mp']), (140, 10, 140, 87))
        s = {'hp': 30, 'mp': 10}
        hpmp.refresh(s, hero())
        self.assertEqual((s['hp'], s['mp']), (30, 10))                  # a clamp never heals
        hpmp.refresh(s, hero(exp=progression.exp_for_level(2)), full=True)
        self.assertEqual((s['hp'], s['mp'], s['max_hp']), (142, 89, 142))
        self.assertEqual(d.level, 1)

    def test_refresh_never_clamps_to_a_zero_maximum(self):
        s = {'hp': 90, 'mp': 40}
        hpmp.refresh(s, hero(**{'class': 9}))                           # class > 6: client max 0
        self.assertEqual((s['hp'], s['mp']), (90, 40))

    def test_the_stored_hp_is_the_current_value_not_the_maximum(self):
        """The live bug: a record that stored 100 made 100 the maximum."""
        s = {}
        hpmp.refresh(s, hero(hp=100, mp=50))
        self.assertEqual((s['hp'], s['max_hp'], s['mp'], s['max_mp']), (100, 140, 50, 87))

    def test_clamp(self):
        s = {'max_hp': 140, 'max_mp': 87}
        self.assertEqual((hpmp.clamp_hp(s, 999), hpmp.clamp_hp(s, -5), hpmp.clamp_mp(s, 90)), (140, 0, 87))
        self.assertEqual(hpmp.clamp_hp({}, 999), 999)                   # unknown max: no cap


class RegenStateMachine(unittest.TestCase):
    """FUN_00417e10's two timers on a fake clock (period 15 s)."""
    P = 15.0

    def setUp(self):
        self.s = {'hp': 50, 'mp': 20}
        self.d = hpmp.Derived(140, 87, 5, 4, None, 1, 0)
        hpmp.regen_reset(self.s, 0.0, self.P)

    def step(self, now):
        return hpmp.regen_step(self.s, self.d, now, self.P)

    def test_both_tick_every_15_s_while_idle(self):
        self.assertEqual(self.step(14.9), (None, None))
        self.assertEqual(self.step(15.0), (55, 24))
        self.assertEqual(self.step(29.0), (None, None))
        self.assertEqual(self.step(30.0), (60, 28))

    def test_moving_stops_hp_only(self):
        hpmp.regen_activity(self.s, True, 5.0, self.P)
        self.assertEqual(self.step(15.0), (None, 24))                    # MP runs regardless
        hpmp.regen_activity(self.s, False, 20.0, self.P)                # stopped at 20 s
        self.assertEqual(self.step(30.0), (None, 28))
        self.assertEqual(self.step(35.0), (55, None))                   # 15 s after stopping
        hpmp.regen_activity(self.s, False, 36.0, self.P)                # another idle packet
        self.assertEqual(self.step(50.0), (60, 32))                     # did not restart it

    def test_interrupt_restarts_the_idle_window(self):
        hpmp.regen_interrupt(self.s, 10.0, self.P)                      # hit reaction at 10 s
        self.assertEqual(self.step(15.0), (None, 24))
        self.assertEqual(self.step(25.0), (55, None))

    def test_clamped_at_max_and_silent_there(self):
        self.s.update(hp=138, mp=86)
        self.assertEqual(self.step(15.0), (140, 87))
        self.assertEqual(self.step(30.0), (None, None))

    def test_a_dead_player_gets_no_hp(self):
        self.s['hp'] = 0
        self.assertEqual(self.step(15.0), (None, 24))

    def test_a_late_tick_skips_ahead_instead_of_bursting(self):
        self.assertEqual(self.step(100.0), (55, 24))
        self.assertEqual(self.step(100.5), (None, None))
        self.assertEqual(self.s['regen']['mp_due'], 115.0)

    def test_moving_bits(self):
        self.assertFalse(hpmp.moving_state(0x61400000))   # live idle 0x0D (garbage in bits 22-31)
        self.assertFalse(hpmp.moving_state(0x200))        # +0x8B6 = 1 after a map change (C12)
        for lo in (0x1, 0x2, 0x4, 0xC, 0x10):             # left, right, attack, jump, down
            self.assertTrue(hpmp.moving_state(lo), hex(lo))


# =========================================================================== server
def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class ServerHpMp(unittest.TestCase):
    """The model inside the running handler loop (fakeclient over socketpair)."""
    accounts = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_hpmp_')
        self.server = F.make_server(self.tmp, accounts=self.accounts)
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def enter(self, monsters=0):
        c = F.FakeClient(self.server)
        self.clients.append(c)
        c.send_c2s('0x44D8BF/0x01', {'account_id': 'test', 'password': 'test'})
        self.assertEqual(F.FakeClient.decode(c.expect(0x02))['result'], 1)
        c.send_c2s('0x42F904/0x2B', {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907, 'char_name': 'TestHero'})
        pkts = c.expect(0x03, 0x07, 0x15, 0x28, 0x44, *F.mob_packets(monsters))
        self.assertTrue(c.wait_session(lambda s: s.get('in_world')))
        return c, pkts

    def disconnect(self, c):
        c.close()
        c.thread.join(timeout=5.0)
        self.assertFalse(c.thread.is_alive())

    def char(self):
        return self.server.store.find_character('test', 'TestHero')

    def set_hp(self, c, hp=None, mp=None):
        with self.server._combat_lock(c.session):
            if hp is not None:
                c.session['hp'] = hp
            if mp is not None:
                c.session['mp'] = mp

    def due(self, c, key='mp_due'):
        return c.session['regen'][key]

    @staticmethod
    def val(pkt):
        rec = F.FakeClient.decode(pkt)
        return rec.get('hp', rec.get('mp'))

    def move(self, c, state_lo, **extra):
        c.send_c2s('0x42CE94/0x0D', {'realtime_delta_ms': 0, 'map_code': 101, 'logic_elapsed_ms': 30,
                                     'state_lo': state_lo, 'state_hi': 0, **extra})

    def portal(self, c):
        c.send(0x7E, CAP_7E_101_TO_102)
        return c.expect(0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(8))


class EnterAndTransfer(ServerHpMp):
    def test_enter_world_sends_the_stored_current_values_and_derives_the_maxima(self):
        c, pkts = self.enter()
        row = F.FakeClient.decode(pkts[1])['repeat[player_count]'][0]
        self.assertEqual((row['cur_hp'], row['cur_mp']), (100, 50))
        self.assertEqual((self.val(pkts[3]), self.val(pkts[4])), (100, 50))
        self.assertEqual((c.session['max_hp'], c.session['max_mp']), (140, 87))
        self.assertIn('regen', c.session)

    def test_a_stored_value_above_the_maximum_is_cut_not_kept(self):
        with self.server.store.lock:
            self.char()['hp'], self.char()['mp'] = 410, 500      # e.g. after a stat reset
        c, pkts = self.enter()
        self.assertEqual((self.val(pkts[3]), self.val(pkts[4])), (140, 87))

    def test_a_record_saved_dead_comes_back_revived_not_refilled(self):
        # cs-player-death (P3 stage 4): a character that logged out dead enters the way the
        # death dialog's revive would have taken him - REVIVE_HP_PCT (50) of max, not a
        # refill; it used to be a flat 1 HP until the death model existed.
        with self.server.store.lock:
            self.char()['hp'] = 0
        with self.assertLogs('WS', logging.WARNING) as cm:
            c, pkts = self.enter()
        self.assertEqual(self.val(pkts[3]), 70)
        self.assertTrue(any('saved dead' in line for line in cm.output))
        self.assertFalse(c.session['dead'])

    def test_portal_neither_refills_nor_keeps_old_timers(self):
        c, _ = self.enter()
        self.set_hp(c, hp=50, mp=10)
        old_due = self.due(c)
        time.sleep(0.02)
        pkts = self.portal(c)
        self.assertEqual((self.val(pkts[3]), self.val(pkts[4])), (50, 10))
        self.assertGreater(self.due(c), old_due)                  # the new entity's timers
        self.assertEqual((self.char()['hp'], self.char()['mp']), (50, 10))


class Regen(ServerHpMp):
    def test_idle_player_regenerates_by_0x28_and_0x44(self):
        """Exit criterion 4, offline: idle 15 s below max -> HP and MP rise."""
        c, _ = self.enter()                                        # 100/140, 50/87
        now = self.due(c)
        self.assertEqual(self.server._tick_regen(now - 0.1), 0)
        c.expect_silence(0.2)
        self.assertEqual(self.server._tick_regen(now), 2)
        hp, mp = c.expect(0x28, 0x44)
        self.assertEqual((self.val(hp), self.val(mp)), (105, 54))
        self.assertEqual(self.server._tick_regen(now + 15.0), 2)
        hp, mp = c.expect(0x28, 0x44)
        self.assertEqual((self.val(hp), self.val(mp)), (110, 58))
        self.assertEqual((c.session['hp'], c.session['mp']), (110, 58))

    def test_walking_holds_hp_but_not_mp(self):
        c, _ = self.enter()
        self.move(c, 0x2)                                          # walking right
        self.assertTrue(c.wait_session(lambda s: s['regen']['idle_since'] is None))
        now = self.due(c)
        self.server._tick_regen(now)
        self.assertEqual(self.val(c.expect(0x44)), 54)
        self.move(c, 0x61400000)                                   # stopped (live idle blob)
        self.assertTrue(c.wait_session(lambda s: s['regen']['idle_since'] is not None))
        hp_due = self.due(c, 'hp_due')
        self.server._tick_regen(hp_due - 0.1)
        c.expect_silence(0.2)
        self.server._tick_regen(hp_due)
        self.assertEqual(self.val(c.expect(0x28)), 105)

    def test_an_action_event_restarts_the_window_and_death_stops_it(self):
        c, _ = self.enter()
        before = self.due(c, 'hp_due')
        time.sleep(0.02)
        self.move(c, 0x2 << 12, event_source_uid=0x000F0000)       # a hit reaction, standing
        self.assertTrue(c.wait_session(lambda s: s['regen']['hp_due'] > before))
        self.move(c, 0xD << 12, event_source_uid=0)                # the client's own death
        # cs-player-death (P3 stage 4, F8 step 6): a client-side death while the server
        # still had HP is a death too - the dialog (0x3E) is the player's only way out - and
        # a dead player regenerates nothing (it used to get MP only).
        self.assertEqual(c.expect(0x3E).payload, b'')
        self.assertTrue(c.wait_session(lambda s: s['regen']['hp_due'] is None and s.get('dead')))
        self.assertEqual(self.server._tick_regen(self.due(c) + 60.0), 0)
        c.expect_silence(0.2)

    def test_full_vitals_send_nothing(self):
        c, _ = self.enter()
        self.set_hp(c, hp=140, mp=87)
        self.assertEqual(self.server._tick_regen(self.due(c)), 0)
        c.expect_silence(0.2)

    def test_skipped_out_of_world_dead_and_in_rooms(self):
        c, _ = self.enter()
        now = self.due(c)
        c.session['dead'] = True
        self.assertEqual(self.server._tick_regen(now), 0)
        c.session['dead'] = False
        c.session['current_map'] = 9801
        self.assertEqual(self.server._tick_regen(now), 0)
        c.session['current_map'] = 101
        c.session['in_world'] = False
        self.assertEqual(self.server._tick_regen(now), 0)
        c.expect_silence(0.2)

    def test_regenerated_hp_survives_portal_and_relog(self):
        c, _ = self.enter()
        self.server._tick_regen(self.due(c))
        c.expect(0x28, 0x44)
        pkts = self.portal(c)
        self.assertEqual((self.val(pkts[3]), self.val(pkts[4])), (105, 54))
        self.disconnect(c)
        self.server.store.flush()
        with open(self.server.db_file, encoding='utf-8') as f:
            stored = json.load(f)['test']['characters'][0]
        self.assertEqual((stored['hp'], stored['mp'], stored['map']), (105, 54, 102))
        self.assertNotIn('max_hp', stored)                         # never persisted
        again, pkts = self.enter(monsters=8)
        self.assertEqual((self.val(pkts[3]), self.val(pkts[4])), (105, 54))

    def test_a_potion_above_the_old_stored_value_survives_a_relog(self):
        """The P2 finding end to end: herb at 100/140 -> 120, relog -> 120 (was 100)."""
        c, _ = self.enter()
        self.assertIsNotNone(self.server._inv_add(c.session, 5, 1, 'test'))
        c.send_c2s('0x44C2B3/0x15', {'item_id': 5})
        c.expect(0x25)
        self.assertEqual(c.session['hp'], 120)
        self.disconnect(c)
        again, pkts = self.enter()
        self.assertEqual(self.val(pkts[3]), 120)

    def test_the_scheduler_owns_the_tick(self):
        """start() registers it; offline tests call _tick_regen with an explicit clock."""
        self.assertEqual(W.GameServer.REGEN_TICK_SECS, 1.0)
        self.assertEqual(self.server.config.REGEN_SECS, 15.0)


class MaximaEvents(ServerHpMp):
    def test_level_up_heals_to_the_new_maxima(self):
        c, _ = self.enter()
        self.set_hp(c, hp=30, mp=5)
        self.server.grant_exp(c.session, progression.exp_for_level(2))
        self.assertEqual(F.FakeClient.decode(c.expect(0x21))['exp_delta'], 56)   # 0x21 only
        self.assertEqual((c.session['hp'], c.session['mp'], c.session['max_hp']), (142, 89, 142))
        self.assertEqual((self.char()['hp'], self.char()['mp']), (142, 89))

    def test_level_down_only_clamps(self):
        c, _ = self.enter()
        self.server.grant_exp(c.session, progression.exp_for_level(3))
        c.recv()
        self.assertEqual(c.session['hp'], 144)
        self.server.grant_exp(c.session, -60)                      # back to Lv2
        c.recv()
        self.assertEqual((c.session['hp'], c.session['max_hp']), (142, 142))

    def test_a_spr_point_raises_the_maximum_not_the_current(self):
        c, _ = self.enter()
        self.server.grant_exp(c.session, progression.exp_for_level(2))   # 4 free points
        c.recv()
        self.set_hp(c, hp=100)
        c.send_c2s(P.variants(0x04, 'C2S')[0]['key'], {'stat_index': 3})
        self.assertEqual(F.FakeClient.decode(c.expect(0x14))['value'], 4)
        self.assertTrue(_wait(lambda: c.session['max_hp'] == 152))
        self.assertEqual(c.session['hp'], 100)

    @unittest.skipUnless(HAVE_HII, 'needs the EN client hs/windslayer.hii')
    def test_equip_and_unequip_move_the_maxima(self):
        c, _ = self.enter()
        stones = [FIRE_5, WATER_5, EARTH_5, WIND_5]
        self.assertIsNotNone(self.server._inv_add(c.session, STICK, 1, 'test', words=stones + [0, 0]))
        c.send_c2s('0x44C481/0x0F', {'item_id': STICK, 'stone_count': 4, 'extra_option': 0,
                                     'repeat[stone_count]': [{'stone_id': s} for s in stones]})
        c.expect(0x1D)
        self.assertTrue(_wait(lambda: c.session['max_hp'] == 165))
        self.assertEqual((c.session['max_mp'], c.session['hp']), (102, 100))
        self.set_hp(c, hp=160, mp=100)
        c.send_c2s('0x46CE18/0x11', {'item_id': STICK, 'enchant_count': 4, 'enchant_last': 0,
                                     'repeat[enchant_count]': [{'enchant': s} for s in stones]})
        c.expect(0x1E)
        self.assertTrue(_wait(lambda: c.session['max_hp'] == 140))
        self.assertEqual((c.session['hp'], c.session['mp']), (140, 87))


class DevCommands(ServerHpMp):
    accounts = GM_ACCOUNTS

    def gm_line(self, c, text):
        c.send_c2s('0x445CA7/0x03', {'msg_len': len(text), 'message': text})

    def test_hp_mp_and_vitals(self):
        c, _ = self.enter()
        self.gm_line(c, '!hp 30')
        hp, line = c.expect(0x28, 0x15)
        self.assertEqual(self.val(hp), 30)
        self.assertIn('HP 30/140', F.FakeClient.decode(line)['text'])
        self.gm_line(c, '!mp -20')
        mp, _ = c.expect(0x44, 0x15)
        self.assertEqual(self.val(mp), 30)
        self.gm_line(c, '!hp 0')                                   # never 0x28 hp=0 (F8)
        self.assertEqual(self.val(c.expect(0x28, 0x15)[0]), 1)
        self.gm_line(c, '!hp 9999')
        self.assertEqual(self.val(c.expect(0x28, 0x15)[0]), 140)
        self.gm_line(c, '!vitals')
        text = F.FakeClient.decode(c.expect(0x15))['text']
        self.assertIn('HP 140/140 MP 30/87', text)
        self.assertIn('+5 HP', text)
        # The model saw it, so the next regen window starts from there.
        self.gm_line(c, '!hp 100')
        c.expect(0x28, 0x15)
        self.server._tick_regen(self.due(c, 'hp_due'))
        self.assertEqual([self.val(p) for p in c.expect(0x28, 0x44)], [105, 34])


if __name__ == '__main__':
    unittest.main()
