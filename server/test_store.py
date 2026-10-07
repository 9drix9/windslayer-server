#!/usr/bin/env python3
"""
test_store.py - offline tests for the P1 foundations modules: store.py (F4 lc-data-model),
world.py (F3/F5 online indexes), ids.py (F3 id spaces), progression.py (exp table used by
the migration), config.py (F12 arch-config), names.py (lc-create name rules) and auth.py
(lc-login-errors password hashing).

Every file lives in a temp dir; the live server/accounts.json and server/config.json are
never opened (checked in tearDownModule). One test runs a subprocess that saves in a loop
and kills it mid-write (P1 exit criterion 8); it binds no port and imports only store.py.
"""
import copy
import hashlib
import json
import logging
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import auth  # noqa: E402
import config as cfgmod  # noqa: E402
import ids  # noqa: E402
import inventory as INV  # noqa: E402
import names  # noqa: E402
import progression  # noqa: E402
import quests as Q  # noqa: E402
import store as S  # noqa: E402
import ticks  # noqa: E402
from world import World  # noqa: E402

logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_FILES = [os.path.join(HERE, n) for n in ('accounts.json', 'config.json')]

# The accounts.json shape the server wrote before P1 (the live file's records, abridged:
# TestHero from the old default, drix/testteststestset from the old create handler which
# stored look slot 10 as `class`).
LEGACY = {
    'test': {'password': 'test', 'characters': [
        {'name': 'TestHero', 'level': 1, 'class': 0, 'map': 0, 'x': 100, 'y': 100, 'hp': 100, 'mp': 50},
        {'name': 'drix', 'class': 1, 'level': 1, 'map': 0, 'x': 100, 'y': 100, 'hp': 100, 'mp': 50,
         'face': 1, 'top': 2, 'bottom': 2, 'shoes': 2, 'str': 3, 'dex': 2, 'int': 1, 'spr': 3},
        {'name': 'testteststestset', 'class': 2, 'level': 1, 'map': 0, 'x': 100, 'y': 100, 'hp': 100,
         'mp': 50, 'face': 1, 'top': 5, 'bottom': 5, 'shoes': 3, 'str': 3, 'dex': 2, 'int': 2, 'spr': 2},
    ]},
    'admin': {'password': 'admin', 'characters': [
        {'name': 'test', 'class': 1, 'level': 3, 'map': 0, 'x': 100, 'y': 100, 'hp': 100, 'mp': 50,
         'face': 1, 'top': 2, 'bottom': 2, 'shoes': 2, 'str': 3, 'dex': 2, 'int': 2, 'spr': 2},
    ]},
}


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


_LIVE = {}


def setUpModule():
    for p in LIVE_FILES:
        if os.path.exists(p):
            _LIVE[p] = (_sha(p), os.path.getmtime(p))


def tearDownModule():
    for p, (digest, mtime) in _LIVE.items():
        assert _sha(p) == digest and os.path.getmtime(p) == mtime, f'{p} changed during test_store.py'


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_store_')
        self.path = os.path.join(self.tmp, 'accounts.json')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, data, raw=None):
        with open(self.path, 'wb') as f:
            f.write(raw if raw is not None else json.dumps(data, indent=2).encode())

    def read(self):
        with open(self.path, encoding='utf-8') as f:
            return json.load(f)

    def leftovers(self):
        return [n for n in os.listdir(self.tmp) if n.endswith('.tmp')]


# ------------------------------------------------------------------ progression ---
class Progression(unittest.TestCase):
    def test_table_matches_the_exe_values(self):
        # login_character.md 1.7 / lc-exp-table unit vectors (exe VA 0x6F0C28, sum 1,200,382,231)
        self.assertEqual(len(progression.EXP_INC), 99)
        self.assertEqual(progression.exp_for_level(1), 0)
        self.assertEqual(progression.exp_for_level(2), 56)
        self.assertEqual(progression.exp_for_level(3), 177)
        self.assertEqual(progression.exp_for_level(4), 415)
        self.assertEqual(progression.exp_for_level(5), 840)
        self.assertEqual(progression.exp_for_level(10), 10260)
        self.assertEqual(progression.exp_for_level(13), 30485)
        self.assertEqual(progression.exp_for_level(99), 1_200_382_230)
        self.assertEqual(progression.EXP_MAX, 1_200_382_230)

    def test_level_for_exp_boundaries(self):
        for exp, level in ((0, 1), (55, 1), (56, 2), (176, 2), (177, 3), (30000, 12), (30485, 13),
                           (progression.EXP_MAX - 1, 98), (progression.EXP_MAX, 99),
                           (progression.EXP_MAX + 5, 99), (-10, 1)):
            with self.subTest(exp=exp):
                self.assertEqual(progression.level_for_exp(exp), level)
        for level in range(1, 100):
            self.assertEqual(progression.level_for_exp(progression.exp_for_level(level)), level)

    def test_stat_total(self):
        self.assertEqual([progression.stat_total(n) for n in (0, 1, 2, 29, 30, 99, 100)],
                         [0, 9, 13, 121, 126, 471, 0])


# -------------------------------------------------------------------------- ids ---
class IdSpaces(unittest.TestCase):
    def test_server_spaces_do_not_overlap(self):
        server = [s for s in (ids.PLAYER, ids.MONSTER, ids.SELECT_SCREEN, ids.MAP_NPC)]
        for a in server:
            for b in server:
                if a is not b:
                    self.assertTrue(a.hi < b.lo or b.hi < a.lo, f'{a.name} overlaps {b.name}')
        self.assertEqual((ids.PLAYER.lo, ids.PLAYER.hi), (1, 0x000EFFFF))
        self.assertEqual((ids.MONSTER.lo, ids.MONSTER.hi), (0x000F0000, 0x001FFFFF))
        self.assertIs(ids.space_of(1), ids.PLAYER)
        self.assertIs(ids.space_of(0xF0003), ids.MONSTER)
        self.assertIs(ids.space_of(30_000_002), ids.SELECT_SCREEN)
        self.assertIsNone(ids.space_of(0))
        self.assertFalse(ids.is_player_uid(0x1000 + 0xF0000))

    def test_next_player_uid_is_max_plus_one_with_fixed_uids_reserved(self):
        self.assertEqual(ids.next_player_uid([]), 3)                # 1 test, 2 admin reserved
        self.assertEqual(ids.next_player_uid([1, 2, 7]), 8)
        self.assertEqual(ids.next_player_uid([1, 2, None, 'x', 0xF0000]), 3)
        self.assertEqual(ids.next_player_uid([1, 2, ids.PLAYER.hi]), 3)   # wraps to the lowest free

    def test_rooms_skip_the_cash_shop_marker(self):
        self.assertEqual(ids.next_room_number([]), 1)
        self.assertEqual(ids.next_room_number({1, 2, 4}), 3)
        self.assertEqual(ids.next_room_number(set(range(1, 128))), 128)
        with self.assertRaises(ids.IdSpaceExhausted):
            ids.next_room_number(set(range(1, 129)))
        self.assertNotIn(0x81, ids.ROOM)                        # the marker can never be a room
        self.assertIn(0x81, ids.ROOM_SKIP)

    def test_counter_never_zero_wraps_and_skips_held(self):
        c = ids.Counter(ids.MESSENGER_ROOM, start=0xFFFFFFFF)
        self.assertEqual([c.next(), c.next()], [0xFFFFFFFF, 1])
        held = {3, 4}
        c = ids.Counter(ids.CASH_SERIAL, in_use=lambda: held)
        self.assertEqual(c.next(), 0x1000)
        c2 = ids.Counter(ids.ROOM, start=3, in_use=lambda: held)
        self.assertEqual(c2.next(), 5)
        with self.assertRaises(ValueError):
            ids.Counter(ids.MAP_NPC)


