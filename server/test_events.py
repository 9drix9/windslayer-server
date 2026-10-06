#!/usr/bin/env python3
"""
test_events.py - P13 stage 1 (events-core), offline, both client builds
======================================================================
ev-e1 (schedule + "[Announcement]" notices), arch09-exp-pipeline (skeleton, no guild tail),
ev-e2 (EXP multiplier), ev-e3 (once-only login gift), arch09-window-open (S2C 0x80 {1, id}
with the allowlist {0x3FB}) and ev-e4 (the Event News popup after the C2S 0x63 reply);
design re_tools/docs/systems_2009/events_bosses.md A4-A8, C, E1-E4.

- the schedule (events.py): UTC windows, validation of every field, the 88-byte 0x15 limit,
  GM overrides with a fake clock, the multiplier maths, the shipped events.json;
- the wire forms of both builds: 0x15 msg_type 2 "[Announcement] ...", 0x99 sub 9, the
  window opener `80 01 FB 03 00 00` and its refusals (never a password-gate id);
- the P13 store migration (event_gifts_claimed, one-time accounts.json.bak-pre-p13);
- the server through fakeclient for BOTH builds: nothing changes without an event; with one,
  the first C2S 0x63 reply ends 0x15 / 0x99 sub 9 / 0x80 and a portal or relog repeats none
  of them; periodic lines; an event started mid-session; a full bag keeps the claim open;
  kill exp x2 (card first), unchanged after the event; party / quest / mentor sources; the
  '!event' / '!expmult' commands; two clients each get the effects exactly once.

No port is bound, no client is started and the live accounts.json is never opened (temp
copies; the module checks its hash at the end).
"""
import dataclasses
import hashlib
import json
import logging
import os
import re
import shutil
import struct
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import config as cfgmod  # noqa: E402
import en_content as EC  # noqa: E402
import events as E  # noqa: E402
import fakeclient as F  # noqa: E402
import gm  # noqa: E402
import inventory as INV  # noqa: E402
import packets as P  # noqa: E402
import store as S  # noqa: E402

W = F.import_server()
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = '2008', '2009'
DIR8 = os.path.join(HERE, cfgmod.DEFAULTS['CLIENT_DIR'])
DIR9 = os.path.join(HERE, cfgmod.DEFAULTS['CLIENT_DIR_2009'])
HAVE_2008 = os.path.exists(os.path.join(DIR8, 'hs', 'windslayer.hii'))
HAVE_2009 = os.path.exists(os.path.join(DIR9, 'hs', 'windslayer.hii'))
needs_2008 = unittest.skipUnless(HAVE_2008, 'needs the EN 2008 client data (hs/)')
needs_2009 = unittest.skipUnless(HAVE_2009, 'needs the EN 2009 client data (CLIENT_DIR_2009)')

LOVE_POTION, HERB, PUPU_CARD, OPEN_STALL = 1282, 5, 2030, 194
TOWN, PUPU_MAP = 101, 102
EVENT_NEWS_BYTES = bytes.fromhex('80 01 FB 03 00 00')
KEYS = {
    B8: {'login': '0x44D8BF/0x01', 'enter': '0x42F904/0x2B', 'move': '0x42CE94/0x0D',
         'portal': '0x42F76B/0x7E', 'chat': '0x445CA7/0x03', 'cards': '0x44EF67/0x63',
         'register': '0x45A4F8/0x64', 'password': '0x460831/0x51'},
    B9: {'login': '0x451CE5/0x01', 'enter': '0x4315D7/0x2B', 'move': '0x42E704/0x0D',
         'portal': '0x431284/0x7E', 'chat': '0x44790E/0x03', 'cards': '0x453557/0x63',
         'register': '0x45FB50/0x64'},
}
ACCOUNTS = {
    'test': {'password': 'test', 'characters': [
        {'name': 'TestHero', 'level': 10, 'class': 0, 'map': TOWN, 'x': 1411, 'y': 714,
         'hp': 100, 'mp': 50, 'gm': 1}]},
    'admin': {'password': 'admin', 'characters': [
        {'name': 'Bob', 'level': 5, 'class': 0, 'map': TOWN, 'x': 1411, 'y': 714,
         'hp': 100, 'mp': 50}]},
    'hunter': {'password': 'hunter', 'characters': [
        {'name': 'Hunter', 'level': 1, 'class': 0, 'map': PUPU_MAP, 'x': 1200, 'y': 714,
         'hp': 100, 'mp': 50}]},
}
EXIT_EVENT = {'id': 'p13-exit', 'enabled': True, 'start': None, 'end': None, 'exp_mult': 2.0,
              'announce': {'text': 'EXP x2 event! Visit Nicolas.', 'every_min': 30, 'on_enter': True},
              'popup_event_news': True, 'login_gift': [[LOVE_POTION, 5]], 'push_quests': [158]}
ANNOUNCE = b'[Announcement] EXP x2 event! Visit Nicolas.'
_LIVE_HASH = None


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def setUpModule():
    global _LIVE_HASH
    _LIVE_HASH = _sha(LIVE_ACCOUNTS) if os.path.exists(LIVE_ACCOUNTS) else None
    P.STRICT_FIELDS.add(B9)          # every 2009 S2C must use the 2009 field names


def tearDownModule():
    P.STRICT_FIELDS.discard(B9)
    EC.configure(cfgmod.DEFAULTS['CLIENT_DIR'], B8)
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_events.py (tests must only use temp copies)'


def use_build(build):
    EC.configure(cfgmod.DEFAULTS['CLIENT_DIR_2009'] if build == B9 else cfgmod.DEFAULTS['CLIENT_DIR'], build)


def event(**over):
    return {**json.loads(json.dumps(EXIT_EVENT)), **over}


def raced_packet(body):
    """A 0x03 body _map_transfer built, as a packet FakeClient.s2c can decode."""
    return F.Packet(0x03, body, True, 0)


def bag_rows(decoded, item=LOVE_POTION):
    """[(item, quantity)] of every bag row of `item` in a decoded S2C 0x03."""
    out = []

    def walk(value):
        if isinstance(value, dict):
            if value.get('item_id') == item:
                out.append((item, value.get('quantity', value.get('count'))))
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
    walk(decoded)
    return out


class FakeClock:
    """events.Events.clock / .mono stand-in: t advanced by hand."""

    def __init__(self, t=1_000_000.0):
        self.t = float(t)

    def __call__(self):
        return self.t


