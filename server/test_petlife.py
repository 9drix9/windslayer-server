#!/usr/bin/env python3
"""
test_petlife.py - P15 stage 2 (p15-pet-life): pet-s3, pet-s4, pet-s5 and the T14 auto-loot check
=================================================================================================
  ConfigPetRates  the PET_* rate keys ([I] defaults = the dead host code's rates) and their checks;
  Builders        S2C 0xAE / 0xB1 / 0xB2 bytes (local forms, no uid), the gear species rule
                  (cp-1 CardNpc, the EN row rule without it), is_pet_gear per build;
  Tick2009        the F4 tick over a fake clock: hunger + EXP -> 0xAE (owner only), a level ->
                  0xAF (owner + pet_info holders), < 2 % -> asleep (0xAD 0, H1), asleep regen,
                  field time only (mall / dead / off-world paused, a portal keeps the partial
                  minute), the low 0xAE -> the client's auto-feed 0x85 -> 0xB1 (T6 / T7);
  TickRates2009   the config rates drive the step;
  Emotes2009      C2S 0x86 -> 0xB2 to owner + viewer, level gates, asleep, throttle (T4 / T5);
  Feed2009        C2S 0x85 (cp-2 'en'): 0xB1 {gauge, serial}, a sleeping pet wakes (0xB1 then
                  0xAD; the viewer's 19-byte wake), refusals, the last unit;
  FeedKr2009      the stock exe ('kr'): its 0x85 KR ids are EN non-food rows and are dropped (pet
                  F5 step 1, X8), the 0x48 fallback -> 0x72 + 0xB1 {gauge, 0} + 0xAD, and
                  refused at a full gauge (the food kept);
  Bell2009        C2S 0x15 Pet Bell: awake / < 10 % refused (the 10 % line), the sleep regen to
                  10 % and the wake (0x25 + 0xAD, T8 / T9), no bell, no pet;
  Gear2009        pet gear as cash records: grant / `!give`, the tab count, C2S 0x0F -> 0x1D (owner
                  echo + viewer), look word 16, the own / remote records and the 0x6F after a
                  portal, species and register gates (T10 / T11), the pet-off gate and the
                  unequip -> 0xAC with B holding the pet_info (T12 / T13), a swap, stale records;
  AutoLoot2009    T14: the pet's C2S 0x1F with a pet out - no range / pet-level gate server-side,
                  loot protection, idempotent repeats, the equipment tab counting pets and gear;
  PetLife2008     the 2008 build: no pet route, tick, emote, feed, bell or gear.

Fake clients only (fakeclient.MultiClient); no port is bound, no game client started, and the
live accounts.json is never opened (temp copies; the module checks its hash).
"""
import hashlib
import logging
import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fakeclient as F  # noqa: E402
import cash as CASH  # noqa: E402
import clientview as cview  # noqa: E402
import inventory as INV  # noqa: E402
import packets as P  # noqa: E402
import pets as PETS  # noqa: E402
import records as R  # noqa: E402
from test_pets import _PetWorld2009, _World, _wait, PORTAL_101_TO_102, PORTAL_102_TO_101  # noqa: E402

W = F.import_server()
cfgmod = W.cfgmod
EC = W.EC
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = cfgmod.BUILD_2008, cfgmod.BUILD_2009
DIRS = {B8: cfgmod.DEFAULTS['CLIENT_DIR'], B9: cfgmod.DEFAULTS['CLIENT_DIR_2009']}

PICKY, ULIE = 4294, 4299
RED_HOOD, RED_GLASS, AVIATION_GEAR = 4290, 4291, 4292       # Picky gear: Kind 16, 16, 15
SKULL_HOOD = 4297                                            # Ulie gear (Kind 15)
FOOD20, FOOD100, BELL = 4289, 4287, 4285
HERB, STICK = 5, 179
FEED_85, EMOTE_86 = '0x46D6E1/0x85', '0x44780D/0x86'
USE_15, EQUIP_0F, UNEQUIP_11 = '0x44FE04/0x15', '0x4500D1/0x0F', '0x477368/0x11'
UNEQUIP_83, PET_PICKUP = '0x47739D/0x83', '0x42EA76/0x1F'
LOCAL = {PETS.LOCAL_INFO_GATE: False}

_LIVE_HASH = None


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def setUpModule():
    global _LIVE_HASH
    _LIVE_HASH = _sha(LIVE_ACCOUNTS) if os.path.exists(LIVE_ACCOUNTS) else None
    P.STRICT_FIELDS.add(B9)


def tearDownModule():
    P.STRICT_FIELDS.discard(B9)
    EC.configure(DIRS[B8], B8)
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_petlife.py (tests must only use temp copies)'


def _ops(pkts):
    return [p.opcode for p in pkts]


def _text(pkts):
    return b' | '.join(p.payload for p in pkts if p.opcode == 0x15)