# ----------------------------------------------------------------------- config ---
class Config(TempDir):
    def test_defaults(self):
        cfg = cfgmod.defaults()
        self.assertEqual((cfg.VERSION_PORT, cfg.GAME_PORT, cfg.ADMIN_PORT, cfg.UDP_ROOM_PORT_BASE),
                         (7011, 7022, 7099, 10000))
        self.assertEqual(cfg.PUBLIC_IP, '127.0.0.1')
        self.assertEqual(cfg.public_ip_host_order(), 0x7F000001)
        self.assertEqual(cfg.channels(), [(1, 'Channel 1', '127.0.0.1')])
        self.assertEqual((cfg.START_MAP, cfg.START_X, cfg.START_Y), (101, 700.0, 812.0))
        self.assertEqual(cfg.SAVE_DEBOUNCE_SECS, 2.0)

    def test_shipped_config_json_equals_the_defaults(self):
        with open(os.path.join(HERE, 'config.json'), encoding='utf-8') as f:
            shipped = {k: v for k, v in json.load(f).items() if not k.startswith('_')}
        self.assertEqual(dict(cfgmod.from_dict(shipped)), dict(cfgmod.defaults()))

    def test_load_file_missing_and_relative_accounts_path(self):
        p = os.path.join(self.tmp, 'config.json')
        cfg = cfgmod.load(p)                                   # missing: defaults
        self.assertEqual(cfg.GAME_PORT, 7022)
        with open(p, 'w') as f:
            json.dump({'_comment': 'x', 'PUBLIC_IP': '10.0.0.5', 'SAVE_DEBOUNCE_SECS': 1,
                       'ACCOUNTS_FILE': 'db/acc.json',
                       'CHANNELS': [{'no': 1}, {'no': 3, 'name': 'PvP', 'ip': '10.0.0.6'}]}, f)
        cfg = cfgmod.load(p)
        self.assertEqual(cfg.public_ip_host_order(), 0x0A000005)
        self.assertEqual(cfg.SAVE_DEBOUNCE_SECS, 1.0)
        self.assertEqual(cfg.accounts_path, os.path.join(self.tmp, 'db', 'acc.json'))
        self.assertEqual(cfg.channels(), [(1, 'Channel 1', '10.0.0.5'), (3, 'PvP', '10.0.0.6')])

    def test_bad_values_are_refused(self):
        bad = [{'GAME_PORT': '7022'}, {'GAME_PORT': 0}, {'DEV_MEMORY_COMBAT': 1}, {'PUBLIC_IP': 'localhost'},
               {'CHANNELS': []}, {'CHANNELS': [{'no': 2}, {'no': 1}]}, {'CHANNELS': [{'no': i} for i in range(1, 12)]},
               {'SAVE_DEBOUNCE_SECS': 0}, {'START_MAP': 70000}]
        for overrides in bad:
            with self.subTest(overrides), self.assertRaises(cfgmod.ConfigError):
                cfgmod.from_dict(overrides)
        with self.assertLogs('WS', logging.WARNING):
            cfgmod.from_dict({'NOPE': 1})
        with self.assertLogs('WS', logging.WARNING):
            self.assertEqual(cfgmod.from_dict({'GAME_PORT': 7023}).GAME_PORT, 7023)
        p = os.path.join(self.tmp, 'broken.json')
        with open(p, 'w') as f:
            f.write('{"GAME_PORT": ')
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.load(p)

    def test_version_reply_is_byte_identical_with_the_default_config(self):
        import fakeclient
        W = fakeclient.import_server()
        # The live-proven hand-built body before arch-config (1 channel, 127.0.0.1, count 0).
        msg = b'Press start button to start the game.\n\n\xA9 2009 OUTSPARK.com. All rights reserved.'
        old = (b'\x01' + struct.pack('<H', 3) + struct.pack('<H', len(msg)) + msg
               + bytes([1, 3, 1, 1, 1]) + struct.pack('<H', 0) + struct.pack('<I', 0x7F000001))
        self.assertEqual(W.VersionServer(config=cfgmod.defaults())._build_version_body(), old)
        two = cfgmod.from_dict({'PUBLIC_IP': '192.168.1.20', 'CHANNELS': [{'no': 1}, {'no': 2, 'ip': '192.168.1.21'}]})
        body = W.VersionServer(config=two)._build_version_body()
        tail = body[1 + 2 + 2 + len(msg):]
        self.assertEqual(tail, bytes([1, 3, 2, 2]) + bytes([1]) + struct.pack('<HI', 0, 0xC0A80114)
                         + bytes([2]) + struct.pack('<HI', 0, 0xC0A80115))


