#!/usr/bin/env python3
"""
test_classvillage.py - class change, quest job/skill rewards and the village transfer
(P3 stage 5: lc-class-change, quest_cards_misc-quest-rewards-skill-class,
world-village-transfer; login_character.md F8 / 3.5.5, world_movement_npc.md 1.6 / F5)

Two layers:
- the pure rules: classchange.py (FUN_004249E0's job-item table, the 0x70C588 class names
  with the tier-2 shift) and village.py (FUN_00422050's fee, the manner discount, towns
  and arrival points), plus the exe tables re-read from WindSlayer.exe when it is present;
- the server through fakeclient (no port, no game client, temp accounts.json): `!job`,
  `!give`, a shop buy and a quest reward change the class and persist it (the next 0x02
  select screen and 0x07 HUD record carry it after a relog), observers get 0x58, and C2S
  0x5D answers 0x81 {1, gold} + the MapTransfer, or 0x81 {0} with nothing moved or paid.
"""
import copy
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

import classchange as CC  # noqa: E402
import config as cfgmod  # noqa: E402
import en_content as EC  # noqa: E402
import fakeclient as F  # noqa: E402
import hpmp  # noqa: E402
import inventory as INV  # noqa: E402
import quests as Q  # noqa: E402
import village as V  # noqa: E402

W = F.import_server()
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
HAVE_HII = os.path.exists(EC.hs_path('windslayer.hii'))
CLIENT_EXE = os.path.join(os.path.dirname(HERE), 'WindSlayer.exe')
WARRIOR, MONK, ARCHER, PRIEST, MAGE, ROGUE = 180, 203, 204, 205, 206, 207
BERSERKER, PALADIN = 3076, 3077
DOUBLE_JUMP, PUPU_NPCCODE = 94, 1
GOLD = W.storemod.DEFAULT_GOLD

