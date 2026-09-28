#!/usr/bin/env python3
"""
test_ground.py - ground items, both client builds (P4 stage 2: item_inventory-ground-loot-
pickup, -drop-bag-item, -drop-equipped; item_inventory.md 1.5, F6-F9)

Three layers:
- the wire records (ground.py): S2C 0x11 / 0x12 / 0x13 against the live bytes of
  item_inventory#06 (37 B), #09 (36 B) and #08 (8 B), the field order of both builds'
  handlers, the always-5-word option block, per-map u16 ids and the loot protection;
- the registry rules: round-robin ids that skip live ones and never reuse a freed id early,
  the per-map cap, take/restore;
- the server through fakeclient for BOTH builds (no port, no game client, temp accounts.json):
  a reported kill drops the loot on the ground (0x12 at the corpse, 0x18 gold only), the
  pickup key moves it into the bag (0x13, idempotent), items despawn on the tick scheduler,
  come back as 0x11 after a map load, bag and worn items can be dropped (0x23 / 0x24 + 0x12)
  and picked back up with their option words, a second session on the map sees it all
  (0x11 state 1 / 0x13 / 0x24), and GROUND_LOOT false keeps the P0 loot-into-the-bag path.
"""
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import config as cfgmod  # noqa: E402
import en_content as EC  # noqa: E402
import fakeclient as F  # noqa: E402
import ground as G  # noqa: E402
import ids  # noqa: E402
import inventory as INV  # noqa: E402
import packets as P  # noqa: E402

W = F.import_server()
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = '2008', '2009'
HAVE_2008 = os.path.exists(EC.hs_path('windslayer.hii'))
HAVE_2009 = os.path.exists(os.path.join(HERE, cfgmod.DEFAULTS['CLIENT_DIR_2009'], 'hs', 'windslayer.hii'))
needs_2008 = unittest.skipUnless(HAVE_2008, 'needs the EN 2008 client data (hs/)')
needs_2009 = unittest.skipUnless(HAVE_2009, 'needs the EN 2009 client data (CLIENT_DIR_2009)')

# EN content both builds share
BLUE_MUSHROOM, HERB, STICK, ELEDUST, DOUBLE_JUMP, CASH_HAT = 3, 5, 179, 281, 94, 1848
PUPU_MAP, TOWN = 102, 101

# C2S send-site keys per build (spec / spec_2009)
KEYS = {
    B8: {'login': '0x44D8BF/0x01', 'enter': '0x42F904/0x2B', 'move': '0x42CE94/0x0D',
         'pickup': '0x43D80F/0x1F', 'drop': '0x469AC9/0x13', 'drop_worn': '0x469BA8/0x14',
         'portal': '0x42F76B/0x7E', 'chat': '0x445CA7/0x03'},
    B9: {'login': '0x451CE5/0x01', 'enter': '0x4315D7/0x2B', 'move': '0x42E704/0x0D',
         'pickup': '0x43DA4E/0x1F', 'pet_pickup': '0x42EA76/0x1F', 'drop': '0x4737D9/0x13',
         'drop_worn': '0x4738B8/0x14', 'portal': '0x431284/0x7E', 'chat': '0x44790E/0x03'},
}
ACCOUNTS = {
    'test': {'password': 'test',
             'characters': [{'name': 'TestHero', 'level': 1, 'class': 0, 'map': PUPU_MAP,
                             'x': 1200, 'y': 714, 'hp': 100, 'mp': 50, 'gm': 1}]},
    'admin': {'password': 'admin',
              'characters': [{'name': 'Plain', 'level': 1, 'class': 0, 'map': PUPU_MAP,
                              'x': 1300, 'y': 714, 'hp': 100, 'mp': 50}]},
}
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
            'accounts.json changed during test_ground.py (tests must only use temp copies)'


def use_build(build):
    EC.configure(cfgmod.DEFAULTS['CLIENT_DIR_2009'] if build == B9 else cfgmod.DEFAULTS['CLIENT_DIR'], build)


def gitem(gid=100, item=HERB, qty=2, x=100, y=667, owner=0, source=0, words=None, map_code=TOWN):
    return G.GroundItem(ground_id=gid, map_code=map_code, item_id=item, qty=qty,
                        words=INV.pack_words(words), x=x, y=y, owner_uid=owner, source_uid=source)


