#!/usr/bin/env python3
"""
test_persistence.py - world state, wallet, bag and quest log survive a relog
(world-persistence, shop_storage-wallet, item_inventory-model-persist,
quest_cards_misc-quest-state-model)

The offline half of the P2 exit criteria that live verification found broken: before this
stage `_map_transfer` wrote x/y onto a COPY of the character record and never wrote
`char['map']` at all, so map, position and HP were NEVER persisted, and gold/bag/quests
lived on the session and died with the connection.

Each test drives the real handler loop through fakeclient (no port, no game client) and
then reconnects, which is exactly what the lead does in the client:

    criterion 1  portal 101 -> 102 -> 101 keeps bag, gold, worn weapon and quest log;
                 buying then changes gold by exactly the price
    criterion 2  log out on 102 with reduced HP, relog there with that HP (no refill)
    criterion 3  quest 26 accept -> the Wooden Stick and the log survive a relog
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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import en_content as EC  # noqa: E402
import fakeclient as F  # noqa: E402
import inventory as INV  # noqa: E402
import packets as P  # noqa: E402
from wsproto import hexbytes  # noqa: E402

W = F.import_server()
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
CAP_7E_101_TO_102 = hexbytes('17 00 00 00')      # the live portal capture (map 101 line 23)
CAP_0F_EQUIP_STICK = hexbytes('B3 00 00 00 00')
# One C2S 0x0D that names a position: the spec sends pos_x/pos_y only in the interact
# branch ((state_lo >> 16) & 0xF != 0, and 12 would add an item id), so a plain walk step
# is 18 bytes of state bits with no coordinates at all.
MOVE_TO = {'realtime_delta_ms': 0, 'map_code': 102, 'logic_elapsed_ms': 30,
           'state_lo': 2 | (9 << 16), 'state_hi': 0, 'target_uid': 0,
           'pos_x': 612.0, 'pos_y': 700.0, 'target_dx': 0.0, 'target_dy': 0.0,
           'flag_8db': 0, 'flag_8e7': 0, 'timer_dac': 0, 'target_action_event': 0}
STICK, HERB = 179, 5
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
            'accounts.json changed during test_persistence.py (tests must only use temp copies)'


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class PersistenceTest(unittest.TestCase):
    """A fresh GameServer on a temp accounts.json; clients come and go inside one test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_persist_')
        self.server = F.make_server(self.tmp)
        self.clients = []

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
        """A connection that is logged in and in world; returns (client, packets)."""
        c = self.client()
        c.send_c2s('0x44D8BF/0x01', {'account_id': user, 'password': password})
        self.assertEqual(F.FakeClient.decode(c.expect(0x02))['result'], 1)
        c.send_c2s('0x42F904/0x2B', {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907, 'char_name': name})
        pkts = c.expect(0x03, 0x07, 0x15, 0x28, 0x44, *F.mob_packets(monsters))
        self.assertTrue(c.wait_session(lambda s: s.get('in_world')))
        return c, pkts

    def disconnect(self, c):
        """Close a connection and wait for the server's finally block to finish: it is the
        block that saves the world state, so a test must not read the record before it."""
        uid = c.session.get('uid')
        c.close()
        c.thread.join(timeout=5.0)
        self.assertFalse(c.thread.is_alive())
        self.assertIsNone(self.server.world.session(uid))

    def char(self, name='TestHero', user='test'):
        return self.server.store.find_character(user, name)

    def disk(self):
        self.server.store.flush()
        with open(self.server.db_file, encoding='utf-8') as f:
            return json.load(f)

    def portal_to_102(self, c):
        c.send(0x7E, CAP_7E_101_TO_102)
        return c.expect(0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(8))

    def state_of(self, pkt):
        return F.FakeClient.decode(pkt, allow_trailing=True)


