#!/usr/bin/env python3
"""
test_blacklist.py - P12 part 1 (ROADMAP_2009_ADDENDUM P12; systems_2009/blacklist_channels.md
Part A), both client builds
==========================================================================================
  arch09-resync-bundle  resync.py: the default order per build, register / replace / before /
                        STOP, a failing step does not cost the next, the 2009 enter-world +
                        0x2F / 0x63 / 0x8A replies in bundle order, a P14-style guild hook;
  bl-1  char['blacklist'] (<= 10, uid != 0, cid): normalizer, the 0xBD / 0xBE / 0xBF
        encodings (the D.1 I-1 bytes), the idempotent migration with its one-time
        accounts.json.bak-pre-p12, hand-edited rows resolved, 0xBD after every 0x2F (portal,
        relog), a deleted character's row dropped at the next 0x2F;
  bl-2  C2S 0x93 -> 0xBE (every F-B2 refusal is the 1-byte {0}, one row per name), C2S 0x94 ->
        0xBF (idempotent, by name; {0} for an empty name);
  bl-3  BLACKLIST_FILTER silent (default) / refuse / client over whisper, map chat, trade,
        party, friend, chat invite, mentor, /f; no pending state for a blocked request (no
        phantom "busy"); the reverse direction (F-B5); the rename hook (C4) re-sends 0xBD;
        a listed character's SIBLING on the id-matched friend request / chat invite (P12
        review); `!blacklist filter <mode>` switches the mode at run time (live triage BL);
  2008  no blacklist anywhere: C2S 0x93 / 0x94 stay room-host dead code, no 0xBD, no filter.

Fake clients (fakeclient.MultiClient); no port is bound and the live accounts.json is never
opened (temp copies; the module checks its hash).
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

import fakeclient as F  # noqa: E402
import blacklist as BL  # noqa: E402
import packets as P  # noqa: E402
import resync as RS  # noqa: E402
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
PORTAL = {B8: '0x42F76B/0x7E', B9: '0x431284/0x7E'}
NICK = 1895                              # the rename ticket (P8 C4 rig)

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
            'accounts.json changed during test_blacklist.py (tests must only use temp copies)'


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _ops(pkts):
    return [p.opcode for p in pkts]


def _text(value):
    return P.to_bytes(value).split(b'\x00', 1)[0].decode('cp949', 'replace')


def _char(name, cls=0, gm=0):
    return {'name': name, 'level': 1, 'class': cls, 'map': 101, 'x': 1411.0, 'y': 714.0, 'hp': 100, 'mp': 50,
            'gm': gm}


def _accounts():
    return {'test': {'password': 'test', 'characters': [_char('TestHero')]},
            'admin': {'password': 'admin', 'characters': [_char('Watcher')]},
            'carol': {'password': 'carol', 'characters': [_char('Carol')]},
            'dave': {'password': 'dave', 'characters': [_char('Dave'), _char('DaveAlt')]}}


# ================================================================== model ===
class Model(unittest.TestCase):
    def test_ensure_normalizes_dedupes_and_caps_at_ten(self):
        char = {'name': 'TestHero', 'blacklist': [
            {'name': 'Bob', 'uid': 2, 'cid': 7}, {'name': 'bob', 'uid': 2}, 'Carol', 5, {'name': ''},
            {'name': 'X' * 20, 'uid': -3}, {'name': 'Dup', 'uid': 4, 'cid': 7}, {'name': 'Late', 'uid': 'x'}]}
        rows = BL.ensure(char)
        self.assertEqual(rows, [{'name': 'Bob', 'uid': 2, 'cid': 7}, {'name': 'Carol', 'uid': 0},
                                {'name': 'X' * 16, 'uid': 0}, {'name': 'Late', 'uid': 0}])
        self.assertEqual(BL.ensure(dict(char)), rows)                              # idempotent
        many = {'blacklist': [{'name': f'P{i}', 'uid': i + 1} for i in range(14)]}
        self.assertEqual(len(BL.ensure(many)), BL.BLACKLIST_MAX)
        self.assertEqual(BL.ensure({}), [])
        self.assertEqual(BL.ensure({'blacklist': 'junk'}), [])

    def test_wire_forms_and_the_id_0_guard(self):
        # D.1 I-1: Bob (2) and Carol (3), id first then str[17]
        fields = BL.list_fields([{'name': 'Bob', 'uid': 2}, {'name': 'Carol', 'uid': 3}])
        self.assertEqual(P.build('0xBD', fields, client_build=B9), bytes.fromhex(
            '02 02000000 426f62' + '00' * 14 + ' 03000000 4361726f6c' + '00' * 12))
        self.assertEqual(P.build('0xBD', BL.list_fields([]), client_build=B9), b'\x00')
        ten = BL.list_fields([{'name': f'Name{i}', 'uid': 100 + i} for i in range(12)])
        body = P.build('0xBD', ten, client_build=B9)
        self.assertEqual((body[0], len(body)), (10, 1 + 21 * 10))
        self.assertTrue(all(body[1 + 21 * i + 4 + 16] == 0 for i in range(10)))   # NUL-terminated names
        for bad in (0, -1, None, 1 << 32):
            with self.assertRaises(ValueError):
                BL.list_fields([{'name': 'Zzz', 'uid': bad}])
            with self.assertRaises(ValueError):
                BL.add_result({'name': 'Zzz', 'uid': bad})
        self.assertEqual(P.build('0xBE', BL.add_result(), client_build=B9), b'\x00')
        self.assertEqual(P.build('0xBE', BL.add_result({'name': 'Dave', 'uid': 4}), client_build=B9),
                         bytes.fromhex('01 04000000 44617665' + '00' * 13))
        self.assertEqual(P.build('0xBF', BL.remove_result('Bob'), client_build=B9),
                         bytes.fromhex('01 426f62' + '00' * 14))
        self.assertEqual(P.build('0xBF', BL.remove_result(''), client_build=B9), b'\x00')
        for key in ('0xBD', '0xBE', '0xBF'):                   # the 2008 client has no such packet
            with self.assertRaises(KeyError):
                P.build(key, {'count': 0, 'result': 0}, client_build=B8)

    def test_resolve_follows_the_cid_and_drops_the_gone(self):
        accounts = {'a': {'uid': 1, 'characters': [{'name': 'TestHero', 'cid': 1}, {'name': 'Alt', 'cid': 5}]},
                    'b': {'uid': 2, 'characters': [{'name': 'Renamed', 'cid': 2}]},
                    'c': {'uid': 3, 'characters': [{'name': 'Carol', 'cid': 3}]}}
        hero = accounts['a']['characters'][0]
        hero['blacklist'] = [{'name': 'Bob', 'uid': 9, 'cid': 2},       # renamed since: follows the cid
                             {'name': 'carol', 'uid': 0},              # hand-edited: resolved by name
                             {'name': 'Ghost', 'uid': 4, 'cid': 4},    # deleted (cid gone): dropped
                             {'name': 'Nobody', 'uid': 6},             # no such character: dropped
                             {'name': 'Alt', 'uid': 1},                # its own account: dropped
                             {'name': 'TestHero', 'uid': 1}]           # itself: dropped
        changes = BL.resolve_all(accounts)
        self.assertEqual(hero['blacklist'], [{'name': 'Renamed', 'uid': 2, 'cid': 2},
                                             {'name': 'Carol', 'uid': 3, 'cid': 3}])
        self.assertTrue(changes)
        self.assertEqual(BL.resolve_all(accounts), [])                  # idempotent
        # a cid that is gone never re-matches a new character that took the old name
        accounts['d'] = {'uid': 7, 'characters': [{'name': 'Ghost', 'cid': 8}]}
        hero['blacklist'].append({'name': 'Ghost', 'uid': 4, 'cid': 4})
        BL.resolve_all(accounts)
        self.assertNotIn('Ghost', [r['name'] for r in hero['blacklist']])

    def test_rename_references_and_lists(self):
        st = type('St', (), {})()
        hero = {'name': 'TestHero', 'cid': 1, 'blacklist': [{'name': 'Bob', 'uid': 2, 'cid': 2}]}
        other = {'name': 'Carol', 'cid': 3, 'blacklist': [{'name': 'Bob', 'uid': 2}]}   # no cid: by name
        bob = {'name': 'Bobby', 'cid': 2}
        st.accounts = {'a': {'characters': [hero, other]}, 'b': {'characters': [bob]}}
        self.assertEqual(BL.rename_references(st, 'b', bob, 'Bob', 'Bobby'), 2)
        self.assertEqual((hero['blacklist'][0]['name'], other['blacklist'][0]['name']), ('Bobby', 'Bobby'))
        self.assertTrue(BL.lists(hero, bob))
        self.assertFalse(BL.lists(hero, {'name': 'Bobby', 'cid': 9}))  # same name, another character
        self.assertTrue(BL.lists(other, {'name': 'bobby', 'cid': 9}))  # a row without cid: by name
        self.assertFalse(BL.lists(None, bob))

    def test_builds_modes_and_config(self):
        self.assertTrue(BL.supported(B9))
        self.assertFalse(BL.supported(B8))
        self.assertEqual(cfgmod.defaults().BLACKLIST_FILTER, BL.MODE_SILENT)
        self.assertEqual(cfgmod.BLACKLIST_FILTERS, BL.FILTER_MODES)
        for mode in BL.FILTER_MODES:
            self.assertEqual(cfgmod.from_dict({'BLACKLIST_FILTER': mode}).BLACKLIST_FILTER, mode)
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.from_dict({'BLACKLIST_FILTER': 'loud'})


# ============================================================== migration ===
class MigrationP12(unittest.TestCase):
    """char['blacklist'] arrives through an idempotent migration that first writes the one-time
    accounts.json.bak-pre-p12 (hard rule for a persisted-schema change)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ws_p12_store_')
        self.path = os.path.join(self.tmp, 'accounts.json')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pre_p12_file(self, build):
        st = S.Store(self.path, client_build=build).load()            # a current file
        with st.lock:
            for acc in st.accounts.values():
                for ch in acc['characters']:
                    del ch[BL.CHAR_FIELD]
        st.save_now()
        for suffix in S.BACKUP_SUFFIXES + (S.BACKUP_SUFFIX_2009,):
            path = self.path + suffix
            if suffix == S.BACKUP_SUFFIX_P12:
                if os.path.exists(path):
                    os.remove(path)
            elif not os.path.exists(path):
                open(path, 'wb').close()                                # earlier phases: done
        with open(self.path, 'rb') as f:
            return f.read()

    def test_the_list_is_added_once_with_its_backup(self):
        self.assertEqual(S.BACKUP_SUFFIX_P12, '.bak-pre-p12')
        self.assertIn(S.BACKUP_SUFFIX_P12, S.BACKUP_SUFFIXES)
        for build in (B8, B9):
            with self.subTest(build=build):
                original = self._pre_p12_file(build)
                st = S.Store(self.path, client_build=build).load()
                self.assertEqual(st.migration_changes, ['test/TestHero: blacklist created'])
                with open(self.path + '.bak-pre-p12', 'rb') as f:
                    self.assertEqual(f.read(), original)
                with open(self.path, encoding='utf-8') as f:
                    self.assertEqual(json.load(f)['test']['characters'][0]['blacklist'], [])
                again = S.Store(self.path, client_build=build).load()
                self.assertEqual((again.migration_changes, again.saves), ([], 0))
                # a second pre-P12 state never overwrites the first backup
                self._pre_p12_file(build)
                with open(self.path + '.bak-pre-p12', 'wb') as f:
                    f.write(b'first')
                S.Store(self.path, client_build=build).load()
                with open(self.path + '.bak-pre-p12', 'rb') as f:
                    self.assertEqual(f.read(), b'first')
                os.remove(self.path + '.bak-pre-p12')

    def test_hand_edited_rows_are_resolved_or_dropped_and_never_uid_0(self):
        st = S.Store(self.path).load()
        with st.lock:
            st.accounts['admin']['characters'].append(st.new_character('Bob', s10=1, s1=1, s6=2, s5=2, s9=2,
                                                                       stats=(3, 2, 1, 3)))
            st.find_character('test', 'TestHero')['blacklist'] = [{'name': 'bob'}, {'name': 'Nobody', 'uid': 5}]
        st.save_now()
        st = S.Store(self.path).load()
        bob = st.find_character('admin', 'Bob')
        self.assertEqual(st.find_character('test', 'TestHero')['blacklist'],
                         [{'name': 'Bob', 'uid': 2, 'cid': bob['cid']}])
        with open(self.path, encoding='utf-8') as f:
            rows = [r for acc in json.load(f).values() for c in acc['characters'] for r in c['blacklist']]
        self.assertTrue(rows and all(r['uid'] != 0 for r in rows))
        self.assertEqual(S.Store(self.path).load().migration_changes, [])

    def test_new_characters_carry_the_list_and_the_rename_rewriter_is_installed(self):
        st = S.Store(self.path).load()
        ch = st.new_character('Nova', s10=1, s1=1, s6=2, s5=2, s9=2, stats=(3, 2, 1, 3))
        self.assertEqual(ch['blacklist'], [])
        self.assertEqual(S.migrate_character(ch), [])
        self.assertEqual(st.rename_rewriters, [S.rewrite_social_references, BL.rename_references])