# ================================================================ wire records ===
class WireRecords(unittest.TestCase):
    """ground.py builders against the live bytes and both builds' grammars."""

    def test_0x11_is_the_37_byte_live_packet_of_item_inventory_06(self):
        body = G.list_fields([gitem()], [0], state=G.STATE_RESTING)
        self.assertEqual(len(body), 1)
        for build in (B8, B9):
            raw = P.build('0x11', body[0], client_build=build)
            self.assertEqual(raw.hex(' '), '01 05 00 02 00 64 00 9b 02 64 00 00 00 00 00 00 00 00 00 00 00 '
                                           '00 00 05 00 00 00 00 00 00 00 00 00 00 00 00 00')
            self.assertEqual(len(raw), 37)

    def test_0x12_is_the_36_byte_live_packet_of_item_inventory_09(self):
        # Stone Debris 287 on map 101: x/y 0, ground id 101, owner 1, source 1
        body = G.drop_fields([gitem(gid=101, item=287, qty=1, x=0, y=0, owner=1, source=1)], [0])[0]
        for build in (B8, B9):
            raw = P.build('0x12', body, client_build=build)
            self.assertEqual(len(raw), 36)
            self.assertEqual(raw[:25].hex(' '), '01 1f 01 01 00 00 00 00 00 65 00 00 00 00 00 01 00 00 00 '
                                                '01 00 00 00 05 00')

    def test_the_field_order_is_the_one_both_handlers_read(self):
        # 2008 0x451516 / 2009 0x4561CE: id, qty, x, y, gid, u32 time, u32 owner, u32 source
        item = gitem(gid=0x0102, item=0x0304, qty=0x0506, x=0x0708, y=0x090A, owner=0x0B0B0B0B,
                     source=0x0C0C0C0C, words=[1, 2, 3, 4, 5, 6])
        for build in (B8, B9):
            raw = P.build('0x12', G.drop_fields([item], [0x0D0D0D0D])[0], client_build=build)
            self.assertEqual(raw.hex(' '), '01 04 03 06 05 08 07 0a 09 02 01 0d 0d 0d 0d 0b 0b 0b 0b '
                                           '0c 0c 0c 0c 05 01 00 02 00 03 00 04 00 05 00 06 00')

    def test_the_option_block_is_always_five_words(self):
        # the client zeroes its option buffer once per PACKET: a short list would inherit the
        # previous entry's words, so every entry carries n = 5 (item_inventory.md 1.3)
        a, b = gitem(gid=1, item=STICK, qty=1, words=[7, 8, 0, 0, 0, 9]), gitem(gid=2)
        rows = P.parse('0x11', P.build('0x11', G.list_fields([a, b], [5, 6])[0]), direction='S2C')
        self.assertEqual([r['opt_count'] for r in rows['repeat[entry_count]']], [5, 5])
        self.assertEqual([w['opt_word'] for w in rows['repeat[entry_count]'][0]['repeat[opt_count]']],
                         [7, 8, 0, 0, 0])
        self.assertEqual((rows['repeat[entry_count]'][0]['opt_tail'], rows['repeat[entry_count]'][1]['opt_tail']),
                         (9, 0))

    def test_0x13_pickup_and_despawn(self):
        for build in (B8, B9):
            self.assertEqual(P.build('0x13', G.picked_fields(gitem(), 1), client_build=build).hex(' '),
                             '64 00 02 00 01 00 00 00')              # live item_inventory#08
            self.assertEqual(P.build('0x13', G.despawn_fields(gitem(gid=102)), client_build=build).hex(' '),
                             '66 00 00 00 00 00 00 00')              # T-S13b: picker 0

    def test_c2s_requests_decode_in_both_builds(self):
        for build, pick, drop, worn in ((B8, '0x43D80F/0x1F', '0x469AC9/0x13', '0x469BA8/0x14'),
                                        (B9, '0x43DA4E/0x1F', '0x4737D9/0x13', '0x4738B8/0x14')):
            with self.subTest(build=build):
                self.assertEqual(P.parse(0x1F, b'\x64\x00', client_build=build), {'ground_item_uid': 100})
                self.assertIn(pick, P.parse(0x1F, b'\x64\x00', client_build=build).candidates)
                rec = P.parse(0x13, bytes.fromhex('05 00 01 00 00 00 00'), client_build=build)  # item_inventory#18
                self.assertEqual((rec.key, rec['item_id'], rec['amount'], rec['opt_count']), (drop, HERB, 1, 0))
                rec = P.parse(0x14, bytes.fromhex('b3 00 00 00 00'), client_build=build)        # item_inventory#19
                self.assertEqual((rec.key, rec['item_id'], rec['opt_count'], rec['opt6']), (worn, STICK, 0, 0))

    def test_batches_fit_one_frame(self):
        items = [gitem(gid=i) for i in range(1, 130)]
        bodies = G.list_fields(items, [1] * len(items))
        self.assertEqual([b['entry_count'] for b in bodies], [56, 56, 17])
        for body in bodies:
            self.assertLessEqual(len(P.build('0x11', body)), G.MAX_PAYLOAD)
        self.assertEqual([b['entry_count'] for b in G.drop_fields(items, [1] * len(items))], [58, 58, 13])

    def test_drop_time_is_the_receivers_clock_minus_the_age_never_zero(self):
        self.assertEqual(G.client_drop_time(5000, 1200), 3800)
        self.assertEqual(G.client_drop_time(1000, 5000), 1)       # 2009 pet loot needs rec+0x24 != 0
        self.assertEqual(G.client_drop_time(0), 1)
        self.assertEqual(G.client_drop_time(0x1_0000_0005), 5)