class WorldState(PersistenceTest):
    def test_map_transfer_persists_map_and_arrival_point(self):
        """The live-verified gap: _map_transfer wrote x/y onto a copy and never wrote the
        map, so the store kept saying 101 whatever the client was looking at."""
        c, _ = self.enter()
        char = self.char()
        # the start point (700, 812) and the portal arrival (50, 712) are both 100 px above
        # their floor: a map load saves the floor point he lands on (livetest bug 5)
        self.assertEqual((char['map'], char['x'], char['y']), (101, 700.0, 912.0))
        self.portal_to_102(c)
        target = EC.portal(101, 23)
        self.assertEqual(target, (102, 50.0, 712.0))
        self.assertEqual((char['map'], char['x'], char['y']), (102, 50.0, 812.0))
        self.assertEqual(self.disk()['test']['characters'][0]['map'], 102)

    def test_a_map_transfer_invalidates_the_driver_entity_cache(self):
        """Live P2 verification: after any warp the memory combat driver kept a cached
        entity pointer for the freed local player (a recycled block still reads uid 1 at
        +0x84), so swings stopped landing and the position sampler wrote the OLD map's
        coordinates - a character was saved on map 102 at a map-201 point and respawned
        mid-air. _map_transfer must bump the epoch the driver caches."""
        c, _ = self.enter()
        before = self.server._driver_epoch
        self.portal_to_102(c)
        self.assertGreater(self.server._driver_epoch, before,
                           'map transfer must invalidate the driver entity cache')

        # The driver's own guard: same uid in a recycled block is NOT enough to keep it.
        pcache = [0x1000, 1, before]                      # [entity, uid, epoch]
        player, my_uid, cached_epoch = pcache
        stale = not (self.server._driver_epoch != cached_epoch or not player or not my_uid)
        self.assertFalse(stale, 'a cache from before the transfer must be re-walked')

    def test_relog_returns_to_the_map_and_hp_you_left(self):
        """P2 exit criterion 2: log out on 102 at a known spot, relog there with the HP you
        left with. The map load used to send max HP, which was a free heal every portal."""
        c, _ = self.enter()
        self.portal_to_102(c)
        with self.server._combat_lock(c.session):
            c.session['hp'] = 37                       # as if a Pupu had hit back
            c.session['pos'] = (600.0, 712.0)
        self.disconnect(c)
        stored = self.disk()['test']['characters'][0]
        self.assertEqual((stored['map'], stored['hp']), (102, 37))
        self.assertEqual((stored['x'], stored['y']), (600.0, 712.0))

        again, pkts = self.enter(monsters=8)
        state, spawn, _, hp, _ = pkts[:5]
        self.assertEqual(self.state_of(state)['map_code'], 102)
        row = F.FakeClient.decode(spawn)['repeat[player_count]'][0]
        # (600, 712) is above 102's floor there: the relog lands on the floor under the
        # saved point, y 912 (livetest bug 5)
        self.assertEqual((row['pos_x'], row['pos_y'], row['cur_hp']), (600.0, 912.0, 37))
        self.assertEqual(F.FakeClient.decode(hp)['hp'], 37)          # no refill
        self.assertEqual(again.session['current_map'], 102)

    def test_a_disconnect_persists_the_last_position_the_server_knows(self):
        """session['pos'] was written by no handler, so every save passed x=y=None and the
        record kept whatever point it already held. It now follows the three points the
        server actually learns: the spawn, each portal arrival, and any C2S 0x0D that
        names one (following a plain walk step is world-move-relay, a later phase)."""
        c, _ = self.enter()
        self.assertEqual(c.session['pos'], (700.0, 912.0))          # the spawn point, on the
        self.portal_to_102(c)                                        # floor (livetest bug 5)
        self.assertEqual(c.session['pos'], (50.0, 812.0))            # the arrival point, ditto
        c.send_c2s('0x42CE94/0x0D', MOVE_TO)                         # the client's own point
        self.assertTrue(c.wait_session(lambda s: s.get('pos') == (612.0, 700.0)))
        self.disconnect(c)
        stored = self.disk()['test']['characters'][0]
        self.assertEqual((stored['map'], stored['x'], stored['y']), (102, 612.0, 700.0))
        # and the relog spawns there
        again, pkts = self.enter(monsters=8)
        row = F.FakeClient.decode(pkts[1])['repeat[player_count]'][0]
        # on the floor under that point (a slope there, y 905: livetest bug 5)
        self.assertEqual((row['pos_x'], row['pos_y']), (612.0, 905.0))

    def test_portal_sends_current_hp_not_max(self):
        c, _ = self.enter()
        with self.server._combat_lock(c.session):
            c.session['hp'], c.session['mp'] = 42, 8
        pkts = self.portal_to_102(c)
        self.assertEqual(F.FakeClient.decode(pkts[3])['hp'], 42)
        self.assertEqual(F.FakeClient.decode(pkts[4])['mp'], 8)
        self.assertEqual((self.char()['hp'], self.char()['mp']), (42, 8))

    def test_the_world_tick_mirrors_live_sessions(self):
        c, _ = self.enter()
        with self.server._combat_lock(c.session):
            c.session['hp'] = 55
            c.session['pos'] = (123.0, 456.0)
        self.assertEqual(self.server._tick_world_state(), 1)
        self.assertEqual((self.char()['hp'], self.char()['x']), (55, 123.0))
        self.assertEqual(self.server._tick_world_state(), 0)         # nothing changed since

    def test_a_character_select_session_saves_nothing(self):
        c = self.client()
        c.send_c2s('0x44D8BF/0x01', {'account_id': 'admin', 'password': 'admin'})
        c.expect(0x02)
        self.assertFalse(self.server._save_world_state(c.session))