# ------------------------------------------------------------------- migration ---
class Migration(TempDir):
    def load(self, **kw):
        return S.Store(self.path, **kw).load()

    def test_one_shot_migration_matches_f4(self):
        self.write(LEGACY)
        st = self.load()
        test, admin = st.accounts['test'], st.accounts['admin']
        self.assertEqual((test['uid'], admin['uid']), (1, 2))
        self.assertEqual((test['gender'], test['manner']), (0, 0))
        hero, drix, tall = test['characters']
        # TestHero had no look keys: the gender-0 creation defaults
        self.assertEqual(hero['look'], [0, 1, 0, 0, 1, 2, 2, 0, 0, 2, 1, 0, 0, 1])
        # [0, face, 0, 0, class, bottom, top, 0, 0, shoes, class, 0, 0, class]
        self.assertEqual(drix['look'], [0, 1, 0, 0, 1, 2, 2, 0, 0, 2, 1, 0, 0, 1])
        self.assertEqual(tall['look'], [0, 1, 0, 0, 2, 5, 5, 0, 0, 3, 2, 0, 0, 2])
        for ch in (hero, drix, tall):
            with self.subTest(ch['name']):
                self.assertEqual((ch['class'], ch['job2'], ch['exp'], ch['fame'], ch['created_at']), (0, 0, 0, 0, 0))
                self.assertNotIn('level', ch)
        self.assertEqual((hero['str'], hero['dex'], hero['int'], hero['spr']), (3, 2, 1, 3))   # defaults
        self.assertEqual((tall['str'], tall['dex'], tall['int'], tall['spr']), (3, 2, 2, 2))   # kept
        lv3 = admin['characters'][0]
        self.assertEqual((lv3['exp'], progression.level_for_exp(lv3['exp'])), (177, 3))
        # legacy render keys stay for the builders that still read them
        self.assertEqual((drix['face'], drix['top'], drix['bottom'], drix['shoes']), (1, 2, 2, 2))
        # world-persistence: a record written before the start point was configurable sits
        # on map 0 at (100,100), which is not a map the client can load, so the migration
        # moves it to the configured start point (F4 migration bullet 5). HP/MP are kept.
        self.assertEqual((drix['map'], drix['x'], drix['y'], drix['hp'], drix['mp']),
                         (101, 700.0, 812.0, 100, 50))
        # P2 schema: wallet, the three bag tabs, the equip grid and the quest log
        for ch in (hero, drix, tall):
            with self.subTest(ch['name']):
                self.assertEqual((ch['gold'], ch['victy']), (S.DEFAULT_GOLD, S.DEFAULT_VICTY))
                self.assertEqual(ch['inventory'], {'equip': [], 'consume': {}, 'etc': {},
                                                   'tab_slots': [35, 35, 35]})
                self.assertEqual(ch['equipped'], {})
                self.assertEqual(ch['quests'], {'active': [0, 0, 0], 'progress': [0, 0, 0],
                                                'completed': []})
                self.assertEqual(ch['card_deck'], [])
        self.assertEqual(self.read(), st.accounts)             # written back at once

    def test_idempotent_and_backup_written_once(self):
        self.write(LEGACY)
        with open(self.path, 'rb') as f:
            original = f.read()
        st = self.load()
        self.assertTrue(st.migration_changes)
        with open(st.backup_path, 'rb') as f:
            self.assertEqual(f.read(), original)               # the pre-migration bytes
        self.assertEqual(st.backup_path, self.path + '.bak-pre-p1')
        migrated = _sha(self.path)
        backup_mtime = os.path.getmtime(st.backup_path)

        again = self.load()
        self.assertEqual(again.migration_changes, [])
        self.assertEqual(again.saves, 0)                        # nothing to write
        self.assertEqual(_sha(self.path), migrated)
        self.assertEqual(again.accounts, st.accounts)
        twice = copy.deepcopy(st.accounts)
        self.assertEqual(S.migrate_accounts(twice), [])
        self.assertEqual(twice, st.accounts)

        # a later legacy record (e.g. written by an old server build) migrates, and the
        # existing backup is never overwritten
        data = self.read()
        data['admin']['characters'].append({'name': 'Old', 'class': 3, 'level': 2, 'map': 0, 'x': 1, 'y': 1})
        self.write(data)
        third = self.load()
        self.assertEqual(third.accounts['admin']['characters'][1]['look'][10], 3)
        self.assertEqual(third.accounts['admin']['characters'][1]['exp'], 56)
        self.assertEqual(os.path.getmtime(third.backup_path), backup_mtime)
        with open(third.backup_path, 'rb') as f:
            self.assertEqual(f.read(), original)

    def test_a_malformed_bag_or_quest_log_is_reported_repaired_and_written_back(self):
        """The change list is what decides that the file is rewritten and that the
        pre-migration bytes are backed up. `before` used to hold the very objects the
        normalizers repair IN PLACE, so old != new could never be true: a corrupt record
        was fixed in memory only, with no change line, no save and no .bak-pre-p2."""
        data = copy.deepcopy(LEGACY)
        hero = data['test']['characters'][0]
        hero['inventory'] = {'equip': [{'id': 179}], 'consume': {'5': '12'}, 'tab_slots': [99, 0]}
        hero['quests'] = {'active': [26], 'progress': [7, 7, 7], 'completed': [26, [26, 4], 0]}
        hero['card_deck'] = [2030, 2030, 0]
        self.write(data)
        st = self.load()
        for field in ('inventory', 'quests', 'card_deck'):
            self.assertIn(f'test/TestHero: {field} normalized', st.migration_changes)
        self.assertIn('test/TestHero: equipped created', st.migration_changes)
        hero = st.accounts['test']['characters'][0]
        self.assertEqual(hero['inventory'], {'equip': [{'id': 179, 'w': [0] * 6}],
                                             'consume': {5: 12}, 'etc': {},
                                             'tab_slots': [45, 1, 35]})     # clamped to 1..45
        self.assertEqual(hero['quests'], {'active': [26, 0, 0], 'progress': [7, 0, 0],
                                          'completed': [[26, 1]]})          # empty slots lose progress
        self.assertEqual(hero['card_deck'], [2030])
        self.assertEqual(self.read(), json.loads(json.dumps(st.accounts)))   # saved, not just repaired
        self.assertTrue(os.path.exists(self.path + '.bak-pre-p2'))

        # and a record that already conforms is not reported again: the stacks come back
        # from the file keyed by string and that round trip is not a repair (_json_shape).
        again = self.load()
        self.assertEqual(again.migration_changes, [])
        self.assertEqual(again.saves, 0)

    def test_passwords_are_hashed_once_at_load(self):
        self.write(LEGACY)
        st = self.load()
        stored = {u: acc['password'] for u, acc in st.accounts.items()}
        for username, value in stored.items():
            self.assertTrue(auth.is_hashed(value), username)
        self.assertTrue(st.verify_password('test', 'test'))
        self.assertFalse(st.verify_password('test', 'admin'))
        self.assertFalse(st.verify_password('nosuch', 'test'))
        with open(self.path, encoding='utf-8') as f:
            raw = f.read()
        self.assertNotIn('"password": "test"', raw)
        # Idempotent: a second load neither re-hashes nor rewrites the file.
        again = self.load()
        self.assertEqual({u: a['password'] for u, a in again.accounts.items()}, stored)
        self.assertEqual(again.migration_changes, [])
        # HASH_PASSWORDS off leaves a plaintext file alone (and login still works).
        self.write(LEGACY)
        plain = S.Store(self.path, hash_passwords=False, backup=False).load()
        self.assertEqual(plain.accounts['test']['password'], 'test')
        self.assertTrue(plain.verify_password('test', 'test'))

    def test_already_current_file_gets_no_backup(self):
        self.write(LEGACY)
        self.load(backup=False)
        self.assertFalse(os.path.exists(self.path + S.BACKUP_SUFFIX))
        self.load()
        self.assertFalse(os.path.exists(self.path + S.BACKUP_SUFFIX))

    def test_uid_assignment_keeps_valid_uids_and_repairs_bad_ones(self):
        self.write({'zed': {'password': 'z', 'characters': []},
                    'test': {'password': 't', 'characters': []},
                    'bob': {'password': 'b', 'uid': 9, 'characters': []},
                    'eve': {'password': 'e', 'uid': 9, 'characters': []},        # duplicate
                    'mal': {'password': 'm', 'uid': 1, 'characters': []},        # test's fixed uid
                    'bad': {'password': 'x', 'uid': 'seven', 'characters': []},
                    'admin': {'password': 'a', 'characters': []}})
        st = self.load()
        uids = {u: a['uid'] for u, a in st.accounts.items()}
        self.assertEqual(uids, {'zed': 10, 'test': 1, 'bob': 9, 'eve': 11, 'mal': 12, 'bad': 13, 'admin': 2})
        self.assertEqual(self.load().migration_changes, [])

    def test_missing_file_creates_the_default_accounts(self):
        st = self.load()
        self.assertEqual({u: a['uid'] for u, a in st.accounts.items()}, {'test': 1, 'admin': 2})
        hero = st.accounts['test']['characters'][0]
        self.assertEqual((hero['name'], hero['class'], hero['exp'], hero['look'], hero['map'], hero['x'], hero['y']),
                         ('TestHero', 0, 0, [0, 1, 0, 0, 1, 2, 2, 0, 0, 2, 1, 0, 0, 1], 101, 700.0, 812.0))
        self.assertEqual(self.read(), st.accounts)
        self.assertFalse(os.path.exists(st.backup_path))
        self.assertEqual(self.load().migration_changes, [])

    def test_unreadable_file_is_never_overwritten(self):
        self.write(None, raw=b'{"test": {"password": ')
        with self.assertRaises(S.StoreError):
            self.load()
        with open(self.path, 'rb') as f:
            self.assertEqual(f.read(), b'{"test": {"password": ')
        self.write(['not', 'an', 'object'])
        with self.assertRaises(S.StoreError):
            self.load()


