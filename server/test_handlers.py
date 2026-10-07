#!/usr/bin/env python3
"""
test_handlers.py - offline handler tests over a fake client (roadmap 1.10 F9;
chat_mail_gm-offline-dispatch-tests generalized; arch-handler-registry; admin injector)

Every test drives the real GameServer._handle_fireway loop through fakeclient.FakeClient
(socket.socketpair + CEncMsg NoEncode/EncodebyArray framing, like the EN client). C2S
payloads are built from protocol_spec.json send-site grammars or taken from live
captures (LIVE_TEST_LOG.md); every S2C reply is decoded with packets.parse and must
consume exactly its bytes, except the known server-builder deviations listed in
KNOWN_S2C_DEVIATIONS (each names its bug id and the item that fixes it; the test fails
once the bug is fixed so the entry gets removed).

No port is bound, no game client is needed, the live accounts.json is never opened
(each server gets a temp copy) and the server's logging is not configured, so
server_live.log / server_history.log are untouched.
"""
import hashlib
import json
import logging
import os
import random
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import en_content as EC  # noqa: E402
import fakeclient as F  # noqa: E402
import inventory as INV  # noqa: E402
import packets as P  # noqa: E402
import registry  # noqa: E402
import ticks  # noqa: E402
from wsproto import hexbytes  # noqa: E402

W = F.import_server()
logging.getLogger('WS').setLevel(logging.WARNING)
# A handler on 'WS' keeps expected warnings off stderr (logging.lastResort); assertLogs
# still captures them.
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')

# Live captures (LIVE_TEST_LOG.md, 2026-09-17), payload after the opcode byte.
CAP_0D_JUMP = hexbytes('00 00 00 00 65 00 60 09 00 00 0C 00 40 72 00 50 41 70')
CAP_7E_101_TO_102 = hexbytes('17 00 00 00')
CAP_0F_EQUIP_STICK = hexbytes('B3 00 00 00 00')
CAP_03_HI = hexbytes('02 68 69')

# S2C opcode -> (trailing bytes after an exact grammar decode, bug id, fixing item).
# The server builders for these still send bytes the client ignores or misreads.
# 0x02 left this table with lc-charlist (13 + 75n bytes, no 16 trailing).
# Empty: S3-04 (the 0x08 body carried the uid plus 4 ignored bytes) is fixed by
# world-maptransfer, which builds all four map-load leads from the 6-byte 0x08 grammar.
KNOWN_S2C_DEVIATIONS = {}


def struct_i32(v):
    return int(v).to_bytes(4, 'little', signed=True)


def _wait(pred, timeout=3.0):
    """Poll a server-side condition written by the connection thread."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


_LIVE_HASH = None


def setUpModule():
    global _LIVE_HASH
    _LIVE_HASH = _sha(LIVE_ACCOUNTS) if os.path.exists(LIVE_ACCOUNTS) else None


def tearDownModule():
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_handlers.py (tests must only use temp copies)'


class ServerTest(unittest.TestCase):
    """A fresh GameServer on a temp accounts.json per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_handlers_')
        self.server = F.make_server(self.tmp)
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self):
        c = F.FakeClient(self.server)
        self.clients.append(c)
        return c

    # ------------------------------------------------------------ decoding ---
    def s2c(self, pkt, length=None):
        """Decode an S2C packet with its grammar; exact unless a known deviation."""
        if length is not None:
            self.assertEqual(len(pkt.payload), length, f'S2C 0x{pkt.opcode:02X} length')
        dev = KNOWN_S2C_DEVIATIONS.get(pkt.opcode)
        if dev is None:
            return F.FakeClient.decode(pkt)
        trailing, bug, item = dev
        n = trailing(pkt.payload)
        if n == 0:
            return F.FakeClient.decode(pkt)
        try:
            F.FakeClient.decode(pkt)
        except P.ParseError as e:
            self.assertIn(f'{n} trailing byte', str(e), f'S2C 0x{pkt.opcode:02X} ({bug}) changed shape')
        else:
            self.fail(f'S2C 0x{pkt.opcode:02X} now decodes exactly: {bug} fixed by {item}? '
                      f'Remove it from KNOWN_S2C_DEVIATIONS.')
        return F.FakeClient.decode(pkt, allow_trailing=True)

    # --------------------------------------------------------------- flows ---
    def login(self, c, user='test', password='test'):
        c.send_c2s('0x44D8BF/0x01', {'account_id': user, 'password': password})
        pkt = c.expect(0x02)
        rec = self.s2c(pkt)
        self.assertEqual(rec['result'], 1)
        return rec

    def _entry_map(self, c, name):
        """The map `name` enters on (its saved map, START_MAP for 0), for expect_entry."""
        char = self.server.store.find_character((c.session or {}).get('username'), name) or {}
        return int(char.get('map') or self.server.config.START_MAP)

    def enter_world(self, c, name='TestHero', monsters=0, state_len=64):
        """Enter the world and consume the spawn cluster. `monsters` is how many S2C 0x1A
        follow: world-persistence means a relog loads the map the character logged out on,
        so a character left on 102 comes back to its 8 Pupu.

        state_len is the 0x03 length: 64 B is the empty form (no bag, no quests). A
        character that owns anything sends more, because 0x03 now carries the real bag and
        quest log (world-03-real-state), so those tests pass None."""
        map_code = self._entry_map(c, name)
        c.send_c2s('0x42F904/0x2B', {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907, 'char_name': name})
        # 0x15 [Announce] welcome on the first entry (chat_mail_gm-system-notices; was a 41 B 0x0A);
        # world-presence (P5 stage 2): + one 0x04 when another client is in world on that map
        # (and a 0x05 to it), which expect_entry checks and leaves out of the returned list.
        pkts = F.expect_entry(c, (0x03, 0x07, 0x15, 0x28, 0x44, *F.mob_packets(monsters)),
                              self.clients, map_code)
        for pkt, length in zip(pkts, (state_len, 369, 35, 2, 2)):
            self.s2c(pkt, length)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world')))
        return pkts


class LoginWorldPortal(ServerTest):
    def test_login_enter_world_move_portal(self):
        c = self.client()
        rec = self.login(c)
        self.assertEqual(rec['account_id'], 1)
        self.assertEqual([ch['name'] for ch in rec['repeat[char_count]']], ['TestHero'])
        self.assertFalse(c.session['in_world'])

        _, spawn, _, hp, mp = self.enter_world(c)
        body = self.s2c(spawn)
        self.assertEqual(body['repeat[player_count]'][0]['name'], 'TestHero')
        self.assertEqual(self.s2c(hp)['hp'], 100)
        self.assertEqual(c.session['char_name'], 'TestHero')
        self.assertEqual(c.session['current_map'], 101)

        # Movement is consumed silently: an echo would desync the client cipher.
        c.send(0x0D, CAP_0D_JUMP)
        c.expect_silence()
        c.send(0x05)                                         # keepalive: never answered
        c.expect_silence()

        # Portal 101 -> 102 replays the map load (live: 0x08, 0x03 64B, 0x07 369B, 0x0A,
        # 0x28, 0x44, 0x1A x8 89B). The "Welcome!" 0x0A whisper is gone (S3-02), and the
        # eight Pupu now share ONE 0x1A: 1 + 8 x 86 B (idle defaults, server_controlled,
        # no effect entry; world-1a-defaults).
        c.send(0x7E, CAP_7E_101_TO_102)
        pkts = c.expect(0x08, 0x03, 0x07, 0x28, 0x44, 0x1A)
        self.assertEqual(self.s2c(pkts[0])['map_code'], 102)
        for pkt, length in zip(pkts[1:5], (64, 369, 2, 2)):
            self.s2c(pkt, length)
        blocks = self.s2c(pkts[5], 1 + 8 * 86)['repeat[count]']
        self.assertEqual([b['uid'] for b in blocks], [W.MOB_UID_BASE + i for i in range(8)])
        self.assertEqual({b['template_index'] for b in blocks}, {1})         # Pupu
        self.assertEqual(c.session['current_map'], 102)
        self.assertTrue(c.session['in_world'])

    def test_bad_password(self):
        c = self.client()
        c.send_c2s('0x44D8BF/0x01', {'account_id': 'test', 'password': 'bad'})
        rec = self.s2c(c.expect(0x02), 1)
        self.assertEqual(rec['result'], 0x11)
        self.assertIsNone(c.session['username'])

    def test_second_login_is_ignored(self):
        c = self.client()
        self.login(c)
        c.send_c2s('0x44D8BF/0x01', {'account_id': 'test', 'password': 'test'})
        c.expect_silence()

    def test_unknown_portal_is_ignored(self):
        c = self.client()
        self.login(c)
        self.enter_world(c)
        c.send_c2s('0x42F76B/0x7E', {'portal_line_index': 999})
        c.expect_silence()
        self.assertEqual(c.session['current_map'], 101)
        self.assertTrue(c.session['in_world'])

    def test_create_character_writes_only_the_temp_db(self):
        c = self.client()
        self.login(c)
        payload = P.build('0x449384/0x0E',
                          {'look_slot10': 1, 'look_slot1': 1, 'look_slot6': 2, 'look_slot5': 2,
                           'look_slot9': 2, 'name': 'Probe1', 'str': 3, 'dex': 2, 'int': 1, 'tol': 3},
                          direction='C2S')
        self.assertEqual(len(payload), 35)
        c.send(0x0E, payload)
        # lc-create: S2C 0x1C result 1, no second 0x02 (it would duplicate the select
        # entities, live login_character#03/#05).
        self.assertEqual(self.s2c(c.expect(0x1C), 1)['result'], 1)
        with open(self.server.db_file, encoding='utf-8') as f:
            self.assertEqual([ch['name'] for ch in json.load(f)['test']['characters']], ['TestHero', 'Probe1'])
        self.assertNotEqual(os.path.abspath(self.server.db_file), os.path.abspath(LIVE_ACCOUNTS))


class ExistingHandlers(ServerTest):
    """Pins today's in-world handlers so the registry refactor provably changed nothing.
    Bug references mark replies later P0 stages change on purpose."""

    def setUp(self):
        super().setUp()
        self.c = self.client()
        self.login(self.c)
        self.enter_world(self.c)

    def test_quest_accept_then_equip_from_bag(self):
        c = self.c
        c.send_c2s('0x47734D/0x16', {'quest_id': 26})
        # live before stage 4: 0x26 2B, 0x18 16B (duplicate stick, S2-09), 0x16 37B (blank, S2-05).
        # The 0x59 {slot 1, 0} arms the counter of the quest's ReqPro 1 (kill one Pupu): the
        # EN hqi carries that, the old hex parse did not (Q-B1). The "Quest 26 accepted."
        # chat line is gone too (quest doc F1 step 7 / Q-B19): the client prints its own
        # "you've received Wooden Stick" on 0x26.
        grant, progress = c.expect(0x26, 0x59)
        self.assertEqual(self.s2c(progress, 2), {'slot': 1, 'progress': 0})
        self.assertEqual(self.s2c(grant, 2)['quest_id'], 26)
        c.send(0x0F, CAP_0F_EQUIP_STICK)
        equip = c.expect(0x1D)                                # live before: 0x1D 9B + 0x23 7B (S2-07)
        self.assertEqual(self.s2c(equip, 9)['item_id'], 179)

    def test_chat_echo(self):
        self.c.send(0x03, CAP_03_HI)                          # was 21 B, shown as "test :" (S2-05/S2-06)
        self.assertEqual(self.s2c(self.c.expect(0x16), 20)['sender_name'], 'TestHero')

    def test_consume_only_routes_stay_silent(self):
        # 0x2C arena list, 0x2D arena list close, 0x6C trap triggered (cs-dispatch-fix): no
        # reply, no refusal. 0x2F (the messenger friend list, S2-03 relabel) is answered
        # with S2C 0x0B since P6 stage 2 (social_friend-friend-list-sync): test_messenger.py.
        for op, fields in ((0x2C, {}), (0x2D, {}), (0x6C, {'trap_skill_id': 2609})):
            with self.subTest(opcode=f'0x{op:02X}'):
                self.c.send(op, P.build(P.variants(op, 'C2S')[0]['key'], fields, direction='C2S'))
                self.c.expect_silence(0.25)


