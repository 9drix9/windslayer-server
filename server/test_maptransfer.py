#!/usr/bin/env python3
"""
test_maptransfer.py - the real 0x03/0x07 state, the F6 MapTransfer primitive and the
portal guards (world-03-real-state, item_inventory-seed-03-07,
quest_cards_misc-03-quest-card-fields, world-maptransfer, world-portal-guards)

Every test drives the real handler loop through fakeclient (no port, no game client, a
temp accounts.json). The P2 exit criteria it covers offline:

    criterion 1  a portal round trip keeps bag, gold, worn weapon and quest log - here
                 checked ON THE WIRE (the 0x03 the client is actually told), not only in
                 the store as test_persistence does
    criterion 8  an unmapped portal and a repeated portal key produce no warp, no hang
                 and no double transfer

Byte offsets in S2C 0x03: mentor_id is always 0, so the fixed head is
channel 1 + map 2 + clock 4 + gold 8 + exp 4 + fame 4 + winnie 4 + battle 16 + mentor 4
= 47 bytes, and the quest block starts at unk_e78 (47), active ids (48), progress (54).
The active ids are read here instead of through packets.parse because the grammar has two
`repeat(3)` blocks that share the key 'repeat[3]' (see quests.fields_for_03).
"""
import hashlib
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
import inventory as INV  # noqa: E402
import packets as P  # noqa: E402
import presence as PR  # noqa: E402
import quests as Q  # noqa: E402
import world as worldmod  # noqa: E402
from wsproto import hexbytes  # noqa: E402

W = F.import_server()
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
PORTAL_101_23 = 23                               # the live capture: map 101 portal line 23
PORTAL_102_31 = 31                               # the EN return portal on 102 (live #04; KR 26)
CAP_0F_EQUIP_STICK = hexbytes('B3 00 00 00 00')  # item 179, no stones, tail 0
STICK, HERB, ETC_ITEM = 179, 5, 2975
QUEST = 26
_LIVE_HASH = None

# S2C 0x03 offsets of the two same-key repeat(3) blocks (see the module docstring).
OFF_ACTIVE, OFF_PROGRESS = 48, 54


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def setUpModule():
    global _LIVE_HASH
    _LIVE_HASH = _sha(LIVE_ACCOUNTS) if os.path.exists(LIVE_ACCOUNTS) else None


def tearDownModule():
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_maptransfer.py (tests must only use temp copies)'


class TransferTest(unittest.TestCase):
    """A fresh GameServer on a temp accounts.json. The portal debounce is off unless the
    test is about the debounce (fakeclient.make_server)."""
    portal_cooldown = 0.0

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_transfer_')
        self.server = F.make_server(self.tmp, portal_cooldown=self.portal_cooldown)
        self.clients = []
        # The portal table is a module cache shared by every test; anything a test adds to
        # it is dropped again here.
        self.addCleanup(EC.reload)

    def tearDown(self):
        for c in self.clients:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- flows ---
    def client(self):
        c = F.FakeClient(self.server)
        self.clients.append(c)
        return c

    def enter(self, monsters=0, name='TestHero', user='test', password='test'):
        c = self.client()
        c.send_c2s('0x44D8BF/0x01', {'account_id': user, 'password': password})
        self.assertEqual(F.FakeClient.decode(c.expect(0x02))['result'], 1)
        c.send_c2s('0x42F904/0x2B', {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907, 'char_name': name})
        pkts = c.expect(0x03, 0x07, 0x15, 0x28, 0x44, *F.mob_packets(monsters))
        self.assertTrue(c.wait_session(lambda s: s.get('in_world')))
        return c, pkts

    def portal(self, c, index=PORTAL_101_23, monsters=8):
        c.send_c2s('0x42F76B/0x7E', {'portal_line_index': index})
        return c.expect(0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(monsters))

    def char(self, name='TestHero', user='test'):
        return self.server.store.find_character(user, name)

    # --------------------------------------------------------------- decode ---
    @staticmethod
    def state(pkt):
        """The 0x03 record. allow_trailing because the two repeat(3) blocks share a key and
        the decoder therefore reports the progress rows for both (see the docstring)."""
        return F.FakeClient.decode(pkt, allow_trailing=True)

    @staticmethod
    def active_ids(pkt):
        return list(struct.unpack_from('<3H', pkt.payload, OFF_ACTIVE))

    @staticmethod
    def progress(pkt):
        return list(struct.unpack_from('<3B', pkt.payload, OFF_PROGRESS))

    @staticmethod
    def grid(pkt):
        """The 16 equip slots of an S2C 0x07 own record."""
        return F.FakeClient.decode(pkt)['repeat[player_count]'][0]['repeat[16]']