# ================================================================ resync ===
class ResyncBundle(unittest.TestCase):
    def test_the_default_order_per_build(self):
        srv = type('Srv', (), {'client_build': B9})()
        rs = RS.install_defaults(RS.Resync(srv))
        self.assertEqual(rs.order(B9), {
            RS.STAGE_SPAWN: ['vitals', 'owned_cash', 'gift_queue'],
            RS.STAGE_FRIENDS: ['friends', 'blacklist'],
            RS.STAGE_CARDS: ['cards', 'channel', 'events'],
            RS.STAGE_GUILD: ['guild', 'gm_tag']})
        self.assertEqual(rs.order(B8), {
            RS.STAGE_SPAWN: ['vitals', 'owned_cash', 'gift_queue'],
            RS.STAGE_FRIENDS: ['friends'],
            RS.STAGE_CARDS: ['cards', 'channel', 'events'],
            RS.STAGE_GUILD: []})

    def test_register_replace_before_after_stop_and_failures(self):
        srv = type('Srv', (), {'client_build': B9})()
        rs = RS.Resync(srv)
        seen = []

        def step(name, result=None):
            def fn(server, sock, session, ctx):
                seen.append((name, ctx.get('x')))
                return result
            return fn

        rs.register(RS.STAGE_GUILD, 'guild', step('guild'))
        rs.register(RS.STAGE_GUILD, 'gm_tag', step('gm_tag'))
        # the P14 hook: the guild reply replaced in place, sub 4 / 0xBB before the GM tag
        rs.register(RS.STAGE_GUILD, 'guild', step('sub3'), replace=True)
        rs.register(RS.STAGE_GUILD, 'guild_apps', step('sub4'), before='gm_tag')
        rs.register(RS.STAGE_GUILD, 'guild_boards', step('0xBB'), after='guild_apps')
        rs.register(RS.STAGE_GUILD, 'b8_only', step('2008'), builds={B8})
        self.assertEqual(rs.run(RS.STAGE_GUILD, None, {}, x=1), ['guild', 'guild_apps', 'guild_boards', 'gm_tag'])
        self.assertEqual(seen, [('sub3', 1), ('sub4', 1), ('0xBB', 1), ('gm_tag', 1)])
        for bad in (lambda: rs.register(RS.STAGE_GUILD, 'guild', step('x')),
                    lambda: rs.register('nope', 'x', step('x'))):
            with self.assertRaises(ValueError):
                bad()
        with self.assertRaises(KeyError):
            rs.register(RS.STAGE_GUILD, 'x', step('x'), before='missing')
        # STOP ends the stage; a raising step is logged and the next one still runs
        seen.clear()

        def boom(server, sock, session, ctx):
            raise RuntimeError('boom')

        rs.register(RS.STAGE_CARDS, 'first', boom)
        rs.register(RS.STAGE_CARDS, 'second', step('second', RS.STOP))
        rs.register(RS.STAGE_CARDS, 'third', step('third'))
        with self.assertLogs('WS', logging.ERROR):
            self.assertEqual(rs.run(RS.STAGE_CARDS, None, {}), ['first', 'second'])
        self.assertEqual(seen, [('second', None)])

        def gone(server, sock, session, ctx):
            raise OSError('closed')

        rs.register(RS.STAGE_FRIENDS, 'gone', gone)
        with self.assertRaises(OSError):                       # a closed connection still propagates
            rs.run(RS.STAGE_FRIENDS, None, {})


