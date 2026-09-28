#!/usr/bin/env python3
"""
test_world2009.py - offline tests of the EN 2009 client in the world (client-2009-world)
========================================================================================
Stage 2 of the 2009 port: everything the server sends in the world uses the 2009 layouts
when CLIENT_BUILD is '2009', and the 2008 build stays byte-identical.

- Builders2009: every S2C the server emits, audited against protocol_spec_2009.json (0x08
  reason byte, 0x1A names, 0x21 guild gate, 0x07/0x04/0x05 records with the 15 + 10 equip
  rows and the per-receiver pet block, the registry refusals), the X-Trap / old-0xA5 bans
  and the 2009 receiver-state (assume) table.
- Routes2009: the C2S the 2009 client sends by itself (0x8A after every 0x03, 0x2C with its
  list_type, the new guild/pet/cash opcodes) and the dead room-host send sites.
- Content2009: the 2009 client data (4322 items, the 205-line hni, 288 maps incl. the
  instance dungeons, the 2009 portal table, the 2009 Kind -> equip slot switch).
- WorldFlow2009: login -> enter world on map 102 (Pupu spawns in the 2009 grammar) -> portal
  to 101 and back -> a 61 B hit report kills a Pupu with exp and a drop -> corpse despawn
  and respawn, every packet decoded exactly with the 2009 spec.

Every flow runs the real GameServer loop through fakeclient.FakeClient; no port is bound, no
client is started and the live accounts.json is never opened (temp copies; checked at the
end of the module).
"""
import hashlib
import json
import logging
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import en_maps as M  # noqa: E402
import fakeclient as F  # noqa: E402
import packets as P  # noqa: E402
import registry  # noqa: E402
import gm  # noqa: E402
import quests as Q  # noqa: E402

W = F.import_server()
cfgmod = W.cfgmod
EC = W.EC
R = W.R
INV = W.invmod
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B9 = '2009'
CLIENT_DIR_2009 = cfgmod.DEFAULTS['CLIENT_DIR_2009']
HAVE_2009_INSTALL = os.path.exists(os.path.join(HERE, CLIENT_DIR_2009, 'hs', 'windslayer.hii'))
needs_2009 = unittest.skipUnless(HAVE_2009_INSTALL, 'needs the EN 2009 client data (CLIENT_DIR_2009)')

# spec_2009 C2S keys (2009 send VAs)
LOGIN = '0x451CE5/0x01'
ENTER = '0x4315D7/0x2B'
MOVE = '0x42E704/0x0D'
PORTAL = '0x431284/0x7E'
SKILL = '0x44FC61/0x15'
FRIENDS, CARDS, GUILD, GUILD_AGAIN = '0x45353B/0x2F', '0x453557/0x63', '0x453581/0x8A', '0x48456D/0x8A'
ROOM_LIST = '0x4455DD/0x2C'
GIFT = '0x467D8C/0x47'
ID_TRANSFER = '0x44A9D6/0x65'
DEAD_0C, DEAD_0E = '0x418A6B/0x0C', '0x418A6B/0x0E'


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
    # the last server of the module may have left en_content on the 2009 install
    EC.configure(cfgmod.DEFAULTS['CLIENT_DIR'], '2008')
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_world2009.py (tests must only use temp copies)'


def config_2009(**overrides):
    values = {'CLIENT_BUILD': B9, **overrides}
    if not HAVE_2009_INSTALL:
        values.setdefault('CLIENT_DIR_2009', cfgmod.DEFAULTS['CLIENT_DIR'])
    return cfgmod.from_dict(values)


def _code_lines(path):
    """The code of a module with its comments and docstrings dropped (source audits)."""
    with open(os.path.join(HERE, path), encoding='utf-8') as f:
        text = f.read()
    text = re.sub(r'"""[\s\S]*?"""', '', text)
    return [line.split('#', 1)[0] for line in text.splitlines()]


class ServerCase(unittest.TestCase):
    """A 2009 server on a temp accounts copy; build=None -> '2009'."""
    build = B9

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_world2009_')
        self.server = self.make()
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, **overrides):
        cfg = config_2009(**overrides) if self.build == B9 else cfgmod.from_dict(overrides)
        return F.make_server(self.tmp, config=cfg)

    def client(self):
        c = F.FakeClient(self.server)
        self.clients.append(c)
        return c

    def hero(self):
        return self.server.store.find_character('test', 'TestHero')

    def login(self, c):
        c.send_c2s(LOGIN, F.sso_login('test', 'test'))
        self.assertEqual(c.s2c(c.expect(0x02))['result'], 1)

    def enter(self, c=None, map_code=101, pos=None, mobs=0):
        """Login + C2S 0x2B; returns (client, [0x03, 0x07, 0x15, 0x65, 0x28, 0x44, (mobs x 0x1A)]).
        The 0x65 is the 2009 bank block (shop_storage-bank-model: the 2009 client opens the
        bank window with no packet, so its bank arrives at every world entry)."""
        with self.server.store.lock:
            ch = self.hero()
            ch['map'] = map_code
            if pos is not None:
                ch['x'], ch['y'] = float(pos[0]), float(pos[1])
        c = c or self.client()
        self.login(c)
        c.send_c2s(ENTER, {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907, 'char_name': 'TestHero'})
        pkts = c.expect(0x03, 0x07, 0x15, 0x65, 0x28, 0x44, *([0x1A] * mobs))
        self.assertTrue(c.wait_session(lambda s: s.get('in_world')))
        return c, pkts


