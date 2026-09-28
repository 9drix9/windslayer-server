#!/usr/bin/env python3
"""
test_en_content.py - the EN content loader (roadmap F7 arch-content-loader,
item_inventory-en-catalog, quest_cards_misc-en-quest-card-catalog)

The catalogs are read from the real client files next to the server
(WindSlayer2Game/hs/windslayer.hii and .hqi). Every number asserted here is one the
design docs computed independently from the same files, so a decode or parse regression
shows up as a mismatch against the doc, not against itself:

    item_inventory.md 1.1 / 3.1   4248 items, EN Kinds, 474 cash items, 342 Union_Kind 2
    quest_cards_misc.md E1 / 1.2  291 quests, quest 26 in full, 9 talk-only ids,
                                  88 ReqPro quests, 7 Money < 0, 152 repeatable, 45 SNPC 0
    quest_cards_misc.md 3.1       64 monster cards

Nothing here opens accounts.json or binds a port.
"""
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import en_content as EC  # noqa: E402

logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

HAS_CLIENT_FILES = os.path.exists(EC.hs_path('windslayer.hii'))
needs_client = unittest.skipUnless(HAS_CLIENT_FILES,
                                   f'{EC.hs_path("windslayer.hii")} not found (client not installed here)')


def setUpModule():
    # These pin the 2008 tables; a 2009 server built earlier in the same process (one
    # unittest run over every suite) leaves the loader pointed at the 2009 install.
    EC.configure(EC.DEFAULT_CLIENT_DIR, '2008')


def tearDownModule():
    EC.reload()


@needs_client
class Decode(unittest.TestCase):
    """The asset cipher: plain[i] = raw[i] + [0xE9,0xDE,0xE0][i % 3], minus the 20-byte
    SHA-1 footer (reference_asset_formats, quest_cards_misc E1)."""

    def test_headers_decode(self):
        self.assertTrue(EC.decode_hs_text(EC.hs_path('windslayer.hii')).startswith('Number_of_ITEM: 4248'))
        self.assertTrue(EC.decode_hs_text(EC.hs_path('windslayer.hqi')).startswith('Number_of_Quest: 291'))

    def test_footer_is_dropped(self):
        with open(EC.hs_path('windslayer.hqi'), 'rb') as f:
            raw = f.read()
        self.assertEqual(len(EC.decode_hs(EC.hs_path('windslayer.hqi'))), len(raw) - EC.HS_FOOTER_BYTES)

    def test_a_short_file_is_refused(self):
        tmp = tempfile.mkdtemp(prefix='ws_content_')
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, 'short.hii')
        with open(path, 'wb') as f:
            f.write(b'\x00' * 10)
        with self.assertRaises(EC.ContentError):
            EC.decode_hs(path)

    def test_record_splitting_keeps_multi_token_values(self):
        head, fields = EC._split_record('7 Title: 9 Job: 1 0 0 0 0 0 0 Lv: 3', 1)
        self.assertEqual(head, ['7'])
        self.assertEqual(fields['Job'], ['1', '0', '0', '0', '0', '0', '0'])
        self.assertEqual((fields['Title'], fields['Lv']), (['9'], ['3']))


