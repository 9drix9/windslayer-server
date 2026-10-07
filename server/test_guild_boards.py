#!/usr/bin/env python3
"""
test_guild_boards.py - P15 guild-g6, the Guild Plaza boards (boards.py; systems_2009/guild.md
1.6, 3, F0, F13; ROADMAP_2009_ADDENDUM P15 + arch09-channel-key), both client builds
=========================================================================================
  Store        Board rows keyed (channel, map, guild), the JSON round trip, a load that drops
               expired / unknown-guild / broken rows, a malformed file moved aside.
  Sale         Moiba's C2S 0x0B (send site 0x474327) with the wire's npc_id 0 (p15 live triage
               1) on the cp-2 exe ('en'): the Guild Billboard EN 4284 at its hii Buy -> 0x18
               {gold, victy, 4284, qty} + the real price line when the dialog named another row
               (Moiba's hni row: stock 4283 or cp-5 4284, whichever is installed); not enough
               gold; npc 0 off the plaza / after a warp 9702 -> 801 (current_map follows the
               departure) / with a non-board or stock-exe id is no sale; npc 181 still sells;
               the cp-5 row (4284) priced exactly as the client's own gold check; the stock exe ('kr'):
               refused "Guild billboards are not available."; 2008: npc 0 is no merchant.
  Placement    C2S 0x88 by the master on 9702 -> 0xBA to everyone on 9702 (not elsewhere) +
               sub 185 {1, 4284}; the bag loses the billboard ONCE (the 0x15 echo gets 0x25, no
               second removal); the premium board (cash 4283) -> 0x72 consume + sub 185 {1,
               4283}; every refusal in order (stock exe, map, not the master = sub 185 {0},
               item, text, double, too close, no item) takes nothing; a commit-time refusal
               gives the item back; a billboard offered in a trade is not taken; an expired but
               unticked board is removed (0xB8) before its replacement's 0xBA.
  Join         a board's C2S 0x89 u16 -> the existing apply path (sub 2 + the master push).
  Re-entry     0xBB after the 0x8A reply in 9702 only, in the bundle order (sub 3 / 15, sub 4,
               0xBB, sub 37); the field order of the 0xBB rows.
  Expiry       the 'guild-boards' tick -> 0xB8 {gid} to 9702; a disband (Break Guild and the
               admin `!guild disband`) drops the board with 0xB8.
  Restart      a new server on the same files shows the live boards again (0xBB), an expired
               one is gone.
  GM           `!guild board place / premium / list / ttl / expire`; the 'kr' exe's ids
               (4280 / 4279) in 0xBA / 0xBB.
  2008         no board module at work: no file, no route, no resync step, no tick.

Fake clients (fakeclient.MultiClient); no port is bound, no client is started, and the live
accounts.json / guilds.json / guild_boards.json are never opened (temp copies; the module
checks all three).
"""
import hashlib
import json
import logging
import math
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

import boards as BD  # noqa: E402
import clientview as CV  # noqa: E402
import fakeclient as F  # noqa: E402
import guild as G  # noqa: E402
import packets as P  # noqa: E402
import progression  # noqa: E402
import resync as RS  # noqa: E402

W = F.import_server()
cfgmod = W.cfgmod
EC = W.EC
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE = [os.path.join(HERE, n) for n in ('accounts.json', 'guilds.json', 'guild_boards.json')]
B8, B9 = cfgmod.BUILD_2008, cfgmod.BUILD_2009
DIRS = {B8: cfgmod.DEFAULTS['CLIENT_DIR'], B9: cfgmod.DEFAULTS['CLIENT_DIR_2009']}
HAVE = {b: os.path.exists(os.path.join(HERE, d, 'hs', 'windslayer.hii')) for b, d in DIRS.items()}

FRIENDS, CARDS, GUILD_INFO = '0x45353B/0x2F', '0x453557/0x63', '0x453581/0x8A'
PLACE, APPLY_BOARD, USE_15 = '0x482C4D/0x88', '0x482DCD/0x89', '0x44FE04/0x15'
MOIBA_BUY, ACTION = '0x474327/0x0B', '0x484C32/0x8E'
MOIBA = 181                                       # hni idx 181, UI 0x4BB (guild.md 1.5)
BOARD, PREMIUM = 4284, 4283                       # EN Guild Billboard (bag) / Premium (cash)
PLAZA = (G.GUILD_PLAZA_MAP, 2200.0, 1300.0)
LV30 = progression.exp_for_level(30)
T0 = 1_790_000_000.0

_LIVE = None


def _sha(path):
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def setUpModule():
    global _LIVE
    _LIVE = [_sha(p) for p in LIVE]
    P.STRICT_FIELDS.add(B9)


def tearDownModule():
    P.STRICT_FIELDS.discard(B9)
    EC.configure(DIRS[B8], B8)
    assert [_sha(p) for p in LIVE] == _LIVE, \
        'accounts.json / guilds.json / guild_boards.json changed during test_guild_boards.py (temp copies only)'


class Wall:
    """The boards' UTC clock, moved by the tests."""

    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def _char(name, exp=0):
    return {'name': name, 'level': 1, 'class': 0, 'exp': exp, 'map': 101, 'x': 1411.0,
            'y': 714.0, 'hp': 100, 'mp': 50}


def _accounts():
    return {'test': {'password': 'test', 'characters': [_char('TestHero', exp=LV30)]},
            'admin': {'password': 'admin', 'characters': [_char('Watcher')]},
            'carol': {'password': 'carol', 'characters': [_char('Carol', exp=LV30)]}}


GOLD = {'TestHero': 60000, 'Watcher': 100, 'Carol': 60000}


def _ops(pkts):
    return [p.opcode for p in pkts]