# ================================================================ config ===
class ConfigPetRates(unittest.TestCase):
    def test_defaults_are_the_host_rates(self):
        d = cfgmod.defaults()
        self.assertEqual((d['PET_HUNGER_SECS'], d['PET_HUNGER_STEP'], d['PET_EXP_STEP'], d['PET_SLEEP_REGEN_SECS'],
                          d['PET_SLEEP_REGEN_STEP'], d['PET_FOOD_GAUGE'], d['PET_EMOTE_MIN_SECS']),
                         (60.0, 1, 1, 300.0, 1, 90, 0.7))
        self.assertEqual(PETS.FOOD_GAUGE, 90)
        ok = cfgmod.from_dict({'PET_HUNGER_SECS': 2, 'PET_EXP_STEP': 0, 'PET_EMOTE_MIN_SECS': 0})
        self.assertEqual((ok['PET_HUNGER_SECS'], ok['PET_EXP_STEP']), (2.0, 0))
        for bad in ({'PET_HUNGER_SECS': 0.5}, {'PET_SLEEP_REGEN_SECS': float('nan')}, {'PET_HUNGER_STEP': 101},
                    {'PET_EXP_STEP': -1}, {'PET_FOOD_GAUGE': 0}, {'PET_FOOD_GAUGE': 101},
                    {'PET_EMOTE_MIN_SECS': 61.0}, {'PET_HUNGER_STEP': 1.5}):
            with self.subTest(bad=bad), self.assertRaises(cfgmod.ConfigError):
                cfgmod.from_dict(bad)


# ================================================================ builders ===
class _Def:
    def __init__(self, id_, card_npc, type_=1, kind=16, cash=1):
        self.id, self.card_npc, self.type, self.kind, self.cash = id_, card_npc, type_, kind, cash


class _Catalog(dict):
    client_build = B9


class Builders(unittest.TestCase):
    def test_local_forms(self):
        body = P.build('0xAE', *PETS.ae_fields(10, 300), client_build=B9)
        self.assertEqual(body, struct.pack('<BH', 10, 300))                  # 3 B, no uid
        body = P.build('0xB1', *PETS.b1_fields(90, 0x2001), client_build=B9)
        self.assertEqual(body, struct.pack('<BI', 90, 0x2001))               # 5 B
        self.assertEqual(P.build('0xAE', *PETS.ae_fields(250, 99999), client_build=B9), struct.pack('<BH', 100, 30600))
        self.assertEqual(P.build('0xB2', {'uid': 1, 'pet_action': 0x12}, client_build=B9), bytes.fromhex('0100000012'))
        with self.assertRaises(P.MissingAssume):                            # the receiver gate is stated
            P.build('0xAE', {'pet_hp': 1, 'pet_exp': 1}, client_build=B9)

    def test_gear_species_rule(self):
        cat = _Catalog({PICKY: _Def(PICKY, 182, 6, 14), RED_HOOD: _Def(RED_HOOD, 182), SKULL_HOOD: _Def(SKULL_HOOD, 183),
                        ULIE: _Def(ULIE, 183, 6, 14)})
        self.assertTrue(PETS.gear_species_ok(RED_HOOD, PICKY, cat))
        self.assertFalse(PETS.gear_species_ok(SKULL_HOOD, PICKY, cat))
        self.assertTrue(PETS.gear_species_ok(SKULL_HOOD, ULIE, cat))
        # an unpatched hii (CardNpc 0 everywhere): the four rows below the pet
        bare = _Catalog({i: _Def(i, 0) for i in (RED_HOOD, SKULL_HOOD)})
        bare.update({PICKY: _Def(PICKY, 0, 6, 14), ULIE: _Def(ULIE, 0, 6, 14)})
        self.assertTrue(PETS.gear_species_ok(RED_HOOD, PICKY, bare))
        self.assertFalse(PETS.gear_species_ok(SKULL_HOOD, PICKY, bare))
        self.assertTrue(PETS.gear_species_ok(SKULL_HOOD, ULIE, bare))
        self.assertFalse(PETS.gear_species_ok(1, PICKY, bare))

    def test_is_pet_gear_per_build(self):
        cat = _Catalog({RED_HOOD: _Def(RED_HOOD, 182), STICK: _Def(STICK, 0, 1, 11, 0),
                        PICKY: _Def(PICKY, 182, 6, 14), 9: _Def(9, 0, 1, 15, 0)})
        self.assertTrue(INV.is_pet_gear(RED_HOOD, cat))
        self.assertFalse(INV.is_pet_gear(STICK, cat))
        self.assertFalse(INV.is_pet_gear(PICKY, cat))
        self.assertFalse(INV.is_pet_gear(9, cat))                          # a non-cash Kind 15
        cat.client_build = B8
        self.assertFalse(INV.is_pet_gear(RED_HOOD, cat))


# ================================================================== worlds ===
class _Life2009(_PetWorld2009):
    """TestHero (GM, uid 1) wearing Picky, Watcher (uid 2) holding its pet_info."""

    def setUp(self):
        super().setUp()
        self.give(wear=True)
        self.a.recv_until_quiet(0.1)
        self.b.recv_until_quiet(0.1)
        self.assertTrue(self.seen())
        self.clock = 1000.0
        self.pets = self.server.pets

    def set_pet(self, **fields):
        with self.server.store.lock:
            rec = CASH.equipped_pet(self.hero)
            pet = CASH.normalize_pet(rec['pet'])
            pet.update(fields)
            rec['pet'] = pet

    def p(self):
        return CASH.normalize_pet(self.pet()['pet'])

    def run_clock(self, secs, step=1.0):
        """Tick the pets over `secs` of fake time; (steps, A's packets, B's packets)."""
        if self.a.session.get(PETS.TICK_KEY) is None:
            self.pets.tick(self.clock)
        steps, t = 0, 0.0
        while t < secs:
            t += step
            self.clock += step
            steps += self.pets.tick(self.clock)
        return steps, self.a.recv_until_quiet(0.25), self.b.recv_until_quiet(0.15)

    def sleep_pet(self):
        self.gm('!pet set awake 0')
        self.assertFalse(self.p()['awake'])

    def food(self, item=FOOD20):
        return self.server.cash.grant(self.hero, item)