@needs_client
class Items(unittest.TestCase):
    def setUp(self):
        self.items = EC.items()

    def test_table_size_matches_the_hii_header(self):
        self.assertEqual(len(self.items), 4248)
        self.assertEqual(self.items.max_id, EC.EN_ITEM_MAX_ID)

    def test_known_records(self):
        stick = self.items.get(179)                       # Wooden Stick, the quest-26 Send item
        self.assertEqual((stick.type, stick.kind, stick.spr_num, stick.lv), (1, 11, 1, 1))
        self.assertEqual((stick.buy, stick.sell, stick.w_att), (189, 0, 1))
        herb = self.items.get(5)                          # Herb: +20 HP, 4 s cooldown
        self.assertEqual((herb.type, herb.hp, herb.ct, herb.buy, herb.sell), (0, 20, 4000, 33, 7))
        relax = self.items.get(148)                       # Relaxation Herbs: Con = buff duration
        self.assertEqual((relax.type, relax.con, relax.ct), (0, 10000, 5000))
        elem = self.items.get(2975)                       # Chipped Elementirium: 10 x Eledust
        self.assertEqual((elem.type, elem.union_kind, elem.recipe), (2, 1, [(281, 10)]))

    def test_id_gate(self):
        for item in (1, 3, 179, 2030, EC.EN_ITEM_MAX_ID):
            self.assertTrue(self.items.exists(item), item)
        # 0, the KR-only ids above the table (4356 is the drop that never arrived) and
        # anything that is not a number are all refused (S2-11).
        for item in (0, -1, EC.EN_ITEM_MAX_ID + 1, 4356, 0xFFFF, None, 'x', True, False):
            self.assertFalse(self.items.exists(item), item)
        self.assertIsNone(self.items.get('nope'))
        self.assertIsNone(self.items.type_of(4356))
        self.assertEqual(self.items.filter_ids([3, 4356, 5, 9999]), [3, 5])

    def test_tabs_and_kinds(self):
        self.assertEqual(self.items.tab_of(179), 'equip')
        self.assertEqual(self.items.tab_of(5), 'consume')
        self.assertEqual(self.items.tab_of(2975), 'etc')
        self.assertIsNone(self.items.tab_of(94))          # Double Jump: a Type 3 skill book
        self.assertIsNone(self.items.tab_of(4356))
        self.assertEqual(self.items.kind_of(179), 11)
        self.assertIsNone(self.items.kind_of(5))          # consumables have no Kind column
        # The EN Kinds the equip-slot table must cover (item_inventory.md 1.1 plus the
        # cash-only Kinds 1 and 4, which are Type 5 records).
        self.assertEqual(sorted({d.kind for d in self.items.defs.values() if d.kind >= 0}),
                         [0, 1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 15, 16, 17])

    def test_prices_are_u64_and_never_negative(self):
        self.assertEqual(self.items.price(5, 300), 33 * 300)
        self.assertEqual(self.items.price(179, 1, victy=True), 0)
        self.assertEqual(self.items.price(4356, 10), 0)
        self.assertEqual(self.items.price(5, -1), 0)

    def test_doc_statistics(self):
        defs = self.items.defs.values()
        self.assertEqual(sum(1 for d in defs if d.is_cash), 474)
        self.assertEqual(sum(1 for d in defs if d.union_kind == 2), 342)

    def test_names_come_from_the_client_dump(self):
        self.assertEqual(EC.item_name(179), 'Wooden Stick')
        self.assertEqual(EC.item_name(2030), 'Monster Card <Pupu>')
        self.assertEqual(EC.item_name(4356), 'item 4356')          # KR-only: no EN name
        self.assertEqual(EC.item_name('x'), '?')


@needs_client
class Quests(unittest.TestCase):
    def setUp(self):
        self.quests = EC.quests()

    def test_table_size(self):
        self.assertEqual(len(self.quests), 291)

    def test_quest_26_decodes_decimal(self):
        """The starter quest, item for item as quest_cards_misc E1 reports it. The old hex
        parse read Send as 377 (a KR id with no EN definition) and lost the Double Jump
        reward, which is what _EN_ITEM_OVERRIDE was covering up (Q-B1)."""
        q = self.quests.get(26)
        self.assertEqual(q.send, [(179, 1)])                       # Wooden Stick x1
        self.assertEqual(q.reward, [(5, 5), (94, 1)])              # Herb x5 + Double Jump
        self.assertEqual(q.demand, [])
        self.assertEqual((q.exp, q.money), (10, 0))
        self.assertEqual((q.reqpro, q.npc), (1, 1))                # kill 1 Pupu (npccode 1)
        self.assertEqual((q.snpc, q.enpc), (75, 75))               # Murubisiri, start and end
        self.assertEqual((q.start_lev, q.end_lev, q.repeat), (1, 20, 0))
        self.assertTrue(q.needs_slot)                              # ReqPro != 0
        self.assertTrue(q.job_allows(0, 0))                        # 21 x '1'

    def test_talk_only_quests_use_no_slot(self):
        talk_only = sorted(i for i, q in self.quests.defs.items() if not q.needs_slot)
        self.assertEqual(talk_only, [1, 2, 65, 103, 110, 126, 222, 243, 283])

    def test_doc_statistics(self):
        defs = self.quests.defs.values()
        self.assertEqual(sum(1 for q in defs if q.reqpro), 88)
        self.assertEqual(sorted(i for i, q in self.quests.defs.items() if q.money < 0),
                         [163, 168, 177, 187, 193, 207, 217])
        self.assertEqual(sum(1 for q in defs if q.repeat), 152)
        self.assertEqual(sum(1 for q in defs if q.snpc == 0), 45)

    def test_lookup_helpers(self):
        self.assertIn(self.quests.get(26), self.quests.offered_by(75))
        self.assertIn(self.quests.get(26), self.quests.kill_quests(1))
        self.assertFalse(self.quests.exists(9999))
        self.assertIsNone(self.quests.get('x'))

    def test_job_flags_are_indexed_class_plus_7_times_tier(self):
        q = EC.QuestDef(1, 0, {'Job': ['100000010000001000000']})
        self.assertTrue(q.job_allows(0, 0))
        self.assertFalse(q.job_allows(1, 0))
        self.assertTrue(q.job_allows(0, 1))                        # index 7
        self.assertTrue(q.job_allows(0, 2))                        # index 14
        self.assertTrue(EC.QuestDef(2, 0, {}).job_allows(3, 1))    # no flags: everyone