# =============================================================== schedule ===
class Schedule(unittest.TestCase):
    """events.py on its own: parsing, validation, windows, overrides, the maths."""

    def test_times_are_utc(self):
        self.assertEqual(E.parse_time('2009-02-13T00:00:00Z', 't'), 1234483200.0)
        self.assertEqual(E.parse_time('2009-02-13T00:00:00', 't'), 1234483200.0)          # naive = UTC
        self.assertEqual(E.parse_time('2009-02-13T09:00:00+09:00', 't'), 1234483200.0)
        self.assertIsNone(E.parse_time(None, 't'))
        for bad in ('', 'soon', 20090213, '2009-13-01T00:00:00Z'):
            with self.subTest(bad=bad), self.assertRaises(E.EventError):
                E.parse_time(bad, 't')
        self.assertEqual(E.fmt_time(1234483200.0), '2009-02-13 00:00Z')

    def test_a_valid_event_and_its_window(self):
        ev = E.parse_event(event(start='2009-02-13T00:00:00Z', end='2009-02-16T00:00:00Z'))
        self.assertEqual((ev.id, ev.exp_mult, ev.every_min, ev.on_enter, ev.popup), ('p13-exit', 2.0, 30.0, True, True))
        self.assertEqual((ev.announce, ev.login_gift, ev.push_quests), (ANNOUNCE, ((LOVE_POTION, 5),), (158,)))
        self.assertFalse(ev.scheduled(1234483200.0 - 1))
        self.assertTrue(ev.scheduled(1234483200.0))
        self.assertFalse(ev.scheduled(1234742400.0))                                        # end is exclusive
        self.assertFalse(E.parse_event(event(enabled=False)).scheduled(0))
        self.assertTrue(E.parse_event(event()).scheduled(0))                                # open-ended

    def test_every_bad_field_is_refused(self):
        bad = [
            {'id': ''}, {'id': 'has space'}, {'id': 'x' * 33}, {'id': 7},
            {'start': '2009-02-16T00:00:00Z', 'end': '2009-02-13T00:00:00Z'},
            {'exp_mult': 0}, {'exp_mult': 101}, {'exp_mult': True}, {'exp_mult': '2'},
            {'enabled': 'yes'}, {'popup_event_news': 1},
            {'announce': {'text': 'x' * 74}},                                               # 15 + 74 = 89 B
            {'announce': {'text': ''}}, {'announce': 5},
            {'announce': {'text': 'hi', 'every_min': -1}},
            {'announce': {'text': 'hi', 'every_min': 0.5}}, {'announce': {'text': 'hi', 'every_min': 1441}},
            {'announce': {'text': 'hi', 'on_enter': False, 'every_min': 0}},
            {'login_gift': [[LOVE_POTION]]}, {'login_gift': [[0, 5]]}, {'login_gift': [[LOVE_POTION, 0]]},
            {'login_gift': [[70000, 1]]}, {'login_gift': 1282},
            {'push_quests': [158, 159, 160, 161, 162, 163]}, {'push_quests': [0]},
            {'cash_gift': 'x'},
        ]
        for over in bad:
            with self.subTest(over=over), self.assertRaises(E.EventError):
                E.parse_event(event(**over))

    def test_the_announcement_interval_is_at_least_a_minute(self):
        """P13 review: every_min 0.01 passed and put a line in every chat window at each 5 s
        tick. 0 is the entry line only; else 1 .. 1440 minutes."""
        for every in (0, 1, 1.5, 1440):
            with self.subTest(every=every):
                ev = E.parse_event(event(announce={'text': 'hi', 'every_min': every}))
                self.assertEqual(ev.every_min, float(every))
        for every in (0.01, 0.99):
            with self.subTest(every=every), self.assertRaisesRegex(E.EventError, r'0 \(entry only\) or 1 \.\. 1440'):
                E.parse_event(event(announce={'text': 'hi', 'every_min': every}))

    def test_the_announcement_fits_the_88_byte_buffer_and_gets_the_prefix_once(self):
        self.assertEqual(len(E.announcement_text('x' * 73)), 88)                            # the limit
        self.assertEqual(E.announcement_text('[Announce] kept as is'), b'[Announce] kept as is')
        self.assertEqual(E.announcement_text('[Announcement] once'), b'[Announcement] once')
        self.assertEqual(E.announcement_text('[Anniversary] party'), b'[Announcement] [Anniversary] party')
        self.assertEqual(E.parse_event(event(announce='plain text')).announce, b'[Announcement] plain text')

    def test_ids_are_unique_and_unknown_keys_only_warn(self):
        with self.assertRaises(E.EventError):
            E.parse_schedule({'events': [event(), event()]})
        with self.assertLogs('WS', logging.WARNING) as logs:
            E.parse_schedule({'events': [event(colour='pink', _note='comment')]})
        self.assertTrue(any("'colour'" in line for line in logs.output))
        self.assertFalse(any('_note' in line for line in logs.output))
        self.assertEqual(E.parse_schedule([event()])[0].id, 'p13-exit')                    # a bare list

    def test_the_shipped_schedule_is_valid_and_off(self):
        events = E.load_file(os.path.join(HERE, 'events.json'))
        self.assertEqual([ev.id for ev in events], ['p13-exit'])
        ev = events[0]
        self.assertFalse(ev.enabled)                  # nothing runs until a GM or the operator starts it
        self.assertEqual((ev.exp_mult, ev.login_gift, ev.popup, ev.push_quests),
                         (2.0, ((LOVE_POTION, 5),), True, (158,)))   # P13 exit criterion 2
        self.assertLessEqual(len(ev.announce), E.ANNOUNCE_MAX_BYTES)
        self.assertEqual(E.load_file(os.path.join(HERE, 'no-such-events.json')), [])

    def test_gift_items_are_checked_against_the_active_catalog(self):
        for build in (B8, B9):
            if not (HAVE_2008 if build == B8 else HAVE_2009):
                continue
            with self.subTest(build=build):
                use_build(build)
                E.check_catalog([E.parse_event(event())])                                   # Type 0: fine
                # Type 3, Type 5, none, and a Type 1 costume with the Cash flag (P8: the next
                # S2C 0x6F purges a bag cash item client-side)
                for item in (OPEN_STALL, 3321, 4999, 1848):
                    with self.assertRaises(E.EventError):
                        E.check_catalog([E.parse_event(event(login_gift=[[item, 1]]))])
        use_build(B8)

    def test_the_multiplier_maths(self):
        self.assertEqual([E.scale_exp(x, 2.0) for x in (10, 11, 5, 1)], [20, 22, 10, 2])
        self.assertEqual([E.scale_exp(x, 1.0) for x in (0, 1, 7, 10, 11, 12345)], [0, 1, 7, 10, 11, 12345])
        self.assertEqual(E.scale_exp(1, 0.5), 1)                                           # at least 1
        self.assertEqual(E.scale_exp(3, 1.1), 3)                                           # truncated
        self.assertEqual(E.scale_exp(100, 0.29), 29)                                       # no float loss
        self.assertEqual((E.scale_exp(0, 2.0), E.scale_exp(-5, 2.0)), (0, -5))             # a loss is never scaled

    def test_the_claim_record_is_normalized_idempotently(self):
        char = {}
        self.assertEqual(E.ensure(char), {})
        char[E.CLAIMS_KEY] = {'ok': '2009-02-13T00:00:00Z', 'bad key!': 'x', 'odd': 5, 'none': None}
        self.assertEqual(E.ensure(char), {'ok': '2009-02-13T00:00:00Z', 'odd': '5'})
        snapshot = dict(char[E.CLAIMS_KEY])
        self.assertEqual(E.ensure(char), snapshot)
        char[E.CLAIMS_KEY] = ['not', 'a', 'dict']
        self.assertEqual(E.ensure(char), {})

    def test_config_keys(self):
        d = cfgmod.defaults()
        self.assertEqual((d.EVENTS_FILE, d.EVENT_EXP_QUESTS, d.EVENT_RELOGIN_QUIET_SECS), ('', False, 1800.0))
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.from_dict({'EVENT_RELOGIN_QUIET_SECS': -1})
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.from_dict({'EVENT_EXP_QUESTS': 1})
        with open(os.path.join(HERE, 'config.json'), encoding='utf-8') as f:
            shipped = json.load(f)
        self.assertEqual(shipped['EVENTS_FILE'], '')
        self.assertTrue(issubclass(E.EventError, cfgmod.ConfigError))    # a bad file stops the start


