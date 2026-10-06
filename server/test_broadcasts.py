#!/usr/bin/env python3
"""
test_broadcasts.py - P5 stage 4 (broadcasts), offline, both client builds
=========================================================================
Two fake clients on one server - TestHero (test/test, uid 1, client 1 =
WindSlayer_patched.exe) and Watcher (admin/admin, uid 2, client 2 = WindSlayer_p2.exe), both
on map 101 - for the 2008 and the 2009 build:

- chat_mail_gm-map-chat-broadcast: C2S 0x03 -> the same S2C 0x16 {sender, text} exactly once
  on EVERY client of the sender's map, the sender included (the client has no local echo);
  nothing to another map, nothing for a GM '!' command or a muted sender; the 5 lines / 3 s
  rate limit drops only the lines past it;
- lc-level-broadcast: a level-up from grant_exp -> S2C 0x22 {uid, level} to the clients
  that hold the player, never the owner (0x21 only); a level-down sends no 0x22 (it would
  play the level-up effect) but re-sends the record to those clients (0x06 + a 0x05 with the
  lower level, livetest bug 8) and the next record carries it; a late joiner's record has the
  level; a shadowed GM's level-up reaches no non-GM client;
- item_inventory-observer-broadcast: equip 0x1D / unequip 0x1E / drop-worn 0x24 with the
  model's block to the clients that hold the player; a dropped item's 0x12 falls at their
  copy of the dropper (source = the dropper); an item buff 0x41, and its expiry's 0x3C/0x43
  on the observer too (no client ends a buff by itself); a late joiner sees the weapon (grid
  + the composed look word 11);
- the record revision that keeps a record built before a change from reaching a client
  after the change's broadcast skipped it (presence.touch / spawn / show_peers_to);
- the player popup 0x50: the record every popup needs (type-3 entity with another uid) and
  its entries' requests (0x20 / 0x27 / 0x29 / 0x30) consumed with the target logged, no
  reply to anyone (0x2A Char. Info is answered since P6 stage 1: test_whisper_info.py);
- the admin port's {"dev": <character>, "cmd": "!level 3"} (`wsdev dev`), which the live
  checks use instead of typing into the 2009 chat box.

No port is bound, no client is started and the live accounts.json is never opened (temp
copies; the module checks its hash at the end).
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

import fakeclient as F  # noqa: E402
import buffs  # noqa: E402
import packets as P  # noqa: E402
import presence as PR  # noqa: E402
import progression  # noqa: E402

W = F.import_server()
cfgmod = W.cfgmod
EC = W.EC
R = W.R
INV = W.invmod
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = cfgmod.BUILD_2008, cfgmod.BUILD_2009
DIRS = {B8: cfgmod.DEFAULTS['CLIENT_DIR'], B9: cfgmod.DEFAULTS['CLIENT_DIR_2009']}
HAVE = {b: os.path.exists(os.path.join(HERE, d, 'hs', 'windslayer.hii')) for b, d in DIRS.items()}

STICK, HERB, RELAX_HERB = 179, 5, 148          # Wooden Stick (Kind 11), Herb, Con 10000 buff
FIRE_5, WATER_5 = 1081, 1086                   # option words the reinforcement writes
PORTAL_101_TO_102 = bytes.fromhex('17000000')  # live 0x7E capture: 101 line 23
PORTAL_102_TO_101 = bytes.fromhex('1f000000')

_LIVE_HASH = None


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def setUpModule():
    global _LIVE_HASH
    _LIVE_HASH = _sha(LIVE_ACCOUNTS) if os.path.exists(LIVE_ACCOUNTS) else None
    P.STRICT_FIELDS.add(B9)                 # every 2009 S2C must use the 2009 field names


def tearDownModule():
    P.STRICT_FIELDS.discard(B9)
    EC.configure(DIRS[B8], B8)              # a 2009 server may have left en_content on 2009
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_broadcasts.py (tests must only use temp copies)'


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _name(value):
    return P.cut_text(value, 16).decode('cp949')


def _text(value):
    return P.to_bytes(value).decode('cp949')


class _Broadcasts:
    """TestHero (uid 1) and Watcher (uid 2) in world on map 101 of one server of `build`."""
    build = B8
    accounts = None

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        self.tmp = tempfile.mkdtemp(prefix=f'ws_broadcasts_{self.build}_')
        self.server = self.make_server(self.accounts)
        self.mc = F.MultiClient(self.server)
        self.a, self.b = self.mc
        self.mc.drain()                     # B's 0x05 on A (test_presence covers it)

    def tearDown(self):
        self.mc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_server(self, accounts=None):
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False})
        accounts = accounts or F.two_player_accounts()
        # a third player for the late-joiner checks (enter_third); MultiClient logs in two
        accounts.setdefault('late', {'password': 'late', 'characters': [
            {'name': 'Late', 'level': 1, 'class': 0, 'map': 101, 'x': 1411.0, 'y': 714.0,
             'hp': 100, 'mp': 50}]})
        return F.make_server(self.tmp, accounts=accounts, config=cfg)

    # ----------------------------------------------------------------- helpers ---
    def key(self, op, index=-1):
        """The build's C2S send-site key of `op` (0x15: index -1 = the consumable site)."""
        return P.variants(op, 'C2S', client_build=self.build)[index]['key']

    def chat(self, c, text):
        text = P.to_bytes(text)
        c.send_c2s(self.key(0x03), {'msg_len': len(text), 'message': text})

    def ops(self, pkts):
        return [p.opcode for p in pkts]

    def char(self, user='test'):
        return self.server._session_char((self.a if user == 'test' else self.b).session)

    def give(self, c, item, count=1, words=None):
        self.assertIsNotNone(self.server._inv_add(c.session, item, count, 'test', words=words))

    def equip(self, c, item, stones=(), extra=0):
        c.send_c2s(self.key(0x0F), {'item_id': item, 'stone_count': len(stones), 'extra_option': extra,
                                    'repeat[stone_count]': [{'stone_id': s} for s in stones]})

    def unequip(self, c, item, stones=(), extra=0):
        c.send_c2s(self.key(0x11), {'item_id': item, 'enchant_count': len(stones), 'enchant_last': extra,
                                    'repeat[enchant_count]': [{'enchant': s} for s in stones]})

    def block_of(self, rec, count_field):
        """(option words, tail) of a decoded 0x1D / 0x1E / 0x24 body."""
        words = [list(e.values())[0] for e in rec[f'repeat[{count_field}]']]
        tail = rec['block_tail'] if 'block_tail' in rec else rec['option_extra']
        return words, tail

    def portal(self, c, portal, dest):
        c.send(0x7E, portal)
        self.assertTrue(_wait(lambda: self.server.world.map_of(c.session) == dest))
        return c.recv_until_quiet()

    def enter_third(self):
        """A third player (Late) entering map 101: its client and its arrival packets."""
        c = F.FakeClient(self.server)
        c.number, c.char_name = 3, 'Late'
        self.mc.clients.append(c)
        self.assertEqual(c.login('late', 'late')['result'], 1)
        entry = c.enter_world('Late', port=F.P2P_PORT_BASE + 2)
        self.mc.drain()
        return c, entry

    def rows_of(self, c, pkts):
        return {row['uid']: row for p in pkts if p.opcode == 0x04 for row in c.s2c(p)['repeat[player_count]']}

    @staticmethod
    def look_of(row):
        """The appearance words of a 0x04 / 0x05 row (2008 0x05 names them 'appearance')."""
        for key, value in row.items():
            if key.startswith('repeat[') and value and isinstance(value[0], dict):
                first = value[0]
                if 'appearance_part' in first or 'appearance' in first:
                    return [e.get('appearance_part', e.get('appearance')) for e in value]
        raise AssertionError('no appearance array in the row')

    @staticmethod
    def grid_of(row):
        """The equip grid item ids of a 0x04 / 0x05 row (regular slots)."""
        for key, value in row.items():
            if key.startswith('repeat[') and value and isinstance(value[0], dict) and 'equip_item_id' in value[0]:
                return [e['equip_item_id'] for e in value]
        raise AssertionError('no equip grid in the row')

    # ------------------------------------------------------------------- chat ---
    def test_a_line_reaches_every_client_on_the_map_exactly_once(self):
        """Exit criterion 3: A types 'hi' -> 'TestHero : hi' once on A (no local echo in the
        client, spec 0x03) and once on B, the same 0x16 bytes."""
        self.chat(self.a, 'hi')
        mine, theirs = self.a.expect(0x16), self.b.expect(0x16)
        self.assertEqual(mine.payload, theirs.payload)
        rec = self.b.s2c(theirs)
        self.assertEqual((_name(rec['sender_name']), _text(rec['text'])), ('TestHero', 'hi'))
        # the other direction, and an emote line (it plays on the sender's entity everywhere)
        self.chat(self.b, '/heart')
        for c in (self.a, self.b):
            rec = c.s2c(c.expect(0x16))
            self.assertEqual((_name(rec['sender_name']), _text(rec['text'])), ('Watcher', '/heart'))

    def test_another_map_hears_nothing_and_hears_again_after_the_return(self):
        self.portal(self.b, PORTAL_101_TO_102, 102)
        self.a.recv_until_quiet()                               # B's 0x06
        self.chat(self.a, 'anyone?')
        self.assertEqual(_text(self.a.s2c(self.a.expect(0x16))['text']), 'anyone?')
        self.b.expect_silence(0.2)
        self.chat(self.b, 'on 102')
        self.b.expect(0x16)
        self.a.expect_silence(0.2)
        self.portal(self.b, PORTAL_102_TO_101, 101)
        self.a.recv_until_quiet()                               # B's 0x05
        self.chat(self.a, 'back')
        self.a.expect(0x16)
        self.assertEqual(_text(self.b.s2c(self.b.expect(0x16))['text']), 'back')

    def test_gm_commands_and_muted_lines_reach_nobody_else(self):
        with self.server.store.lock:
            self.char()['gm'] = 1
        self.a.session['gm'] = 1
        self.chat(self.a, '!who')
        self.assertTrue(all(p.opcode == 0x15 for p in self.a.recv_until_quiet()))
        self.b.expect_silence(0.2)
        with self.server.store.lock:
            self.server.store.accounts['admin']['manner'] = -40
        self.chat(self.b, 'muted')
        self.a.expect_silence(0.2)
        self.b.expect_silence(0.1)

    def test_the_rate_limit_drops_the_sixth_line_in_three_seconds(self):
        for i in range(6):
            self.chat(self.a, f'line {i}')
        for c in (self.a, self.b):
            got = [_text(c.s2c(p)['text']) for p in c.recv_until_quiet(0.3)]
            self.assertEqual(got, [f'line {i}' for i in range(5)], c.char_name)
        # the window slides: once the oldest line is 3 s old, one more gets through
        times = self.a.session['chat_times']
        times[0] -= W.GameServer.CHAT_RATE_SECS
        self.chat(self.a, 'again')
        for c in (self.a, self.b):
            self.assertEqual(_text(c.s2c(c.expect(0x16))['text']), 'again')

    def test_rate_window_unit(self):
        s = {}
        ok = [self.server._chat_rate_ok(s, now=t) for t in (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 2.99, 3.0, 3.1)]
        self.assertEqual(ok, [True] * 5 + [False, False, True, False])

    # ------------------------------------------------------------------ level ---
    def test_a_level_up_is_0x22_for_the_observer_and_only_0x21_for_the_owner(self):
        """Exit criterion 7 (level): A levels up; B's copy of A gets 0x22 {1, 2} - level,
        effect, heal - and A gets its own 0x21 only (a 0x22 would play the effect twice)."""
        self.server.grant_exp(self.a.session, progression.exp_for_level(2))
        self.assertEqual(self.a.s2c(self.a.expect(0x21))['exp_delta'], progression.exp_for_level(2))
        self.assertEqual(self.b.s2c(self.b.expect(0x22)), {'uid': 1, 'level': 2})
        # exp inside the level changes nothing for the observer
        self.server.grant_exp(self.a.session, 1)
        self.a.expect(0x21)
        self.b.expect_silence(0.2)
        # a level-down (only a GM !exp) sends B no 0x22: it plays the level-UP effect and heal
        # on every receipt (spec 0x22 gates_and_hazards). livetest bug 8: B kept level 2, so
        # B's copy of A is replaced instead - 0x06, then a 0x05 whose record says level 1 -
        # and the record is touched, so one built from now on (a late joiner's) says 1 too.
        rev = self.a.session.get('presence_rev')
        self.server.grant_exp(self.a.session, -2)
        self.a.expect(0x21)
        self.a.expect_silence(0.1)                               # nothing else for the owner
        gone, back = self.b.expect(0x06, 0x05)
        self.assertEqual(self.b.s2c(gone), {'uid': 1})
        row = self.b.s2c(back)
        self.assertEqual((row['uid'], row['level']), (1, 1))
        self.b.expect_silence(0.2)                               # and never a 0x22
        self.assertIs(PR.spawned(self.b.session)[1], self.a.session)
        self.assertNotEqual(self.a.session.get('presence_rev'), rev)
        c, entry = self.enter_third()
        self.assertEqual(self.rows_of(c, entry)[1]['level'], 1)
        # only the clients that hold him get the new record: B went to 102, Late stayed
        self.portal(self.b, PORTAL_101_TO_102, 102)
        self.a.recv_until_quiet()
        c.recv_until_quiet()
        self.server.grant_exp(self.a.session, progression.exp_for_level(3) - self.char()['exp'])
        self.a.expect(0x21)
        self.assertEqual(c.s2c(c.expect(0x22)), {'uid': 1, 'level': 3})
        self.server.grant_exp(self.a.session, -1)
        self.a.expect(0x21)
        c.expect(0x06, 0x05)
        self.b.expect_silence(0.2)
        self.assertEqual(PR.reshow_to_holders(self.server, self.b.session), 0)    # nobody holds B
        self.b.expect_silence(0.1)

    def test_the_admin_dev_command_levels_a_character_the_live_check_way(self):
        """`wsdev dev TestHero !level 3` (admin port {"dev", "cmd"}): the command runs as A,
        whose client gets its 0x21 and the 0x15 answer; B sees 0x22 {1, 3}. A character not
        in world is an error, and nothing is sent."""
        reply = self.server._admin_command(json.dumps({'dev': 'TestHero', 'cmd': '!level 3'}))
        self.assertEqual(reply, 'ok dev TestHero: !level 3')
        exp, answer = self.a.expect(0x21, 0x15)
        self.assertEqual(self.a.s2c(exp)['exp_delta'], progression.exp_for_level(3))
        self.assertIn('Level 3', _text(self.a.s2c(answer)['text']))
        self.assertEqual(self.b.s2c(self.b.expect(0x22)), {'uid': 1, 'level': 3})
        self.assertTrue(self.server._admin_command(json.dumps({'dev': 'Late', 'cmd': 'level 2'}))
                        .startswith('error:'))
        self.assertTrue(self.server._admin_command(json.dumps({'dev': 'TestHero', 'cmd': ''}))
                        .startswith('error:'))
        self.a.expect_silence(0.1)
        self.b.expect_silence(0.1)

    def test_a_late_joiner_gets_the_new_level_in_the_record(self):
        self.server.grant_exp(self.a.session, progression.exp_for_level(3))
        self.a.expect(0x21)
        self.b.expect(0x22)
        c, entry = self.enter_third()
        self.assertEqual(self.rows_of(c, entry)[1]['level'], 3)

    def test_a_level_up_on_another_map_or_mid_map_load_reaches_nobody(self):
        self.portal(self.b, PORTAL_101_TO_102, 102)
        self.a.recv_until_quiet()
        self.server.grant_exp(self.a.session, progression.exp_for_level(2))
        self.a.expect(0x21)
        self.b.expect_silence(0.2)
        self.assertEqual(PR.to_holders(self.server, self.a.session, '0x22', {'uid': 1, 'level': 2}), 0)

    # -------------------------------------------------------------- equipment ---
    def test_equip_and_unequip_reach_the_observer_with_the_weapon(self):
        """Exit criterion 7 (weapon): A equips the Wooden Stick -> B gets 0x1D {uid 1, 179} (its
        copy writes the grid and recomposes the sprite); A takes it off -> B gets 0x1E."""
        self.give(self.a, STICK)
        self.equip(self.a, STICK)
        self.a.expect(0x1D)
        rec = self.b.s2c(self.b.expect(0x1D))
        self.assertEqual((rec['uid'], rec['item_id']), (1, STICK))
        self.assertEqual(self.block_of(rec, 'stone_count'), ([], 0))
        self.unequip(self.a, STICK)
        self.a.expect(0x1E)
        rec = self.b.s2c(self.b.expect(0x1E))
        self.assertEqual((rec['uid'], rec['item_id'], self.block_of(rec, 'option_count')), (1, STICK, ([], 0)))

    def test_the_observer_gets_the_model_block_and_the_owner_his_echo(self):
        """A block with a zero gap: the owner's bag holds the bytes he sent (the echo), every
        other copy of the slot holds the stored, packed words (records, observer 0x1D) - so
        the observer 0x1D / 0x1E carry those, and B's later 0x1E matches its own slot."""
        self.give(self.a, STICK, words=[FIRE_5, WATER_5, 0, 0, 0, 7])
        self.equip(self.a, STICK, stones=[FIRE_5, WATER_5], extra=7)
        mine = self.a.s2c(self.a.expect(0x1D))
        theirs = self.b.s2c(self.b.expect(0x1D))
        self.assertEqual(self.block_of(mine, 'stone_count'), ([FIRE_5, WATER_5], 7))
        self.assertEqual(self.block_of(theirs, 'stone_count'), ([FIRE_5, WATER_5], 7))
        self.unequip(self.a, STICK, stones=[FIRE_5, WATER_5], extra=7)
        self.a.expect(0x1E)
        self.assertEqual(self.block_of(self.b.s2c(self.b.expect(0x1E)), 'option_count'), ([FIRE_5, WATER_5], 7))

    def test_a_late_joiner_sees_the_worn_weapon(self):
        self.give(self.a, STICK)
        self.equip(self.a, STICK)
        self.a.expect(0x1D)
        self.b.expect(0x1D)
        c, entry = self.enter_third()
        row = self.rows_of(c, entry)[1]
        self.assertIn(STICK, self.grid_of(row))
        # the composed look (inventory.compose, as A's 0x1D composed it): the stick's Spr_Num
        self.assertEqual(self.char()['look'][INV.LAYER_WEAPON], EC.items().get(STICK).spr_num)
        self.assertEqual(self.look_of(row)[INV.LAYER_WEAPON], EC.items().get(STICK).spr_num)

    def test_dropping_the_worn_weapon_and_a_bag_item_on_the_observer(self):
        """0x24 (grid clear, no bag add) to the observer, then the ground item as 0x12 with
        source = the dropper: it falls at B's own copy of A (spec 0x12 position rule)."""
        self.give(self.a, STICK)
        self.equip(self.a, STICK)
        self.a.expect(0x1D)
        self.b.expect(0x1D)
        self.a.send_c2s(self.key(0x14), {'item_id': STICK, 'opt_count': 0, 'opt6': 0, 'repeat[opt_count]': []})
        self.a.expect(0x24, 0x12)
        removal, shown = self.b.expect(0x24, 0x12)
        self.assertEqual((self.b.s2c(removal)['uid'], self.b.s2c(removal)['item_id']), (1, STICK))
        (row,) = self.b.s2c(shown)['repeat[entry_count]']
        self.assertEqual((row['item_id'], row['source_uid'], row['owner_uid']), (STICK, 1, 0))
        self.give(self.a, HERB, 2)
        self.a.send_c2s(self.key(0x13), {'item_id': HERB, 'amount': 1, 'opt_count': 0, 'opt6': 0,
                                         'repeat[opt_count]': []})
        self.a.expect(0x23, 0x12)
        (row,) = self.b.s2c(self.b.expect(0x12))['repeat[entry_count]']
        self.assertEqual((row['item_id'], row['source_uid']), (HERB, 1))

    def test_an_item_buff_is_0x41_on_the_observer(self):
        """item_inventory F5 step 6: the owner 0x42, the observer 0x41 with the same body; a
        re-use removes the old record on both (identical item buffs stack everywhere)."""
        self.give(self.a, RELAX_HERB, 2)
        self.a.send_c2s(self.key(0x15), {'item_id': RELAX_HERB})
        self.a.expect(0x25, 0x42)
        body = {'target_uid': 1, 'source_uid': 0, 'item_id': RELAX_HERB}
        self.assertEqual(self.b.s2c(self.b.expect(0x41)), body)
        self.a.session[W.GameServer.ITEM_COOLDOWN_KEY].clear()
        self.a.send_c2s(self.key(0x15), {'item_id': RELAX_HERB})
        removal = self.a.expect(0x25, 0x3C, 0x42)[1]
        self.assertEqual(self.b.expect(0x3C, 0x41)[0].payload, removal.payload)

    def test_an_item_buff_expiry_reaches_the_observer_too(self):
        """Spec correction C4: no client ends a buff by itself. When 148's Con runs out, B's
        copy of A gets the same removal bytes as A - without it identical item buffs stack on
        B's copy (item_inventory#11) until its 21-slot buff array is full. The expiry
        touch()es A's record, so one built before it is rebuilt without the slot."""
        self.give(self.a, RELAX_HERB)
        self.a.send_c2s(self.key(0x15), {'item_id': RELAX_HERB})
        self.a.expect(0x25, 0x42)
        self.b.expect(0x41)
        rev = self.a.session.get('presence_rev')
        op = int(buffs.removal_opcode(RELAX_HERB), 16)
        con = buffs.duration_ms(RELAX_HERB) / 1000.0
        self.assertGreater(con, 0)
        self.assertEqual(self.server._tick_buffs(time.monotonic() + con / 2), 0)   # not yet
        self.b.expect_silence(0.1)
        self.assertEqual(self.server._tick_buffs(time.monotonic() + con + 1), 1)
        mine = self.a.expect(op)
        self.assertEqual(self.a.s2c(mine), {'buff_item_id': RELAX_HERB, 'target_uid': 1})
        self.assertEqual(self.b.expect(op).payload, mine.payload)
        self.assertNotEqual(self.a.session.get('presence_rev'), rev)
        self.assertIsNone(buffs.find(self.a.session, RELAX_HERB))

    def test_a_shadowed_gm_changes_reach_no_client_that_cannot_see_him(self):
        """A GM in shadow is not spawned on a non-GM client (presence), so neither his level-up
        effect (0x22 queues it before the entity lookup) nor his gear reaches it."""
        accounts = F.two_player_accounts()
        accounts['test']['characters'][0].update({'gm': 1, 'gm_hidden': 1})
        self.mc.close()
        self.server = self.make_server(accounts)
        with F.MultiClient(self.server) as mc:
            a, b = mc
            mc.drain()
            self.assertEqual(PR.spawned(b.session), {})
            self.server.grant_exp(a.session, progression.exp_for_level(2))
            a.expect(0x21)
            self.assertIsNotNone(self.server._inv_add(a.session, STICK, 1, 'test'))
            a.send_c2s(self.key(0x0F), {'item_id': STICK, 'stone_count': 0, 'extra_option': 0})
            a.expect(0x1D)
            b.expect_silence(0.2)
            self.chat(a, 'hi')                      # chat is by map: the line still arrives
            a.expect(0x16)
            b.expect(0x16)

    # ------------------------------------------------------- record revision ---
    def test_a_record_built_before_a_change_is_rebuilt_before_it_is_sent(self):
        """The race the revision closes: B's record of A is being built (old weapon) while A
        equips; the 0x1D broadcast skips B (it does not hold A yet), so the record B then gets
        must be rebuilt with the stick."""
        self.assertTrue(PR.despawn(self.server, self.a.session, self.b.session))
        self.b.expect(0x06)
        self.give(self.a, STICK)
        real = PR.record_of
        builds = []

        def record_of(server, subject):
            rec = real(server, subject)
            builds.append(rec)
            if len(builds) == 1:                    # A's equip lands mid-build
                self.equip(self.a, STICK)
                self.a.expect(0x1D)
                self.assertTrue(_wait(lambda: self.char()['look'][INV.LAYER_WEAPON]))
            return rec
        with mock.patch.object(PR, 'record_of', record_of):
            self.assertTrue(PR.spawn(self.server, self.a.session, self.b.session))
        self.assertEqual(len(builds), 2)
        rec = self.b.s2c(self.b.expect(0x05))      # no 0x1D before it: B did not hold A
        self.assertIn(STICK, self.grid_of(rec))
        self.assertEqual(self.look_of(rec)[INV.LAYER_WEAPON], EC.items().get(STICK).spr_num)
        # the first build was the stale one: no stick in it
        self.assertNotIn(STICK, self.grid_of(builds[0]))

    def test_show_peers_to_respawns_a_row_that_went_stale(self):
        self.assertTrue(PR.despawn(self.server, self.a.session, self.b.session))
        self.b.expect(0x06)
        real = PR.record_of
        calls = []

        def record_of(server, subject):
            calls.append(subject.get('uid'))
            if len(calls) == 1:
                PR.touch(subject)                   # changed after its row was built
            return real(server, subject)
        with mock.patch.object(PR, 'record_of', record_of):
            self.assertEqual(PR.show_peers_to(self.server, self.b.session), 1)
        self.assertEqual(calls, [1, 1])            # built, found stale, rebuilt by spawn()
        self.assertEqual(self.ops(self.b.recv_until_quiet()), [0x05])
        self.assertEqual(PR.spawned(self.b.session), {1: self.a.session})

    # ---------------------------------------------------------- popup 0x50 ---
    def test_the_popup_needs_only_the_record_and_its_requests_are_consumed(self):
        """Exit criterion 6: the popup opens client-side on a type-3 entity (every 0x04/0x05
        record) whose uid is not the clicker's; the uid its entries send is the one the
        record carried. Every entry has its owner now: 0x2A Char. Info since P6 stage 1
        (0x52 / 0x53, test_whisper_info.py), 0x30 Add as Friend since P6 stage 2 (0x0D on the
        target, test_messenger.py), 0x27 / 0x29 since P6 stage 3 (test_party.py) and 0x20
        Trade since P7 stage 1: the target gets window 0x70 (S2C 0x45 with the requester's
        name), the requester nothing (test_trade.py)."""
        self.assertEqual(PR.spawned(self.a.session), {2: self.b.session})
        with self.assertLogs('WS', logging.INFO) as cm:
            self.a.send_c2s(self.key(0x20, 0), {'target_uid': 2})
            self.assertEqual(self.b.expect(0x45).payload, P.name17('TestHero'))
            self.a.expect_silence(0.15)
        text = '\n'.join(cm.output)
        self.assertIn("'TestHero' (uid 1) requests a trade with 'Watcher' (0x45)", text)
        self.assertNotIn('Unhandled opcode', text)


class Broadcasts2008(_Broadcasts, unittest.TestCase):
    build = B8


class Broadcasts2009(_Broadcasts, unittest.TestCase):
    build = B9


if __name__ == '__main__':
    unittest.main()