# ================================================================== builders ===
class Builders2009(ServerCase):
    def test_0x08_carries_the_2009_reason_byte_and_2008_is_unchanged(self):
        for lead, reason in (('0x08', 0), ('0x5E', 1), ('0xA3', 3), ('0xA4', 4)):
            with self.subTest(lead=lead):
                key, body = self.server._lead_packet(lead, 102, 4321)
                self.assertEqual((key, len(body)), ('0x08', 7))     # spec_2009 0x08: u8 + u16 + u32
                self.assertEqual(P.parse('0x08', body, direction='S2C', client_build=B9),
                                 {'reason': reason, 'map_code': 102, 'game_time_ms': 4321})
        s8 = F.make_server(self.tmp)
        for lead in s8.LEAD_OPCODES:
            key, body = s8._lead_packet(lead, 102, 4321)
            self.assertEqual((key, body), (lead, struct.pack('<HI', 102, 4321)))   # 2008: 6 B

    def test_0x1A_record_uses_the_2009_names_and_order(self):
        mob = W.Monster(uid=W.MOB_UID_BASE, npccode=1, name='Pupu', level=1, hp=5, max_hp=5,
                        body_atk=1, defense=2, exp=10, x=1339.0, y=423.0, spawn_x=1339.0,
                        spawn_y=423.0, template=1)
        rec9 = self.server._monster_spawn_record(mob)
        fields = {'count': 1, 'repeat[count]': [rec9]}
        self.assertEqual(P.unknown_fields('0x1A', fields, client_build=B9), [])
        body = P.build('0x1A', fields, client_build=B9)
        # same record sizes as 2008: 82 B, + 4 (cmd_hold_ms) when server-controlled; the
        # shipped default is the client wander AI (MOB_SERVER_CONTROLLED false)
        self.assertEqual(len(body), 1 + 82 + (4 if rec9['server_controlled'] else 0))
        # pos_x/pos_y right after the (empty) effect list: count, template, uid, effect_count
        self.assertEqual(struct.unpack_from('<dd', body, 1 + 4 + 4 + 1), (1339.0, 423.0))
        row = P.parse('0x1A', body, direction='S2C', client_build=B9)['repeat[count]'][0]
        self.assertEqual((row['unk_95b'], row['action_state'], row['unk_949'], row['facing'], row['cur_hp']),
                         (8, 8, 2, 8, 5))
        # the 2008 record keeps its names and its bytes (pos after unk_8bd)
        rec8 = F.make_server(self.tmp)._monster_spawn_record(mob)
        f8 = {'count': 1, 'repeat[count]': [rec8]}
        self.assertEqual(P.unknown_fields('0x1A', f8), [])
        self.assertIn('unk_8cf', rec8)
        self.assertNotEqual(P.build('0x1A', f8), body)
        self.assertEqual(P.parse('0x1A', P.build('0x1A', f8), direction='S2C')['repeat[count]'][0]['unk_8cf'], 8)

    def test_0x21_guild_points_only_for_a_receiver_in_a_guild(self):
        session = {'uid': 1}
        fields, assume = self.server._exp_delta_packet(session, 10)
        self.assertEqual(P.build('0x21', fields, assume, client_build=B9), struct.pack('<i', 10))
        self.assertEqual(P.build('0x21', {'exp_delta': 10}, client_build=B9), struct.pack('<i', 10))
        with mock.patch.object(self.server, '_receiver_guild_id', return_value=7):
            fields, assume = self.server._exp_delta_packet(session, 10)
            self.assertEqual(len(P.build('0x21', fields, assume, client_build=B9)), 8)
            fields, assume = self.server._exp_delta_packet(session, -3)       # a loss: no tail
            self.assertEqual(len(P.build('0x21', fields, assume, client_build=B9)), 4)
        s8 = F.make_server(self.tmp)
        fields, assume = s8._exp_delta_packet(session, 10)
        self.assertEqual((fields, assume), ({'exp_delta': 10}, None))
        self.assertEqual(P.build('0x21', fields), struct.pack('<i', 10))

    def test_0x07_equip_grid_15_slots_and_10_id_attr_rows(self):
        char = self.hero()
        char['equipped'] = {5: {'id': 179, 'w': [1, 2, 3, 0, 0, 9]},
                            15: {'id': 1234, 'w': [5, 0, 0, 0, 0, 0]},     # a 2008 slot-15 item
                            16: {'id': 3000, 'w': [7, 0, 0, 0, 0, 0]}}
        char['cash_equip'] = [0, 3101, 0, 0, 0, 0, 0, 0, 3108]
        session = {'uid': 1, 'hp': 50, 'mp': 20}
        rec = R.player_record(session, char, {'uid': 1}, client_build=B9)
        self.assertEqual(P.unknown_fields('0x07', R.player_list(rec), client_build=B9), [])
        grid = rec['repeat[15]']
        self.assertEqual(len(grid), 15)
        self.assertEqual((grid[5]['equip_item_id'], [w['equip_item_attr'] for w in grid[5]['repeat[6]']]),
                         (179, [1, 2, 3, 0, 0, 9]))
        cash = [(r['cash_equip_item_id'], r['cash_equip_item_attr0']) for r in rec['repeat[10]']]
        # slot 15 = the 2009 pet slot (always empty: no pets), 16 from the grid with its word 0,
        # 17..24 from the 2008 +0x15C list the 2008 build sends them in
        self.assertEqual(cash, [(0, 0), (3000, 7), (3101, 0), (0, 0), (0, 0), (0, 0), (0, 0), (0, 0),
                                (0, 0), (3108, 0)])
        own = P.build('0x07', R.player_list(rec), client_build=B9, receiver_uid=1)
        self.assertEqual(len(own), 1 + 382)
        back = P.parse('0x07', own, direction='S2C', client_build=B9)['repeat[player_count]'][0]
        self.assertEqual(back['repeat[10]'][1], {'cash_equip_item_id': 3000, 'cash_equip_item_attr0': 7})
        # the 2008 record of the same character is untouched (16 + 9)
        rec8 = R.player_record(session, char, {'uid': 1})
        self.assertEqual((len(rec8['repeat[16]']), len(rec8['repeat[9]'])), (16, 9))
        self.assertEqual(P.unknown_fields('0x07', R.player_list(rec8)), [])
        self.assertEqual(len(P.build('0x07', R.player_list(rec8))), 1 + 368)

    def test_0x04_pet_block_per_receiver_and_0x05_always(self):
        char = self.hero()
        rec = R.player_record({'uid': 1, 'hp': 50, 'mp': 20}, char, {'uid': 1}, client_build=B9,
                              remote=True)
        other = dict(rec, uid=2, name='Other')
        remote = P.build('0x04', R.player_list(rec), client_build=B9, receiver_uid=2)
        self.assertEqual(len(remote), 1 + 383)                       # + has_pet
        self.assertEqual(P.parse('0x04', remote, direction='S2C', client_build=B9)
                         ['repeat[player_count]'][0]['has_pet'], 0)
        both = P.build('0x04', R.player_list(rec, other), client_build=B9, receiver_uid=3)
        self.assertEqual(len(both), 1 + 2 * 383)
        with self.assertRaises(P.MissingAssume):                      # the receiver's own row mixed in
            P.build('0x04', R.player_list(rec, other), client_build=B9, receiver_uid=2)
        # 0x05: the pet block is read unconditionally, no stall block, no buff ground point
        rec['buff_count'] = 1
        rec['repeat[buff_count]'] = [{'buff_skill_id': 0x0A31, 'buff_duration': 5000,
                                      'buff_param_a': 10, 'buff_param_b': 20}]
        row = R.to_0x05(rec, B9)
        self.assertEqual(P.unknown_fields('0x05', row, client_build=B9), [])
        body = P.build('0x05', row, client_build=B9, receiver_uid=1)
        self.assertEqual(len(body), 382 + 6 - 1 + 1)                 # + buff 6, - shop_open, + has_pet
        parsed = P.parse('0x05', body, direction='S2C', client_build=B9)
        self.assertEqual((parsed['name'], parsed['has_pet'], len(parsed['repeat[17]'])), ('TestHero', 0, 17))
        # the 2008 0x05 is still the renamed 2008 row
        rec8 = R.player_record({'uid': 1, 'hp': 50, 'mp': 20}, char, {'uid': 1}, remote=True)
        self.assertEqual(P.unknown_fields('0x05', R.to_0x05(rec8)), [])
        self.assertEqual(len(P.build('0x05', R.to_0x05(rec8))), 367)

    def test_x_trap_0xC5_is_never_sent(self):
        self.assertIsNotNone(P.forbidden_reason(0xC5, B9))
        self.assertIsNone(P.forbidden_reason(0xC5, '2008'))
        with self.assertRaises(P.PacketError):
            P.send(self.server, None, {}, '0xC5', {})
        with self.assertRaises(P.PacketError):
            self.server._send_encrypted(None, {}, 0xC5, bytes(128))      # raw / admin injection too

    def test_no_server_module_sends_0xC5_or_the_old_0xA5(self):
        # spec_2009 0xA5 = "load an instance dungeon stage"; the 2008 {u8, u32} form is gone.
        # A packet key is a quoted '0xA5' / '0xC5'; an opcode int is a bare 0xA5 / 0xC5 outside
        # any string (a log or route text may name them).
        key = re.compile(r"""['"]0x(?:A5|C5|a5|c5)['"]""")
        bare = re.compile(r'(?<!\w)0x(?:A5|C5|a5|c5)(?!\w)')
        strings = re.compile(r"""'[^'\n]*'|"[^"\n]*\"""")
        for module in ('windslayer_server.py', 'registry.py', 'records.py', 'gm.py', 'inventory.py',
                       'quests.py', 'combat.py', 'skills.py', 'buffs.py', 'debuffs.py', 'world.py',
                       'hpmp.py'):
            with self.subTest(module=module):
                hits = [line.strip() for line in _code_lines(module)
                        if key.search(line) or bare.search(strings.sub("''", line))]
                self.assertEqual(hits, [])

    # S2C keys the server can emit that the 2009 client has no handler for (s2c_format_diff
    # gone_in_2009). Each one is build-gated, and the gate has its own test here.
    GATED_2008_ONLY = {
        '0x5E': 'map-load lead -> 0x08 reason 1 (test_0x08_carries_the_2009_reason_byte...)',
        '0xA3': 'map-load lead -> 0x08 reason 3',
        '0xA4': 'map-load lead -> 0x08 reason 4',
        '0x5F': 'cast refusal -> nothing (Routes2009.test_a_refused_cast_is_not_answered)',
        '0x8C': 'ID transfer -> nothing (Routes2009.test_id_transfer_is_dropped)',
    }

    def test_every_s2c_the_server_emits_exists_in_the_2009_spec_or_is_gated(self):
        keys = set()
        for module in ('windslayer_server.py', 'registry.py', 'records.py', 'gm.py'):
            text = '\n'.join(_code_lines(module))
            keys |= set(re.findall(r"P\.send\([^()]*?'(0x[0-9A-F]{2})'", text))
            keys |= set(re.findall(r"_reply\('(0x[0-9A-F]{2})'", text))
            keys |= set(re.findall(r"\(\s*'(0x[0-9A-F]{2})',\s*[{(]", text))
            keys |= set(re.findall(r"P\.build\(\s*'(0x[0-9A-F]{2})'", text))
            keys |= set(re.findall(r"_chat_line\('(0x[0-9A-F]{2})'", text))
        keys |= set(W.GameServer.LEAD_OPCODES)
        self.assertGreater(len(keys), 40)
        missing = sorted(k for k in keys if k not in P._db(B9).by_key)
        self.assertEqual(set(missing), set(self.GATED_2008_ONLY) & set(missing))
        self.assertTrue(set(missing) <= set(self.GATED_2008_ONLY), missing)
        for key in keys - set(self.GATED_2008_ONLY):
            P.spec(key, client_build=B9)

    def test_every_2009_receiver_state_gate_has_a_default(self):
        self.assertEqual(P.check_assume_table(B9), [])
        db, table = P._db(B9), P.assume_table(B9)
        for s in db.s2c.values():
            key = s['key']
            if key in P.NO_DEFAULT_ASSUME_2009 or key in P._DERIVED_U32:
                continue
            with self.subTest(key=key):
                exprs = db.grammar(key).client_state_exprs()
                self.assertEqual([e for e in exprs if e not in table.get(key, {})], [])

    def test_registry_tables_per_build(self):
        # 2008: the module tables themselves (a patch of MUST_REPLY still reaches dispatch)
        self.assertIs(registry.must_reply_table(), registry.MUST_REPLY)
        self.assertIs(registry.must_reply_table('2008'), registry.MUST_REPLY)
        self.assertIs(registry.never_reply_table(), registry.NEVER_REPLY)
        never9 = registry.never_reply_table(B9)
        self.assertNotIn(0x2D, never9)                                # now the dungeon room kick
        self.assertNotIn(0x75, never9)
        self.assertIn(0x9E, never9)
        must9 = registry.must_reply_table(B9)
        session = {'username': 'test', 'uid': 1, 'in_world': True}
        for op, policy in must9.items():
            with self.subTest(opcode=f'0x{op:02X}'):
                self.assertTrue(P.variants(op, 'C2S', client_build=B9), 'policy row without a 2009 C2S grammar')
                if policy.refusal is None:
                    continue
                for key, fields, assume in policy.refusal(self.server, session, None):
                    if isinstance(fields, (bytes, bytearray)):
                        continue
                    self.assertEqual(P.unknown_fields(key, fields, client_build=B9), [])
                    P.build(key, fields, assume, client_build=B9, receiver_uid=1)
        self.assertEqual(must9[0x15].refusal(self.server, session, None), [])
        self.assertEqual(must9[0x47].refusal(self.server, session, None),
                         [('0x71', {'is_trade': 0, 'result': 0}, None)])

    def test_gm_subcommands_resolve_in_the_2009_spec(self):
        for sub, cmd in gm.SUBCOMMANDS_2009.items():
            with self.subTest(sub=sub):
                s = P.spec(cmd.key, 'C2S', client_build=B9)
                self.assertEqual(s['opcode'], '0x06')
        self.assertIs(gm.subcommands(), gm.SUBCOMMANDS)
        self.assertIs(gm.subcommands(B9), gm.SUBCOMMANDS_2009)

    def test_the_hand_packed_builders_are_gone(self):
        for name in ('_build_opcode_80', '_send_cash_balance', '_build_opcode_12_drop', '_build_opcode_13',
                     '_build_opcode_14', '_build_opcode_40', '_build_en_opcode_1D', '_build_en_opcode_1E'):
            with self.subTest(name):
                self.assertFalse(hasattr(W.GameServer, name))