class Registry(unittest.TestCase):
    def setUp(self):
        self.t = [100.0]
        self.reg = G.GroundRegistry(max_per_map=3, clock=lambda: self.t[0])

    def add(self, map_code=PUPU_MAP, **kw):
        return self.reg.add(map_code, HERB, 1, None, 10, 20, **kw)

    def test_ids_are_u16_per_map_round_robin(self):
        a, _ = self.add()
        b, _ = self.add()
        c, _ = self.add(TOWN)
        self.assertEqual((a.ground_id, b.ground_id, c.ground_id), (1, 2, 1))     # per map
        self.assertTrue(self.reg.remove(a))
        d, _ = self.add()
        self.assertEqual(d.ground_id, 3)            # a freed id is not handed out again at once
        self.reg._next[PUPU_MAP] = ids.GROUND_ITEM.hi
        e, evicted = self.add()
        self.assertEqual((e.ground_id, evicted), (0xFFFF, []))
        f, evicted = self.add()                     # the cap (3) evicts the oldest: b
        self.assertEqual((f.ground_id, [i.ground_id for i in evicted]), (1, [2]))   # wrapped, never 0
        g, evicted = self.add()                     # evicts d; 2 is free again after the wrap
        self.assertEqual((g.ground_id, [i.ground_id for i in evicted]), (2, [3]))
        self.assertEqual([i.ground_id for i in self.reg.items(PUPU_MAP)], [0xFFFF, 1, 2])
        self.assertEqual(self.reg.count(), 4)

    def test_take_is_once_and_honours_the_15_s_protection(self):
        item, _ = self.add(owner_uid=1, source_uid=0xF0000)
        self.assertIsNone(self.reg.take(PUPU_MAP, item.ground_id, 2)[0])     # another player
        self.assertIn('owned by uid 1', self.reg.take(PUPU_MAP, item.ground_id, 2)[1])
        self.t[0] += G.OWNER_PROTECT_SECS
        taken, why = self.reg.take(PUPU_MAP, item.ground_id, 2)             # protection over
        self.assertIs(taken, item)
        self.assertIsNone(self.reg.take(PUPU_MAP, item.ground_id, 2)[0])     # idempotent
        self.reg.restore(item)
        self.assertIs(self.reg.take(PUPU_MAP, item.ground_id, 1)[0], item)   # the owner, any time
        free, _ = self.add()
        self.assertIsNone(G.ownership_refusal(free, 7))                      # owner 0 = anyone

    def test_remove_only_the_same_item(self):
        item, _ = self.add()
        self.reg.take(PUPU_MAP, item.ground_id, 0)
        other, _ = self.add()
        self.assertFalse(self.reg.remove(item))                             # already picked
        self.assertTrue(self.reg.remove(other))


