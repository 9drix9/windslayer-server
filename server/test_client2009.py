#!/usr/bin/env python3
"""
test_client2009.py - offline tests of the EN 2009 client build (client-2009-login)
==================================================================================
The 2009 client (Outspark v1.04 Build 14, WindSlayer2009) next to the default 2008 one:
config CLIENT_BUILD, per-build spec (protocol_spec_2009.json) and client data, the version
reply (version 14 + channel port), the SSO login C2S 0x01 with its session key and relogin,
the 82-byte S2C 0x02 records, C2S 0x0E with gender, C2S 0x2B with the trailing byte (both
send sites) up to the first world packets, delete, and the store's 2009 schema step.

Every flow runs the real GameServer._handle_fireway loop through fakeclient.FakeClient with
the 2009 spec (make_server(client_build='2009')); every S2C is decoded with the 2009 grammar
and must consume exactly its bytes. No port is bound, no client is started, and the live
accounts.json is never opened (each server gets a temp copy; checked at module end).
"""
import copy
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

import fakeclient as F  # noqa: E402
import packets as P  # noqa: E402
import registry  # noqa: E402
import store as S  # noqa: E402

W = F.import_server()
cfgmod = W.cfgmod
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B9 = '2009'
# The 2009 client data install; the tests fall back to the 2008 one if it is missing (only
# the maps/items the login flow touches are read, and both installs carry them).
CLIENT_DIR_2009 = cfgmod.DEFAULTS['CLIENT_DIR_2009']
HAVE_2009_INSTALL = os.path.isdir(os.path.join(HERE, CLIENT_DIR_2009, 'hs'))

# spec_2009 keys (2009 send VAs)
LOGIN = '0x451CE5/0x01'
CREATE = '0x44B961/0x0E'
ENTER = '0x4315D7/0x2B'
ENTER_RELOGIN = '0x452133/0x2B'
PASSWORD_WINDOW = '0x468F05/0x51'
DELETE = '0x44D41C/0x12'
LOOKS_0 = (3, 2, 4, 5, 3)                       # s10, s1, s6, s5, s9 in the gender-0 ranges
LOOKS_1 = (0x66, 0x65, 0x67, 0x66, 4)           # the 101-based gender-1 set


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


_LIVE_HASH = None


def setUpModule():
    global _LIVE_HASH
    _LIVE_HASH = _sha(LIVE_ACCOUNTS) if os.path.exists(LIVE_ACCOUNTS) else None
    # every S2C a 2009 flow builds must use the 2009 field names (packets.unknown_fields)
    P.STRICT_FIELDS.add(B9)


def tearDownModule():
    P.STRICT_FIELDS.discard(B9)
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_client2009.py (tests must only use temp copies)'


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def config_2009(**overrides):
    values = {'CLIENT_BUILD': B9, **overrides}
    if not HAVE_2009_INSTALL:
        values.setdefault('CLIENT_DIR_2009', cfgmod.DEFAULTS['CLIENT_DIR'])
    return cfgmod.from_dict(values)