# ==================================================================== tick ===
class Tick2009(_Life2009, unittest.TestCase):
    def test_hunger_and_exp_go_to_the_owner_only(self):
        self.set_pet(gauge=50, exp=0)
        steps, a_pkts, b_pkts = self.run_clock(59)
        self.assertEqual((steps, _ops(a_pkts), _ops(b_pkts)), (0, [], []))
        steps, a_pkts, b_pkts = self.run_clock(1)
        self.assertEqual((steps, _ops(a_pkts), _ops(b_pkts)), (1, [0xAE], []))
        self.assertEqual(self.a.s2c(a_pkts[0], assume=LOCAL), {'pet_hp': 49, 'pet_exp': 1})
        self.assertEqual((self.p()['gauge'], self.p()['exp'], self.p()['level']), (49, 1, 1))
        steps, a_pkts, _ = self.run_clock(60)
        self.assertEqual((steps, self.p()['gauge'], self.p()['exp']), (1, 48, 2))

    def test_level_up_reaches_the_holders(self):
        self.set_pet(gauge=50, exp=CASH.PET_EXP_TABLE[1] - 1)               # 119: the next step is Lv 2
        steps, a_pkts, b_pkts = self.run_clock(60)
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([0xAF, 0xAE], [0xAF]))
        self.assertEqual(a_pkts[0].payload, bytes.fromhex('0100000002'))
        self.assertEqual(b_pkts[0].payload, bytes.fromhex('0100000002'))
        self.assertEqual(self.p()['level'], 2)
        self.assertEqual(R.pet_block(self.hero)['pet_level'], 2)           # new viewers see it

    def test_falls_asleep_below_2_then_regenerates(self):
        self.set_pet(gauge=1)
        steps, a_pkts, b_pkts = self.run_clock(60)
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([0xAD], [0xAD]))
        self.assertEqual(a_pkts[0].payload, bytes.fromhex('0100000000'))
        self.assertEqual(b_pkts[0].payload, bytes.fromhex('0100000000'))   # H1: B holds the pet_info
        self.assertEqual((self.p()['awake'], self.p()['gauge']), (False, 0))
        self.assertTrue(self.seen())
        # asleep: +1 per 300 s of field time, 0xAE to the owner (the bell gate's gauge)
        steps, a_pkts, b_pkts = self.run_clock(299)
        self.assertEqual(steps, 0)
        steps, a_pkts, b_pkts = self.run_clock(1)
        self.assertEqual((steps, _ops(a_pkts), _ops(b_pkts)), (1, [0xAE], []))
        self.assertEqual(self.a.s2c(a_pkts[0], assume=LOCAL)['pet_hp'], 1)
        # !pet tick skips the waits: 9 more steps -> 10 %
        a_pkts, _ = self.gm('!pet tick 9')
        self.assertEqual(_ops(a_pkts).count(0xAE), 9)
        self.assertEqual(self.p()['gauge'], 10)

    def test_pet_tick_stops_at_the_auto_feed_gauge(self):
        """`!pet tick n` on an awake pet stops after the first 0xAE at <= 10 %: each one makes the
        cp-2 client auto-feed (H8), so a longer burst could use up to n foods."""
        self.set_pet(gauge=14, awake=True)
        a_pkts, _ = self.gm('!pet tick 10')
        self.assertEqual(_ops(a_pkts).count(0xAE), 4)                       # 13, 12, 11, 10
        self.assertEqual((self.p()['gauge'], self.p()['awake']), (10, True))
        self.assertTrue(any(b'after 4 step(s)' in p.payload and b'stopped' in p.payload
                            for p in a_pkts if p.opcode == 0x15), a_pkts)

    def test_sleep_reaches_only_pet_info_holders(self):
        with cview.lock(self.b.session):
            cview.forget_pet_info(self.b.session, 1)
        self.set_pet(gauge=0)
        _steps, a_pkts, b_pkts = self.run_clock(60)
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([0xAD], []))       # H1

    def test_only_field_time_counts(self):
        self.set_pet(gauge=50)
        self.run_clock(30)
        for flag in ('in_cash_shop', 'dead'):
            with self.subTest(flag=flag):
                self.a.session[flag] = True
                try:
                    steps, a_pkts, _ = self.run_clock(120)
                finally:
                    self.a.session[flag] = False
                self.assertEqual((steps, _ops(a_pkts)), (0, []))
        steps, _, _ = self.run_clock(29)
        self.assertEqual(steps, 0)                                          # 30 + 29 field seconds
        # a portal: the load pauses the count, the partial minute is kept
        self.portal(self.a, PORTAL_101_TO_102, 102)
        steps, a_pkts, _ = self.run_clock(1)
        self.assertEqual((steps, self.p()['gauge']), (0, 50))               # the first scan after re-arms
        steps, a_pkts, _ = self.run_clock(1)
        self.assertEqual((steps, _ops(a_pkts), self.p()['gauge']), (1, [0xAE], 49))

    def test_no_tick_without_a_worn_pet_or_offline(self):
        self.gm('!pet off')
        steps, a_pkts, _ = self.run_clock(120)
        self.assertEqual((steps, _ops(a_pkts)), (0, []))
        self.gm('!pet wear')
        self.set_pet(gauge=40)
        self.run_clock(30)
        self.mc.clients.remove(self.a)
        self.a.close()
        self.assertTrue(_wait(lambda: not self.seen()))
        self.assertEqual(self.pets.tick(self.clock + 600), 0)
        self.assertEqual(self.p()['gauge'], 40)                              # nothing ticks offline

    def test_t6_low_gauge_auto_feed(self):
        food = self.food()
        self.set_pet(gauge=11)
        _steps, a_pkts, _ = self.run_clock(60)
        self.assertEqual(self.a.s2c(a_pkts[-1], assume=LOCAL)['pet_hp'], 10)  # <= 10: the client auto-feeds
        self.a.send_c2s(FEED_85, {'food_item_id': FOOD20})                   # what the cp-2 client sends
        a_pkts, b_pkts = self.a.recv_until_quiet(0.3), self.b.recv_until_quiet(0.15)
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([0xB1], []))
        self.assertEqual(self.a.s2c(a_pkts[0], assume=LOCAL), {'pet_hp': 90, 'food_item_serial': food['serial']})
        self.assertEqual((self.p()['gauge'], self.owned()[food['serial']]['qty']), (90, 19))

    def test_t7_no_food_sleeps_on_both(self):
        self.set_pet(gauge=2)
        self.run_clock(60)                                                   # 2 -> 1
        _steps, a_pkts, b_pkts = self.run_clock(60)                          # < 2 -> asleep
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([0xAD], [0xAD]))
        self.assertEqual(R.pet_block(self.hero), {'has_pet': 0})