# ================================================================ server flows ===
class Rig(unittest.TestCase):
    build = B8
    ground_loot = True

    def setUp(self):
        use_build(self.build)
        self.tmp = tempfile.mkdtemp(prefix=f'ws_ground{self.build}_')
        cfg = {'MOB_SERVER_CONTROLLED': True, 'CLIENT_BUILD': self.build}
        self.server = F.make_server(self.tmp, accounts=json.loads(json.dumps(ACCOUNTS)),
                                    config=cfgmod.from_dict(cfg), ground_loot=self.ground_loot)
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

    def enter(self, user='test', password='test', extra=()):
        """Login + enter world on map 102 (its 8 monsters in one 0x1A) and whatever ground
        items the map holds (`extra` = the opcodes expected after the 0x1A)."""
        c = F.FakeClient(self.server)
        self.clients.append(c)
        if self.build == B9:
            c.send_c2s(self.keys['login'], F.sso_login(user, password))
        else:
            c.send_c2s(self.keys['login'], {'account_id': user, 'password': password})
        self.assertEqual(c.s2c(c.expect(0x02))['result'], 1)
        name = self.char(user)['name']
        map_code = int(self.char(user).get('map') or 101)
        c.send_c2s(self.keys['enter'], {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907, 'char_name': name})
        ops = [0x03, 0x07, 0x15] + ([0x65] if self.build == B9 else []) + [0x28, 0x44]
        # world-presence: + 0x04 / 0x05 when another client is on that map (F.expect_entry)
        pkts = F.expect_entry(c, (*ops, *F.mob_packets(8), *extra), self.clients, map_code)
        for pkt in pkts:
            c.s2c(pkt, allow_trailing=pkt.opcode == 0x03)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world')))
        return c, pkts

    def relog(self, c, user='test', password='test', extra=()):
        c.close()
        c.thread.join(timeout=5.0)
        return self.enter(user, password, extra)

    def give(self, c, item, qty=1, words=None):
        self.assertIsNotNone(self.server._inv_add(c.session, item, qty, 'test', words))

    def bag(self, user='test'):
        return INV.Inventory(self.char(user))

    def disk_char(self, user='test'):
        self.server.store.flush()
        with open(self.server.db_file, encoding='utf-8') as f:
            return json.load(f)[user]['characters'][0]

    def report_hit(self, c, mob):
        """The 61 B C2S 0x0D hit report (interact event 7, the victim in the tail), identical
        in both builds (spec_2009 0x42E704/0x0D)."""
        payload = c.send_c2s(self.keys['move'], {
            'realtime_delta_ms': 0, 'map_code': PUPU_MAP, 'logic_elapsed_ms': 30,
            'state_lo': W.HIT_REPORT_EVENT << 16, 'state_hi': 0, 'target_uid': mob.uid,
            'pos_x': mob.x, 'pos_y': mob.y, 'target_dx': 0.0, 'target_dy': 0.0,
            'flag_8db': False, 'flag_8e7': False, 'timer_dac': 0, 'target_action_event': 7})
        self.assertEqual(len(payload), 61)

    def kill(self, c, index=0, drop=BLUE_MUSHROOM):
        """A reported one-hit kill of monster `index` whose drop roll yields `drop`."""
        mob = c.session['monsters'][W.MOB_UID_BASE + index]
        mob.hp = 1
        with mock.patch.object(W.random, 'random', return_value=0.0), \
                mock.patch.object(W.random, 'choice', return_value=drop):
            self.report_hit(c, mob)
            pkts = c.recv_until_quiet()
        self.assertFalse(mob.alive)
        return mob, pkts

    def pick(self, c, gid, key='pickup'):
        c.send_c2s(self.keys[key], {'ground_item_uid': gid})

    def drop(self, c, item, amount=1, opts=(), extra=0):
        c.send_c2s(self.keys['drop'], {'item_id': item, 'amount': amount, 'opt_count': len(opts),
                                       'repeat[opt_count]': [{'opt': w} for w in opts], 'opt6': extra})

    def drop_worn(self, c, item, opts=(), extra=0):
        c.send_c2s(self.keys['drop_worn'], {'item_id': item, 'opt_count': len(opts),
                                            'repeat[opt_count]': [{'opt': w} for w in opts], 'opt6': extra})

    def gm(self, c, line):
        c.send_c2s(self.keys['chat'], {'msg_len': len(line), 'message': line})

    @staticmethod
    def warning_text(c, pkt):
        text = c.s2c(pkt)['text']
        return text.decode('latin-1') if isinstance(text, bytes) else text

    def warning(self, c, pkt):
        self.assertEqual(c.s2c(pkt)['msg_type'], 2)
        return self.warning_text(c, pkt)

    def only_entry(self, c, pkt):
        rec = c.s2c(pkt)
        self.assertEqual(rec['entry_count'], 1)
        return rec['repeat[entry_count]'][0]