# ============================================================ config + codec ===
class Build2009Config(unittest.TestCase):
    def test_2008_stays_the_default(self):
        cfg = cfgmod.defaults()
        self.assertEqual((cfg.CLIENT_BUILD, cfg.client_dir(), cfg.version_code()), ('2008', '..', 3))
        self.assertEqual(P.spec_path(), P.SPEC_PATH)
        self.assertEqual(P.spec_path(cfg.CLIENT_BUILD), P.SPEC_PATH)

    def test_2009_build_selects_its_spec_data_and_version(self):
        cfg = cfgmod.from_dict({'CLIENT_BUILD': B9})
        self.assertEqual(cfg.version_code(), cfgmod.VERSION_OK_2009)
        self.assertEqual(cfg.version_code(), 14)
        self.assertEqual(os.path.normcase(os.path.abspath(os.path.join(HERE, cfg.client_dir()))),
                         os.path.normcase(os.path.abspath(os.path.join(HERE, '..', '..', 'WindSlayer2009'))))
        self.assertEqual(os.path.basename(P.spec_path(B9)), 'protocol_spec_2009.json')
        self.assertEqual(cfgmod.from_dict({'CLIENT_BUILD': B9, 'MAINTENANCE': True}).version_code(),
                         cfgmod.VERSION_MAINTENANCE)
        for bad in ('2010', '', 2009):
            with self.subTest(bad=bad), self.assertRaises(cfgmod.ConfigError):
                cfgmod.from_dict({'CLIENT_BUILD': bad})

    def test_the_memory_driver_reads_the_layout_of_its_build(self):
        # client-2009-tooling: the driver runs for both builds, each with its own exe layout
        # (it used to be off for 2009 because it only knew the 2008 addresses); the 2009
        # offsets themselves are checked against client_map_2009.json in test_tooling2009.py.
        tmp = tempfile.mkdtemp(prefix='ws_2009_drv_')
        try:
            s08, s09 = F.make_server(tmp), F.make_server(tmp, config=config_2009())
            self.assertTrue(s08.memory_driver_enabled())
            self.assertTrue(s09.memory_driver_enabled())
            self.assertEqual((s08.client_layout.build, s08.client_layout.scene_ptr), ('2008', 0x70EECC))
            self.assertEqual((s09.client_layout.build, s09.client_layout.scene_ptr), (B9, 0x54F0C0))
            self.assertFalse(s09.config.DRIVER_MELEE)
            self.assertFalse(F.make_server(tmp, config=cfgmod.from_dict({'DEV_MEMORY_COMBAT': False}))
                             .memory_driver_enabled())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_2009_server_uses_its_own_build_data_and_store_schema(self):
        tmp = tempfile.mkdtemp(prefix='ws_2009_cfg_')
        try:
            server = F.make_server(tmp, config=config_2009())
            self.assertEqual((server.client_build, server.store.client_build), (B9, B9))
            self.assertEqual(W.EC.client_dir(), os.path.abspath(os.path.join(HERE, server.config.client_dir())))
            # make_server(client_build=...) switches the build of any config
            self.assertEqual(F.make_server(tmp, config=config_2009(), client_build='2008').client_build, '2008')
            self.assertEqual(F.make_server(tmp, client_build=B9).config.CLIENT_BUILD, B9)
            default = F.make_server(tmp)
            self.assertEqual((default.client_build, default.store.client_build), ('2008', '2008'))
            self.assertEqual(W.EC.client_dir(), os.path.abspath(os.path.join(HERE, '..')))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Codec2009(unittest.TestCase):
    def test_both_specs_live_side_by_side(self):
        self.assertIn('session_key', P.spec('0x02', client_build=B9)['grammar'])
        self.assertNotIn('session_key', P.spec('0x02')['grammar'])
        self.assertEqual(len(P.build(LOGIN, F.sso_login('test', 'test', 7), direction='C2S', client_build=B9)), 135)
        with self.assertRaises(KeyError):
            P.spec(LOGIN, 'C2S')                         # a 2009 send site is not a 2008 key
        # using the 2009 spec leaves the default untouched
        self.assertEqual(P.spec('0x02')['grammar'], P._db('2008').by_key['0x02']['grammar'])
        self.assertEqual(len(P.build('0x44D8BF/0x01', {'account_id': 'test', 'password': 'test'},
                                     direction='C2S')), 62)

    def test_2009_assume_table_and_caps_are_consistent(self):
        self.assertEqual(P.check_assume_table(B9), [])
        self.assertEqual(P.check_assume_table(), [])
        self.assertEqual(P.check_list_caps(B9), [])
        table = P.assume_table(B9)
        self.assertEqual(table['0x02'], {'client_relogin_pending_gs_408': False})
        # a 2008 condition text the 2009 grammar lost is dropped, never guessed (0x21 moved
        # from scene+0x970 to scene+0x988); the 2009 table states its own (client-2009-world)
        self.assertNotIn('scene+0x970 == 0', table['0x21'])
        self.assertEqual(table['0x21'], {'scene+0x988 == 0': False, 'local_player_guild_id > 1': False})
        self.assertEqual(P.assume_table()['0x21'], {'scene+0x970 == 0': False})

    def test_own_0x07_has_no_pet_block_and_a_remote_one_needs_its_receiver(self):
        row = W.R.player_record({'uid': 1}, {'name': 'Hero', 'look': S.default_look(0)}, {'uid': 1},
                                client_build=B9)
        own = P.build('0x07', W.R.player_list(row), client_build=B9, receiver_uid=1)
        self.assertEqual(len(own), 1 + 382)
        remote = P.build('0x07', W.R.player_list(row), client_build=B9, receiver_uid=2)
        self.assertEqual(len(remote), 1 + 383)                # + bool has_pet (0)
        with self.assertRaises(P.MissingAssume):
            P.build('0x07', W.R.player_list(row), client_build=B9)

    def test_registry_decodes_with_the_server_build(self):
        body = P.build(LOGIN, F.sso_login('test', 'pw'), direction='C2S', client_build=B9)
        rec, err = registry.decode(0x01, body, B9)
        self.assertIsNone(err)
        self.assertEqual(rec.key, LOGIN)
        rec, err = registry.decode(0x01, body)                 # 135 B is no 2008 login
        self.assertIsNone(rec)
        self.assertIsNotNone(err)
        enter = P.build(ENTER, {'char_name': 'Hero'}, direction='C2S', client_build=B9)
        rec, _ = registry.decode(0x2B, enter, B9)
        self.assertEqual(len(enter), 43)
        self.assertEqual(set(rec.candidates), {ENTER, ENTER_RELOGIN})   # byte-identical sites

    def test_a_packet_several_send_sites_decode_is_logged_with_every_candidate(self):
        """livetest bug 12: the 2009 W-key pickup (0x43DA4E/0x1F) and the pet auto-loot
        (0x42EA76/0x1F, first in the spec) have one grammar, and the log named the pickup
        "pet auto-loot". A record several sites decode gets a neutral name and every key."""
        pet, key = '0x42EA76/0x1F', '0x43DA4E/0x1F'
        body = P.build(key, {'ground_item_uid': 7}, direction='C2S', client_build=B9)
        rec, err = registry.decode(0x1F, body, B9)
        self.assertIsNone(err)
        self.assertEqual(rec.candidates, (pet, key))
        self.assertEqual(registry.record_label(rec, B9), f'GroundItemPickupRequest ({pet} | {key})')
        # one candidate: the site's own name and key, as before
        one = registry.decode(0x1F, P.build('0x43D80F/0x1F', {'ground_item_uid': 7}, direction='C2S'))[0]
        self.assertEqual(registry.record_label(one), 'GroundItemPickupRequest (0x43D80F/0x1F)')
        # names that share nothing but a prefix, or nothing at all
        self.assertEqual(registry._neutral_name(['ShopBuy (npc)', 'ShopBuyBack'], 2), 'ShopBuy')
        self.assertEqual(registry._neutral_name(['Alpha', 'Beta'], 2), 'one of 2 send sites')

        class _Server:                                          # what dispatch reads of a server
            client_build = B9
            DEAD_C2S_KEYS = {}
        with self.assertLogs('WS', logging.INFO) as cm:
            registry.dispatch(_Server(), {}, None, {}, 0x1F, body)
        text = '\n'.join(cm.output)
        self.assertIn(f'Unhandled opcode 0x1F GroundItemPickupRequest ({pet} | {key}) 2B', text)
        self.assertNotIn('pet auto-loot', text)
        with self.assertLogs('WS', logging.DEBUG) as cm:           # a routed packet's debug line
            registry.dispatch(_Server(), {0x1F: registry.Route(log='consumed')}, None, {}, 0x1F, body)
        text = '\n'.join(cm.output)
        self.assertIn(f'[C2S] 0x1F GroundItemPickupRequest ({pet} | {key}) ground_item_uid=7', text)
        self.assertNotIn('pet auto-loot', text)


