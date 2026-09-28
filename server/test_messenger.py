#!/usr/bin/env python3
"""
test_messenger.py - P6 stage 2 (the messenger), offline, both client builds
===========================================================================
social_friend-persistence / -friend-list-sync / -friend-add / -friend-delete /
-presence-status / -chat-room / -mentor / -mentor-exp-share (+lc-menti-exp) / -memos
(+chat_mail_gm-memo-delivery) / chat_mail_gm-note-reply-9999 / social_friend-slot-expand
(docs/systems/social_friend.md F1-F12, chat_mail_gm.md F6/F7):

- pure rules (social.py): the persisted shape and its normalizer, capacity steps, the 0x0C
  result set, the 0x78 record of each build (2008 126 B, 2009 130 B), SYSTEMTIME, memo ids;
- the store migration: every messenger field + account `social` added once, with the
  one-time accounts.json.bak-pre-p6 of the original bytes, idempotent;
- two / three fake clients on one server (MultiClient; TestHero test/test uid 1 = client 1,
  Watcher admin/admin uid 2 = client 2, class 1 so it can be a mentor; Carol and the
  offline 'Late' on the side), for 2008 and 2009: C2S 0x2F -> 0x0B (+0x7E, +0x78); add
  (window and popup) -> 0x0D -> accept / refuse -> 0x0C on both, every refusal code;
  delete (directed) -> 0x0E; presence 0x60 on login / logout / status / portal reset;
  rooms 0x33 -> 0x10 -> 0x34 -> 0x0F, lines 0x61 to every member, 0x62 on leave / portal /
  disconnect, the refusals; mentor 0x5C -> 0x7A + 0x7B, the 0x03 mentor block, 0x7E,
  0x7B / 0x7C / 0x7D presence, 0x50 -> 0x7C, a mentee's kill -> 0x7F to the mentor; notes
  0x4B -> memo + 0x77 (offline recipient, live push, the msgr_synced guard), the 0x44
  guard, the 9999 thank-you, `!note` / `!mail` / `!msgr`; 0x73 -> 0x9D.

No port is bound, no client is started and the live accounts.json is never opened (temp
copies; the module checks its hash at the end).
"""
import copy
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

import fakeclient as F  # noqa: E402
import chat  # noqa: E402
import packets as P  # noqa: E402
import registry  # noqa: E402
import social  # noqa: E402
import store as S  # noqa: E402

W = F.import_server()
cfgmod = W.cfgmod
EC = W.EC
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = cfgmod.BUILD_2008, cfgmod.BUILD_2009
DIRS = {B8: cfgmod.DEFAULTS['CLIENT_DIR'], B9: cfgmod.DEFAULTS['CLIENT_DIR_2009']}
HAVE = {b: os.path.exists(os.path.join(HERE, d, 'hs', 'windslayer.hii')) for b, d in DIRS.items()}
PORTAL_101_TO_102 = bytes.fromhex('17000000')   # live 0x7E capture: 101 line 23
NOTE = 1894

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
    EC.configure(DIRS[B8], B8)
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_messenger.py (tests must only use temp copies)'


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _name(value):
    return P.cut_text(value, 16).decode('cp949')


def _ops(pkts):
    return [p.opcode for p in pkts]


def _char(name, cls=0):
    return {'name': name, 'level': 1, 'class': cls, 'map': 101, 'x': 1411.0, 'y': 714.0, 'hp': 100, 'mp': 50}