class RegistryPolicy(ServerTest):
    def test_route_table_is_consistent(self):
        self.assertEqual(registry.check_routes(W.GameServer.ROUTES, W.GameServer), [])
        for op in list(registry.MUST_REPLY) + list(registry.NEVER_REPLY):
            with self.subTest(opcode=f'0x{op:02X}'):
                self.assertTrue(P.variants(op, 'C2S'), 'policy row without a C2S grammar')
        self.assertFalse(set(registry.MUST_REPLY) & set(registry.NEVER_REPLY))
        for op, m in registry.MUST_REPLY.items():
            route = W.GameServer.ROUTES.get(op)
            if m.refusal is None:
                continue
            with self.subTest(opcode=f'0x{op:02X}'):
                for key, fields, _ in m.refusal(self.server, {'username': 'test', 'account_id': 1}, None):
                    P.spec(key, 'S2C')                        # refusal keys are TCP S2C packets
            if route is not None and not route.fallback:
                self.assertTrue(route.note, f'0x{op:02X}: a suppressed refusal needs a note naming its owner')

    def test_never_reply_requests_are_silent(self):
        c = self.client()
        self.login(c)
        self.enter_world(c)
        for op in sorted(registry.NEVER_REPLY):
            with self.subTest(opcode=f'0x{op:02X}'):
                c.send(op, P.build(P.variants(op, 'C2S')[0]['key'], {}, direction='C2S'))
                c.expect_silence(0.15)

    # Sample C2S fields and the exact refusal each must produce: (fields, [(S2C opcode,
    # length, {field: value})]). Every enabled MUST_REPLY row must appear here.
    REFUSALS = {
        0x15: ({'skill_id': 2188}, [(0x5F, 0, {})]),                   # Reinforce (EN Type 3)
        0x18: ({'room_title': 'r', 'room_type': 1}, [(0x30, 0, {})]),
        0x1A: ({'room_no': 3, 'room_type': 1}, [(0x34, 2, {'result': 2, 'room_type': 1})]),
        0x1B: ({'room_no': 5, 'room_kind': 2}, [(0x34, 2, {'result': 2, 'room_type': 2})]),
        0x1C: ({'room_no': 5, 'room_category': 4}, [(0x34, 2, {'result': 2, 'room_type': 4})]),
        0x39: ({'battle_type': 1}, [(0x39, 0, {}), (0x20, 1, {'waiting_count': 0})]),
        0x3A: ({}, [(0x20, 1, {'waiting_count': 0})]),
        0x47: ({'item_code': 100, 'recipient_name': 'Bob'}, [(0x71, 1, {'result': 0})]),
        0x48: ({'item_id': 3000}, [(0x72, 10, {'player_uid': 1, 'item_id': 0})]),
        0x49: ({'new_name': 'Renamed'}, [(0x73, 1, {'result': 0})]),
        # premium_cash-stat-reset (P8 stage 3): item 0 is no stat reset item -> the 18 B owner
        # form with the stored stats (the migrated TestHero's 3/2/1/3), serial 0, count 0.
        0x4A: ({}, [(0x76, 18, {'target_uid': 1, 'str': 3, 'dex': 2, 'int': 1, 'spi': 3,
                                'cash_item_serial': 0, 'consume_count': 0})]),
        0x4B: ({'recipient_name': 'Bob'}, [(0x77, 1, {'result': 0})]),
        0x46: ({}, [(0x70, 9, {'cash_balance': 0, 'mileage_balance': 0, 'first_purchase_bonus': 0})]),
        0x51: ({'password': 'x', 'target_window_id': 0x1A7}, [(0x80, 1, {'result': 0})]),
        # shop_storage-npc-buy: npc 1 (Pupu) is no merchant -> resync + warning, never a grant
        0x0B: ({'item_id': 5, 'qty': 1, 'npc_id': 1},
               [(0x18, 16, {'gold': W.storemod.DEFAULT_GOLD, 'victy': W.storemod.DEFAULT_VICTY,
                            'item_id': 0, 'count': 0}),
                (0x15, None, {'msg_type': 2})]),
        0x5E: ({}, [(0x82, 1, {'result': 2})]),
        0x5F: ({}, [(0x83, 1, {'result': 1})]),
        0x61: ({'stall_owner_uid': 7}, [(0x87, 1, {'result': 0})]),
        0x62: ({'seller_uid': 7, 'item_id': 5, 'qty': 1}, [(0x88, 1, {'result': 0})]),
        # shop_storage-sell-parse: not owned -> resync (item 0 grants nothing) + warning, never 0x19
        0x0C: ({'item_id': 3, 'qty': 5},
               # The resync carries the STORED wallet (shop_storage-wallet); the old
               # 999999/999999 session defaults made the label jump on the first refusal (B2).
               [(0x18, 16, {'gold': W.storemod.DEFAULT_GOLD, 'victy': W.storemod.DEFAULT_VICTY,
                            'item_id': 0, 'count': 0}),
                (0x15, None, {'msg_type': 2})]),
        0x24: ({}, [(0x49, 0, {})]),
        0x25: ({'my_gold': 0}, [(0x49, 0, {})]),
        0x5D: ({'village_index': 1, 'fee': 100}, [(0x81, 1, {'result': 0})]),
        0x70: ({'item_id': 3400, 'dest_map_id': 101}, [(0x9A, 1, {'result': 0})]),
        0x71: ({'item_id': 3429, 'friend_name': 'Bob'}, [(0x9B, 1, {'result': 0})]),
    }

    # Refusals that depend on session state, tested by their own classes.
    STATEFUL_REFUSALS = {0x60: 'StallStub (0x84 only while a 0x5E is on record)',
                         0x2E: 'test_combat.Death (0x3E only while dead: the revive backstop)'}

    # Rows whose full handler always answers, so the refusal is only the exception
    # backstop (test_handler_exception_still_sends_refusal_and_keeps_the_connection).
    HANDLED_MUST_REPLY = {
        0x0E: 'CharacterSelectFlows: lc-create answers 0x1C 1/2/3/4',
        0x12: 'CharacterSelectFlows: lc-delete answers 0x1F 1/2/3/12',
        0x2B: 'test_unknown_character_gets_the_character_list (lc-enter-world)',
        # P4 stage 3: the interim failure replies became full handlers (crafting.py)
        0x67: 'test_crafting.CraftFlow: item_inventory-crafting answers 0x8D 1/0/0x0F/0x10/0x11/2',
        0x68: 'test_crafting.CraftFlow: item_inventory-reinforcement answers 0x8E 1/0/0x11',
        0x69: 'test_crafting.CraftFlow: item_inventory-gathering answers 0x8F 1/0/0x11',
        # P4 stage 4: the Card Deck register handler (cards.py)
        0x64: 'test_cards.CardFlow: quest_cards_misc-card-register answers 0x8B 1/2/3/6',
        # P8 stage 4: the last interim craft stub became GameServer._handle_stone_extract
        0x72: 'test_cashextras.Extraction: item_inventory-stone-extraction answers 0x9C 1 (+ 0x18) / 0',
    }

    def test_must_reply_requests_get_exactly_their_refusal(self):
        enabled = {op for op, m in registry.MUST_REPLY.items()
                   if m.refusal is not None and op not in self.HANDLED_MUST_REPLY
                   and op not in self.STATEFUL_REFUSALS
                   and getattr(W.GameServer.ROUTES.get(op), 'fallback', True)}
        self.assertEqual(enabled, set(self.REFUSALS), 'REFUSALS must cover every enabled MUST_REPLY row')
        c = self.client()
        self.login(c)
        self.enter_world(c)
        for op in sorted(self.REFUSALS):
            fields, expected = self.REFUSALS[op]
            with self.subTest(opcode=f'0x{op:02X}'):
                key = P.variants(op, 'C2S')[0]['key']
                c.send(op, P.build(key, fields, direction='C2S'))
                pkts = c.expect(*[e[0] for e in expected], quiet=0.12)
                pkts = pkts if isinstance(pkts, list) else [pkts]
                for pkt, (_, length, values) in zip(pkts, expected):
                    rec = self.s2c(pkt, length)
                    for k, v in values.items():
                        self.assertEqual(rec[k], v, f'S2C 0x{pkt.opcode:02X}.{k}')
        self.assertTrue(c.session['in_world'])

    def test_create_room_type_4_is_a_play_room(self):
        c = self.client()
        self.login(c)
        c.send_c2s('0x449384/0x18', {'room_title': 'p', 'room_type': 4})
        self.assertEqual(c.expect(0xA2).payload, b'')

    def test_unknown_character_gets_the_character_list(self):
        c = self.client()
        self.login(c)
        with self.assertLogs('WS', logging.INFO) as cm:
            c.send_c2s('0x42F904/0x2B', {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907, 'char_name': 'Nobody'})
            rec = self.s2c(c.expect(0x02))
        self.assertEqual([ch['name'] for ch in rec['repeat[char_count]']], ['TestHero'])
        # lc-enter-world sends the same refusal itself (registry.MUST_REPLY[0x2B] keeps the
        # bytes), so the policy has nothing left to do.
        self.assertTrue(any('is not on account' in line for line in cm.output))
        self.assertFalse(c.session['in_world'])
        self.enter_world(c)                                  # the select screen still works

    def test_refusal_without_builder_is_logged(self):
        # 0x2E had no refusal until cs-player-death (P3 stage 4) and the stat reset 0x4A until
        # premium_cash-stat-reset (P8 stage 3): every row has one now, so the "no automatic
        # refusal yet" path is checked on a stand-in: 0x4A with no refusal and a log-only route.
        self.assertTrue(all(m.refusal is not None for m in registry.MUST_REPLY.values()))
        c = self.client()
        self.login(c)
        self.enter_world(c)
        op = 0x4A
        row = registry.MustReply('modal', None, 'a refusal nobody built', 'some-owner')
        stub = W.Route(log='[0x4A] stand-in - consumed')
        with mock.patch.dict(registry.MUST_REPLY, {op: row}), mock.patch.dict(W.GameServer.ROUTES, {op: stub}):
            with self.assertLogs('WS', logging.WARNING) as cm:
                c.send(op, P.build(P.variants(op, 'C2S')[0]['key'], {}, direction='C2S'))
                c.expect_silence()
        self.assertTrue(any('C2S 0x4A stub route and has no automatic refusal yet' in line
                            and 'some-owner' in line for line in cm.output), cm.output)

    def test_no_refusal_before_login(self):
        c = self.client()
        with self.assertLogs('WS', logging.WARNING) as cm:
            c.send_c2s('0x449384/0x18', {'room_title': 'r', 'room_type': 1})
            c.expect_silence()
        self.assertTrue(any('before login' in line for line in cm.output))

    def test_unknown_opcode_is_logged_with_hex(self):
        c = self.client()
        self.login(c)
        with self.assertLogs('WS', logging.INFO) as cm:
            c.send(0xEE, b'\xde\xad\xbe\xef')
            c.send_c2s(P.variants(0x74, 'C2S')[0]['key'], {})     # has a grammar, no route
            c.expect_silence()
        text = '\n'.join(cm.output)
        self.assertIn('Unhandled opcode 0xEE (no C2S spec) 4B: de ad be ef', text)
        self.assertIn(f"Unhandled opcode 0x74 {P.variants(0x74, 'C2S')[0]['name']} 0B: (empty)", text)

    def test_malformed_payload_is_logged_and_legacy_handler_still_runs(self):
        c = self.client()
        self.login(c)
        self.enter_world(c)
        with self.assertLogs('WS', logging.WARNING) as cm:
            c.send(0x7E, b'\x17\x00')                         # 2 of 4 bytes
            c.expect_silence()
        self.assertTrue(any('0x7E 2B matches no C2S grammar: 17 00' in line for line in cm.output))

    # ------------------------------------------------ handler-level behaviour ---
    def _route(self, opcode, route, **methods):
        self.server.ROUTES = dict(W.GameServer.ROUTES)
        self.server.ROUTES[opcode] = route
        for name, fn in methods.items():
            setattr(self.server, name, fn)

    def test_handler_exception_still_sends_refusal_and_keeps_the_connection(self):
        def boom(sock, session, payload, no_enc):
            raise RuntimeError('handler bug')
        self._route(0x12, registry.Route('_boom'), _boom=boom)
        c = self.client()
        self.login(c)
        with self.assertLogs('WS', logging.ERROR) as cm:
            c.send_c2s('0x44ABA4/0x12', {'char_name': 'TestHero', 'confirm_password': 'test'})
            self.assertEqual(self.s2c(c.expect(0x1F), 1)['result'], 2)
        self.assertTrue(any('handler for C2S 0x12 raised' in line for line in cm.output))
        self.enter_world(c)                                  # same socket keeps working

    def test_handler_reply_suppresses_refusal(self):
        server = self.server

        def delete_ok(sock, session, payload, no_enc):
            P.send(server, sock, session, 0x1F, {'result': 1})
        self._route(0x12, registry.Route('_delete_ok'), _delete_ok=delete_ok)
        c = self.client()
        self.login(c)
        c.send_c2s('0x44ABA4/0x12', {'char_name': 'TestHero', 'confirm_password': 'test'})
        self.assertEqual(self.s2c(c.expect(0x1F), 1)['result'], 1)   # exactly one reply

    def test_packets_to_another_session_do_not_count_as_the_reply(self):
        server = self.server
        a, b = self.client(), self.client()
        self.login(a)
        self.login(b, 'admin', 'admin')

        def notify_other(sock, session, payload, no_enc):
            other = b.session
            P.send(server, other['sock'], other, 0x15, {'type': 1, 'text': b'hello'})
        self._route(0x12, registry.Route('_notify_other'), _notify_other=notify_other)
        a.send_c2s('0x44ABA4/0x12', {'char_name': 'TestHero', 'confirm_password': 'test'})
        self.assertEqual(self.s2c(a.expect(0x1F), 1)['result'], 2)  # A still gets its refusal
        self.s2c(b.expect(0x15))

    def test_rec_style_handler_gets_the_decoded_record(self):
        got = []

        def portal(sock, session, rec, no_enc):
            got.append((rec.key, rec['portal_line_index'], rec.raw))
        self._route(0x7E, registry.Route('_portal', style=registry.STYLE_REC), _portal=portal)
        c = self.client()
        self.login(c)
        c.send(0x7E, CAP_7E_101_TO_102)
        c.expect_silence(0.2)
        c.send(0x7E, b'\x17')                                 # malformed: handler not called
        c.expect_silence(0.2)
        self.assertEqual(got, [('0x42F76B/0x7E', 23, CAP_7E_101_TO_102)])

    def test_reply_to_never_reply_opcode_is_a_policy_error(self):
        server = self.server

        def chatty(sock, session, payload, no_enc):
            P.send(server, sock, session, 0x15, {'type': 1, 'text': b'pong'})
        self._route(0x05, registry.Route('_chatty'), _chatty=chatty)
        c = self.client()
        self.login(c)
        with self.assertLogs('WS', logging.ERROR) as cm:
            c.send(0x05)
            c.expect(0x15)
        self.assertTrue(any('policy violation: C2S 0x05 must never be answered' in line for line in cm.output))


class ChatGmDispatchRig(ServerTest):
    """chat_mail_gm-offline-dispatch-tests scaffolding: every chat C2S the design doc lists
    (chat_mail_gm.md 1.5 and 6.x captures) reaches the registry, decodes to the right send
    site, and gets today's answer.

    The nine C2S 0x06 GM sub-commands moved to test_gm.py when chat_mail_gm-gm-dispatch
    landed (they are handled now, /업데이트 included - it is covered offline because it
    cannot be typed without a Korean IME)."""

    def setUp(self):
        super().setUp()
        self.c = self.client()
        self.login(self.c)
        self.enter_world(self.c)

    @staticmethod
    def name17(text):
        return text.encode() + b'\x00' * (17 - len(text))

    def test_chat_requests_decode_and_get_todays_answer(self):
        # P6 stage 1: the whisper (chat_mail_gm-whisper) and friend chat (-friend-chat-relay)
        # are handled now - test_whisper_info.py covers both with two clients; here the
        # captured single-client forms: a whisper to nobody online is "can not be found"
        # (0x09 0x66, 18 B), a /f line with no stored friend reaches nobody and is never
        # answered (NEVER_REPLY 0x6B). P7 stage 3 (chat_mail_gm-report): a report on a name no
        # character has is 0x95 {2} "The character doesn't exist." (test_reputation.py has the
        # rest with two clients).
        cases = [
            # (label, opcode, payload, expected send-site key, expected reply opcode | None, log)
            ('/w alice hi (T-02)', 0x02, self.name17('alice') + b'\x02hi', '0x445ADD/0x02', 0x09,
             "[WHISPER] 'TestHero' -> 'alice': not found (0x09 0x66)"),
            ('/f hi (T-6B)', 0x6B, bytes.fromhex('01 01 02000000 0d 546573744865726f203a206869'), '0x46FE14/0x6B',
             None, "[FRIEND] 'TestHero : hi' -> nobody"),
            ('report (T-6E)', 0x6E, (2).to_bytes(4, 'little') + self.name17('Bob') + (100).to_bytes(4, 'little')
             + b'\x01\x03bad', '0x4735D1/0x6E', 0x95,
             "[REPORT] 'TestHero' report on 'Bob' (uid 2): 0x95 {2} (no such character)"),
        ]
        for label, op, payload, key, reply, line in cases:
            with self.subTest(label):
                rec, err = registry.decode(op, payload)
                self.assertIsNone(err)
                self.assertIn(key, rec.candidates)
                with self.assertLogs('WS', logging.INFO) as cm:
                    self.c.send(op, payload)
                    if reply is None:
                        self.c.expect_silence(0.15)
                    elif reply == 0x95:
                        self.assertEqual(self.s2c(self.c.expect(reply), 1), {'result': 2})
                    else:
                        got = self.s2c(self.c.expect(reply), 18)
                        self.assertEqual((got['status'], got['target_name']), (0x66, 'alice'))
                self.assertTrue(any(line in out for out in cm.output), cm.output)

    def test_note_send_is_refused_but_gift_reply_9999_is_not(self):
        # The 0x4B handler answers (not the exception fallback): since P6 stage 2
        # (social_friend-memos) a note to an unknown name is 0x77 {0}; the 9999 gift reply to
        # one is dropped with no reply (test_messenger.py has the delivered cases). The Note is
        # a cash inventory record since P8 stage 1 (DEV_FREE_NOTES retired), so TestHero owns one.
        self.server.cash.grant(self.server.store.find_character('test', 'TestHero'), 1894, 1)
        with self.assertLogs('WS', logging.INFO) as cm:
            self.c.send_c2s('0x461064/0x4B', {'item_id': 1894, 'recipient_name': 'Bob', 'contents': 'hi'})
            self.assertEqual(self.s2c(self.c.expect(0x77), 1)['result'], 0)
            self.c.send_c2s('0x46098E/0x4B', {'note_item_id': registry.GIFT_REPLY_NOTE_ID,
                                              'recipient_name': 'Bob', 'message': 'thanks'})
            self.c.expect_silence()                           # no waiting box to release
        text = '\n'.join(cm.output)
        self.assertIn("[NOTE] 'TestHero' note 1894 to 'Bob': 0x77 {0} (unknown recipient)", text)
        self.assertIn("[NOTE] 'TestHero' gift thank-you to 'Bob' dropped (unknown recipient)", text)
        self.assertNotIn('[MUST-REPLY]', text)


class InWorldTest(ServerTest):
    def setUp(self):
        super().setUp()
        self.c = self.client()
        self.login(self.c)
        self.enter_world(self.c)

    def portal_to_102(self):
        self.c.send(0x7E, CAP_7E_101_TO_102)
        self.c.expect(0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(8))
        mobs = self.c.session['monsters']
        self.assertEqual(len(mobs), 8)
        return mobs

    @staticmethod
    def mob_state(mobs):
        return {uid: (m.hp, m.alive) for uid, m in mobs.items()}