class GroundFlow:
    """P4 exit criteria 3 and 4, per build."""

    # ------------------------------------------------------ loot and pickup ---
    def test_a_kill_drops_the_loot_at_the_corpse_and_the_pickup_key_bags_it(self):
        c, _ = self.enter()
        uid = P.session_uid(c.session)
        gold0 = self.char()['gold']
        mob, pkts = self.kill(c)
        self.assertEqual([p.opcode for p in pkts], [0x29, 0x21, 0x12, 0x18])
        entry = self.only_entry(c, pkts[2])
        self.assertEqual((entry['item_id'], entry['quantity'], entry['ground_id'], entry['owner_uid'],
                          entry['source_uid'], entry['x'], entry['y'], entry['opt_count']),
                         (BLUE_MUSHROOM, 1, 1, uid, mob.uid, int(mob.x), int(mob.y), 5))
        clock = c.session['clock']
        self.assertTrue(clock <= entry['drop_time'] <= clock + 2000, (clock, entry['drop_time']))
        grant = c.s2c(pkts[3])                                # the 0x18 carries the gold only
        self.assertEqual((grant['item_id'], grant['count'], grant['gold']),
                         (0, 0, gold0 + mob.gold))
        self.assertEqual(self.bag().count(BLUE_MUSHROOM), 0)
        self.assertEqual(self.server.ground.count(PUPU_MAP), 1)

        with mock.patch.object(self.server, '_quest_credit_item') as credit:
            self.pick(c, 1)
            self.assertEqual(c.s2c(c.expect(0x13)), {'ground_id': 1, 'quantity': 1, 'picker_uid': uid})
        credit.assert_called_once()
        self.assertEqual(credit.call_args[0][2:4], (BLUE_MUSHROOM, 1))
        self.assertEqual(self.bag().count(BLUE_MUSHROOM), 1)
        self.assertEqual(self.server.ground.count(PUPU_MAP), 0)
        self.pick(c, 1)                                       # the key held: repeats change nothing
        self.pick(c, 1)
        c.expect_silence(0.2)
        self.assertEqual(self.bag().count(BLUE_MUSHROOM), 1)
        self.assertEqual(self.disk_char()['inventory']['consume'], {str(BLUE_MUSHROOM): 1})
        c, _ = self.relog(c)
        self.assertEqual(self.bag().count(BLUE_MUSHROOM), 1)

    def test_loot_the_pickup_key_can_never_request_stays_off_the_ground(self):
        c, _ = self.enter()
        _mob, pkts = self.kill(c, drop=DOUBLE_JUMP)           # a Type 3 skill book
        self.assertEqual([p.opcode for p in pkts], [0x29, 0x21, 0x18])
        self.assertEqual(c.s2c(pkts[2])['item_id'], 0)
        self.assertEqual(self.server.ground.count(), 0)

    def test_pickup_refusals_are_silent_and_leave_the_item(self):
        with self.server.store.lock:
            INV.Inventory(self.char()).set_capacity('consume', 1)
        c, _ = self.enter()
        self.give(c, HERB)                                    # the one consume slot is taken
        self.kill(c)
        self.pick(c, 1)                                       # no room for the mushroom
        self.pick(c, 999)                                     # nothing with that id
        c.expect_silence(0.2)
        self.assertEqual(self.server.ground.count(PUPU_MAP), 1)
        self.assertEqual(self.bag().count(BLUE_MUSHROOM), 0)

    def test_unpicked_items_despawn_on_the_tick_scheduler(self):
        c, _ = self.enter()
        self.gm(c, b'!loot 5 3')
        drop, reply = c.expect(0x12, 0x15)
        entry = self.only_entry(c, drop)
        self.assertEqual((entry['item_id'], entry['quantity'], entry['owner_uid'], entry['source_uid']),
                         (HERB, 3, 0, P.session_uid(c.session)))
        self.assertIn('ground id 1', self.warning_text(c, reply))
        item = self.server.ground.get(PUPU_MAP, 1)
        self.assertAlmostEqual(item.timer.when - item.dropped_at, W.cfgmod.DEFAULTS['GROUND_ITEM_SECS'], delta=0.5)
        now = time.monotonic()
        self.server.ticks.run_due(now + 5)
        c.expect_silence(0.1)
        self.server.ticks.run_due(now + W.cfgmod.DEFAULTS['GROUND_ITEM_SECS'] + 1)
        self.assertEqual(c.s2c(c.expect(0x13)), {'ground_id': 1, 'quantity': 0, 'picker_uid': 0})
        self.assertEqual(self.server.ground.count(), 0)
        self.pick(c, 1)                                       # gone: a late pickup does nothing
        c.expect_silence(0.2)
        self.assertEqual(self.bag().count(HERB), 0)

    def test_a_picked_item_never_despawns_later(self):
        c, _ = self.enter()
        self.gm(c, b'!loot 5')
        c.expect(0x12, 0x15)
        self.pick(c, 1)
        c.expect(0x13)
        self.server.ticks.run_due(time.monotonic() + W.cfgmod.DEFAULTS['GROUND_ITEM_SECS'] + 1)
        c.expect_silence(0.2)

    def test_the_map_cap_despawns_the_oldest(self):
        c, _ = self.enter()
        self.server.ground.max_per_map = 2
        for n in range(3):
            self.gm(c, f'!loot {HERB}'.encode())
            if n < 2:
                c.expect(0x12, 0x15)
        gone, new, _reply = c.expect(0x13, 0x12, 0x15)
        self.assertEqual(c.s2c(gone), {'ground_id': 1, 'quantity': 0, 'picker_uid': 0})
        self.assertEqual(self.only_entry(c, new)['ground_id'], 3)
        self.assertEqual([i.ground_id for i in self.server.ground.items(PUPU_MAP)], [2, 3])

    def test_a_map_load_re_sends_what_lies_there(self):
        c, _ = self.enter()
        self.kill(c)
        self.gm(c, b'!loot 281 7')
        c.expect(0x12, 0x15)
        back = [k for k, v in EC.portals().items() if k.startswith(f'{PUPU_MAP}_') and v[0] == TOWN]
        c.send_c2s(self.keys['portal'], {'portal_line_index': int(back[0].split('_')[1])})
        c.expect(0x08, 0x03, 0x07, 0x28, 0x44)                # the town: no monsters, no items
        self.assertTrue(c.wait_session(lambda s: s.get('current_map') == TOWN and s.get('in_world')))
        # age the loot past its 15 s protection: drop_time follows the item's age, in a client
        # clock that has run for 100 s (a fresh one would floor every drop_time at 1)
        for item in self.server.ground.items(PUPU_MAP):
            item.dropped_at -= 20.0
        c.session['clock'] = 100000
        c.send_c2s(self.keys['portal'], {'portal_line_index': 23})
        pkts = c.expect(0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(8), 0x11)
        clock = c.s2c(pkts[1], allow_trailing=True)['game_clock_ms']
        rows = c.s2c(pkts[-1])['repeat[entry_count]']
        self.assertEqual([(r['ground_id'], r['item_id'], r['quantity'], r['state']) for r in rows],
                         [(1, BLUE_MUSHROOM, 1, 0), (2, ELEDUST, 7, 0)])
        for r in rows:
            self.assertTrue(clock - 21000 <= r['drop_time'] <= clock - 19000, (clock, r['drop_time']))
        self.pick(c, 2)                                       # still pickable after the reload
        self.assertEqual(c.s2c(c.expect(0x13))['quantity'], 7)
        self.assertEqual(self.bag().count(ELEDUST), 7)

    # ------------------------------------------------------------- drops ---
    def test_drop_part_of_a_stack_and_pick_it_back_up(self):
        c, _ = self.enter()
        self.give(c, HERB, 5)
        uid = P.session_uid(c.session)
        self.drop(c, HERB, 2)
        remove, spawn = c.expect(0x23, 0x12)
        self.assertEqual(c.s2c(remove), {'item_id': HERB, 'count': 2, 'opt_count': 0,
                                         'repeat[opt_count]': [], 'opt_extra': 0})
        entry = self.only_entry(c, spawn)
        self.assertEqual((entry['item_id'], entry['quantity'], entry['owner_uid'], entry['source_uid']),
                         (HERB, 2, 0, uid))
        self.assertEqual(self.bag().count(HERB), 3)
        self.assertEqual(self.disk_char()['inventory']['consume'], {str(HERB): 3})
        self.pick(c, entry['ground_id'])
        self.assertEqual(c.s2c(c.expect(0x13))['quantity'], 2)
        self.assertEqual(self.bag().count(HERB), 5)

    def test_a_socketed_bag_item_drops_and_comes_back_with_its_block(self):
        c, _ = self.enter()
        block = [2945, 7, 0, 0, 0, 3]
        self.give(c, STICK, 1, block)
        self.give(c, STICK)                                   # a plain one stays behind
        self.drop(c, STICK, 1, opts=[2945, 7], extra=3)
        remove, spawn = c.expect(0x23, 0x12)
        rec = c.s2c(remove)                                    # the client's own 12 bytes, echoed
        self.assertEqual((rec['count'], [w['opt'] for w in rec['repeat[opt_count]']], rec['opt_extra']),
                         (1, [2945, 7], 3))
        entry = self.only_entry(c, spawn)
        self.assertEqual(([w['opt_word'] for w in entry['repeat[opt_count]']], entry['opt_tail']),
                         ([2945, 7, 0, 0, 0], 3))
        self.assertIsNone(self.bag().instance(STICK, block))
        self.assertIsNotNone(self.bag().instance(STICK, [0] * 6))
        self.pick(c, entry['ground_id'])
        c.expect(0x13)
        self.assertIsNotNone(self.bag().instance(STICK, block))
        self.assertEqual(self.bag().count(STICK), 2)

    def test_drop_refusals_change_nothing_and_say_why(self):
        c, _ = self.enter()
        self.give(c, HERB, 2)
        self.give(c, CASH_HAT)
        self.give(c, STICK, 1, [9, 0, 0, 0, 0, 0])
        cases = [
            (HERB, 3, (), 0, "You've exceeded the amount you have."),     # more than owned
            (HERB, 0, (), 0, "That item can't be dropped."),              # amount 0
            (ELEDUST, 100, (), 0, "That item can't be dropped."),         # etc max 99
            (CASH_HAT, 1, (), 0, "That item can't be dropped."),          # def+0x1F0 cash
            (DOUBLE_JUMP, 1, (), 0, "That item can't be dropped."),       # a skill book
            (STICK, 1, (), 0, "You've exceeded the amount you have."),    # wrong block
        ]
        for item, amount, opts, extra, text in cases:
            with self.subTest(item=item, amount=amount):
                self.drop(c, item, amount, opts, extra)
                self.assertEqual(self.warning(c, c.expect(0x15)), f'[Warning] {text}')
        self.assertEqual((self.bag().count(HERB), self.bag().count(CASH_HAT), self.bag().count(STICK)), (2, 1, 1))
        self.assertEqual(self.server.ground.count(), 0)

    def test_dropping_the_worn_weapon_empties_the_hand_and_it_can_be_picked_back(self):
        with self.server.store.lock:
            ch = self.char()
            ch['equipped'] = {INV.WEAPON_SLOT: {'id': STICK, 'w': [0] * 6}}
            INV.Inventory(ch).compose_all(0)
        self.assertEqual(self.char()['look'][INV.LAYER_WEAPON], EC.items().get(STICK).spr_num)
        c, _ = self.enter()
        uid = P.session_uid(c.session)
        self.drop_worn(c, STICK)
        remove, spawn = c.expect(0x24, 0x12)
        self.assertEqual(c.s2c(remove), {'uid': uid, 'item_id': STICK, 'option_count': 0,
                                         'repeat[option_count]': [], 'option_extra': 0})
        entry = self.only_entry(c, spawn)
        self.assertEqual((entry['item_id'], entry['quantity'], entry['source_uid']), (STICK, 1, uid))
        # the stored look is recomposed with Spr 0 as the 0x24 handler does: the empty hand
        self.assertEqual((self.char()['equipped'], self.char()['look'][INV.LAYER_WEAPON]), ({}, 0))
        self.assertEqual(self.bag().count(STICK), 0)
        disk = self.disk_char()
        self.assertEqual((disk['equipped'], disk['look'][INV.LAYER_WEAPON]), ({}, 0))
        self.drop_worn(c, STICK)                             # no longer worn
        self.assertIn('dropped', self.warning(c, c.expect(0x15)))
        self.pick(c, entry['ground_id'])
        c.expect(0x13)
        self.assertEqual(self.bag().count(STICK), 1)         # back in the bag, not in the hand
        c, pkts = self.relog(c)
        me = c.s2c(pkts[1])['repeat[player_count]'][0]
        grid = [v for k, rows in me.items() if k.startswith('repeat[') and isinstance(rows, list)
                for row in rows if isinstance(row, dict) for f, v in row.items() if f == 'equip_item_id']
        self.assertEqual(len(grid), 16 if self.build == B8 else 15)      # 2009: repeat(15)
        self.assertNotIn(STICK, grid)
        look = [v for k, rows in me.items() if k.startswith('repeat[') and isinstance(rows, list)
                for row in rows if isinstance(row, dict) for f, v in row.items() if f == 'appearance_part']
        self.assertEqual(look[INV.LAYER_WEAPON], 0)                       # the relog 0x07 too

    def test_drop_requests_outside_the_world_are_ignored(self):
        c, _ = self.enter()
        self.give(c, HERB, 2)
        c.session['in_world'] = False
        self.drop(c, HERB, 1)
        self.drop_worn(c, STICK)
        self.pick(c, 1)
        c.expect_silence(0.2)
        self.assertEqual(self.bag().count(HERB), 2)

    # ------------------------------------------------------- two sessions ---
    def test_a_second_player_on_the_map_sees_drops_pickups_and_despawns(self):
        a, _ = self.enter()
        b, _ = self.enter('admin', 'admin')
        ua, ub = P.session_uid(a.session), P.session_uid(b.session)
        mob, _pkts = self.kill(a)
        # world-move-relay (P5 stage 2): b sees a's hit report as a 0x1B node; the monster is
        # shared (P5 stage 3), so b sees it die (0x29) and the loot fall at b's own copy of the
        # corpse (0x12, source = the corpse), with a's loot rights
        relay, death, drop = b.expect(0x1B, 0x29, 0x12)
        self.assertEqual(b.s2c(relay)['uid'], ua)
        self.assertEqual(b.s2c(death)['uid'], mob.uid)
        seen = self.only_entry(b, drop)
        self.assertEqual((seen['ground_id'], seen['item_id'], seen['owner_uid'],
                          seen['source_uid'], seen['x'], seen['y']),
                         (1, BLUE_MUSHROOM, ua, mob.uid, int(mob.x), int(mob.y)))
        self.pick(b, 1)                                       # 15 s protection: not b's yet
        b.expect_silence(0.2)
        self.server.ground.get(PUPU_MAP, 1).dropped_at -= G.OWNER_PROTECT_SECS
        self.pick(b, 1)
        self.assertEqual(b.s2c(b.expect(0x13)), {'ground_id': 1, 'quantity': 1, 'picker_uid': ub})
        self.assertEqual(a.s2c(a.expect(0x13)), {'ground_id': 1, 'quantity': 1, 'picker_uid': ub})
        self.assertEqual((self.bag().count(BLUE_MUSHROOM), self.bag('admin').count(BLUE_MUSHROOM)), (0, 1))
        # a drop: 0x12 for the dropper, and the same 0x12 (source = a) for b, whose client
        # holds a (item_inventory-observer-broadcast, P5 stage 4: it falls at b's own copy of
        # a); the despawn reaches both
        self.give(a, HERB, 1)
        self.drop(a, HERB, 1)
        a.expect(0x23, 0x12)
        seen = self.only_entry(b, b.expect(0x12))
        self.assertEqual((seen['item_id'], seen['source_uid'], seen['owner_uid']), (HERB, ua, 0))
        self.server.ticks.run_due(time.monotonic() + W.cfgmod.DEFAULTS['GROUND_ITEM_SECS'] + 1)
        for c in (a, b):
            pkt = [p for p in c.recv_until_quiet() if p.opcode == 0x13]
            self.assertEqual([c.s2c(p)['picker_uid'] for p in pkt], [0])
        # a worn item leaving a's grid: 0x24 for every client that holds a, then the 0x12
        with self.server.store.lock:
            self.char()['equipped'] = {INV.WEAPON_SLOT: {'id': STICK, 'w': [0] * 6}}
        self.drop_worn(a, STICK)
        a.expect(0x24, 0x12)
        removal, shown = b.expect(0x24, 0x12)
        self.assertEqual(b.s2c(removal)['uid'], ua)
        self.assertEqual(self.only_entry(b, shown)['source_uid'], ua)

    # ------------------------------------------------------------ routes ---
    def test_routes(self):
        routes = self.server.routes
        for op, handler in ((0x1F, '_handle_ground_pickup'), (0x13, '_handle_drop_bag_item'),
                            (0x14, '_handle_drop_worn_item')):
            self.assertEqual(routes[op].handler, handler)