def _board(**kw):
    row = dict(channel=1, map_code=BD.PLAZA, guild_id=2, kind=BD.KIND_BOARD, master_uid=1, master_name='TestHero',
               guild_name='Testers', emblem_fg=18, emblem_bg=18, ad_text='Join us', pos_x=2200.0, pos_y=1300.0,
               placed_at=T0, expires_at=T0 + 3600, placed_by=4)
    row.update(kw)
    return BD.Board(**row)


# ================================================================== store ===
class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_g6_store_')
        self.path = os.path.join(self.tmp, 'guild_boards.json')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rows_round_trip_by_their_channel_key(self):
        b = _board()
        self.assertEqual(b.key, (1, 9702, 2))                        # arch09-channel-key
        self.assertEqual(BD.Board.from_json(json.loads(json.dumps(b.to_json()))), b)
        st = BD.BoardStore(self.path)
        st.boards[b.key] = b
        st._mark()
        self.assertTrue(st.flush())
        self.assertFalse(st.flush())                                 # nothing new
        data = json.load(open(self.path, encoding='utf-8'))
        self.assertEqual(data['version'], 1)
        self.assertEqual(data['boards'][0]['channel'], 1)
        self.assertEqual((data['boards'][0]['map'], data['boards'][0]['guild_id'], data['boards'][0]['kind']),
                         (9702, 2, 'board'))
        again = BD.BoardStore(self.path)
        self.assertEqual(again.load(T0 + 10, {2}), 1)
        self.assertEqual(again.boards[(1, 9702, 2)], b)
        self.assertFalse(again.dirty)

    def test_a_load_drops_expired_unknown_and_broken_rows(self):
        rows = [_board().to_json(), _board(guild_id=3, expires_at=T0 - 1).to_json(),
                _board(guild_id=4).to_json(), {'channel': 1, 'guild_id': 'x'},
                dict(_board(guild_id=5).to_json(), kind='mega'), dict(_board(guild_id=6).to_json(), emblem_fg=1000)]
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump({'version': 1, 'boards': rows}, f)
        st = BD.BoardStore(self.path)
        self.assertEqual(st.load(T0, guild_ids={2, 3, 5, 6}), 1)     # guild 4 is gone
        self.assertEqual(list(st.boards), [(1, 9702, 2)])
        self.assertTrue(st.dirty)                                    # the next save drops them
        st.flush()
        self.assertEqual([r['guild_id'] for r in json.load(open(self.path, encoding='utf-8'))['boards']], [2])

    def test_a_malformed_file_is_moved_aside(self):
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write('{not json')
        with self.assertLogs('WS', logging.ERROR):
            self.assertEqual(BD.BoardStore(self.path).load(T0), 0)
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(any(n.startswith('guild_boards.json.bad-') for n in os.listdir(self.tmp)))

    def test_kinds_follow_the_exe_ids(self):
        self.assertEqual((BD.exe_item('board', 'en'), BD.exe_item('premium', 'en')), (4284, 4283))
        self.assertEqual((BD.exe_item('board', 'kr'), BD.exe_item('premium', 'kr')), (4280, 4279))
        self.assertEqual((BD.kind_of_exe(4284), BD.kind_of_exe(4283), BD.kind_of_exe(4280)), ('board', 'premium', None))
        self.assertEqual(BD.kind_of_exe(4280, 'kr'), 'board')
        self.assertEqual(BD.EN_ITEM, {'board': 4284, 'premium': 4283})

    def test_config(self):
        d = cfgmod.defaults()
        self.assertEqual((d['GUILD_BOARDS_FILE'], d['GUILD_BOARD_MINUTES'], d['GUILD_PREMIUM_BOARD_MINUTES']),
                         ('guild_boards.json', 60.0, 1440.0))
        for bad in (0.0, -1.0, math.nan, math.inf, 43201.0):
            with self.assertRaises(cfgmod.ConfigError):
                cfgmod.from_dict({'GUILD_BOARD_MINUTES': bad})
        self.assertEqual(cfgmod.from_dict({'GUILD_PREMIUM_BOARD_MINUTES': 2})['GUILD_PREMIUM_BOARD_MINUTES'], 2.0)