class Opcode03RealState(TransferTest):
    """world-03-real-state + item_inventory-seed-03-07 + quest_cards_misc-03-quest-card-fields."""

    def test_an_empty_character_still_sends_the_64_byte_live_form(self):
        """The live 101 -> 102 capture is 64 B, which is the minimum of the grammar. A
        character that owns nothing must still produce exactly that."""
        _, pkts = self.enter()
        state = pkts[0]
        self.assertEqual(len(state.payload), 64)
        rec = self.state(state)
        self.assertEqual(rec['channel_id'], 1)                  # PySlayer: "must be >= 1"
        self.assertEqual(rec['map_code'], 101)
        self.assertEqual(rec['mentor_id'], 0)
        self.assertEqual([rec['equip_item_count'], rec['consume_item_count'],
                          rec['etc_item_count'], rec['completed_quest_count']], [0, 0, 0, 0])

    def test_wallet_exp_and_capacities_come_from_the_record_not_from_constants(self):
        """S1-05: gold 100000, exp_total 30000 and clock 1000 were hardcoded, so every map
        load overwrote the wallet and put a fresh character at level 12 progress."""
        char = self.char()
        char.update({'gold': 4242, 'victy': 7, 'exp': 123, 'fame': 9})
        INV.Inventory(char).set_capacity('consume', 20)
        self.server.store.mark_dirty('test')
        _, pkts = self.enter()
        rec = self.state(pkts[0])
        self.assertEqual(rec['gold'], 4242)
        self.assertNotEqual(rec['gold'], 100000)
        self.assertEqual(rec['winnie_points'], 7)
        self.assertEqual(rec['exp_total'], 123)
        self.assertNotEqual(rec['exp_total'], 30000)
        self.assertEqual(rec['fame_points'], 9)
        self.assertEqual((rec['equip_tab_slots'], rec['consume_tab_slots'], rec['etc_tab_slots']),
                         (35, 20, 35))

    def test_the_three_bag_lists_are_the_bag(self):
        """item_inventory B5: the client memsets all four tabs before reading these lists,
        so four zero counts WERE the bag wipe every portal did."""
        c, _ = self.enter()
        self.server._inv_add(c.session, HERB, 1200, 'test')      # > 999: two consume slots
        self.server._inv_add(c.session, ETC_ITEM, 3, 'test')
        self.server._inv_add(c.session, STICK, 1, 'test', words=[11, 22, 0, 0, 0, 33])
        rec = self.state(self.portal(c)[1])
        self.assertEqual([(r['item_id'], r['quantity']) for r in rec['repeat[consume_item_count]']],
                         [(HERB, 999), (HERB, 201)])
        self.assertEqual([(r['item_id'], r['quantity']) for r in rec['repeat[etc_item_count]']],
                         [(ETC_ITEM, 3)])
        equip = rec['repeat[equip_item_count]']
        self.assertEqual(len(equip), 1)
        # S1-16: the option words are the stored ones. A zero block here is what let a
        # reinforced item be duplicated by 0x1E.
        self.assertEqual(equip[0]['item_id'], STICK)
        self.assertEqual([w['option_value'] for w in equip[0]['repeat[option_count]']], [11, 22])
        self.assertEqual(equip[0]['item_extra'], 33)

    def test_a_worn_item_is_in_the_0x07_grid_and_not_in_the_0x03_bag(self):
        c, _ = self.enter()
        self.server._inv_add(c.session, STICK, 1, 'test')
        c.send(0x0F, CAP_0F_EQUIP_STICK)
        c.expect(0x1D)
        state, spawn = self.portal(c)[1:3]
        self.assertEqual(self.state(state)['equip_item_count'], 0)
        self.assertEqual(self.grid(spawn)[5]['equip_item_id'], STICK)

    def test_the_equipment_grid_carries_its_real_option_words(self):
        """item_inventory-seed-03-07 / S1-16: the grid always sent zero option words, so a
        reinforced item stored a zero block on the next portal and 0x1E could not clear it."""
        c, _ = self.enter()
        self.server._inv_add(c.session, STICK, 1, 'test', words=[7, 0, 0, 0, 0, 5])
        c.send(0x0F, hexbytes('B3 00 01 07 00 05 00'))          # 1 stone (7), tail 5
        c.expect(0x1D)
        spawn = self.portal(c)[2]
        slot = self.grid(spawn)[5]
        self.assertEqual(slot['equip_item_id'], STICK)
        self.assertEqual([w['equip_item_attr'] for w in slot['repeat[6]']], [7, 0, 0, 0, 0, 5])

    def test_buffs_and_skills_are_seeded_from_the_persisted_record(self):
        """Changed on purpose in P3 stage 3 (cs-buffs): the stored buff now RUNS again from
        its remaining time (a few ms have passed by the time the 0x07 is built), and a
        persisted trap is dropped - a Booby Trap stays on the map it was placed on (F2 step
        2). The ground-point encoding itself is test_records.test_buffs_and_skills."""
        char = self.char()
        char['skills'] = [194]
        char['buffs'] = [{'id': 292, 'remaining_ms': 5000},
                         {'id': 0x0A31, 'remaining_ms': 5000, 'x': 12, 'y': 34}]
        self.server.store.mark_dirty('test')
        _, pkts = self.enter()
        row = F.FakeClient.decode(pkts[1])['repeat[player_count]'][0]
        self.assertEqual([s['skill_id'] for s in row['repeat[skill_count]']], [194])
        self.assertEqual(len(row['repeat[buff_count]']), 1)
        buff = row['repeat[buff_count]'][0]
        self.assertEqual(buff['buff_skill_id'], 292)
        self.assertTrue(4500 <= buff['buff_duration'] <= 5000, buff['buff_duration'])

    def test_quest_slots_completed_list_and_card_count(self):
        c, _ = self.enter()
        c.send_c2s('0x47734D/0x16', {'quest_id': QUEST})        # accept: slot 1
        c.expect(0x26, 0x59)
        state = self.server._quest_state(c.session)
        state.set_progress(0, 1)                                # as a Pupu kill would
        state.complete_append(7)                                # as a turn-in would
        state.add_card(2030)
        self.server.store.mark_dirty('test')
        state_pkt = self.portal(c)[1]
        self.assertEqual(self.active_ids(state_pkt), [QUEST, 0, 0])
        self.assertEqual(self.progress(state_pkt), [1, 0, 0])
        rec = self.state(state_pkt)
        self.assertEqual(rec['unk_e78'], 1)                     # the card deck count
        self.assertEqual([(r['completed_quest_id'], r['completed_quest_times'])
                          for r in rec['repeat[completed_quest_count]']], [(7, 1)])

    def test_the_clock_follows_the_client_instead_of_resetting_to_1000(self):
        """Both 0x03 game_clock_ms and 0x08 game_time_ms write scene+0xF1C, and the client
        reports its own logic time in every C2S 0x0D (item_inventory.md 1.6)."""
        c, pkts = self.enter()
        self.assertEqual(self.state(pkts[0])['game_clock_ms'], W.GameServer.START_CLOCK_MS)
        for _ in range(4):
            c.send_c2s('0x42CE94/0x0D', {'map_code': 101, 'logic_elapsed_ms': 30,
                                         'state_lo': 2, 'state_hi': 0})
        self.assertTrue(c.wait_session(lambda s: s.get('clock') == 1000 + 4 * 30))
        lead, state = self.portal(c)[:2]
        self.assertEqual(F.FakeClient.decode(lead)['game_time_ms'], 1120)
        self.assertEqual(self.state(state)['game_clock_ms'], 1120)   # the two cannot disagree