@needs_2008
class Ground2008(GroundFlow, Rig):
    build = B8


@needs_2009
class Ground2009(GroundFlow, Rig):
    build = B9

    def test_the_pet_auto_loot_site_picks_up_too(self):
        # spec_2009 0x42EA76/0x1F: the same u16 as the key, sent every tick near a pet
        c, _ = self.enter()
        self.gm(c, b'!loot 5')
        c.expect(0x12, 0x15)
        self.pick(c, 1, key='pet_pickup')
        self.assertEqual(c.s2c(c.expect(0x13))['quantity'], 1)
        self.pick(c, 1, key='pet_pickup')
        c.expect_silence(0.2)


class BagLootFallback:
    """GROUND_LOOT false: the P0 path (loot into the bag with the 0x18) until the ground flow
    is live-verified."""
    ground_loot = False

    def test_loot_goes_straight_into_the_bag(self):
        c, _ = self.enter()
        _mob, pkts = self.kill(c)
        self.assertEqual([p.opcode for p in pkts], [0x29, 0x21, 0x18])
        grant = c.s2c(pkts[2])
        self.assertEqual((grant['item_id'], grant['count']), (BLUE_MUSHROOM, 1))
        self.assertEqual(self.bag().count(BLUE_MUSHROOM), 1)
        self.assertEqual(self.server.ground.count(), 0)
        self.give(c, HERB, 2)                                 # dropping still uses the ground
        self.drop(c, HERB, 1)
        c.expect(0x23, 0x12)