@needs_client
class Cards(unittest.TestCase):
    def test_deck_is_the_64_hii_card_records(self):
        cards = EC.cards()
        self.assertEqual(len(cards), EC.TOTAL_CARDS)
        self.assertTrue(cards.exists(2030))
        self.assertEqual(cards.for_npc(1), [2030])                 # the Pupu card
        self.assertFalse(cards.exists(5))                          # a Herb is not a card
        self.assertEqual(EC.items().get(2030).card_npc, 1)


class GamedefAndPortals(unittest.TestCase):
    """The KR content DB supplies only what the EN files lack (F7), and the portal table is
    read through one loader: the EN table generated from the client maps
    (world-portal-table-en; test_world_content.py covers the generator)."""

    def test_monster_row(self):
        npc = EC.gamedef_npc(1)
        self.assertIsNotNone(npc)
        self.assertEqual((int(npc['Lv']), int(npc['Exp'])), (1, 10))
        self.assertIsNone(EC.gamedef_npc(999999))

    def test_portal_lookup(self):
        target = EC.portal(101, 23)
        self.assertIsNotNone(target)
        self.assertEqual(target[0], 102)
        # 102_17 -> 103 is the live capture the KR table lacked (C17); its 102_26 row is
        # KR-only. An unmapped portal must return None so the handler can do nothing at
        # all: re-loading the same map duplicates the player entity (world-portal-guards).
        self.assertEqual(EC.portal(102, 17)[0], 103)
        self.assertIsNone(EC.portal(102, 26))
        self.assertIsNone(EC.portal(9999, 1))


class FallbackAndReload(unittest.TestCase):
    """A server copied without hs/ still starts: the item catalog falls back to the client
    dump (ids + Type), and the quest catalog comes up empty so quest paths refuse."""

    def setUp(self):
        self.original = EC.client_dir()
        self.addCleanup(EC.configure, self.original)
        self.tmp = tempfile.mkdtemp(prefix='ws_content_')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_fallback_to_the_client_dump(self):
        EC.configure(self.tmp)                      # no hs/ here
        items = EC.items()
        self.assertEqual(items.source, EC.ITEM_CATALOG_JSON)
        self.assertEqual(len(items), 4248)
        self.assertTrue(items.exists(179))
        self.assertFalse(items.exists(4356))
        self.assertEqual(items.type_of(179), 1)
        self.assertEqual(items.tab_of(179), 'equip')
        self.assertIsNone(items.kind_of(179))       # no numeric columns in the dump
        self.assertEqual(len(EC.quests()), 0)
        self.assertIsNone(EC.quests().get(26))

    def test_the_dump_matches_the_hii_types(self):
        """The live memory dump and the packed table agree on every id's Type: two
        independent reads of the same client table."""
        if not HAS_CLIENT_FILES:
            self.skipTest('client files not installed here')
        with open(EC.ITEM_CATALOG_JSON, encoding='utf-8') as f:
            dumped = {int(k): v['type'] for k, v in json.load(f).items()}
        hii = EC.items()
        self.assertEqual({i: hii.type_of(i) for i in dumped}, dumped)

    def test_reload_drops_the_caches(self):
        first = EC.items()
        self.assertIs(EC.items(), first)
        EC.reload()
        self.assertIsNot(EC.items(), first)


if __name__ == '__main__':
    unittest.main()