# ================================================================== rigs ===
class _Rig:
    """TestHero (c:1, uid 1), Watcher (c:2, uid 2) and Carol (c:3, uid 3) in world on map 101;
    Dave / DaveAlt (uid 4) offline."""
    build = B9
    mode = None
    players = (('test', 'test', 'TestHero'), ('admin', 'admin', 'Watcher'), ('carol', 'carol', 'Carol'))

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        self.tmp = tempfile.mkdtemp(prefix=f'ws_p12_{self.build}_')
        extra = {'BLACKLIST_FILTER': self.mode} if self.mode else {}
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False,
                                **extra})
        self.server = F.make_server(self.tmp, accounts=_accounts(), config=cfg)
        self.prepare()
        self.extra = []
        self.mc = F.MultiClient(self.server, players=self.players)
        self.a, self.b, self.c = self.mc
        _wait(lambda: len(self.server.world.peers(self.a.session)) == 2)
        self.mc.drain(0.3)

    def prepare(self):
        """Store edits before anyone logs in."""

    def tearDown(self):
        for c in self.extra:
            try:
                c.close()
            except OSError:
                pass
        self.mc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rec(self, name):
        found = self.server.store.character_by_name(name)
        return found[2] if found else None

    def key(self, op, index=0):
        return P.variants(op, 'C2S', client_build=self.build)[index]['key']

    def send(self, c, op, fields=None, index=0):
        c.send_c2s(self.key(op, index), fields or {})

    def ops(self, c, quiet=0.3):
        return _ops(c.recv_until_quiet(quiet))

    def sync(self, c):
        """C2S 0x2F -> the decoded replies."""
        self.send(c, 0x2F)
        return [(p.opcode, c.s2c(p)) for p in c.recv_until_quiet(0.3)]

    def rows(self, name='TestHero'):
        return [(r['name'], r['uid']) for r in self.rec(name)['blacklist']]

    def seed(self, name, *blocked):
        with self.server.store.lock:
            rows = []
            for other in blocked:
                found = self.server.store.character_by_name(other)
                rows.append({'name': found[2]['name'], 'uid': found[1]['uid'], 'cid': found[2]['cid']})
            self.rec(name)['blacklist'] = rows

    def whisper(self, c, to, text=b'hi'):
        self.send(c, 0x02, {'target_name': to, 'msg_len': len(text), 'message': text})

    def chat(self, c, text=b'hello'):
        self.send(c, 0x03, {'msg_len': len(text), 'message': text})

    def uid(self, name):
        return self.server.store.character_by_name(name)[1]['uid']

    def gm(self, c, line):
        """A '!' line typed by `c`: the 0x15 texts it answered."""
        self.server._gm_chat_command(c.session['sock'], c.session, line.encode())
        return [_text(c.s2c(p)['text']) for p in c.recv_until_quiet(0.3) if p.opcode == 0x15]