class TickRates2009(_Life2009, unittest.TestCase):
    config = {'PET_HUNGER_SECS': 2.0, 'PET_HUNGER_STEP': 5, 'PET_EXP_STEP': 400, 'PET_SLEEP_REGEN_SECS': 3.0,
              'PET_SLEEP_REGEN_STEP': 7}

    def test_config_rates(self):
        self.set_pet(gauge=50, exp=0)
        steps, a_pkts, _ = self.run_clock(2)
        self.assertEqual(steps, 1)
        self.assertEqual((self.p()['gauge'], self.p()['exp'], self.p()['level']), (45, 400, 2))
        self.assertEqual(_ops(a_pkts), [0xAF, 0xAE])
        self.sleep_pet()
        steps, _, _ = self.run_clock(3)
        self.assertEqual((steps, self.p()['gauge']), (1, 7))


# ================================================================== emotes ===
class Emotes2009(_Life2009, unittest.TestCase):
    def emote(self, action):
        self.a.session.pop('pet_emote_at', None)
        self.a.send_c2s(EMOTE_86, {'action': action})
        return self.a.recv_until_quiet(0.25), self.b.recv_until_quiet(0.15)

    def test_t4_smile_on_both(self):
        a_pkts, b_pkts = self.emote(0x12)
        self.assertEqual([(p.opcode, p.payload) for p in a_pkts], [(0xB2, bytes.fromhex('0100000012'))])
        self.assertEqual([(p.opcode, p.payload) for p in b_pkts], [(0xB2, bytes.fromhex('0100000012'))])

    def test_t5_level_gates_and_refusals(self):
        for action in (0x14, 0x15, 0x13, 0x00):
            with self.subTest(action=action):
                self.assertEqual(self.emote(action), ([], []))
        self.gm('!pet set level 5')
        self.assertEqual(_ops(self.emote(0x14)[1]), [0xB2])
        self.assertEqual(self.emote(0x15), ([], []))
        self.gm('!pet set level 9')
        self.assertEqual(_ops(self.emote(0x15)[0]), [0xB2])
        self.sleep_pet()
        self.assertEqual(self.emote(0x12), ([], []))

    def test_throttle(self):
        self.assertEqual(_ops(self.emote(0x12)[0]), [0xB2])
        self.a.send_c2s(EMOTE_86, {'action': 0x12})                           # inside 700 ms
        self.assertEqual(_ops(self.a.recv_until_quiet(0.2)), [])
        self.assertEqual(self.server.routes[0x86].handler, '_handle_pet_emote')
        # two emotes 700 ms apart at the client may arrive closer: 0.15 s of slack
        for ago, ops in ((0.62, [0xB2]), (0.4, [])):
            with self.subTest(ago=ago):
                self.a.session['pet_emote_at'] = PETS.time.monotonic() - ago
                self.a.send_c2s(EMOTE_86, {'action': 0x12})
                self.assertEqual(_ops(self.a.recv_until_quiet(0.2)), ops)
                self.b.recv_until_quiet(0.1)