# ==================================================================== routes ===
class Routes2009(ServerCase):
    # C2S opcodes both clients send with different meanings (spec_2009 diff_vs_2008).
    REPURPOSED = {0x08, 0x09, 0x0A, 0x2C, 0x2D, 0x93, 0x94, 0x95, 0x97, 0x99}

    def test_route_table_covers_every_new_2009_c2s(self):
        routes = W.GameServer.ROUTES_2009
        ops_2008 = {int(s['opcode'], 16) for s in P._db('2008').by_key.values() if s['direction'] == 'C2S'}
        self.assertEqual(registry.check_routes(routes, W.GameServer), [])
        self.assertIs(self.server.routes, routes)
        self.assertIs(F.make_server(self.tmp).routes, W.GameServer.ROUTES)
        db = P._db(B9)
        dead = W.GameServer.DEAD_C2S_KEYS[B9]
        for key, s in db.by_key.items():
            if s['direction'] != 'C2S' or key.startswith('UDP') or key in dead:
                continue
            op = int(s['opcode'], 16)
            # an opcode the 2008 client never sends, or one whose meaning changed (routes_2009)
            new = op not in ops_2008 or op in self.REPURPOSED
            with self.subTest(key=key):
                if new:
                    self.assertIn(op, routes, f'{key} {s["name"]} is new/repurposed in 2009 but has no route')
                elif op not in routes:
                    # same request as in 2008 and unhandled in both builds (later stages)
                    self.assertNotIn(op, W.GameServer.ROUTES)

    def test_new_2009_requests_are_consumed_without_unknown_or_errors(self):
        c, _ = self.enter()
        routes = self.server.routes
        db = P._db(B9)
        sent = 0
        for key, s in sorted(db.by_key.items()):
            if s['direction'] != 'C2S' or key.startswith('UDP'):
                continue
            op = int(s['opcode'], 16)
            route = routes.get(op)
            if route is None or route.handler is not None or op in W.GameServer.ROUTES:
                continue                        # only the new log-only consumes
            with self.subTest(key=key), self.assertLogs('WS', logging.INFO) as cm:
                c.send_c2s(key)
                c.expect_silence(0.1)
            self.assertFalse([line for line in cm.output if 'Unhandled opcode' in line or 'ERROR' in line
                              or 'matches no C2S grammar' in line], cm.output)
            self.assertTrue(any('consumed' in line for line in cm.output), cm.output)
            sent += 1
        self.assertGreater(sent, 25)

    def test_dead_room_host_sites_are_dropped_before_the_handlers(self):
        c, _ = self.enter()
        chars = len(self.server.store.characters('test'))
        for key in (DEAD_0C, DEAD_0E):
            with self.subTest(key=key), self.assertLogs('WS', logging.WARNING) as cm:
                c.send_c2s(key, {'room_id': 1, 'entity_uid': 2, 'skill_id': 3})
                c.expect_silence(0.15)
            self.assertTrue(any('unreachable in the EN 2009 exe' in line for line in cm.output), cm.output)
        self.assertEqual(len(self.server.store.characters('test')), chars)

    def test_after_every_0x03_the_client_trio_is_answered(self):
        c, _ = self.enter()
        with self.server.store.lock:
            st = Q.QuestState(self.hero())
            st.active[0], st.progress[0] = 26, 1
            self.hero()['card_deck'] = [2030]
        with self.assertNoLogs('WS', logging.ERROR):
            c.send_c2s(FRIENDS)
            c.send_c2s(CARDS)
            c.send_c2s(GUILD)
            # 0x99 sub 8: the connection's first 0x63 also prints the channel (P4 stage 4);
            # 0x0B: the friend list answers 0x2F since P6 stage 2 (social_friend F1)
            _friends, deck, progress, channel, guild = c.expect(0x0B, 0x8A, 0x59, 0x99, 0xB3)
        self.assertEqual(c.s2c(deck), {'deck_count': 1, 'repeat[deck_count]': [{'card_item_id': 2030}]})
        self.assertEqual(c.s2c(progress), {'slot': 1, 'progress': 1})
        self.assertEqual(c.s2c(channel), {'sub_type': 8, 'channel_no': 1})
        self.assertEqual(c.s2c(guild), {'sub': 15, 's15_result': 0})
        # the automatic re-request site is answered the same way - never with sub 19
        c.send_c2s(GUILD_AGAIN)
        self.assertEqual(c.s2c(c.expect(0xB3)), {'sub': 15, 's15_result': 0})
        c.expect_silence(0.2)

    def test_a_gm_keeps_the_game_master_nameplate_after_the_guild_answer(self):
        # live 2009: sub 15 zeroes entity+0x12, which also held the GM's 1 ("Game Master");
        # sub 37 {own uid} sets it back, and only for a visible GM
        c, _ = self.enter()
        c.session['gm'], c.session['gm_hidden'] = 1, 0
        c.send_c2s(GUILD)
        first, second = c.expect(0xB3, 0xB3)
        self.assertEqual(c.s2c(first), {'sub': 15, 's15_result': 0})
        self.assertEqual(c.s2c(second), {'sub': 37, 's37_uid': P.session_uid(c.session)})
        c.session['gm_hidden'] = 1                                    # hidden GM: no tag
        c.send_c2s(GUILD)
        self.assertEqual(c.s2c(c.expect(0xB3)), {'sub': 15, 's15_result': 0})
        c.expect_silence(0.2)

    def test_guild_info_outside_the_world_is_not_answered(self):
        c = self.client()
        self.login(c)
        c.send_c2s(GUILD)
        c.expect_silence(0.2)

    def test_room_list_windows(self):
        c, _ = self.enter()
        for list_type, (op, fields) in ((1, (0x33, {'room_count': 0, 'repeat[room_count]': []})),
                                        (4, (0xA1, {'room_count': 0, 'repeat[room_count]': []})),
                                        (5, (0xC2, {'total_rooms': 0, 'room_count': 0,
                                                    'repeat[room_count]': []}))):
            with self.subTest(list_type=list_type):
                c.send_c2s(ROOM_LIST, {'list_type': list_type})
                self.assertEqual(c.s2c(c.expect(op)), fields)
        c.send_c2s(ROOM_LIST, {'list_type': 0})                  # a list closed: never answered
        c.expect_silence(0.2)

    def test_a_refused_cast_is_not_answered(self):
        c, _ = self.enter()
        with self.assertLogs('WS', logging.INFO) as cm:
            c.send_c2s(SKILL, {'skill_id': 2188})                  # not learned
            c.expect_silence(0.2)
        self.assertTrue(any('refused with no reply' in line for line in cm.output), cm.output)

    def test_id_transfer_is_dropped(self):
        c = self.client()
        self.login(c)
        c.send_c2s(ID_TRANSFER, {'target_id': 'someone'})
        c.expect_silence(0.2)

    def test_cash_gift_refusal_has_the_2009_is_trade_byte(self):
        c, _ = self.enter()
        c.send_c2s(GIFT, {'item_code': 100, 'recipient_name': 'Bob'})
        pkt = c.expect(0x71)
        self.assertEqual(bytes(pkt.payload), b'\x00\x00')
        self.assertEqual(c.s2c(pkt), {'is_trade': 0, 'result': 0})

    def test_gm_notice_decodes_at_the_2009_send_site(self):
        with self.server.store.lock:
            self.hero()['gm'] = 1
        c, _ = self.enter()
        text = b'ice hello'
        c.send_c2s('0x446548/0x06', {'gm_subcmd': 1, 'text_len': len(text), 'notice_text': text})
        line = c.s2c(c.expect(0x15))
        self.assertIn(b'hello', P.to_bytes(line['text']))