# ============================================================ version reply ===
class VersionReply2009(unittest.TestCase):
    def body(self, counts=None, **overrides):
        vs = W.VersionServer(config=cfgmod.from_dict({'CLIENT_BUILD': B9, **overrides}),
                             user_counts=(lambda: counts) if counts is not None else None)
        return vs._build_version_body()

    def test_one_channel_decodes_with_the_2009_grammar(self):
        body = self.body()
        self.assertEqual(body[0], 0x01)
        rec = P.parse('0x01', body[1:], direction='S2C', client_build=B9)
        self.assertEqual(rec['version_code'], 14)
        server = rec['repeat[server_count]'][0]
        self.assertEqual((server['server_status'], server['channel_slot_count'], server['channel_no']), (3, 1, 1))
        entry = server['repeat[channel_slot_count]'][0]
        self.assertEqual((entry['game_server_ip'], entry['game_server_port']), (0x7F000001, 7022))

    def test_channel_entries_carry_ip_users_and_game_port(self):
        notice = cfgmod.defaults()['NOTICE'].encode('latin-1')
        body = self.body(counts={1: 5, 2: 130}, PUBLIC_IP='192.168.1.50', GAME_PORT=7123,
                         CHANNELS=[{'no': 1}, {'no': 2, 'ip': '10.0.0.7'}])
        self.assertEqual(body[1:3], struct.pack('<H', 14))
        tail = body[5 + len(notice):]
        self.assertEqual(tail[:4], bytes([1, 3, 2, 2]))
        # 11-byte entries: u8 no, u16 users, u32 ip (host order), u32 port (spec_2009 0x01)
        self.assertEqual(tail[4:], bytes([1]) + struct.pack('<HII', 5, cfgmod.ip_host_order('192.168.1.50'), 7123)
                         + bytes([2]) + struct.pack('<HII', 130, cfgmod.ip_host_order('10.0.0.7'), 7123))

    def test_maintenance_and_a_free_game_port(self):
        self.assertEqual(struct.unpack_from('<H', self.body(MAINTENANCE=True), 1)[0], cfgmod.VERSION_MAINTENANCE)
        # the 2009 client takes the port from the entry: no "hard-coded 7022" warning
        with self.assertNoLogs('WS', logging.WARNING):
            cfgmod.from_dict({'CLIENT_BUILD': B9, 'GAME_PORT': 7023})
        with self.assertLogs('WS', logging.WARNING):
            cfgmod.from_dict({'GAME_PORT': 7023})
        # the 2008 reply is unchanged: 7-byte entries, version 3
        vs = W.VersionServer(config=cfgmod.defaults())
        self.assertEqual(vs._build_version_body()[-11:], bytes([1, 3, 1, 1, 1]) + struct.pack('<HI', 0, 0x7F000001))