# =================================================================== rig ===
class _Rig:
    """TestHero (A, uid 1, the master of Testers), Watcher (B, uid 2, no guild) and Carol (C, uid
    3, the master of Rivals) in world on 101. A and C have 60,000 gold, B 100."""
    build = B9
    extra_config = {}

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        self.tmp = tempfile.mkdtemp(prefix=f'ws_g6_{self.build}_')
        self.wall = Wall()
        self.server = self.make(accounts=_accounts())
        with self.server.store.lock:
            for name, gold in GOLD.items():
                char = self.server.store.character_by_name(name)[2]
                char['gold'], char['class'], char['job2'] = gold, 1, 1
        if self.build == B9:
            self.server.guilds.seed('Testers', 'TestHero')
            self.server.guilds.seed('Rivals', 'Carol')
        self.mc = F.MultiClient(self.server, players=(('test', 'test', 'TestHero'), ('admin', 'admin', 'Watcher'),
                                                      ('carol', 'carol', 'Carol')))
        self.a, self.b, self.c = self.mc
        self.mc.drain(0.3)

    def make(self, accounts=None):
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False,
                                **self.extra_config})
        BD.CLOCK = self.wall                    # the store loads at the fake time too
        self.addCleanup(setattr, BD, 'CLOCK', time.time)
        if accounts is None:
            server = F.make_server(self.tmp, accounts=json.load(open(os.path.join(self.tmp, 'accounts.json'))),
                                   config=cfg)
        else:
            server = F.make_server(self.tmp, accounts=accounts, config=cfg)
        self.assertIs(server.boards.clock, self.wall)
        return server

    def tearDown(self):
        self.mc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers ---
    def char(self, name):
        return self.server.store.character_by_name(name)[2]

    def gold(self, name):
        with self.server.store.lock:
            return self.char(name)['gold']

    def bag(self, name, item):
        with self.server.store.lock:
            import inventory as invmod
            return invmod.Inventory(self.char(name)).count(item)

    def give(self, name, item, n=1):
        import inventory as invmod
        with self.server.store.lock:
            invmod.Inventory(self.char(name)).add(item, n)

    def gid(self, name='Testers'):
        return self.server.guilds.get(name).id

    def warp(self, c, map_code, x, y):
        """A server map load + the client's own resync (C2S 0x2F, 0x63, 0x8A): every packet."""
        s = c.session
        c.recv_until_quiet(0.2)
        self.server._map_transfer(s['sock'], s, map_code, x, y, reason='test warp')
        pkts = c.recv_until_quiet(0.3)
        self.assertEqual(_ops(pkts)[:3], [0x08, 0x03, 0x07])
        c.send_c2s(FRIENDS)
        c.send_c2s(CARDS)
        c.send_c2s(GUILD_INFO)
        return c.recv_until_quiet(0.3)

    def to_plaza(self, *clients):
        for c in clients:
            self.warp(c, *PLAZA)
        self.mc.drain(0.3)

    def place(self, c, item=BOARD, text='Join us', x=2200.0, y=1300.0, gid=None):
        gid = self.gid() if gid is None else gid
        c.send_c2s(PLACE, {'master_char_id': P.session_uid(c.session) or 0, 'master_name': c.char_name,
                           'board_item_id': item, 'guild_id': gid, 'guild_name': 'Testers', 'emblem_symbol': 18,
                           'emblem_bg': 18, 'ad_text': text, 'pos_x': x, 'pos_y': y})
        return c.recv_until_quiet(0.3)

    def lines(self, c, pkts):
        return [P.to_bytes(c.s2c(p)['text']).decode('latin-1') for p in pkts if p.opcode == 0x15]

    def gm(self, c, line):
        self.server._gm_chat_command(c.session['sock'], c.session, line.encode())
        got = c.recv_until_quiet(0.3)
        return got, self.lines(c, got)