# ============================================================ strict sweep ===
@needs_2009
class StrictSweep2009(ServerCase):
    """The everyday world requests of a 2009 client, run with packets.STRICT_FIELDS on for
    2009: every S2C the handlers build must use only 2009 field names (a handler that built
    one with a 2008 name would raise and log an ERROR) and every reply must decode exactly
    with the 2009 grammar."""

    def drain(self, c):
        pkts = c.recv_until_quiet()
        for pkt in pkts:
            c.s2c(pkt)                                               # exact, 2009 grammar
        return [p.opcode for p in pkts]

    def chat(self, c, text):
        text = text.encode('latin-1')
        c.send_c2s('0x44790E/0x03', {'msg_len': len(text), 'message': text})

    def test_items_shop_skills_death_and_revive(self):
        with self.server.store.lock:
            self.hero()['gm'] = 1
        c, _ = self.enter()
        steps = [
            ('give weapon', lambda: self.chat(c, '!give 179')),
            ('give herbs', lambda: self.chat(c, '!give 5 3')),
            ('equip', lambda: c.send_c2s('0x4500D1/0x0F', {'item_id': 179, 'stone_count': 0})),
            ('unequip', lambda: c.send_c2s('0x477368/0x11', {'item_id': 179, 'enchant_count': 0})),
            ('use herb', lambda: c.send_c2s('0x44FE04/0x15', {'item_id': 5})),
            ('shop buy', lambda: c.send_c2s('0x4745A4/0x0B', {'item_id': 5, 'qty': 1, 'npc_id': 8})),
            ('shop sell', lambda: c.send_c2s('0x4747C9/0x0C', {'item_id': 5, 'qty': 1})),
            ('stat point', lambda: c.send_c2s('0x44A9D6/0x04', {'stat_index': 1})),
            ('exp', lambda: self.chat(c, '!exp +500')),
            ('learn', lambda: self.chat(c, '!learn 80 force')),
            ('cast', lambda: c.send_c2s(SKILL, {'skill_id': 80})),
            ('hp', lambda: self.chat(c, '!hp 10')),
            ('die', lambda: self.chat(c, '!die')),
            ('revive', lambda: c.send_c2s('0x44A9D6/0x2E')),
            ('warp', lambda: self.chat(c, '!warp 102')),
        ]
        seen = {}
        for name, action in steps:
            with self.subTest(step=name), self.assertNoLogs('WS', logging.ERROR):
                action()
                seen[name] = self.drain(c)
        self.assertIn(0x18, seen['give weapon'])
        self.assertIn(0x3E, seen['die'])
        self.assertIn(0x08, seen['revive'])                           # the map load, 2009 0x08
        self.assertIn(0x1A, seen['warp'])