class MapTransferPrimitive(TransferTest):
    """world-maptransfer (+ premium_cash-map-replay-helper, pvp-warp-refactor)."""

    def test_the_portal_sequence_is_the_live_verified_one(self):
        """C1: a bare lead leaves the client on the loading overlay for ever; 0x03 then 0x07
        are mandatory, and the HP/MP that follow are the CURRENT ones (F6 step 5)."""
        c, _ = self.enter()
        with self.server._combat_lock(c.session):
            c.session['hp'], c.session['mp'] = 42, 8
        with self.assertLogs('WS', logging.INFO) as cm:
            lead, state, spawn, hp, mp = self.portal(c)[:5]
        # the [PORTAL] line names the point he lands on, as the [MAP] line does (review of
        # livetest bug 5: it printed the raw table point, 100 px above it)
        line = next(m for m in cm.output if '[PORTAL]' in m)
        self.assertIn('-> map 102', line)
        self.assertTrue(line.endswith('at (50, 812)'), line)
        self.assertEqual(len(lead.payload), 6)                   # S3-04: no uid, no padding
        self.assertEqual(F.FakeClient.decode(lead), {'map_code': 102, 'game_time_ms': 1000})
        self.assertEqual(self.state(state)['map_code'], 102)
        row = F.FakeClient.decode(spawn)['repeat[player_count]'][0]
        # livetest bug 5: the portal arrival (50, 712) is 100 px above 102's floor; the 0x07
        # puts him ON the floor (the 2009 client never drops an idle local player)
        ax, ay = EC.portal(101, PORTAL_101_23)[1:]
        self.assertEqual((ax, ay), (50.0, 712.0))
        self.assertEqual((row['pos_x'], row['pos_y']), (50.0, 812.0))
        self.assertEqual(c.session['pos'], (50.0, 812.0))
        self.assertEqual((row['cur_hp'], row['cur_mp']), (42, 8))
        self.assertEqual((F.FakeClient.decode(hp)['hp'], F.FakeClient.decode(mp)['mp']), (42, 8))

    def test_every_arrival_is_settled_onto_the_floor(self):
        """livetest bug 5: portal arrivals, the 101 start point and the revive points sit 100 px
        above their floor and the 2009 client does not drop an idle local player, so he floated
        there while the other clients drew him on the floor (presence.floor_point). Every map
        load now puts the owner's 0x07, session['pos'] and the saved record on the floor point."""
        with self.server.store.lock:
            ch = self.char()
            ch['map'], ch['x'], ch['y'] = 101, 700.0, 812.0          # START_X / START_Y
        c, pkts = self.enter()                                        # enter world
        row = F.FakeClient.decode(pkts[1])['repeat[player_count]'][0]
        self.assertEqual((row['pos_x'], row['pos_y']), (700.0, 912.0))
        self.assertEqual(c.session['pos'], (700.0, 912.0))
        self.assertEqual((self.char()['x'], self.char()['y']), (700.0, 912.0))
        for dest, x, y, floor in ((102, 48.0, 713.0, 812.0),           # a pvp return (48, 713)
                                  (101, 1411.0, 714.0, 814.0),         # 102 -> 101 portal arrival
                                  (102, 1000.0, 714.0, 714.0)):        # already on a floor: kept
            with self.subTest(dest=dest, y=y):
                self.server._map_transfer(c.session['sock'], c.session, dest, x, y, reason='test')
                pkts = c.expect(0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(8 if dest == 102 else 0))
                row = F.FakeClient.decode(pkts[2])['repeat[player_count]'][0]
                self.assertEqual((row['pos_x'], row['pos_y']), (x, floor))
                self.assertEqual(c.session['pos'], (x, floor))
                self.assertEqual((self.char()['map'], self.char()['x'], self.char()['y']), (dest, x, floor))
        # the point the transfer uses is the observers' floor point too, and settling is idempotent
        self.assertEqual(W.GameServer._arrival_point(101, 700, 812), PR.settle(101, 700, 812, PR.SLOPE_SLACK_PX))
        self.assertEqual(W.GameServer._arrival_point(101, 700, 912), (700.0, 912.0))

    def test_enter_world_sends_no_lead_at_all(self):
        """An 0x08 during enter world put the client in a re-map-load loop (2026-06-09)."""
        _, pkts = self.enter()
        self.assertEqual([p.opcode for p in pkts], [0x03, 0x07, 0x15, 0x28, 0x44])

    def test_every_pvp_lead_carries_the_same_six_byte_body(self):
        """pvp-warp-refactor: room exits and forced returns reuse the primitive with their
        own popup opcode."""
        c, _ = self.enter()
        for lead, dest, mobs in (('0x5E', 102, 8), ('0xA3', 101, 0), ('0xA4', 102, 8)):
            with self.subTest(lead=lead):
                self.server._map_transfer(c.session['sock'], c.session, dest, 48.0, 713.0,
                                          lead=lead, reason='pvp return')
                pkts = c.expect(P.opcode(lead), 0x03, 0x07, 0x28, 0x44, *F.mob_packets(mobs))
                self.assertEqual(F.FakeClient.decode(pkts[0]),
                                 {'map_code': dest, 'game_time_ms': 1000})
                self.assertEqual(c.session['current_map'], dest)

    def test_an_unknown_lead_opcode_is_a_programming_error(self):
        c, _ = self.enter()
        with self.assertRaises(ValueError):
            self.server._map_transfer(c.session['sock'], c.session, 102, 1.0, 2.0, lead='0x99')
        self.assertEqual(c.session['current_map'], 101)

    def test_nothing_is_committed_when_a_packet_cannot_be_built(self):
        """F6 opening line: resolve and build first, then commit. A half-done transfer would
        leave the session on a map the client never loaded."""
        c, _ = self.enter()

        def explode(*a, **kw):
            raise P.PacketError('a field this record cannot encode')
        self.server._build_opcode_03, original = explode, self.server._build_opcode_03
        try:
            with self.assertRaises(P.PacketError):
                self.server._map_transfer(c.session['sock'], c.session, 102, 48.0, 713.0)
        finally:
            self.server._build_opcode_03 = original
        c.expect_silence()
        self.assertEqual(c.session['current_map'], 101)
        self.assertTrue(c.session['in_world'])
        self.assertEqual(self.char()['map'], 101)

    def test_a_session_without_a_character_transfers_nothing(self):
        c = self.client()
        c.send_c2s('0x44D8BF/0x01', {'account_id': 'admin', 'password': 'admin'})
        c.expect(0x02)
        self.assertFalse(self.server._map_transfer(c.session['sock'], c.session, 102, 1.0, 2.0))
        c.expect_silence()

    def test_the_lifecycle_hooks_fire_around_the_sequence(self):
        """F5/F6 step 1: before_server_map_load runs BEFORE anything is committed or sent
        (its callbacks still see the old map, and a stall close must precede the lead);
        on_enter_world runs after the own 0x07."""
        seen = []

        def before(server, session, map_code=None, reason=None, **_):
            seen.append(('before', map_code, reason, session['current_map'], session['in_world']))

        def after(server, session, map_code=None, reason=None, **_):
            seen.append(('after', map_code, reason, session['current_map'], session['in_world']))

        self.server.world.hooks.register(worldmod.BEFORE_SERVER_MAP_LOAD, before)
        self.server.world.hooks.register(worldmod.ON_ENTER_WORLD, after)
        c, _ = self.enter()
        # Enter world is a map load too, so both hooks run for it (with nothing to clean up).
        self.assertEqual(seen, [('before', 101, 'enter_world', 101, False),
                                ('after', 101, 'enter_world', 101, True)])
        seen.clear()
        self.portal(c)
        self.assertEqual(seen, [('before', 102, 'portal', 101, True),      # still the old map
                                ('after', 102, 'portal', 102, True)])

    def test_a_failing_hook_never_strands_the_client(self):
        def broken(*a, **kw):
            raise RuntimeError('this group is on fire')
        self.server.world.hooks.register(worldmod.BEFORE_SERVER_MAP_LOAD, broken)
        c, _ = self.enter()
        with self.assertLogs('WS', logging.ERROR):
            self.portal(c)
        self.assertEqual(c.session['current_map'], 102)

    def test_the_welcome_line_is_a_hook_and_fires_once_per_connection(self):
        c, pkts = self.enter()
        self.assertEqual(pkts[2].opcode, 0x15)                   # after 0x07, before 0x28
        self.assertIn('[Announce]', P.to_bytes(F.FakeClient.decode(pkts[2])['text']).decode('cp949'))
        self.portal(c)                                           # no second welcome
        self.assertTrue(c.session['welcomed'])

    def test_dev_warp_uses_the_primitive_and_persists(self):
        c, _ = self.enter()
        c.session['gm'] = 1
        self.server._dev_warp(c.session, '102 600 700')
        c.expect(0x15, 0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(8))
        self.assertEqual(c.session['current_map'], 102)
        self.assertEqual((self.char()['map'], self.char()['x']), (102, 600.0))

    def test_the_driver_follows_the_players_own_position(self):
        """premium_cash-map-replay-helper: a plain walk step carries no coordinates, so the
        combat driver's memory read is the only source for where the player actually is."""
        c, _ = self.enter()
        uid = c.session['uid']
        c.session['map_confirmed'] = c.session['current_map']   # its first 0x0D named this map
        self.assertTrue(self.server._track_driver_position(uid, 812.5, 704.0))
        self.assertEqual(c.session['pos'], (812.5, 704.0))
        # A failed read (0.0), a NaN and a session that is between maps are all ignored.
        self.assertFalse(self.server._track_driver_position(uid, 0.0, 0.0))
        self.assertFalse(self.server._track_driver_position(uid, float('nan'), 1.0))
        c.session['in_world'] = False
        self.assertFalse(self.server._track_driver_position(uid, 1.0, 2.0))
        self.assertEqual(c.session['pos'], (812.5, 704.0))
        self.assertFalse(self.server._track_driver_position(999, 1.0, 2.0))

    def test_the_old_map_monsters_and_their_timers_are_dropped(self):
        """S1-07: a pending respawn for an old uid would land on the new map and the new
        map's own 0x1A would duplicate it."""
        c, _ = self.enter()
        self.portal(c)
        mobs = dict(c.session['monsters'])
        self.assertEqual(len(mobs), 8)
        self.portal(c, index=PORTAL_102_31, monsters=0)          # back to town 101: no mobs
        self.assertEqual(c.session['monsters'], {})
        self.assertTrue(all(not t.is_alive() for m in mobs.values() for t in m.timers))