# ================================================================ pure rules ===
class Rules(unittest.TestCase):
    def test_ensure_builds_and_repairs_the_messenger_fields(self):
        char = {'name': 'TestHero'}
        social.ensure(char)
        self.assertEqual({k: char[k] for k in social.CHAR_FIELDS},
                         {'friends': [], 'friend_capacity': 20, 'mentor': None, 'mentees': [],
                          'memos': [], 'memo_seq': 0})
        again = copy.deepcopy(char)
        social.ensure(again)
        self.assertEqual(again, char)                                      # idempotent
        messy = {'name': 'TestHero', 'friends': ['Watcher', 'watcher', '', 'TestHero', {'name': 'Late'}, 7,
                                                 'X' * 20],
                 'friend_capacity': 37, 'mentor': 'testhero', 'mentees': 'nope',
                 'memos': [{'id': 3, 'from': 'A', 'text': 'x' * 200, 't': [2026, 9]}, {'id': 3, 'from': 'B'},
                           {'id': 0, 'from': 'C'}, 'junk', {'id': 9, 'from': ''}]}
        social.ensure(messy)
        self.assertEqual(messy['friends'], ['Watcher', 'Late', 'X' * 16])  # unique, no self, <= 16 B
        self.assertEqual(messy['friend_capacity'], 35)                     # 20..50 in steps of 5
        self.assertEqual((messy['mentor'], messy['mentees']), (None, []))  # never oneself
        self.assertEqual([(m['id'], m['from'], len(m['text']), len(m['t'])) for m in messy['memos']],
                         [(3, 'A', social.MEMO_TEXT_MAX, 8)])
        self.assertEqual(messy['memo_seq'], 3)
        for cap, want in ((0, 20), (19, 20), (24, 20), (25, 25), (50, 50), (99, 50), ('x', 20)):
            self.assertEqual(social.clamp_capacity(cap), want)

    def test_account_social_limits(self):
        acc = {}
        social.ensure_account(acc)
        self.assertEqual(acc['social'], social.default_account_social())
        acc = {'social': {'report_day': '2026-09-24', 'compliments_received': {'day': 5, 'count': -2}}}
        social.ensure_account(acc)
        self.assertEqual(acc['social']['report_day'], '2026-09-24')
        self.assertEqual(acc['social']['compliments_received'], {'day': None, 'count': 0})

    def test_memo_ids_are_never_reused_and_the_cap_drops_the_oldest(self):
        char = {'name': 'Late'}
        social.ensure(char)
        first = social.add_memo(char, 'TestHero', b'one')
        self.assertEqual(first['id'], 1)
        self.assertEqual(social.delete_memos(char, {1}), 1)
        self.assertEqual(social.add_memo(char, 'TestHero', b'two')['id'], 2)   # not 1 again
        self.assertIsNone(social.add_memo(char, 'TestHero', b'\x00junk'))      # empty after the NUL cut
        for i in range(social.MEMO_MAX + 5):
            social.add_memo(char, 'TestHero', f'm{i}')
        self.assertEqual(len(char['memos']), social.MEMO_MAX)
        self.assertEqual(char['memos'][-1]['text'], f'm{social.MEMO_MAX + 4}')
        self.assertEqual(social.add_memo(char, 'A', 'x' * 150)['text'], 'x' * social.MEMO_TEXT_MAX)

    def test_systemtime_is_the_windows_layout(self):
        t = social.systemtime(time.mktime((2026, 9, 20, 13, 5, 7, 0, 0, -1)) + 0.25)   # a Sunday
        self.assertEqual(t, [2026, 9, 0, 20, 13, 5, 7, 250])

    def test_memo_records_per_build(self):
        memo = {'id': 1, 'from': 'TestHero', 'text': 'Hello from TestHero', 't': [2026, 9, 4, 17, 6, 55, 0, 0]}
        raw8 = P.build('0x78', social.memo_packets([memo], B8)[0], client_build=B8)
        raw9 = P.build('0x78', social.memo_packets([memo], B9)[0], client_build=B9)
        self.assertEqual((len(raw8), len(raw9)), (1 + 126, 1 + 130))       # spec_2009 0x78 diff
        self.assertEqual(raw8[1:18], P.name17('TestHero'))
        self.assertEqual(raw9[1:3], b'\x00T')                               # new leading u8, then the name
        self.assertEqual(raw9[1 + 0x72:1 + 0x74], (2026).to_bytes(2, 'little'))   # SYSTEMTIME at +0x72
        self.assertEqual(social.memo_packets([], B9), [])                   # never a count 0
        many = social.memo_packets([dict(memo, id=i) for i in range(300)], B8)
        self.assertEqual([p['count'] for p in many], [255, 45])

    def test_0x0c_only_sends_codes_the_client_can_show(self):
        row = social.friend_row('Watcher', 1, 2, 0)
        for build in (B8, B9):
            self.assertEqual(len(P.build('0x0C', social.add_result(1, 'Watcher', row), client_build=build)), 24)
            self.assertEqual(len(P.build('0x0C', social.add_result(0x0B, 'Watcher', row), client_build=build)), 24)
            self.assertEqual(len(P.build('0x0C', social.add_result(6, 'Watcher'), client_build=build)), 18)
        for bad in (0, 8, 9, 0x0A, 0x0C, 0x14, 0x16, 0xFF):
            with self.subTest(result=bad), self.assertRaises(ValueError):
                social.add_result(bad, 'Watcher')
        with self.assertRaises(ValueError):
            social.add_result(1, 'Watcher')                                 # success needs the row
        self.assertEqual([social.clamp_status(v) for v in (0, 1, 2, 3, 4, 255)], [0, 0, 2, 3, 0, 0])

    def test_routes_and_policy(self):
        want = {0x2F: '_handle_friend_list', 0x30: '_handle_friend_add', 0x31: '_handle_friend_reply',
                0x32: '_handle_friend_delete', 0x33: '_handle_room_invite', 0x34: '_handle_room_reply',
                0x35: '_handle_room_chat', 0x36: '_handle_room_leave', 0x37: '_handle_messenger_status',
                0x44: '_handle_memo_delete', 0x4B: '_handle_note_send', 0x50: '_handle_mentor_remove',
                0x5C: '_handle_mentor_register', 0x73: '_handle_friend_slot_expand'}
        for routes in (W.GameServer.ROUTES, W.GameServer.ROUTES_2009):
            self.assertEqual({op: routes[op].handler for op in want}, want)
            self.assertEqual(registry.check_routes(routes, W.GameServer), [])
        self.assertNotIn(0x30, W.GameServer.POPUP_REQUESTS)
        for build in (B8, B9):
            self.assertIn(0x37, registry.never_reply_table(build))
            self.assertIn(0x44, registry.never_reply_table(build))
            self.assertEqual(registry.must_reply_table(build)[0x4B].owner, 'social_friend-memos')


