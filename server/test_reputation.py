#!/usr/bin/env python3
"""
test_reputation.py - P7 stage 3 (compliments and player reports), offline, both client builds
=========================================================================================
social_friend-compliment, chat_mail_gm-report (+ social_friend-report-result);
docs/systems/social_friend.md F10 / F11, chat_mail_gm.md 1.6 / F9, roadmap P7 exit 5:

- pure rules (reputation.py): the S2C 0x94..0x97 bytes and the C2S 0x6D / 0x6E send sites of
  both builds, the client's report fee, the 7 / 6 / 5 compliment order and pruning, the
  category clamp, the routes, the reply policy and the config checks;
- two / three fake clients on one server (MultiClient; TestHero test/test uid 1 = client 1,
  Watcher admin/admin uid 2 = client 2, Carol uid 3), for 2008 and 2009:
  exit 5 - A praises B from the popup: 0x94 {1, "Watcher"} + 0x97 to A (it holds B), the 25 B
  0x96 to B, the account manner +1 persisted and in B's 0x02 / 0x07 after a relog; a second
  compliment the same day is 0x94 {7}. A reports B: the client's fee is taken, 0x95 {1, new
  absolute gold}, one reports.jsonl line, a [Warning] line to an online GM; a second report
  the same day is 0x95 {7} and costs nothing;
  plus the weekly (6) and daily-cap (5) rules on later days, every "doesn't exist" (2) case,
  the HUD Report path (uid 0, offline target), own account, fee by level, short of gold,
  gold locked in a trade, the category clamp and the `!rep` / `!reports` dev commands.

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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import fakeclient as F  # noqa: E402
import packets as P  # noqa: E402
import progression  # noqa: E402
import registry  # noqa: E402
import reputation as REP  # noqa: E402
import social  # noqa: E402

W = F.import_server()
cfgmod = W.cfgmod
logging.getLogger('WS').setLevel(logging.WARNING)
logging.getLogger('WS').addHandler(logging.NullHandler())

LIVE_ACCOUNTS = os.path.join(HERE, 'accounts.json')
B8, B9 = cfgmod.BUILD_2008, cfgmod.BUILD_2009
DIRS = {B8: cfgmod.DEFAULTS['CLIENT_DIR'], B9: cfgmod.DEFAULTS['CLIENT_DIR_2009']}
HAVE = {b: os.path.exists(os.path.join(HERE, d, 'hs', 'windslayer.hii')) for b, d in DIRS.items()}

# The send sites of each build (spec_2009: the same grammars at new addresses).
KEYS = {
    B8: {'praise': '0x473701/0x6D', 'report': '0x4735D1/0x6E', 'enter': '0x42F904/0x2B',
         'trade_request': '0x4484CC/0x20', 'trade_accept': '0x469C33/0x21', 'trade_lock': '0x469D05/0x23'},
    B9: {'praise': '0x4815C8/0x6D', 'report': '0x4819CD/0x6E', 'enter': '0x4315D7/0x2B',
         'trade_request': '0x44A9D6/0x20', 'trade_accept': '0x47394E/0x21', 'trade_lock': '0x473ABA/0x23'},
}
DAY = 86400.0

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
    W.EC.configure(DIRS[B8], B8)
    if _LIVE_HASH is not None:
        assert _sha(LIVE_ACCOUNTS) == _LIVE_HASH, \
            'accounts.json changed during test_reputation.py (tests must only use temp copies)'


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _ops(pkts):
    return [p.opcode for p in pkts]


def _char(name):
    return {'name': name, 'level': 1, 'class': 0, 'map': 101, 'x': 1411.0, 'y': 714.0, 'hp': 100, 'mp': 50}


# ================================================================ pure rules ===
class Rules(unittest.TestCase):
    def test_packet_bytes_of_both_builds(self):
        """social_friend 1.5 / spec 0x94..0x97 (spec_2009 "identical"): 0x94 18 B / 1 B, 0x95
        9 B / 1 B, 0x96 always 25 B (DEFAULT_ASSUME), 0x97 8 B."""
        for build in (B8, B9):
            with self.subTest(build=build):
                self.assertEqual(P.build('0x94', {'result': 1, 'target_name': b'Watcher'}, client_build=build),
                                 b'\x01' + P.name17('Watcher'))
                self.assertEqual(P.build('0x94', {'result': 7}, client_build=build), b'\x07')
                self.assertEqual(P.build('0x95', {'result': 1, 'gold': 900}, client_build=build).hex(),
                                 '01' + '8403000000000000')
                self.assertEqual(P.build('0x95', {'result': 2}, client_build=build), b'\x02')
                body = P.build('0x96', {'target_uid': 2, 'manner_delta': 1, 'complimenter_name': b'TestHero'},
                               client_build=build)
                self.assertEqual(body.hex(), '02000000' + '01000000' + P.name17('TestHero').hex())
                self.assertEqual(P.build('0x97', {'uid': 2, 'manner_delta': -3}, client_build=build).hex(),
                                 '02000000' + 'fdffffff')

    def test_c2s_send_sites_of_both_builds(self):
        for build in (B8, B9):
            k = KEYS[build]
            with self.subTest(build=build):
                rec = P.parse(0x6D, P.build(k['praise'], {'target_id': 2, 'target_name': 'Watcher'},
                                            direction='C2S', client_build=build), client_build=build)
                self.assertEqual((rec.key, rec['target_id'], rec['target_name']), (k['praise'], 2, 'Watcher'))
                body = P.build(k['report'], {'target_uid': 0, 'target_name': 'Carol', 'report_fee': 100,
                                             'category': 3, 'content_len': 3, 'content': b'bad'},
                               direction='C2S', client_build=build)
                self.assertEqual(len(body), 27 + 3)
                rec = P.parse(0x6E, body, client_build=build)
                self.assertEqual((rec.key, rec['target_uid'], rec['report_fee'], rec['category'], rec['content']),
                                 (k['report'], 0, 100, 3, b'bad'))

    def test_report_fee_is_the_clients_formula(self):
        """window 0x434: ceil(level / 10) * 100 (live: 100 at level 1), never below 100."""
        self.assertEqual([REP.report_fee(lv) for lv in (0, 1, 9, 10, 11, 20, 21, 25, 99)],
                         [100, 100, 100, 100, 200, 200, 300, 300, 1000])

    def test_categories_are_clamped(self):
        self.assertEqual(REP.category_name(0), 'Abuse')
        self.assertEqual(REP.category_name(1), 'Spamming')
        self.assertEqual(REP.category_name(6), 'Hacking Tools')
        self.assertEqual(REP.clamp_category(9), 6)
        self.assertEqual(REP.clamp_category(-1), 0)

    def test_compliment_rules_order_and_pruning(self):
        giver, target = social.default_account_social(), social.default_account_social()
        day = '2026-09-25'
        self.assertIsNone(REP.compliment_refusal(giver, 'admin', target, day))
        REP.apply_compliment(giver, 'admin', target, day)
        self.assertEqual(giver['compliment_given_day'], day)
        self.assertEqual(giver['complimented_accounts'], {'admin': day})
        self.assertEqual(target['compliments_received'], {'day': day, 'count': 1})
        # 7 first (once a day), then 6 (the same account within 7 days), then 5 (the cap)
        self.assertEqual(REP.compliment_refusal(giver, 'admin', target, day), REP.COMPLIMENT_ONCE_A_DAY)
        self.assertEqual(REP.compliment_refusal(giver, 'carol', target, day), REP.COMPLIMENT_ONCE_A_DAY)
        self.assertEqual(REP.compliment_refusal(giver, 'admin', target, '2026-10-01'), REP.COMPLIMENT_SAME_PLAYER)
        self.assertIsNone(REP.compliment_refusal(giver, 'admin', target, '2026-10-02'))
        self.assertIsNone(REP.compliment_refusal(giver, 'admin', target, '2026-09-26', repeat_days=1))
        other = social.default_account_social()
        self.assertEqual(REP.compliment_refusal(other, 'admin', target, day, cap=1), REP.COMPLIMENT_TARGET_CAPPED)
        self.assertIsNone(REP.compliment_refusal(other, 'admin', target, '2026-09-26', cap=1))   # a new day
        # a later giving prunes what the weekly rule no longer needs
        REP.apply_compliment(giver, 'carol', social.default_account_social(), '2026-10-03')
        self.assertEqual(giver['complimented_accounts'], {'carol': '2026-10-03'})
        self.assertEqual(REP.days_between('2026-09-25', '2026-10-02'), 7)
        self.assertIsNone(REP.days_between('junk', '2026-10-02'))

    def test_routes_policy_and_dev_commands(self):
        want = {0x6D: '_handle_compliment', 0x6E: '_handle_report'}
        for routes in (W.GameServer.ROUTES, W.GameServer.ROUTES_2009):
            self.assertEqual({op: routes[op].handler for op in want}, want)
            self.assertEqual(registry.check_routes(routes, W.GameServer), [])
        for build in (B8, B9):
            # neither opens a waiting box: no refusal row, and both are answered
            self.assertFalse(set(want) & set(registry.must_reply_table(build)))
            self.assertFalse(set(want) & set(registry.never_reply_table(build)))
        self.assertEqual(W.GameServer.DEV_COMMANDS['rep'].handler, '_dev_rep')
        self.assertEqual(W.GameServer.DEV_COMMANDS['reports'].handler, '_dev_reports')

    def test_config_defaults_and_checks(self):
        d = cfgmod.defaults()
        self.assertEqual((d['COMPLIMENT_DELTA'], d['COMPLIMENT_DAILY_CAP'], d['COMPLIMENT_REPEAT_DAYS']), (1, 5, 7))
        for bad in ({'COMPLIMENT_DELTA': 0}, {'COMPLIMENT_DAILY_CAP': 0}, {'COMPLIMENT_REPEAT_DAYS': -1}):
            with self.subTest(bad=bad), self.assertRaises(cfgmod.ConfigError):
                cfgmod.from_dict(bad)


# ============================================================== two players ===
class _Base:
    """TestHero (uid 1, 1000 gold) and Watcher (uid 2, 500 gold) in world on map 101 of one
    server of `build`; Carol (uid 3, 300 gold) exists and is offline until a test logs her in.
    The reputation clock is pinned to noon of a fixed day, so the day rules are exact."""
    build = B8
    config = {}

    def setUp(self):
        if not HAVE[self.build]:
            self.skipTest(f'needs the EN {self.build} client data')
        self.tmp = tempfile.mkdtemp(prefix=f'ws_rep_{self.build}_')
        cfg = cfgmod.from_dict({'CLIENT_BUILD': self.build, 'MOB_SERVER_CONTROLLED': True, 'MOB_AGGRO': False,
                                **self.config})
        accounts = F.two_player_accounts()
        accounts['carol'] = {'password': 'carol', 'characters': [_char('Carol')]}
        self.server = F.make_server(self.tmp, accounts=accounts, config=cfg)
        self.k = KEYS[self.build]
        self.now = time.mktime((2026, 9, 25, 12, 0, 0, 0, 0, -1))
        self.server.reputation.clock = lambda: self.now
        for name, gold in (('TestHero', 1000), ('Watcher', 500), ('Carol', 300)):
            self.rec(name)['gold'] = gold
        self.extra = []
        self.mc = F.MultiClient(self.server)
        self.a, self.b = self.mc
        self.mc.drain()

    def tearDown(self):
        for c in self.extra:
            try:
                c.close()
            except OSError:
                pass
        self.mc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ----------------------------------------------------------------- helpers ---
    def rec(self, name):
        found = self.server.store.character_by_name(name)
        return found[2] if found else None

    def account(self, username):
        return self.server.store.account(username)

    def manner(self, username):
        return self.server.store.manner(username)

    def social(self, username):
        return self.account(username)['social']

    def gold(self, name):
        return self.rec(name)['gold']

    def praise(self, c, uid, name):
        c.send_c2s(self.k['praise'], {'target_id': uid, 'target_name': name})

    def report(self, c, uid, name, text=b'spam bot', category=1, fee=100):
        c.send_c2s(self.k['report'], {'target_uid': uid, 'target_name': name, 'report_fee': fee,
                                      'category': category, 'content_len': len(text), 'content': text})

    def result(self, c, op):
        return c.s2c(c.expect(op))

    def notice(self, pkt, c):
        return P.to_bytes(c.s2c(pkt)['text']).decode('cp949')

    def reports(self):
        path = os.path.join(self.tmp, REP.LOG_NAME)
        if not os.path.exists(path):
            return []
        with open(path, encoding='utf-8') as f:
            return [json.loads(line) for line in f if line.strip()]

    def login(self, account, password, name, number):
        c = F.FakeClient(self.server)
        self.extra.append(c)
        got = c.login(account, password)
        self.assertEqual(got['result'], 1)
        c.char_name = name
        pkts = c.enter_world(name, port=F.P2P_PORT_BASE + number - 1)
        return c, got, pkts

    def carol(self):
        c, _, _ = self.login('carol', 'carol', 'Carol', 3)
        self.mc.drain()
        c.recv_until_quiet(0.2)
        return c

    def next_day(self, days=1):
        self.now += days * DAY


class _Reputation(_Base):
    # ================================================================ exit 5 ===
    def test_exit5_praise_gives_plus_one_once_a_day_and_persists(self):
        """Exit criterion 5, compliment half: popup Praise on B -> A gets 0x94 {1, "Watcher"}
        (the modal "<Watcher> has been complimented") and, holding B, 0x97 {2, +1}; B gets the
        25 B 0x96 {2, +1, "TestHero"} (the green "<TestHero> added 1 of your manner points." and
        its Status Manner); the admin account's manner is 1 in the store and on disk. The
        second Praise the same day is 0x94 {7} and changes nothing. B's relog shows manner 1
        in its 0x02 and in its own 0x07 karma."""
        a, b = self.a, self.b
        self.praise(a, 2, 'Watcher')
        got = a.recv_until_quiet(0.3)
        self.assertEqual(_ops(got), [0x94, 0x97])
        self.assertEqual(a.s2c(got[0]), {'result': 1, 'target_name': 'Watcher'})
        self.assertEqual(a.s2c(got[1]), {'uid': 2, 'manner_delta': 1})
        pkt = b.expect(0x96)
        self.assertEqual(len(pkt.payload), 25)
        self.assertEqual(b.s2c(pkt), {'target_uid': 2, 'manner_delta': 1, 'complimenter_name': 'TestHero'})
        self.assertEqual((self.manner('admin'), self.manner('test')), (1, 0))
        self.assertEqual(self.social('test')['compliment_given_day'], '2026-09-25')
        self.assertEqual(self.social('test')['complimented_accounts'], {'admin': '2026-09-25'})
        self.assertEqual(self.social('admin')['compliments_received'], {'day': '2026-09-25', 'count': 1})
        # the second compliment the same day
        self.praise(a, 2, 'Watcher')
        self.assertEqual(self.result(a, 0x94), {'result': REP.COMPLIMENT_ONCE_A_DAY})
        b.expect_silence(0.15)
        self.assertEqual(self.manner('admin'), 1)
        # persisted (the store's debounced save; flushed here as the tick thread would)
        self.server.store.flush()
        with open(self.server.db_file, encoding='utf-8') as f:
            disk = json.load(f)
        self.assertEqual(disk['admin']['manner'], 1)
        self.assertEqual(disk['test']['social']['compliment_given_day'], '2026-09-25')
        # B's relog: the 0x02 manner_points and its own 0x07 karma are the account's
        b.close()
        self.assertTrue(_wait(lambda: self.server.world.session(2) is None))
        c, listing, pkts = self.login('admin', 'admin', 'Watcher', 2)
        self.assertEqual(listing['manner_points'], 1)
        own = next(p for p in pkts if p.opcode == 0x07)
        self.assertEqual(c.s2c(own)['repeat[player_count]'][0]['karma'], 1)

    def test_exit5_report_takes_the_fee_once_a_day_and_is_stored(self):
        """Exit criterion 5, report half: popup Report on B (uid 2, category Spamming) -> A pays
        the level-1 fee 100 and gets 0x95 {1, 900} ("Reported successively." + the gold label);
        reports.jsonl holds the report; the online GM gets a [Warning] line. A second report
        the same day (another target) is 0x95 {7}: no fee, no line."""
        a, b = self.a, self.b
        b.session['gm'] = 1                                  # B is an online GM for the alert
        self.report(a, 2, 'Watcher', b'spam bot', category=1)
        self.assertEqual(self.result(a, 0x95), {'result': REP.REPORT_OK, 'gold': 900})
        self.assertEqual(self.gold('TestHero'), 900)
        self.assertIn('Report #1: TestHero -> Watcher (Spamming)', self.notice(b.expect(0x15), b))
        rows = self.reports()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r['id'], r['reporter']['name'], r['reporter']['account'], r['target']['name'],
                          r['target']['account'], r['target']['online'], r['category_name'], r['content'],
                          r['fee'], r['gold_after'], r['day']),
                         (1, 'TestHero', 'test', 'Watcher', 'admin', True, 'Spamming', 'spam bot', 100, 900,
                          '2026-09-25'))
        self.assertEqual(self.social('test')['report_day'], '2026-09-25')
        # the second report the same day: refused, free, not stored
        self.report(a, 0, 'Carol', b'again')
        self.assertEqual(self.result(a, 0x95), {'result': REP.REPORT_ONCE_A_DAY})
        b.expect_silence(0.15)
        self.assertEqual((self.gold('TestHero'), len(self.reports())), (900, 1))
        # the next day it works again
        self.next_day()
        self.report(a, 0, 'Carol', b'again')
        self.assertEqual(self.result(a, 0x95), {'result': REP.REPORT_OK, 'gold': 800})
        self.assertEqual([r['id'] for r in self.reports()], [1, 2])

    # ======================================================== compliment rules ===
    def test_weekly_rule_and_a_new_target_on_later_days(self):
        """0x94 {6} for the same account within COMPLIMENT_REPEAT_DAYS; another account the
        next day is fine; after 7 days the first again."""
        a, b = self.a, self.b
        self.praise(a, 2, 'Watcher')
        a.recv_until_quiet(0.3)
        b.recv_until_quiet(0.2)
        self.next_day()
        self.praise(a, 2, 'Watcher')
        self.assertEqual(self.result(a, 0x94), {'result': REP.COMPLIMENT_SAME_PLAYER})
        carol = self.carol()
        self.praise(a, 3, 'Carol')
        self.assertEqual(_ops(a.recv_until_quiet(0.3)), [0x94, 0x97])
        self.assertEqual(carol.s2c(carol.expect(0x96))['complimenter_name'], 'TestHero')
        self.assertEqual(self.manner('carol'), 1)
        b.recv_until_quiet(0.2)                              # B holds Carol: its 0x97
        self.next_day(6)                                     # 7 days after the first
        self.praise(a, 2, 'Watcher')
        got = a.recv_until_quiet(0.3)
        self.assertEqual(_ops(got), [0x94, 0x97])
        self.assertEqual(a.s2c(got[0]), {'result': 1, 'target_name': 'Watcher'})
        self.assertEqual(self.manner('admin'), 2)

    def test_a_player_holding_the_target_gets_the_0x97(self):
        """social_friend F10 step 6: every other client holding B (Carol here) gets 0x97 so
        its copy's name tag / Player Info follow; B itself only the 0x96."""
        a, b = self.a, self.b
        carol = self.carol()
        b.recv_until_quiet(0.2)
        self.praise(a, 2, 'Watcher')
        self.assertEqual(_ops(a.recv_until_quiet(0.3)), [0x94, 0x97])
        self.assertEqual(carol.s2c(carol.expect(0x97)), {'uid': 2, 'manner_delta': 1})
        self.assertEqual(_ops(b.recv_until_quiet(0.2)), [0x96])

    def test_not_found_cases_are_0x94_2(self):
        """F10 step 2: no online visible character by uid+name or name, or one's own -> {2};
        nothing is recorded."""
        a, b = self.a, self.b
        for uid, name, why in ((0, 'Nobody', 'unknown'), (3, 'Carol', 'offline'), (1, 'TestHero', 'oneself'),
                               (0, '', 'empty')):
            with self.subTest(why):
                self.praise(a, uid, name)
                self.assertEqual(self.result(a, 0x94), {'result': REP.COMPLIMENT_NOT_FOUND})
        # a GM in shadow is nobody to a non-GM
        b.session.update(gm=1, gm_hidden=True)
        self.praise(a, 2, 'Watcher')
        self.assertEqual(self.result(a, 0x94), {'result': REP.COMPLIMENT_NOT_FOUND})
        b.session.update(gm=0, gm_hidden=False)
        b.expect_silence(0.1)
        self.assertEqual((self.manner('admin'), self.social('test')['compliment_given_day']), (0, None))

    def test_a_stale_uid_is_ignored_and_the_name_wins(self):
        """The popup uid of an entity whose name differs (a stale +0x134) -> the name decides,
        as in messenger.friend_add."""
        a, b = self.a, self.b
        carol = self.carol()
        b.recv_until_quiet(0.2)
        self.praise(a, 2, 'Carol')
        self.assertEqual(a.s2c(a.recv_until_quiet(0.3)[0]), {'result': 1, 'target_name': 'Carol'})
        carol.expect(0x96)
        self.assertEqual(_ops(b.recv_until_quiet(0.2)), [0x97])      # B holds Carol
        self.assertEqual((self.manner('carol'), self.manner('admin')), (1, 0))
        self.praise(self.b, 99, 'TestHero')                 # an unknown uid, a good name
        self.assertEqual(self.b.s2c(self.b.recv_until_quiet(0.3)[0]), {'result': 1, 'target_name': 'TestHero'})
        self.assertEqual(self.manner('test'), 1)

    def test_outside_the_world_is_dropped(self):
        self.a.session['in_world'] = False
        self.praise(self.a, 2, 'Watcher')
        self.report(self.a, 2, 'Watcher')
        self.a.expect_silence(0.2)
        self.a.session['in_world'] = True
        self.assertEqual((self.manner('admin'), self.gold('TestHero')), (0, 1000))

    # =========================================================== report rules ===
    def test_hud_report_path_offline_target_and_unknown_name(self):
        """The red HUD Report button sends uid 0 (or a stale one): any stored character of
        that name is reportable, online or not; an unknown name is 0x95 {2}, free, and does
        not use up the day."""
        a = self.a
        self.report(a, 0, 'Nobody')
        self.assertEqual(self.result(a, 0x95), {'result': REP.REPORT_NOT_FOUND})
        self.assertEqual((self.gold('TestHero'), self.social('test')['report_day'], self.reports()), (1000, None, []))
        self.report(a, 77, 'carol', b'offline')                 # stale uid, case-insensitive name
        self.assertEqual(self.result(a, 0x95), {'result': 1, 'gold': 900})
        r = self.reports()[0]
        self.assertEqual((r['target']['name'], r['target']['account'], r['target']['online'], r['target']['uid_sent']),
                         ('Carol', 'carol', False, 77))

    def test_own_account_is_dropped_silently(self):
        self.account('test')['characters'].append(_char('Twin'))
        self.server.store.name_index['twin'] = 'test'
        self.report(self.a, 0, 'Twin')
        self.a.expect_silence(0.2)
        self.assertEqual((self.gold('TestHero'), self.reports()), (1000, []))

    def test_fee_follows_the_level_and_the_server_fee_is_charged(self):
        """Lv.25: the client shows and sends 300; a forged 1 is logged and 300 is charged."""
        self.rec('TestHero')['exp'] = progression.exp_for_level(25)
        with self.assertLogs('WS', logging.WARNING) as cm:
            self.report(self.a, 2, 'Watcher', fee=1)
            self.assertEqual(self.result(self.a, 0x95), {'result': 1, 'gold': 700})
        self.assertTrue(any('client fee 1 != server fee 300' in line for line in cm.output), cm.output)
        self.assertEqual(self.reports()[0]['fee'], 300)

    def test_short_of_gold_resyncs_the_label(self):
        """The label was ahead of the store (the client pre-checked gold >= fee): 0x3F with the
        stored gold + the client's own "You are short of gold." line; nothing is used up."""
        self.rec('TestHero')['gold'] = 50
        self.report(self.a, 2, 'Watcher')
        got = self.a.recv_until_quiet(0.3)
        self.assertEqual(_ops(got), [0x3F, 0x15])
        self.assertEqual(self.a.s2c(got[0]), {'gold': 50})
        self.assertIn('You are short of gold.', self.notice(got[1], self.a))
        self.assertEqual((self.gold('TestHero'), self.social('test')['report_day'], self.reports()), (50, None, []))

    def test_gold_locked_in_a_trade_cannot_pay_the_fee(self):
        """trade-escrow-guards: 950 of 1000 gold locked by C2S 0x23 -> the 100 fee is refused
        with a 0x15 line; the trade and the gold are untouched."""
        a, b = self.a, self.b
        a.send_c2s(self.k['trade_request'], {'target_uid': 2})
        b.expect(0x45)
        b.send_c2s(self.k['trade_accept'], {'requester_name': 'TestHero'})
        a.expect(0x46)
        b.expect(0x46)
        a.send_c2s(self.k['trade_lock'], {'gold': 950})
        a.expect(0x48)
        b.expect(0x48)
        self.report(a, 2, 'Watcher')
        self.assertIn('That gold is offered in a trade.', self.notice(a.expect(0x15), a))
        self.assertEqual((self.gold('TestHero'), self.social('test')['report_day']), (1000, None))
        self.assertIsNotNone(self.server.trade.trade_of(a.session))

    def test_category_is_clamped_and_content_kept(self):
        self.report(self.a, 2, 'Watcher', 'hack tool 한'.encode('cp949'), category=9)
        self.assertEqual(self.result(self.a, 0x95)['result'], 1)
        r = self.reports()[0]
        self.assertEqual((r['category'], r['category_name'], r['category_sent'], r['content']),
                         (6, 'Hacking Tools', 9, 'hack tool 한'))

    def test_no_request_is_unhandled(self):
        with self.assertLogs('WS', logging.INFO) as cm:
            self.praise(self.a, 2, 'Watcher')
            self.a.recv_until_quiet(0.3)
            self.report(self.a, 2, 'Watcher')
            self.a.recv_until_quiet(0.3)
        text = '\n'.join(cm.output)
        self.assertNotIn('Unhandled opcode', text)
        self.assertNotIn('MUST-REPLY', text)
        self.assertIn("[MANNER] 'TestHero' compliments 'Watcher'", text)
        self.assertIn("[REPORT] #1 'TestHero' -> 'Watcher'", text)

    # ============================================================ dev commands ===
    def test_dev_rep_and_reports(self):
        """`!rep praise` / `!rep report` run the C2S paths; `!rep` shows the limits, `!rep
        reset` clears them; `!reports` lists the journal."""
        a, b = self.a, self.b
        srv = self.server
        srv._dev_rep(a.session, 'praise Watcher')
        self.assertEqual(_ops(a.recv_until_quiet(0.3)), [0x94, 0x97])
        b.expect(0x96)
        srv._dev_rep(a.session, 'report Watcher 2 item fraud')
        self.assertEqual(self.result(a, 0x95), {'result': 1, 'gold': 900})
        self.assertEqual(self.reports()[0]['category_name'], 'Item Fraud')
        srv._dev_rep(a.session, 'Watcher')
        lines = [self.notice(p, a) for p in a.recv_until_quiet(0.3)]
        self.assertIn('Watcher (account admin): manner 1', lines[0])
        self.assertIn('received today: 1/5', lines[1])
        srv._dev_rep(a.session, '')
        lines = [self.notice(p, a) for p in a.recv_until_quiet(0.3)]
        self.assertIn('complimented today: yes', lines[1])
        self.assertIn('reported today: yes; recent: admin 2026-09-25', lines[2])
        srv._dev_rep(a.session, 'reset')
        lines = [self.notice(p, a) for p in a.recv_until_quiet(0.3)]
        self.assertIn('limits of TestHero cleared', lines[0])
        self.assertEqual(self.social('test'), social.default_account_social())
        self.assertEqual(self.manner('admin'), 1)                  # manner itself stays
        srv._dev_reports(a.session, '')
        lines = [self.notice(p, a) for p in a.recv_until_quiet(0.3)]
        self.assertEqual(len(lines), 1)
        self.assertIn('#1 09-25 12:00 TestHero > Watcher Item Fraud: item fraud', lines[0])


