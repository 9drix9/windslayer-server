#!/usr/bin/env python3
"""
test_cards.py - the Card Deck, the card EXP bonus, S2C 0x99, the flea-market warp and the
stall entry, both client builds (P4 stage 4 cards-stall-entry: quest_cards_misc-card-register,
-card-exp-bonus, -system-notice-0x99, shop_storage-flea-warp, shop_storage-stall-entry;
roadmap P4 exit criteria 6 and 7)

Three layers:
- the content both builds share (64 registrable Monster Cards, card 2030 = npccode 1) and
  cards.py on a bare character record (every 0x8B result in the F10 order, the bag/deck
  change, the bonus rounding);
- the server through fakeclient for BOTH builds (no port, no game client, temp
  accounts.json): C2S 0x64 -> exactly one S2C 0x8B, the deck in the 0x03 count and the 0x8A
  list after a portal and a relog, a reported kill's 0x21 with and without the card, the
  0x99 sub 8 channel line on the first 0x63, 0x99 sub 9 grants (`!grant`, 2009 `/additem`)
  that leave the wallet alone;
- `/warp 9701` to the market's arrival point, the Open Stall cast (0x25), Start -> the
  interim 0x82 {2}, Close -> 0x84 {1}, and a portal while selling: 0x84 {1} BEFORE the
  map-load lead (shop_storage F14.1).
"""
import hashlib
import json
import logging
import os
import shutil
import struct
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import cards  # noqa: E402
import config as cfgmod  # noqa: E402
import en_content as EC  # noqa: E402
import fakeclient as F  # noqa: E402
import inventory as INV  # noqa: E402
import packets as P  # noqa: E402
import progression  # noqa: E402
import quests as Q  # noqa: E402
import registry  # noqa: E402
import skills as SK  # noqa: E402
import world as worldmod  # noqa: E402

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

# EN content both builds share
PUPU_CARD, BLUE_PUPU_CARD = 2030, 2031       # Monster Card <Pupu> / 2009 <Ssiyo>, npccode 1 / 2
HERB, STICK, OPEN_STALL = 5, 179, 194
TOWN, PUPU_MAP, FLEA = 101, 102, 9701
FLEA_ARRIVAL = (1500.0, 2168.0)              # 201_178 etc. -> 9701: the 9701_76 exit-portal line
# where the map load puts him: FLEA_ARRIVAL is 100 px above that line, settled onto it (livetest bug 5)
FLEA_FLOOR = (1500.0, 2268.0)

KEYS = {
    B8: {'login': '0x44D8BF/0x01', 'enter': '0x42F904/0x2B', 'move': '0x42CE94/0x0D',
         'portal': '0x42F76B/0x7E', 'chat': '0x445CA7/0x03', 'cards': '0x44EF67/0x63',
         'register': '0x45A4F8/0x64', 'cast': '0x44C239/0x15', 'stall_open': '0x46B635/0x5E',
         'stall_close': '0x469BA8/0x60'},
    B9: {'login': '0x451CE5/0x01', 'enter': '0x4315D7/0x2B', 'move': '0x42E704/0x0D',
         'portal': '0x431284/0x7E', 'chat': '0x44790E/0x03', 'cards': '0x453557/0x63',
         'register': '0x45FB50/0x64', 'cast': '0x44FC61/0x15', 'stall_open': '0x475A25/0x5E',
         'stall_close': '0x473E14/0x60', 'additem': '0x44691E/0x06'},
}
# The 0x03 byte the deck count lands in (scene+0xE78; 2009 scene+0xE90 = charinfo+0xE48).
DECK_COUNT_FIELD = {B8: 'unk_e78', B9: 'unk_e90'}
LV10 = progression.exp_for_level(10)