class Overrides(unittest.TestCase):
    """Events.active / exp_multiplier with GM overrides on a fake clock (no server loop)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_events_ov_')
        path = os.path.join(self.tmp, 'events.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'events': [event(enabled=False),
                                  event(id='later', start='2030-01-01T00:00:00Z', exp_mult=3.0,
                                        popup_event_news=False, login_gift=[])]}, f)
        self.server = F.make_server(self.tmp, config=cfgmod.from_dict({'EVENTS_FILE': path}))
        self.ev = self.server.events
        self.clock = self.ev.clock = FakeClock(E.parse_time('2026-09-27T12:00:00Z', 't'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_overrides_and_their_expiry(self):
        ev = self.ev
        self.assertEqual(ev.active(), [])
        self.assertEqual(ev.exp_multiplier(), (1.0, None))
        self.assertEqual(ev.state(ev.get('p13-exit')), 'disabled')
        self.assertEqual(ev.state(ev.get('later')), 'scheduled')
        ev.overrides['p13-exit'] = (True, self.clock.t + 60)
        self.assertEqual([e.id for e in ev.active()], ['p13-exit'])
        self.assertEqual(ev.exp_multiplier(), (2.0, 'event p13-exit'))
        self.assertTrue(ev.state(ev.get('p13-exit')).startswith('ACTIVE (GM until'))
        self.clock.t += 61                                                                   # the timed start ran out
        self.assertEqual(ev.active(), [])
        self.assertNotIn('p13-exit', ev.overrides)
        self.clock.t = E.parse_time('2030-06-01T00:00:00Z', 't')
        self.assertEqual(ev.exp_multiplier(), (3.0, 'event later'))
        ev.overrides['later'] = (False, None)                                              # !event stop
        self.assertEqual((ev.active(), ev.state(ev.get('later'))), ([], 'off (GM)'))
        ev.overrides['p13-exit'] = (True, None)
        ev.overrides.pop('later')
        self.assertEqual(ev.exp_multiplier(), (3.0, 'event later'))                        # the largest, no stacking
        ev.exp_override = 1.5
        self.assertEqual(ev.exp_multiplier(), (1.5, '!expmult'))

    def test_reload_keeps_the_schedule_when_the_file_is_bad(self):
        with open(self.ev.path, 'w', encoding='utf-8') as f:
            f.write('{"events": [{"id": "x", "exp_mult": 0}]}')
        ok, why = self.ev.reload()
        self.assertFalse(ok)
        self.assertIn('exp_mult', why)
        self.assertEqual([e.id for e in self.ev.events], ['p13-exit', 'later'])
        with open(self.ev.path, 'w', encoding='utf-8') as f:
            json.dump({'events': [event(id='fresh')]}, f)
        self.assertEqual(self.ev.reload(), (True, '1 event(s) loaded'))
        self.assertEqual([e.id for e in self.ev.active()], ['fresh'])

    def test_a_bad_file_stops_the_server_start(self):
        path = os.path.join(self.tmp, 'bad.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'events': [event(login_gift=[[OPEN_STALL, 1]])]}, f)                 # a skill book
        with self.assertRaises(cfgmod.ConfigError):
            F.make_server(self.tmp, config=cfgmod.from_dict({'EVENTS_FILE': path}))

    def test_exp_sources(self):
        ev = self.ev
        ev.exp_override = 2.0
        self.assertEqual([ev.scale_exp(10, s) for s in ('kill', 'party', 'quest', 'mentor')], [20, 20, 10, 10])
        self.server.config['EVENT_EXP_QUESTS'] = True
        self.assertEqual(ev.scale_exp(50, 'quest'), 100)
        with self.assertRaises(ValueError):
            ev.scale_exp(10, 'gm')


# ================================================================== wire ===
class Wire(unittest.TestCase):
    """The three packets an event sends, byte-exact in both builds."""

    def test_the_window_opener_is_80_01_FB_03_00_00_in_both_builds(self):
        for build in (B8, B9):
            with self.subTest(build=build):
                body = P.build('0x80', E.window_open_fields(E.WINDOW_EVENT_NEWS), client_build=build)
                self.assertEqual(bytes([0x80]) + body, EVENT_NEWS_BYTES)

    def test_the_window_opener_never_takes_a_password_or_special_id(self):
        for wid in (0x1A7, 0x1F9, 0x1FA, 0x235, 0x4B8, 0x4CF, 0x52, 0, 0x3FC):
            with self.subTest(window=hex(wid)), self.assertRaises(E.WindowOpenRefused):
                E.window_open_fields(wid)
        # two separate allowlists that never share an id (X2); the password one is unchanged
        self.assertEqual(W.GameServer.PASSWORD_WINDOWS, frozenset({0x1A7, 0x1F9, 0x235}))
        self.assertEqual(E.WINDOW_OPEN_ALLOWLIST, frozenset({0x3FB}))
        self.assertFalse(E.WINDOW_OPEN_ALLOWLIST & W.GameServer.PASSWORD_WINDOWS)

    def test_the_announcement_and_gift_bytes(self):
        for build in (B8, B9):
            with self.subTest(build=build):
                body = P.build('0x15', E.announcement_fields('EXP x2 event! Visit Nicolas.'), client_build=build)
                self.assertEqual(body, bytes([2, len(ANNOUNCE)]) + ANNOUNCE)
                self.assertTrue(body[2:].startswith(E.ANNOUNCE_TEST))                       # teal in both
                grant = P.build('0x99', {'sub_type': 9, 'item_id': LOVE_POTION, 'count': 5}, client_build=build)
                self.assertEqual(grant, bytes.fromhex('09 02 05 05 00'))


# =============================================================== migration ===
class MigrationP13(unittest.TestCase):
    """event_gifts_claimed arrives through an idempotent migration that first writes the
    one-time accounts.json.bak-pre-p13 (hard rule for a persisted-schema change)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_p13_store_')
        self.path = os.path.join(self.tmp, 'accounts.json')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pre_p13_file(self, build):
        st = S.Store(self.path, client_build=build).load()            # a current file
        with st.lock:
            for acc in st.accounts.values():
                for ch in acc['characters']:
                    del ch[E.CLAIMS_KEY]
        st.save_now()
        for suffix in S.BACKUP_SUFFIXES + (S.BACKUP_SUFFIX_2009,):
            path = self.path + suffix
            if suffix == S.BACKUP_SUFFIX_P13:
                if os.path.exists(path):
                    os.remove(path)
            elif not os.path.exists(path):
                open(path, 'wb').close()                                # earlier phases: done
        with open(self.path, 'rb') as f:
            return f.read()

    def test_the_claims_are_added_once_with_their_backup(self):
        self.assertEqual(S.BACKUP_SUFFIX_P13, '.bak-pre-p13')
        self.assertIn(S.BACKUP_SUFFIX_P13, S.BACKUP_SUFFIXES)
        for build in (B8, B9):
            with self.subTest(build=build):
                original = self._pre_p13_file(build)
                st = S.Store(self.path, client_build=build).load()
                self.assertEqual(st.migration_changes, ['test/TestHero: event_gifts_claimed created'])
                with open(self.path + '.bak-pre-p13', 'rb') as f:
                    self.assertEqual(f.read(), original)
                with open(self.path, encoding='utf-8') as f:
                    self.assertEqual(json.load(f)['test']['characters'][0][E.CLAIMS_KEY], {})
                again = S.Store(self.path, client_build=build).load()
                self.assertEqual((again.migration_changes, again.saves), ([], 0))
                # a second pre-P13 state never overwrites the first backup
                self._pre_p13_file(build)
                with open(self.path + '.bak-pre-p13', 'wb') as f:
                    f.write(b'first')
                S.Store(self.path, client_build=build).load()
                with open(self.path + '.bak-pre-p13', 'rb') as f:
                    self.assertEqual(f.read(), b'first')
                os.remove(self.path + '.bak-pre-p13')

    def test_a_malformed_claim_record_is_repaired_and_claims_survive(self):
        st = S.Store(self.path).load()
        with st.lock:
            st.find_character('test', 'TestHero')[E.CLAIMS_KEY] = {'p13-exit': '2026-09-27T00:00:00Z', '??': 1}
        st.save_now()
        st = S.Store(self.path).load()
        self.assertEqual(st.find_character('test', 'TestHero')[E.CLAIMS_KEY], {'p13-exit': '2026-09-27T00:00:00Z'})
        self.assertIn('test/TestHero: event_gifts_claimed normalized', st.migration_changes)

    def test_new_characters_carry_the_record(self):
        st = S.Store(self.path).load()
        ch = st.new_character('Nova', s10=1, s1=1, s6=2, s5=2, s9=2, stats=(3, 2, 1, 3))
        self.assertEqual(ch[E.CLAIMS_KEY], {})
        self.assertEqual(S.migrate_character(ch), [])