# ------------------------------------------------------------ 2009 resync ---
class Resync2009(_Rig, unittest.TestCase):
    def test_the_trio_after_0x03_in_bundle_order(self):
        a = self.a
        self.seed('TestHero', 'Carol')
        a.session['gm'], a.session['gm_hidden'] = 1, 0
        with self.assertNoLogs('WS', logging.ERROR):
            for op, index in ((0x2F, 0), (0x63, 0), (0x8A, 0)):
                self.send(a, op, index=index)
            # 0x2F: friends then the blacklist; 0x63: the deck, then "In channel 1." (the first
            # 0x63 of the connection); 0x8A: sub 15, then the GM tag sub 37 last
            pkts = a.expect(0x0B, 0xBD, 0x8A, 0x99, 0xB3, 0xB3)
        friends, blist, deck, channel, sub15, sub37 = pkts
        self.assertEqual(a.s2c(channel), {'sub_type': 8, 'channel_no': 1})
        self.assertEqual(a.s2c(blist), {'count': 1, 'repeat[count]': [{'char_id': 3, 'name': 'Carol'}]})
        self.assertEqual(a.s2c(sub15), {'sub': 15, 's15_result': 0})
        self.assertEqual(a.s2c(sub37), {'sub': 37, 's37_uid': 1})

    def test_the_map_load_spawn_stage_and_a_p14_style_hook(self):
        a = self.a
        seen = []
        rs = self.server.resync
        # P14 guild-g1 (guild.install) registered the real 'guild' / 'guild_apps' steps in the
        # bundle order this hook was written for; replacing them keeps that order. P15 guild-g6
        # (boards.install) added 'guild_boards' (0xBB in 9702) between them and the GM tag.
        self.assertEqual(rs.order(self.build)[RS.STAGE_GUILD], ['guild', 'guild_apps', 'guild_boards', 'gm_tag'])
        rs.register(RS.STAGE_GUILD, 'guild', lambda s, sock, sess, ctx: seen.append('sub3'), replace=True)
        rs.register(RS.STAGE_GUILD, 'guild_apps', lambda s, sock, sess, ctx: seen.append('sub4'), replace=True)
        a.send_c2s(PORTAL[self.build], {'portal_line_index': 23})
        self.assertTrue(_wait(lambda: self.server.world.map_of(a.session) == 102))
        ops = self.ops(a)
        self.assertEqual(ops[:3], [0x08, 0x03, 0x07])
        # the spawn stage: 0x28 / 0x44 after the own 0x07 (and its on_enter_world lines), before
        # the map's monsters
        self.assertLess(ops.index(0x28), ops.index(0x44))
        self.assertLess(ops.index(0x44), ops.index(0x1A))
        self.send(a, 0x8A)
        a.expect_silence(0.2)
        self.assertEqual(seen, ['sub3', 'sub4'])