ACCOUNTS = {
    'test': {'password': 'test', 'characters': [
        {'name': 'TestHero', 'level': 10, 'exp': LV10, 'class': 0, 'map': TOWN, 'x': 1411, 'y': 714,
         'hp': 100, 'mp': 50, 'gm': 1}]},
    'hunter': {'password': 'hunter', 'characters': [
        {'name': 'Hunter', 'level': 1, 'class': 0, 'map': PUPU_MAP, 'x': 1200, 'y': 714,
         'hp': 100, 'mp': 50}]},
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
            'accounts.json changed during test_cards.py (tests must only use temp copies)'


def use_build(build):
    EC.configure(cfgmod.DEFAULTS['CLIENT_DIR_2009'] if build == B9 else cfgmod.DEFAULTS['CLIENT_DIR'], build)


def bare_char(*items):
    char = {'name': 'Bare', 'exp': 0}
    INV.ensure(char)
    Q.ensure(char)
    for item in items:
        INV.Inventory(char).add(item)
    return char


# ================================================================= content ===
class ContentChecks:
    build = B8

    def setUp(self):
        use_build(self.build)

    def tearDown(self):
        use_build(B8)

    def test_the_registrable_monster_cards(self):
        cat = EC.cards()
        self.assertEqual(len(cat), 64)                        # hii Type 2 with CardSpr != 0
        self.assertEqual((cat.get(PUPU_CARD).card_npc, cat.get(BLUE_PUPU_CARD).card_npc), (1, 2))
        self.assertEqual(cat.for_npc(1), [PUPU_CARD])
        self.assertIsNone(cat.get(HERB))
        # the npccode is the hni template of the map-102 beginner mob (Pupu / 2009 Ssiyo)
        self.assertEqual({s.npc for s in EC.map_spawns(PUPU_MAP)}, {1})
        self.assertEqual(INV.Inventory(bare_char()).tab_of(PUPU_CARD), 'etc')

    def test_the_card_packets_are_the_2008_bytes(self):
        # spec_2009 0x8A / 0x8B / 0x99: "wire identical" (live quest_cards_misc#13-#15 bytes)
        b = self.build
        self.assertEqual(P.build('0x8B', {'result': 1, 'new_deck_count': 1, 'card_item_id': PUPU_CARD},
                                 client_build=b), bytes.fromhex('0101ee07'))
        self.assertEqual(P.build('0x8B', {'result': 6}, client_build=b), b'\x06')
        self.assertEqual(P.build('0x8A', {'deck_count': 2, 'repeat[deck_count]': [
            {'card_item_id': PUPU_CARD}, {'card_item_id': BLUE_PUPU_CARD}]}, client_build=b),
            bytes.fromhex('02ee07ef07'))
        self.assertEqual(P.build('0x99', {'sub_type': 8, 'channel_no': 1}, client_build=b), b'\x08\x01')
        self.assertEqual(P.build('0x99', {'sub_type': 9, 'item_id': STICK, 'count': 1}, client_build=b),
                         bytes.fromhex('09b3000100'))
        self.assertEqual(P.parse(0x64, b'\xee\x07', direction='C2S', client_build=b)['card_item_id'], PUPU_CARD)


@needs_2008
class Content2008(ContentChecks, unittest.TestCase):
    build = B8


@needs_2009
class Content2009(ContentChecks, unittest.TestCase):
    build = B9


@needs_2008
class RegisterModel(unittest.TestCase):
    """cards.register / bonus_exp on a bare record (quest_cards_misc.md F10 / F11)."""

    def setUp(self):
        use_build(B8)

    def test_success_moves_one_card_from_the_bag_into_the_deck(self):
        char = bare_char(PUPU_CARD, PUPU_CARD)
        out = cards.register(char, PUPU_CARD)
        self.assertEqual((out.result, out.deck_count), (cards.RESULT_REGISTERED, 1))
        self.assertEqual(INV.Inventory(char).count(PUPU_CARD), 1)
        self.assertEqual(char['card_deck'], [PUPU_CARD])
        self.assertEqual(cards.result_fields(out), {'result': 1, 'new_deck_count': 1, 'card_item_id': PUPU_CARD})
        out = cards.register(char, BLUE_PUPU_CARD)                  # not in the bag
        self.assertEqual(out.result, cards.RESULT_FAILED)
        INV.Inventory(char).add(BLUE_PUPU_CARD)
        out = cards.register(char, BLUE_PUPU_CARD)
        self.assertEqual((out.result, out.deck_count), (1, 2))       # new_deck_count = old + 1
        self.assertEqual(char['card_deck'], [PUPU_CARD, BLUE_PUPU_CARD])

    def test_refusals_follow_the_F10_order_and_change_nothing(self):
        self.assertEqual(cards.register(None, PUPU_CARD).result, cards.RESULT_NO_DECK)
        char = bare_char(PUPU_CARD, HERB)
        self.assertEqual(cards.register(char, HERB).result, cards.RESULT_FAILED)       # no card
        self.assertEqual(cards.register(char, 0).result, cards.RESULT_FAILED)
        self.assertEqual(cards.register(char, PUPU_CARD).result, 1)
        INV.Inventory(char).add(PUPU_CARD)
        dup = cards.register(char, PUPU_CARD)
        self.assertEqual((dup.result, dup.deck_count), (cards.RESULT_DUPLICATE, 1))
        self.assertEqual(INV.Inventory(char).count(PUPU_CARD), 1)                     # kept
        # 51 = the slots before the manner i32 at charinfo+0xE98: the 52nd is refused
        others = [c for c in sorted(EC.cards().defs) if c != BLUE_PUPU_CARD][:Q.MAX_DECK]
        char['card_deck'][:] = others
        INV.Inventory(char).add(BLUE_PUPU_CARD)
        full = cards.register(char, BLUE_PUPU_CARD)
        self.assertEqual((full.result, full.deck_count), (cards.RESULT_FAILED, Q.MAX_DECK))
        self.assertIn('full', full.why)
        self.assertEqual(INV.Inventory(char).count(BLUE_PUPU_CARD), 1)
        self.assertEqual(cards.result_fields(full), {'result': 3})
        self.assertEqual(P.build('0x8B', cards.result_fields(full)), b'\x03')

    def test_the_exp_bonus_is_ten_percent_and_at_least_one(self):
        char = bare_char()
        self.assertEqual(cards.bonus_exp(char, 1, 10), (10, None))                     # no card yet
        char['card_deck'].append(PUPU_CARD)
        self.assertEqual(cards.bonus_exp(char, 1, 10), (11, PUPU_CARD))                 # 2009 Ssiyo
        self.assertEqual(cards.bonus_exp(char, 1, 5), (6, PUPU_CARD))                   # 2008 Pupu
        self.assertEqual(cards.bonus_exp(char, 1, 17), (18, PUPU_CARD))
        self.assertEqual(cards.bonus_exp(char, 1, 250), (275, PUPU_CARD))
        self.assertEqual(cards.bonus_exp(char, 1, 0), (0, None))
        self.assertEqual(cards.bonus_exp(char, 2, 10), (10, None))                      # other monster
        self.assertEqual(cards.bonus_exp(None, 1, 10), (10, None))


class Config(unittest.TestCase):
    def test_the_new_switches_default_on(self):
        d = cfgmod.defaults()
        self.assertEqual((d.CARD_EXP_BONUS, d.CHANNEL_NOTICE), (True, True))
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.from_dict({'CARD_EXP_BONUS': 1})
        with open(os.path.join(HERE, 'config.json'), encoding='utf-8') as f:
            shipped = json.load(f)
        self.assertEqual((shipped['CARD_EXP_BONUS'], shipped['CHANNEL_NOTICE']), (True, True))

    def test_routes_and_hooks(self):
        for routes in (W.GameServer.ROUTES, W.GameServer.ROUTES_2009):
            self.assertEqual(routes[0x64].handler, '_handle_card_register')
            self.assertEqual(routes[0x63].handler, '_handle_card_deck_list')
        self.assertIn(0x64, registry.MUST_REPLY)                       # the exception backstop
        self.assertEqual(W.gm.subcommands(B9)[0x09].name, 'additem')


# ============================================================== server rig ===
class Rig(unittest.TestCase):
    build = B8

    def setUp(self):
        use_build(self.build)
        self.tmp = tempfile.mkdtemp(prefix=f'ws_cards{self.build}_')
        cfg = {'MOB_SERVER_CONTROLLED': True, 'CLIENT_BUILD': self.build}
        self.server = F.make_server(self.tmp, accounts=json.loads(json.dumps(ACCOUNTS)),
                                    config=cfgmod.from_dict(cfg))
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
        c = F.FakeClient(self.server)
        self.clients.append(c)
        if self.build == B9:
            c.send_c2s(self.keys['login'], F.sso_login(user, user))
        else:
            c.send_c2s(self.keys['login'], {'account_id': user, 'password': user})
        self.assertEqual(c.s2c(c.expect(0x02))['result'], 1)
        c.send_c2s(self.keys['enter'], {'p2p_ip': '127.0.0.1', 'p2p_udp_port': 42907,
                                        'char_name': self.char(user)['name']})
        ops = [0x03, 0x07, 0x15] + ([0x65] if self.build == B9 else []) + [0x28, 0x44]
        pkts = c.expect(*ops, *F.mob_packets(mobs))
        self.assertTrue(c.wait_session(lambda s: s.get('in_world')))
        return c, c.s2c(pkts[0], allow_trailing=True)

    def relog(self, c, user='test', mobs=0):
        c.close()
        c.thread.join(timeout=5.0)
        return self.enter(user, mobs)

    def give(self, c, item, qty=1):
        self.assertIsNotNone(self.server._inv_add(c.session, item, qty, 'test'))

    def chat(self, c, line):
        c.send_c2s(self.keys['chat'], {'msg_len': len(line), 'message': line})

    @staticmethod
    def text(c, pkt):
        text = c.s2c(pkt)['text']
        return text.decode('latin-1') if isinstance(text, bytes) else text

    def deck_list(self, c, first=False):
        """The client's own C2S 0x63 after a map load -> the S2C 0x8A ids (the connection's
        first 0x63 also carries the 0x99 sub 8 channel line)."""
        c.send_c2s(self.keys['cards'])
        if first:
            deck, channel = c.expect(0x8A, 0x99)
            self.assertEqual(c.s2c(channel), {'sub_type': 8, 'channel_no': 1})
        else:
            deck = c.expect(0x8A)
        rec = c.s2c(deck)
        ids = [r['card_item_id'] for r in rec.get('repeat[deck_count]', [])]
        self.assertEqual(rec['deck_count'], len(ids))
        return ids

    def register(self, c, card):
        c.send_c2s(self.keys['register'], {'card_item_id': card})
        pkt = c.expect(0x8B)
        rec = c.s2c(pkt)
        self.assertEqual(len(pkt.payload), 4 if rec['result'] == 1 else 1)
        return rec

    def portal_index(self, src, dst):
        return next(int(k.split('_')[1]) for k, v in sorted(EC.portals().items())
                    if k.startswith(f'{src}_') and v[0] == dst)

    def portal(self, c, src, dst, mobs=0, lead=()):
        c.send_c2s(self.keys['portal'], {'portal_line_index': self.portal_index(src, dst)})
        pkts = c.expect(*lead, 0x08, 0x03, 0x07, 0x28, 0x44, *F.mob_packets(mobs))
        self.assertTrue(c.wait_session(lambda s: s.get('in_world') and s.get('current_map') == dst))
        return pkts

    def report_hit(self, c, mob):
        """The 61 B C2S 0x0D hit report (interact event 7, the victim in the tail) - the
        retail kill path, identical in both builds (spec_2009 0x42E704/0x0D)."""
        c.send_c2s(self.keys['move'], {
            'realtime_delta_ms': 0, 'map_code': PUPU_MAP, 'logic_elapsed_ms': 30,
            'state_lo': W.HIT_REPORT_EVENT << 16, 'state_hi': 0, 'target_uid': mob.uid,
            'pos_x': mob.x, 'pos_y': mob.y, 'target_dx': 0.0, 'target_dy': 0.0,
            'flag_8db': False, 'flag_8e7': False, 'timer_dac': 0, 'target_action_event': 7})

    def kill_exp(self, c, index):
        """Kill monster `index` with one reported hit and no drop; the 0x21 exp delta."""
        mob = c.session['monsters'][W.MOB_UID_BASE + index]
        mob.hp, mob.drop_items = 1, []
        self.report_hit(c, mob)
        pkts = c.expect(0x29, 0x21, 0x18)
        self.assertFalse(mob.alive)
        return struct.unpack_from('<i', pkts[1].payload)[0]


class CardFlow:
    """P4 exit criterion 6, per build."""

    def test_a_registered_card_is_listed_after_portal_and_relog(self):
        c, rec03 = self.enter()
        self.assertEqual(rec03[DECK_COUNT_FIELD[self.build]], 0)
        self.assertEqual(self.deck_list(c, first=True), [])
        self.give(c, PUPU_CARD)
        self.assertEqual(self.register(c, PUPU_CARD),
                         {'result': 1, 'new_deck_count': 1, 'card_item_id': PUPU_CARD})
        c.expect_silence(0.2)                  # no 0x23/0x19 (the client removes its card) and no 0x8A
        self.assertEqual(self.bag().count(PUPU_CARD), 0)
        self.assertEqual(self.char()['card_deck'], [PUPU_CARD])
        disk = self.disk_char()
        self.assertEqual(disk['card_deck'], [PUPU_CARD])
        self.assertNotIn(str(PUPU_CARD), disk['inventory']['etc'])
        # portal: the 0x03 carries the count, the client's 0x63 gets the list (no second 0x99)
        pkts = self.portal(c, TOWN, PUPU_MAP, mobs=8)
        self.assertEqual(c.s2c(pkts[1], allow_trailing=True)[DECK_COUNT_FIELD[self.build]], 1)
        self.assertEqual(self.deck_list(c), [PUPU_CARD])
        # relog: the same from the record (and a new connection prints the channel again)
        c, rec03 = self.relog(c, mobs=8)
        self.assertEqual(rec03[DECK_COUNT_FIELD[self.build]], 1)
        self.assertEqual(self.deck_list(c, first=True), [PUPU_CARD])
        self.give(c, BLUE_PUPU_CARD)
        self.assertEqual(self.register(c, BLUE_PUPU_CARD),
                         {'result': 1, 'new_deck_count': 2, 'card_item_id': BLUE_PUPU_CARD})
        self.assertEqual(self.deck_list(c), [PUPU_CARD, BLUE_PUPU_CARD])

    def test_every_register_request_gets_exactly_one_0x8B(self):
        c, _ = self.enter()
        self.assertEqual(self.register(c, PUPU_CARD), {'result': 3})             # not in the bag
        self.give(c, HERB)
        self.assertEqual(self.register(c, HERB), {'result': 3})                  # not a card
        self.assertEqual(self.bag().count(HERB), 1)
        self.give(c, PUPU_CARD, 2)
        self.assertEqual(self.register(c, PUPU_CARD)['result'], 1)
        self.assertEqual(self.register(c, PUPU_CARD), {'result': 6})             # "Already registered."
        self.assertEqual(self.bag().count(PUPU_CARD), 1)
        with self.server.store.lock:
            self.char()['card_deck'][:] = [cid for cid in sorted(EC.cards().defs)
                                           if cid != BLUE_PUPU_CARD][:Q.MAX_DECK]
        self.give(c, BLUE_PUPU_CARD)
        self.assertEqual(self.register(c, BLUE_PUPU_CARD), {'result': 3})        # 51: full
        self.assertEqual(self.bag().count(BLUE_PUPU_CARD), 1)
        self.assertEqual(len(self.char()['card_deck']), Q.MAX_DECK)
        c.expect_silence(0.2)
        self.assertTrue(c.session['in_world'])

    def test_a_raising_handler_still_closes_the_waiting_box(self):
        c, _ = self.enter()
        self.give(c, PUPU_CARD)
        with mock.patch.object(W.cardsmod, 'register', side_effect=RuntimeError('card bug')), \
                self.assertLogs('WS', logging.ERROR):
            c.send_c2s(self.keys['register'], {'card_item_id': PUPU_CARD})
            self.assertEqual(c.s2c(c.expect(0x8B)), {'result': 3})
        self.assertEqual(self.bag().count(PUPU_CARD), 1)                         # nothing moved

    def test_killing_a_registered_monster_gives_10_percent_more_exp(self):
        c, _ = self.enter('hunter', mobs=8)
        base = EC.npcs().get(1).exp                                               # 2008 Pupu 5, 2009 Ssiyo 10
        self.assertEqual(self.kill_exp(c, 0), base)
        self.give(c, PUPU_CARD)
        c.send_c2s(self.keys['register'], {'card_item_id': PUPU_CARD})
        self.assertEqual(c.s2c(c.expect(0x8B))['result'], 1)
        exp0 = self.char('hunter')['exp']
        self.assertEqual(self.kill_exp(c, 1), base + max(1, base // 10))           # 6 / 11
        self.assertEqual(self.char('hunter')['exp'], exp0 + base + max(1, base // 10))
        self.server.config['CARD_EXP_BONUS'] = False
        self.assertEqual(self.kill_exp(c, 2), base)

    def test_the_deck_dev_command_lists_and_clears(self):
        c, _ = self.enter()
        self.chat(c, b'!deck')
        self.assertIn('empty', self.text(c, c.expect(0x15)))
        self.give(c, PUPU_CARD)
        self.register(c, PUPU_CARD)
        self.chat(c, b'!deck')
        self.assertIn(f'1/{Q.MAX_DECK}', self.text(c, c.expect(0x15)))
        self.chat(c, b'!deck clear')
        deck, reply = c.expect(0x8A, 0x15)                                        # the client's list too
        self.assertEqual((deck.payload, c.s2c(deck)['deck_count']), (bytes(1), 0))
        self.assertEqual(self.char()['card_deck'], [])
        self.assertEqual(self.disk_char()['card_deck'], [])

    def test_the_channel_notice_follows_the_config(self):
        self.server.config['CHANNELS'] = [{'no': 3, 'name': 'Channel 3'}]
        c, rec03 = self.enter()
        self.assertEqual(rec03['channel_id'], 3)                                  # the same number
        c.send_c2s(self.keys['cards'])
        self.assertEqual(c.s2c(c.expect(0x8A, 0x99)[1]), {'sub_type': 8, 'channel_no': 3})
        self.server.config['CHANNEL_NOTICE'] = False
        c, _ = self.relog(c)
        self.assertEqual(self.deck_list(c), [])                                   # no 0x99

    def test_grants_use_0x99_sub_9_and_leave_the_wallet_alone(self):
        c, _ = self.enter()
        gold = self.char()['gold']
        self.chat(c, b'!grant 2030')
        grant, reply = c.expect(0x99, 0x15)
        self.assertEqual(c.s2c(grant), {'sub_type': 9, 'item_id': PUPU_CARD, 'count': 1})
        self.assertIn('Granted 1 x', self.text(c, reply))
        self.chat(c, b'!grant 5 1500')                                            # 999 + 501
        pkts = c.expect(0x99, 0x99, 0x15)
        self.assertEqual([c.s2c(p)['count'] for p in pkts[:2]], [999, 501])
        self.chat(c, b'!grant 179 2')                                             # equipment: 1 per packet
        pkts = c.expect(0x99, 0x99, 0x15)
        self.assertEqual([c.s2c(p)['count'] for p in pkts[:2]], [1, 1])
        bag = self.bag()
        self.assertEqual((bag.count(PUPU_CARD), bag.count(HERB), bag.count(STICK)), (1, 1500, 2))
        self.assertEqual(self.char()['gold'], gold)
        self.chat(c, b'!grant 194')                                               # a skill book: refused
        self.assertIn('Type 3', self.text(c, c.expect(0x15)))
        self.assertNotIn(OPEN_STALL, SK.learned(self.char()))
        bag.set_capacity('etc', 1)                                                # the card fills it
        self.chat(c, b'!grant 2031')
        self.assertIn('full', self.text(c, c.expect(0x15)))
        self.assertEqual(bag.count(BLUE_PUPU_CARD), 0)
        # the granted card registers like a dropped one
        self.assertEqual(self.register(c, PUPU_CARD)['result'], 1)


@needs_2008
class Cards2008(CardFlow, Rig):
    build = B8


@needs_2009
class Cards2009(CardFlow, Rig):
    build = B9

    def test_the_2009_additem_gm_command_grants_through_0x99(self):
        c, _ = self.enter()
        gold = self.char()['gold']
        c.send_c2s(self.keys['additem'], {'gm_subcmd': 9, 'item_id': PUPU_CARD, 'count': 1})
        grant, reply = c.expect(0x99, 0x15)
        self.assertEqual(c.s2c(grant), {'sub_type': 9, 'item_id': PUPU_CARD, 'count': 1})
        self.assertEqual((self.bag().count(PUPU_CARD), self.char()['gold']), (1, gold))
        c.send_c2s(self.keys['additem'], {'gm_subcmd': 9, 'item_id': 0, 'count': 0})   # "/additem x"
        self.assertIn('needs an item id', self.text(c, c.expect(0x15)))


# ======================================================= flea market + stall ===
class StallFlow:
    """P4 exit criterion 7, per build: /warp 9701, Open Stall, Start, Close, portal away."""

    def warp_to_market(self, c):
        self.chat(c, b'/warp 9701')
        pkts = c.expect(0x15, 0x08, 0x03, 0x07, 0x28, 0x44)
        self.assertEqual(self.text(c, pkts[0])[-len('at (1500.0, 2268.0).'):], 'at (1500.0, 2268.0).')
        self.assertEqual(c.s2c(pkts[1])['map_code'], FLEA)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world') and s.get('current_map') == FLEA))
        return pkts

    def cast(self, c, skill):
        c.send_c2s(self.keys['cast'], {'skill_id': skill})

    def start(self, c, result=1):
        c.send_c2s(self.keys['stall_open'], {
            'item_count': 1, 'shop_name': 'Test Stall',
            'repeat[item_count]': [{'item_id': HERB, 'qty': 5, 'unit_price': 100, 'socket_count': 0,
                                    'item_extra': 0}]})
        self.assertEqual(c.s2c(c.expect(0x82)), {'result': result})
        self.assertTrue(c.session['stall_client_selling'])

    def test_slash_warp_lands_on_the_market_arrival_point(self):
        c, _ = self.enter()
        self.warp_to_market(c)
        self.assertEqual(tuple(c.session['pos']), FLEA_FLOOR)
        self.assertEqual((self.char()['map'], self.char()['x'], self.char()['y']), (FLEA,) + FLEA_FLOOR)
        self.chat(c, b'/warp 9701')
        self.assertIn('already on map 9701', self.text(c, c.expect(0x15)))
        # an explicit point still wins, settled onto the floor below it like any arrival
        # (livetest bug 5: 101's floor under x 1300 is y 814); `!warp` and `/warp` are the
        # same command
        self.chat(c, b'!warp 101 1300 700')
        self.assertTrue(self.text(c, c.expect(0x15, 0x08, 0x03, 0x07, 0x28, 0x44)[0]).endswith(
            'at (1300.0, 814.0).'))
        self.assertEqual(tuple(c.session['pos']), (1300.0, 814.0))
        self.assertEqual(self.server._warp_point(TOWN)[:2], (700.0, 812.0))       # START on 101
        self.assertEqual(self.server._warp_point(FLEA)[:2], FLEA_ARRIVAL)

    def test_a_non_gm_slash_warp_is_ordinary_chat(self):
        c, _ = self.enter('hunter', mobs=8)
        self.chat(c, b'/warp 9701')
        self.assertEqual(P.to_bytes(c.s2c(c.expect(0x16))['text']), b'/warp 9701')
        self.assertEqual(c.session['current_map'], PUPU_MAP)

    def test_open_stall_start_close_and_portal_work(self):
        """P4 exit criterion 7 with the real stall of P7 stage 2 (market.py): Start without
        the listed herbs -> 0x82 {9}; with them -> 0x82 {1} and the herbs leave the bag
        model into the escrow; Close -> 0x84 {1} and they are back; Start, then a portal
        while selling -> 0x84 {1} BEFORE the lead and the 0x03 lists the herbs again."""
        with self.server.store.lock:
            self.assertTrue(SK.learn(self.char(), OPEN_STALL, check=False).ok)
        c, _ = self.enter()
        self.warp_to_market(c)
        # cast: 0x25 {194} - 2008 opens window 0x259 on it (asm 0x453D1C); 2009 opened it on
        # the send (FUN_0044f070) and only applies the record's 0 HP/MP deltas
        self.cast(c, OPEN_STALL)
        self.assertEqual(c.s2c(c.expect(0x25)), {'item_or_skill_id': OPEN_STALL})
        self.start(c, result=9)                                  # herbs not owned: tampered list
        c.send_c2s(self.keys['stall_close'])
        self.assertEqual(c.s2c(c.expect(0x84)), {'result': 1})   # a failed Start still closes
        with self.server.store.lock:
            self.assertIsNotNone(self.bag().add(HERB, 5))
        self.start(c)
        self.assertEqual(self.bag().count(HERB), 0)              # escrowed
        self.assertEqual(self.char()['stall_escrow'], [{'id': HERB, 'qty': 5, 'price': 100}])
        c.send_c2s(self.keys['stall_close'])
        self.assertEqual(c.s2c(c.expect(0x84)), {'result': 1})
        self.assertFalse(c.session['stall_client_selling'])
        self.assertEqual((self.bag().count(HERB), self.char()['stall_escrow']), (5, []))
        c.send_c2s(self.keys['stall_close'])                     # the client's automatic re-send
        c.expect_silence(0.2)
        # Start again, then portal away while selling: 0x84 {1} BEFORE the lead (F14.1)
        self.start(c)
        pkts = self.portal(c, FLEA, TOWN, lead=(0x84,))
        self.assertEqual(c.s2c(pkts[0]), {'result': 1})
        self.assertEqual(c.s2c(pkts[1])['map_code'], TOWN)
        bag03 = c.s2c(pkts[2], allow_trailing=True)
        self.assertEqual([(r['item_id'], r['quantity']) for r in bag03['repeat[consume_item_count]']
                          if r['item_id'] == HERB], [(HERB, 5)])   # rebuilt after the escrow return
        self.assertFalse(c.session['stall_client_selling'])
        c.send_c2s(self.keys['stall_close'])                     # the 0x08 path's 0x60, if any
        c.expect_silence(0.2)
        self.assertEqual((self.bag().count(HERB), self.char()['stall_escrow']), (5, []))

    def test_the_market_exit_leads_back_to_the_town_it_was_entered_from(self):
        """world-flea-return. Live 2026-09-25: in from Ozi Village, out on map 101 - the
        market's only exit tile names 101 whatever town the player came from."""
        ozi = 401
        with self.server.store.lock:
            ch = self.char()
            ch['map'], ch['x'], ch['y'] = ozi, 2350.0, 1800.0
        c, _ = self.enter()
        into = self.portal_index(ozi, FLEA)
        c.send_c2s(self.keys['portal'], {'portal_line_index': into})
        c.expect(0x08, 0x03, 0x07, 0x28, 0x44)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world') and s.get('current_map') == FLEA))
        line = EC.portal_line(ozi, into)
        home = ((line[0] + line[2]) / 2, line[1] - W.GameServer.MARKET_RETURN_LIFT)
        self.assertEqual(self.char()['market_return'], [ozi, *home])
        # a relog inside the market keeps the way home (the record holds it)
        c, _ = self.relog(c)
        self.assertEqual(c.session['current_map'], FLEA)
        c.send_c2s(self.keys['portal'], {'portal_line_index': self.portal_index(FLEA, TOWN)})
        pkts = c.expect(0x08, 0x03, 0x07, 0x28, 0x44)
        self.assertEqual(c.s2c(pkts[0])['map_code'], ozi)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world') and s.get('current_map') == ozi))
        # the remembered point, 100 px above the entry portal's line, settled onto that line
        # (livetest bug 5)
        self.assertEqual(tuple(c.session['pos']), (home[0], line[1]))
        # no remembered town (an older save): the map file's own destination, 101
        c.send_c2s(self.keys['portal'], {'portal_line_index': into})
        c.expect(0x08, 0x03, 0x07, 0x28, 0x44)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world') and s.get('current_map') == FLEA))
        with self.server.store.lock:
            self.char().pop('market_return', None)
        c.send_c2s(self.keys['portal'], {'portal_line_index': self.portal_index(FLEA, TOWN)})
        c.expect(0x08, 0x03, 0x07, 0x28, 0x44)
        self.assertTrue(c.wait_session(lambda s: s.get('in_world') and s.get('current_map') == TOWN))

    def test_a_portal_without_a_started_stall_sends_no_0x84(self):
        c, _ = self.enter()
        self.warp_to_market(c)
        self.portal(c, FLEA, TOWN)                               # setup only: closes locally
        self.assertIn(self.server._hook_stall_close,
                      self.server.world.hooks.registered(worldmod.BEFORE_SERVER_MAP_LOAD))

    def test_open_stall_off_the_market_is_still_confirmed(self):
        # The client runs the flea-market gate itself ("Item stall can be opened only in the
        # flea market."), so the server confirms a known skill anywhere (F8: prefer 0x25).
        with self.server.store.lock:
            self.assertTrue(SK.learn(self.char(), OPEN_STALL, check=False).ok)
        c, _ = self.enter()
        self.cast(c, OPEN_STALL)
        self.assertEqual(c.s2c(c.expect(0x25)), {'item_or_skill_id': OPEN_STALL})


@needs_2008
class Stall2008(StallFlow, Rig):
    build = B8

    def test_an_unlearned_open_stall_is_refused_with_0x5F(self):
        c, _ = self.enter()
        self.warp_to_market(c)
        self.cast(c, OPEN_STALL)
        self.assertEqual(c.expect(0x5F).payload, b'')            # clears the scene+0x258 lock


@needs_2009
class Stall2009(StallFlow, Rig):
    build = B9

    def test_an_unlearned_open_stall_gets_no_reply(self):
        # 2009 has no pending-skill lock and no S2C 0x5F (spec_2009 0x25 gates)
        c, _ = self.enter()
        self.warp_to_market(c)
        self.cast(c, OPEN_STALL)
        c.expect_silence(0.2)


if __name__ == '__main__':
    unittest.main()