class _CapConfig:
    config = {'COMPLIMENT_DAILY_CAP': 1}

    def test_daily_received_cap_is_0x94_5(self):
        """COMPLIMENT_DAILY_CAP 1: after A's compliment, Carol's for B the same day is {5}; the
        next day it goes through."""
        a, b = self.a, self.b
        carol = self.carol()
        self.praise(a, 2, 'Watcher')
        a.recv_until_quiet(0.3)
        self.assertEqual(_ops(carol.recv_until_quiet(0.2)), [0x97])  # Carol holds B
        self.praise(carol, 2, 'Watcher')
        self.assertEqual(self.result(carol, 0x94), {'result': REP.COMPLIMENT_TARGET_CAPPED})
        self.next_day()
        self.praise(carol, 2, 'Watcher')
        self.assertEqual(carol.s2c(carol.recv_until_quiet(0.3)[0]), {'result': 1, 'target_name': 'Watcher'})
        self.assertEqual(self.manner('admin'), 2)


class Reputation2008(_Reputation, unittest.TestCase):
    build = B8


class Reputation2009(_Reputation, unittest.TestCase):
    build = B9


class Cap2008(_CapConfig, _Base, unittest.TestCase):
    build = B8


class Cap2009(_CapConfig, _Base, unittest.TestCase):
    build = B9


if __name__ == '__main__':
    unittest.main()