# ============================================================ login flows ===
class Server2009Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_2009_')
        self.server = F.make_server(self.tmp, config=config_2009())
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def reconfigure(self, **overrides):
        for c in self.clients:
            c.close()
        self.clients = []
        self.server = F.make_server(self.tmp, config=config_2009(**overrides))

    def client(self):
        c = F.FakeClient(self.server)
        self.clients.append(c)
        return c

    def s2c(self, c, pkt, length=None):
        if length is not None:
            self.assertEqual(len(pkt.payload), length, f'S2C 0x{pkt.opcode:02X} length')
        return c.s2c(pkt)

    def login_result(self, c, user='test', password='test', key=0):
        c.send_c2s(LOGIN, F.sso_login(user, password, key))
        pkt = c.expect(0x02)
        return self.s2c(c, pkt)

    def login(self, c, user='test', password='test', key=0):
        rec = self.login_result(c, user, password, key)
        self.assertEqual(rec['result'], 1)
        return rec

    def create(self, c, name, looks=LOOKS_0, gender=0, stats=(3, 2, 1, 3), result=1):
        s10, s1, s6, s5, s9 = looks
        c.send_c2s(CREATE, {'look_slot10': s10, 'look_slot1': s1, 'look_slot6': s6, 'look_slot5': s5,
                            'look_slot9': s9, 'name': name, 'gender': gender,
                            'str': stats[0], 'dex': stats[1], 'int': stats[2], 'tol': stats[3]})
        rec = self.s2c(c, c.expect(0x1C), 1)
        self.assertEqual(rec['result'], result, f'S2C 0x1C for {name!r}')
        return rec

    def enter_world(self, c, name='TestHero', key=ENTER, hop=False):
        """0x2B up to the first world packets: 0x03 then the own 0x07, both exact under the
        2009 grammar (map 101 has no monsters, so 0x15 welcome + 0x28/0x44 end the burst).
        hop: the entry continues a channel hop (P12 arch09-session-continuity) - no welcome."""
        c.send_c2s(key, {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907, 'char_name': name})
        ops = (0x03, 0x07) + (() if hop else (0x15,)) + (0x65, 0x28, 0x44)   # 0x65: the 2009 bank block
        pkts = c.expect(*ops)
        state = self.s2c(c, pkts[0])
        spawn = self.s2c(c, pkts[1])
        for pkt in pkts[2:]:
            self.s2c(c, pkt)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world')))
        return state, spawn, pkts

    def disk(self):
        with open(self.server.db_file, encoding='utf-8') as f:
            return json.load(f)