# --------------------------------------------------------- 2009 bl-1 / bl-2 ---
class Blacklist2009(_Rig, unittest.TestCase):
    def test_add_lists_persists_and_survives_portal_and_relog(self):
        a = self.a
        self.assertEqual(self.sync(a)[-1], (0xBD, {'count': 0, 'repeat[count]': []}))   # T-B1 "(0/10)"
        self.send(a, 0x93, {'name': 'Watcher'})
        pkt = a.expect(0xBE)
        self.assertEqual(len(pkt.payload), 22)
        self.assertEqual(a.s2c(pkt), {'result': 1, 'char_id': 2, 'name': 'Watcher'})
        self.assertEqual(self.rows(), [('Watcher', 2)])                       # T-B2
        self.assertEqual(self.rec('TestHero')['blacklist'][0]['cid'], self.rec('Watcher')['cid'])
        self.server.store.save_now()
        with open(self.server.db_file, encoding='utf-8') as f:
            disk = json.load(f)['test']['characters'][0]['blacklist']
        self.assertEqual([(r['name'], r['uid']) for r in disk], [('Watcher', 2)])
        # T-B10: a portal (0x03 wipes the client list; its 0x2F gets it back) ...
        a.send_c2s(PORTAL[self.build], {'portal_line_index': 23})
        self.assertTrue(_wait(lambda: self.server.world.map_of(a.session) == 102))
        a.recv_until_quiet(0.3)
        want = (0xBD, {'count': 1, 'repeat[count]': [{'char_id': 2, 'name': 'Watcher'}]})
        self.assertEqual(self.sync(a)[-1], want)
        # ... and a relog
        a.close()
        self.assertTrue(_wait(lambda: self.server.world.by_char_name('TestHero') is None))
        again = F.FakeClient(self.server)
        self.extra.append(again)
        self.assertEqual(again.login('test', 'test')['result'], 1)
        again.enter_world('TestHero')
        self.assertEqual(self.sync(again)[-1], want)

    def test_every_add_refusal_is_one_byte_and_one_row_per_name(self):
        a = self.a
        self.sync(a)
        with self.server.store.lock:
            self.rec('TestHero')['friends'] = ['Carol']
        for name, why in (('Nobody', 'unknown'), ('TestHero', 'own'), ('', 'empty')):
            with self.subTest(why=why):
                self.send(a, 0x93, {'name': name})
                pkt = a.expect(0xBE)
                self.assertEqual((pkt.payload, a.s2c(pkt)), (b'\x00', {'result': 0}))
        self.send(a, 0x93, {'name': 'Carol'})                                 # a friend
        self.assertEqual(a.expect(0xBE).payload, b'\x00')
        self.send(a, 0x93, {'name': 'Watcher'})
        self.assertEqual(a.s2c(a.expect(0xBE))['result'], 1)
        for dup in ('Watcher', 'watcher'):                                    # T-B11 forged duplicates
            self.send(a, 0x93, {'name': dup})
            self.assertEqual(a.expect(0xBE).payload, b'\x00')
        self.assertEqual(self.rows(), [('Watcher', 2)])
        # a mentor / mentee is refused like a friend
        with self.server.store.lock:
            self.rec('TestHero')['friends'], self.rec('TestHero')['mentor'] = [], 'Carol'
        self.send(a, 0x93, {'name': 'Carol'})
        self.assertEqual(a.expect(0xBE).payload, b'\x00')
        with self.server.store.lock:
            self.rec('TestHero')['mentor'], self.rec('TestHero')['mentees'] = None, ['Carol']
        self.send(a, 0x93, {'name': 'Carol'})
        self.assertEqual(a.expect(0xBE).payload, b'\x00')
        # the 10-row cap (the client's own gate is "Can't be add."; a forged 11th is refused)
        with self.server.store.lock:
            self.rec('TestHero')['mentees'] = []
            self.rec('TestHero')['blacklist'] = [{'name': f'P{i}', 'uid': 100 + i} for i in range(10)]
        self.send(a, 0x93, {'name': 'Dave'})
        self.assertEqual(a.expect(0xBE).payload, b'\x00')
        self.assertEqual(len(self.rec('TestHero')['blacklist']), 10)

    def test_a_sibling_character_is_refused_and_the_account_uid_is_the_id(self):
        dave = F.FakeClient(self.server)
        self.extra.append(dave)
        dave.login('dave', 'dave')
        dave.enter_world('Dave', port=F.P2P_PORT_BASE + 3)
        self.mc.drain()
        self.send(dave, 0x2F)
        dave.recv_until_quiet(0.3)
        self.send(dave, 0x93, {'name': 'DaveAlt'})
        self.assertEqual(dave.expect(0xBE).payload, b'\x00')
        self.send(self.c, 0x93, {'name': 'DaveAlt'})
        self.assertEqual(self.c.s2c(self.c.expect(0xBE)), {'result': 1, 'char_id': 4, 'name': 'DaveAlt'})

    def test_remove_is_by_name_idempotent_and_refuses_only_an_empty_name(self):
        a = self.a
        self.seed('TestHero', 'Watcher', 'Carol')
        self.sync(a)
        self.send(a, 0x94, {'char_id': 999, 'name': 'Watcher'})               # the id is ignored
        self.assertEqual(a.s2c(a.expect(0xBF)), {'result': 1, 'name': 'Watcher'})
        self.assertEqual(self.rows(), [('Carol', 3)])
        self.send(a, 0x94, {'char_id': 2, 'name': 'Watcher'})                 # not listed: still 1
        self.assertEqual(a.s2c(a.expect(0xBF)), {'result': 1, 'name': 'Watcher'})
        self.send(a, 0x94, {'char_id': 0, 'name': ''})
        self.assertEqual(a.expect(0xBF).payload, b'\x00')
        self.assertEqual(self.rows(), [('Carol', 3)])

    def test_nothing_before_the_world_and_a_deleted_character_drops_out(self):
        late = F.FakeClient(self.server)
        self.extra.append(late)
        late.login('dave', 'dave')
        for op, fields in ((0x93, {'name': 'Carol'}), (0x94, {'char_id': 3, 'name': 'Carol'}), (0x2F, {})):
            self.send(late, op, fields)
        late.expect_silence(0.3)                    # SubHandler3 reads nothing before the first 0x03
        self.assertEqual(self.rec('Dave')['blacklist'], [])
        # A.8: Carol's character is deleted; TestHero's next 0x2F drops her row (and persists it)
        self.seed('TestHero', 'Watcher', 'Carol')
        self.c.close()
        self.assertTrue(_wait(lambda: self.server.world.by_char_name('Carol') is None))
        self.assertTrue(self.server.store.remove_character('carol', 'Carol'))
        got = self.sync(self.a)[-1]
        self.assertEqual(got, (0xBD, {'count': 1, 'repeat[count]': [{'char_id': 2, 'name': 'Watcher'}]}))
        self.assertEqual(self.rows(), [('Watcher', 2)])

    def test_a_rename_follows_the_row_and_resends_the_list(self):
        a, b = self.a, self.b
        self.seed('TestHero', 'Watcher')
        self.sync(a)                                                          # a's list is on its client
        self.server.cash.grant(self.rec('Watcher'), NICK)
        self.send(b, 0x49, {'new_name': 'Bobby'})
        self.assertEqual(_ops(b.recv_until_quiet(0.3))[:2], [0x73, 0x74])
        mine = [p for p in a.recv_until_quiet(0.3) if p.opcode == 0xBD]
        self.assertEqual(len(mine), 1)
        self.assertEqual(a.s2c(mine[0]), {'count': 1, 'repeat[count]': [{'char_id': 2, 'name': 'Bobby'}]})
        self.assertEqual(self.rows(), [('Bobby', 2)])
        # the filter follows the cid: Bobby's whisper is still dropped
        self.whisper(b, 'TestHero')
        self.assertEqual(b.s2c(b.expect(0x09))['status'], 0x65)
        a.expect_silence(0.2)

    def test_msgr_shows_the_list(self):
        self.seed('TestHero', 'Watcher')
        lines = self.server.messenger.describe(self.a.session) + [self.server.blacklist.describe(self.a.session)]
        self.assertIn('blacklist 1/10 (filter silent): Watcher(2)', lines)