# =================================================================== content ===
@needs_2009
class Content2009(ServerCase):
    def test_item_table_and_names(self):
        self.assertEqual(EC.client_build(), B9)
        items = EC.items()
        self.assertEqual((len(items), items.max_id, EC.item_max_id(), items.client_build), (4322, 4322, 4322, B9))
        self.assertTrue(items.exists(4322))
        self.assertFalse(items.exists(4323))
        self.assertEqual(EC.item_name(179), 'Wooden Stick')
        self.assertEqual(EC.item_name(4294), 'Picky')              # a 2009 pet, no 2008 id

    def test_the_205_line_hni(self):
        text = EC.decode_hs_text(EC.hs_path('windslayer.hni'))
        self.assertEqual(len([line for line in text.splitlines() if line.strip()]), 205)
        npcs = EC.npcs()
        self.assertEqual(len(npcs), 204)
        self.assertEqual(npcs.template_index(1), 1)
        spawns = EC.map_spawns(102)
        self.assertEqual([sp.npc for sp in spawns], [1] * 8)

    def test_288_maps_including_the_instance_dungeons(self):
        inv = M.hmi_inventory()
        self.assertEqual({k: len(v) for k, v in inv.items()}, {'stage': 266, 'indun': 18, 'other': 4})
        self.assertEqual(sum(len(v) for v in inv.values()), 288)
        self.assertEqual([c for c in M.map_codes() if M.load_map(c) is None], [])
        self.assertEqual([c for c in M.indun_codes() if M.load_indun(c) is None], [])
        # indun codes are their own namespace: stage 105 and indun 105 are different files
        self.assertNotEqual(M.map_path(105), M.indun_path(105))

    def test_the_2009_portal_table(self):
        self.assertEqual(os.path.basename(EC.portals_source()), 'portals_en_2009.json')
        self.assertEqual(len(EC.portals()), 579)
        rows, _ = M.build_portal_table()
        with open(EC.portals_en_path(), encoding='utf-8') as f:
            stored = json.load(f)
        self.assertEqual(stored['portals'], json.loads(json.dumps(rows)),
                         'portals_en_2009.json is stale: re-run `python en_maps.py --build 2009`')
        self.assertEqual(M.check_live_portals(rows), [])
        self.assertEqual((EC.portal(101, 23)[0], EC.portal(102, 31)[0], EC.portal(102, 17)[0]), (102, 101, 103))
        self.assertIsNone(EC.portal(201, 192))                        # a 2008-only line

    def test_the_2009_kind_to_slot_switch(self):
        bag = INV.Inventory({})
        self.assertEqual(bag.catalog.client_build, B9)
        self.assertEqual(bag.slot_for(179), 5)                        # weapon: same in both builds
        self.assertEqual(bag.slot_for(1343), 7)                       # ring (Kind 17): FUN_00427af0 -> 7
        self.assertIsNone(bag.slot_for(1443))                         # belt (Kind 14): no 2009 case
        self.assertEqual(bag.slot_for(4294), 15)                      # pet (Kind 14 cash): the pet slot
        self.assertEqual((bag.slot_for(4292), bag.slot_for(4290)), (23, 24))   # pet hat / glasses

    def test_the_2008_tables_come_back_with_a_2008_server(self):
        F.make_server(self.tmp)
        self.assertEqual(EC.client_build(), '2008')
        self.assertEqual((len(EC.items()), EC.item_max_id(), len(EC.portals())), (4248, 4248, 593))
        bag = INV.Inventory({})
        self.assertEqual((bag.slot_for(1343), bag.slot_for(1443)), (13, 7))
        self.assertEqual(os.path.basename(EC.portals_source()), 'portals_en.json')