class Boards2009(_Rig, unittest.TestCase):
    # ---------------------------------------------------------------- sale ---
    def buy(self, c, item=PREMIUM, qty=1, npc=0):
        """Moiba's C2S 0x0B as the real client sends it: npc_id 0 (p15 live triage 1)."""
        c.send_c2s(MOIBA_BUY, {'item_id': item, 'qty': qty, 'npc_id': npc})
        return c.recv_until_quiet(0.3)

    def test_moiba_sells_the_guild_billboard(self):
        """Either hni: Moiba's row is 4283 on the stock data (the dialog names the premium board
        at "(0Gold)", so a line after the 0x18 names the real sale) or 4284 after the cp-5 data
        patch (the dialog names and prices the sale itself: no line). The client asks for that
        row's id; the server sells the Guild Billboard 4284 at its hii Buy either way."""
        a, b = self.a, self.b
        self.to_plaza(a, b)
        price = EC.items().get(BOARD).buy
        self.assertEqual(price, 1000)
        row = EC.npcs().get(MOIBA).shop_items                              # the hni row the client asks for
        self.assertIn(row, ([PREMIUM], [BOARD]))                            # stock / cp-5
        asked = row[0]
        got = self.buy(a, item=asked, qty=2)
        r = a.s2c(got[0])
        self.assertEqual((r['gold'], r['item_id'], r['count']), (60000 - 2 * price, BOARD, 2))
        if asked == PREMIUM:
            # the dialog said "Premium Guild Billboard ... (0Gold)": the line names the real sale
            self.assertEqual(_ops(got), [0x18, 0x15])
            self.assertEqual((a.s2c(got[1])['msg_type'], self.lines(a, got)),
                             (1, ['Guild Billboard x2 bought for 2,000 gold.']))
        else:
            self.assertEqual(_ops(got), [0x18])                              # the dialog was right
        self.assertEqual((self.gold('TestHero'), self.bag('TestHero', BOARD), self.bag('TestHero', PREMIUM)),
                         (58000, 2, 0))
        # not enough gold: the 0x18 resync (item 0) + the line, nothing granted
        got = self.buy(b, item=asked)
        self.assertEqual((_ops(got), b.s2c(got[0])['item_id']), ([0x18, 0x15], 0))
        self.assertEqual(self.lines(b, got), ['[Warning] Not enough gold.'])
        self.assertEqual((self.gold('Watcher'), self.bag('Watcher', BOARD)), (100, 0))
        # an id that is no board / not Moiba's
        self.assertEqual(self.lines(a, self.buy(a, item=3)), ['[Warning] That item is not for sale.'])

    def test_moiba_sale_with_the_wire_npc_id_zero(self):
        """p15 live triage 1: send site 0x474327 always sends npc_id 0 (dialog 0x10's OK closes it
        and FUN_00497e00 zeroes +0x11C before the read at 0x474315). The server names Moiba from
        the map (9702) and the item (a board id); everything else is no sale."""
        a = self.a
        self.assertIs(EC.guild_npc_2009(), EC.npcs().get(MOIBA))
        self.assertEqual(EC.guild_npc_2009().ui, EC.UI_GUILD_NPC_2009)
        refused = ['[Warning] That item is not for sale.']
        # off the plaza (map 101): npc 0 is no merchant - the 0x18 resync, the line, gold kept
        got = self.buy(a)
        self.assertEqual((_ops(got), a.s2c(got[0])['item_id'], self.lines(a, got)), ([0x18, 0x15], 0, refused))
        self.assertEqual((self.gold('TestHero'), self.bag('TestHero', BOARD)), (60000, 0))
        self.to_plaza(a)
        with self.assertLogs('WS', logging.INFO) as logs:
            got = self.buy(a)
        self.assertEqual(_ops(got), [0x18, 0x15])
        r = a.s2c(got[0])
        self.assertEqual((r['gold'], r['item_id'], r['count']), (59000, BOARD, 1))
        self.assertTrue(any('from guild NPC 181' in m and '(wire npc_id 0: 0x474327)' in m for m in logs.output),
                        logs.output)
        # on the plaza, ids that are no board of this exe: a plain item and the stock exe's ids
        # (EN 4280 / 4279 are bows under 'en')
        for item in (3, 4280, 4279):
            with self.subTest(item=item):
                got = self.buy(a, item=item)
                self.assertEqual((_ops(got), a.s2c(got[0])['item_id'], self.lines(a, got)), ([0x18, 0x15], 0, refused))
        self.assertEqual((self.gold('TestHero'), self.bag('TestHero', BOARD)), (59000, 1))
        # the EN board id itself, and an explicit npc_id 181 (a client that does send it)
        self.assertEqual(a.s2c(self.buy(a, item=BOARD)[0])['item_id'], BOARD)
        got = self.buy(a, npc=MOIBA)
        self.assertEqual((a.s2c(got[0])['gold'], a.s2c(got[0])['item_id']), (57000, BOARD))
        self.assertEqual(self.bag('TestHero', BOARD), 3)

    def test_npc_id_zero_is_refused_after_leaving_the_plaza(self):
        """Review (p15 live triage 1): _board_sale_npc names Moiba from session['current_map'],
        so that gate must follow the departure. Sold on 9702; after a warp to 801 (the town of
        the plaza portal 801_75) current_map is 801, and the same npc_id 0 buy is no sale -
        the merchant path's refusal (npc 0 is no merchant), gold and bag unchanged."""
        a = self.a
        self.to_plaza(a)
        self.assertEqual((a.session['current_map'], a.s2c(self.buy(a, item=BOARD)[0])['item_id']),
                         (G.GUILD_PLAZA_MAP, BOARD))
        self.warp(a, 801, 1550.0, 1136.0)
        self.assertEqual((a.session['current_map'], self.server.world.map_of(a.session)), (801, 801))
        self.assertIsNone(self.server._board_sale_npc(a.session, BOARD))
        for item in (BOARD, PREMIUM):
            with self.subTest(item=item), self.assertLogs('WS', logging.INFO) as logs:
                got = self.buy(a, item=item)
                self.assertEqual((_ops(got), a.s2c(got[0])['item_id'], self.lines(a, got)),
                                 ([0x18, 0x15], 0, ['[Warning] That item is not for sale.']))
            self.assertTrue(any('npc 0' in m and 'not a merchant' in m for m in logs.output), logs.output)
        self.assertEqual((self.gold('TestHero'), self.bag('TestHero', BOARD)), (59000, 1))

    def test_the_cp5_row_is_priced_as_the_client_checks_it(self):
        """cp-5 (Moiba's hni row 4283 -> 4284): the dialog names and prices the Guild Billboard
        itself. FUN_00471450 (called with param_7 = 1 at 0x4742EB) checks ROUND(qty x itemdef
        +0x1E0 x discount) <= gold; the server charges exactly that - so the player who has it
        buys, one gold short is refused - and sends no price line (the dialog was right)."""
        a = self.a
        moiba = EC.npcs().get(MOIBA)
        self.addCleanup(setattr, moiba, 'shop_items', list(moiba.shop_items))
        moiba.shop_items = [BOARD]
        self.to_plaza(a)
        self.server.store.adjust_manner('test', 200)                       # manner > 199: x 0.9
        d = 0.9
        client_cost = int(round(3 * EC.items().get(BOARD).buy * d))        # what the dialog checks
        self.assertEqual(client_cost, 2700)
        with self.server.store.lock:
            self.char('TestHero')['gold'] = client_cost - 1
        got = self.buy(a, item=BOARD, qty=3)
        self.assertEqual((_ops(got), self.lines(a, got)), ([0x18, 0x15], ['[Warning] Not enough gold.']))
        with self.server.store.lock:
            self.char('TestHero')['gold'] = client_cost
        got = self.buy(a, item=BOARD, qty=3)
        self.assertEqual(_ops(got), [0x18])                                  # no price line
        r = a.s2c(got[0])
        self.assertEqual((r['gold'], r['item_id'], r['count']), (0, BOARD, 3))
        self.assertEqual(self.bag('TestHero', BOARD), 3)

    # ----------------------------------------------------------- placement ---
    def test_a_master_places_a_board_and_the_plaza_sees_it(self):
        a, b, c = self.a, self.b, self.c
        self.to_plaza(a, b)
        self.give('TestHero', BOARD, 2)
        gid = self.gid()
        got = self.place(a, text='Join Testers!')
        self.assertEqual(_ops(got), [0xBA, 0xB3])
        add = a.s2c(got[0])
        self.assertEqual(add, {'master_char_id': 1, 'master_name': 'TestHero', 'board_item_id': BOARD, 'guild_id': gid,
                               'guild_name': 'Testers', 'emblem_symbol': G.EMBLEM_DEFAULT,
                               'emblem_bg': G.EMBLEM_DEFAULT, 'ad_text': 'Join Testers!', 'pos_x': 2200.0,
                               'pos_y': 1300.0})
        self.assertEqual(len(got[0].payload), 87)
        self.assertEqual(a.s2c(got[1]), {'sub': 185, 's185_flag': 1, 's185_item_id': BOARD})
        self.assertEqual([b.s2c(p) for p in b.recv_until_quiet(0.3) if p.opcode == 0xBA], [add])
        self.assertEqual([p for p in c.recv_until_quiet(0.3) if p.opcode in (0xBA, 0xBB)], [])   # not on 9702
        self.assertEqual(self.bag('TestHero', BOARD), 1)                   # taken at the 0x88
        # the client's echo of sub 185: 0x25 (its own consume), no second removal
        a.send_c2s(USE_15, {'item_id': BOARD})
        got = a.recv_until_quiet(0.3)
        self.assertEqual([(p.opcode, a.s2c(p)) for p in got], [(0x25, {'item_or_skill_id': BOARD})])
        self.assertEqual(self.bag('TestHero', BOARD), 1)
        self.assertNotIn(BD.BOARD_ECHO_KEY, a.session)
        b_ = self.server.boards.of_guild(gid)
        self.assertEqual((b_.kind, b_.master_uid, b_.ad_text, b_.expires_at - b_.placed_at), ('board', 1,
                                                                                            'Join Testers!', 3600.0))
        # persisted (guild_boards.json in the temp dir)
        self.server.boards.flush()
        rows = json.load(open(os.path.join(self.tmp, 'guild_boards.json'), encoding='utf-8'))['boards']
        self.assertEqual([(r['channel'], r['map'], r['guild_id'], r['ad_text']) for r in rows],
                         [(1, 9702, gid, 'Join Testers!')])

    def test_the_premium_board_is_a_cash_record(self):
        a, b = self.a, self.b
        self.to_plaza(a, b)
        rec = self.server.cash.grant(self.char('TestHero'), PREMIUM)
        got = self.place(a, item=PREMIUM, text='VIP')
        self.assertEqual(_ops(got), [0xBA, 0x72, 0xB3])
        self.assertEqual(a.s2c(got[0])['board_item_id'], PREMIUM)
        self.assertEqual(a.s2c(got[1]), {'player_uid': 1, 'item_id': PREMIUM, 'item_serial': rec['serial']})
        self.assertEqual(a.s2c(got[2]), {'sub': 185, 's185_flag': 1, 's185_item_id': PREMIUM})
        self.assertIsNone(self.server.cash.find(self.char('TestHero'), PREMIUM))
        self.assertNotIn(BD.BOARD_ECHO_KEY, a.session)
        b_ = self.server.boards.of_guild(self.gid())
        self.assertEqual((b_.kind, b_.expires_at - b_.placed_at), ('premium', 1440 * 60.0))
        self.assertEqual([b.s2c(p)['board_item_id'] for p in b.recv_until_quiet(0.3) if p.opcode == 0xBA], [PREMIUM])

    def test_every_refusal_takes_nothing(self):
        a, b, c = self.a, self.b, self.c
        gid = self.gid()
        self.give('TestHero', BOARD, 1)
        # not on 9702 (the cp-2 client says the same itself)
        self.assertEqual(self.lines(a, self.place(a)), ['[Warning] ' + BD.TEXT_PLAZA_ONLY])
        self.to_plaza(a, b, c)
        # not a guild master: sub 185 {0} "Guild master can only do this."
        self.give('Watcher', BOARD, 1)
        got = self.place(b, gid=gid)
        self.assertEqual([b.s2c(p) for p in got], [{'sub': 185, 's185_flag': 0}])
        # a stock-exe id, an empty text
        self.assertEqual(self.lines(a, self.place(a, item=4280)), ['[Warning] ' + BD.TEXT_NO_ITEM])
        self.assertEqual(self.lines(a, self.place(a, text='')), ['[Warning] ' + BD.TEXT_EMPTY])
        # no premium record
        self.assertEqual(self.lines(a, self.place(a, item=PREMIUM)), ['[Warning] ' + BD.TEXT_NO_ITEM])
        self.assertEqual(self.bag('TestHero', BOARD), 1)
        self.assertEqual(self.server.boards.live(), [])
        # placed; then a second board of the same guild, and Carol's within 158 px
        self.assertEqual(_ops(self.place(a)), [0xBA, 0xB3])
        self.give('TestHero', BOARD, 1)
        self.assertEqual(self.lines(a, self.place(a, x=3000.0)), ['[Warning] ' + BD.TEXT_DOUBLE])
        self.give('Carol', BOARD, 2)
        rivals = self.gid('Rivals')
        too_close = self.place(c, x=2300.0, y=1350.0, gid=rivals)          # 111.8 px
        self.assertEqual(self.lines(c, too_close), ['[Warning] ' + BD.TEXT_TOO_CLOSE])
        self.assertEqual((self.bag('TestHero', BOARD), self.bag('Carol', BOARD)), (1, 2))
        self.mc.drain(0.3)
        got = self.place(c, x=2200.0, y=1460.0, gid=rivals)                # 160 px: fine
        self.assertEqual(_ops(got), [0xBA, 0xB3])
        self.assertEqual(self.bag('Carol', BOARD), 1)
        self.assertEqual(sorted(x.guild_id for x in self.server.boards.live()), sorted([gid, rivals]))

    def test_a_commit_refusal_gives_the_item_back(self):
        """The commit re-check (under flow_lock, after _take) refuses: the master was demoted in
        between. The bag billboard and the premium cash record (same serial, same qty) come back
        server side - the client was never told they were gone - and no 0xBA / 0x72 goes out."""
        a, b = self.a, self.b
        self.to_plaza(a, b)
        self.give('TestHero', BOARD, 1)
        rec = dict(self.server.cash.grant(self.char('TestHero'), PREMIUM))
        boards, gid, cid = self.server.boards, self.gid(), self.char('TestHero')['cid']
        take = boards._take

        def grade(value):
            with self.server.guilds.lock:
                self.server.guilds.db.guilds[gid].member(cid)['grade'] = value

        def take_then_demote(session, char, kind):
            taken = take(session, char, kind)
            grade(G.GRADE_GUARDIAN)
            return taken
        for item in (BOARD, PREMIUM):
            with self.subTest(item=item), mock.patch.object(boards, '_take', take_then_demote):
                got = self.place(a, item=item)
                self.assertEqual([a.s2c(p) for p in got], [{'sub': 185, 's185_flag': 0}])
                self.assertEqual([p.opcode for p in b.recv_until_quiet(0.2) if p.opcode in (0xBA, 0xB8)], [])
            grade(G.GRADE_MASTER)
        self.assertEqual(self.bag('TestHero', BOARD), 1)
        back = self.server.cash.find(self.char('TestHero'), PREMIUM)
        self.assertEqual((back['serial'], back['qty']), (rec['serial'], rec['qty']))
        self.assertEqual((boards.live(), a.session.get(BD.BOARD_ECHO_KEY)), ([], None))
        # and it was only the race: the same items place fine now
        self.assertEqual(_ops(self.place(a)), [0xBA, 0xB3])

    def test_a_billboard_offered_in_a_trade_is_not_taken(self):
        """trade-escrow-guards: the one billboard A offers in a trade cannot also make a board
        ("That item is offered in a trade."); nothing is taken, no board is stored."""
        a, b = self.a, self.b
        self.to_plaza(a, b)
        self.give('TestHero', BOARD, 1)
        a.send_c2s('0x44A9D6/0x20', {'target_uid': P.session_uid(b.session)})
        self.assertEqual(_ops(b.recv_until_quiet(0.3)), [0x45])
        b.send_c2s('0x47394E/0x21', {'requester_name': a.char_name})
        a.send_c2s('0x473E14/0x22', {'item_id': BOARD, 'qty': 1})
        self.mc.drain(0.3)
        self.assertEqual(self.server.trade.offered(a.session, BOARD), 1)
        got = self.place(a)
        self.assertEqual(self.lines(a, got), ['[Warning] ' + W.trademod.IN_TRADE_TEXT])
        self.assertEqual((self.bag('TestHero', BOARD), self.server.boards.live()), (1, []))
        self.assertEqual([p.opcode for p in b.recv_until_quiet(0.2) if p.opcode == 0xBA], [])

    def test_a_board_expired_before_its_tick_is_removed_first(self):
        """A board past expires_at that the 'guild-boards' tick (every SCAN_SECS) has not dropped
        yet, replaced by a new 0x88 of its guild under the same (channel, map, guild) key: its
        0xB8 goes out BEFORE the new 0xBA, or 9702 would keep drawing both (0xBA has no duplicate
        check) until a re-entry."""
        a, b = self.a, self.b
        self.to_plaza(a, b)
        self.give('TestHero', BOARD, 2)
        self.place(a, text='old')
        self.mc.drain(0.3)
        self.wall.t = T0 + 3600 + 1                                        # expired, not ticked
        got = self.place(a, text='new')
        self.assertEqual(_ops(got), [0xB8, 0xBA, 0xB3])
        self.assertEqual([(p.opcode, b.s2c(p).get('ad_text')) for p in b.recv_until_quiet(0.3)
                          if p.opcode in (0xB8, 0xBA)], [(0xB8, None), (0xBA, 'new')])
        self.assertEqual(self.server.boards.tick(), [])                    # nothing left for the tick
        self.assertEqual([x.ad_text for x in self.server.boards.live()], ['new'])

    def test_the_plaza_holds_one_0xBB_frame_of_boards(self):
        a, b = self.a, self.b
        self.to_plaza(a, b)
        self.give('TestHero', BOARD, 1)
        with self.server.boards.db.lock:                                   # 23 boards of other guilds
            for i in range(BD.MAX_BOARDS):
                row = _board(guild_id=100 + i, guild_name=f'G{i}', pos_x=10000.0 + 500 * i, placed_at=self.wall.t,
                             expires_at=self.wall.t + 600)
                self.server.boards.db.boards[row.key] = row
        self.assertEqual(BD.MAX_BOARDS, 23)
        self.assertEqual(self.lines(a, self.place(a)), ['[Warning] ' + BD.TEXT_FULL])
        self.assertEqual(self.bag('TestHero', BOARD), 1)
        with self.assertRaises(ValueError):
            self.server.boards.seed('Rivals', 'one too many')
        got = self.warp(b, *PLAZA)
        rows = [b.s2c(p) for p in got if p.opcode == 0xBB]
        self.assertEqual([r['board_count'] for r in rows], [23])
        self.assertEqual(len([p for p in got if p.opcode == 0xBB][0].payload), 1 + 23 * 87)

    def test_a_board_click_applies_through_the_u16_join(self):
        a, b = self.a, self.b
        self.to_plaza(a, b)
        self.give('TestHero', BOARD, 1)
        self.place(a)
        self.mc.drain(0.3)
        gid = self.gid()
        b.send_c2s(APPLY_BOARD, {'guild_id': gid})                          # 0x482DCD: u16
        self.assertEqual([b.s2c(p) for p in b.recv_until_quiet(0.3) if p.opcode == 0xB3],
                         [{'sub': 2, 's2_result': 1}])
        push = [p for p in a.recv_until_quiet(0.3) if p.opcode == 0xB3]
        self.assertEqual(push[0].payload[:2], bytes((2, 0x10)))
        self.assertEqual(G.parse_application(push[0].payload[2:])[0], 'Watcher')
        self.assertEqual([x['name'] for x in self.server.guilds.get(gid).applications], ['Watcher'])

    # ------------------------------------------------------------ re-entry ---
    def test_entering_9702_restores_the_boards_after_the_8A_reply(self):
        a, b = self.a, self.b
        self.to_plaza(a)
        self.give('TestHero', BOARD, 1)
        self.place(a, text='Hello')
        self.server.boards.seed('Rivals', 'Rivals rock', pos=(1000.0, 1300.0))
        self.mc.drain(0.3)
        order = RS.Resync.order(self.server.resync, B9)[RS.STAGE_GUILD]
        self.assertEqual(order, ['guild', 'guild_apps', 'guild_boards', 'gm_tag'])
        got = self.warp(b, *PLAZA)
        ops = _ops(got)
        self.assertIn(0xBB, ops)
        after = ops[ops.index(0xBB) + 1:]
        before = ops[:ops.index(0xBB)]
        self.assertIn(0xB3, before)                                        # the 0x8A reply first
        self.assertTrue(all(o == 0xB3 for o in after))                     # at most sub 37 after it
        listing = b.s2c(got[ops.index(0xBB)])
        self.assertEqual(listing['board_count'], 2)
        rows = listing['repeat[board_count]']
        self.assertEqual([(r['guild_name'], r['ad_text'], r['board_item_id'], r['pos_x']) for r in rows],
                         [('Testers', 'Hello', BOARD, 2200.0), ('Rivals', 'Rivals rock', BOARD, 1000.0)])
        # the 0xBB record order: guild id after the master name, the item after the text [V]
        raw = got[ops.index(0xBB)].payload
        self.assertEqual(len(raw), 1 + 2 * 87)
        self.assertEqual(int.from_bytes(raw[1 + 4 + 17:1 + 4 + 17 + 2], 'little'), self.gid())
        self.assertEqual(int.from_bytes(raw[1 + 87 - 18:1 + 87 - 16], 'little'), BOARD)   # before 2 x f64
        # another map: no 0xBB; back in the plaza for a GM master: 0xBB before sub 37
        self.assertNotIn(0xBB, _ops(self.warp(b, 101, 1411.0, 714.0)))
        a.session['gm'], a.session['gm_hidden'] = 1, 0
        got = self.warp(a, *PLAZA)
        b3 = [(p.opcode, a.s2c(p).get('sub')) for p in got if p.opcode in (0xB3, 0xBB)]
        self.assertEqual(b3[-2:], [(0xBB, None), (0xB3, 37)])

    # -------------------------------------------------------------- expiry ---
    def test_expiry_sends_B8_and_the_plaza_forgets_it(self):
        a, b, c = self.a, self.b, self.c
        self.to_plaza(a, b)
        self.give('TestHero', BOARD, 1)
        self.place(a)
        self.mc.drain(0.3)
        gid = self.gid()
        self.assertEqual(self.server.boards.tick(T0 + 3599), [])
        self.wall.t = T0 + 3600
        gone = self.server.boards.tick()
        self.assertEqual([x.guild_id for x in gone], [gid])
        for cl in (a, b):
            self.assertEqual([cl.s2c(p) for p in cl.recv_until_quiet(0.3) if p.opcode == 0xB8], [{'guild_id': gid}])
        self.assertEqual([p for p in c.recv_until_quiet(0.3) if p.opcode == 0xB8], [])
        self.assertEqual(self.server.boards.live(), [])
        self.assertNotIn(0xBB, _ops(self.warp(b, *PLAZA)))
        self.mc.drain(0.3)
        # the master may place again
        self.give('TestHero', BOARD, 1)
        self.assertEqual(_ops(self.place(a)), [0xBA, 0xB3])

    def test_disband_and_admin_disband_drop_the_board(self):
        a, b, c = self.a, self.b, self.c
        self.to_plaza(a, b, c)
        self.give('TestHero', BOARD, 1)
        self.place(a)
        self.server.boards.seed('Rivals', 'hi', pos=(500.0, 1300.0))
        self.mc.drain(0.3)
        gid, rivals = self.gid(), self.gid('Rivals')
        a.send_c2s(ACTION, {'guild_id': gid, 'char_id': 1, 'char_name': 'TestHero', 'action': G.ACT_DISBAND})
        got = a.recv_until_quiet(0.3)
        self.assertEqual([(p.opcode, a.s2c(p)) for p in got if p.opcode in (0xB3, 0xB8)],
                         [(0xB3, {'sub': 8, 's8_result': 1}), (0xB8, {'guild_id': gid})])
        self.assertEqual([b.s2c(p) for p in b.recv_until_quiet(0.3) if p.opcode == 0xB8], [{'guild_id': gid}])
        self.assertEqual([x.guild_id for x in self.server.boards.live()], [rivals])
        self.server.guilds.disband('Rivals')                               # `!guild disband`
        self.assertEqual([b.s2c(p) for p in b.recv_until_quiet(0.3) if p.opcode == 0xB8], [{'guild_id': rivals}])
        self.assertEqual(self.server.boards.live(), [])

    # ------------------------------------------------------------- restart ---
    def test_a_restart_keeps_the_live_boards(self):
        a = self.a
        self.to_plaza(a)
        self.give('TestHero', BOARD, 1)
        self.place(a, text='Still here')
        self.server.boards.seed('Rivals', 'short', pos=(500.0, 1300.0), minutes=1)
        self.server.boards.flush()
        self.server.guilds.flush()
        self.server.store.flush()
        self.mc.close()
        self.wall.t = T0 + 120                                             # Rivals' board is over
        self.server = self.make()
        self.assertEqual([(x.guild_name, x.ad_text) for x in self.server.boards.live()], [('Testers', 'Still here')])
        self.mc = F.MultiClient(self.server, players=(('admin', 'admin', 'Watcher'),))
        b = self.mc[0]
        self.mc.drain(0.3)
        got = self.warp(b, *PLAZA)
        rows = [b.s2c(p) for p in got if p.opcode == 0xBB]
        self.assertEqual([[r['ad_text'] for r in x['repeat[board_count]']] for x in rows], [['Still here']])
        self.server.boards.flush()
        rows = json.load(open(os.path.join(self.tmp, 'guild_boards.json'), encoding='utf-8'))['boards']
        self.assertEqual([r['ad_text'] for r in rows], ['Still here'])

    # ------------------------------------------------------------------ GM ---
    def test_gm_board_commands(self):
        a, b = self.a, self.b
        self.to_plaza(a, b)
        a.session['pos'] = (2400.0, 1300.0)
        got, texts = self.gm(a, '!guild board place Rivals Come to Rivals')
        self.assertEqual([a.s2c(p)['ad_text'] for p in got if p.opcode == 0xBA], ['Come to Rivals'])
        self.assertTrue(texts[0].startswith('Rivals: board "Come to Rivals" at (2400,1300)'), texts)
        self.assertEqual([b.s2c(p)['pos_x'] for p in b.recv_until_quiet(0.3) if p.opcode == 0xBA], [2400.0])
        rivals = self.server.boards.of_guild(self.gid('Rivals'))
        self.assertEqual((rivals.master_uid, rivals.master_name), (3, 'Carol'))   # the master's (double check)
        # a second place replaces it: 0xB8 then 0xBA
        got, _ = self.gm(a, '!guild board premium Rivals New text')
        self.assertEqual([(p.opcode, a.s2c(p).get('board_item_id')) for p in got if p.opcode in (0xB8, 0xBA)],
                         [(0xB8, None), (0xBA, PREMIUM)])
        _, texts = self.gm(a, '!guild board')
        self.assertTrue(texts[0].startswith('1 live board(s) on 9702 (ch1)'), texts)
        self.assertIn('Rivals premium "New text"', texts[1])
        _, texts = self.gm(a, '!guild board ttl Rivals 5')
        self.assertEqual(texts, ['Rivals: the board expires in 5 s.'])
        self.wall.t += 6
        self.server.boards.tick()
        self.assertEqual([b.s2c(p) for p in b.recv_until_quiet(0.3) if p.opcode == 0xB8][-1:],
                         [{'guild_id': self.gid('Rivals')}])
        self.gm(a, '!guild board place Testers one')
        got, texts = self.gm(a, '!guild board expire all')
        self.assertEqual(texts, ['1 board(s) removed (0xB8 to 9702).'])
        _, texts = self.gm(a, '!guild board place Nobody x')
        self.assertTrue(texts[0].startswith("[Warning] no guild named 'Nobody'"), texts)