# ==================================================================== feed ===
class Feed2009(_Life2009, unittest.TestCase):
    def feed(self, item=FOOD20):
        self.a.send_c2s(FEED_85, {'food_item_id': item})
        return self.a.recv_until_quiet(0.3), self.b.recv_until_quiet(0.15)

    def test_feed_awake(self):
        food = self.food()
        self.set_pet(gauge=40)
        a_pkts, b_pkts = self.feed()
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([0xB1], []))       # no 0x72 / 0x6F: by serial
        self.assertEqual(a_pkts[0].payload, struct.pack('<BI', 90, food['serial']))
        self.assertEqual(self.owned()[food['serial']]['qty'], 19)
        self.set_pet(gauge=95)                                               # max(gauge, 90)
        a_pkts, _ = self.feed()
        self.assertEqual((self.a.s2c(a_pkts[0], assume=LOCAL)['pet_hp'], self.p()['gauge']), (95, 95))
        self.assertEqual(self.server.routes[0x85].handler, '_handle_pet_feed')

    def test_feed_wakes_a_sleeping_pet(self):
        food = self.food()
        self.sleep_pet()
        self.portal(self.b, PORTAL_101_TO_102, 102)                          # B: no pet_info of A
        self.portal(self.b, PORTAL_102_TO_101, 101)
        self.a.recv_until_quiet(0.2)
        self.assertFalse(self.seen())
        a_pkts, b_pkts = self.feed()
        self.assertEqual(_ops(a_pkts), [0xB1, 0xAD])                          # 0xB1 never wakes
        self.assertEqual(a_pkts[1].payload, bytes.fromhex('0100000001'))
        self.assertEqual(_ops(b_pkts), [0xAD])
        got = self.b.s2c(b_pkts[0], assume={PETS.AD_GATE: False, PETS.LOCAL: False})
        self.assertEqual((got['awake'], got['pet_name'], len(b_pkts[0].payload)), (1, 'Picky', 19))
        self.assertTrue(self.seen())
        self.assertEqual((self.p()['awake'], self.p()['gauge'], self.owned()[food['serial']]['qty']), (True, 90, 19))

    def test_refusals_send_nothing(self):
        self.set_pet(gauge=100)
        food = self.food()
        self.assertEqual(self.feed(), ([], []))                              # full: the client refuses too
        self.set_pet(gauge=50)
        for item in (BELL, 1234, PICKY):                                     # no food id of the cp-2 exe
            with self.subTest(item=item):
                self.assertEqual(self.feed(item), ([], []))
        self.assertEqual(self.feed(FOOD100), ([], []))                       # owns none
        self.assertEqual(self.owned()[food['serial']]['qty'], 20)
        self.gm('!pet off')
        self.assertEqual(self.feed(), ([], []))
        self.assertEqual(self.owned()[food['serial']]['qty'], 20)

    def test_the_last_unit_frees_the_record(self):
        food = self.food()
        with self.server.store.lock:
            self.owned()[food['serial']]['qty'] = 1
        self.set_pet(gauge=5)
        a_pkts, _ = self.feed()
        self.assertEqual(_ops(a_pkts), [0xB1])
        self.assertNotIn(food['serial'], self.owned())
        self.set_pet(gauge=5)
        self.assertEqual(self.feed(), ([], []))


class FeedKr2009(_Life2009, unittest.TestCase):
    """CLIENT_ITEM_IDS 'kr' (the stock exe): 0x85 carries the KR ids; bag food goes 0x48."""
    config = {'CLIENT_ITEM_IDS': 'kr'}

    def test_kr_ids_on_0x85_are_no_food(self):
        """pet F5 step 1: the stock exe's auto-feed asks for its KR 4282..4285 - EN non-food rows
        (ADDENDUM X8): it found e.g. the Premium Guild Billboard record 4283 in its owned list.
        Dropped (no reply), the player's Pet Food kept; an EN food id as sent still feeds."""
        food = self.food(FOOD20)
        self.server.cash.grant(self.hero, 4283)                             # the premium board record
        self.set_pet(gauge=30)
        for kr in (FOOD20 - 4, FOOD100 - 4):                                 # KR 4285 / 4283
            with self.subTest(kr=kr), self.assertLogs('WS', logging.INFO) as logs:
                self.a.send_c2s(FEED_85, {'food_item_id': kr})
                self.assertEqual(_ops(self.a.recv_until_quiet(0.2)), [])
            self.assertTrue(any('X8 misroute' in m for m in logs.output), logs.output)
        self.assertEqual((self.p()['gauge'], self.owned()[food['serial']]['qty']), (30, 20))
        self.a.send_c2s(FEED_85, {'food_item_id': FOOD20})
        a_pkts = self.a.recv_until_quiet(0.3)
        self.assertEqual(a_pkts[0].payload, struct.pack('<BI', 90, food['serial']))
        self.assertEqual(self.owned()[food['serial']]['qty'], 19)

    def test_0x48_at_a_full_gauge_keeps_the_food(self):
        """The 0x48 fallback refuses at 100 % like the 0x85 path (the client's manual feed): the
        0x72 {uid, 0, 0} closes the box and the food is kept."""
        food = self.food(FOOD20)
        self.set_pet(gauge=100)
        self.a.send_c2s(P.variants(0x48, 'C2S', client_build=B9)[0]['key'], {'item_id': FOOD20})
        a_pkts = self.a.recv_until_quiet(0.3)
        self.assertEqual(_ops(a_pkts), [0x72])
        self.assertEqual(self.a.s2c(a_pkts[0])['item_id'], 0)
        self.assertEqual((self.p()['gauge'], self.owned()[food['serial']]['qty']), (100, 20))

    def test_0x48_fallback_with_0xb1(self):
        food = self.food(FOOD20)
        self.sleep_pet()
        self.a.send_c2s(P.variants(0x48, 'C2S', client_build=B9)[0]['key'], {'item_id': FOOD20})
        a_pkts, b_pkts = self.a.recv_until_quiet(0.3), self.b.recv_until_quiet(0.2)
        self.assertEqual(_ops(a_pkts)[:3], [0x72, 0xB1, 0xAD])
        self.assertEqual(a_pkts[1].payload, struct.pack('<BI', 90, 0))       # consumed once (the 0x72)
        self.assertIn(0xAD, _ops(b_pkts))
        self.assertEqual((self.p()['awake'], self.owned()[food['serial']]['qty']), (True, 19))