# ---------------------------------------------------- 2009 bl-3 (silent) ---
class Silent2009(_Rig, unittest.TestCase):
    """BLACKLIST_FILTER silent (the default): TestHero blacklisted Watcher."""

    def prepare(self):
        self.seed('TestHero', 'Watcher')

    def assertNoPending(self):
        srv, a = self.server, self.a.session
        self.assertIsNone(srv.trade.invites.get(a['uid']))
        self.assertFalse(srv.party.invites.get(a['uid']))
        self.assertFalse(srv.messenger.requests.get(a['uid']))
        self.assertFalse(srv.messenger.invites.get(a['uid']))

    def test_whisper_and_map_chat(self):
        a, b, c = self.a, self.b, self.c
        self.whisper(b, 'TestHero', b'psst')                                  # T-B3
        echo = b.s2c(b.expect(0x09))
        self.assertEqual((echo['status'], _text(echo['target_name'])), (0x65, 'TestHero'))
        a.expect_silence(0.2)
        self.chat(b)                                                          # T-B4
        self.assertEqual(_ops(b.recv_until_quiet(0.3)), [0x16])               # the speaker's own line
        self.assertEqual(_ops(c.recv_until_quiet(0.3)), [0x16])               # a third player hears it
        a.expect_silence(0.2)
        self.chat(a, b'fine')                                                 # the other way: delivered
        self.assertEqual(_ops(b.recv_until_quiet(0.3)), [0x16])
        a.recv_until_quiet(0.2)

    def test_requests_leave_no_pending_state(self):
        a, b = self.a, self.b
        with self.server.store.lock:
            self.rec('TestHero')['class'] = 1                                 # a possible mentor
        for op, fields in ((0x20, {'target_uid': 1}), (0x27, {'target_uid': 1}),
                           (0x30, {'target_uid': 1, 'target_name': 'TestHero'}),
                           (0x33, {'target_uid': 1, 'target_name': 'TestHero'}),
                           (0x5C, {'mentor_name': 'TestHero'})):
            with self.subTest(op=hex(op)):
                self.send(b, op, fields)                                      # T-B6
                b.expect_silence(0.2)
                a.expect_silence(0.1)
                self.assertNoPending()
        self.assertIsNone(self.rec('Watcher')['mentor'])
        self.assertEqual(self.rec('TestHero')['mentees'], [])

    def test_no_phantom_trade_prompt(self):
        a, b, c = self.a, self.b, self.c
        self.send(b, 0x20, {'target_uid': 1})                                 # blocked: nothing
        a.expect_silence(0.2)
        self.send(c, 0x20, {'target_uid': 1})                                 # T-B5: Carol is not "busy"
        self.assertEqual(_text(a.s2c(a.expect(0x45))['requester_name']), 'Carol')
        c.expect_silence(0.2)

    def test_removing_the_row_lets_the_next_request_through_at_once(self):
        a, b = self.a, self.b
        self.send(b, 0x20, {'target_uid': 1})
        a.expect_silence(0.2)
        self.send(a, 0x94, {'char_id': 2, 'name': 'Watcher'})                 # exit criterion 3
        a.expect(0xBF)
        self.send(b, 0x20, {'target_uid': 1})                                 # within 30 s
        self.assertEqual(_text(a.s2c(a.expect(0x45))['requester_name']), 'Watcher')
        self.whisper(b, 'TestHero')                                           # T-B12
        self.assertEqual(_ops(a.recv_until_quiet(0.3)), [0x0A])

    def test_friend_chat_is_filtered_by_the_server(self):
        a, b, c = self.a, self.b, self.c
        with self.server.store.lock:
            self.rec('Watcher')['friends'] = ['TestHero', 'Carol']
        line = b'Watcher : yo'
        self.send(b, 0x6B, {'recipient_count': 2,
                            'repeat[recipient_count]': [{'friend_channel': 1, 'friend_id': 1},
                                                        {'friend_channel': 1, 'friend_id': 3}],
                            'msg_len': len(line), 'message': line})           # T-B8
        self.assertEqual(_ops(c.recv_until_quiet(0.3)), [0x91])
        a.expect_silence(0.2)
        b.expect_silence(0.1)

    def test_the_reverse_direction_is_dropped_too(self):
        a, b = self.a, self.b                                                 # F-B5: forged A -> B
        self.whisper(a, 'Watcher')
        self.assertEqual(a.s2c(a.expect(0x09))['status'], 0x65)
        for op, fields in ((0x20, {'target_uid': 2}), (0x27, {'target_uid': 2}),
                           (0x30, {'target_uid': 2, 'target_name': 'Watcher'}),
                           (0x33, {'target_uid': 2, 'target_name': 'Watcher'})):
            self.send(a, op, fields)
        a.expect_silence(0.2)
        b.expect_silence(0.2)
        self.chat(b)                                       # a fan-out is filtered by the RECEIVER only:
        self.assertEqual(_ops(b.recv_until_quiet(0.3)), [0x16])
        a.expect_silence(0.2)                              # (TestHero listed Watcher)
        self.chat(a)
        self.assertEqual(_ops(b.recv_until_quiet(0.3)), [0x16])               # Watcher listed nobody
        a.recv_until_quiet(0.2)