class StockExe2009(_Rig, unittest.TestCase):
    """CLIENT_ITEM_IDS 'kr': the stock exe never reaches 0x88; boards are GM-seeded, with its ids."""
    extra_config = {'CLIENT_ITEM_IDS': 'kr'}

    def test_sale_and_placement_are_refused_and_seeds_use_the_kr_ids(self):
        a, b = self.a, self.b
        self.to_plaza(a, b)
        # the wire's npc_id 0 (send site 0x474327): the hni row, the stock exe's own ids, and an
        # explicit 181 all reach Moiba - and the stock exe has no billboard
        for item, npc in ((PREMIUM, 0), (4280, 0), (4279, 0), (PREMIUM, MOIBA)):
            with self.subTest(item=item, npc=npc):
                a.send_c2s(MOIBA_BUY, {'item_id': item, 'qty': 1, 'npc_id': npc})
                got = a.recv_until_quiet(0.3)
                self.assertEqual((_ops(got), a.s2c(got[0])['item_id']), ([0x18, 0x15], 0))
                self.assertEqual(self.lines(a, got), ['[Warning] ' + BD.TEXT_UNAVAILABLE])
        self.assertEqual(self.gold('TestHero'), 60000)
        self.give('TestHero', BOARD, 1)
        self.assertEqual(self.lines(a, self.place(a, item=4280)), ['[Warning] ' + BD.TEXT_UNAVAILABLE])
        self.assertEqual((self.bag('TestHero', BOARD), self.server.boards.live()), (1, []))
        self.server.boards.seed('Testers', 'Seeded')
        self.assertEqual([b.s2c(p)['board_item_id'] for p in b.recv_until_quiet(0.3) if p.opcode == 0xBA], [4280])
        got = self.warp(b, *PLAZA)
        rows = [b.s2c(p)['repeat[board_count]'] for p in got if p.opcode == 0xBB]
        self.assertEqual([[r['board_item_id'] for r in x] for x in rows], [[4280]])
        # a 0x15 of the EN board with no placement pending is the old consumable path
        self.mc.drain(0.3)
        a.send_c2s(USE_15, {'item_id': BOARD})
        self.assertEqual(_ops(a.recv_until_quiet(0.3)), [0x25])
        self.assertEqual(self.bag('TestHero', BOARD), 0)