# ==================================================================== bell ===
class Bell2009(_Life2009, unittest.TestCase):
    def ring(self):
        self.a.send_c2s(USE_15, {'item_id': BELL})
        return self.a.recv_until_quiet(0.3), self.b.recv_until_quiet(0.15)

    def bells(self):
        return INV.Inventory(self.hero).count(BELL)

    def test_t8_t9_the_10_percent_rule(self):
        self.gm('!pet bell 2')
        self.assertEqual(self.ring(), ([], []))                              # awake: nothing
        self.sleep_pet()
        a_pkts, b_pkts = self.ring()                                         # 0 %: the line (stock exe)
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([0x15], []))
        self.assertIn(PETS.BELL_TEXT.encode(), a_pkts[0].payload)
        self.set_pet(gauge=9)
        self.assertEqual(_ops(self.ring()[0]), [0x15])
        self.assertEqual(self.bells(), 2)
        # the sleep regen brings it to 10 % (the client's gauge with it: 0xAE)
        self.gm('!pet tick')
        self.assertEqual(self.p()['gauge'], 10)
        a_pkts, b_pkts = self.ring()
        self.assertEqual(_ops(a_pkts), [0x25, 0xAD])
        self.assertEqual((a_pkts[0].payload, a_pkts[1].payload), (struct.pack('<H', BELL), bytes.fromhex('0100000001')))
        self.assertEqual(_ops(b_pkts), [0xAD])
        self.assertEqual(len(b_pkts[0].payload), 19)
        self.assertEqual((self.p()['awake'], self.p()['gauge'], self.bells()), (True, 10, 1))
        self.assertEqual(R.pet_block(self.hero)['has_pet'], 1)

    def test_no_bell_no_pet(self):
        self.sleep_pet()
        self.set_pet(gauge=50)
        a_pkts, _ = self.ring()                                              # the bag has none
        self.assertEqual(_ops(a_pkts), [0x23, 0x15])
        self.assertFalse(self.p()['awake'])
        self.gm('!pet bell')
        self.gm('!pet set awake 1')
        self.gm('!pet off')
        self.assertEqual(self.ring(), ([], []))
        self.assertEqual(self.bells(), 1)