ACCOUNTS = {
    'test': {'password': 'test',
             'characters': [{'name': 'TestHero', 'level': 1, 'class': 0, 'map': 101,
                             'x': 1411, 'y': 714, 'hp': 100, 'mp': 50, 'gm': 1}]},
    'admin': {'password': 'admin',
              'characters': [{'name': 'Watcher', 'level': 1, 'class': 0, 'map': 101,
                              'x': 1411, 'y': 714, 'hp': 100, 'mp': 50}]},
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
            'accounts.json changed during test_classvillage.py (tests must only use temp copies)'


def dec(pkt, length=None):
    rec = F.FakeClient.decode(pkt, allow_trailing=pkt.opcode == 0x03)
    if length is not None:
        assert len(pkt.payload) == length, f'S2C 0x{pkt.opcode:02X}: {len(pkt.payload)} B, want {length}'
    return rec


# =========================================================================== pure rules
class JobTable(unittest.TestCase):
    """FUN_004249E0 (decomp 0x4249E0), one row per case of its two switches."""

    def test_base_items_need_a_novice_and_set_only_the_class(self):
        for item, cls in ((WARRIOR, 1), (MONK, 2), (ARCHER, 3), (ROGUE, 4), (MAGE, 5), (PRIEST, 6)):
            with self.subTest(item=item):
                self.assertEqual(CC.apply(0, 0, item)[:4], (True, item, cls, 0))
                self.assertFalse(CC.apply(cls, 0, item).ok)            # a class changes once
                self.assertFalse(CC.apply(1, 0, item).ok)
        # the tier is neither tested nor written by a base item
        self.assertEqual(CC.apply(0, 2, WARRIOR)[:4], (True, WARRIOR, 1, 2))

    def test_tier_items_need_their_base_class_at_tier_0_and_set_only_the_tier(self):
        rows = {3076: (1, 1), 3077: (1, 2), 3078: (2, 1), 3079: (2, 2), 3080: (4, 1), 3081: (4, 2),
                3083: (5, 1), 3084: (5, 2), 3085: (6, 1), 3086: (6, 2), 3087: (3, 1), 3088: (3, 2)}
        self.assertEqual(set(rows) | set(CC.BASE_ITEMS), set(CC.JOB_ITEMS))
        for item, (cls, tier) in rows.items():
            with self.subTest(item=item):
                self.assertEqual(CC.apply(cls, 0, item)[:4], (True, item, cls, tier))
                self.assertEqual(CC.apply(cls, 0, item).old, (cls, 0))
                self.assertFalse(CC.apply(cls, 1, item).ok)            # already advanced
                self.assertFalse(CC.apply(0, 0, item).ok)              # a Novice cannot
                other = 1 + cls % 6
                self.assertFalse(CC.apply(other, 0, item).ok)          # another class cannot
                self.assertEqual(CC.prerequisite(item), (cls, 0))
        self.assertEqual(CC.prerequisite(WARRIOR), (0, 0))

    def test_anything_else_is_not_a_job_item(self):
        # 3082 "====" and every other Type-4 id take FUN_004249E0's default case: no change
        # and FUN_00440BC0 shows nothing, so the server never grants them.
        for item in (3082, 5, 179, 3089, 0):
            self.assertFalse(CC.is_job_item(item))
            self.assertIn('not a job-change item', CC.apply(0, 0, item).why)

    @unittest.skipUnless(HAVE_HII, 'needs the EN client hs/windslayer.hii')
    def test_every_job_item_is_an_en_type_4_record(self):
        for item in CC.JOB_ITEMS:
            self.assertEqual(EC.items().type_of(item), EC.TYPE_CLASS_CHANGE, item)
        self.assertEqual(EC.item_name(BERSERKER), 'Job Change: Berserker')
        self.assertEqual(EC.items().type_of(3082), EC.TYPE_CLASS_CHANGE)   # the unused "===="

    def test_names_are_what_the_client_prints(self):
        self.assertEqual([CC.class_name(c) for c in range(7)],
                         ['Novice', 'Warrior', 'Monk', 'Archer', 'Rogue', 'Mage', 'Priest'])
        self.assertEqual(CC.class_name(1, 1), 'Berserker')
        self.assertEqual(CC.class_name(6, 1), 'Bishop')
        # tier-2 row shifted by one (login_character 3.5.5): a Paladin reads "Counter"
        self.assertEqual(CC.class_name(1, 2), 'Counter')
        self.assertEqual(CC.class_name(6, 2), '')
        self.assertEqual(CC.class_name(7, 0), '')
        self.assertEqual(CC.popup_text(1, 1), 'Berserker is your new class.')
        self.assertIsNone(CC.popup_text(1, 3))

    def test_only_base_items_recompute_the_maxima(self):
        self.assertTrue(CC.recomputes_maxima(WARRIOR))
        self.assertFalse(CC.recomputes_maxima(BERSERKER))


class VillageFees(unittest.TestCase):
    """FUN_00422050 + FUN_0043dc40's discount (world_movement_npc.md 1.6)."""

    # world_movement_npc.md 1.6 base-fee matrix, cur (row) x dest (column), 1..10
    MATRIX = [
        [0, 1000, 1940, 2840, 3700, 3700, 4520, 5300, 6040, 6740],
        [1000, 0, 1000, 1940, 2840, 2840, 3700, 4520, 5300, 6040],
        [1940, 1000, 0, 1000, 1940, 1940, 2840, 3700, 4520, 5300],
        [2840, 1940, 1000, 0, 1000, 1000, 1940, 2840, 3700, 4520],
        [3700, 2840, 1940, 1000, 0, 1000, 1940, 2840, 3700, 4520],
        [3700, 2840, 1940, 1000, 1000, 0, 1000, 1940, 2840, 3700],
        [4520, 3700, 2840, 1940, 1940, 1000, 0, 1000, 1940, 2840],
        [5300, 4520, 3700, 2840, 2840, 1940, 1000, 0, 1000, 1940],
        [6040, 5300, 4520, 3700, 3700, 2840, 1940, 1000, 0, 1000],
        [6740, 6040, 5300, 4520, 4520, 3700, 2840, 1940, 1000, 0],
    ]

    def test_the_whole_matrix(self):
        for cur in range(1, 11):
            town = V.town_map(cur)
            for dest in range(1, 11):
                with self.subTest(cur=cur, dest=dest):
                    self.assertEqual(V.base_fee(town, dest), self.MATRIX[cur - 1][dest - 1])

    def test_towns_and_groups(self):
        self.assertEqual([V.town_map(i) for i in range(1, 11)],
                         [101, 201, 401, 701, 501, 801, 601, 901, 1001, 1101])
        self.assertIsNone(V.town_map(0))
        self.assertIsNone(V.town_map(11))
        self.assertEqual(V.village_of(501), 5)
        self.assertEqual(V.village_of(514), 5)               # a field map of the group
        self.assertIsNone(V.village_of(301))                 # group 3 is no village
        self.assertIsNone(V.village_of(9701))

    def test_the_fee_follows_the_group_not_the_map(self):
        self.assertEqual(V.fee(102, 2), 1000)                # world doc: 102 -> Popola 1000
        self.assertEqual(V.fee(514, 2), V.fee(501, 2))
        self.assertEqual(V.fee(501, 5), 0)                   # already there
        self.assertEqual(V.fee(301, 2), 0)                   # no village group: no dialog
        self.assertEqual(V.fee(5, 1), 0)                     # group 0 (map < 100)

    def test_amakusa_mining_is_distance_0_and_crossing_amakusa_costs_one_more(self):
        self.assertEqual(V.base_fee(501, 6), 1000)           # ORDER 5 == 5: FEE[1], no bonus
        self.assertEqual(V.base_fee(801, 5), 1000)
        self.assertEqual(V.base_fee(501, 7), 1940)           # d 1 + crossing -> FEE[2]
        self.assertEqual(V.base_fee(601, 5), 1940)
        self.assertEqual(V.base_fee(801, 7), 1000)           # Mining is not the crossing village

    def test_manner_500_takes_ten_percent_truncated(self):
        base = [1000, 1940, 2840, 3700, 4520, 5300, 6040, 6740, 7400, 8020]
        want = [900, 1746, 2556, 3330, 4068, 4770, 5436, 6066, 6660, 7218]
        self.assertEqual([int(b * V.DISCOUNT) for b in base], want)
        self.assertEqual(V.fee(501, 2, manner=499), 2840)
        self.assertEqual(V.fee(501, 2, manner=500), 2556)
        self.assertEqual(V.fee(501, 2, manner=-40), 2840)
        self.assertEqual(V.fee(501, 5, manner=900), 0)

    def test_arrival_points(self):
        class Line:
            def __init__(self, x1, y1, x2):
                self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y1

        class Tile:
            def __init__(self, npc_id, x, y):
                self.npc_id, self.x, self.y = npc_id, x, y

        class Map:
            def __init__(self, tiles):
                self.tiles = tiles

            def npc_tiles(self):
                return self.tiles

            def floor_line(self, tile):
                return Line(tile.x, tile.y + 20, tile.x + 300)

        maria = Map([Tile(9, 10, 10), Tile(V.GARAN_MARIA, 2115, 645)])
        got = V.arrival(5, map_data=lambda town: maria, fallback=lambda town: (1.0, 2.0))
        self.assertEqual(got[:3], (501, 2265.0, 665.0 - 100.0))
        self.assertIn('Garan Maria', got[3])
        got = V.arrival(2, map_data=lambda town: Map([]), fallback=lambda town: (50.0, 1612.0))
        self.assertEqual(got[:3], (201, 50.0, 1612.0))
        got = V.arrival(5, overrides={'501': [10, 20]}, map_data=lambda town: maria)
        self.assertEqual(got[:3], (501, 10.0, 20.0))
        self.assertIsNone(V.arrival(11))
        self.assertIsNone(V.arrival(2, map_data=lambda town: None, fallback=lambda town: None))

    @unittest.skipUnless(HAVE_HII, 'needs the EN client map files')
    def test_the_real_towns_with_garan_maria(self):
        # world doc 1.6 lists her tile on 501/601/701/801/901/1001/1101, not on 101/201/401
        for index in range(1, 11):
            town, x, y, how = V.arrival(index, fallback=lambda town: (0.0, 0.0))
            self.assertEqual(town, V.town_map(index))
            self.assertEqual(how.startswith('next to Garan Maria'), town not in (101, 201, 401),
                             (town, how))
        self.assertEqual(V.arrival(5)[:3], (501, 2332.0, 565.0))


class ExeTables(unittest.TestCase):
    """The constants above are the client's own bytes (skipped without the exe)."""

    def setUp(self):
        try:
            import pefile
        except ImportError:
            self.skipTest('pefile not available')
        if not os.path.exists(CLIENT_EXE):
            self.skipTest(f'{CLIENT_EXE} not available')
        pe = pefile.PE(CLIENT_EXE, fast_load=True)
        self.rd = lambda va, n: pe.get_data(va - pe.OPTIONAL_HEADER.ImageBase, n)   # noqa: E731

    def test_village_tables(self):
        self.assertEqual(struct.unpack('<13H', self.rd(0x70D07C, 26)), V.FEES)
        self.assertEqual(struct.unpack('<11H', self.rd(0x70D098, 22)), V.ORDER)
        self.assertEqual(struct.unpack('<11H', self.rd(0x70D04C, 22)), V.GROUP)
        self.assertEqual(struct.unpack('<d', self.rd(0x6F8AF8, 8))[0], V.DISCOUNT)

    def test_class_name_table(self):
        raw = self.rd(0x70C588, 21 * 15)
        names = tuple(raw[i * 15:(i + 1) * 15].split(b'\0')[0].decode() for i in range(21))
        self.assertEqual(names, CC.CLASS_NAMES)


# ================================================================ server (fakeclient)
class Server(unittest.TestCase):
    """A fresh GameServer on a temp accounts.json; TestHero is a GM on map 101."""
    config = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_class_')
        self.server = F.make_server(self.tmp, accounts=copy.deepcopy(ACCOUNTS), config=self.config)
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def char(self, name='TestHero', user='test'):
        return self.server.store.find_character(user, name)

    def place(self, map_code, x=1411.0, y=714.0, name='TestHero', user='test', **fields):
        with self.server.store.lock:
            ch = self.char(name, user)
            ch.update({'map': map_code, 'x': x, 'y': y, **fields})
        self.server.store.mark_dirty('test setup')
        return ch

    def enter(self, user='test', password='test', name='TestHero'):
        """Log in and enter world; returns (client, 0x02 record, 0x07 record)."""
        c = F.FakeClient(self.server)
        self.clients.append(c)
        c.send_c2s('0x44D8BF/0x01', {'account_id': user, 'password': password})
        login = dec(c.expect(0x02))
        self.assertEqual(login['result'], 1)
        c.send_c2s('0x42F904/0x2B', {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907, 'char_name': name})
        map_code = int(self.char(name, user).get('map') or 101)
        mobs = len(EC.map_spawns(map_code))
        # world-presence: + 0x04 / 0x05 when another client is on that map (F.expect_entry)
        pkts = F.expect_entry(c, (0x03, 0x07, 0x15, 0x28, 0x44, *F.mob_packets(mobs)), self.clients, map_code)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world')))
        return c, login, dec(pkts[1])['repeat[player_count]'][0]

    def relog(self, c, **kw):
        F.close_seen(c, self.clients)           # world-presence: the others see it leave (0x06)
        return self.enter(**kw)

    def gm(self, c, text):
        c.send_c2s('0x445CA7/0x03', {'msg_len': len(text), 'message': text})

    @staticmethod
    def text(pkt):
        return dec(pkt)['text']

    @staticmethod
    def selected(login, name='TestHero'):
        return next(ch for ch in login['repeat[char_count]'] if ch['name'] == name)

    def gold(self):
        return INV.Wallet(self.char()).gold


@unittest.skipUnless(HAVE_HII, 'needs the EN client hs/windslayer.hii')
class ClassChange(Server):
    """lc-class-change: P3 exit criterion 6 offline."""

    def test_job_180_makes_a_warrior_with_the_popup_packet_and_no_heal(self):
        c, _, rec = self.enter()
        self.assertEqual((rec['job'], rec['job2']), (0, 0))
        self.assertEqual(c.session['max_hp'], hpmp.max_hp(0, 1, 3))           # Novice 140
        self.gm(c, f'!job {WARRIOR}')
        grant, line = c.expect(0x18, 0x15)
        self.assertEqual(dec(grant, 16), {'gold': GOLD, 'victy': 0, 'item_id': WARRIOR, 'count': 1})
        self.assertIn('Warrior', self.text(line))
        self.assertEqual(CC.of(self.char()), (1, 0))
        # FUN_00440BC0 recomputes the maxima and heals nothing (live login_character#17)
        self.assertEqual(c.session['max_hp'], hpmp.max_hp(1, 1, 3))           # Warrior 306
        self.assertEqual(c.session['max_mp'], hpmp.max_mp(1, 1, 1))
        self.assertEqual(c.session['hp'], 100)
        # a second 180 does nothing, on the client (live #17) and here: no packet but the reply
        self.gm(c, f'!give {WARRIOR}')
        self.assertIn('not applied', self.text(c.expect(0x15)))
        self.assertEqual(CC.of(self.char()), (1, 0))

    def test_job_3076_from_a_novice_goes_through_warrior_and_persists_across_relog(self):
        c, login, _ = self.enter()
        self.assertEqual((self.selected(login)['class_id'], self.selected(login)['job_branch']), (0, 0))
        self.gm(c, f'!job {BERSERKER}')
        fixup, grant, line = c.expect(0x58, 0x18, 0x15)
        # the silent GM fix-up to the prerequisite (F8 step 3), then the real job item
        self.assertEqual(dec(fixup, 6), {'uid': 1, 'class': 1, 'class_tier': 0})
        self.assertEqual(dec(grant)['item_id'], BERSERKER)
        self.assertIn('Warrior -> Berserker', self.text(line))
        self.assertEqual(CC.of(self.char()), (1, 1))
        # relog: the select screen (0x02 class_id/job_branch) and the HUD record (0x07) agree
        c, login, rec = self.relog(c)
        self.assertEqual((self.selected(login)['class_id'], self.selected(login)['job_branch']), (1, 1))
        self.assertEqual((rec['job'], rec['job2']), (1, 1))
        # and a portal keeps it too
        c.send(0x7E, bytes.fromhex('17000000'))
        rec = dec(c.expect(0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(8))[2])
        self.assertEqual((rec['repeat[player_count]'][0]['job'], rec['repeat[player_count]'][0]['job2']),
                         (1, 1))

    def test_a_tier_switch_resets_the_tier_first(self):
        self.place(101, **{'class': 1, 'job2': 1})
        c, _, _ = self.enter()
        self.gm(c, f'!job {PALADIN}')
        fixup, grant, _line = c.expect(0x58, 0x18, 0x15)
        self.assertEqual(dec(fixup), {'uid': 1, 'class': 1, 'class_tier': 0})
        self.assertEqual(dec(grant)['item_id'], PALADIN)
        self.assertEqual(CC.of(self.char()), (1, 2))

    def test_job_novice_resets_with_0x58_and_a_redraw(self):
        self.place(101, **{'class': 5, 'job2': 1})
        c, _, _ = self.enter()
        self.gm(c, '!job novice')
        fixup, redraw, line = c.expect(0x58, 0x14, 0x15)
        self.assertEqual(dec(fixup), {'uid': 1, 'class': 0, 'class_tier': 0})
        self.assertEqual(dec(redraw, 7), {'uid': 1, 'stat_index': 0, 'value': self.char()['str']})
        self.assertIn('Elementalist -> Novice', self.text(line))
        self.assertEqual(CC.of(self.char()), (0, 0))

    def test_bad_arguments_change_nothing(self):
        c, _, _ = self.enter()
        for arg, why in (('', 'no job item id'), ('5', 'not a job-change item'),
                         ('3082', 'not a job-change item'), ('x', 'not a number')):
            self.gm(c, f'!job {arg}'.strip())
            self.assertIn(why, self.text(c.expect(0x15)))
        self.assertEqual(CC.of(self.char()), (0, 0))

    def test_observers_get_0x58(self):
        c, _, _ = self.enter()
        other, _, _ = self.enter('admin', 'admin', 'Watcher')
        self.gm(c, f'!job {MAGE}')
        c.expect(0x18, 0x15)
        self.assertEqual(dec(other.expect(0x58), 6), {'uid': 1, 'class': 5, 'class_tier': 0})

    def test_give_of_a_job_item_is_applied_and_persisted(self):
        c, _, _ = self.enter()
        self.gm(c, f'!give {ARCHER}')
        grant, line = c.expect(0x18, 0x15)
        self.assertEqual(dec(grant)['item_id'], ARCHER)
        self.assertIn('Archer', self.text(line))
        self.assertEqual(CC.of(self.char()), (3, 0))
        self.assertEqual(INV.Inventory(self.char()).totals(), {})              # never bagged

    def test_a_shop_buy_checks_the_table_before_it_charges(self):
        # No EN merchant stocks a job item (shop_storage-npc-buy validates the hni list), so
        # the path is exercised on a merchant whose stock the test extends.
        misty = W.EC.npcs().get(8)
        self.addCleanup(setattr, misty, 'shop_items', list(misty.shop_items))
        misty.shop_items = list(misty.shop_items) + [MONK, PRIEST]
        c, _, _ = self.enter()
        c.send_c2s('0x469D9C/0x0B', {'item_id': 5, 'qty': 1, 'npc_id': 1})    # npc 1: no merchant
        resync, why = c.expect(0x18, 0x15)
        self.assertEqual(dec(resync)['item_id'], 0)
        self.assertIn('not for sale', self.text(why))
        c.send_c2s('0x469D9C/0x0B', {'item_id': MONK, 'qty': 1, 'npc_id': 8})
        grant = dec(c.expect(0x18))
        self.assertEqual((grant['item_id'], grant['count']), (MONK, 1))
        self.assertEqual(CC.of(self.char()), (2, 0))
        # a Monk cannot take another base class: 0x18 resync (item 0) + the reason
        c.send_c2s('0x469D9C/0x0B', {'item_id': PRIEST, 'qty': 1, 'npc_id': 8})
        resync, why = c.expect(0x18, 0x15)
        self.assertEqual(dec(resync)['item_id'], 0)
        self.assertIn('cannot change to that class', self.text(why))
        self.assertEqual(CC.of(self.char()), (2, 0))
        self.assertEqual(self.gold(), GOLD)                                    # hii Buy 0

    def test_not_in_world_is_refused(self):
        res = self.server._apply_job_item({'username': 'test', 'char_name': 'TestHero'}, WARRIOR)
        self.assertFalse(res.ok)
        self.assertIn('not in world', res.why)
        self.assertEqual(CC.of(self.char()), (0, 0))


@unittest.skipUnless(HAVE_HII, 'needs the EN client hs/windslayer.hii')
class QuestRewards(Server):
    """quest_cards_misc-quest-rewards-skill-class: type-3 and type-4 rewards are mirrored
    (no packet: the client applied them on 0x27) and survive a relog."""

    def make_quest(self, reward, quest_id=65005, **fields):
        spec = {'Start_Lev': ['1'], 'End_Lev': ['99'], 'SNPC': ['75'], 'ENPC': ['75'],
                'Reward': [''.join(f'{i:04d}' for i in reward) + '0' * (80 - 4 * len(reward))],
                'Reward_Num': ['001' * len(reward) + '0' * (60 - 3 * len(reward))],
                'ReqPro': ['1'], 'NPC': [str(PUPU_NPCCODE)]}
        spec.update(fields)
        quests = EC.quests().defs
        quests[quest_id] = EC.QuestDef(quest_id, 0, spec)
        self.addCleanup(quests.pop, quest_id, None)
        return quests[quest_id]

    def turn_in(self, c, q):
        c.send_c2s('0x47734D/0x16', {'quest_id': q.idx})
        c.expect(0x26, 0x59)
        Q.QuestState(self.char()).set_progress(0, 1)
        with self.assertLogs('WS', logging.INFO) as cm:
            c.send_c2s('0x477F3E/0x17', {'quest_id': q.idx})
            c.expect(0x27)                                   # no 0x18 and no 0x58 to the owner
        return cm.output

    def test_a_class_change_reward_is_persisted_without_a_packet(self):
        self.assertEqual(EC.quests().get(8).reward, [(WARRIOR, 1)])   # the real Warrior quest
        q = self.make_quest([WARRIOR])
        c, _, _ = self.enter()
        other, _, _ = self.enter('admin', 'admin', 'Watcher')
        out = self.turn_in(c, q)
        self.assertFalse(any('NOT persisted' in line for line in out))
        self.assertTrue(any('mirrored, the client applied it itself' in line for line in out))
        self.assertEqual(CC.of(self.char()), (1, 0))
        self.assertEqual(dec(other.expect(0x58)), {'uid': 1, 'class': 1, 'class_tier': 0})
        self.assertEqual(INV.Inventory(self.char()).totals(), {})
        c, login, rec = self.relog(c)
        self.assertEqual(self.selected(login)['class_id'], 1)
        self.assertEqual(rec['job'], 1)

    def test_a_refused_class_reward_changes_nothing(self):
        # the client's FUN_004249E0 refuses a tier item for a Novice silently; so does the mirror
        q = self.make_quest([BERSERKER])
        c, _, _ = self.enter()
        out = self.turn_in(c, q)
        self.assertTrue(any('refused by FUN_004249E0' in line for line in out))
        self.assertEqual(CC.of(self.char()), (0, 0))

    def test_skill_and_class_rewards_together_survive_a_relog(self):
        q = self.make_quest([DOUBLE_JUMP, WARRIOR])
        c, _, _ = self.enter()
        out = self.turn_in(c, q)
        self.assertFalse(any('NOT persisted' in line for line in out))
        c, _, rec = self.relog(c)
        self.assertIn(DOUBLE_JUMP, [s['skill_id'] for s in rec['repeat[skill_count]']])
        self.assertEqual((rec['job'], rec['job2']), (1, 0))


@unittest.skipUnless(HAVE_HII, 'needs the EN client hs/windslayer.hii')
class VillageTransfer(Server):
    """world-village-transfer: P3 exit criterion 7 offline (start: Amakusa 501, next to Garan
    Maria)."""

    def setUp(self):
        super().setUp()
        self.place(501, 2332.0, 565.0)

    def transfer(self, c, index, fee):
        c.send_c2s('0x44ADE3/0x5D', {'village_index': index, 'fee': fee})

    def arrive(self, c, town):
        pkts = c.expect(0x81, 0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(len(EC.map_spawns(town))))
        self.assertEqual(dec(pkts[1], 6)['map_code'], town)
        return dec(pkts[0], 9), dec(pkts[3])['repeat[player_count]'][0]

    def refused(self, c, gold_resync=None):
        pkts = c.expect(0x81) if gold_resync is None else c.expect(0x81, 0x3F)
        first = pkts if gold_resync is None else pkts[0]
        self.assertEqual(dec(first, 1), {'result': 0})
        if gold_resync is not None:
            self.assertEqual(dec(pkts[1], 8), {'gold': gold_resync})

    def test_a_transfer_charges_the_fee_once_and_lands_in_the_town(self):
        c, _, _ = self.enter()
        self.transfer(c, 2, 2840)                                  # Amakusa -> Popola
        result, rec = self.arrive(c, 201)
        self.assertEqual(result, {'result': 1, 'gold': GOLD - 2840})
        self.assertEqual(self.gold(), GOLD - 2840)
        self.assertEqual(c.session['current_map'], 201)
        self.assertEqual(c.session['pos'], (50.0, 1612.0))       # 201 has no Garan Maria
        self.assertEqual((rec['pos_x'], self.char()['map']), (50.0, 201))
        # relog: still in Popola with the fee paid exactly once
        c, _, _ = self.relog(c)
        self.assertEqual((c.session['current_map'], self.gold()), (201, GOLD - 2840))

    def test_a_transfer_to_a_garan_maria_town_lands_next_to_her(self):
        self.place(701, 3935.0, 360.0)                              # Balderan
        c, _, _ = self.enter()
        self.transfer(c, 5, 1000)                                  # -> Amakusa
        result, _ = self.arrive(c, 501)
        self.assertEqual(result['gold'], GOLD - 1000)
        self.assertEqual(c.session['pos'], (2332.0, 565.0))

    def test_manner_500_pays_ninety_percent(self):
        self.server.store.adjust_manner('test', 500)
        c, _, _ = self.enter()
        self.transfer(c, 2, 2556)
        result, _ = self.arrive(c, 201)
        self.assertEqual(result['gold'], GOLD - 2556)

    def test_the_server_fee_is_charged_whatever_the_client_says(self):
        c, _, _ = self.enter()
        with self.assertLogs('WS', logging.WARNING) as cm:
            self.transfer(c, 2, 1)
            result, _ = self.arrive(c, 201)
        self.assertEqual(result['gold'], GOLD - 2840)
        self.assertTrue(any('client fee 1 != server fee 2840' in line for line in cm.output))

    def test_insufficient_gold_is_the_failure_box_and_nothing_moves(self):
        with self.server.store.lock:
            INV.Wallet(self.char()).gold = 2839
        c, _, _ = self.enter()
        self.transfer(c, 2, 2840)
        self.refused(c, gold_resync=2839)                         # label back to the store's
        self.assertEqual((c.session['current_map'], self.gold()), (501, 2839))
        self.assertTrue(c.session['in_world'])

    def test_no_fee_or_a_bad_index_is_refused(self):
        c, _, _ = self.enter()
        for index in (5, 0, 11, 255):                             # 5 = already in Amakusa
            with self.subTest(index=index):
                self.transfer(c, index, 1000)
                self.refused(c)
        self.assertEqual((c.session['current_map'], self.gold()), (501, GOLD))

    def test_the_cooldown_refuses_a_second_transfer(self):
        c, _, _ = self.enter()
        self.transfer(c, 2, 2840)
        self.arrive(c, 201)
        self.transfer(c, 5, 2840)                                 # Popola -> Amakusa at once
        self.refused(c)
        self.assertEqual(self.gold(), GOLD - 2840)
        c.session['last_village_t'] -= self.server.config.VILLAGE_COOLDOWN_SECS
        self.transfer(c, 5, 2840)
        result, _ = self.arrive(c, 501)
        self.assertEqual(result['gold'], GOLD - 2 * 2840)

    def test_dead_is_refused(self):
        c, _, _ = self.enter()
        c.session['dead'] = True
        self.transfer(c, 2, 2840)
        self.refused(c)
        self.assertEqual(self.gold(), GOLD)

    def test_a_failed_map_load_refunds_the_fee(self):
        c, _, _ = self.enter()
        self.server._map_transfer = lambda *a, **k: False
        self.transfer(c, 2, 2840)
        paid, refund = c.expect(0x81, 0x3F)
        self.assertEqual(dec(paid)['gold'], GOLD - 2840)
        self.assertEqual(dec(refund)['gold'], GOLD)
        self.assertEqual((self.gold(), c.session['current_map']), (GOLD, 501))
        # a failed attempt does not hold the cooldown: the retry goes through
        del self.server._map_transfer
        self.transfer(c, 2, 2840)
        self.assertEqual(self.arrive(c, 201)[0]['gold'], GOLD - 2840)

    def test_a_field_map_of_the_group_pays_the_group_fee(self):
        self.place(102, 1405.0, 714.0)                            # a 101-group field
        c, _, _ = self.enter()
        self.transfer(c, 2, 1000)
        result, _ = self.arrive(c, 201)
        self.assertEqual(result['gold'], GOLD - 1000)


@unittest.skipUnless(HAVE_HII, 'needs the EN client hs/windslayer.hii')
class VillageConfig(Server):
    config = cfgmod.from_dict({'VILLAGE_ARRIVALS': {'201': [700, 1500]}, 'VILLAGE_COOLDOWN_SECS': 0.0})

    def test_config_arrival_and_no_cooldown(self):
        self.place(501, 2332.0, 565.0)
        c, _, _ = self.enter()
        for index, town, fee in ((2, 201, 2840), (5, 501, 2840)):
            c.send_c2s('0x44ADE3/0x5D', {'village_index': index, 'fee': fee})
            c.expect(0x81, 0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(len(EC.map_spawns(town))))
            if town == 201:
                self.assertEqual(c.session['pos'], (700.0, 1500.0))
        self.assertEqual(self.gold(), GOLD - 2 * 2840)

    def test_bad_config_rows_are_refused_at_load(self):
        for bad in ({'VILLAGE_ARRIVALS': {'201': [1]}}, {'VILLAGE_ARRIVALS': {'x': [1, 2]}},
                    {'VILLAGE_COOLDOWN_SECS': -1.0}):
            with self.assertRaises(cfgmod.ConfigError):
                cfgmod.from_dict(bad)


if __name__ == '__main__':
    unittest.main()