class PortalGuards(TransferTest):
    """world-portal-guards, P2 exit criterion 8: no warp, no hang, no double transfer."""
    portal_cooldown = W.GameServer.PORTAL_COOLDOWN_SECS      # the live 2 s debounce

    def test_an_unmapped_portal_warps_nothing_and_names_the_en_candidates(self):
        c, _ = self.enter()
        with self.assertLogs('WS', logging.INFO) as cm:
            c.send_c2s('0x42F76B/0x7E', {'portal_line_index': 4242})
            c.expect_silence()
        line = next(l for l in cm.output if 'refused' in l)
        self.assertIn('no portal table entry 101_4242', line)
        self.assertIn('[102]', line)                         # the EN stage01_01.hmi value_num
        self.assertEqual(c.session['current_map'], 101)
        self.assertTrue(c.session['in_world'])               # not stuck mid-transfer

    def test_portal_index_zero_is_the_client_bug_and_is_dropped(self):
        # Map 101's first collision line is no portal (EN table), so a 0 there is the
        # uninitialised-slot client bug. A genuine index-0 portal (103_0) is honoured:
        # test_world_content.PortalHandler.
        c, _ = self.enter()
        self.assertIsNone(EC.portal(101, 0))
        with self.assertLogs('WS', logging.INFO) as cm:
            c.send_c2s('0x42F76B/0x7E', {'portal_line_index': 0})
            c.expect_silence()
        self.assertTrue(any('portal index 0' in l for l in cm.output))
        self.assertEqual(c.session['current_map'], 101)

    def test_two_fast_presses_do_one_transfer(self):
        c, _ = self.enter()
        c.send_c2s('0x42F76B/0x7E', {'portal_line_index': PORTAL_101_23})
        c.send_c2s('0x42F76B/0x7E', {'portal_line_index': PORTAL_101_23})
        pkts = c.expect(0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(8))
        self.assertEqual([p.opcode for p in pkts].count(0x08), 1)
        self.assertEqual(c.session['current_map'], 102)

    def test_the_cooldown_expires(self):
        self.server.PORTAL_COOLDOWN_SECS = 0.3
        c, _ = self.enter()
        self.portal(c)
        c.send_c2s('0x42F76B/0x7E', {'portal_line_index': PORTAL_102_31})
        c.expect_silence()                                   # still inside the cooldown
        time.sleep(0.35)
        self.portal(c, index=PORTAL_102_31, monsters=0)
        self.assertEqual(c.session['current_map'], 101)

    def test_a_portal_while_a_map_load_is_in_flight_is_refused(self):
        c, _ = self.enter()
        c.session['in_world'] = False
        c.send_c2s('0x42F76B/0x7E', {'portal_line_index': PORTAL_101_23})
        c.expect_silence()
        self.assertEqual(c.session['current_map'], 101)

    def test_a_same_map_destination_is_refused(self):
        c, _ = self.enter()
        EC.portals()['101_77'] = [101, 1.0, 2.0]
        c.send_c2s('0x42F76B/0x7E', {'portal_line_index': 77})
        c.expect_silence()
        self.assertEqual(c.session['current_map'], 101)

    def test_malformed_portal_rows_are_not_in_the_table(self):
        # The live table is the generated EN one (world-portal-table-en); the KR file is the
        # last-resort fallback and still loads without its 20 malformed rows.
        self.assertEqual(len(EC.portals()), 593)
        kr = EC._load_kr_portals()
        self.assertEqual(len(kr), 569)                       # 589 rows, 20 malformed
        for key in ('None_None', '_', '1002_None'):
            self.assertNotIn(key, kr)
            self.assertNotIn(key, EC.portals())