# ------------------------------------------------------------------------ store ---
class StoreRecords(TempDir):
    def setUp(self):
        super().setUp()
        self.write(LEGACY)
        self.st = S.Store(self.path, start_map=101, start_x=1411.0, start_y=714.0).load()

    def test_round_trip(self):
        st = self.st
        with st.lock:
            drix = st.find_character('test', 'drix')
            drix['exp'] = 60
            drix['map'], drix['x'], drix['y'] = 102, 48.0, 713.0
        st.mark_dirty('test')
        self.assertTrue(st.dirty)
        self.assertTrue(st.flush())
        self.assertFalse(st.dirty)
        self.assertFalse(st.flush())                         # nothing new
        back = S.Store(self.path).load()
        self.assertEqual(back.accounts, st.accounts)
        self.assertEqual(back.migration_changes, [])
        self.assertEqual(back.find_character('test', 'drix')['exp'], 60)
        self.assertEqual(self.leftovers(), [])

    def test_new_character_schema_and_immediate_save(self):
        st = self.st
        # ROADMAP_2009_ADDENDUM C4 / X14: the next stable character id, store-wide max + 1.
        want_cid = max(c['cid'] for acc in st.accounts.values() for c in acc['characters']) + 1
        char = st.new_character('Nova', s10=3, s1=2, s6=4, s5=5, s9=3, stats=(3, 2, 1, 3), now=1726550000)
        self.assertEqual(char, {
            'name': 'Nova', 'cid': want_cid, 'created_at': 1726550000, 'class': 0, 'job2': 0, 'exp': 0,
            'look': [0, 2, 0, 0, 3, 5, 4, 0, 0, 3, 3, 0, 0, 3], 'str': 3, 'dex': 2, 'int': 1, 'spr': 3,
            # cs-hp-mp-model: born at the client's maxima for class 0 / Lv1 / SPR 3 / INT 1
            # (hpmp.new_character_vitals), not the old flat 100/50.
            'fame': 0, 'map': 101, 'x': 1411.0, 'y': 714.0, 'hp': 140, 'mp': 87,
            # chat_mail_gm-gm-flag-manner: written as 0 so F10.0 step 1 is a 0 -> 1 edit.
            'gm': 0, 'gm_hidden': 0,
            # P2: the wallet, bag and quest log a character is born with (shop_storage-wallet,
            # item_inventory-model-persist, quest_cards_misc-quest-state-model). The shape is
            # the owners' own ensure(), so a new record equals a migrated one.
            'gold': S.DEFAULT_GOLD, 'victy': S.DEFAULT_VICTY,
            'inventory': {'equip': [], 'consume': {}, 'etc': {}, 'tab_slots': [35, 35, 35]},
            'equipped': {},
            'quests': {'active': [0, 0, 0], 'progress': [0, 0, 0], 'completed': []},
            'card_deck': [],
            # P3 (cs-skill-learn / cs-buffs): the learned list and the persisted buffs.
            'skills': [], 'buffs': [],
            # P4 (shop_storage-bank-model): the S2C 0x65 capacities and an empty bank.
            'bank_slots': [35, 35, 35], 'bank': {'equip': [], 'consume': [], 'etc': [], 'gold': 0},
            # P5 (chat_mail_gm-privacy-flags): the five refuse flags, nothing refused.
            'refuse': {'whisper': False, 'exchange': False, 'party': False, 'talk': False,
                       'friend': False},
            # P6 (social_friend-persistence): an empty messenger - 20 friend slots, no
            # mentor, no memos (memo ids start after memo_seq).
            'friends': [], 'friend_capacity': 20, 'mentor': None, 'mentees': [], 'memos': [],
            'memo_seq': 0,
            # P7 (shop_storage-stall-registry): no stall open, nothing in escrow.
            'stall_escrow': [],
            # P8 (premium_cash-wallet-model): no cash items owned.
            'cash_items': [],
            # P13 (ev-e3, events.py): no event login gift claimed yet.
            'event_gifts_claimed': {},
            # P12 (bl-1, blacklist.py): nobody blacklisted.
            'blacklist': []})
        saves = st.saves
        st.add_character('admin', char)
        self.assertEqual(st.saves, saves + 1)
        self.assertEqual([c['name'] for c in self.read()['admin']['characters']], ['test', 'Nova'])
        self.assertEqual(st.name_owner('NOVA'), 'admin')
        self.assertEqual(st.name_owner('testhero'), 'test')
        self.assertIsNone(st.name_owner('nobody'))
        self.assertEqual(S.migrate_character(copy.deepcopy(char)), [])

    def test_remove_character_keeps_order(self):
        st = self.st
        self.assertTrue(st.remove_character('test', 'drix'))
        self.assertFalse(st.remove_character('test', 'drix'))
        self.assertEqual([c['name'] for c in self.read()['test']['characters']], ['TestHero', 'testteststestset'])
        self.assertIsNone(st.name_owner('drix'))

    def test_create_account_uids(self):
        st = self.st
        self.assertEqual(st.create_account('carol', 'pw')['uid'], 3)
        self.assertEqual(st.create_account('dave', 'pw', gender=1)['uid'], 4)
        dave = self.read()['dave']
        # The password is stored hashed (lc-login-errors), never in clear.
        self.assertTrue(S.auth.verify(dave.pop('password'), 'pw'))
        self.assertEqual(dave, {'uid': 4, 'gender': 1, 'manner': 0, 'banned': False,
                                'deleted': False, 'characters': [],
                                # P6 (social_friend-persistence): the compliment / report limits
                                'social': S.social.default_account_social(),
                                # P8 (premium_cash-wallet-model): an empty wallet, box and inbox
                                'cash': 0, 'mileage': 0, 'first_purchase_done': False,
                                'first_purchase_notice': False, 'cash_box': [], 'gift_inbox': []})
        with self.assertRaises(S.StoreError):
            st.create_account('carol', 'x')
        self.assertEqual(st.uid_of('dave'), 4)
        self.assertEqual(st.account_by_uid(2)[0], 'admin')
        self.assertIsNone(st.account(None))