class WalletPersistence(PersistenceTest):
    def test_buy_charges_exactly_the_price_and_persists(self):
        """P2 exit criterion 1 (second half): buying a potion changes gold by exactly the
        price, with no jump to 999999 (the old session default) on the way."""
        c, pkts = self.enter()
        gold0 = self.char()['gold']
        self.assertEqual(self.state_of(pkts[0])['gold'], gold0)      # 0x03 carries the store's gold
        price = EC.items().price(HERB, 3)
        c.send_c2s('0x469D9C/0x0B', {'item_id': HERB, 'qty': 3, 'npc_id': 8})     # Misty
        rec = F.FakeClient.decode(c.expect(0x18))
        self.assertEqual(rec['gold'], gold0 - price)
        self.assertEqual((rec['item_id'], rec['count']), (HERB, 3))
        self.assertEqual(self.char()['gold'], gold0 - price)
        self.assertEqual(self.disk()['test']['characters'][0]['gold'], gold0 - price)

    def test_gold_and_bag_survive_a_portal_round_trip(self):
        """P2 exit criterion 1: three round trips keep bag, gold, worn weapon and quest log.
        The 0x03 of every map load carries the stored values instead of the constants."""
        c, _ = self.enter()
        self.server._inv_add(c.session, HERB, 4, 'test')
        self.server._inv_add(c.session, STICK, 1, 'test')
        c.send(0x0F, CAP_0F_EQUIP_STICK)                             # equip the stick
        c.expect(0x1D)
        c.send_c2s('0x47734D/0x16', {'quest_id': 26})
        c.expect(0x26, 0x59)
        gold = self.char()['gold']

        for _ in range(3):
            state = self.portal_to_102(c)[1]
            self.assertEqual(self.state_of(state)['gold'], gold)
            c.send_c2s('0x42F76B/0x7E', {'portal_line_index': 31})   # 102 -> 101 (EN line 31)
            state = c.expect(0x08, 0x03, 0x07, 0x28, 0x44)[1]
            self.assertEqual(self.state_of(state)['gold'], gold)
        char = self.char()
        self.assertEqual(INV.Inventory(char).totals(), {HERB: 4, STICK: 1})   # stick from the quest
        self.assertEqual(char['equipped'][5]['id'], STICK)
        self.assertEqual(char['quests']['active'], [26, 0, 0])
        self.assertEqual(char['gold'], gold)

    def test_a_session_without_a_character_has_an_empty_wallet(self):
        c = self.client()
        c.send_c2s('0x44D8BF/0x01', {'account_id': 'admin', 'password': 'admin'})
        c.expect(0x02)
        self.assertEqual(self.server._wallet(c.session), (0, 0))     # not a 999999 default
        self.assertIsNone(self.server._bag(c.session))