# ------------------------------------------- live triage BL: `!blacklist filter` ---
class FilterCommand2009(_Rig, unittest.TestCase):
    """The mode is switched at run time - no config edit, no restart (Blacklist.mode() reads
    server.config on every call; the `!channel scale` pattern). TestHero listed Watcher."""

    def prepare(self):
        self.seed('TestHero', 'Watcher')

    def test_the_filter_switches_without_a_restart(self):
        a, b = self.a, self.b
        self.assertEqual(self.gm(a, '!blacklist filter'), ["BLACKLIST_FILTER is 'silent' (client | silent | refuse)."])
        with self.assertLogs('WS', logging.INFO) as cm:
            self.assertEqual(self.gm(a, '!blacklist filter refuse'), ['BLACKLIST_FILTER silent -> refuse (until a restart).'])
        self.assertTrue(any("[BLACKLIST] filter silent -> refuse by 'TestHero'" in line for line in cm.output))
        self.assertEqual(self.server.blacklist.mode(), BL.MODE_REFUSE)
        self.whisper(b, 'TestHero')                                           # "TestHero is rejecting whispers."
        got = b.s2c(b.expect(0x09))
        self.assertEqual((got['status'], _text(got['target_name'])), (0x67, 'TestHero'))
        a.expect_silence(0.2)
        self.gm(a, '!bl filter SILENT')                                       # the alias, any case
        self.whisper(b, 'TestHero')                                           # the echo-only path again
        self.assertEqual(b.s2c(b.expect(0x09))['status'], 0x65)
        a.expect_silence(0.2)
        self.gm(a, '!blacklist filter client')                                # the client filters itself
        self.whisper(b, 'TestHero')
        self.assertEqual(_ops(a.recv_until_quiet(0.3)), [0x0A])
        b.recv_until_quiet(0.2)
        # a bad mode is refused with the usage; the mode stays
        lines = self.gm(a, '!blacklist filter loud')
        self.assertIn("'loud': not a mode", lines[0])
        self.assertIn('!blacklist [filter [client|silent|refuse]]', lines[0])
        self.assertEqual(self.server.blacklist.mode(), BL.MODE_CLIENT)
        # no argument: the rows and the mode
        self.assertEqual(self.gm(a, '!blacklist'), [f'blacklist 1/10 (filter client): Watcher({self.uid("Watcher")})'])


# -------------------------------- P12 review: a listed character's sibling ---
class Sibling2009(_Rig, unittest.TestCase):
    """TestHero listed Dave; DaveAlt (Dave's account, uid 4) is the third player. The 2009
    client drops S2C 0x0D / 0x10 by id OR name (FUN_00484190 at 0x47C180 / 0x47C5CD) - the id
    is the account uid, so it drops DaveAlt's too; the name-matched paths (whisper, trade) let
    DaveAlt through. The server mirrors exactly that, in every mode."""
    players = (('test', 'test', 'TestHero'), ('admin', 'admin', 'Watcher'), ('dave', 'dave', 'DaveAlt'))

    def prepare(self):
        self.seed('TestHero', 'Dave')

    def test_the_id_matched_requests_are_filtered_for_the_sibling_too(self):
        a, c = self.a, self.c
        srv, uid = self.server, a.session['uid']
        self.assertEqual(self.uid('DaveAlt'), self.uid('Dave'))
        friend = {'target_uid': 1, 'target_name': 'TestHero'}
        # silent (default): no prompt, no window, nothing back - and NO pending state
        self.send(c, 0x30, friend)
        self.send(c, 0x33, friend)
        c.expect_silence(0.2)
        a.expect_silence(0.2)
        self.assertFalse(srv.messenger.requests.get(uid))
        self.assertFalse(srv.messenger.invites.get(uid))
        # the name-matched paths are not the sibling's business: delivered as the client shows them
        self.whisper(c, 'TestHero')
        self.assertEqual(_ops(a.recv_until_quiet(0.3)), [0x0A])
        self.assertEqual(c.s2c(c.expect(0x09))['status'], 0x65)
        self.send(c, 0x20, {'target_uid': 1})
        self.assertEqual(_text(a.s2c(a.expect(0x45))['requester_name']), 'DaveAlt')
        c.recv_until_quiet(0.2)
        # refuse: the consumers' own refusals
        self.gm(a, '!blacklist filter refuse')
        self.send(c, 0x30, friend)
        self.assertEqual(c.s2c(c.expect(0x0C))['result'], social.ADD_FRIEND_OFF)
        self.send(c, 0x33, friend)
        self.assertEqual(c.s2c(c.expect(0x0F))['subtype'], 4)
        a.expect_silence(0.2)
        self.assertFalse(srv.messenger.requests.get(uid))
        self.assertFalse(srv.messenger.invites.get(uid))
        # client: delivered (the receiving client drops it itself)
        self.gm(a, '!blacklist filter client')
        self.send(c, 0x30, friend)
        self.assertEqual(_ops(a.recv_until_quiet(0.3)), [0x0D])
        # the model: only the id half (gate by_uid) lists the sibling
        self.assertFalse(srv.blacklist.blocks(a.session, c.session))
        self.assertTrue(BL.lists_uid(self.rec('TestHero'), self.uid('DaveAlt')))
        self.assertFalse(BL.lists_uid(self.rec('TestHero'), 0))