class AtomicSave(TempDir):
    def setUp(self):
        super().setUp()
        self.write(LEGACY)
        self.st = S.Store(self.path).load()
        with open(self.path, 'rb') as f:
            self.good = f.read()
        with self.st.lock:
            self.st.accounts['test']['characters'][0]['exp'] = 999
        self.st.mark_dirty()

    def assert_old_file_intact(self):
        with open(self.path, 'rb') as f:
            self.assertEqual(f.read(), self.good)
        json.loads(self.good)
        self.assertEqual(self.leftovers(), [])
        self.assertTrue(self.st.dirty)

    def test_exception_while_writing_the_temp_file(self):
        with mock.patch.object(S.os, 'fsync', side_effect=OSError(28, 'No space left on device')):
            with self.assertRaises(OSError):
                self.st.save_now()
            self.assert_old_file_intact()
            with self.assertLogs('WS', logging.ERROR):
                self.assertFalse(self.st.flush())             # logged, kept dirty for the next attempt
        self.assert_old_file_intact()
        self.assertTrue(self.st.flush())
        self.assertEqual(self.read()['test']['characters'][0]['exp'], 999)

    def test_exception_at_the_replace(self):
        with mock.patch.object(S.os, 'replace', side_effect=PermissionError(13, 'locked')), \
                mock.patch.object(S.time, 'sleep'):
            with self.assertRaises(PermissionError):
                self.st.save_now()
        self.assert_old_file_intact()

    def test_unserializable_value_leaves_the_file_alone(self):
        with self.st.lock:
            self.st.accounts['test']['bad'] = object()
        with self.assertRaises(TypeError):
            self.st.save_now()
        self.assert_old_file_intact()

    def test_transient_replace_failure_is_retried(self):
        real = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) == 1:
                raise PermissionError(13, 'locked by a reader')
            return real(src, dst)
        with mock.patch.object(S.os, 'replace', side_effect=flaky), mock.patch.object(S.time, 'sleep'):
            self.st.save_now()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.read()['test']['characters'][0]['exp'], 999)
        self.assertEqual(self.leftovers(), [])

    # ---- livetest bug 7: the save snapshots under the lock and writes outside it ----
    def test_replace_backs_off_exponentially_for_about_three_seconds(self):
        """A reader holding accounts.json (WinError 5) outlasted the old five tries (~0.5 s)."""
        delays = S.replace_delays()
        self.assertEqual([round(d, 6) for d in delays], [0.05, 0.1, 0.2, 0.4, 0.8, 1.45])
        self.assertAlmostEqual(sum(delays), S.REPLACE_BUDGET_SECS)
        with mock.patch.object(S.os, 'replace', side_effect=PermissionError(13, 'held by a reader')) as rep, \
                mock.patch.object(S.time, 'sleep') as slept:
            with self.assertRaises(PermissionError):
                self.st.save_now()
        self.assertEqual([c.args[0] for c in slept.call_args_list], delays)
        self.assertEqual(rep.call_count, len(delays) + 1)
        self.assert_old_file_intact()
        self.assertEqual([round(d, 6) for d in S.replace_delays(0.1, 0.25)], [0.1, 0.15])

    def test_an_older_snapshot_never_replaces_a_newer_one(self):
        st = self.st
        older = st._take_snapshot()                       # exp 999
        with st.lock:
            st.accounts['test']['characters'][0]['exp'] = 1234
        st.mark_dirty('newer')
        newer = st._take_snapshot()
        saves = st.saves
        self.assertTrue(st._write_snapshot(*newer))       # the newer one reaches the disk first
        self.assertEqual((self.read()['test']['characters'][0]['exp'], st.saves, st.dirty), (1234, saves + 1, False))
        self.assertTrue(st._write_snapshot(*older))       # dropped: a newer one is on disk
        self.assertEqual((self.read()['test']['characters'][0]['exp'], st.saves, st.dirty), (1234, saves + 1, False))
        self.assertEqual(self.leftovers(), [])

    def test_a_failed_newer_write_lets_an_older_one_land_and_stays_dirty(self):
        st = self.st
        older = st._take_snapshot()
        with st.lock:
            st.accounts['test']['characters'][0]['exp'] = 1234
        st.mark_dirty('newer')
        newer = st._take_snapshot()
        with mock.patch.object(S.os, 'replace', side_effect=PermissionError(13, 'held')), \
                mock.patch.object(S.time, 'sleep'):
            with self.assertRaises(PermissionError):
                st._write_snapshot(*newer)
        self.assertTrue(st._write_snapshot(*older))       # nothing newer on disk: it lands
        self.assertEqual(self.read()['test']['characters'][0]['exp'], 999)
        self.assertTrue(st.dirty)                         # 1234 is not on disk yet
        self.assertTrue(st.flush())
        self.assertEqual(self.read()['test']['characters'][0]['exp'], 1234)
        self.assertFalse(st.dirty)

    def test_the_write_runs_outside_the_store_lock(self):
        """flush (the debounced save's path) holds db_lock only for the snapshot: a handler can
        take it while the file is written - it used to wait out every replace retry."""
        st, real, seen = self.st, S.atomic_write, []

        def writer(path, data, delays=None):
            t = threading.Thread(target=lambda: (st.lock.acquire(), seen.append('lock taken'), st.lock.release()),
                                 daemon=True)
            t.start()
            t.join(2.0)
            seen.append('blocked' if t.is_alive() else 'free')
            return real(path, data, delays)
        with mock.patch.object(S, 'atomic_write', side_effect=writer):
            self.assertTrue(st.flush())
        self.assertEqual(seen, ['lock taken', 'free'])
        self.assertEqual(self.read()['test']['characters'][0]['exp'], 999)

    def test_a_failed_immediate_save_is_never_marked_clean_by_an_older_write(self):
        """Review of livetest bug 7: create_account changed the records with no mark_dirty(),
        so an older flush snapshot that landed after its failed write saw the change count it
        covered unchanged and marked the store clean - 'newbie' was in memory only, and the
        scheduled retry and the shutdown flush both found nothing to write."""
        st = self.st
        older = st._take_snapshot()                       # a debounced flush's, still in flight
        with mock.patch.object(S, 'atomic_write', side_effect=PermissionError(13, 'held by a reader')):
            with self.assertRaises(PermissionError):
                st.create_account('newbie', 'pw')
        self.assertTrue(st.dirty)
        self.assertTrue(st._write_snapshot(*older))       # nothing newer on disk: it lands
        self.assertNotIn('newbie', self.read())
        self.assertTrue(st.dirty)                         # 'newbie' is still to save
        self.assertTrue(st.flush())
        self.assertIn('newbie', self.read())
        self.assertFalse(st.dirty)
        # an immediate save in flight keeps the store dirty until it is on disk, whatever an
        # older snapshot's write does first
        older = st._take_snapshot()
        with st.lock:
            st.accounts['newbie']['manner'] = 5
        immediate = st._take_snapshot(change=True)
        self.assertTrue(st._write_snapshot(*older))
        self.assertTrue(st.dirty)
        self.assertTrue(st._write_snapshot(*immediate))
        self.assertFalse(st.dirty)
        self.assertEqual(self.read()['newbie']['manner'], 5)

    def test_immediate_saves_write_outside_the_store_lock(self):
        """Review of livetest bug 7: create_account / add_character / remove_character / set_gm
        wrote - and backed off up to ~3 s while a reader held the file - holding db_lock, which
        every handler takes. They snapshot under it and write after letting it go; save=False
        only marks the store dirty, for a caller that validates under db_lock itself and
        calls save_now() after its own `with`."""
        st, real, seen = self.st, S.atomic_write, []

        def writer(path, data, delays=None):
            t = threading.Thread(target=lambda: (st.lock.acquire(), st.lock.release()), daemon=True)
            t.start()
            t.join(2.0)
            seen.append('blocked' if t.is_alive() else 'free')
            return real(path, data, delays)
        with mock.patch.object(S, 'atomic_write', side_effect=writer):
            st.create_account('newbie', 'pw')
            nova = st.new_character('Nova', **S.DEFAULT_LOOK_SLOTS[0], stats=S.DEFAULT_STATS, now=0)
            st.add_character('newbie', nova)
            self.assertEqual(st.set_gm('Nova')[0], 'newbie')
            self.assertTrue(st.remove_character('newbie', 'Nova'))
            self.assertFalse(st.remove_character('newbie', 'Nova'))     # nothing removed, nothing saved
        self.assertEqual(seen, ['free'] * 4)
        self.assertEqual(self.read()['newbie']['characters'], [])
        self.assertFalse(st.dirty)
        saves = st.saves
        with st.lock:
            st.create_account('later', 'pw', save=False)
        self.assertEqual((st.saves, st.dirty), (saves, True))
        self.assertNotIn('later', self.read())
        st.save_now()
        self.assertEqual((st.saves, st.dirty), (saves + 1, False))
        self.assertIn('later', self.read())

    def test_a_change_during_the_write_keeps_the_store_dirty(self):
        st, real = self.st, S.atomic_write

        def writer(path, data, delays=None):
            with st.lock:
                st.accounts['test']['characters'][0]['exp'] = 777
            st.mark_dirty('a kill while the file is written')
            return real(path, data, delays)
        with mock.patch.object(S, 'atomic_write', side_effect=writer):
            self.assertTrue(st.flush())
        self.assertEqual(self.read()['test']['characters'][0]['exp'], 999)   # the snapshot's value
        self.assertTrue(st.dirty)                                           # 777 still to save
        self.assertTrue(st.flush())
        self.assertEqual(self.read()['test']['characters'][0]['exp'], 777)
        self.assertFalse(st.dirty)

    def test_concurrent_writers_never_tear_the_file(self):
        st = self.st
        errors = []

        def worker(n):
            try:
                for i in range(40):
                    with st.lock:
                        st.accounts['test']['characters'][0]['exp'] = n * 1000 + i
                        st.accounts['test'][f'pad{n}'] = 'x' * random.randint(0, 3000)
                    st.mark_dirty()
                    st.flush() if i % 2 else st.save_now()
            except Exception as e:                            # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        st.save_now()
        self.assertEqual(self.read(), json.loads(json.dumps(st.accounts)))
        self.assertEqual(self.leftovers(), [])

    def test_killed_during_a_save_loop_leaves_valid_json(self):
        """P1 exit criterion 8, offline: a process saving in a tight loop is killed at random
        points; the file is complete JSON every time."""
        script = (
            'import sys, random\n'
            f'sys.path.insert(0, {HERE!r})\n'
            'import store\n'
            f'st = store.Store({self.path!r}, backup=False).load()\n'
            'i = 0\n'
            'while True:\n'
            '    with st.lock:\n'
            '        st.accounts["test"]["characters"][0]["exp"] = i\n'
            '        st.accounts["test"]["pad"] = "x" * random.randint(0, 200000)\n'
            '    st.save_now()\n'
            '    i += 1\n'
            '    if i == 3:\n'
            '        print("saving", flush=True)\n')
        for attempt in range(6):
            with self.subTest(attempt=attempt):
                proc = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, cwd=self.tmp)
                try:
                    self.assertEqual(proc.stdout.readline().strip(), b'saving')
                    time.sleep(random.uniform(0.0, 0.25))
                finally:
                    proc.kill()
                    proc.wait(10)
                    proc.stdout.close()
                data = self.read()                            # raises if torn
                self.assertIn('test', data)
                self.assertGreaterEqual(data['test']['characters'][0]['exp'], 2)
        for name in self.leftovers():                         # a killed writer may leave its temp file
            os.remove(os.path.join(self.tmp, name))