class Login2009(Server2009Test):
    def test_login_list_create_enter_world(self):
        c = self.client()
        rec = self.login(c)
        # header: account uid 1 (scene+0x224, the registration gate), flag_152 0, a live key
        self.assertEqual((rec['account_id'], rec['account_flag_152'], rec['transfer_status']), (1, 0, 3))
        key = rec['session_key']
        self.assertNotEqual(key, 0)
        self.assertEqual(self.server.session_keys[1], key)
        self.assertEqual(c.session['session_key'], key)
        row = rec['repeat[char_count]'][0]
        self.assertEqual((row['name'], row['gender'], row['class_id'], row['job_branch']), ('TestHero', 0, 0, 0))
        self.assertEqual([a['appearance'] for a in row['repeat[17]']],
                         [0, 1, 0, 0, 1, 2, 2, 0, 0, 2, 1, 0, 0, 1, 0, 0, 0])

        # C2S 0x0E with gender 1 and the 101-based looks -> 0x1C 1, stored per character
        self.create(c, 'Lady', looks=LOOKS_1, gender=1)
        lady = self.server.store.find_character('test', 'Lady')
        self.assertEqual((lady['gender'], lady['look_ext']), (1, [0, 0, 0]))
        self.assertEqual(lady['look'], [0, 0x65, 0, 0, 0x66, 0x66, 0x67, 0, 0, 4, 0x66, 0, 0, 0x66])
        self.assertEqual(self.disk()['test']['characters'][1]['gender'], 1)

        # a fresh connection lists both, 18 + 82 x 2 bytes
        c.close()
        c = self.client()
        c.send_c2s(LOGIN, F.sso_login('test', 'test'))
        pkt = c.expect(0x02)
        self.assertEqual(len(pkt.payload), 18 + 82 * 2)
        rows = c.s2c(pkt)['repeat[char_count]']
        self.assertEqual([(r['name'], r['gender']) for r in rows], [('TestHero', 0), ('Lady', 1)])

        # C2S 0x2B (43 B) -> 0x03 (2009: 71 B empty form, gender, 5 quest slots) + own 0x07
        state, spawn, pkts = self.enter_world(c, 'Lady')
        self.assertEqual(len(pkts[0].payload), 71)
        self.assertEqual((state['map_code'], state['gender'], state['mentor_id']), (101, 1, 0))
        self.assertEqual(len(state['repeat[5]']), 5)
        self.assertEqual(len(pkts[1].payload), 1 + 382)             # own record: no pet block
        me = spawn['repeat[player_count]'][0]
        self.assertEqual((me['name'], me['uid'], me['gender'], me['gm_or_guild_id']), ('Lady', 1, 1, 0))
        self.assertEqual(len(me['repeat[17]']), 17)
        # the start point (700, 812) settled onto 101's floor (livetest bug 5)
        self.assertEqual((me['pos_x'], me['pos_y']), (700.0, 912.0))
        self.assertTrue(me['cur_hp'] > 0)
        self.assertEqual(c.session['char_name'], 'Lady')
        self.assertIs(self.server.world.by_char_name('lady'), c.session)
        # after S2C 0x03 the 2009 client sends 0x2F, 0x63 and 0x8A by itself (spec_2009
        # 0x4315D7/0x2B expected_response): 0x2F gets the S2C 0x0B friend list (P6 stage 2,
        # social_friend F1), 0x63 the S2C 0x8A card list and 0x8A the 0xB3 sub 15 "not in a
        # guild" (client-2009-world; never sub 19)
        with self.assertNoLogs('WS', logging.ERROR):
            for key in ('0x45353B/0x2F', '0x453557/0x63', '0x453581/0x8A'):
                c.send_c2s(key)
            # + 0x99 sub 8 "In channel 1." on the connection's first 0x63 (P4 stage 4), and the
            # empty blacklist 0xBD after the friend list (P12 bl-1: after every 0x2F)
            friends, blist, deck, channel, guild = c.expect(0x0B, 0xBD, 0x8A, 0x99, 0xB3)
            self.assertEqual(self.s2c(c, friends, 2), {'friend_capacity': 20, 'friend_count': 0,
                                                       'repeat[friend_count]': []})
            self.assertEqual(self.s2c(c, blist, 1), {'count': 0, 'repeat[count]': []})
            self.assertEqual(self.s2c(c, deck, 1)['deck_count'], 0)
            self.assertEqual(self.s2c(c, channel, 2), {'sub_type': 8, 'channel_no': 1})
            self.assertEqual(self.s2c(c, guild, 2), {'sub': 15, 's15_result': 0})
            c.expect_silence(0.3)

    def test_bad_password_and_unknown_account(self):
        c = self.client()
        self.assertEqual(self.login_result(c, 'test', 'nope')['result'], 0x14)   # "Invalid password."
        c = self.client()
        self.assertEqual(self.login_result(c, 'nobody', 'test')['result'], 0x11)  # "ID does not exist"
        self.assertEqual(self.server.world.by_uid, {})
        self.assertEqual(self.server.session_keys, {})

    def test_failures_are_one_byte_and_carry_no_login(self):
        for user, password in (('test', 'nope'), ('nobody', 'x')):
            c = self.client()
            c.send_c2s(LOGIN, F.sso_login(user, password))
            self.assertEqual(len(c.expect(0x02).payload), 1)
            self.assertIsNone(c.session['username'])

    def test_sso_field_without_a_password_or_malformed_is_refused(self):
        for field in ('test ', 'test', '   ', ''):
            with self.subTest(field=field):
                c = self.client()
                c.send_c2s(LOGIN, {'sso_account': field, 'session_key': 0})
                self.assertEqual(self.s2c(c, c.expect(0x02), 1)['result'], 0x0F)
                self.assertIsNone(c.session['username'])
        # extra whitespace around and between the two tokens is fine
        c = self.client()
        c.send_c2s(LOGIN, {'sso_account': '  test \t test  ', 'session_key': 0})
        self.assertEqual(self.s2c(c, c.expect(0x02))['result'], 1)
        # a 2008-shaped (62 B) login is corrupted for this build
        c = self.client()
        c.send(0x01, P.build('0x44D8BF/0x01', {'account_id': 'test', 'password': 'test'}, direction='C2S'))
        self.assertEqual(self.s2c(c, c.expect(0x02), 1)['result'], 0x0B)

    def test_maintenance_banned_and_auto_register(self):
        with self.server.store.lock:
            self.server.store.account('admin')['banned'] = True
        self.assertEqual(self.login_result(self.client(), 'admin', 'admin')['result'], 5)
        self.reconfigure(MAINTENANCE=True)
        self.assertEqual(self.login_result(self.client())['result'], 0x0E)
        self.reconfigure(AUTO_REGISTER=True)
        rec = self.login_result(self.client(), 'newbie', 'secret')
        self.assertEqual((rec['result'], rec['char_count']), (1, 0))
        self.assertTrue(W.auth.verify(self.disk()['newbie']['password'], 'secret'))

    def test_delete_flow(self):
        c = self.client()
        self.login(c)
        self.create(c, 'Nova')
        c.send_c2s(PASSWORD_WINDOW, {'password': 'test', 'target_window_id': 0x235})
        rec = self.s2c(c, c.expect(0x80), 5)
        self.assertEqual((rec['result'], rec['target_window_id']), (1, 0x235))
        c.send_c2s(DELETE, {'char_name': 'Nova', 'confirm_password': 'nope'})
        self.assertEqual(self.s2c(c, c.expect(0x1F), 1)['result'], 12)
        c.send_c2s(DELETE, {'char_name': 'Nova', 'confirm_password': 'test'})
        self.assertEqual(self.s2c(c, c.expect(0x1F), 1)['result'], 1)
        self.assertIsNone(self.server.store.find_character('test', 'Nova'))

    def test_create_look_ranges_follow_the_packet_gender(self):
        with self.server.store.lock:
            # the 2008 account flag no longer decides (TestHero keeps the gender it was
            # migrated with at load)
            self.server.store.account('test')['gender'] = 1
        c = self.client()
        self.login(c)
        self.create(c, 'Boy', looks=LOOKS_0, gender=0)
        self.create(c, 'Girl', looks=LOOKS_1, gender=1)
        self.create(c, 'Mixed', looks=LOOKS_1, gender=0, result=3)     # 101-based set with gender 0
        self.create(c, 'Mixed2', looks=LOOKS_0, gender=1, result=3)
        self.create(c, 'Stats', stats=(4, 2, 1, 3), result=3)
        self.create(c, 'boy', result=2)
        self.assertEqual([(ch['name'], ch['gender']) for ch in self.server.store.characters('test')],
                         [('TestHero', 0), ('Boy', 0), ('Girl', 1)])