@needs_2008
class BagLoot2008(BagLootFallback, Rig):
    build = B8
    ground_loot = False


@needs_2009
class BagLoot2009(BagLootFallback, Rig):
    build = B9
    ground_loot = False


class Config(unittest.TestCase):
    def test_defaults_and_validation(self):
        d = cfgmod.defaults()
        self.assertEqual((d.GROUND_LOOT, d.GROUND_ITEM_SECS, d.GROUND_ITEMS_PER_MAP), (True, 60.0, 100))
        for bad in ({'GROUND_ITEM_SECS': 0}, {'GROUND_ITEMS_PER_MAP': 0}, {'GROUND_ITEMS_PER_MAP': 5000},
                    {'GROUND_LOOT': 1}):
            with self.subTest(bad=bad), self.assertRaises(cfgmod.ConfigError):
                cfgmod.from_dict(bad)

    def test_the_rig_pins_bag_loot_unless_asked(self):
        tmp = tempfile.mkdtemp(prefix='ws_ground_cfg_')
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertFalse(F.make_server(tmp).config.GROUND_LOOT)
        self.assertTrue(F.make_server(tmp, ground_loot=True).config.GROUND_LOOT)
        server = F.make_server(tmp, config=cfgmod.from_dict({'GROUND_ITEMS_PER_MAP': 7}), ground_loot=None)
        self.assertTrue(server.config.GROUND_LOOT)
        self.assertEqual(server.ground.max_per_map, 7)

    def test_client_clock_extrapolates_from_the_last_agreed_value(self):
        server = F.make_server(tempfile.mkdtemp(prefix='ws_ground_clk_'))
        session = {}
        self.assertEqual(server._client_clock(session), server.START_CLOCK_MS)
        server._advance_clock(session, 30)
        t = session['clock_t']
        self.assertEqual(server._client_clock(session, t + 2.5), 1030 + 2500)
        self.assertEqual(server._client_clock(session, t + 10_000),
                         1030 + server.CLIENT_CLOCK_EXTRAPOLATE_MS)
        self.assertEqual(server._game_clock(session), 1030)             # what the next 0x03 sends


if __name__ == '__main__':
    unittest.main()