class Boards2008(_Rig, unittest.TestCase):
    build = B8

    def test_no_board_module_at_work(self):
        boards = self.server.boards
        self.assertFalse(boards.supported)
        self.assertEqual(boards.db.path, '')
        self.assertEqual(boards.tick(T0 + 1e9), [])
        self.assertEqual(boards.remove_guild(2, 'x', always=True), [])
        self.assertNotIn('guild_boards', RS.Resync.order(self.server.resync, B8)[RS.STAGE_GUILD])
        self.assertNotEqual(self.server.ROUTES.get(0x88) and self.server.ROUTES[0x88].handler,
                            '_handle_guild_board_place')
        self.assertEqual(self.server.ROUTES_2009[0x88].handler, '_handle_guild_board_place')
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'guild_boards.json')))
        with self.assertRaises(ValueError):
            boards.seed('Testers', 'x')

    def test_npc_id_zero_is_no_sale(self):
        """The 2008 client has no guild NPC: a 0x0B with npc_id 0 stays "not a merchant", even
        for a session the server believes is on 9702."""
        a = self.a
        a.session['current_map'] = G.GUILD_PLAZA_MAP
        self.assertIsNone(self.server._board_sale_npc(a.session, PREMIUM))
        with self.assertLogs('WS', logging.INFO) as logs:
            a.send_c2s('0x469D9C/0x0B', {'item_id': PREMIUM, 'qty': 1, 'npc_id': 0})
            got = a.recv_until_quiet(0.3)
        self.assertEqual((_ops(got), a.s2c(got[0])['item_id']), ([0x18, 0x15], 0))
        self.assertEqual(self.lines(a, got), ['[Warning] That item is not for sale.'])
        self.assertTrue(any('not a merchant' in m for m in logs.output), logs.output)
        self.assertEqual(self.gold('TestHero'), 60000)


if __name__ == '__main__':
    unittest.main()