class Relogin2009(Server2009Test):
    """The 2009 client reconnects by itself (change channel: gs+0x408) and sends the u32
    session key of its last S2C 0x02; with the key the server replaces the old session
    instead of refusing with result 4, and takes the automatic C2S 0x452133/0x2B."""

    def test_relogin_with_the_live_key_replaces_the_session_and_enters_the_world(self):
        first = self.client()
        key = self.login(first)['session_key']
        self.enter_world(first)
        old = first.session
        second = self.client()
        with self.assertLogs('WS', logging.INFO) as cm:
            rec = self.login(second, key=key)
        self.assertEqual(rec['session_key'], key)                 # unchanged: the client keeps it
        self.assertTrue(any('relogin with the live session key' in line for line in cm.output))
        self.assertIs(self.server.world.session(1), second.session)
        self.assertTrue(_wait(lambda: old['addr'] not in self.server.sessions))
        self.assertTrue(old.get('kicked'))
        # gs+0x408: the client read only the result and now sends the automatic 0x2B; the
        # same character on the new connection is a channel hop (P12 session continuity):
        # no second welcome line
        state, spawn, _ = self.enter_world(second, key=ENTER_RELOGIN, hop=True)
        self.assertEqual(spawn['repeat[player_count]'][0]['uid'], 1)
        self.assertIs(self.server.world.by_char_name('TestHero'), second.session)

    def test_relogin_after_the_old_connection_closed(self):
        first = self.client()
        key = self.login(first)['session_key']
        first.close()
        self.assertTrue(_wait(lambda: self.server.world.session(1) is None))
        second = self.client()
        self.assertEqual(self.login(second, key=key)['session_key'], key)
        self.enter_world(second, key=ENTER_RELOGIN)

    def test_stale_key_is_a_fresh_login_and_duplicate_is_still_result_4(self):
        first = self.client()
        key = self.login(first)['session_key']
        second = self.client()
        self.assertEqual(self.login_result(second, key=key ^ 0x5A5A5A5A)['result'], 4)
        self.assertTrue(_wait(lambda: first.addr not in self.server.sessions))
        third = self.client()
        fresh = self.login(third)['session_key']                   # key 0: a new key
        self.assertNotIn(fresh, (0, key))
        self.assertEqual(self.server.session_keys[1], fresh)
        # the old key is dead now: it is a duplicate login again
        fourth = self.client()
        self.assertEqual(self.login_result(fourth, key=key)['result'], 4)

    def test_relogin_still_checks_the_password_and_the_account(self):
        first = self.client()
        key = self.login(first)['session_key']
        second = self.client()
        self.assertEqual(self.login_result(second, 'test', 'nope', key)['result'], 0x14)
        self.assertIs(self.server.world.session(1), first.session)
        # another account's live key is no relogin for this one
        admin = self.client()
        admin_key = self.login(admin, 'admin', 'admin')['session_key']
        third = self.client()
        self.assertEqual(self.login_result(third, 'test', 'test', admin_key)['result'], 4)
        self.assertTrue(_wait(lambda: first.addr not in self.server.sessions))   # plain duplicate
        self.assertIs(self.server.world.session(2), admin.session)