class ConcurrentEnsureAndSave(TempDir):
    """A handler thread normalizing a record while the store serializes it.

    inventory.ensure / quests.ensure used to clear() and re-fill the record's live nested
    dicts on EVERY _bag(), _wallet_of(), _quest_state() and 0x03 build - all handler-thread
    calls - while save_now handed those same dicts to json.dumps. The encoder is pure
    Python, so the interpreter can switch threads inside the dict it is iterating and the
    save dies with "dictionary changed size during iteration", losing everything dirty.
    A conforming record is now left untouched and the encoder walks a store.snapshot()."""

    ROUNDS, READERS = 1200, 4
    HERB, POTION = 5, 3                                   # two EN consumables (one stack each)

    def setUp(self):
        super().setUp()
        self.write(LEGACY)
        self.st = S.Store(self.path).load()
        self.char = self.st.accounts['test']['characters'][0]
        INV.Inventory(self.char).add(self.HERB, 5)        # something for the encoder to walk
        # A thread switch at (almost) every bytecode: the race is a window of microseconds
        # inside json.dumps, and this is what makes hitting it a certainty per run instead
        # of "somewhere in the next ten seconds".
        self.addCleanup(sys.setswitchinterval, sys.getswitchinterval())
        sys.setswitchinterval(1e-6)

    def normalize(self, errors, stop):
        """A handler thread reaching for the model: _bag(), _wallet_of() and _quest_state()
        all run ensure() on the live record, from any thread, taking no lock."""
        try:
            for _ in range(self.ROUNDS):
                if stop.is_set():
                    return
                INV.ensure(self.char)
                Q.ensure(self.char)
        except Exception as e:                            # noqa: BLE001
            errors.append(e)

    def mutate(self, errors, stop):
        """The one thread that changes the bag (in the server every mutation is serialized
        by the session's combat lock): a stack appears and disappears, so a nested dict
        changes SIZE while the others normalize and the store encodes."""
        try:
            bag = INV.Inventory(self.char)                # binds char['inventory'] by identity
            for _ in range(self.ROUNDS):
                if stop.is_set():
                    return
                bag.add(self.POTION, 1)
                bag.remove(self.POTION, 1)
        except Exception as e:                            # noqa: BLE001
            errors.append(e)

    def test_saving_never_races_the_records_being_normalized(self):
        errors, stop = [], threading.Event()
        threads = [threading.Thread(target=self.normalize, args=(errors, stop), daemon=True)
                   for _ in range(self.READERS)]
        threads.append(threading.Thread(target=self.mutate, args=(errors, stop), daemon=True))
        saves = 0
        for t in threads:
            t.start()
        try:
            while any(t.is_alive() for t in threads):
                self.st.save_now()                        # the debounced save and the shutdown flush
                saves += 1
        finally:
            stop.set()
            for t in threads:
                t.join(timeout=30)
            self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        self.assertGreater(saves, 10)
        stored = self.read()['test']['characters'][0]     # the last save is complete JSON
        self.assertEqual(stored['inventory']['consume'].get(str(self.HERB)), 5)
        self.assertEqual(stored['quests'], {'active': [0, 0, 0], 'progress': [0, 0, 0],
                                            'completed': []})