# ================================================================= migration ===
class MigrationP6(unittest.TestCase):
    """The messenger fields arrive through an idempotent migration that first writes the
    one-time accounts.json.bak-pre-p6 (hard rule for a persisted-schema change)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_p6_store_')
        self.path = os.path.join(self.tmp, 'accounts.json')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pre_p6_file(self, build):
        st = S.Store(self.path, client_build=build).load()               # a current P5 file
        with st.lock:
            for acc in st.accounts.values():
                del acc['social']
                for ch in acc['characters']:
                    for k in social.CHAR_FIELDS:
                        del ch[k]
        st.save_now()
        for suffix in S.BACKUP_SUFFIXES + (S.BACKUP_SUFFIX_2009,):
            path = self.path + suffix
            if suffix == '.bak-pre-p6':
                if os.path.exists(path):
                    os.remove(path)
            elif not os.path.exists(path):
                open(path, 'wb').close()                                   # earlier phases: done
        with open(self.path, 'rb') as f:
            return f.read()

    def test_the_messenger_fields_are_added_once_with_their_backup(self):
        for build in (B8, B9):
            with self.subTest(build=build):
                original = self._pre_p6_file(build)
                st = S.Store(self.path, client_build=build).load()
                self.assertEqual(sorted(st.migration_changes),
                                 sorted(['test: social created', 'admin: social created']
                                        + [f'test/TestHero: {k} created' for k in social.CHAR_FIELDS]))
                with open(self.path + '.bak-pre-p6', 'rb') as f:
                    self.assertEqual(f.read(), original)
                with open(self.path, encoding='utf-8') as f:
                    disk = json.load(f)
                hero = disk['test']['characters'][0]
                self.assertEqual({k: hero[k] for k in social.CHAR_FIELDS},
                                 {'friends': [], 'friend_capacity': 20, 'mentor': None, 'mentees': [],
                                  'memos': [], 'memo_seq': 0})
                self.assertEqual(disk['admin']['social'], social.default_account_social())
                again = S.Store(self.path, client_build=build).load()
                self.assertEqual((again.migration_changes, again.saves), ([], 0))
                # a second pre-P6 state never overwrites the first backup
                self._pre_p6_file(build)
                with open(self.path + '.bak-pre-p6', 'wb') as f:
                    f.write(b'first')
                S.Store(self.path, client_build=build).load()
                with open(self.path + '.bak-pre-p6', 'rb') as f:
                    self.assertEqual(f.read(), b'first')
                os.remove(self.path + '.bak-pre-p6')

    def test_new_characters_and_accounts_carry_them(self):
        st = S.Store(self.path).load()
        ch = st.new_character('Nova', s10=1, s1=1, s6=2, s5=2, s9=2, stats=(3, 2, 1, 3))
        self.assertEqual((ch['friends'], ch['friend_capacity'], ch['mentor'], ch['memos']), ([], 20, None, []))
        acc = st.create_account('dora', 'pw')
        self.assertEqual(acc['social'], social.default_account_social())
        st.add_character('dora', ch)
        self.assertEqual(S.Store(self.path).load().migration_changes, [])


# ============================================================== two players ===
class _Messenger:
    """TestHero (uid 1, Novice) and Watcher (uid 2, class 1) in world on map 101 of one server
    of `build`; Carol (uid 3) and Late (uid 4) exist and are offline until a test logs them in."""
    build = B8

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        self.tmp = tempfile.mkdtemp(prefix=f'ws_msgr_{self.build}_')
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False})
        accounts = F.two_player_accounts()
        accounts['carol'] = {'password': 'carol', 'characters': [_char('Carol')]}
        accounts['late'] = {'password': 'late', 'characters': [_char('Late')]}
        self.server = F.make_server(self.tmp, accounts=accounts, config=cfg)
        with self.server.store.lock:        # the migration makes every legacy record a Novice
            self.rec('Watcher')['class'] = 1
        self.prepare()
        self.extra = []
        self.mc = F.MultiClient(self.server)
        self.a, self.b = self.mc
        self.mc.drain()

    def prepare(self):
        """Store edits before anyone logs in (subclass hook)."""

    def tearDown(self):
        for c in self.extra:
            try:
                c.close()
            except OSError:
                pass
        self.mc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ----------------------------------------------------------------- helpers ---
    def key(self, op, gift=False):
        for v in P.variants(op, 'C2S', client_build=self.build):
            if op != 0x4B or ('9999' in v['name']) == gift:
                return v['key']
        raise KeyError(op)

    def send(self, c, op, fields=None, **kw):
        c.send_c2s(self.key(op, **kw), fields or {})

    def rec(self, name):
        found = self.server.store.character_by_name(name)
        return found[2] if found else None

    def uid(self, name):
        return self.server.store.character_by_name(name)[1]['uid']

    def set_friends(self, name, friends):
        with self.server.store.lock:
            self.rec(name)['friends'] = list(friends)

    def sync(self, c):
        """C2S 0x2F (what the client sends after every S2C 0x03); the decoded replies."""
        self.send(c, 0x2F)
        return [(p.opcode, c.s2c(p)) for p in c.recv_until_quiet(0.25)]

    def rows(self, fields):
        return [(_name(r['name']), r['channel'], r['friend_id'], r['status'])
                for r in fields['repeat[friend_count]']]

    def login(self, account, password, name, number):
        c = F.FakeClient(self.server)
        self.extra.append(c)
        self.assertEqual(c.login(account, password)['result'], 1)
        c.char_name = name
        c.enter_world(name, port=F.P2P_PORT_BASE + number - 1)
        return c

    def close(self, c):
        c.close()
        self.assertTrue(_wait(lambda: not self.server.messenger.is_online(c.session or {'closed': 1})
                              if c.session is not None else True))
        self.assertTrue(_wait(lambda: c.session is None or c.session.get('closed')))

    def portal(self, c):
        c.send(0x7E, PORTAL_101_TO_102)
        self.assertTrue(_wait(lambda: self.server.world.map_of(c.session) == 102))
        c.recv_until_quiet()

    def make_friends(self):
        self.set_friends('TestHero', ['Watcher'])
        self.set_friends('Watcher', ['TestHero'])

    def only(self, c, op):
        """Exactly one S2C `op` among what `c` got (presence 0x05/0x06 may ride along)."""
        pkts = [p for p in c.recv_until_quiet(0.3)]
        mine = [p for p in pkts if p.opcode == op]
        self.assertEqual(len(mine), 1, f'expected one 0x{op:02X}, got {[hex(o) for o in _ops(pkts)]}')
        return c.s2c(mine[0])

    # ----------------------------------------------------------- F1 list sync ---
    def test_the_friend_list_is_answered_after_every_0x03_even_empty(self):
        """0x2F -> 0x0B {20, 0}: without it the client's capacity stays 0 and Add says
        "Can't be add. Visit Frenaiga."; no 0x7E / 0x78 when there is nothing to list."""
        got = self.sync(self.a)
        self.assertEqual(got, [(0x0B, {'friend_capacity': 20, 'friend_count': 0, 'repeat[friend_count]': []})])
        self.portal(self.a)
        self.assertEqual([op for op, _ in self.sync(self.a)], [0x0B])

    def test_online_and_offline_rows_and_lazy_drop_of_a_deleted_friend(self):
        self.set_friends('TestHero', ['Watcher', 'late', 'Gone'])
        (op, fields), = self.sync(self.a)
        self.assertEqual(op, 0x0B)
        self.assertEqual(self.rows(fields), [('Watcher', 1, 2, 0), ('Late', 0, self.uid('Late'), 1)])
        self.assertEqual(self.rec('TestHero')['friends'], ['Watcher', 'Late'])   # canonical, 'Gone' dropped

    # ------------------------------------------------------------- F2 add ---
    def test_exit2_add_request_accept_and_both_lists(self):
        """Exit criterion 2: A adds B (window 0x179: uid 0) -> 0x0D on B; B accepts -> A gets
        0x0C 1 with B's row, B 0x0C 0x0B with A's; both stored; both 0x0B show the other
        online (x/20); the /f relay now reaches B."""
        self.send(self.a, 0x30, {'target_uid': 0, 'target_name': 'watcher'})
        prompt = self.b.s2c(self.b.expect(0x0D))
        self.assertEqual((prompt['request_id'], _name(prompt['requester_name'])), (1, 'TestHero'))
        self.a.expect_silence(0.1)
        self.send(self.b, 0x31, {'requester_id': 1, 'requester_name': 'TestHero', 'accept': 1})
        mine = self.a.s2c(self.a.expect(0x0C))
        theirs = self.b.s2c(self.b.expect(0x0C))
        self.assertEqual((mine['result'], _name(mine['name']), mine['channel'], mine['friend_id'], mine['status']),
                         (1, 'Watcher', 1, 2, 0))
        self.assertEqual((theirs['result'], _name(theirs['name']), theirs['friend_id'], theirs['status']),
                         (0x0B, 'TestHero', 1, 0))
        self.assertEqual((self.rec('TestHero')['friends'], self.rec('Watcher')['friends']), (['Watcher'], ['TestHero']))
        self.assertEqual(self.rows(self.sync(self.a)[0][1]), [('Watcher', 1, 2, 0)])
        self.assertEqual(self.rows(self.sync(self.b)[0][1]), [('TestHero', 1, 1, 0)])
        text = b'TestHero : hi'
        self.send(self.a, 0x6B, {'recipient_count': 1, 'repeat[recipient_count]': [{'friend_channel': 1, 'friend_id': 2}],
                                 'msg_len': len(text), 'message': text})
        self.assertEqual(P.to_bytes(self.b.s2c(self.b.expect(0x91))['text']), text)

    def test_the_popup_add_carries_the_uid(self):
        self.send(self.b, 0x30, {'target_uid': 1, 'target_name': 'TestHero'})
        self.assertEqual(_name(self.a.s2c(self.a.expect(0x0D))['requester_name']), 'Watcher')
        self.send(self.b, 0x30, {'target_uid': 1, 'target_name': 'TestHero'})   # pending: no second prompt
        self.a.expect_silence(0.15)
        self.b.expect_silence(0.05)

    def test_a_refused_request_and_the_add_refusals(self):
        def refused(target, want, c=None, uid=0):
            c = c or self.a
            self.send(c, 0x30, {'target_uid': uid, 'target_name': target})
            pkt = c.expect(0x0C)
            self.assertEqual(len(pkt.payload), 18)
            got = c.s2c(pkt)
            self.assertEqual((got['result'], _name(got['target_name'])), want)
        refused('Nobody', (0x15, 'Nobody'))                  # unknown name
        refused('Late', (2, 'Late'))                         # offline
        self.set_friends('TestHero', ['Watcher'])
        refused('Watcher', (6, 'Watcher'))                   # already listed
        self.set_friends('TestHero', [f'F{i}' for i in range(20)])
        refused('Watcher', (7, 'Watcher'))                   # own list full (20/20)
        self.set_friends('TestHero', [])
        self.set_friends('Watcher', [f'F{i}' for i in range(20)])
        refused('Watcher', (5, 'Watcher'))                   # their list full
        self.set_friends('Watcher', [])
        self.send(self.b, 0x40, {f'refuse_{k}': int(k == 'friend') for k in
                                 ('whisper', 'exchange', 'party', 'talk', 'friend')})
        self.assertTrue(_wait(lambda: self.b.session.get('refuse', {}).get('friend')))
        refused('Watcher', (4, 'Watcher'))                   # "has turned friend function off"
        self.send(self.a, 0x30, {'target_uid': 0, 'target_name': 'TestHero'})   # oneself: dropped
        self.a.expect_silence(0.15)
        self.b.expect_silence(0.05)

    def test_refuse_answer_and_a_requester_who_left(self):
        self.send(self.a, 0x30, {'target_uid': 0, 'target_name': 'Watcher'})
        self.b.expect(0x0D)
        self.send(self.b, 0x31, {'requester_id': 1, 'requester_name': 'TestHero', 'accept': 0})
        got = self.a.s2c(self.a.expect(0x0C))
        self.assertEqual((got['result'], _name(got['target_name'])), (3, 'Watcher'))
        self.b.expect_silence(0.1)
        self.assertEqual(self.rec('TestHero')['friends'], [])
        # an answer to nothing pending is dropped
        self.send(self.b, 0x31, {'requester_id': 1, 'requester_name': 'TestHero', 'accept': 1})
        self.b.expect_silence(0.15)
        # the requester closes before the answer: "is not on line." to the accepter
        self.send(self.a, 0x30, {'target_uid': 0, 'target_name': 'Watcher'})
        self.b.expect(0x0D)
        self.close(self.a)
        self.b.recv_until_quiet(0.3)                          # 0x06 despawn
        self.send(self.b, 0x31, {'requester_id': 1, 'requester_name': 'TestHero', 'accept': 1})
        got = self.b.s2c(self.b.expect(0x0C))
        self.assertEqual((got['result'], _name(got['target_name'])), (2, 'TestHero'))
        self.assertEqual(self.rec('Watcher')['friends'], [])

    def test_requests_expire_and_die_with_the_target_client(self):
        """A friend request is answerable for FRIEND_REQUEST_TTL (120 s); one whose target
        client closed is gone (its prompt went with it), so a relogged target cannot accept
        it; the requester may ask again."""
        msgr = self.server.messenger
        self.send(self.a, 0x30, {'target_uid': 0, 'target_name': 'Watcher'})
        self.b.expect(0x0D)
        with msgr.lock:
            msgr.requests[2][1].t -= social.FRIEND_REQUEST_TTL
        self.send(self.b, 0x31, {'requester_id': 1, 'requester_name': 'TestHero', 'accept': 1})
        self.b.expect_silence(0.15)
        self.a.expect_silence(0.05)
        self.send(self.a, 0x30, {'target_uid': 0, 'target_name': 'Watcher'})   # asked again: a new prompt
        self.b.expect(0x0D)
        self.close(self.b)
        self.a.recv_until_quiet(0.3)
        self.assertEqual(msgr.requests, {})
        b = self.login('admin', 'admin', 'Watcher', 2)
        self.a.recv_until_quiet(0.3)
        self.send(b, 0x31, {'requester_id': 1, 'requester_name': 'TestHero', 'accept': 1})
        b.expect_silence(0.15)
        self.assertEqual(self.rec('Watcher')['friends'], [])
        # an invite likewise expires after ROOM_INVITE_TTL (60 s)
        self.invite(self.a, b, 2, 'Watcher')
        with msgr.lock:
            msgr.invites[2][1].t -= social.ROOM_INVITE_TTL
        self.send(b, 0x34, {'inviter_id': 1, 'accept': 1})
        b.expect_silence(0.15)
        self.a.expect_silence(0.05)

    # ---------------------------------------------------------- F3 delete ---
    def test_exit2_delete_is_directed(self):
        self.make_friends()
        self.send(self.a, 0x32, {'friend_id': 2, 'friend_name': 'Watcher'})
        got = self.a.s2c(self.a.expect(0x0E))
        self.assertEqual((got['success'], _name(got['name'])), (1, 'Watcher'))
        self.b.expect_silence(0.1)
        self.assertEqual((self.rec('TestHero')['friends'], self.rec('Watcher')['friends']), ([], ['TestHero']))
        self.send(self.a, 0x32, {'friend_id': 2, 'friend_name': 'Watcher'})
        got = self.a.s2c(self.a.expect(0x0E))
        self.assertEqual((got['success'], _name(got['name'])), (0, 'Watcher'))
        self.assertEqual(self.sync(self.a), [(0x0B, {'friend_capacity': 20, 'friend_count': 0,
                                                     'repeat[friend_count]': []})])

    # -------------------------------------------------------- F4 presence ---
    def test_exit2_presence_logoff_relog_and_the_login_line(self):
        """B logs off: A's entry goes grey (0x60 {2, B, 0, 1}, no text). B logs in again: A
        gets 0x60 {2, B, 1, 0}, which the client prints as "<Watcher> has logged in.
        (Friend)" (2008 0x4708C0 / 2009 FUN_0047bd60 case 0x60, colour 0xFF00C800)."""
        self.make_friends()
        self.close(self.b)
        got = self.only(self.a, 0x60)
        self.assertEqual((got['friend_uid'], _name(got['friend_name']), got['channel'], got['presence']),
                         (2, 'Watcher', 0, 1))
        self.assertEqual(self.rows(self.sync(self.a)[0][1]), [('Watcher', 0, 2, 1)])
        b = self.login('admin', 'admin', 'Watcher', 2)
        got = self.only(self.a, 0x60)
        self.assertEqual((got['friend_uid'], _name(got['friend_name']), got['channel'], got['presence']),
                         (2, 'Watcher', 1, 0))
        for build in (B8, B9):                     # always the full 23 B (the watcher has the entry)
            self.assertEqual(len(P.build('0x60', {'friend_uid': 2, 'friend_name': 'Watcher', 'channel': 1,
                                                 'presence': 0}, client_build=build)), 23)
        self.assertEqual(self.rows(self.sync(self.a)[0][1]), [('Watcher', 1, 2, 0)])
        self.assertEqual(self.rows(self.sync(b)[0][1]), [('TestHero', 1, 1, 0)])
        self.assertEqual(self.a.session['uid'], 1)

    def test_a_portal_is_not_a_login_and_status_goes_to_the_watchers_only(self):
        self.make_friends()
        self.portal(self.b)
        self.a.recv_until_quiet(0.2)
        self.assertEqual([p.opcode for p in self.a.recv_until_quiet(0.1)], [])
        # Busy -> 0x60 presence 2 to A, nothing to B (NEVER_REPLY 0x37)
        self.send(self.b, 0x37, {'status': 2})
        got = self.only(self.a, 0x60)
        self.assertEqual((got['channel'], got['presence']), (1, 2))
        self.b.expect_silence(0.1)
        self.assertEqual(self.rows(self.sync(self.a)[0][1]), [('Watcher', 1, 2, 2)])
        self.send(self.b, 0x37, {'status': 9})              # clamped to Online
        self.assertEqual(self.only(self.a, 0x60)['presence'], 0)
        # a server map load resets Busy/AFK (the 0x03 zeroes the client's status too)
        self.send(self.b, 0x37, {'status': 3})
        self.assertEqual(self.only(self.a, 0x60)['presence'], 3)
        self.b.send(0x7E, bytes.fromhex(self._back_portal()))
        self.assertTrue(_wait(lambda: self.server.world.map_of(self.b.session) == 101))
        self.assertEqual(self.b.session['msgr_status'], 0)
        pkts = self.a.recv_until_quiet(0.3)
        self.assertIn(0x60, _ops(pkts))
        self.assertEqual(self.a.s2c([p for p in pkts if p.opcode == 0x60][0])['presence'], 0)

    def _back_portal(self):
        index = next(int(k.split('_')[1]) for k, v in EC.portals().items()
                     if k.startswith('102_') and v[0] == 101)
        return index.to_bytes(4, 'little').hex()

    def test_a_shadowed_gm_friend_stays_offline_to_a_player(self):
        self.make_friends()
        self.close(self.b)
        self.a.recv_until_quiet(0.3)
        with self.server.store.lock:
            self.rec('Watcher').update(gm=1, gm_hidden=1)
        self.login('admin', 'admin', 'Watcher', 2)
        self.assertNotIn(0x60, _ops(self.a.recv_until_quiet(0.3)))
        self.assertEqual(self.rows(self.sync(self.a)[0][1]), [('Watcher', 0, 2, 1)])

    # ---------------------------------------------------------- F5 rooms ---
    def invite(self, host, guest, guest_uid, guest_name):
        self.send(host, 0x33, {'target_uid': guest_uid, 'target_name': guest_name})
        return guest.s2c(guest.expect(0x10))

    def members(self, fields):
        return [_name(r['member_name']) for r in fields['repeat[member_count]']]

    def test_exit3_room_invite_lines_and_portal_leave(self):
        """Exit criterion 3: 0x10 "<A> would like to have a chat with you" on B; accept ->
        both get 0x0F 1 with themselves first (each prints "<other> has entered."); a line
        from either reaches both room windows (0x61, sender included); B's portal -> A gets
        0x62 {B} and B's later lines are dropped."""
        got = self.invite(self.a, self.b, 2, 'Watcher')
        self.assertEqual((got['inviter_id'], _name(got['inviter_name'])), (1, 'TestHero'))
        self.send(self.b, 0x34, {'inviter_id': 1, 'accept': 1})
        ra, rb = self.a.s2c(self.a.expect(0x0F)), self.b.s2c(self.b.expect(0x0F))
        self.assertEqual((ra['subtype'], ra['member_count'], self.members(ra)), (1, 2, ['TestHero', 'Watcher']))
        self.assertEqual((rb['room_id'], self.members(rb)), (ra['room_id'], ['Watcher', 'TestHero']))
        self.assertNotEqual(ra['room_id'], 0)
        assume = {'client.messenger_room_id != 0': True}
        for speaker, text in ((self.a, b'hi there'), (self.b, b'x' * 70)):
            self.send(speaker, 0x35, {'text_len': len(text), 'text': text})
            for c in (self.a, self.b):
                line = c.s2c(c.expect(0x61), assume)
                self.assertEqual((_name(line['sender_name']), line['message']),
                                 (speaker.char_name, text[:chat.ROOM_TEXT_MAX]))
        self.portal(self.b)
        pkts = self.a.recv_until_quiet(0.3)
        left = [self.a.s2c(p, {'messenger_room_id != 0': True}) for p in pkts if p.opcode == 0x62]
        self.assertEqual([_name(r['member_name']) for r in left], ['Watcher'])
        self.assertIsNone(self.b.session.get('room_id'))
        self.send(self.b, 0x35, {'text_len': 2, 'text': b'yo'})
        self.a.expect_silence(0.15)
        self.b.expect_silence(0.05)
        self.assertEqual(len(self.server.messenger.rooms[ra['room_id']]), 1)   # A can invite again

    def test_a_third_member_joins_and_disconnect_leaves(self):
        carol = self.login('carol', 'carol', 'Carol', 3)
        self.mc.drain()
        self.invite(self.a, self.b, 2, 'Watcher')
        self.send(self.b, 0x34, {'inviter_id': 1, 'accept': 1})
        room = self.a.s2c(self.a.expect(0x0F))['room_id']
        self.b.expect(0x0F)
        self.invite(self.a, carol, 0, 'Carol')                  # by name (window 0x178)
        self.send(carol, 0x34, {'inviter_id': 1, 'accept': 1})
        joined = carol.s2c(carol.expect(0x0F))
        self.assertEqual((joined['room_id'], self.members(joined)), (room, ['Carol', 'TestHero', 'Watcher']))
        for c in (self.a, self.b):
            got = c.s2c(c.expect(0x0F))
            self.assertEqual((got['member_count'], self.members(got)), (1, ['Carol']))
        self.close(self.b)
        for c in (self.a, carol):
            pkts = c.recv_until_quiet(0.3)
            left = [c.s2c(p, {'messenger_room_id != 0': True}) for p in pkts if p.opcode == 0x62]
            self.assertEqual([_name(r['member_name']) for r in left], ['Watcher'])
        self.send(carol, 0x36)                                  # leaves by closing the window
        self.assertEqual(_name(self.a.s2c(self.a.expect(0x62), {'messenger_room_id != 0': True})['member_name']),
                         'Carol')
        carol.expect_silence(0.1)
        self.send(carol, 0x36)                                  # in no room: accepted, silent
        carol.expect_silence(0.1)

    def test_room_refusals(self):
        def refused(c, target, uid, want):
            self.send(c, 0x33, {'target_uid': uid, 'target_name': target})
            got = c.s2c(c.expect(0x0F))
            self.assertEqual((got['subtype'], _name(got['target_name'])), want)
        refused(self.a, 'Late', 0, (8, 'Late'))                  # offline
        refused(self.a, '', 0, (8, ''))                          # the empty name window 0x178 can send
        self.send(self.a, 0x33, {'target_uid': 1, 'target_name': 'TestHero'})   # oneself: dropped
        self.a.expect_silence(0.1)
        self.send(self.b, 0x40, {'refuse_talk': 1})
        self.assertTrue(_wait(lambda: self.b.session.get('refuse', {}).get('talk')))
        refused(self.a, 'Watcher', 2, (4, 'Watcher'))           # "is rejecting chatting."
        self.send(self.b, 0x40, {})
        self.assertTrue(_wait(lambda: not self.b.session.get('refuse', {}).get('talk')))
        self.invite(self.a, self.b, 2, 'Watcher')
        self.send(self.b, 0x34, {'inviter_id': 1, 'accept': 0})
        got = self.a.s2c(self.a.expect(0x0F))
        self.assertEqual((got['subtype'], _name(got['target_name'])), (3, 'Watcher'))
        self.send(self.b, 0x34, {'inviter_id': 1, 'accept': 1})   # nothing pending any more
        self.b.expect_silence(0.1)
        self.invite(self.a, self.b, 2, 'Watcher')
        self.send(self.b, 0x34, {'inviter_id': 1, 'accept': 1})
        self.a.expect(0x0F)
        self.b.expect(0x0F)
        carol = self.login('carol', 'carol', 'Carol', 3)
        self.mc.drain()
        refused(carol, 'Watcher', 2, (6, 'Watcher'))             # already in a room
        self.send(carol, 0x35, {'text_len': 2, 'text': b'hi'})   # no room: dropped
        carol.expect_silence(0.1)
        with self.server.messenger.lock:                          # a full room (10 members)
            room = self.server.messenger.room_of(self.a.session)
            room.extend({'name': f'x{i}'} for i in range(8))
        refused(self.a, 'Carol', 3, (5, 'Carol'))

    # --------------------------------------------------------- F6-F8 mentor ---
    def test_exit7_mentor_link_block_list_and_exp_share(self):
        """Exit criterion 7: TestHero (Novice) registers Watcher (class 1) -> 0x7A {0x64,
        Watcher, 2} on A and 0x7B {1, 100, TestHero} on B ("(Menti)"); A's 0x03 carries the
        mentor block; B's 0x2F lists A in 0x7E; A's kill gives B 0x7F {share, menti 1}."""
        self.send(self.a, 0x5C, {'mentor_name': 'watcher'})
        got = self.a.s2c(self.a.expect(0x7A))
        self.assertEqual((got['result'], _name(got['mentor_name']), got['mentor_uid']), (0x64, 'Watcher', 2))
        got = self.b.s2c(self.b.expect(0x7B))
        self.assertEqual((got['mentee_uid'], got['channel'], _name(got['mentee_name'])), (1, 100, 'TestHero'))
        self.assertEqual((self.rec('TestHero')['mentor'], self.rec('Watcher')['mentees']), ('Watcher', ['TestHero']))
        got = dict(self.sync(self.b))
        self.assertEqual(got[0x7E], {'count': 1, 'repeat[count]': [{'channel': 100, 'mentee_name': 'TestHero',
                                                                    'mentee_uid': 1}]})
        # the mentee's map load carries the mentor (0x03 mentor block, online = channel 1)
        self.a.send(0x7E, PORTAL_101_TO_102)
        pkts = self.a.recv_until_quiet(0.4)
        self.assertTrue(_wait(lambda: self.server.world.map_of(self.a.session) == 102))
        state = self.a.s2c([p for p in pkts if p.opcode == 0x03][0])
        self.assertEqual((state['mentor_id'], _name(state['mentor_name']), state['mentor_channel']),
                         (2, 'Watcher', 1))
        # a kill by the mentee: the killer gets its 0x21, the mentor 0x7F (+10 %) via the tick
        self.b.recv_until_quiet(0.2)
        mob = next(m for m in self.a.session['monsters'].values() if m.alive)
        for _ in range(50):
            if not mob.alive:
                break
            self.server._memory_melee(mob.x, mob.y, uid=1)
        self.assertFalse(mob.alive)
        exp0 = self.rec('Watcher')['exp']
        self.server.ticks.run_due()
        share = max(1, mob.exp * 10 // 100)
        got = self.b.s2c(self.b.expect(0x7F))
        self.assertEqual(got, {'exp_delta': share, 'menti_id': 1})
        self.assertEqual(self.rec('Watcher')['exp'], exp0 + share)
        self.assertIn(0x21, _ops(self.a.recv_until_quiet(0.3)))

    def test_mentor_refusals_and_removal(self):
        self.send(self.a, 0x5C, {'mentor_name': 'Late'})
        pkt = self.a.expect(0x7A)
        self.assertEqual(len(pkt.payload), 18)                           # no uid on an error
        self.assertEqual(self.a.s2c(pkt)['result'], 0x66)                # "can not be found."
        carol = self.login('carol', 'carol', 'Carol', 3)
        self.mc.drain()
        self.send(self.a, 0x5C, {'mentor_name': 'Carol'})
        self.assertEqual(self.a.s2c(self.a.expect(0x7A))['result'], 0x65)   # a Novice
        self.send(self.b, 0x5C, {'mentor_name': 'TestHero'})             # B is no Novice: dropped
        self.b.expect_silence(0.1)
        self.send(self.a, 0x5C, {'mentor_name': 'Watcher'})
        self.a.expect(0x7A)
        self.b.expect(0x7B)
        self.send(self.a, 0x5C, {'mentor_name': 'Watcher'})             # already has one: dropped
        self.a.expect_silence(0.1)
        self.send(self.a, 0x50)
        self.assertEqual(self.b.s2c(self.b.expect(0x7C)), {'mentee_uid': 1})
        self.a.expect_silence(0.1)
        self.assertEqual((self.rec('TestHero')['mentor'], self.rec('Watcher')['mentees']), (None, []))
        self.send(self.a, 0x50)                                           # none left: dropped
        self.b.expect_silence(0.1)
        carol.expect_silence(0.05)

    def test_mentor_and_mentee_presence(self):
        with self.server.store.lock:
            self.rec('TestHero')['mentor'] = 'Watcher'
            self.rec('Watcher')['mentees'] = ['TestHero']
        self.close(self.b)                                                # the mentor leaves
        got = self.only(self.a, 0x7D)
        self.assertEqual(got, {'mentor_uid': 2, 'channel_or_state': 0x66})
        b = self.login('admin', 'admin', 'Watcher', 2)
        self.assertEqual(self.only(self.a, 0x7D), {'mentor_uid': 2, 'channel_or_state': 100})
        self.close(self.a)                                                # the mentee leaves
        self.assertEqual(self.only(b, 0x7C), {'mentee_uid': 1})
        a = self.login('test', 'test', 'TestHero', 1)
        got = self.only(b, 0x7B)
        self.assertEqual((got['mentee_uid'], got['channel'], _name(got['mentee_name'])), (1, 100, 'TestHero'))
        self.assertTrue(a.session['msgr_online'])

    # ----------------------------------------------------------- F9 memos ---
    def note(self, c, to, text, item=NOTE):
        self.send(c, 0x4B, {'item_id': item, 'recipient_name': to, 'contents': text})

    def memos_in(self, got):
        return [(_name(r['sender_name']), P.to_bytes(r['text'])) for op, f in got if op == 0x78
                for r in f['repeat[count]']]

    def test_exit4_note_to_an_offline_player_is_listed_at_login_until_deleted(self):
        """Exit criterion 4: A's note to offline Late -> 0x77 {1, serial} ("You successfully
        sent the message."); Late logs in: its 0x2F lists the memo (0x78) and again after
        every map load, until its memo window deletes it (C2S 0x44)."""
        self.note(self.a, 'late', b'see you')
        got = self.a.s2c(self.a.expect(0x77))
        self.assertEqual(got, {'result': 1, 'cash_item_serial': 0})       # DEV_FREE_NOTES: no cash inventory
        self.assertEqual([(m['from'], m['text']) for m in self.rec('Late')['memos']], [('TestHero', 'see you')])
        late = self.login('late', 'late', 'Late', 3)
        self.mc.drain()
        got = self.sync(late)
        self.assertEqual([op for op, _ in got], [0x0B, 0x78])
        self.assertEqual(self.memos_in(got), [('TestHero', b'see you')])
        rec = [f for op, f in got if op == 0x78][0]['repeat[count]'][0]
        self.assertEqual(rec['year'], time.localtime().tm_year)
        self.portal(late)
        self.assertEqual(self.memos_in(self.sync(late)), [('TestHero', b'see you')])   # kept
        self.send(late, 0x44)
        late.expect_silence(0.15)                                          # NEVER_REPLY 0x44
        self.assertEqual(self.rec('Late')['memos'], [])
        self.assertEqual([op for op, _ in self.sync(late)], [0x0B])

    def test_a_note_to_an_online_player_arrives_at_once_and_the_delete_guard(self):
        self.sync(self.b)                                                  # B's view is synced
        self.note(self.a, 'Watcher', b'hello')
        self.assertEqual(self.a.s2c(self.a.expect(0x77))['result'], 1)
        got = self.b.s2c(self.b.expect(0x78))
        self.assertEqual((got['count'], _name(got['repeat[count]'][0]['sender_name'])), (1, 'TestHero'))
        # a memo stored behind the client's back (not shown) survives the 0x44
        with self.server.store.lock:
            social.add_memo(self.rec('Watcher'), 'Late', 'unseen')
        self.send(self.b, 0x44)
        self.b.expect_silence(0.1)
        self.assertEqual([m['text'] for m in self.rec('Watcher')['memos']], ['unseen'])
        # between a server 0x03 and the client's 0x2F nothing is pushed: the 0x2F brings it once
        self.portal(self.b)
        self.a.recv_until_quiet(0.2)                                       # B's 0x06 despawn
        self.note(self.a, 'Watcher', b'after portal')
        self.a.expect(0x77)
        self.b.expect_silence(0.15)
        self.send(self.b, 0x44)                                           # this view showed nothing
        self.assertEqual(len(self.rec('Watcher')['memos']), 2)
        self.assertEqual(self.memos_in(self.sync(self.b)), [('Late', b'unseen'), ('TestHero', b'after portal')])

    def test_note_refusals_and_the_gift_thank_you(self):
        for to, text, item, why in (('Nobody', b'x', NOTE, 'unknown'), ('TestHero', b'x', NOTE, 'own name'),
                                    ('Watcher', b'', NOTE, 'empty'), ('Watcher', b'x', 5, 'not a note')):
            with self.subTest(why):
                self.note(self.a, to, text, item)
                pkt = self.a.expect(0x77)
                self.assertEqual((len(pkt.payload), self.a.s2c(pkt)['result']), (1, 0))
        self.assertEqual(self.rec('Watcher')['memos'], [])
        # 9999: a memo for the gift's sender, no item, never a reply (no wait box)
        self.send(self.a, 0x4B, {'note_item_id': 9999, 'recipient_name': 'Late', 'message': 'thanks!'}, gift=True)
        self.a.expect_silence(0.15)
        self.assertEqual([(m['from'], m['text']) for m in self.rec('Late')['memos']], [('TestHero', 'thanks!')])
        self.send(self.a, 0x4B, {'note_item_id': 9999, 'recipient_name': 'Nobody', 'message': 'x'}, gift=True)
        self.a.expect_silence(0.15)
        # DEV_FREE_NOTES off: no cash inventory owns a note before P8
        self.server.config['DEV_FREE_NOTES'] = False
        self.note(self.a, 'Late', b'x')
        self.assertEqual(self.a.s2c(self.a.expect(0x77))['result'], 0)

    def test_dev_note_mail_and_msgr(self):
        self.a.session['gm'] = 1
        line = b'!note 2'
        self.send(self.a, 0x03, {'msg_len': len(line), 'message': line})
        cash, notice = self.a.expect(0x6F, 0x15)
        rec = self.a.s2c(cash)
        self.assertEqual((rec['count'], rec['repeat[count]'][0]['item_id'], rec['repeat[count]'][0]['quantity']),
                         (1, NOTE, 2))
        if self.build == B9:
            self.assertEqual(rec['mode'], 0)
        self.note(self.a, 'Late', b'with a real note')
        self.assertEqual(self.a.s2c(self.a.expect(0x77))['cash_item_serial'], W.GameServer.DEV_NOTE_SERIAL)
        self.assertEqual(self.a.session['dev_notes'][NOTE][1], 1)
        self.sync(self.b)
        line = b'!mail Watcher from the GM'
        self.send(self.a, 0x03, {'msg_len': len(line), 'message': line})
        self.assertIn(b'delivered now', P.to_bytes(self.a.s2c(self.a.expect(0x15))['text']))
        self.assertEqual(self.memos_in([(0x78, self.b.s2c(self.b.expect(0x78)))]), [('TestHero', b'from the GM')])
        self.make_friends()
        line = b'!msgr'
        self.send(self.a, 0x03, {'msg_len': len(line), 'message': line})
        lines = [P.to_bytes(self.a.s2c(p)['text']) for p in self.a.recv_until_quiet(0.3)]
        self.assertTrue(any(b'friends 1/20: Watcher(on)' in t for t in lines), lines)

    # ---------------------------------------------------------- F12 slots ---
    def test_friend_slot_expansion(self):
        gold = self.rec('TestHero')['gold']
        with self.server.store.lock:
            self.rec('TestHero')['gold'] = 100000
        self.send(self.a, 0x73)
        self.assertEqual(self.a.s2c(self.a.expect(0x9D)), {'result': 1, 'friend_capacity': 25, 'gold': 50000})
        self.send(self.a, 0x73)
        pkt = self.a.expect(0x9D)
        self.assertEqual((len(pkt.payload), self.a.s2c(pkt)), (10, {'result': 1, 'friend_capacity': 30, 'gold': 0}))
        self.send(self.a, 0x73)                                             # no gold left
        pkt = self.a.expect(0x9D)
        self.assertEqual((len(pkt.payload), self.a.s2c(pkt)), (1, {'result': 0}))
        self.assertEqual(self.sync(self.a)[0][1]['friend_capacity'], 30)
        with self.server.store.lock:
            self.rec('TestHero').update(friend_capacity=50, gold=10 ** 6)
        self.send(self.a, 0x73)                                             # "Maximum of 50."
        self.assertEqual(self.a.s2c(self.a.expect(0x9D)), {'result': 0})
        self.assertEqual((self.rec('TestHero')['friend_capacity'], self.rec('TestHero')['gold']), (50, 10 ** 6))
        self.assertTrue(gold >= 0)


class Messenger2008(_Messenger, unittest.TestCase):
    build = B8


class Messenger2009(_Messenger, unittest.TestCase):
    build = B9

    def test_a_2009_relogin_announces_no_logoff(self):
        """The 2009 client reconnects with its session key (channel change): the old session
        is superseded, so the watchers get no 0x60 offline - only the new entry's login."""
        self.make_friends()
        old = self.b.session
        c = F.FakeClient(self.server)
        self.extra.append(c)
        self.assertEqual(c.login('admin', 'admin', session_key=old['session_key'])['result'], 1)
        self.assertTrue(_wait(lambda: old.get('closed')))
        pkts = self.a.recv_until_quiet(0.3)
        self.assertNotIn(0x60, _ops(pkts))
        c.enter_world('Watcher', port=F.P2P_PORT_BASE + 1)
        got = [self.a.s2c(p) for p in self.a.recv_until_quiet(0.3) if p.opcode == 0x60]
        self.assertEqual([(g['friend_uid'], g['presence']) for g in got], [(2, 0)])


if __name__ == '__main__':
    unittest.main(verbosity=1)