class BagAndQuestRelog(PersistenceTest):
    def test_everything_this_stage_persists_comes_back_after_a_relog(self):
        c, _ = self.enter()
        self.server._inv_add(c.session, HERB, 7, 'test')
        self.server._inv_add(c.session, 2975, 3, 'test')              # etc tab
        c.send_c2s('0x47734D/0x16', {'quest_id': 26})                 # grants the Wooden Stick
        c.expect(0x26, 0x59)
        c.send(0x0F, CAP_0F_EQUIP_STICK)
        c.expect(0x1D)
        state = self.server._quest_state(c.session)
        state.set_progress(0, 1)                                      # as a Pupu kill would
        self.server.store.mark_dirty('test')
        self.portal_to_102(c)
        self.disconnect(c)

        # Everything is on disk, in the F4 shapes.
        stored = self.disk()['test']['characters'][0]
        self.assertEqual(stored['inventory']['consume'], {'5': 7})
        self.assertEqual(stored['inventory']['etc'], {'2975': 3})
        self.assertEqual(stored['inventory']['equip'], [])            # the stick is worn
        self.assertEqual(stored['equipped'], {'5': {'id': STICK, 'w': [0] * 6}})
        self.assertEqual(stored['quests'], {'active': [26, 0, 0], 'progress': [1, 0, 0],
                                            'completed': []})
        self.assertEqual(stored['map'], 102)

        # And it is what the next connection is told and can act on.
        again, pkts = self.enter(monsters=8)
        row = F.FakeClient.decode(pkts[1])['repeat[player_count]'][0]
        self.assertEqual(row[f'repeat[{16}]'][5]['equip_item_id'], STICK)
        bag = self.server._bag(again.session)
        self.assertEqual(bag.totals(), {HERB: 7, 2975: 3})
        self.assertEqual(self.server._quest_state(again.session).active, [26, 0, 0])
        self.assertEqual(self.server._quest_slot(again.session, 26), 1)
        # A quest that is held cannot be accepted again, and a second 0x16 is NOT a turn-in
        # (S1-11): it is refused with a system line and the slot keeps its progress.
        again.send_c2s('0x47734D/0x16', {'quest_id': 26})
        self.assertEqual(F.FakeClient.decode(again.expect(0x15))['text'],
                         '[Warning] You are already on this quest.')
        self.assertEqual(self.char()['quests'], {'active': [26, 0, 0], 'progress': [1, 0, 0],
                                                 'completed': []})
        # The turn-in is C2S 0x17, and it completes the quest exactly once.
        again.send_c2s('0x477F3E/0x17', {'quest_id': 26})
        again.expect(0x27, 0x21)
        self.assertEqual(self.char()['quests']['completed'], [[26, 1]])

    def test_a_completed_non_repeatable_quest_is_not_offered_again(self):
        c, _ = self.enter()
        state = self.server._quest_state(c.session)
        state.complete_append(26)                                     # as a turn-in would
        self.server.store.mark_dirty('test')
        c.send_c2s('0x47734D/0x16', {'quest_id': 26})                 # Repeat 0: exhausted
        self.assertEqual(F.FakeClient.decode(c.expect(0x15))['text'],
                         '[Warning] You have already completed this quest.')
        self.assertEqual(self.char()['quests']['active'], [0, 0, 0])

    def test_kr_only_ids_never_reach_the_persisted_bag(self):
        c, _ = self.enter()
        with self.assertLogs('WS', logging.WARNING):
            self.assertIsNone(self.server._inv_add(c.session, 4356, 1, 'test'))
        self.disconnect(c)
        self.assertEqual(self.disk()['test']['characters'][0]['inventory']['etc'], {})


if __name__ == '__main__':
    unittest.main()