class DebouncedSave(TempDir):
    def setUp(self):
        super().setUp()
        self.write(LEGACY)
        self.clock = FakeClock()
        self.sched = ticks.Scheduler(clock=self.clock, name='test')
        self.st = S.Store(self.path, debounce_secs=2.0, autosave_secs=60.0).load()
        self.base = self.st.saves

    def test_first_change_is_on_disk_within_the_debounce(self):
        st = self.st
        st.attach(self.sched, autosave=False)
        with st.lock:
            st.accounts['test']['characters'][0]['exp'] = 10
        st.mark_dirty('kill')
        self.clock.t += 1.0
        with st.lock:
            st.accounts['test']['characters'][0]['exp'] = 20
        st.mark_dirty('kill')                                 # no second timer
        self.assertEqual(self.sched.pending(), 1)
        self.clock.t += 0.9
        self.assertEqual(self.sched.run_due(), 0)
        self.assertEqual(self.read()['test']['characters'][0]['exp'], 0)
        self.clock.t += 0.1                                   # 2.0 s after the first change
        self.assertEqual(self.sched.run_due(), 1)
        self.assertEqual(st.saves, self.base + 1)
        self.assertEqual(self.read()['test']['characters'][0]['exp'], 20)
        self.assertFalse(st.dirty)
        st.mark_dirty('again')                                # a new window opens
        self.assertEqual(self.sched.pending(), 1)

    def test_a_failed_save_is_retried_with_backoff(self):
        """livetest bug 7: after a failed save nothing rescheduled it - it waited for the next
        change or the 60 s autosave. The failure keeps the store dirty and flushes again
        RETRY_SECS later, twice as long after each further failure (up to RETRY_MAX_SECS).
        Review: the retry is a timer - the tick thread's save sleeps only
        QUICK_REPLACE_DELAYS, never the ~3 s backoff - and the traceback is logged once per
        run of failures, then one line per failure."""
        st = self.st
        st.attach(self.sched, autosave=False)
        with st.lock:
            st.accounts['test']['characters'][0]['exp'] = 30
        st.mark_dirty('kill')
        self.clock.t += 2.0

        def held():
            return mock.patch.object(S.os, 'replace', side_effect=PermissionError(13, 'held by a reader'))
        with held(), mock.patch.object(S.time, 'sleep') as slept:
            with self.assertLogs('WS', logging.WARNING) as cm:
                self.assertEqual(self.sched.run_due(), 1)
            self.assertEqual([r.levelno for r in cm.records], [logging.ERROR])
            self.assertIsNotNone(cm.records[0].exc_info)                   # the traceback, once
            self.assertEqual([c.args[0] for c in slept.call_args_list], list(S.QUICK_REPLACE_DELAYS))
            self.assertAlmostEqual(sum(S.QUICK_REPLACE_DELAYS), S.QUICK_REPLACE_BUDGET_SECS)
            self.assertTrue(st.dirty)
            self.assertEqual(self.read()['test']['characters'][0]['exp'], 0)   # the old file, intact
            self.assertEqual(self.sched.pending(), 1)                          # the retry
            for n, delay in enumerate((0.25, 0.5, 1.0), start=2):             # RETRY_SECS, doubling
                self.clock.t += delay * 0.9
                self.assertEqual(self.sched.run_due(), 0)
                self.clock.t += delay * 0.1 + 1e-6
                with self.assertLogs('WS', logging.WARNING) as cm:
                    self.assertEqual(self.sched.run_due(), 1)
                self.assertEqual([r.levelno for r in cm.records], [logging.WARNING])
                self.assertIsNone(cm.records[0].exc_info)                       # one line
                self.assertIn(f'({n} in a row)', cm.output[0])
                self.assertEqual(self.sched.pending(), 1)
        self.assertEqual(st.retry_delay(), 2.0)
        self.clock.t += 2.0 + 1e-6                                             # the reader let go
        with self.assertLogs('WS', logging.INFO) as cm:
            self.assertEqual(self.sched.run_due(), 1)
        self.assertIn('saved after 4 failed attempt(s)', '\n'.join(cm.output))
        self.assertEqual(self.read()['test']['characters'][0]['exp'], 30)
        self.assertFalse(st.dirty)
        self.assertEqual(st.saves, self.base + 1)
        self.assertEqual(self.sched.pending(), 0)
        self.assertEqual(st.retry_delay(), st.RETRY_SECS)                      # the run is over
        # an explicit save_now that fails schedules the retry too; a detached store does not
        st.mark_dirty('again')
        self.sched.run_due(self.clock.t + 5.0)                             # (the debounced save)
        with held(), mock.patch.object(S.time, 'sleep'):
            with self.assertRaises(PermissionError):
                st.save_now()
        self.assertEqual(self.sched.pending(), 1)
        st.detach()
        with held(), mock.patch.object(S.time, 'sleep'):
            with self.assertLogs('WS', logging.ERROR):                     # save_now logged nothing
                self.assertFalse(st.flush())
        self.assertEqual(self.sched.pending(), 0)
        self.assertTrue(st.flush())

    def test_the_backoff_is_capped(self):
        st = self.st
        for failures, delay in ((0, 0.25), (1, 0.25), (2, 0.5), (7, 16.0), (8, 30.0), (400, 30.0)):
            st._failures = failures
            self.assertEqual(st.retry_delay(), delay, failures)

    def test_a_held_file_never_holds_the_world_lock_long(self):
        """Review of livetest bug 7: the debounced save, its retry and the autosave run on the
        tick thread, which holds the world lock around every callback (GameServer wires
        ticks.Scheduler(lock=world_lock)). While a reader held accounts.json each of those
        saves slept through the ~3 s replace backoff there and the retry came a second later,
        so the world lock was held ~3 s of every 4: monster AI, regen, DoT, buff expiry and
        the login claim all waited. A tick's save now sleeps at most QUICK_REPLACE_DELAYS and
        the retry is a timer."""
        world_lock = threading.RLock()
        sched = ticks.Scheduler(lock=world_lock, name='test-world')
        st = self.st
        st.debounce_secs, st.autosave_secs = 0.05, 0.2
        st.attach(sched, autosave=True)
        attempts, waits = [], []

        def held(src, dst):
            attempts.append(time.monotonic())
            raise PermissionError(13, 'held by a reader')
        with mock.patch.object(S.os, 'replace', side_effect=held), self.assertLogs('WS', logging.WARNING):
            sched.start()
            try:
                with st.lock:
                    st.accounts['test']['characters'][0]['exp'] = 42
                st.mark_dirty('kill')
                end = time.monotonic() + 1.5
                while time.monotonic() < end:
                    t0 = time.monotonic()
                    with world_lock:                              # a handler / the next tick
                        waits.append(time.monotonic() - t0)
                    time.sleep(0.01)
            finally:
                sched.stop()
                st.detach()
        self.assertLess(max(waits), 0.5)                          # it was ~3.0 s
        saves = len(attempts) // (len(S.QUICK_REPLACE_DELAYS) + 1)
        self.assertGreaterEqual(saves, 3)                         # debounce, autosaves, retries
        self.assertTrue(st.dirty)
        self.assertEqual(self.read()['test']['characters'][0]['exp'], 0)
        self.assertTrue(st.flush())                               # the reader let go
        self.assertEqual(self.read()['test']['characters'][0]['exp'], 42)

    def test_a_tick_save_never_waits_for_another_writer(self):
        """Review of livetest bug 7: a create / delete / !gm save holds _save_mutex through its
        whole ~3 s replace backoff while a reader holds the file, and the tick thread's save
        waiting for that mutex held the world lock as long. tick_flush does not wait: the
        store stays dirty and the retry writes it once the other writer is done. A trade /
        stall commit (under store.lock) waits at most the quick budget."""
        st = self.st
        st.attach(self.sched, autosave=False)
        with st.lock:
            st.accounts['test']['characters'][0]['exp'] = 7
        st.mark_dirty('kill')
        self.clock.t += 2.0
        with st._save_mutex:                                      # another writer is on disk
            t0 = time.monotonic()
            with self.assertLogs('WS', logging.INFO) as cm:
                self.assertEqual(self.sched.run_due(), 1)         # the debounced save
            self.assertLess(time.monotonic() - t0, 0.5)
            self.assertIn('another save is writing', '\n'.join(cm.output))
            self.assertEqual((st.dirty, st.saves), (True, self.base))
            self.assertEqual(self.sched.pending(), 1)             # the retry
            t0 = time.monotonic()
            with self.assertLogs('WS', logging.INFO):
                self.assertFalse(st.save_now(delays=S.QUICK_REPLACE_DELAYS,
                                             wait=S.QUICK_REPLACE_BUDGET_SECS))
            self.assertLess(time.monotonic() - t0, 0.5)
            self.assertEqual(self.sched.pending(), 1)             # still the one retry
        self.assertEqual(self.read()['test']['characters'][0]['exp'], 0)
        self.assertEqual(st._failures, 0)                         # busy is not a failure
        self.clock.t += st.RETRY_SECS + 1e-6
        self.assertEqual(self.sched.run_due(), 1)
        self.assertEqual(self.read()['test']['characters'][0]['exp'], 7)
        self.assertEqual((st.dirty, st.saves, self.sched.pending()), (False, self.base + 1, 0))

    def test_autosave_backstop_and_detach(self):
        st = self.st
        st.attach(self.sched, autosave=True)
        self.assertEqual(self.sched.pending(), 1)
        self.clock.t += 60.0
        self.sched.run_due()
        self.assertEqual(st.saves, self.base)                 # clean: nothing written
        st.dirty = True                                       # dirty without a debounce timer
        self.clock.t += 60.0
        self.sched.run_due()
        self.assertEqual(st.saves, self.base + 1)
        st.mark_dirty()
        st.detach()
        self.assertEqual(self.sched.pending(), 0)
        self.assertTrue(st.flush())

    def test_without_a_scheduler_saves_only_on_request(self):
        self.st.mark_dirty()
        self.assertEqual(self.st.saves, self.base)
        self.assertTrue(self.st.flush())