class MisrouteFixes(InWorldTest):
    """cs-dispatch-fix (+trade-route-fix-0x25), pvp-dispatch-cleanup,
    quest_cards_misc-stop-0x63-echo (roadmap S1-12, S2-01, S1-09; P0 exit criteria 4 and 5)."""

    def test_T_key_battlefield_window_gets_0x63_and_damages_no_monster(self):
        mobs = self.portal_to_102()
        pupu = mobs[W.MOB_UID_BASE]
        # The old _handle_melee hit the nearest mob within reach of session x/y.
        self.c.session['x'], self.c.session['y'] = pupu.x, pupu.y
        before = self.mob_state(mobs)
        with self.assertLogs('WS', logging.INFO) as cm:
            self.c.send_c2s('0x446628/0x38')                  # live capture: T -> 0x38 0 B
            rec = self.s2c(self.c.expect(0x63), 3)            # all 3 bytes, never a short 0x63
        self.assertEqual((rec['player_count'], rec['join_count']), (0, 0))   # "0 Players", "Join(0/12)"
        self.assertEqual(self.mob_state(mobs), before)
        self.assertFalse(any('[ATK]' in line or '[KILL]' in line for line in cm.output))

    def test_trade_final_confirm_is_canceled_and_damages_no_monster(self):
        mobs = self.portal_to_102()
        before = self.mob_state(mobs)
        # Item ids 0..7 used to become mob uids 0xF0000..0xF0007 (S1-12 exploit).
        items = [{'item_id': i, 'amount': 1, 'opt_count': 0, 'attr_0c': 0} for i in range(8)]
        payload = P.build('0x469BA8/0x25', {'my_gold': 5000, 'my_item_count': 8, 'repeat[my_item_count]': items,
                                            'partner_gold': 0, 'partner_item_count': 0}, direction='C2S')
        with self.assertLogs('WS', logging.INFO) as cm:
            self.c.send(0x25, payload)
            self.assertEqual(self.c.expect(0x49).payload, b'')   # "Trade has been canceled."
        self.assertEqual(self.mob_state(mobs), before)
        text = '\n'.join(cm.output)
        # P7 stage 1: the trade handler answers a confirm with no trade on record itself
        # (trade.md 2.8 step 2.2: a stale window still gets its 0x49), so the policy's
        # refusal stays the exception backstop (test_trade.py covers the real trade).
        self.assertIn('confirms with no trade on record -> 0x49', text)
        self.assertNotIn('[MUST-REPLY] C2S 0x25', text)
        self.assertNotIn('[ATK]', text)

    def test_card_deck_request_gets_0x8A_not_0x63(self):
        self.c.send_c2s('0x44EF67/0x63')
        # P4 stage 4: the FIRST 0x63 of a connection also carries the 0x99 sub 8 channel line
        pkt, notice = self.c.expect(0x8A, 0x99)
        self.assertEqual(pkt.payload, b'\x00')                # an empty deck
        self.assertEqual(self.s2c(pkt)['deck_count'], 0)
        self.assertEqual(self.s2c(notice), {'sub_type': 8, 'channel_no': 1})
        self.c.send_c2s('0x44EF67/0x63')
        self.assertEqual(self.c.expect(0x8A).payload, b'\x00')   # once per connection

    def test_post_map_load_handshake_after_portal(self):
        # Live: after the portal replay the client sends 0x2F then 0x63 (LIVE_TEST_LOG row 28).
        # P6 stage 2: 0x2F is answered with the friend list S2C 0x0B (20 slots, no friends).
        self.portal_to_102()
        with self.assertLogs('WS', logging.INFO) as cm:
            self.c.send_c2s('0x44EF4F/0x2F')
            self.c.send_c2s('0x44EF67/0x63')
            # the first 0x63 of the connection: + 0x99 sub 8 "In channel 1." (P4 stage 4)
            friends, deck, _channel = self.c.expect(0x0B, 0x8A, 0x99)
            self.assertEqual(deck.payload, b'\x00')
        self.assertEqual(friends.payload, b'\x14\x00')
        self.assertTrue(any("[MSGR] 'TestHero': 0x0B 0/20 friend(s)" in line for line in cm.output))

    def test_room_join_from_either_list_is_refused(self):
        # 0x1A was consumed as "mob query" and left the waiting modal up (S1-09).
        for key, fields, room_type in (('0x449384/0x1A', {'room_no': 7, 'room_type': 2}, 2),
                                       ('0x44B0AC/0x1A', {'room_no': 9, 'room_category': 4}, 4)):
            with self.subTest(key):
                self.c.send_c2s(key, fields)
                rec = self.s2c(self.c.expect(0x34), 2)
                self.assertEqual((rec['result'], rec['room_type']), (2, room_type))

    def test_list_closes_and_combat_notices_are_logged_not_unhandled(self):
        cases = [('0x446A93/0x2D', {}, '[0x2D] arena room list close - consumed'),
                 ('0x446A93/0x75', {}, '[0x75] play room list close - consumed'),
                 ('0x417DB0/0x6C', {'trap_skill_id': 2609}, '[0x6C] trap triggered - consumed'),
                 ('0x4484CC/0x2E', {}, '[0x2E] death revive request')]
        for key, fields, line in cases:
            with self.subTest(key):
                with self.assertLogs('WS', logging.INFO) as cm:
                    self.c.send_c2s(key, fields)
                    self.c.expect_silence(0.2)
                text = '\n'.join(cm.output)
                self.assertIn(line, text)
                self.assertNotIn('Unhandled opcode', text)

    def test_dead_room_host_opcodes_warn(self):
        for key, fields in (('0x4285DE/0x93', {'room_no': 1, 'server_index': 0, 'sub_op': 3, 'uid': 5}),
                            ('0x418294/0x97', {'room_id': 1, 'entity_uid': 5, 'skill_id': 292}),
                            ('0x418294/0x99', {'room_id': 1, 'entity_uid': 5, 'skill_id': 292})):
            with self.subTest(key):
                with self.assertLogs('WS', logging.WARNING) as cm:
                    self.c.send_c2s(key, fields)
                    self.c.expect_silence(0.2)
                self.assertTrue(any('room-host dead-code opcode' in line for line in cm.output))
        self.assertTrue(self.c.session['in_world'])

    def test_memory_driver_melee_still_kills_and_rewards(self):
        # The combat driver (not a C2S opcode) stays the attack source: _memory_melee ->
        # _resolve_hit -> _kill_monster -> 0x29 + 0x21 (+0x22) + 0x18.
        mobs = self.portal_to_102()
        pupu = mobs[W.MOB_UID_BASE]
        hits = 0
        while pupu.alive and hits < 30:
            self.server._memory_melee(pupu.x, pupu.y)
            hits += 1
        self.assertFalse(pupu.alive)
        opcodes = [p.opcode for p in self.c.recv_until_quiet()]
        self.assertEqual(opcodes[0], 0x29)
        self.assertIn(0x21, opcodes)
        self.assertEqual(opcodes[-1], 0x18)


class CastUnlock(InWorldTest):
    """cs-cast-unlock (S1-10; P0 exit criterion 8): a skill cast must clear scene+0x258."""

    def test_skill_cast_gets_0x5F(self):
        with self.assertLogs('WS', logging.INFO) as cm:
            self.c.send_c2s('0x44C239/0x15', {'skill_id': 2188})   # Reinforce, EN Type 3
            self.assertEqual(self.c.expect(0x5F).payload, b'')
        text = '\n'.join(cm.output)
        self.assertIn('[SKILL] cast 2188 refused with 0x5F', text)
        self.assertNotIn('[MUST-REPLY]', text)                     # the handler answered
        # equip afterwards is still served (the lock itself is client-side)
        self.c.send_c2s('0x47734D/0x16', {'quest_id': 26})
        self.c.expect(0x26, 0x59)
        self.c.send(0x0F, CAP_0F_EQUIP_STICK)
        self.c.expect(0x1D)

    def test_booby_trap_form_with_position_gets_0x5F(self):
        payload = hexbytes('31 0A B0 04 BC 02')                      # 2609 at (1200, 700): 6 B
        rec = P.parse(0x15, payload)
        self.assertEqual(rec.candidates, (registry.SKILL_CAST_KEY,))
        with self.assertLogs('WS', logging.INFO) as cm:
            self.c.send(0x15, payload)
            self.c.expect(0x5F)
        self.assertTrue(any('cast 2609 at (1200,700) refused' in line for line in cm.output))

    def test_consumable_gets_no_0x5F(self):
        self.assertEqual(W.en_item_type(3), 0)                       # Blue Mushroom, +20 MP
        self.server._inv_add(self.c.session, 3, 1)
        with self.assertLogs('WS', logging.DEBUG) as cm:
            self.c.send_c2s('0x44C2B3/0x15', {'item_id': 3})         # MP already full: no change
            self.c.expect(0x25)                                      # the use, never a 0x5F
        self.assertFalse(any('sent refusal' in line for line in cm.output))

    def test_cast_while_not_in_world_sends_nothing(self):
        c = self.client()
        self.login(c, 'admin', 'admin')                              # character select
        with self.assertLogs('WS', logging.WARNING) as cm:
            c.send_c2s('0x44C239/0x15', {'skill_id': 2188})
            c.expect_silence()
        self.assertTrue(any('while not in world - no 0x5F' in line for line in cm.output))

    def test_handler_exception_still_unlocks_skill_casts_only(self):
        def boom(sock, session, payload, no_enc):
            raise RuntimeError('use-item bug')
        self.server._handle_use_item = boom
        with self.assertLogs('WS', logging.INFO) as cm:
            self.c.send_c2s('0x44C239/0x15', {'skill_id': 292})      # Increase Attack Power
            self.c.expect(0x5F)
            self.c.send_c2s('0x44C2B3/0x15', {'item_id': 3})
            self.c.expect_silence()
        self.assertTrue(any('[MUST-REPLY] C2S 0x15 handler raised: sent refusal 0x5F' in line
                            for line in cm.output))


class StallStub(InWorldTest):
    """shop_storage-stall-stub (+trade-stall-visit): shop_storage.md F9/F12/F13, B9."""

    def open_stall(self, payload=None):
        if payload is None:
            self.c.send_c2s('0x46B635/0x5E', {
                'item_count': 1, 'shop_name': 'Test',
                'repeat[item_count]': [{'item_id': 3, 'qty': 5, 'unit_price': 100, 'socket_count': 0,
                                        'item_extra': 0}]})
        else:
            self.c.send(0x5E, payload)
        self.assertEqual(self.s2c(self.c.expect(0x82), 1)['result'], 2)   # "Item stall opening failed"
        self.assertTrue(self.c.session['stall_client_selling'])

    def test_failed_open_then_close_gets_one_0x84(self):
        self.open_stall()
        self.c.send_c2s('0x469BA8/0x60')
        self.assertEqual(self.s2c(self.c.expect(0x84), 1)['result'], 1)
        self.assertFalse(self.c.session['stall_client_selling'])
        with self.assertLogs('WS', logging.INFO) as cm:
            self.c.send_c2s('0x469BA8/0x60')                  # automatic re-send (after 0x08 etc.)
            self.c.expect_silence()
        self.assertTrue(any('ignored (automatic re-send)' in line for line in cm.output))

    def test_close_without_open_is_ignored(self):
        self.c.send_c2s('0x469BA8/0x60')
        self.c.expect_silence()

    def test_undecodable_open_is_still_refused(self):
        # item_count 2 with one item listed (the client counts NULL list nodes, F9 3.3)
        good = P.build('0x46B635/0x5E', {'item_count': 1, 'shop_name': 'Test', 'repeat[item_count]': [
            {'item_id': 3, 'qty': 5, 'unit_price': 100, 'socket_count': 0, 'item_extra': 0}]}, direction='C2S')
        self.open_stall(b'\x02' + good[1:])

    def test_stop_for_edit_gets_0x83_and_clears_selling(self):
        self.open_stall()
        self.c.send_c2s('0x469BA8/0x5F')
        self.assertEqual(self.s2c(self.c.expect(0x83), 1)['result'], 1)
        self.assertFalse(self.c.session['stall_client_selling'])
        self.c.send_c2s('0x469BA8/0x60')
        self.c.expect_silence()

    def test_visit_and_buy_are_refused(self):
        self.c.send_c2s('0x44CA2F/0x61', {'stall_owner_uid': 2})
        self.assertEqual(self.s2c(self.c.expect(0x87), 1)['result'], 0)   # "The shop is closed..."
        self.c.send_c2s(P.variants(0x62, 'C2S')[0]['key'], {'seller_uid': 2, 'item_id': 3, 'qty': 1})
        self.assertEqual(self.s2c(self.c.expect(0x88), 1)['result'], 0)

    def test_close_handler_exception_falls_back_to_0x84_while_selling(self):
        self.open_stall()

        def boom(sock, session, payload, no_enc):
            raise RuntimeError('stall bug')
        self.server._handle_stall_close = boom
        with self.assertLogs('WS', logging.ERROR):
            self.c.send_c2s('0x469BA8/0x60')
            self.assertEqual(self.s2c(self.c.expect(0x84), 1)['result'], 1)


class CraftStubs(InWorldTest):
    """item_inventory-interim-craft-replies (B12): one reply per completion. Since P4 stage 3
    0x67 / 0x68 / 0x69 have their handlers (test_crafting.py), since P8 stage 4 0x72 too
    (test_cashextras.py): no stub route is left."""

    def test_gather_failure_mirrors_the_tool_the_client_removes(self):
        tool = 2215                                                   # Crude Garden Shovel (EN Type 2)
        self.server._inv_add(self.c.session, tool, 2)
        # record 3 is no gather node (and this map has none): result 0, the tool still goes
        self.c.send_c2s('0x4685B2/0x69', {'gather_node_record_index': 3, 'tool_item_id': tool})
        rec = self.s2c(self.c.expect(0x8F), 3)
        self.assertEqual((rec['result'], rec['tool_item_id']), (0, tool))
        self.assertEqual(self.server._inventory(self.c.session).get(tool), 1)
        # a tool the server bag does not hold: still answered, nothing removed
        self.c.session.pop('gather_last_ms', None)
        self.c.send_c2s('0x4685B2/0x69', {'gather_node_record_index': 3, 'tool_item_id': 2216})
        self.assertEqual(self.s2c(self.c.expect(0x8F), 3)['tool_item_id'], 2216)
        self.assertNotIn(2216, self.server._inventory(self.c.session))

    def test_extraction_and_crafting_are_handled(self):
        # 0x72 without an owned Element Separator: the handler's own 0x9C {0}, not the stub
        cases = [(0x67, {'product_item_id': 2975}, 0x8D, '[CRAFT]'),
                 (0x68, {'equip_item_id': 179, 'stone_item_id': 900}, 0x8E, '[REINFORCE]'),
                 (0x72, {'mode_id': 0xF70, 'equip_item_id': 179, 'stone_item_id': 900}, 0x9C, '[EXTRACT]')]
        for op, fields, reply, tag in cases:
            with self.subTest(opcode=f'0x{op:02X}'):
                with self.assertLogs('WS', logging.INFO) as cm:
                    self.c.send_c2s(P.variants(op, 'C2S')[0]['key'], fields)
                    rec = self.s2c(self.c.expect(reply))
                text = '\n'.join(cm.output)
                self.assertIn(tag, text)
                self.assertNotIn(f'[MUST-REPLY] C2S 0x{op:02X}', text)
                self.assertNotIn('Unhandled opcode', text)
                if op == 0x72:
                    self.assertEqual(rec, {'result': 0})


class ChatBuilders(InWorldTest):
    """chat_mail_gm-chat-builders (roadmap S2-05, S2-06, S1-14, S3-03; P0 exit criterion 2)."""

    def chat(self, message):
        self.c.send_c2s('0x445CA7/0x03', {'msg_len': len(message), 'message': message})

    def test_live_hi_capture_is_echoed_as_character_name_without_count_byte(self):
        self.c.send(0x03, CAP_03_HI)                          # live: 03 02 68 69
        pkt = self.c.expect(0x16)
        self.assertEqual(pkt.payload, P.name17('TestHero') + b'\x02hi')   # "TestHero : hi", not "test :"
        rec = self.s2c(pkt, 20)
        self.assertEqual((rec['sender_name'], rec['text_len'], rec['text']), ('TestHero', 2, 'hi'))

    def test_text_is_raw_bytes_nul_cut_and_clamped_to_60(self):
        self.chat(b'\xbe\xc8\xb3\xe7')                        # cp949 text passes through (no '?')
        self.assertEqual(self.c.expect(0x16).payload[17:], b'\x04\xbe\xc8\xb3\xe7')
        self.c.send(0x03, hexbytes('05 68 69 00 68 69'))      # mode 5 "/a hi": msg_len counts the prefix
        self.assertEqual(self.s2c(self.c.expect(0x16), 20)['text'], 'hi')
        self.chat(b'ABCDEFGHIJKLMNOPQRSTUVWXYZ' * 4)           # 104 B
        rec = self.s2c(self.c.expect(0x16), 18 + W.CHAT_TEXT_MAX)
        self.assertEqual(rec['text'], ('ABCDEFGHIJKLMNOPQRSTUVWXYZ' * 3)[:60])
        # a cp949 pair straddling byte 60 is dropped whole, never split
        self.chat(b'a' * 59 + b'\xbe\xc8')
        self.assertEqual(self.s2c(self.c.expect(0x16), 18 + 59)['text_len'], 59)

    def test_empty_line_and_character_select_chat_are_dropped(self):
        self.chat(b'')
        self.chat(b'\x00hidden')
        self.c.expect_silence()
        c = self.client()
        self.login(c, 'admin', 'admin')
        c.send(0x03, CAP_03_HI)
        c.expect_silence()

    def test_emotes_are_chat_and_gm_bang_lines_are_the_command_hook(self):
        self.chat(b'/heart')                                  # never intercepted: emotes reach clients
        self.assertEqual(self.s2c(self.c.expect(0x16))['text'], '/heart')
        self.chat(b'!who')                                    # no GM flag: ordinary chat
        self.assertEqual(self.s2c(self.c.expect(0x16))['text'], '!who')
        # With the flag the line is a dev command: answered with S2C 0x15, never broadcast
        # (chat_mail_gm-dev-commands; the commands themselves are tested in test_gm.py).
        self.c.session['gm'] = 1
        self.chat(b'!who')
        for pkt in self.c.recv_until_quiet():
            self.assertEqual(pkt.opcode, 0x15)