# ================================================================ world flow ===
@needs_2009
class WorldFlow2009(ServerCase):
    """P2/P3 exit flow on the 2009 client: world, portals, a reported hit that kills."""

    def mobs(self, pkts):
        rows = []
        for pkt in pkts:
            self.assertEqual(pkt.opcode, 0x1A)
            rows += self.clients[-1].s2c(pkt)['repeat[count]']
        return rows

    def portal(self, c, index, dest, mobs=0):
        c.send_c2s(PORTAL, {'portal_line_index': index})
        pkts = c.expect(0x08, 0x03, 0x07, 0x28, 0x44, *([0x1A] if mobs else []))
        lead = c.s2c(pkts[0])
        self.assertEqual(len(pkts[0].payload), 7)
        self.assertEqual((lead['reason'], lead['map_code']), (0, dest))
        state = c.s2c(pkts[1])
        self.assertEqual(state['map_code'], dest)
        me = c.s2c(pkts[2])['repeat[player_count]'][0]
        self.assertEqual((me['uid'], len(pkts[2].payload)), (1, 1 + 382))
        for pkt in pkts[3:5]:
            c.s2c(pkt)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world') and s.get('current_map') == dest))
        return pkts

    def report_hit(self, c, mob):
        """C2S 0x0D as the 2009 client sends it the tick after its own hit detection caught a
        swing (spec_2009 0x42E704/0x0D, identical to 2008): interact event 7 in state_lo bits
        16-19 and the victim in the 43 B tail - 61 B."""
        payload = c.send_c2s(MOVE, {
            'realtime_delta_ms': 0, 'map_code': 102, 'logic_elapsed_ms': 30,
            'state_lo': W.HIT_REPORT_EVENT << 16, 'state_hi': 0, 'target_uid': mob.uid,
            'pos_x': mob.x, 'pos_y': mob.y, 'target_dx': 0.0, 'target_dy': 0.0,
            'flag_8db': False, 'flag_8e7': False, 'timer_dac': 0, 'target_action_event': 7})
        self.assertEqual(len(payload), 61)

    def test_world_portals_and_a_reported_kill(self):
        arrival = EC.portal(101, 23)
        self.assertEqual(arrival[0], 102)
        c, pkts = self.enter(map_code=102, pos=arrival[1:], mobs=1)
        state, spawn = c.s2c(pkts[0]), c.s2c(pkts[1])
        self.assertEqual((len(pkts[0].payload), state['map_code'], len(state['repeat[5]'])), (71, 102, 5))
        self.assertEqual(len(pkts[1].payload), 1 + 382)                  # own record: no pet block
        self.assertEqual(spawn['repeat[player_count]'][0]['uid'], 1)
        for pkt in pkts[2:6]:                          # 0x15, 0x65 (bank block), 0x28, 0x44
            c.s2c(pkt)

        # the map's 8 Pupu, in one 0x1A, exact under the 2009 grammar
        rows = self.mobs(pkts[6:])
        tpl = EC.npcs().get(1)
        self.assertEqual(len(rows), 8)
        self.assertEqual({r['template_index'] for r in rows}, {EC.npcs().template_index(1)})
        self.assertEqual([r['uid'] for r in rows], [W.MOB_UID_BASE + i for i in range(8)])
        self.assertTrue(all((r['action_state'], r['unk_95b'], r['unk_949'], r['cur_hp']) == (8, 8, 2, tpl.hp)
                            for r in rows))
        spawns = EC.map_spawns(102)
        self.assertEqual([(r['pos_x'], r['pos_y']) for r in rows], [(float(s.x), float(s.y)) for s in spawns])

        # what the 2009 client sends by itself after the 0x03
        c.send_c2s(FRIENDS)
        c.send_c2s(CARDS)
        c.send_c2s(GUILD)
        # 0x0B: the friend list (P6 stage 2, social_friend F1)
        self.assertEqual(c.s2c(c.expect(0x0B, 0x8A, 0x99, 0xB3)[3]), {'sub': 15, 's15_result': 0})

        # portal to 101 (town: no monsters) and back to 102 (the Pupu again)
        back = [(k, v) for k, v in EC.portals().items() if k.startswith('102_') and v[0] == 101]
        self.assertTrue(back)
        index = int(back[0][0].split('_')[1])
        self.portal(c, index, 101)
        c.expect_silence(0.2)
        pkts = self.portal(c, 23, 102, mobs=1)
        self.assertEqual(len(self.mobs(pkts[5:])), 8)

        # a Pupu killed by the client's own hit reports: 0x2A releases while it lives, then
        # 0x29 (hold tick + spawn point), 0x21 exp (no guild tail), 0x18 gold + the drop
        uid = P.session_uid(c.session)
        mob = c.session['monsters'][W.MOB_UID_BASE + 1]
        exp0 = self.hero()['exp']
        gold0 = W.invmod.Wallet(self.hero()).gold
        releases = 0
        with mock.patch.object(W.random, 'random', return_value=0.0), \
                mock.patch.object(W.random, 'choice', side_effect=lambda seq: seq[0]):
            while True:
                self.report_hit(c, mob)
                pkts = c.recv_until_quiet()
                if not mob.alive:
                    break
                self.assertEqual([p.opcode for p in pkts], [0x2A])
                self.assertEqual(c.s2c(pkts[0]), {'mover_uid': mob.uid, 'move_bits': bytes(8),
                                                  'target_uid': uid})
                releases += 1
                self.assertLess(releases, 30)
        self.assertGreater(releases, 0)
        self.assertEqual([p.opcode for p in pkts], [0x29, 0x21, 0x18])
        self.assertEqual(c.s2c(pkts[0]), {'uid': mob.uid, 'respawn_tick': 0x7FFFFFFF,
                                          'respawn_x': int(mob.spawn_x), 'respawn_y': int(mob.spawn_y)})
        self.assertEqual(len(pkts[1].payload), 4)
        self.assertEqual(c.s2c(pkts[1]), {'exp_delta': tpl.exp})
        drop = c.s2c(pkts[2])
        self.assertEqual((drop['gold'], drop['item_id'], drop['count']),
                         (gold0 + W.monster_gold(1, tpl.exp), tpl.drop_ids[0], 1))
        self.assertEqual(self.hero()['exp'], exp0 + tpl.exp)
        self.assertEqual(self.server._inventory(c.session).get(tpl.drop_ids[0]), 1)
        self.report_hit(c, mob)                                    # a corpse takes no hit
        c.expect_silence(0.2)

        # corpse despawn (0x06) and respawn (0x1A in the 2009 grammar) from the tick scheduler
        # (run_due also runs the store's debounced save of the temp accounts file)
        now = time.monotonic()
        self.assertGreaterEqual(self.server.ticks.run_due(now + W.MOB_CORPSE_SECS + 0.5), 1)
        self.assertEqual(c.s2c(c.expect(0x06)), {'uid': mob.uid})
        self.assertGreaterEqual(self.server.ticks.run_due(now + W.MOB_RESPAWN_SECS + 0.5), 1)
        row = self.mobs([c.expect(0x1A)])[0]
        self.assertEqual((row['uid'], row['cur_hp'], row['pos_x'], row['unk_95b']),
                         (mob.uid, tpl.hp, float(mob.spawn_x), 8))


if __name__ == '__main__':
    unittest.main()