# ==================================================================== gear ===
class Gear2009(_Life2009, unittest.TestCase):
    def wear_gear(self, item=RED_HOOD):
        self.a.send_c2s(EQUIP_0F, {'item_id': item, 'stone_count': 0})
        return self.a.recv_until_quiet(0.3), self.b.recv_until_quiet(0.15)

    def take_off_gear(self, item=RED_HOOD):
        self.a.send_c2s(UNEQUIP_11, {'item_id': item, 'enchant_count': 0})
        return self.a.recv_until_quiet(0.3), self.b.recv_until_quiet(0.15)

    def gear_recs(self, item):
        return [r for r in self.hero['cash_items'] if r['item_id'] == item]

    def test_grant_is_a_cash_record_in_the_equipment_tab(self):
        free = INV.Inventory(self.hero).free_slots('equip')
        a_pkts, _ = self.gm(f'!pet gear {RED_HOOD}')
        self.assertIn(0x6F, _ops(a_pkts))
        rows = [r for p in a_pkts if p.opcode == 0x6F for r in self.a.s2c(p)['repeat[count]']]
        self.assertEqual([(r['item_id'], r['is_equipped'], r['limit_type']) for r in rows if r['item_id'] == RED_HOOD],
                         [(RED_HOOD, 0, 0)])
        self.assertEqual(INV.Inventory(self.hero).free_slots('equip'), free - 1)
        self.assertEqual(INV.Inventory(self.hero).count(RED_HOOD), 0)         # never in the bag model
        a_pkts, _ = self.gm(f'!give {SKULL_HOOD}')                            # !give takes the same path
        self.assertIn(b'pet gear', _text(a_pkts))
        self.assertEqual([r['kind'] for r in self.gear_recs(SKULL_HOOD)], [CASH.KIND_PERMANENT])

    def test_t10_wear_and_take_off(self):
        self.gm(f'!pet gear {RED_HOOD}')
        a_pkts, b_pkts = self.wear_gear()
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([0x1D], [0x1D]))
        self.assertEqual(self.a.s2c(a_pkts[0])['uid'], 1)
        self.assertEqual(self.b.s2c(b_pkts[0])['item_id'], RED_HOOD)
        self.assertEqual(R.grid_item(self.a.session, self.hero, 24), RED_HOOD)  # Kind 16 -> grid 24
        self.assertEqual(self.hero['look_ext'][2], EC.items().get(RED_HOOD).spr_num)   # word 16
        self.assertEqual([r['equipped'] for r in self.gear_recs(RED_HOOD)], [True])
        # T12: the pet cannot come off with its hood on
        self.a.send_c2s(UNEQUIP_83, {'pet_item_id': PICKY})
        a_pkts = self.a.recv_until_quiet(0.3)
        self.assertEqual(_ops(a_pkts), [0x15])
        self.assertIn(PETS.GEAR_ON_TEXT.encode(), a_pkts[0].payload)
        # the records after a portal: the own 0x07 slot 24, the 0x6F is_equipped, B's row
        pkts = self.portal(self.a, PORTAL_101_TO_102, 102)
        own = self.a.s2c(next(p for p in pkts if p.opcode == 0x07))['repeat[player_count]'][0]
        self.assertEqual(own['repeat[10]'][9]['cash_equip_item_id'], RED_HOOD)
        rows = [r for p in pkts if p.opcode == 0x6F for r in self.a.s2c(p)['repeat[count]']]
        self.assertEqual({r['item_id']: r['is_equipped'] for r in rows}, {PICKY: 1, RED_HOOD: 1})
        self.b.recv_until_quiet(0.2)
        self.portal(self.a, PORTAL_102_TO_101, 101)
        rows = self.rows_of(self.b, self.b.recv_until_quiet(0.3))
        self.assertEqual(rows[0]['repeat[10]'][9]['cash_equip_item_id'], RED_HOOD)
        self.assertEqual(rows[0]['repeat[17]'][16]['appearance_part'], self.hero['look_ext'][2])
        self.a.recv_until_quiet(0.2)
        # T13: off, then the pet off: B (holding its pet_info) gets the 0xAC
        a_pkts, b_pkts = self.take_off_gear()
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([0x1E], [0x1E]))
        self.assertEqual((R.grid_item(self.a.session, self.hero, 24), self.hero['look_ext'][2]), (0, 0))
        self.assertEqual([r['equipped'] for r in self.gear_recs(RED_HOOD)], [False])
        self.assertEqual(self.take_off_gear(), ([], []))                    # a repeat: nothing
        self.assertTrue(self.seen())
        a_pkts, b_pkts = self.take_off(PICKY)
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([0xAC], [0xAC]))
        self.assertFalse(self.seen())

    def test_t11_species_and_register_gates(self):
        self.gm(f'!pet gear {SKULL_HOOD}')
        a_pkts, b_pkts = self.wear_gear(SKULL_HOOD)
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([0x15], []))
        self.assertIn(PETS.NOT_YOURS_TEXT.encode(), a_pkts[0].payload)
        self.gm(f'!pet gear {RED_HOOD}')
        self.sleep_pet()
        a_pkts, _ = self.wear_gear()
        self.assertIn(PETS.REGISTER_TEXT.encode(), _text(a_pkts))
        self.gm('!pet set awake 1')
        self.gm('!pet off')
        a_pkts, _ = self.wear_gear()
        self.assertIn(PETS.REGISTER_TEXT.encode(), _text(a_pkts))
        self.assertFalse(any(r.get('equipped') for r in self.gear_recs(RED_HOOD)))
        # a worn gear piece blocks wearing a pet (F1) as well
        self.gm('!pet wear')
        self.wear_gear()
        self.give(ULIE)
        a_pkts, b_pkts = self.wear(ULIE)
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([0x15], []))
        self.assertEqual(self.pet()['item_id'], PICKY)

    def test_swap_in_one_slot_and_two_slots(self):
        self.gm(f'!pet gear {RED_HOOD}')
        self.gm(f'!pet gear {RED_GLASS}')
        self.gm(f'!pet gear {AVIATION_GEAR}')
        self.wear_gear(RED_HOOD)
        a_pkts, b_pkts = self.wear_gear(RED_GLASS)                           # the same Kind 16 slot
        self.assertEqual(_ops(a_pkts), [0x1D])
        self.assertEqual(([r['equipped'] for r in self.gear_recs(RED_HOOD)], [r['equipped'] for r in self.gear_recs(RED_GLASS)]),
                         ([False], [True]))
        self.wear_gear(AVIATION_GEAR)                                        # Kind 15 -> grid 23
        self.assertEqual((R.grid_item(self.a.session, self.hero, 23), R.grid_item(self.a.session, self.hero, 24)),
                         (AVIATION_GEAR, RED_GLASS))
        self.assertEqual(self.hero['look_ext'][1], EC.items().get(AVIATION_GEAR).spr_num)   # word 15

    def test_stale_and_record_less_entries(self):
        # a 0x0F for gear the server has no bagged record of: no 0x1D, the 0x6F re-files the bag
        a_pkts, _ = self.wear_gear()
        self.assertEqual(_ops(a_pkts), [0x6F])
        # a worn grid entry without a record (store data from before pet-s5): adopted on its way off
        with self.server.store.lock:
            self.hero['equipped'][24] = {'id': RED_HOOD, 'w': [0] * 6}
        a_pkts, _ = self.take_off_gear()
        self.assertEqual(_ops(a_pkts), [0x1E])
        self.assertEqual([r['equipped'] for r in self.gear_recs(RED_HOOD)], [False])

    def test_full_equipment_tab_refuses_the_take_off(self):
        self.gm(f'!pet gear {RED_HOOD} wear')
        self.gm(f'!give {STICK}')                                           # the tab's one slot (capacity >= 1)
        bag = INV.Inventory(self.hero)
        with self.server.store.lock:
            bag.set_capacity('equip', bag.used_slots('equip'))
        self.assertEqual(bag.free_slots('equip'), 0)
        self.assertEqual(self.take_off_gear(), ([], []))
        self.assertEqual(R.grid_item(self.a.session, self.hero, 24), RED_HOOD)