# ================================================================ server ===
class Rig(unittest.TestCase):
    build = B8
    # The exit event without its quest push: ev-e5 (P13 stage 2) adds 0x26 + 0x59 to the
    # 2009 tail, and test_eventquests.py runs the whole exit event in both builds. Here the
    # stage-1 tail stays 0x15 / 0x99 / 0x80.
    events = ({**EXIT_EVENT, 'push_quests': []},)
    extra_config = {}

    def setUp(self):
        use_build(self.build)
        self.tmp = tempfile.mkdtemp(prefix=f'ws_events{self.build}_')
        path = os.path.join(self.tmp, 'events.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'events': list(self.events)}, f)
        cfg = {'MOB_SERVER_CONTROLLED': True, 'CLIENT_BUILD': self.build, 'EVENTS_FILE': path,
               **self.extra_config}
        self.server = F.make_server(self.tmp, accounts=json.loads(json.dumps(ACCOUNTS)),
                                    config=cfgmod.from_dict(cfg))
        self.mono = self.server.events.mono = FakeClock()
        self.keys = KEYS[self.build]
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        use_build(B8)

    # ------------------------------------------------------------ helpers ---
    def char(self, user='test'):
        return self.server.store.characters(user)[0]

    def bag(self, user='test'):
        return INV.Inventory(self.char(user))

    def disk_char(self, user='test'):
        self.server.store.flush()
        with open(self.server.db_file, encoding='utf-8') as f:
            return json.load(f)[user]['characters'][0]

    def enter(self, user='test', mobs=0):
        """Login + enter world; the entry burst must be the unchanged P0-P7 one (plus the
        world-presence 0x04 / 0x05 when another client stands on the same map)."""
        others = list(self.clients)
        c = F.FakeClient(self.server)
        self.clients.append(c)
        self.assertEqual(c.login(user, user)['result'], 1)
        c.send_c2s(self.keys['enter'], {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907 + len(others),
                                        'char_name': self.char(user)['name']})
        self.assertTrue(c.wait_session(lambda s: s.get('in_world')))
        ops = [0x03, 0x07, 0x15] + ([0x65] if self.build == B9 else []) + [0x28, 0x44]
        F.expect_entry(c, ops + F.mob_packets(mobs), others=others, map_code=self.char(user)['map'])
        return c

    def relog(self, c, user='test', mobs=0):
        self.clients.remove(c)
        F.close_seen(c, self.clients)
        return self.enter(user, mobs)

    def resync(self, c, *extra, first=False):
        """The client's own C2S 0x63 after a map load: 0x8A [+ 0x99 sub 8 on the first of a
        connection] + `extra` (the event tail). Returns the extra packets."""
        c.send_c2s(self.keys['cards'])
        head = [0x8A] + ([0x99] if first else [])
        pkts = c.expect(*head, *extra)
        pkts = pkts if isinstance(pkts, list) else [pkts]
        if first:
            self.assertEqual(c.s2c(pkts[1]), {'sub_type': 8, 'channel_no': 1})
        return pkts[len(head):]

    def check_tail(self, c, pkts, gift=True, announce=True, popup=True):
        """The event tail of a 0x63 reply: 0x15 announcement, 0x99 sub 9 gift, 0x80 popup."""
        want = [0x15] * announce + [0x99] * gift + [0x80] * popup
        self.assertEqual([p.opcode for p in pkts], want)
        for p in pkts:
            if p.opcode == 0x15:
                self.assertEqual(p.payload, bytes([2, len(ANNOUNCE)]) + ANNOUNCE)
            elif p.opcode == 0x99:
                self.assertEqual(c.s2c(p), {'sub_type': 9, 'item_id': LOVE_POTION, 'count': 5})
            elif p.opcode == 0x80:
                self.assertEqual(bytes([0x80]) + p.payload, EVENT_NEWS_BYTES)

    def chat(self, c, line):
        c.send_c2s(self.keys['chat'], {'msg_len': len(line), 'message': line})

    def replies(self, c, n=1):
        pkts = c.expect(*([0x15] * n))
        pkts = pkts if isinstance(pkts, list) else [pkts]
        texts = [c.s2c(p)['text'] for p in pkts]
        return [t.decode('latin-1') if isinstance(t, bytes) else t for t in texts]

    def portal_index(self, src, dst):
        return next(int(k.split('_')[1]) for k, v in sorted(EC.portals().items())
                    if k.startswith(f'{src}_') and v[0] == dst)

    def portal(self, c, src, dst, mobs=0):
        c.send_c2s(self.keys['portal'], {'portal_line_index': self.portal_index(src, dst)})
        c.expect(0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(mobs))
        self.assertTrue(c.wait_session(lambda s: s.get('in_world') and s.get('current_map') == dst))

    def kill_exp(self, c, index):
        """Kill monster `index` with one reported 61 B hit and no drop; the 0x21 delta."""
        mob = c.session['monsters'][W.MOB_UID_BASE + index]
        mob.hp, mob.drop_items = 1, []
        c.send_c2s(self.keys['move'], {
            'realtime_delta_ms': 0, 'map_code': PUPU_MAP, 'logic_elapsed_ms': 30,
            'state_lo': W.HIT_REPORT_EVENT << 16, 'state_hi': 0, 'target_uid': mob.uid,
            'pos_x': mob.x, 'pos_y': mob.y, 'target_dx': 0.0, 'target_dy': 0.0,
            'flag_8db': False, 'flag_8e7': False, 'timer_dac': 0, 'target_action_event': 7})
        pkts = c.expect(0x29, 0x21, 0x18)
        self.assertFalse(mob.alive)
        self.assertEqual(len(pkts[1].payload), 4)                  # 2009: no guild tail (P14)
        return struct.unpack_from('<i', pkts[1].payload)[0]


class NoEventFlow:
    """Without an active event nothing changes: the P0-P7 sequences and numbers."""
    events = (event(enabled=False),)

    def test_the_resync_reply_and_the_exp_are_unchanged(self):
        c = self.enter()
        self.assertEqual(self.resync(c, first=True), [])
        self.portal(c, TOWN, PUPU_MAP, mobs=8)
        self.assertEqual(self.resync(c), [])
        self.assertEqual(self.bag().count(LOVE_POTION), 0)
        self.assertEqual(self.char()[E.CLAIMS_KEY], {})
        base = EC.npcs().get(1).exp                                       # 2008 Pupu 5, 2009 Ssiyo 10
        self.assertEqual(self.kill_exp(c, 0), base)
        self.assertEqual(self.server.events.tick(), 0)
        c.expect_silence(0.2)
        for source in E.EXP_SOURCES:                                      # every source: the amount itself
            self.assertEqual(self.server.events.scale_exp(37, source), 37)


class EventFlow:
    """The exit-criteria event (exp x2, Love Potion x5, popup) in one build."""

    def test_the_first_resync_carries_the_effects_once_and_a_portal_or_relog_none(self):
        c = self.enter()
        self.check_tail(c, self.resync(c, 0x15, 0x99, 0x80, first=True))
        self.assertEqual(self.bag().count(LOVE_POTION), 5)
        claimed = self.disk_char()[E.CLAIMS_KEY]
        self.assertEqual(list(claimed), ['p13-exit'])
        self.assertTrue(re.match(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$', claimed['p13-exit']))
        # portal: the 0x63 after it gets the card list only
        self.portal(c, TOWN, PUPU_MAP, mobs=8)
        self.assertEqual(self.resync(c), [])
        self.portal(c, PUPU_MAP, TOWN)
        self.assertEqual(self.resync(c), [])
        # relog within EVENT_RELOGIN_QUIET_SECS: nothing again (a new connection prints the channel)
        self.mono.t += 60
        c = self.relog(c)
        self.assertEqual(self.resync(c, first=True), [])
        self.assertEqual(self.bag().count(LOVE_POTION), 5)
        # a relog after the quiet window: the line and the popup again, never the gift
        self.mono.t += 1800
        c = self.relog(c)
        self.check_tail(c, self.resync(c, 0x15, 0x80, first=True), gift=False)
        self.assertEqual(self.bag().count(LOVE_POTION), 5)

    def test_the_announcement_repeats_every_n_minutes(self):
        c = self.enter()
        self.check_tail(c, self.resync(c, 0x15, 0x99, 0x80, first=True))
        self.assertEqual(self.server.events.tick(), 0)
        self.mono.t += 30 * 60 - 1
        self.assertEqual(self.server.events.tick(), 0)
        c.expect_silence(0.2)
        self.mono.t += 1
        self.assertEqual(self.server.events.tick(), 1)
        self.check_tail(c, [c.expect(0x15)], gift=False, popup=False)
        self.assertEqual(self.server.events.tick(), 0)                   # not again at once

    def test_a_popup_that_did_not_go_out_stays_due(self):
        """P13 review: the popup ledger was written before the send, so a 0x80 that never
        went out still kept the Event News away for EVENT_RELOGIN_QUIET_SECS. The ledger is
        written only once the window went out."""
        c = self.enter()
        key = (E.Events._char_key(c.session), 'p13-exit', 'popup')
        with mock.patch.object(E, 'open_window', return_value=False):
            self.check_tail(c, self.resync(c, 0x15, 0x99, first=True), popup=False)
        self.assertNotIn(key, self.server.events.ledger)
        # a relog inside the quiet window: the line stays quiet, the gift is claimed - and the
        # popup that never showed comes now
        self.mono.t += 60
        c = self.relog(c)
        self.check_tail(c, self.resync(c, 0x80, first=True), gift=False, announce=False)
        self.assertEqual(self.server.events.ledger[key], self.mono.t)

    def test_the_map_load_hook_waits_for_an_effect_under_way(self):
        """P13 review: the tick checked events_ready and then granted with no lock, so a map
        load could begin in between. Effects run under the session's combat lock with the
        readiness re-checked inside it, and the before_server_map_load hook clears the flag
        under the same lock: it waits for an effect under way."""
        c = self.enter()
        self.check_tail(c, self.resync(c, 0x15, 0x99, 0x80, first=True))
        done = threading.Event()

        def hook():
            self.server.events._before_map_load(self.server, c.session)
            done.set()
        with self.server._combat_lock(c.session):                       # "the tick's effect"
            t = threading.Thread(target=hook, daemon=True)
            t.start()
            self.assertFalse(done.wait(0.2))
            self.assertTrue(c.session['events_ready'])
        self.assertTrue(done.wait(5.0))
        t.join(5.0)
        self.assertFalse(c.session['events_ready'])
        # a gift tried now re-checks under the lock: not ready, nothing granted, nothing tried
        self.server.events.reset(c.session)
        self.assertEqual(self.server.events._gift(c.session, self.server.events.events[0]), 0)
        self.assertNotIn('events_gift_tried', c.session)
        c.expect_silence(0.2)

    def test_a_gift_that_races_the_portal_is_in_its_0x03(self):
        """P13 review: _map_transfer builds its 0x03 BEFORE the map-load hook clears
        events_ready, so the tick could grant the login gift in between and the 0x03 built
        from the old bag would miss it on a client that just got 0x99 sub 9. The grant
        counter (events.grants) makes the transfer rebuild it; the tick's packets are queued
        before the lead."""
        ev = self.server.events
        ev.events = [dataclasses.replace(ev.events[0], enabled=False)]    # starts mid-session
        c = self.enter()
        self.assertEqual(self.resync(c, first=True), [])
        with ev.lock:
            ev.overrides['p13-exit'] = (True, None)
        build = self.server._build_opcode_03
        raced = []

        def build_then_tick(session, *args, **kw):
            body = build(session, *args, **kw)
            if session is c.session and not raced:
                raced.append(body)
                self.assertEqual(ev.tick(), 3)                          # line, gift, popup
            return body

        before = E.grants(c.session)
        with mock.patch.object(self.server, '_build_opcode_03', side_effect=build_then_tick), \
                self.assertLogs('WS', logging.INFO) as logs:
            c.send_c2s(self.keys['portal'], {'portal_line_index': self.portal_index(TOWN, PUPU_MAP)})
            pkts = c.expect(0x15, 0x99, 0x80, 0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(8))
            self.assertTrue(c.wait_session(lambda s: s.get('in_world') and s.get('current_map') == PUPU_MAP))
        self.assertEqual(len(raced), 1)
        self.assertEqual(E.grants(c.session), before + 1)
        self.assertTrue(any('an event grant landed' in line for line in logs.output))
        self.check_tail(c, pkts[:3])
        self.assertNotEqual(pkts[4].payload, raced[0])                  # the stale build was replaced
        self.assertEqual(bag_rows(c.s2c(raced_packet(raced[0]), allow_trailing=True)), [])
        self.assertEqual(bag_rows(c.s2c(pkts[4], allow_trailing=True)), [(LOVE_POTION, 5)])
        self.assertEqual(self.bag().count(LOVE_POTION), 5)

    def test_the_tick_waits_for_the_resync_of_the_latest_map_load(self):
        c = self.enter()
        self.assertEqual(self.server.events.tick(), 0)                   # no 0x63 yet: nothing
        c.expect_silence(0.2)
        self.check_tail(c, self.resync(c, 0x15, 0x99, 0x80, first=True))
        c.send_c2s(self.keys['portal'], {'portal_line_index': self.portal_index(TOWN, PUPU_MAP)})
        c.expect(0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(8))
        self.assertTrue(c.wait_session(lambda s: s.get('current_map') == PUPU_MAP and s.get('in_world')))
        self.assertFalse(c.session['events_ready'])                       # cleared by the map load
        self.mono.t += 30 * 60
        self.assertEqual(self.server.events.tick(), 0)                   # due, but no 0x63 yet
        self.assertEqual(self.resync(c), [])
        self.assertEqual(self.server.events.tick(), 1)
        c.expect(0x15)

    def test_a_full_bag_keeps_the_claim_open_for_the_next_login(self):
        with self.server.store.lock:
            bag = self.bag()
            bag.set_capacity('consume', INV.MIN_CAPACITY)                  # one slot, a Herb in it
            self.assertIsNotNone(bag.add(HERB, 1))
        c = self.enter()
        with self.assertLogs('WS', logging.WARNING) as logs:
            self.check_tail(c, self.resync(c, 0x15, 0x80, first=True), gift=False)
        self.assertTrue(any('claim stays open' in line for line in logs.output))
        self.assertEqual((self.bag().count(LOVE_POTION), self.char()[E.CLAIMS_KEY]), (0, {}))
        self.assertEqual(self.server.events.tick(), 0)                   # tried once per connection
        with self.server.store.lock:
            self.bag().set_capacity('consume', INV.MAX_CAPACITY)
        c = self.relog(c)
        self.check_tail(c, self.resync(c, 0x99, first=True), announce=False, popup=False)
        self.assertEqual(self.bag().count(LOVE_POTION), 5)
        self.assertIn('p13-exit', self.disk_char()[E.CLAIMS_KEY])

    def test_kill_exp_doubles_after_the_card_bonus_and_returns_when_the_event_ends(self):
        c = self.enter('hunter', mobs=8)
        base = EC.npcs().get(1).exp
        self.assertEqual(self.kill_exp(c, 0), 2 * base)                   # exit criterion 3: +20
        ev = self.server.events
        ev.dev_event(c.session, 'stop p13-exit')
        self.assertIn('off (GM)', self.replies(c)[0])
        self.assertEqual(self.kill_exp(c, 1), base)                       # after it: +10
        ev.dev_event(c.session, 'auto p13-exit')
        c.expect(0x15)
        with self.server.store.lock:
            self.bag('hunter').add(PUPU_CARD, 1)
        c.send_c2s(self.keys['register'], {'card_item_id': PUPU_CARD})
        self.assertEqual(c.s2c(c.expect(0x8B))['result'], 1)
        card = base + max(1, base // 10)
        self.assertEqual(self.kill_exp(c, 2), 2 * card)                   # card first, then x2 (11 -> 22)
        ev.dev_expmult(c.session, '3')
        self.assertEqual(self.replies(c), ['EXP x3 (!expmult).'])
        self.assertEqual(self.kill_exp(c, 3), 3 * card)
        ev.dev_expmult(c.session, 'off')
        self.assertEqual(self.replies(c), ['EXP x2 (event p13-exit).'])

    def test_party_quest_and_mentor_sources(self):
        c = self.enter()
        self.resync(c, 0x15, 0x99, 0x80, first=True)
        srv = self.server
        self.assertEqual(srv.award_exp(c.session, 7, 'party'), 14)
        self.assertEqual(struct.unpack('<i', c.expect(0x21).payload)[0], 14)
        self.assertEqual(srv.award_exp(c.session, 50, 'quest'), 50)          # EVENT_EXP_QUESTS off
        c.expect(0x21)
        srv.config['EVENT_EXP_QUESTS'] = True
        self.assertEqual(srv.award_exp(c.session, 50, 'quest'), 100)
        c.expect(0x21)
        self.assertEqual(srv.award_exp(c.session, 3, 'mentor', menti_id=2), 3)   # a share of scaled exp
        self.assertEqual(c.s2c(c.expect(0x7F)), {'exp_delta': 3, 'menti_id': 2})
        # GM exp is not an earned grant: never scaled
        self.chat(c, b'!exp +10')
        pkts = c.expect(0x21, 0x15)
        self.assertEqual(struct.unpack('<i', pkts[0].payload)[0], 10)

    def test_only_the_latest_map_loads_0x63_re_arms(self):
        """P13 post-merge review: the C2S 0x63 of an earlier map load, handled after the next
        load's hook already cleared events_ready (a transfer from another thread), leaves the
        effects off until that load's own 0x63; a stray 0x63 banks nothing."""
        c = self.enter()
        self.check_tail(c, self.resync(c, 0x15, 0x99, 0x80, first=True))
        srv, ev = self.server, self.server.events
        self.assertEqual((c.session[E.LOADS_KEY], c.session[E.RESYNCS_KEY]), (1, 1))
        self.assertEqual(self.resync(c), [])                              # a stray 0x63: capped
        self.assertEqual(c.session[E.RESYNCS_KEY], 1)
        ev.suspend(c.session, load=True)                                  # load A's hook
        ev.suspend(c.session, load=True)                                  # load B's, another thread
        with self.assertLogs('WS', logging.INFO) as logs:
            self.assertEqual(self.resync(c), [])                          # A's 0x63
        self.assertTrue(any('an earlier map load (2 of 3)' in line for line in logs.output))
        self.assertFalse(c.session['events_ready'])
        self.mono.t += 30 * 60
        self.assertEqual(ev.tick(), 0)                                    # due, but B's 0x63 is not in
        self.assertEqual(self.resync(c), [])                              # B's 0x63
        self.assertTrue(c.session['events_ready'])
        self.assertEqual(ev.tick(), 1)
        c.expect(0x15)
        # the mall entry clears the flag without counting a load (it sends no 0x03)
        ev.suspend(c.session)
        self.assertEqual((c.session[E.LOADS_KEY], c.session['events_ready']), (3, False))
        self.assertEqual(self.resync(c), [])
        self.assertTrue(c.session['events_ready'])

    def test_the_mall_entry_suspends_the_event_effects(self):
        """P8 x P13 merge: the mall entry leaves the map without a map load. It clears
        events_ready under the combat lock (Events.suspend, the map-load hook's body), so an
        effect the tick is delivering finishes before the mall's 0x6F / 0x6A, and none starts
        again before the exit's map load's C2S 0x63."""
        c = self.enter()
        self.check_tail(c, self.resync(c, 0x15, 0x99, 0x80, first=True))
        srv = self.server
        entered = threading.Event()
        with srv._combat_lock(c.session):                  # an event effect in flight holds it
            t = threading.Thread(target=lambda: (srv.mall.enter(c.session), entered.set()), daemon=True)
            t.start()
            self.assertFalse(entered.wait(0.3))
            self.assertTrue(c.session['events_ready'])
        t.join(5)
        self.assertTrue(entered.is_set())
        self.assertFalse(c.session['events_ready'])
        self.assertTrue(c.session['in_cash_shop'])
        c.expect(0x6A)
        self.mono.t += 30 * 60
        self.assertEqual(srv.events.tick(), 0)             # due, but in the mall
        c.expect_silence(0.2)

    def test_the_cash_exp_item_stage_comes_before_the_event(self):
        """P8 x P13 merge: the EXP item (cashuse.boost_exp) is award_exp's stage 2, per
        receiver, for kill and party exp only; the event multiplier (stage 3) scales its
        result. Guild points still come from the pre-multiplier amount (X9)."""
        import cash as CASH
        import datetime
        c = self.enter()
        self.resync(c, 0x15, 0x99, 0x80, first=True)
        srv = self.server
        char = srv._session_char(c.session)
        rec = srv.cash.grant(char, 3321, what='test')                 # +50% EXP / 7 days
        with srv.store.lock:
            rec['expire'] = CASH.format_expire(CASH.local_now() + datetime.timedelta(days=7))
        seen = []
        real = srv.grant_exp
        with mock.patch.object(srv, 'grant_exp', side_effect=lambda s, d, **kw: seen.append(kw) or real(s, d, **kw)):
            self.assertEqual(srv.award_exp(c.session, 10, 'kill'), 30)       # 10 -> 15 -> x2
            self.assertEqual(struct.unpack('<i', c.expect(0x21).payload)[0], 30)
            self.assertEqual(srv.award_exp(c.session, 7, 'party'), 20)       # 7 -> 10 -> x2
            c.expect(0x21)
        self.assertEqual([kw['guild_base'] for kw in seen], [10, 7])
        srv.config['EVENT_EXP_QUESTS'] = True
        self.assertEqual(srv.award_exp(c.session, 50, 'quest'), 100)         # no item stage
        c.expect(0x21)
        self.assertEqual(srv.award_exp(c.session, 3, 'mentor', menti_id=2), 3)
        c.expect(0x7F)
        srv.events.dev_event(c.session, 'stop p13-exit')
        self.replies(c)
        self.assertEqual(srv.award_exp(c.session, 10, 'kill'), 15)           # the item alone

    def test_the_gm_commands(self):
        c = self.enter()
        self.resync(c, 0x15, 0x99, 0x80, first=True)
        self.chat(c, b'!event')
        lines = self.replies(c, 3)
        self.assertEqual(lines[0], 'EXP x2 (event p13-exit), 1 event(s) in the schedule')
        self.assertEqual(lines[1], 'p13-exit: ACTIVE x2 notice/30m gift 1282x5 popup')
        self.assertEqual(lines[2], '  - .. -')
        for line in lines:
            self.assertLessEqual(len(line), 80)
        self.chat(c, b'!event start nope')
        self.assertIn('no event', self.replies(c)[0])
        self.chat(c, b'!event popup')
        self.assertEqual(bytes([0x80]) + c.expect(0x80).payload, EVENT_NEWS_BYTES)
        self.chat(c, b'!event reset')
        self.assertIn('1 gift claim(s)', self.replies(c)[0])
        self.assertEqual(self.char()[E.CLAIMS_KEY], {})
        self.assertEqual(self.server.events.tick(), 3)                    # line, gift, popup again
        self.check_tail(c, c.expect(0x15, 0x99, 0x80))
        self.assertEqual(self.bag().count(LOVE_POTION), 10)
        self.chat(c, b'!expmult x')
        self.assertIn('not a number', self.replies(c)[0])
        self.chat(c, b'!expmult 0')
        self.assertIn('above 0', self.replies(c)[0])
        self.chat(c, b'!help event')
        self.assertTrue(self.replies(c)[0].startswith('!event [list|start'))

    def test_an_event_started_mid_session_reaches_players_on_the_next_tick(self):
        ev = self.server.events
        ev.events = [dataclasses.replace(ev.events[0], enabled=False)]    # as shipped: GM-started only
        c = self.enter()
        self.assertEqual(self.resync(c, first=True), [])
        self.chat(c, b'!event start p13-exit 10')
        self.assertIn('ACTIVE (GM until', self.replies(c)[0])
        self.assertEqual(ev.tick(), 3)
        self.check_tail(c, c.expect(0x15, 0x99, 0x80))
        self.assertEqual(ev.tick(), 0)
        ev.clock = FakeClock(ev.clock() + 601)                             # the 10 minutes ran out
        self.assertEqual((ev.active(), ev.exp_multiplier()), ([], (1.0, None)))
        self.assertEqual(ev.state(ev.events[0]), 'disabled')


@needs_2008
class NoEvent2008(NoEventFlow, Rig):
    build = B8


@needs_2009
class NoEvent2009(NoEventFlow, Rig):
    build = B9


@needs_2008
class Events2008(EventFlow, Rig):
    build = B8

    def test_the_password_gate_still_refuses_the_event_window(self):
        # C2S 0x51 is the 2008 password dialog's request; 0x3FB is not a password window.
        c = self.enter()
        c.send_c2s(self.keys['password'], {'password': 'test', 'target_window_id': E.WINDOW_EVENT_NEWS})
        self.assertEqual(c.expect(0x80).payload, b'\x00')


@needs_2009
class Events2009(EventFlow, Rig):
    build = B9

    def test_two_clients_each_get_the_effects_exactly_once(self):
        """P13 exit criterion 2 in the 2009 rig: A (TestHero) and B (Bob) log in."""
        a, b = self.enter(), self.enter('admin')
        for c, user in ((a, 'test'), (b, 'admin')):
            self.check_tail(c, self.resync(c, 0x15, 0x99, 0x80, first=True))
            self.assertEqual(self.bag(user).count(LOVE_POTION), 5)
        self.assertEqual(self.server.events.tick(), 0)
        for c in (a, b):
            c.expect_silence(0.2)
        b = self.relog(b, 'admin')
        self.assertEqual(self.resync(b, first=True), [])
        self.assertEqual(self.bag('admin').count(LOVE_POTION), 5)


# ============================================================ source audit ===
class Pipeline(unittest.TestCase):
    """arch09-exp-pipeline: every earned grant goes through award_exp; only the GM commands
    and the death penalty call grant_exp directly."""

    def test_grant_exp_callers(self):
        callers = set()
        for module in ('windslayer_server.py', 'messenger.py', 'party.py', 'quests.py', 'combat.py',
                       'events.py'):
            fn = None
            with open(os.path.join(HERE, module), encoding='utf-8') as f:
                for line in f:
                    m = re.match(r'\s*def (\w+)', line)
                    if m:
                        fn = m.group(1)
                    code = line.split('#', 1)[0]
                    if re.search(r'\.grant_exp\(', code):
                        callers.add((module, fn))
        self.assertEqual(callers, {('windslayer_server.py', '_dev_level'), ('windslayer_server.py', '_dev_exp'),
                                   ('windslayer_server.py', '_player_death'),
                                   ('windslayer_server.py', 'award_exp')})

    def test_the_exp_item_is_a_stage_of_award_exp(self):
        """P8's EXP items raise exp only inside award_exp (its stage 2), never at a call site."""
        callers = set()
        for module in ('windslayer_server.py', 'messenger.py', 'party.py', 'events.py', 'bosses.py',
                       'mall.py', 'cashuse.py', 'cash.py'):
            fn = None
            with open(os.path.join(HERE, module), encoding='utf-8') as f:
                for line in f:
                    m = re.match(r'\s*def (\w+)', line)
                    if m:
                        fn = m.group(1)
                    code = line.split('#', 1)[0]
                    if fn is not None and re.search(r'\.boost_exp\(', code):     # not the API list
                        callers.add((module, fn))                                 # of cashuse's docstring
        self.assertEqual(callers, {('windslayer_server.py', 'award_exp')})

    def test_the_dev_commands_exist(self):
        for name in ('event', 'expmult'):
            cmd = gm.lookup(name, W.GameServer.DEV_COMMANDS, gm.COMMANDS)
            self.assertTrue(callable(getattr(W.GameServer, cmd.handler, None)), name)


if __name__ == '__main__':
    unittest.main()