class SystemNotices(InWorldTest):
    """chat_mail_gm-system-notices (S2-05 B2, S3-02, roadmap D17; P0 exit criterion 3)."""

    def test_welcome_is_one_announce_line_on_first_entry_only(self):
        # A fresh connection of the same account: the setUp session logs out first (a
        # second login while it is online is refused with 0x02 result 4, lc-uid-online).
        self.c.close()
        self.assertTrue(_wait(lambda: self.server.world.session(1) is None))
        c = self.client()
        self.login(c)
        c.send_c2s('0x42F904/0x2B', {'p2p_ip': '', 'p2p_udp_port': 42907, 'char_name': 'TestHero'})
        pkts = c.expect(0x03, 0x07, 0x15, 0x28, 0x44)
        rec = self.s2c(pkts[2], 35)
        self.assertEqual((rec['msg_type'], rec['text']), (2, '[Announce] Welcome to WindSlayer!'))
        c.send(0x7E, CAP_7E_101_TO_102)                       # no welcome on a portal, no 0x0A whisper
        c.expect(0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(8))
        c.send_c2s('0x42F904/0x2B', {'p2p_ip': '', 'p2p_udp_port': 42907, 'char_name': 'TestHero'})
        opcodes = [p.opcode for p in c.recv_until_quiet()]
        self.assertNotIn(0x15, opcodes)
        self.assertNotIn(0x0A, opcodes)

    def test_notice_kinds_and_88_byte_clamp(self):
        s = self.c.session
        for kind, (msg_type, text) in {'plain': (0, 'hello'), 'info': (1, 'hello'),
                                       'warn': (2, '[Warning] hello'),
                                       'announce': (2, '[Announce] hello')}.items():
            with self.subTest(kind):
                self.server._notice(s['sock'], s, 'hello', kind)
                rec = self.s2c(self.c.expect(0x15), 2 + len(text))
                self.assertEqual((rec['msg_type'], rec['text']), (msg_type, text))
        self.server._notice(s['sock'], s, 'x' * 200, 'warn')    # prefix counts toward the 88
        rec = self.s2c(self.c.expect(0x15), 2 + 88)
        self.assertTrue(rec['text'].startswith('[Warning] xxx'))

    def test_orange_and_green_lines_clamp_to_87(self):
        s = self.c.session
        self.server._orange(s, 'party line')
        self.assertEqual(self.s2c(self.c.expect(0x90), 11)['text'], 'party line')
        self.server._green([s], 'y' * 100)
        self.assertEqual(self.s2c(self.c.expect(0x91), 88)['text_len'], 87)

    def test_quest_refusals_and_notices_are_system_lines(self):
        self.c.send_c2s('0x47734D/0x16', {'quest_id': 65000})     # no such quest
        rec = self.s2c(self.c.expect(0x15))
        self.assertEqual((rec['msg_type'], rec['text']), (2, '[Warning] Unknown quest.'))
        # The 3 active slots live in the character record (quest-state-model), so the
        # "log full" refusal is driven by filling them, not by a session key.
        state = self.server._quest_state(self.c.session)
        for quest_id in (1, 2, 3):
            state.accept(quest_id)
        self.c.send_c2s('0x47734D/0x16', {'quest_id': 26})
        self.assertEqual(self.s2c(self.c.expect(0x15))['text'], '[Warning] Quest log full (3).')
        for quest_id in (1, 2, 3):
            state.abandon(quest_id)
        # A successful accept carries NO system line: the client prints its own
        # "you've received ..." for the Send items (quest doc F1 step 7, Q-B19).
        self.c.send_c2s('0x47734D/0x16', {'quest_id': 26})
        self.c.expect(0x26, 0x59)

    def test_buy_refusal_is_a_warning_line(self):
        INV.Wallet(self.server._session_char(self.c.session)).gold = 0
        self.c.send_c2s('0x469D9C/0x0B', {'item_id': 3, 'qty': 10, 'npc_id': 8})   # Misty
        # Like every shop refusal: the 0x18 puts the labels back, the 0x15 says why.
        resync, warning = self.c.expect(0x18, 0x15)
        self.assertEqual(self.s2c(resync, 16)['gold'], 0)
        self.assertEqual(self.s2c(warning)['text'], '[Warning] Not enough gold.')


class QuestGrantMirror(InWorldTest):
    """item_inventory-quest-grant-mirror slice (a) (S2-09, LIVE_TEST_LOG bug 4; P0 exit criterion 6):
    the client grants quest Send (0x26) and Reward/Money (0x27) itself, so no 0x18 follows."""

    QID = 65001

    def test_accept_sends_no_0x18_and_mirrors_the_send_item(self):
        self.c.send_c2s('0x47734D/0x16', {'quest_id': 26})      # Elder's Test: Send Wooden Stick x1
        grant, _ = self.c.expect(0x26, 0x59)             # "you've received Wooden Stick" once
        self.assertEqual(self.s2c(grant, 2)['quest_id'], 26)
        # The EN hqi Send item is the Wooden Stick 179 itself: the old hex parse read 377
        # and _EN_ITEM_OVERRIDE mapped it back (Q-B1, both gone).
        self.assertEqual(EC.quests().get(26).send, [(179, 1)])
        self.assertEqual(self.server._inventory(self.c.session), {179: 1})
        # It is in the character record, so it is still there after a relog.
        char = self.server.store.find_character('test', 'TestHero')
        self.assertEqual([e['id'] for e in char['inventory']['equip']], [179])
        self.assertEqual(char['quests']['active'], [26, 0, 0])

    def test_turn_in_sends_no_0x18_and_mirrors_rewards_money_and_demand(self):
        # A synthetic quest in the EN catalog: a ReqPro so it takes a slot (a talk-only
        # quest is filed as completed at once and can never be turned in), no Demand so the
        # turn-in gate is the kill counter alone, and a KR-only reward id to prove the
        # catalog gate still filters what reaches the bag.
        quests = EC.quests().defs
        quests[self.QID] = EC.QuestDef(self.QID, 0, {
            'Send': ['4356' + '0' * 36], 'Send_Num': ['001' + '0' * 27],
            'Reward': ['0005' + '4356' + '0' * 72], 'Reward_Num': ['005' + '002' + '0' * 54],
            'Exp': ['7'], 'Money': ['250'], 'Start_Lev': ['1'], 'End_Lev': ['99'],
            'ReqPro': ['1'], 'NPC': ['9999'], 'SNPC': ['1']})
        self.addCleanup(quests.pop, self.QID, None)
        gold0 = self.server._wallet(self.c.session)[0]
        with self.assertLogs('WS', logging.WARNING) as cm:
            self.c.send_c2s('0x47734D/0x16', {'quest_id': self.QID})
            self.c.expect(0x26, 0x59)
            self.server._quest_state(self.c.session).set_progress(0, 1)   # as a kill would
            self.c.send_c2s('0x477F3E/0x17', {'quest_id': self.QID})      # the real turn-in
            done, exp, gold = self.c.expect(0x27, 0x21, 0x3F)
        self.assertEqual(self.s2c(done, 2)['quest_id'], self.QID)
        self.assertEqual(self.s2c(exp, 4)['exp_delta'], 7)
        self.assertEqual(self.server._inventory(self.c.session), {5: 5})     # KR-only 4356 never mirrored
        self.assertEqual(self.server._wallet(self.c.session)[0], gold0 + 250)  # 0x27 adds Money client-side
        # 0x3F is the silent absolute resync of that same wallet (F5 step 7), not a second grant.
        self.assertEqual(self.s2c(gold, 8)['gold'], gold0 + 250)
        self.assertEqual(self.server.store.find_character('test', 'TestHero')['gold'], gold0 + 250)
        self.assertEqual(sum('item 4356 refused' in line for line in cm.output), 2)


class EquipNoDoubleRemove(InWorldTest):
    """item_inventory-no-double-remove (S2-07, LIVE_TEST_LOG bug 5; P0 exit criterion 7)."""

    def test_equipping_one_of_two_sticks_sends_only_0x1D(self):
        self.server._inv_add(self.c.session, 179, 2)
        self.c.send(0x0F, CAP_0F_EQUIP_STICK)
        pkt = self.c.expect(0x1D)                             # and nothing else: no 0x23
        self.assertEqual(pkt.payload, hexbytes('01 00 00 00 B3 00 00 00 00'))   # same 9 B as live
        self.assertEqual(self.server._inventory(self.c.session), {179: 1})     # one stick stays

    def test_socketed_block_is_echoed(self):
        # A reinforced stick: the bag instance must carry the same 12 bytes, or the equip is
        # refused as a phantom (item_inventory-equip).
        self.server._inv_add(self.c.session, 179, 1, words=[2945, 2950, 0, 0, 0, 7])
        self.c.send_c2s('0x44C481/0x0F', {'item_id': 179, 'stone_count': 2, 'extra_option': 7,
                                          'repeat[stone_count]': [{'stone_id': 2945}, {'stone_id': 2950}]})
        rec = self.s2c(self.c.expect(0x1D), 13)
        self.assertEqual(([e['stone_id'] for e in rec['repeat[stone_count]']], rec['block_tail']), ([2945, 2950], 7))

    def test_overlong_block_and_unknown_items_are_ignored(self):
        with self.assertLogs('WS', logging.WARNING) as cm:
            self.c.send_c2s('0x44C481/0x0F', {'item_id': 179, 'stone_count': 6,
                                              'repeat[stone_count]': [{'stone_id': 1}] * 6})
            self.c.send_c2s('0x44C481/0x0F', {'item_id': 4356})
            self.c.expect_silence()
        text = '\n'.join(cm.output)
        self.assertIn('6 stones refused', text)
        self.assertIn('item=4356 is not an EN client item', text)


class ShopBuy(InWorldTest):
    """shop_storage-npc-buy: the price is charged exactly once, under the same lock that
    decides it can be charged at all."""

    HERB = 5

    MISTY = 8                                  # hni 8, UI 13: sells 3, 5, 6, 7, 51, ...

    def buy(self, item, qty=1):
        self.c.send_c2s('0x469D9C/0x0B', {'item_id': item, 'qty': qty, 'npc_id': self.MISTY})

    def expect_refusal(self, text):
        resync, warning = self.c.expect(0x18, 0x15)
        gold, victy = self.server._wallet(self.c.session)
        self.assertEqual(self.s2c(resync, 16), {'gold': gold, 'victy': victy, 'item_id': 0, 'count': 0})
        self.assertEqual(self.s2c(warning)['text'], f'[Warning] {text}')

    def test_a_balance_that_drops_before_the_debit_gets_no_free_item(self):
        """The affordability test used to run outside the lock and pay()'s answer was
        thrown away, so a balance that dropped in between (the combat driver credits and
        charges gold too) handed out the item for nothing. pay() is now the check."""
        gold0 = self.server._wallet(self.c.session)[0]
        with mock.patch.object(INV.Wallet, 'pay', return_value=False) as pay:
            self.buy(self.HERB, 3)
            self.expect_refusal('Not enough gold.')
        self.assertTrue(pay.called)
        self.assertEqual(self.server._wallet(self.c.session)[0], gold0)
        self.assertNotIn(self.HERB, self.server._inventory(self.c.session))

    def test_an_item_that_does_not_fit_is_not_charged_for(self):
        bag = self.server._bag(self.c.session)
        bag.set_capacity('consume', 1)
        self.server._inv_add(self.c.session, 3, 1)             # the one consume slot
        gold0 = self.server._wallet(self.c.session)[0]
        self.buy(self.HERB, 1)
        self.expect_refusal("There isn't empty space in the inventory.")
        self.assertEqual(self.server._wallet(self.c.session)[0], gold0)      # price refunded
        self.assertNotIn(self.HERB, self.server._inventory(self.c.session))

    def test_a_bought_item_is_charged_once(self):
        gold0 = self.server._wallet(self.c.session)[0]
        price = EC.items().price(self.HERB, 3)
        self.buy(self.HERB, 3)
        rec = self.s2c(self.c.expect(0x18), 16)
        self.assertEqual((rec['item_id'], rec['count'], rec['gold']), (self.HERB, 3, gold0 - price))
        self.assertEqual(self.server._wallet(self.c.session)[0], gold0 - price)
        self.assertEqual(self.server._inventory(self.c.session)[self.HERB], 3)


class ShopSell(InWorldTest):
    """shop_storage-sell-parse (S2-18; shop_storage.md F2, B1/B6)."""

    def sell(self, **fields):
        self.c.send_c2s('0x46A679/0x0C', fields)

    def expect_refusal(self):
        resync, warning = self.c.expect(0x18, 0x15)
        gold, victy = self.server._wallet(self.c.session)
        self.assertEqual(self.s2c(resync, 16), {'gold': gold, 'victy': victy, 'item_id': 0, 'count': 0})
        rec = self.s2c(warning)
        self.assertEqual(rec['msg_type'], 2)
        self.assertTrue(rec['text'].startswith('[Warning] '))
        return rec['text']

    def test_quantity_is_u16(self):
        self.server._inv_add(self.c.session, 3, 300)
        gold0 = self.server._wallet(self.c.session)[0]
        self.c.send(0x0C, hexbytes('03 00 2C 01 00 00 00'))   # item 3, qty 300 (the old u8 read sold 44)
        rec = self.s2c(self.c.expect(0x19), 15)
        self.assertEqual((rec['item_id'], rec['count'], rec['opt_count'], rec['opt_extra']), (3, 300, 0, 0))
        price = EC.items().get(3).sell                     # hii Sell, not the KR gamedef
        self.assertEqual(rec['gold'], gold0 + 300 * price)
        self.assertEqual(self.server._wallet(self.c.session)[0], gold0 + 300 * price)
        self.assertNotIn(3, self.server._inventory(self.c.session))

    def test_the_option_block_is_echoed_verbatim(self):
        """The client removes the item only when the 12 bytes come back unchanged. The
        echo used to go through the STORED form, which packs the words toward w0: a
        request carrying n=2 [0, 7] came back as n=1 [7] and matched nothing, so the gold
        moved and the item stayed in the bag."""
        self.server._inv_add(self.c.session, 3, 1)
        self.sell(item_id=3, qty=1, socket_count=2, item_extra=9,
                  **{'repeat[socket_count]': [{'socket_stone_id': 0}, {'socket_stone_id': 7}]})
        rec = self.s2c(self.c.expect(0x19))
        self.assertEqual(rec['opt_count'], 2)
        self.assertEqual([e['opt'] for e in rec['repeat[opt_count]']], [0, 7])
        self.assertEqual(rec['opt_extra'], 9)

    def test_equipment_zero_block_is_sold_with_its_descriptor(self):
        self.server._inv_add(self.c.session, 179, 1)
        self.sell(item_id=179, qty=1)
        self.assertEqual(self.c.expect(0x19).payload[8:], hexbytes('B3 00 01 00 00 00 00'))
        self.assertEqual(self.server._inventory(self.c.session), {})

    def test_refusals_resync_and_warn_and_never_send_0x19(self):
        self.server._inv_add(self.c.session, 3, 2)
        self.server._inv_add(self.c.session, 179, 1)
        self.server._inv_add(self.c.session, 1848, 1)          # Brown Fedora: type 1, Cash 1
        cases = [
            ('not owned', dict(item_id=5, qty=1)),
            ('more than owned', dict(item_id=3, qty=5)),
            ('qty 0', dict(item_id=3, qty=0)),
            ('u16 stack over 999', dict(item_id=3, qty=1000)),
            ('KR-only id', dict(item_id=4356, qty=1)),
            ('skill book (type 3)', dict(item_id=94, qty=1)),
            ('cash item', dict(item_id=1848, qty=1)),
            ('equipment qty 2', dict(item_id=179, qty=2)),
            ('socketed copy the bag model does not hold',
             dict(item_id=179, qty=1, socket_count=1, **{'repeat[socket_count]': [{'socket_stone_id': 2945}]})),
            ('six option words', dict(item_id=179, qty=1, socket_count=6,
                                      **{'repeat[socket_count]': [{'socket_stone_id': 1}] * 6})),
        ]
        before = dict(self.server._inventory(self.c.session))
        for label, fields in cases:
            with self.subTest(label):
                self.sell(**fields)
                self.expect_refusal()
        self.assertEqual(self.server._inventory(self.c.session), before)

    def test_undecodable_payload_gets_the_policy_refusal(self):
        with self.assertLogs('WS', logging.INFO) as cm:
            self.c.send(0x0C, hexbytes('03 00 01'))               # 3 of the 7 bytes
            self.assertEqual(self.expect_refusal(), '[Warning] The item could not be sold.')
        self.assertTrue(any('[MUST-REPLY] C2S 0x0C' in line for line in cm.output))


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class ItemCatalogValidation(InWorldTest):
    """One EN item-id gate for grants, drops, buys and sells (S2-11, LIVE_TEST_LOG bug 6)."""

    def test_en_item_exists_bounds(self):
        for item in (1, 3, 179, 2030, W.EN_ITEM_MAX_ID):
            self.assertTrue(W.en_item_exists(item), item)
        for item in (0, -1, W.EN_ITEM_MAX_ID + 1, 4356, 0xFFFF, None, 'x'):
            self.assertFalse(W.en_item_exists(item), item)

    def test_monster_drop_tables_hold_only_en_items(self):
        # The drop column of every EN monster template (hs/windslayer.hni, world-template-map);
        # the KR tables listed 4356 for every mob (LIVE_TEST_LOG bug 6).
        for tpl in EC.npcs().monsters():
            with self.subTest(tpl.name):
                self.assertEqual([d for d in tpl.drop_ids if not W.en_item_exists(d)], [])

    def test_bag_model_refuses_kr_only_ids(self):
        with self.assertLogs('WS', logging.WARNING):
            self.assertIsNone(self.server._inv_add(self.c.session, 4356, 1, 'test'))
        self.assertNotIn(4356, self.server._inventory(self.c.session))

    def test_buy_of_a_kr_only_item_is_refused(self):
        with self.assertLogs('WS', logging.WARNING):
            self.c.send_c2s('0x469D9C/0x0B', {'item_id': 4356, 'qty': 1, 'npc_id': 8})
            resync, warning = self.c.expect(0x18, 0x15)           # the shop refusal pair
        gold, victy = self.server._wallet(self.c.session)
        self.assertEqual(self.s2c(resync, 16), {'gold': gold, 'victy': victy, 'item_id': 0, 'count': 0})
        self.assertEqual(self.s2c(warning)['text'], '[Warning] That item is not for sale.')
        self.assertNotIn(4356, self.server._inventory(self.c.session))

    def test_spawn_filters_kr_ids_and_kills_never_drop_them(self):
        tpl = EC.npcs().get(1)
        self.addCleanup(setattr, tpl, 'drops', tpl.drops)
        tpl.drops = [(4356, 50), (5000, 50)]
        with self.assertLogs('WS', logging.WARNING):
            mobs = self.portal_to_102()
        pupu = mobs[W.MOB_UID_BASE]
        self.assertEqual(pupu.drop_items, [])
        pupu.drop_items = [4356]                                # even if one slips into a live table
        self.server.ticks = ticks.Scheduler(lock=self.server.world_lock, clock=FakeClock())
        with self.assertLogs('WS', logging.WARNING):
            random_state = random.getstate()
            self.addCleanup(random.setstate, random_state)
            random.seed(1)
            while pupu.alive:
                self.server._memory_melee(pupu.x, pupu.y)
        drop = [p for p in self.c.recv_until_quiet() if p.opcode == 0x18][0]
        self.assertEqual(self.s2c(drop, 16)['item_id'], 0)
        self.assertNotIn(4356, self.server._inventory(self.c.session))