# ------------------------------------------------------------------------ world ---
class OnlineRegistry(unittest.TestCase):
    def test_claim_enter_drop(self):
        w = World()
        a, b = {'addr': 'a'}, {'addr': 'b'}
        self.assertIsNone(w.claim(a, 1, 'test'))
        self.assertEqual((a['uid'], a['username']), (1, 'test'))
        self.assertIs(w.session(1), a)
        self.assertIsNone(w.claim(a, 1, 'test'))                 # same session again: no-op
        self.assertTrue(w.enter(a, 'TestHero'))
        self.assertIs(w.by_char_name('TESTHERO'), a)

        old = w.claim(b, 1, 'test')                               # duplicate login
        self.assertIs(old, a)
        self.assertIsNone(w.session(1))                           # b is refused, a removed
        self.assertNotIn('uid', b)
        self.assertIsNone(w.by_char_name('testhero'))
        self.assertFalse(w.enter(a, 'TestHero'))                  # kicked session cannot re-index
        self.assertFalse(w.drop(a))

        self.assertIsNone(w.claim(b, 1, 'test'))                  # the retry succeeds
        self.assertFalse(w.drop(a))                               # a stale drop never removes b
        self.assertIs(w.session(1), b)
        self.assertEqual(w.online(), [b])
        self.assertTrue(w.drop(b))
        self.assertEqual(w.online(), [])

    def test_enter_replaces_the_previous_name(self):
        w = World()
        a = {}
        w.claim(a, 2, 'admin')
        w.enter(a, 'One')
        w.enter(a, 'Two')
        self.assertEqual(list(w.by_name), ['two'])

    def test_leave_frees_the_name_but_keeps_the_account_online(self):
        w = World()
        a = {}
        w.claim(a, 1, 'test')
        w.enter(a, 'Nova')
        self.assertTrue(w.leave(a))                 # character deleted / back at select
        self.assertIsNone(w.by_char_name('Nova'))
        self.assertIs(w.session(1), a)              # still logged in
        self.assertFalse(w.leave(a))


# ----------------------------------------------------------------- name rules ---
class Names(unittest.TestCase):
    """names.py mirrors the client's own FUN_0043DF40 plus the server policy
    (login_character.md 3.5.4; lc-create)."""

    def test_usable_names(self):
        for name in ('Nova', 'a', 'TestHero', 'A1234567890BCDEF', 'X9'):
            self.assertIsNone(names.check(name), name)
            self.assertTrue(names.is_valid(name))

    def test_length_is_measured_in_wire_bytes(self):
        self.assertIsNone(names.check('A' * 16))
        self.assertIn('17 bytes', names.check('A' * 17))
        self.assertEqual(names.check(''), 'empty')
        # cp949 names are two bytes per character and are refused by the policy anyway.
        self.assertIsNotNone(names.check('한글'))

    def test_client_refusals(self):
        self.assertEqual(names.check('two words'), 'contains a space')
        self.assertEqual(names.check(b'a\xa1\xa1b'), 'contains a space')      # CP949 blank
        for name, word in (('windy', 'wind'), ('Wind', 'wind'), ('GameMaster', 'gamemaster'),
                           ('xxMASTERxx', 'master'), ('Yahoo1', 'yahoo'), ('hamelin', 'hamelin')):
            self.assertEqual(names.check(name), f'reserved word {word!r}', name)
        self.assertIn('reserved', names.check("O'Brien"))

    def test_policy_charset(self):
        for name in ('Nova-2', 'a_b', 'hi!', 'Mädchen', 'a b'):
            self.assertIsNotNone(names.check(name), name)

    def test_clean_cuts_at_the_first_nul(self):
        self.assertEqual(names.clean(b'Nova\x00\xff\xff'), 'Nova')
        self.assertEqual(names.clean('Nova\x00junk'), 'Nova')
        self.assertIsNone(names.check(b'Nova\x00\xff'))       # trailing stack garbage ignored


# ------------------------------------------------------------- password hashing ---
class Passwords(unittest.TestCase):
    """auth.py: accounts.json holds a pbkdf2 hash, never the password (lc-login-errors)."""

    def test_hash_and_verify(self):
        stored = auth.hash_password('test')
        self.assertTrue(stored.startswith('pbkdf2_sha256$'))
        self.assertNotIn('test', stored.split('$', 2)[2])
        self.assertTrue(auth.verify(stored, 'test'))
        for wrong in ('Test', 'test ', '', 'tes'):
            self.assertFalse(auth.verify(stored, wrong), wrong)
        # The wire field is NUL-terminated with stack garbage behind it (live #07).
        self.assertTrue(auth.verify(stored, 'test\x00\xff\xff'))
        self.assertNotEqual(stored, auth.hash_password('test'))      # per-record salt
        self.assertTrue(auth.verify(auth.hash_password('test'), 'test'))

    def test_legacy_plaintext_record_still_verifies(self):
        self.assertTrue(auth.verify('test', 'test'))
        self.assertFalse(auth.verify('test', 'nope'))
        # No password on the record, or an empty one on the wire: never a match.
        self.assertFalse(auth.verify(None, ''))
        self.assertFalse(auth.verify(None, 'x'))
        self.assertFalse(auth.verify('', ''))
        self.assertFalse(auth.verify(auth.hash_password(''), ''))
        self.assertTrue(auth.needs_upgrade('test'))
        self.assertFalse(auth.needs_upgrade(auth.hash_password('test')))
        self.assertTrue(auth.needs_upgrade(auth.hash_password('test', iterations=1)))

    def test_corrupt_record_and_overlong_password_are_refused(self):
        for stored in ('pbkdf2_sha256$x$y$z', 'pbkdf2_sha256$1', 'pbkdf2_sha256$1$@@$@@'):
            self.assertFalse(auth.verify(stored, 'test'), stored)
            self.assertTrue(auth.needs_upgrade(stored))
        # The client field is str[21]: a longer value can only come from a built packet.
        long_pw = 'a' * (auth.MAX_PASSWORD_BYTES + 1)
        self.assertFalse(auth.verify(auth.hash_password(long_pw), long_pw))
        self.assertTrue(auth.verify(auth.hash_password('a' * auth.MAX_PASSWORD_BYTES),
                                    'a' * auth.MAX_PASSWORD_BYTES))


if __name__ == '__main__':
    unittest.main(verbosity=1)