# ---------------------------------------------------- 2009 bl-3 (refuse) ---
class Refuse2009(_Rig, unittest.TestCase):
    mode = BL.MODE_REFUSE

    def prepare(self):
        self.seed('TestHero', 'Watcher')
        with self.server.store.lock:
            self.rec('TestHero')['class'] = 1

    def test_each_consumer_sends_its_refusal_and_keeps_nothing(self):
        a, b = self.a, self.b
        self.whisper(b, 'TestHero')
        got = b.s2c(b.expect(0x09))
        self.assertEqual((got['status'], _text(got['target_name'])), (0x67, 'TestHero'))   # "is rejecting whispers."
        self.send(b, 0x20, {'target_uid': 1})
        self.assertEqual(b.s2c(b.expect(0x47)), {'result': 4})
        self.send(b, 0x27, {'target_uid': 1})
        self.assertEqual(_text(b.s2c(b.expect(0x15))['text']), '[Warning] TestHero is refusing party invitations.')
        self.send(b, 0x30, {'target_uid': 1, 'target_name': 'TestHero'})
        self.assertEqual(b.s2c(b.expect(0x0C))['result'], social.ADD_FRIEND_OFF)
        self.send(b, 0x33, {'target_uid': 1, 'target_name': 'TestHero'})
        self.assertEqual(b.s2c(b.expect(0x0F))['subtype'], 4)
        self.send(b, 0x5C, {'mentor_name': 'TestHero'})
        self.assertEqual(b.s2c(b.expect(0x7A))['result'], social.MENTOR_NOT_FOUND)
        a.expect_silence(0.2)
        self.assertIsNone(self.server.trade.invites.get(1))
        self.assertFalse(self.server.party.invites.get(1))
        self.chat(b)                                                          # map chat: still skipped
        self.assertEqual(_ops(b.recv_until_quiet(0.3)), [0x16])
        a.expect_silence(0.2)
        # the privacy API answers it too (a GM actor is not exempt from a blacklist)
        self.assertTrue(self.server.refuses(a.session, 'whisper', b.session))
        self.assertTrue(self.server.refuses('TestHero', 'exchange', b.session))
        self.assertFalse(self.server.blacklist_drops(a.session, b.session))
        self.assertFalse(self.server.refuses(a.session, 'whisper', self.c.session))


# ---------------------------------------------------- 2009 bl-3 (client) ---
class Client2009(_Rig, unittest.TestCase):
    mode = BL.MODE_CLIENT

    def prepare(self):
        self.seed('TestHero', 'Watcher')

    def test_the_server_delivers_and_the_client_filters(self):
        a, b = self.a, self.b
        self.whisper(b, 'TestHero')
        self.assertEqual(_ops(a.recv_until_quiet(0.3)), [0x0A])
        self.assertEqual(b.s2c(b.expect(0x09))['status'], 0x65)
        self.chat(b)
        self.assertEqual(_ops(a.recv_until_quiet(0.3)), [0x16])
        self.assertEqual(self.sync(a)[-1][1]['count'], 1)                     # the list still syncs


# ------------------------------------------------------------------ 2008 ---
class NoBlacklist2008(_Rig, unittest.TestCase):
    build = B8

    def prepare(self):
        self.seed('TestHero', 'Watcher')                   # a shared accounts.json may hold rows

    def test_no_packets_and_no_filter_on_the_2008_client(self):
        a, b = self.a, self.b
        self.assertEqual([op for op, _ in self.sync(a)], [0x0B])               # no 0xBD
        self.send(a, 0x63)
        self.assertEqual(self.ops(a), [0x8A, 0x99])
        self.assertEqual(self.server.resync.order(), {
            RS.STAGE_SPAWN: ['vitals', 'owned_cash', 'gift_queue'], RS.STAGE_FRIENDS: ['friends'],
            RS.STAGE_CARDS: ['cards', 'channel', 'events'], RS.STAGE_GUILD: []})
        self.whisper(b, 'TestHero')
        self.assertEqual(_ops(a.recv_until_quiet(0.3)), [0x0A])
        b.recv_until_quiet(0.2)
        self.chat(b)
        self.assertEqual(_ops(a.recv_until_quiet(0.3)), [0x16])
        self.assertIsNone(self.server.blacklist.gate(a.session, b.session))
        self.assertFalse(self.server.blacklist.hides(a.session, b.session))
        self.assertTrue(self.server.blacklist.blocks(a.session, b.session))    # the model itself
        self.assertEqual(self.server.ROUTES[0x93].handler, '_warn_dead_host_code')
        self.assertEqual(self.server.ROUTES[0x94].handler, '_warn_dead_host_code')
        self.assertEqual(self.server.ROUTES_2009[0x93].handler, '_handle_blacklist_add')
        self.assertEqual(self.server.ROUTES_2009[0x94].handler, '_handle_blacklist_remove')
        self.assertIn('none on the 2008 client', self.server.blacklist.describe(a.session))
        self.assertIn('No blacklist on the 2008 client', self.gm(a, '!blacklist filter refuse')[0])
        self.assertIsNone(self.server.blacklist.gate(a.session, b.session))


if __name__ == '__main__':
    unittest.main()