class RoundTripAndRestore(TransferTest):
    """P2 exit criterion 1 on the wire, plus the post-0x63 re-arm (quest doc F7 step 3)."""

    def test_three_round_trips_keep_bag_gold_weapon_and_quest_log(self):
        c, _ = self.enter()
        self.server._inv_add(c.session, HERB, 4, 'test')
        c.send_c2s('0x47734D/0x16', {'quest_id': QUEST})         # grants the Wooden Stick
        c.expect(0x26, 0x59)
        c.send(0x0F, CAP_0F_EQUIP_STICK)                         # wear it
        c.expect(0x1D)
        gold = self.char()['gold']

        for _ in range(3):
            state, spawn = self.portal(c)[1:3]
            self.assertEqual(self.state(state)['gold'], gold)
            self.assertEqual([(r['item_id'], r['quantity'])
                              for r in self.state(state)['repeat[consume_item_count]']],
                             [(HERB, 4)])
            self.assertEqual(self.grid(spawn)[5]['equip_item_id'], STICK)
            self.assertEqual(self.active_ids(state), [QUEST, 0, 0])
            state, spawn = self.portal(c, index=PORTAL_102_31, monsters=0)[1:3]
            self.assertEqual(self.state(state)['gold'], gold)
            self.assertEqual(self.grid(spawn)[5]['equip_item_id'], STICK)
            self.assertEqual(self.active_ids(state), [QUEST, 0, 0])
        # The stick is worn, so the bag holds only the herbs (equipment lives in the grid).
        self.assertEqual(INV.Inventory(self.char()).totals(), {HERB: 4})
        self.assertEqual(self.char()['equipped'][5]['id'], STICK)

    def test_0x63_returns_the_deck_and_re_arms_every_quest_slot(self):
        """S2C 0x03 zeroes the ready flags and cannot send them, so the 0x59 burst on the
        client's own 0x63 is what brings the green "ready to complete" state back."""
        c, _ = self.enter()
        c.send_c2s('0x47734D/0x16', {'quest_id': QUEST})
        c.expect(0x26, 0x59)
        state = self.server._quest_state(c.session)
        state.set_progress(0, 1)
        state.add_card(2030)
        state.add_card(2031)
        self.server.store.mark_dirty('test')
        self.portal(c)
        c.send_c2s('0x44EF4F/0x2F')
        c.send_c2s('0x44EF67/0x63')
        # + the once-per-connection 0x99 sub 8 channel line after the re-arm (P4 stage 4);
        # the 0x2F is answered with the S2C 0x0B friend list since P6 stage 2 (social_friend F1)
        _friends, deck, progress, _channel = c.expect(0x0B, 0x8A, 0x59, 0x99)
        self.assertEqual([r['card_item_id'] for r in F.FakeClient.decode(deck)['repeat[deck_count]']],
                         [2030, 2031])
        self.assertEqual(F.FakeClient.decode(progress), {'slot': 1, 'progress': 1})

    def test_0x63_with_no_quests_is_the_deck_alone(self):
        c, _ = self.enter()
        c.send_c2s('0x44EF67/0x63')
        deck, channel = c.expect(0x8A, 0x99)                 # 0x99: the first 0x63 only
        self.assertEqual(F.FakeClient.decode(deck)['deck_count'], 0)
        self.assertEqual(F.FakeClient.decode(channel), {'sub_type': 8, 'channel_no': 1})

    def test_a_quest_accepted_before_a_relog_is_still_in_the_0x03(self):
        c, _ = self.enter()
        c.send_c2s('0x47734D/0x16', {'quest_id': QUEST})
        c.expect(0x26, 0x59)
        c.close()
        c.thread.join(timeout=5.0)
        again, pkts = self.enter()
        self.assertEqual(self.active_ids(pkts[0]), [QUEST, 0, 0])
        self.assertEqual(Q.QuestState(self.char()).active, [QUEST, 0, 0])


if __name__ == '__main__':
    unittest.main()