# ======================================================= store 2009 schema ===
class Store2009(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_2009_store_')
        self.path = os.path.join(self.tmp, 'accounts.json')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read(self):
        with open(self.path, encoding='utf-8') as f:
            return json.load(f)

    def test_migration_adds_gender_and_look_ext_once_with_its_backup(self):
        st = S.Store(self.path).load()                             # a current 2008 file
        with st.lock:
            st.accounts['admin']['gender'] = 1
            st.accounts['admin']['characters'].append(
                st.new_character('Lass', s10=0x66, s1=0x65, s6=0x67, s5=0x66, s9=4, stats=(3, 2, 1, 3)))
        st.save_now()
        for suffix in S.BACKUP_SUFFIXES:                           # already migrated to P3
            open(self.path + suffix, 'wb').close()
        with open(self.path, 'rb') as f:
            original = f.read()
        self.assertEqual(S.Store(self.path).load().migration_changes, [])        # 2008: nothing
        self.assertFalse(os.path.exists(self.path + S.BACKUP_SUFFIX_2009))

        st = S.Store(self.path, client_build=B9).load()
        self.assertIn('test/TestHero: gender = 0', st.migration_changes)
        self.assertIn('admin/Lass: gender = 1', st.migration_changes)             # the account's flag
        self.assertIn('test/TestHero: look_ext [0, 0, 0]', st.migration_changes)
        with open(self.path + S.BACKUP_SUFFIX_2009, 'rb') as f:
            self.assertEqual(f.read(), original)
        disk = self.read()
        self.assertEqual((disk['test']['characters'][0]['gender'], disk['test']['characters'][0]['look_ext']),
                         (0, [0, 0, 0]))
        # idempotent, and the 2008 build reads the migrated file unchanged
        again = S.Store(self.path, client_build=B9).load()
        self.assertEqual((again.migration_changes, again.saves), ([], 0))
        back = S.Store(self.path).load()
        self.assertEqual((back.migration_changes, back.saves), ([], 0))
        self.assertEqual(back.accounts, again.accounts)

    def test_a_malformed_look_ext_is_repaired(self):
        st = S.Store(self.path, client_build=B9).load()
        with st.lock:
            hero = st.find_character('test', 'TestHero')
            hero['look_ext'] = [5, 'x']
            hero['gender'] = 7
        st.save_now()
        st = S.Store(self.path, client_build=B9).load()
        hero = st.find_character('test', 'TestHero')
        self.assertEqual((hero['look_ext'], hero['gender']), ([5, 0, 0], 1))
        self.assertIn('test/TestHero: look_ext normalized', st.migration_changes)

    def test_new_character_schema_per_build(self):
        st9 = S.Store(self.path, client_build=B9)
        char = st9.new_character('Nova', s10=3, s1=2, s6=4, s5=5, s9=3, stats=(3, 2, 1, 3), gender=1)
        self.assertEqual((char['gender'], char['look_ext']), (1, [0, 0, 0]))
        self.assertEqual(S.migrate_character_2009(copy.deepcopy(char)), [])
        char8 = S.Store(self.path).new_character('Nova', s10=3, s1=2, s6=4, s5=5, s9=3, stats=(3, 2, 1, 3),
                                                  gender=1)
        self.assertNotIn('gender', char8)
        self.assertNotIn('look_ext', char8)


# ============================================== both builds in one process ===
class BothBuilds(unittest.TestCase):
    def test_2008_and_2009_servers_side_by_side(self):
        t8, t9 = tempfile.mkdtemp(prefix='ws_2008_'), tempfile.mkdtemp(prefix='ws_2009_')
        try:
            s8 = F.make_server(t8)
            s9 = F.make_server(t9, config=config_2009())
            with F.FakeClient(s8) as a, F.FakeClient(s9) as b:
                b.send_c2s(LOGIN, F.sso_login('test', 'test'))
                a.send_c2s('0x44D8BF/0x01', {'account_id': 'test', 'password': 'test'})
                p8, p9 = a.expect(0x02), b.expect(0x02)
                self.assertEqual(len(p8.payload), 13 + 75)             # 2008: 75-byte records
                self.assertEqual(len(p9.payload), 18 + 82)             # 2009: 82-byte records
                self.assertEqual(a.s2c(p8)['repeat[char_count]'][0]['name'], 'TestHero')
                self.assertEqual(F.FakeClient.decode(p8)['account_id'], 1)
                self.assertNotEqual(b.s2c(p9)['session_key'], 0)
                with self.assertRaises(P.ParseError):
                    F.FakeClient.decode(p9)                          # not a 2008 0x02
                # a wrong password answers per build
                a2, b2 = F.FakeClient(s8), F.FakeClient(s9)
                try:
                    a2.send_c2s('0x44D8BF/0x01', {'account_id': 'test', 'password': 'x'})
                    b2.send_c2s(LOGIN, F.sso_login('test', 'x'))
                    self.assertEqual(a2.s2c(a2.expect(0x02))['result'], 0x11)
                    self.assertEqual(b2.s2c(b2.expect(0x02))['result'], 0x14)
                finally:
                    a2.close()
                    b2.close()
        finally:
            shutil.rmtree(t8, ignore_errors=True)
            shutil.rmtree(t9, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