class MonsterLifecycle(InWorldTest):
    """cs-monster-death (+world-entity-lifecycle) and cs-combat-lock (roadmap S1-07, S1-08c, D4;
    P0 exit criterion 10). The tick scheduler runs on a fake clock through run_due()."""

    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.server.ticks = ticks.Scheduler(lock=self.server.world_lock, clock=self.clock, name='test')
        self.mobs = self.portal_to_102()
        # map 102 tile (1239,411): spawn on its floor line (1239..1439 @ 423); EN HP 5
        self.pupu = self.mobs[W.MOB_UID_BASE]

    def kill(self, mob):
        hits = 0
        while mob.alive and hits < 30:
            self.server._memory_melee(mob.x, mob.y)
            hits += 1
        self.t0 = self.clock.t
        return self.c.recv_until_quiet()

    def at(self, secs):
        """Run the timers due `secs` after the kill."""
        self.clock.t = self.t0 + secs
        return self.server.ticks.run_due()

    def report_hit(self, mob):
        """C2S 0x0D as the client sends it on the tick after its own hit detection caught a
        swing: interact event 7 in state_lo bits 16-19, the victim in the 43 B tail (61 B)."""
        self.c.send_c2s('0x42CE94/0x0D', {
            'realtime_delta_ms': 0, 'map_code': 102, 'logic_elapsed_ms': 30,
            'state_lo': 7 << 16, 'state_hi': 0, 'target_uid': mob.uid,
            'pos_x': mob.x, 'pos_y': mob.y, 'target_dx': 0.0, 'target_dy': 0.0,
            'flag_8db': False, 'flag_8e7': False, 'timer_dac': 0, 'target_action_event': 7})

    def test_client_hit_report_damages_and_releases_until_the_kill(self):
        uid = P.session_uid(self.c.session)
        self.pupu.hp = self.pupu.max_hp = 20                    # several hits before the kill
        releases = 0
        while True:
            self.report_hit(self.pupu)
            pkts = self.c.recv_until_quiet()
            if not self.pupu.alive:
                self.assertEqual(pkts[0].opcode, 0x29)            # the kill: 0x29 only, no 0x2A
                self.assertNotIn(0x2A, [p.opcode for p in pkts])
                break
            self.assertEqual([p.opcode for p in pkts], [0x2A])
            self.assertEqual(bytes(pkts[0].payload),
                             struct.pack('<I', self.pupu.uid) + bytes(8) + struct.pack('<I', uid))
            releases += 1
            self.assertLess(releases, 30)
        self.assertGreater(releases, 0)
        self.report_hit(self.pupu)                                   # a corpse takes no hit
        self.c.expect_silence()

    def test_swing_driver_deals_no_damage_by_default(self):
        self.assertFalse(self.server.config.get('DRIVER_MELEE'))

    def test_death_sends_hold_tick_and_spawn_point_exactly_once(self):
        pkts = self.kill(self.pupu)
        self.assertEqual([p.opcode for p in pkts], [0x29, 0x21, 0x18])
        self.assertEqual(self.s2c(pkts[0], 16), {'uid': self.pupu.uid, 'respawn_tick': 0x7FFFFFFF,
                                                 'respawn_x': 1339, 'respawn_y': 423})
        s = self.c.session
        self.server._resolve_hit(s['sock'], s, 0, self.pupu.uid)   # a dead monster takes no hit
        self.server._memory_melee(self.pupu.x, self.pupu.y)
        self.c.expect_silence()
        self.assertEqual(self.server.ticks.pending(), 2)

    def test_corpse_despawns_after_3s_and_respawns_at_the_spawn_point(self):
        self.pupu.x, self.pupu.y = 1300.0, 450.0               # died away from its spawn point
        self.kill(self.pupu)
        self.assertEqual(self.at(W.MOB_CORPSE_SECS - 0.1), 0)
        self.assertEqual(self.at(W.MOB_CORPSE_SECS), 1)
        self.assertEqual(self.s2c(self.c.expect(0x06), 4)['uid'], self.pupu.uid)
        self.assertEqual(self.at(W.MOB_RESPAWN_SECS - 0.1), 0)
        self.assertFalse(self.pupu.alive)
        self.assertEqual(self.at(W.MOB_RESPAWN_SECS), 1)
        block = self.s2c(self.c.expect(0x1A), 87)['repeat[count]'][0]
        self.assertEqual((block['uid'], block['pos_x'], block['pos_y'], block['home_x'], block['home_y'],
                          block['cur_hp'], block['action_state']),
                         (self.pupu.uid, 1339.0, 423.0, 1339, 423, 5, 8))
        self.assertTrue(self.pupu.alive)
        self.assertEqual((self.pupu.hp, self.pupu.x, self.pupu.y), (5, 1339.0, 423.0))
        self.assertEqual(self.server.ticks.pending(), 0)
        self.assertEqual(self.kill(self.pupu)[0].opcode, 0x29)    # the new life can die again

    def test_respawn_due_before_the_corpse_despawn_removes_the_corpse_first(self):
        self.addCleanup(setattr, W, 'MOB_RESPAWN_SECS', W.MOB_RESPAWN_SECS)
        W.MOB_RESPAWN_SECS = 1.0
        self.kill(self.pupu)
        self.assertEqual(self.at(1.0), 1)
        despawn, spawn = self.c.expect(0x06, 0x1A)            # never a 0x1A for a live uid
        self.assertEqual(self.s2c(despawn)['uid'], self.s2c(spawn)['repeat[count]'][0]['uid'])
        self.assertEqual(self.at(W.MOB_CORPSE_SECS), 0)       # the despawn timer was cancelled
        self.c.expect_silence()

    def test_a_portal_leaves_the_timers_with_the_map(self):
        # world-shared-monsters (P5 stage 3): the corpse and its timers belong to map 102, not
        # to the player; they run on the empty map and never reach the client on 101
        self.kill(self.pupu)
        self.c.send_c2s('0x42F76B/0x7E', {'portal_line_index': 31})    # 102 -> 101 (town, no monsters)
        self.c.expect(0x08, 0x03, 0x07, 0x28, 0x44)
        self.assertEqual(self.server.ticks.pending(), 2)
        self.assertEqual(self.at(60.0), 2)                     # despawn + respawn, on nobody's screen
        self.c.expect_silence()
        self.assertTrue(self.pupu.alive)
        mobs = self.portal_to_102()                            # the same 8 Pupu, same uids, all alive
        self.assertIs(mobs[self.pupu.uid], self.pupu)
        self.assertTrue(all(m.alive for m in mobs.values()))
        self.assertEqual(self.at(120.0), 0)

    def test_a_disconnect_leaves_the_timers_with_the_map(self):
        self.kill(self.pupu)
        self.assertEqual(self.server.ticks.pending(), 2)
        self.c.close()
        # the map keeps its monsters for MOB_MAP_KEEP_SECS; the timers send into no socket
        self.assertEqual(self.server.ticks.pending(), 2)
        self.assertEqual(self.at(60.0), 2)
        self.assertTrue(self.pupu.alive)

    def test_concurrent_hits_kill_exactly_once(self):
        self.pupu.hp = 1
        s = self.c.session
        barrier = threading.Barrier(8)

        def hit():
            barrier.wait()
            self.server._resolve_hit(s['sock'], s, 0, self.pupu.uid)
        threads = [threading.Thread(target=hit) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        opcodes = [p.opcode for p in self.c.recv_until_quiet()]
        self.assertEqual((opcodes.count(0x29), opcodes.count(0x21), opcodes.count(0x18)), (1, 1, 1))
        self.assertEqual(self.server.ticks.pending(), 2)

    def test_driver_thread_during_map_reloads(self):
        # S1-08c: the driver iterated mons.values() while _spawn_map_monsters rebuilt the dict.
        s = self.c.session
        stop, errors = threading.Event(), []

        def driver():
            while not stop.is_set():
                try:
                    self.server._memory_melee(1339.0, 423.0)
                except Exception as e:                        # noqa: BLE001
                    errors.append(e)
        t = threading.Thread(target=driver)
        t.start()
        try:
            for _ in range(15):
                self.server._spawn_map_monsters(s['sock'], s, 102)
                self.c.recv_until_quiet(0.02)
        finally:
            stop.set()
            t.join()
        self.c.recv_until_quiet()
        self.assertEqual(errors, [])
        self.assertEqual(len(s['monsters']), 8)


class MonsterRespawnTicker(InWorldTest):
    """Exit criterion 10 with the real scheduler thread: a driver-thread kill respawns with no
    C2S traffic at all (respawns used to run only on C2S 0x38/0x25, S1-07)."""

    def test_driver_kill_despawns_and_respawns_on_the_tick_thread(self):
        for name, value in (('MOB_CORPSE_SECS', 0.2), ('MOB_RESPAWN_SECS', 0.6)):
            self.addCleanup(setattr, W, name, getattr(W, name))
            setattr(W, name, value)
        pupu = self.portal_to_102()[W.MOB_UID_BASE]
        self.server.ticks.start()
        self.addCleanup(self.server.ticks.stop)

        def driver():
            while pupu.alive:
                self.server._memory_melee(pupu.x, pupu.y)
        t = threading.Thread(target=driver)
        t.start()
        t.join(5)
        pkts = self.c.recv_until_quiet(quiet=1.0, timeout=5.0)
        self.assertEqual([p.opcode for p in pkts], [0x29, 0x21, 0x18, 0x06, 0x1A])
        self.assertEqual(self.s2c(pkts[4], 87)['repeat[count]'][0]['uid'], pupu.uid)
        self.assertTrue(pupu.alive)


class DeadCodeRemoved(unittest.TestCase):
    """cs-cleanup, world-cleanup (+pvp-stale-cleanup): no route or caller can reach them."""

    def test_dead_builders_and_handlers_are_gone(self):
        for name in ('_handle_attack', '_handle_melee', '_handle_use_skill', '_send_hit_feedback',
                     '_build_opcode_54', '_build_pyslayer_opcode_07', '_build_pyslayer_enter_world',
                     '_build_enter_world_response', '_build_map_enter_packet', '_fake_map_server',
                     '_spawn_test_monster', '_reply_empty_0x63',
                     # stage 4: replaced by packets.build / the tick scheduler / _notice
                     '_tick_respawns', '_send_chat_line', '_build_opcode_23', '_build_opcode_29',
                     '_send_death', '_build_opcode_1A', '_build_opcode_18', '_build_opcode_19',
                     '_build_en_opcode_1D_equip', '_send_buy_result', '_send_sell_result',
                     # P1 stage 2: the hand-packed 0x07 is records.player_record, _send_exp
                     # is grant_exp, and the owner never gets a 0x22 (lc-exp-persist).
                     '_build_en_opcode_07', '_build_en_opcode_07_packet', '_send_exp', '_send_level'):
            with self.subTest(name):
                self.assertFalse(hasattr(W.GameServer, name))
        self.assertFalse(hasattr(W, 'UDPMapServer'))

    def test_routes_and_labels(self):
        routes = W.GameServer.ROUTES
        self.assertEqual(routes[0x38].handler, '_handle_battlefield_info')
        self.assertEqual(routes[0x63].handler, '_handle_card_deck_list')
        # 0x25 is the trade final confirm since P7 stage 1 (trade.py), never _handle_attack
        self.assertEqual(routes[0x25].handler, '_handle_trade_confirm')
        for op in (0x93, 0x94, 0x95, 0x97, 0x99):
            self.assertEqual(routes[op].handler, '_warn_dead_host_code')
        self.assertTrue(all(r.fallback for op, r in routes.items() if op in (0x15, 0x1A, 0x25)))
        self.assertEqual(routes[0x2F].handler, '_handle_friend_list')     # P6 stage 2 (S2-03)
        self.assertIn('arena room list close', routes[0x2D].log)
        self.assertFalse(hasattr(W, 'MONSTER_DB'))            # world-template-map
        self.assertFalse(hasattr(W, 'MAP_SPAWNS'))
        self.assertEqual(EC.npc_name(1), 'Pupu')

    def test_monster_spawn_record_carries_the_idle_defaults(self):
        # world-1a-defaults replaced the 89 B live-proven record (non-idle seed 904 = 0,
        # facing 127,0,0,1, 954 = 32, 8CF = 1, E00 = 501, effect 0x0B3B): same layout, the
        # client's own idle values, no effect entry, server_controlled 1 + hold 0 = 87 B.
        # test_world_content.SpawnRecordBytes packs more templates field by field.
        idle = bytes.fromhex('01 01000000 00000f00 00 00 00 00000000 00000000 d7040000 9b010000'
                             '00000000 00000000 00 08 08000000 00000000 02 00000000005c9340 0000000000b07940'
                             '00000000 08 00 00 00 00 00 00 00 00 1800 00000000 01 00000000')
        mob = W.Monster(uid=0xF0000, npccode=1, name='Pupu', level=1, hp=24, max_hp=24, body_atk=3, defense=2,
                        exp=10, x=1239.0, y=411.0, spawn_x=1239.0, spawn_y=411.0)
        stub = mock.Mock(config=W.cfgmod.from_dict({'MOB_SERVER_CONTROLLED': True}))   # builder reads only config
        body = P.build('0x1A', {'count': 1, 'repeat[count]': [W.GameServer._monster_spawn_record(stub, mob)]})
        self.assertEqual(body, idle)


class UidOnline(ServerTest):
    """lc-uid-online (+party-mp-identity, trade-mp-uid; roadmap F3, S1-04) and the store
    wiring of lc-data-model (F4) over the fake client: per-account uids, one session per
    account, exp in the store (P1 exit criteria 5, 6, 8)."""

    def create(self, c, name, looks=(3, 2, 4, 5, 3), stats=(3, 2, 1, 3), result=1):
        """C2S 0x0E -> the S2C 0x1C result (lc-create; no 0x02 follows a create)."""
        s10, s1, s6, s5, s9 = looks
        c.send_c2s('0x449384/0x0E', {'look_slot10': s10, 'look_slot1': s1, 'look_slot6': s6,
                                     'look_slot5': s5, 'look_slot9': s9, 'name': name,
                                     'str': stats[0], 'dex': stats[1], 'int': stats[2], 'tol': stats[3]})
        rec = self.s2c(c.expect(0x1C), 1)
        self.assertEqual(rec['result'], result, f'S2C 0x1C for {name!r}')
        return rec

    def disk(self):
        with open(self.server.db_file, encoding='utf-8') as f:
            return json.load(f)

    def test_uids_are_per_account_and_every_own_record_carries_it(self):
        test, admin = self.client(), self.client()
        self.assertEqual(self.login(test)['account_id'], 1)
        rec = self.login(admin, 'admin', 'admin')
        self.assertEqual(rec['account_id'], 2)
        self.assertEqual((test.session['uid'], admin.session['uid']), (1, 2))
        self.assertIs(self.server.world.session(2), admin.session)

        self.create(admin, 'Nova')
        nova = self.server.store.find_character('admin', 'Nova')
        self.assertEqual((nova['name'], nova['class'], nova['job2'], nova['exp']), ('Nova', 0, 0, 0))
        _, spawn, _, _, _ = self.enter_world(admin, 'Nova')
        row = self.s2c(spawn)['repeat[player_count]'][0]
        self.assertEqual((row['uid'], row['job'], row['level']), (2, 0, 1))   # registration gate: uid == 0x02 account_id
        self.assertIs(self.server.world.by_char_name('nova'), admin.session)

        admin.send(0x7E, CAP_7E_101_TO_102)                   # the portal replay carries uid 2 too
        pkts = admin.expect(0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(8))
        self.assertEqual(self.s2c(pkts[2])['repeat[player_count]'][0]['uid'], 2)
        admin.send_c2s('0x47734D/0x16', {'quest_id': 26})
        admin.expect(0x26, 0x59)
        admin.send(0x0F, CAP_0F_EQUIP_STICK)
        self.assertEqual(self.s2c(admin.expect(0x1D), 9)['uid'], 2)
        admin.send_c2s(P.variants(0x04, 'C2S')[0]['key'], {'stat_index': 0})
        self.assertEqual(self.s2c(admin.expect(0x14), 7)['uid'], 2)

    def test_duplicate_login_closes_the_old_session_and_refuses_with_result_4(self):
        first = self.client()
        self.login(first)
        self.enter_world(first)
        old = first.session
        second = self.client()
        with self.assertLogs('WS', logging.INFO) as cm:
            second.send_c2s('0x44D8BF/0x01', {'account_id': 'test', 'password': 'test'})
            rec = self.s2c(second.expect(0x02), 1)
        self.assertEqual(rec['result'], 4)                    # "Connection already exists..."
        self.assertIsNone(second.session['username'])         # not logged in
        self.assertTrue(any('already online (uid 1)' in line for line in cm.output))
        # client 1 is disconnected: its socket is closed and its session is gone
        with self.assertRaises((ConnectionError, OSError)):
            while first.recv(2.0) is not None:
                pass
        self.assertTrue(_wait(lambda: old['addr'] not in self.server.sessions))
        self.assertIsNone(self.server.world.session(1))
        self.assertIsNone(self.server.world.by_char_name('TestHero'))
        self.assertTrue(old.get('kicked'))
        # the user logs in again (the real client reconnects): accepted, no hang
        rec = self.login(second)
        self.assertEqual(rec['account_id'], 1)
        self.enter_world(second)
        self.assertIs(self.server.world.session(1), second.session)

    def test_duplicate_login_at_character_select_and_other_accounts_unaffected(self):
        a, b, other = self.client(), self.client(), self.client()
        self.login(a)
        self.login(other, 'admin', 'admin')
        b.send_c2s('0x44D8BF/0x01', {'account_id': 'test', 'password': 'test'})
        self.assertEqual(self.s2c(b.expect(0x02), 1)['result'], 4)
        self.assertTrue(_wait(lambda: a.addr not in self.server.sessions))
        self.assertIs(self.server.world.session(2), other.session)
        # a wrong password never kicks the online session
        self.login(b)
        c = self.client()
        c.send_c2s('0x44D8BF/0x01', {'account_id': 'test', 'password': 'nope'})
        self.assertEqual(self.s2c(c.expect(0x02), 1)['result'], 0x11)
        self.assertIs(self.server.world.session(1), b.session)
        b.expect_silence(0.2)

    def test_disconnect_leaves_the_online_indexes(self):
        c = self.client()
        self.login(c)
        self.enter_world(c)
        c.close()
        self.assertTrue(_wait(lambda: self.server.world.session(1) is None))
        self.assertEqual(self.server.world.by_name, {})

    def test_create_writes_the_lc_data_model_record(self):
        c = self.client()
        self.login(c, 'admin', 'admin')
        self.create(c, 'Nova', looks=(2, 3, 4, 5, 3))
        nova = self.disk()['admin']['characters'][0]
        for key, value in {'class': 0, 'job2': 0, 'exp': 0, 'look': [0, 3, 0, 0, 2, 5, 4, 0, 0, 3, 2, 0, 0, 2],
                           'map': 101, 'x': 700.0, 'y': 812.0, 'str': 3, 'spr': 3, 'fame': 0}.items():
            self.assertEqual(nova[key], value, key)
        self.assertNotIn('level', nova)
        self.assertGreater(nova['created_at'], 1_700_000_000)
        self.assertEqual(self.server.store.name_owner('NOVA'), 'admin')

    def test_fixture_was_migrated_with_a_backup_in_the_temp_dir(self):
        data = self.disk()
        self.assertEqual((data['test']['uid'], data['admin']['uid']), (1, 2))
        hero = data['test']['characters'][0]
        self.assertEqual((hero['class'], hero['exp'], hero['look']), (0, 0, [0, 1, 0, 0, 1, 2, 2, 0, 0, 2, 1, 0, 0, 1]))
        backup = self.server.db_file + '.bak-pre-p1'
        self.assertTrue(os.path.exists(backup))
        self.assertEqual(os.path.dirname(backup), self.tmp)
        with open(backup, encoding='utf-8') as f:
            self.assertEqual(json.load(f), F.DEFAULT_ACCOUNTS)

    def test_exp_lives_in_the_store_and_survives_relog(self):
        c = self.client()
        self.login(c)
        self.enter_world(c)
        c.send(0x7E, CAP_7E_101_TO_102)
        c.expect(0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(8))
        s = c.session
        for mob in s['monsters'].values():
            mob.exp = 10                                       # EN Pupu pays 5; keep 60 = 6 x 10
        with self.assertLogs('WS', logging.INFO):
            for _ in range(6):                                 # 6 Pupu x 10 exp = 60: level 2 at 56
                pupu = next(m for m in s['monsters'].values() if m.alive)
                while pupu.alive:
                    self.server._memory_melee(pupu.x, pupu.y, uid=1)
        pkts = c.recv_until_quiet()
        deltas = [self.s2c(p)['exp_delta'] for p in pkts if p.opcode == 0x21]
        self.assertEqual(deltas, [10] * 6)
        # lc-exp-persist / F6: the owner gets 0x21 only. The client crosses its own 56
        # threshold and plays the level-up effect once; the 0x22 the server used to send
        # afterwards played it a second time (S2-43, B4).
        self.assertEqual([p.opcode for p in pkts if p.opcode == 0x22], [])
        hero = self.server.store.find_character('test', 'TestHero')
        self.assertEqual(hero['exp'], 60)
        self.assertTrue(self.server.store.dirty)
        c.close()
        self.assertTrue(_wait(lambda: self.server.world.session(1) is None))
        self.assertTrue(self.server.store.flush() or not self.server.store.dirty)
        self.assertEqual(self.disk()['test']['characters'][0]['exp'], 60)

        again = self.client()
        rec = self.login(again)
        self.assertEqual(rec['repeat[char_count]'][0]['total_exp'], 60)
        self.assertEqual(rec['repeat[char_count]'][0]['job_branch'], 0)
        # The exp test portalled to 102 first, and world-persistence means the relog
        # loads that map with its 8 monsters.
        # state_len=None: the 6 Pupu dropped loot, so this 0x03 carries a real bag now.
        state, spawn, _, _, _ = self.enter_world(again, monsters=8, state_len=None)[:5]
        self.assertEqual(self.s2c(state)['map_code'], 102)
        self.assertEqual(self.s2c(state)['exp_total'], 60)     # not the old constant 30000
        self.assertEqual(self.s2c(spawn)['repeat[player_count]'][0]['level'], 2)

    def test_debounced_save_reaches_the_disk_on_the_tick_thread(self):
        self.server.store.debounce_secs = 0.2
        self.server.ticks.start()
        self.addCleanup(self.server.ticks.stop)
        c = self.client()
        self.login(c)
        self.enter_world(c)
        s = c.session
        self.server.grant_exp(s, 177)
        c.recv_until_quiet()
        self.assertTrue(_wait(lambda: self.disk()['test']['characters'][0]['exp'] == 177, timeout=5.0))
        self.assertFalse(self.server.store.dirty)

    def test_memory_driver_swings_as_the_session_of_the_client_uid(self):
        a, b = self.client(), self.client()
        self.login(a)
        self.enter_world(a)
        self.login(b, 'admin', 'admin')
        self.create(b, 'Nova')
        self.enter_world(b, 'Nova')
        for c in (a, b):
            c.send(0x7E, CAP_7E_101_TO_102)
            # world-presence (P5 stage 2): B first sees A leave 101 (0x06), then finds A on 102
            # (0x04 in its map load, and A gets B as a 0x05) - F.expect_entry checks both.
            if c is b:
                self.assertEqual(self.s2c(b.expect(0x06, 0x08, 0x03, 0x07, 0x04, 0x28, 0x44,
                                                   *F.mob_packets(8))[0]), {'uid': 1})
                a.expect(0x05)
            else:
                F.expect_entry(c, (0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(8)), [a, b], 102)
        mob_a = a.session['monsters'][W.MOB_UID_BASE]
        mob_b = b.session['monsters'][W.MOB_UID_BASE]
        self.assertIs(mob_a, mob_b)                             # world-shared-monsters: ONE Pupu
        mob_b.hp = mob_b.max_hp = 20
        self.server._memory_melee(mob_b.x, mob_b.y, uid=2)
        self.assertLess(mob_b.hp, mob_b.max_hp)                 # B's swing
        a.expect_silence(0.2)                                   # a survivor: no packet (no aggro)
        b.expect_silence(0.1)
        hp = mob_b.hp
        self.server._memory_melee(mob_b.x, mob_b.y, uid=99)     # no such session: nothing
        self.assertEqual(mob_b.hp, hp)
        a.expect_silence(0.1)
        b.expect_silence(0.1)
        # B's kill: the 0x29 reaches both clients, the exp and the gold only B
        mob_b.hp = 1
        self.server._memory_melee(mob_b.x, mob_b.y, uid=2)
        self.assertEqual([p.opcode for p in b.recv_until_quiet()], [0x29, 0x21, 0x18])
        self.assertEqual([p.opcode for p in a.recv_until_quiet()], [0x29])

    def test_driver_finds_the_local_player_by_scene_uid(self):
        a = self.client()
        self.login(a, 'admin', 'admin')
        scene, node1, node2, ent1, ent2 = 0x500000, 0x600000, 0x620000, 0x610000, 0x630000
        # the 2008 layout (client_layout.LAYOUT_2008): scene 0x70EECC, uid scene+0x220 / +0x84
        scene_ptr = self.server.client_layout.scene_ptr
        self.assertEqual(scene_ptr, 0x70EECC)
        mem = {scene_ptr: scene, scene + 0x220: 2, scene + 0xC: node1,
               node1 + 8: ent1, node1: node2, node2 + 8: ent2, node2: 0,
               ent1 + 0x84: 1, ent2 + 0x84: 2}
        u32 = lambda addr: mem.get(addr, 0)                    # noqa: E731
        self.assertEqual(self.server._driver_local_player(u32), (ent2, 2))   # not the uid-1 entity
        mem[scene + 0x220] = 1                                  # uid 1 has no online session
        self.assertEqual(self.server._driver_local_player(u32), (0, 0))
        mem[scene + 0x220] = 2
        mem[ent2 + 0x84] = 7                                    # not spawned yet
        self.assertEqual(self.server._driver_local_player(u32), (0, 2))
        mem[scene_ptr] = 0
        self.assertEqual(self.server._driver_local_player(u32), (0, 0))

    def test_no_uid_1_defaults_remain(self):
        with open(os.path.join(HERE, 'windslayer_server.py'), encoding='utf-8') as f:
            src = f.read()
        for pattern in ("get('uid', 1)", 'uid=1', "'account_id'] = 1", "session.get('account_id', 1)",
                        'u32(player + 0x84) == 1', 'EXP_TABLE =', '_DEFAULT_EXP', "session.get('exp'",
                        # world-player-record: the record's +0x8B3 bytes are the idle input
                        # state, never the four octets of an IP address (S2-27).
                        'client_ip'):
            with self.subTest(pattern):
                self.assertFalse(pattern in src, f'{pattern!r} is back in windslayer_server.py')


class ExpRecordsCharList(ServerTest):
    """P1 stage 2 over the fake client: lc-exp-table / lc-exp-persist (0x21 only, one
    level-up at the client's own threshold), world-player-record + lc-spawn-fields (the
    own 0x07 is the store) and lc-charlist (0x02 is the store)."""

    def hero(self):
        return self.server.store.find_character('test', 'TestHero')

    def test_character_list_is_the_store(self):
        with self.server.store.lock:
            account = self.server.store.account('test')
            account.update(gender=1, manner=-7, first_purchase_notice=True)   # P8 wallet field
            account['characters'].append(
                self.server.store.new_character('Tier2', s10=2, s1=3, s6=4, s5=5, s9=3,
                                                stats=(3, 2, 1, 3), now=0))
            account['characters'][1].update({'class': 1, 'job2': 2, 'exp': 30_485})
            hero_look = account['characters'][0]['look']
        c = self.client()
        pkt = c.send_c2s('0x44D8BF/0x01', {'account_id': 'test', 'password': 'test'}) and c.expect(0x02)
        rec = self.s2c(pkt, 13 + 75 * 2)                      # S2-46: no 16 trailing bytes
        self.assertEqual((rec['result'], rec['account_id']), (1, 1))
        self.assertEqual((rec['account_gender_flag'], rec['manner_points'],
                          rec['cash_first_purchase_flag']), (1, -7, 1))
        self.assertEqual(rec['transfer_status'], 3)
        hero, tier2 = rec['repeat[char_count]']
        self.assertEqual((hero['name'], hero['class_id'], hero['job_branch'], hero['total_exp']),
                         ('TestHero', 0, 0, 0))
        self.assertEqual([e['appearance'] for e in hero['repeat[14]']], hero_look)
        # job_branch is the job tier; the level the select screen shows comes from total_exp.
        self.assertEqual((tier2['class_id'], tier2['job_branch'], tier2['total_exp']),
                         (1, 2, 30_485))
        self.assertEqual([hero[k] for k in ('rank_1', 'rank_2', 'rank_3')], [0, 0, 0])

    def test_own_spawn_record_is_the_store(self):
        with self.server.store.lock:
            self.server.store.account('test').update(gender=1, manner=-3)
            char = self.hero()
            # A record stored before the server composed looks: worn Wooden Sword (9, Spr 5)
            # and Blue Novice Hat (2068, Spr 360) under the creation look, and a stale legacy
            # `weapon` item id that must never reach the wire again.
            char.update({'exp': 177, 'str': 7, 'dex': 6, 'int': 5, 'spr': 4, 'weapon': 0xB3,
                         'hp': 63, 'mp': 21, 'x': 1411.0, 'y': 714.0,
                         'equipped': {5: {'id': 9, 'w': [0] * 6}, 0: {'id': 2068, 'w': [0] * 6}}})
            look = list(char['look'])
        c = self.client()
        self.login(c)
        _, spawn, _, hp, _ = self.enter_world(c)
        row = self.s2c(spawn)['repeat[player_count]'][0]
        self.assertEqual((row['name'], row['uid'], row['karma']), ('TestHero', 1, -3))
        self.assertEqual((row['job'], row['job2'], row['level'], row['gender']), (0, 0, 3, 1))
        # fame 0 -> rank 0 'Trainee' (the client's own table 0x6F11D8; P6 lc-player-info:
        # 99 made the Player Info window index past its rank names)
        self.assertEqual(row['rank_icon'], 0)
        self.assertEqual((row['stat_str'], row['stat_dex'], row['stat_int'], row['stat_tol']),
                         (7, 6, 5, 4))
        self.assertEqual((row['cur_hp'], row['cur_mp']), (63, 21))    # 0 would render dead
        self.assertEqual(self.s2c(hp)['hp'], 63)
        # the saved (1411, 714), 100 px above 101's floor, settled onto it (livetest bug 5)
        self.assertEqual((row['pos_x'], row['pos_y']), (1411.0, 814.0))
        # composed from the worn items at login (GameServer._compose_looks), as the client's
        # own 0x1D composition would have: the sword (Spr 5) and the hat (Spr 360)
        expect = list(look)
        expect[10], expect[11] = 360, 5
        self.assertEqual([e['appearance_part'] for e in row['repeat[14]']], expect)
        self.assertEqual((self.hero()['look'], self.hero()['weapon']), (expect, 5))
        # Idle motion, no IP octets (S2-27), no room/GM/stall blocks.
        self.assertEqual({k: row[k] for k in W.R.IDLE_MOTION}, W.R.IDLE_MOTION)
        self.assertEqual((row['room_id'], row['gm_level'], row['shop_open']), (0, 0, 0))

    def test_portal_replays_the_same_record_at_the_arrival_point(self):
        with self.server.store.lock:
            self.hero()['exp'] = 30_485
        c = self.client()
        self.login(c)
        self.enter_world(c)
        c.send(0x7E, CAP_7E_101_TO_102)
        pkts = c.expect(0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(8))
        # The replayed 0x03 re-installs the exp baseline, so it must be the store's total
        # (the old constant 30000 auto-levelled the client on the next kill, B2).
        self.assertEqual(self.s2c(pkts[1], 64)['exp_total'], 30_485)
        row = self.s2c(pkts[2], 369)['repeat[player_count]'][0]
        self.assertEqual((row['uid'], row['level'], row['name']), (1, 13, 'TestHero'))
        # the portal's arrival point, settled onto 102's floor 100 px below it (livetest bug 5)
        ax, ay = (float(v) for v in self.server._get_portals()['101_23'][1:])
        self.assertEqual((row['pos_x'], row['pos_y']), (ax, ay + 100.0))
        self.assertEqual({k: row[k] for k in W.R.IDLE_MOTION}, W.R.IDLE_MOTION)

    def test_level_up_happens_once_at_the_client_threshold(self):
        c = self.client()
        self.login(c)
        self.enter_world(c)
        s = c.session
        with self.server.store.lock:
            self.hero()['exp'] = 55                            # one exp short of level 2
        with self.assertLogs('WS', logging.INFO) as cm:
            self.assertEqual(self.server.grant_exp(s, 1), 1)   # crosses 56
            self.assertEqual(self.server.grant_exp(s, 4), 4)   # no second threshold
        self.assertEqual([self.s2c(p)['exp_delta'] for p in c.recv_until_quiet()], [1, 4])
        self.assertEqual(sum('[LEVEL]' in line for line in cm.output), 1)
        self.assertEqual((self.hero()['exp'], s['level']), (60, 2))
        # The client full-heals itself on its level-up; the server mirrors it so the next
        # 0x28/0x44 and 0x07 agree (cs-hp-mp-model owns the max formulas).
        self.assertEqual((s['hp'], s['mp']), (s['max_hp'], s['max_mp']))
        # The level is derived from the persisted exp, so a relog shows the same one.
        c.close()
        self.assertTrue(_wait(lambda: self.server.world.session(1) is None))
        again = self.client()
        rec = self.login(again)
        self.assertEqual(rec['repeat[char_count]'][0]['total_exp'], 60)
        _, spawn, _, _, _ = self.enter_world(again)
        self.assertEqual(self.s2c(spawn)['repeat[player_count]'][0]['level'], 2)

    def test_exp_is_clamped_and_never_sent_before_the_own_0x07(self):
        c = self.client()
        self.login(c)
        s = c.session
        self.assertEqual(self.server.grant_exp(s, 500), 500)   # at character select
        c.expect_silence(0.2)                                  # 0x21 with no local player is dropped
        self.assertEqual(self.hero()['exp'], 500)
        self.enter_world(c)
        self.assertEqual(self.server.grant_exp(s, 0), 0)       # nothing to apply: no packet
        self.assertEqual(self.server.grant_exp(s, -10_000), -500)   # clamped at 0
        self.assertEqual(self.server.grant_exp(s, 10 ** 12), W.progression.EXP_MAX)
        self.assertEqual([self.s2c(p)['exp_delta'] for p in c.recv_until_quiet()],
                         [-500, W.progression.EXP_MAX])
        self.assertEqual(self.hero()['exp'], W.progression.EXP_MAX)
        self.assertEqual(self.server.grant_exp(s, 1), 0)       # already at the cap


class CharacterSelectFlows(ServerTest):
    """P1 stage 3 over the fake client: lc-create (C2S 0x0E -> S2C 0x1C), lc-delete
    (0x51 -> 0x80, 0x12 -> 0x1F), lc-enter-world (0x2B: flags, p2p, saved map),
    lc-stats (0x04 -> 0x14), lc-login-errors (0x02 result codes) and
    lc-id-transfer-stub (0x65/0x66 -> 0x8C)."""

    LOOKS = (3, 2, 4, 5, 3)            # s10, s1, s6, s5, s9 inside the gender-0 ranges

    def reconfigure(self, **overrides):
        """Restart the server on this test's temp dir with config overrides."""
        for c in self.clients:
            c.close()
        self.clients = []
        self.server = F.make_server(self.tmp, config=W.cfgmod.from_dict(overrides))
        return self.server

    def create(self, c, name, looks=None, stats=(3, 2, 1, 3), result=1):
        s10, s1, s6, s5, s9 = looks or self.LOOKS
        c.send_c2s('0x449384/0x0E', {'look_slot10': s10, 'look_slot1': s1, 'look_slot6': s6,
                                     'look_slot5': s5, 'look_slot9': s9, 'name': name,
                                     'str': stats[0], 'dex': stats[1], 'int': stats[2], 'tol': stats[3]})
        rec = self.s2c(c.expect(0x1C), 1)
        self.assertEqual(rec['result'], result, f'S2C 0x1C for {name!r}')
        return rec

    def delete(self, c, name, password='test', result=1):
        c.send_c2s('0x44ABA4/0x12', {'char_name': name, 'confirm_password': password})
        rec = self.s2c(c.expect(0x1F), 1)
        self.assertEqual(rec['result'], result, f'S2C 0x1F for {name!r}')
        return rec

    def disk(self):
        with open(self.server.db_file, encoding='utf-8') as f:
            return json.load(f)

    # ------------------------------------------------------------- lc-create ---
    def test_create_answers_0x1C_and_writes_a_novice(self):
        c = self.client()
        self.login(c)
        with self.assertLogs('WS', logging.INFO) as cm:
            self.create(c, 'Nova')
        nova = self.server.store.find_character('test', 'Nova')
        self.assertEqual((nova['class'], nova['job2'], nova['exp']), (0, 0, 0))
        self.assertEqual(nova['look'], [0, 2, 0, 0, 3, 5, 4, 0, 0, 3, 3, 0, 0, 3])
        self.assertEqual((nova['map'], nova['x'], nova['y']), (101, 700.0, 812.0))
        self.assertEqual([nova[k] for k in ('str', 'dex', 'int', 'spr')], [3, 2, 1, 3])
        self.assertEqual([ch['name'] for ch in self.disk()['test']['characters']], ['TestHero', 'Nova'])
        self.assertTrue(any('Novice at map 101' in line for line in cm.output))
        # The select screen shows it as a Lv.1 Novice after a relog (0x02 from the store).
        c.close()
        again = self.client()
        row = self.login(again)['repeat[char_count]'][1]
        self.assertEqual((row['name'], row['class_id'], row['job_branch'], row['total_exp']),
                         ('Nova', 0, 0, 0))

    def test_starter_weapon_is_off_by_default_and_validated_against_the_en_catalog(self):
        c = self.client()
        self.login(c)
        self.create(c, 'Bare')
        self.assertNotIn('weapon', self.server.store.find_character('test', 'Bare'))
        self.reconfigure(STARTER_WEAPON=179)               # EN Wooden Stick
        c = self.client()
        self.login(c)
        self.create(c, 'Armed')
        char = self.server.store.find_character('test', 'Armed')
        # worn (grid slot 5, the 0x07 equip id) and composed like a 0x1D: look word 11 is
        # the stick's Spr_Num 1, never the item id the old code wrote into `weapon`
        self.assertEqual(char['equipped'], {5: {'id': 179, 'w': [0] * 6}})
        self.assertEqual(W.R.appearance(char)[11], W.en_item(179).spr_num)
        self.assertEqual(W.R.appearance(char)[:11], W.storemod.compose_look(3, 2, 4, 5, 3)[:11])
        self.reconfigure(STARTER_WEAPON=60000)             # not in the EN catalog
        c = self.client()
        self.login(c)
        with self.assertLogs('WS', logging.WARNING):
            self.create(c, 'Fake')
        self.assertNotIn('weapon', self.server.store.find_character('test', 'Fake'))

    def test_create_never_sends_a_second_0x02_unless_configured(self):
        c = self.client()
        self.login(c)
        self.create(c, 'Nova')                         # 0x1C only: expect() fails on extras
        self.reconfigure(CREATE_REPLY_0x02=True)
        c2 = self.client()
        self.login(c2)
        c2.send_c2s('0x449384/0x0E', dict(zip(('look_slot10', 'look_slot1', 'look_slot6',
                                               'look_slot5', 'look_slot9'), self.LOOKS),
                                          name='Nova', str=3, dex=2, int=1, tol=3))
        result, chars = c2.expect(0x1C, 0x02)
        self.assertEqual(self.s2c(result, 1)['result'], 1)
        self.assertEqual([ch['name'] for ch in self.s2c(chars)['repeat[char_count]']],
                         ['TestHero', 'Nova'])

    def test_name_in_use_is_result_2_case_insensitively_across_accounts(self):
        c = self.client()
        self.login(c)
        self.create(c, 'Nova')
        self.create(c, 'nova', result=2)
        self.create(c, 'NOVA', result=2)
        self.assertEqual(len(self.server.store.characters('test')), 2)
        other = self.client()
        self.login(other, 'admin', 'admin')
        self.create(other, 'nOvA', result=2)           # names are globally unique (F3)
        self.assertEqual(self.server.store.characters('admin'), [])
        self.create(other, 'TESTHERO', result=2)       # the migrated fixture name too

    def test_invalid_names_are_result_4(self):
        c = self.client()
        self.login(c)
        for name in ('', 'two words', 'wind', 'Windy', 'GameMaster', 'admin!', 'Nova-2',
                     'Mädchen'):
            with self.subTest(name=name):
                self.create(c, name, result=4)
        # 17 bytes with no NUL: the encoder would cut it, so this one goes out raw. The
        # client's str[17] has no room for the terminator, which is the S1-13 overflow.
        c.send(0x0E, struct.pack('<5H', *self.LOOKS) + b'A' * 17 + struct.pack('<4H', 3, 2, 1, 3))
        self.assertEqual(self.s2c(c.expect(0x1C), 1)['result'], 4)
        self.assertEqual(len(self.server.store.characters('test')), 1)
        self.create(c, 'A' * 16)                       # 16 bytes is the limit, not 17

    def test_cap_stat_sum_and_look_range_are_result_3(self):
        c = self.client()
        self.login(c)
        self.create(c, 'Stats1', stats=(4, 2, 1, 3), result=3)      # sum 10 != stat_total(1)
        self.create(c, 'Stats2', stats=(0, 0, 0, 0), result=3)
        self.create(c, 'Look1', looks=(5, 2, 4, 5, 3), result=3)    # s10 outside 1..4
        self.create(c, 'Look2', looks=(3, 2, 4, 5, 9), result=3)    # s9 outside 2..5
        self.create(c, 'Look3', looks=(0x65, 2, 4, 5, 3), result=3)  # the gender-1 body set
        self.assertEqual(len(self.server.store.characters('test')), 1)
        for i in range(4):                                          # fill the 5 slots
            self.create(c, f'Slot{i}')
        self.create(c, 'Sixth', result=3)
        self.assertEqual(len(self.server.store.characters('test')), 5)

    def test_gender_1_account_uses_the_101_based_look_ranges(self):
        with self.server.store.lock:
            self.server.store.account('admin')['gender'] = 1
        c = self.client()
        self.login(c, 'admin', 'admin')
        self.create(c, 'Lady', looks=(0x66, 0x65, 0x67, 0x66, 4))
        self.assertEqual(self.server.store.find_character('admin', 'Lady')['look'],
                         [0, 0x65, 0, 0, 0x66, 0x66, 0x67, 0, 0, 4, 0x66, 0, 0, 0x66])
        self.create(c, 'Lass', looks=(3, 2, 4, 5, 3), result=3)     # gender-0 set refused

    # ------------------------------------------------------------- lc-delete ---
    def test_delete_flow_password_window_then_0x12(self):
        c = self.client()
        self.login(c)
        self.create(c, 'Nova')
        # Dialog 0x201 OK -> C2S 0x51 {password, 0x235}. Without S2C 0x80 {1, 0x235} the
        # client never opens the confirm window and never sends 0x12 (live #07).
        c.send_c2s('0x460831/0x51', {'password': 'test', 'target_window_id': 0x235})
        rec = self.s2c(c.expect(0x80), 5)
        self.assertEqual((rec['result'], rec['target_window_id']), (1, 0x235))
        self.delete(c, 'Nova')
        self.assertIsNone(self.server.store.find_character('test', 'Nova'))
        self.assertIsNone(self.server.store.name_owner('nova'))
        self.assertEqual([ch['name'] for ch in self.disk()['test']['characters']], ['TestHero'])
        # The slot stays empty after a relog, and the name is free again.
        c.close()
        again = self.client()
        self.assertEqual([ch['name'] for ch in self.login(again)['repeat[char_count]']], ['TestHero'])
        self.create(again, 'Nova')

    def test_delete_password_window_refuses_a_wrong_password(self):
        c = self.client()
        with self.assertLogs('WS', logging.WARNING):        # before login: no reply at all
            c.send_c2s('0x460831/0x51', {'password': 'test', 'target_window_id': 0x235})
            c.expect_silence(0.25)
        self.login(c)
        c.send_c2s('0x460831/0x51', {'password': 'nope', 'target_window_id': 0x235})
        self.assertEqual(self.s2c(c.expect(0x80), 1)['result'], 0)
        # The bank window needs a character in world (shop_storage-password-gate): at the
        # select screen the right password still gets 0x80 {0}.
        c.send_c2s('0x460831/0x51', {'password': 'test', 'target_window_id': 0x1A7})
        self.assertEqual(self.s2c(c.expect(0x80), 1)['result'], 0)

    def test_delete_refusals(self):
        c = self.client()
        self.login(c)
        self.create(c, 'Nova')
        self.delete(c, 'Ghost', result=2)                  # not a character of this account
        self.delete(c, 'Nova', password='nope', result=12)
        self.assertIsNotNone(self.server.store.find_character('test', 'Nova'))
        other = self.client()
        self.login(other, 'admin', 'admin')
        self.delete(other, 'Nova', password='admin', result=2)    # another account's name
        self.assertIsNotNone(self.server.store.find_character('test', 'Nova'))

    def test_delete_min_age_is_result_3(self):
        self.reconfigure(DELETE_MIN_AGE_HOURS=24.0)
        c = self.client()
        self.login(c)
        self.create(c, 'Nova')
        self.delete(c, 'Nova', result=3)
        with self.server.store.lock:                       # created 25 h ago: allowed
            self.server.store.find_character('test', 'Nova')['created_at'] = time.time() - 25 * 3600
        self.delete(c, 'Nova')

    def test_deleting_the_character_a_session_holds_clears_its_refs(self):
        c = self.client()
        self.login(c)
        self.create(c, 'Nova')
        self.enter_world(c, 'Nova')
        self.assertIs(self.server.world.by_char_name('nova'), c.session)
        self.delete(c, 'Nova')
        self.assertIsNone(c.session['char_name'])
        self.assertIsNone(c.session['char'])
        self.assertFalse(c.session['in_world'])
        self.assertIsNone(self.server.world.by_char_name('nova'))
        self.assertIs(self.server.world.session(1), c.session)      # still logged in
        self.assertEqual(c.session.get('monsters') or {}, {})

    def test_delete_keeps_the_order_of_the_rest(self):
        c = self.client()
        self.login(c)
        for name in ('Alpha', 'Beta', 'Gamma'):
            self.create(c, name)
        self.delete(c, 'Beta')
        self.assertEqual([ch['name'] for ch in self.disk()['test']['characters']],
                         ['TestHero', 'Alpha', 'Gamma'])

    # -------------------------------------------------------- lc-enter-world ---
    def test_enter_world_stores_the_privacy_flags_and_p2p_endpoint(self):
        c = self.client()
        self.login(c)
        c.send_c2s('0x42F904/0x2B', {'refuse_whisper': 1, 'refuse_party': 1, 'p2p_ip': '10.5.0.2',
                                     'p2p_udp_port': 42907, 'char_name': 'TestHero'})
        for pkt, length in zip(c.expect(0x03, 0x07, 0x15, 0x28, 0x44), (64, 369, 35, 2, 2)):
            self.s2c(pkt, length)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world')))
        self.assertEqual(c.session['refuse'], {'whisper': True, 'exchange': False, 'party': True,
                                               'talk': False, 'friend': False})
        self.assertEqual(c.session['p2p'], ('10.5.0.2', 42907))
        self.assertEqual(c.session['level'], 1)
        self.assertIs(c.session['char'], self.server.store.find_character('test', 'TestHero'))

    def test_enter_world_loads_the_saved_map_and_position(self):
        with self.server.store.lock:
            self.server.store.find_character('test', 'TestHero').update({'map': 102, 'x': 700.0, 'y': 500.0})
        c = self.client()
        self.login(c)
        c.send_c2s('0x42F904/0x2B', {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907, 'char_name': 'TestHero'})
        pkts = c.expect(0x03, 0x07, 0x15, 0x28, 0x44, *F.mob_packets(8))   # map 102 has Pupus
        self.assertEqual(self.s2c(pkts[0], 64)['map_code'], 102)
        row = self.s2c(pkts[1], 369)['repeat[player_count]'][0]
        # the saved x, and y settled onto the floor below the saved point (livetest bug 5)
        self.assertEqual((row['pos_x'], row['pos_y']), (700.0, 635.0))
        self.assertEqual(c.session['current_map'], 102)

    def test_enter_world_with_another_accounts_character_is_refused(self):
        c = self.client()
        self.login(c, 'admin', 'admin')
        c.send_c2s('0x42F904/0x2B', {'p2p_ip': '', 'p2p_udp_port': 42907, 'char_name': 'TestHero'})
        rec = self.s2c(c.expect(0x02))
        self.assertEqual((rec['result'], rec['account_id'], rec['char_count']), (1, 2, 0))
        self.assertFalse(c.session['in_world'])
        self.assertIsNone(self.server.world.by_char_name('testhero'))

    # -------------------------------------------------------------- lc-stats ---
    def stat(self, c, index, value=None, uid=1):
        c.send_c2s(P.variants(0x04, 'C2S')[0]['key'], {'stat_index': index})
        rec = self.s2c(c.expect(0x14), 7)
        self.assertEqual((rec['uid'], rec['stat_index']), (uid, index))
        if value is not None:
            self.assertEqual(rec['value'], value, f'stat {index}')
        return rec

    def test_allocation_increments_persists_and_stops_at_the_free_points(self):
        c = self.client()
        self.login(c)
        self.enter_world(c)
        char = self.server.store.find_character('test', 'TestHero')
        self.assertEqual([char[k] for k in W.GameServer.STAT_KEYS], [3, 2, 1, 3])   # sum 9 = stat_total(1)
        # Level 1 has no free points left: the reply re-syncs the current value (B10 sent 4).
        self.stat(c, 0, 3)
        self.assertEqual(char['str'], 3)
        with self.server.store.lock:                       # level 2 -> 13 points, 4 free
            char['exp'] = W.progression.exp_for_level(2)
        self.stat(c, 0, 4)
        self.stat(c, 1, 3)
        self.stat(c, 2, 2)
        self.stat(c, 3, 4)
        self.assertEqual([char[k] for k in W.GameServer.STAT_KEYS], [4, 3, 2, 4])
        self.assertEqual(sum(char[k] for k in W.GameServer.STAT_KEYS), W.progression.stat_total(2))
        self.stat(c, 0, 4)                                 # spent: resync, no 5th point
        # Persisted: the values survive a portal and a relog (P1 exit criterion 4).
        c.send(0x7E, CAP_7E_101_TO_102)
        pkts = c.expect(0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(8))
        row = self.s2c(pkts[2], 369)['repeat[player_count]'][0]
        self.assertEqual([row[k] for k in ('stat_str', 'stat_dex', 'stat_int', 'stat_tol')], [4, 3, 2, 4])
        c.close()
        again = self.client()
        self.login(again)
        # The portal above persisted map 102 (world-persistence), so the relog spawns there.
        state, spawn = self.enter_world(again, monsters=8)[:2]
        self.assertEqual(self.s2c(state)['map_code'], 102)
        row = self.s2c(spawn)['repeat[player_count]'][0]
        self.assertEqual([row[k] for k in ('stat_str', 'stat_dex', 'stat_int', 'stat_tol')], [4, 3, 2, 4])

    def test_other_indexes_and_character_select_are_ignored(self):
        c = self.client()
        self.login(c)
        c.send_c2s(P.variants(0x04, 'C2S')[0]['key'], {'stat_index': 0})
        c.expect_silence(0.25)                             # not in world: nothing to allocate
        self.enter_world(c)
        with self.server.store.lock:
            self.server.store.find_character('test', 'TestHero')['exp'] = W.progression.exp_for_level(2)
        for index in (4, 7, 255):                          # 7 is the client's redraw trigger
            with self.subTest(index=index):
                c.send_c2s(P.variants(0x04, 'C2S')[0]['key'], {'stat_index': index})
                c.expect_silence(0.2)
        self.assertEqual(self.server.store.find_character('test', 'TestHero')['str'], 3)

    # ------------------------------------------------------- lc-login-errors ---
    def login_result(self, user='test', password='test', client=None):
        c = client or self.client()
        c.send_c2s('0x44D8BF/0x01', {'account_id': user, 'password': password})
        return self.s2c(c.expect(0x02))['result'], c

    def test_unknown_account_and_wrong_password_share_result_0x11(self):
        self.assertEqual(self.login_result('nobody', 'x')[0], 0x11)
        self.assertEqual(self.login_result('test', 'x')[0], 0x11)
        self.assertIsNone(self.server.store.account('nobody'))

    def test_banned_and_deleted_accounts(self):
        with self.server.store.lock:
            self.server.store.account('test')['banned'] = True
            self.server.store.account('admin')['deleted'] = True
        self.assertEqual(self.login_result('test', 'test')[0], 5)
        self.assertEqual(self.login_result('admin', 'admin')[0], 0x13)
        self.assertEqual(self.server.world.by_uid, {})

    def test_maintenance_refuses_every_login_and_flips_the_version_reply(self):
        self.reconfigure(MAINTENANCE=True)
        self.assertEqual(self.login_result('test', 'test')[0], 0x0E)
        vs = W.VersionServer(config=self.server.config)
        self.assertEqual(struct.unpack_from('<H', vs._build_version_body(), 1)[0],
                         W.cfgmod.VERSION_MAINTENANCE)

    def test_max_online_is_result_6_but_a_relog_of_an_online_account_is_not(self):
        self.reconfigure(MAX_ONLINE=1)
        first = self.client()
        self.assertEqual(self.login_result(client=first)[0], 1)
        self.assertEqual(self.login_result('admin', 'admin')[0], 6)
        # The same account logging in again replaces its session (result 4), never 6.
        self.assertEqual(self.login_result('test', 'test')[0], 4)

    def test_auto_register_creates_the_account_with_a_hashed_password(self):
        self.reconfigure(AUTO_REGISTER=True)
        result, c = self.login_result('newbie', 'secret')
        self.assertEqual(result, 1)
        self.assertEqual(c.session['username'], 'newbie')
        acc = self.disk()['newbie']
        self.assertEqual((acc['uid'], acc['characters']), (3, []))
        self.assertNotIn('secret', json.dumps(self.disk()))
        self.assertTrue(W.auth.verify(acc['password'], 'secret'))
        self.assertEqual(self.login_result('bad name!', 'x')[0], 0x11)   # not auto-registered

    def test_register_create_and_delete_write_outside_the_store_lock(self):
        """Review of livetest bug 7: the AUTO_REGISTER, create and delete saves ran - and
        backed off up to ~3 s while a reader held accounts.json - under store.lock, which
        every handler takes. The handlers validate and change the records under it and write
        after letting it go; a failed write still answers (the store keeps it dirty and
        retries)."""
        self.reconfigure(AUTO_REGISTER=True)
        store, real, seen = self.server.store, W.storemod.atomic_write, []

        def writer(path, data, delays=None):
            t = threading.Thread(target=lambda: (store.lock.acquire(), store.lock.release()), daemon=True)
            t.start()
            t.join(2.0)
            seen.append('blocked' if t.is_alive() else 'free')
            return real(path, data, delays)
        with mock.patch.object(W.storemod, 'atomic_write', side_effect=writer):
            result, c = self.login_result('newbie', 'secret')
            self.assertEqual(result, 1)
            self.create(c, 'Nova')
            self.delete(c, 'Nova', password='secret')
        self.assertEqual(seen, ['free'] * 3)
        self.assertEqual(self.disk()['newbie']['characters'], [])
        # a held file: the reply still goes out, the record is in memory and still to save
        with mock.patch.object(W.storemod, 'atomic_write', side_effect=PermissionError(13, 'held by a reader')):
            with self.assertLogs('WS', logging.ERROR):
                self.create(c, 'Held')
        self.assertIsNotNone(store.find_character('newbie', 'Held'))
        self.assertNotIn('Held', json.dumps(self.disk()))
        self.assertTrue(store.dirty)
        self.assertTrue(store.flush())
        self.assertEqual([ch['name'] for ch in self.disk()['newbie']['characters']], ['Held'])

    def test_passwords_are_hashed_at_rest_and_plaintext_records_still_log_in(self):
        with open(self.server.db_file, encoding='utf-8') as f:
            self.assertNotIn('"password": "test"', f.read())
        self.assertTrue(W.auth.is_hashed(self.disk()['test']['password']))
        self.assertEqual(self.login_result('test', 'test')[0], 1)
        # A hand-edited plaintext record keeps working and is hashed at the next load.
        server = F.make_server(self.tmp, accounts={'bob': {'password': 'plain', 'characters': []}})
        self.assertTrue(W.auth.is_hashed(server.store.account('bob')['password']))
        self.assertTrue(server.store.verify_password('bob', 'plain'))

    # -------------------------------------------------- lc-id-transfer-stub ---
    def test_legacy_id_transfer_is_refused_with_result_6(self):
        c = self.client()
        self.assertEqual(self.login(c)['transfer_status'], 3)     # hides the UI in the first place
        for key in ('0x4484CC/0x65', '0x4484CC/0x66'):
            with self.subTest(key=key):
                with self.assertLogs('WS', logging.WARNING) as cm:
                    c.send_c2s(key, {'target_id': 'old-yahoo-id'})
                    self.assertEqual(self.s2c(c.expect(0x8C), 1)['result'], 6)
                self.assertTrue(any('[ID-TRANSFER]' in line for line in cm.output))


class VersionReply(unittest.TestCase):
    """lc-version-config: the S2C 0x01 body is config, not constants (S2-30 / B16)."""

    def body(self, counts=None, **overrides):
        vs = W.VersionServer(config=W.cfgmod.from_dict(overrides),
                             user_counts=(lambda: counts) if counts is not None else None)
        return vs._build_version_body()

    def test_default_body_is_the_live_proven_one(self):
        body = self.body()
        notice = W.cfgmod.defaults()['NOTICE'].encode('latin-1')
        self.assertEqual(body[:3], bytes([0x01]) + struct.pack('<H', 3))
        self.assertEqual(body[3:5], struct.pack('<H', len(notice)))
        self.assertEqual(body[5:5 + len(notice)], notice)
        tail = body[5 + len(notice):]
        # server_count, status 3, slot count, entry count, then one channel entry.
        self.assertEqual(tail, bytes([1, 3, 1, 1, 1]) + struct.pack('<H', 0)
                         + struct.pack('<I', 0x7F000001))

    def test_channels_sharing_an_ip_are_warned_about(self):
        # The client's game port is hard-coded, so two channels on one IP are one server.
        with self.assertLogs('WS', logging.WARNING) as cm:
            W.cfgmod.from_dict({'CHANNELS': [{'no': 1}, {'no': 2}]})
        self.assertTrue(any('same game server' in line for line in cm.output))

    def test_public_ip_channels_and_live_user_counts(self):
        body = self.body(counts={1: 5, 2: 130}, PUBLIC_IP='192.168.1.50',
                         CHANNELS=[{'no': 1}, {'no': 2, 'ip': '10.0.0.7'}])
        tail = body[5 + len(W.cfgmod.defaults()['NOTICE'].encode('latin-1')):]
        self.assertEqual(tail[:4], bytes([1, 3, 2, 2]))
        self.assertEqual(tail[4:11], bytes([1]) + struct.pack('<H', 5)
                         + struct.pack('<I', W.cfgmod.ip_host_order('192.168.1.50')))
        self.assertEqual(tail[11:18], bytes([2]) + struct.pack('<H', 130)
                         + struct.pack('<I', W.cfgmod.ip_host_order('10.0.0.7')))

    def test_maintenance_and_notice_text(self):
        body = self.body(MAINTENANCE=True, NOTICE='Back at 20:00.')
        self.assertEqual(struct.unpack_from('<H', body, 1)[0], W.cfgmod.VERSION_MAINTENANCE)
        self.assertEqual(body[3:5], struct.pack('<H', 14))
        self.assertEqual(body[5:19], b'Back at 20:00.')

    def test_a_failing_count_provider_still_sends_a_reply(self):
        def boom():
            raise RuntimeError('no world yet')
        vs = W.VersionServer(config=W.cfgmod.defaults(), user_counts=boom)
        with self.assertLogs('WS', logging.ERROR):
            self.assertEqual(vs._build_version_body()[-6:], struct.pack('<H', 0)
                             + struct.pack('<I', 0x7F000001))

    def test_config_refuses_an_unusable_notice_or_limit(self):
        for overrides in ({'NOTICE': 'x' * 1001}, {'MAX_ONLINE': 0}, {'DELETE_MIN_AGE_HOURS': -1.0},
                          {'CHANNELS': [{'no': 2}, {'no': 1}]}):
            with self.subTest(**overrides), self.assertRaises(W.cfgmod.ConfigError):
                W.cfgmod.from_dict(overrides)


class AdminInjector(ServerTest):
    def setUp(self):
        super().setUp()
        self.world = self.client()                            # test / TestHero, in world
        self.login(self.world)
        self.enter_world(self.world)
        self.select = self.client()                           # admin, character select
        self.login(self.select, 'admin', 'admin')
        self.fresh = self.client()                            # connected, not logged in

    def cmd(self, **kw):
        return self.server._admin_command(json.dumps({'opcode': 0x59, 'payload_hex': '0103', **kw}))

    def got(self, c):
        # _admin_command sends before it returns, so the packets are already buffered
        pkts = c.recv_until_quiet(0.05)
        for p in pkts:
            self.assertEqual((p.opcode, p.payload), (0x59, b'\x01\x03'))
        return len(pkts)

    def counts(self):
        return self.got(self.world), self.got(self.select), self.got(self.fresh)

    def test_default_only_in_world_sessions(self):
        reply = self.cmd()
        self.assertTrue(reply.startswith('ok 0x59 2B sessions=1 skipped=2 not in_world'), reply)
        self.assertEqual(self.counts(), (1, 0, 0))

    def test_select_state_and_all(self):
        self.assertTrue(self.cmd(state='select').startswith('ok 0x59 2B sessions=1'))
        self.assertEqual(self.counts(), (0, 1, 0))
        self.assertTrue(self.cmd(state='all').startswith('ok 0x59 2B sessions=3'))
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_target_by_account_character_and_uid(self):
        self.assertTrue(self.cmd(target='TEST').startswith('ok 0x59 2B sessions=1'))
        self.assertEqual(self.counts(), (1, 0, 0))
        self.assertTrue(self.cmd(target='TestHero').startswith('ok 0x59 2B sessions=1'))
        self.assertEqual(self.counts(), (1, 0, 0))
        reply = self.cmd(target='admin')                      # at select: filtered by default
        self.assertIn('sessions=0 skipped=1 not in_world: admin/-@select', reply)
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertTrue(self.cmd(target='admin', state='select').startswith('ok 0x59 2B sessions=1'))
        self.assertEqual(self.counts(), (0, 1, 0))
        # lc-uid-online: uids are per account (test = 1, admin = 2), so a uid names one session
        self.assertTrue(self.cmd(target=1, state='all').startswith('ok 0x59 2B sessions=1'))
        self.assertEqual(self.counts(), (1, 0, 0))
        self.assertTrue(self.cmd(target=2, state='all').startswith('ok 0x59 2B sessions=1'))
        self.assertEqual(self.counts(), (0, 1, 0))

    def test_no_match_and_bad_state(self):
        self.assertIn('no connected session matches target', self.cmd(target='nobody'))
        with self.assertRaises(ValueError):
            self.cmd(state='everyone')
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_portal_clears_in_world_until_the_respawn(self):
        seen = []
        orig = self.server._send_encrypted

        def spy(sock, session, opcode, payload=b'', use_by_array=False, **kw):
            # **kw: the map load queues its 0x03 / 0x07 with flush=False (P14 receiver mirror)
            if session is self.world.session and opcode in (0x08, 0x07):
                seen.append((opcode, session['in_world']))
            return orig(sock, session, opcode, payload, use_by_array, **kw)
        self.server._send_encrypted = spy
        self.world.send(0x7E, CAP_7E_101_TO_102)
        self.world.recv_until_quiet()
        self.assertEqual(seen, [(0x08, False), (0x07, False)])   # flag set right after 0x07
        self.assertTrue(self.world.session['in_world'])


if __name__ == '__main__':
    unittest.main(verbosity=1)