# ================================================================ auto-loot ===
class AutoLoot2009(_Life2009, unittest.TestCase):
    def place(self, item, owner_uid=0, dx=400.0, words=None):
        x, y = self.a.session.get('pos') or (0.0, 0.0)
        got = self.server._place_ground_item(self.a.session, item, 1, words, x=x + dx, y=y, owner_uid=owner_uid,
                                             source_uid=0, what='test')
        self.a.recv_until_quiet(0.2)
        self.b.recv_until_quiet(0.1)
        return got.ground_id

    def pick(self, gid):
        self.a.send_c2s(PET_PICKUP, {'ground_item_uid': gid})
        return self.a.recv_until_quiet(0.3), self.b.recv_until_quiet(0.15)

    def test_t14_the_server_adds_no_range_or_pet_level_gate(self):
        herbs = INV.Inventory(self.hero).count(HERB)
        gid = self.place(HERB, owner_uid=1)                                  # the owner's own drop, far away
        a_pkts, b_pkts = self.pick(gid)
        self.assertEqual((_ops(a_pkts), _ops(b_pkts)), ([0x13], [0x13]))
        self.assertEqual(self.a.s2c(a_pkts[0])['picker_uid'], 1)
        self.assertEqual(INV.Inventory(self.hero).count(HERB), herbs + 1)
        self.assertEqual(self.pick(gid), ([], []))                           # the 30 ms repeats: nothing
        # equipment at pet level 1: the pet never asks (client gate), the W key may - granted
        self.assertEqual(self.p()['level'], 1)
        gid = self.place(STICK, owner_uid=0)
        self.assertEqual(_ops(self.pick(gid)[0]), [0x13])

    def test_t14_protection_and_the_equipment_tab(self):
        gid = self.place(HERB, owner_uid=2)                                  # Watcher's drop, < 15 s
        self.assertEqual(self.pick(gid), ([], []))
        # a full equipment tab - the bagged pet and pet gear records count in it
        self.gm(f'!pet gear {RED_HOOD}')
        self.give(ULIE)
        bag = INV.Inventory(self.hero)
        self.assertEqual(INV.pet_slots(self.hero), 2)
        with self.server.store.lock:
            bag.set_capacity('equip', bag.used_slots('equip'))
        gid = self.place(STICK)
        self.assertEqual(self.pick(gid), ([], []))
        self.assertIsNotNone(self.server.ground.get(self.a.session['current_map'], gid))


# ==================================================================== 2008 ===
class PetLife2008(_World, unittest.TestCase):
    build = B8

    def test_nothing_on_the_2008_client(self):
        a, b = self.a, self.b
        for op in (0x85, 0x86):
            route = self.server.routes.get(op)
            self.assertNotIn(getattr(route, 'handler', None), ('_handle_pet_feed', '_handle_pet_emote'))
        pets = self.server.pets
        self.assertEqual(pets.tick(1000.0), 0)
        self.assertEqual(pets.emote(a.session, 0x12), 0)
        self.assertIsNone(pets.feed(a.session, FOOD20))
        self.assertIsNone(pets.use_bell(a.session.get('sock'), a.session, BELL))
        self.assertIsNone(pets.step(a.session))
        self.assertFalse(any(INV.is_pet_gear(i) for i in range(1, EC.item_max_id() + 1)))
        a.send_c2s(_use_key_2008(), {'item_id': BELL})                    # 4285 is no 2008 item
        self.assertFalse({0xAD, 0xAE, 0xB1, 0xB2, 0x25} & set(_ops(a.recv_until_quiet(0.2) + b.recv_until_quiet(0.1))))


def _use_key_2008():
    """The 2008 consumable-use site of C2S 0x15 (the item branch)."""
    for v in P.variants(0x15, 'C2S', client_build=B8):
        if [f['name'] for f in v.get('fields', [])] == ['item_id']:
            return v['key']
    return P.variants(0x15, 'C2S', client_build=B8)[0]['key']


if __name__ == '__main__':
    unittest.main()
