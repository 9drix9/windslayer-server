#!/usr/bin/env python3
"""
reputation.py - compliments (manner points) and player reports (P7 stage 3)
=========================================================================
social_friend-compliment, chat_mail_gm-report (+ its alias social_friend-report-result);
docs/systems/social_friend.md 1.5 / F10 / F11, chat_mail_gm.md 1.6 / F9, roadmap P7 exit 5.

    rep = Reputation(server)                 # GameServer.__init__
    rep.compliment(sock, session, rec)       # C2S 0x6D -> 0x94 {1, name} to the giver, 0x96 to
                                             #   the target, 0x97 to its holders; or 0x94 {2|5|6|7}
    rep.report(sock, session, rec)           # C2S 0x6E -> fee paid, reports.jsonl, 0x95 {1, gold}
                                             #   + a 0x15 line to every online GM; or 0x95 {2|7}
    fee = report_fee(level)                  # the client's own ceil(level / 10) * 100

Both client builds: every packet here is wire-identical (spec_2009 C2S 0x4815C8/0x6D and
0x4819CD/0x6E, S2C 0x94 / 0x95 / 0x96 / 0x97: "identical (fingerprint)"), so nothing below
branches on the build; parsing and building go through packets.py with the server's build.

Where the two requests come from
--------------------------------
- Praise: the player popup window 0x50 (right-click a player; entry "Praise", ctrl 10) opens
  window 0x435 with the clicked entity's uid (+0x134) and name; its confirm sends C2S 0x6D
  {target_id, target_name} (2008 FUN_00472900 @0x4736C9, 2009 FUN_00480430 @0x48155E). The
  client neither closes the window nor changes anything itself: the manner moves only on
  S2C 0x96 / 0x97, and 0x94 is a modal box (live C49, social_friend#24).
- Report: popup entry "Report" (ctrl 11, the clicked uid) or the red HUD Report button (uid 0
  or stale) open window 0x434, which shows "Report fee: N Gold" from ceil(level / 10) * 100
  and pre-checks gold >= fee, a valid name that is not its own, and non-empty contents; then
  C2S 0x6E {target_uid, target_name, report_fee, category, content_len, content} and it hides
  the window (2008 @0x47356C, 2009 @0x481950). It deducts nothing: the server sends the new
  ABSOLUTE gold in S2C 0x95 {1, u64 gold} (live social_friend#29: 123456 -> 99900).
Neither request opens a "Waiting for the server" box, so neither is a registry MUST_REPLY;
every path below still answers what the client has a text for.

Rules (UI 8944 / 8957, social_friend 1.5 / 3.5; the per-ACCOUNT `social` dict that P6's
store migration already created - no schema change, no migration, no backup here)
--------------------------------------------------------------------------------------
  compliment  target = the online, visible session with the popup uid AND name, else the one
              with that name; not found / oneself / one's own account   -> 0x94 {2}
              the giver's account complimented today                    -> 0x94 {7}
              ... complimented the target's account within
              COMPLIMENT_REPEAT_DAYS (7: "the same person only once a week") -> 0x94 {6}
              the target's account received COMPLIMENT_DAILY_CAP today   -> 0x94 {5}
              else manner += COMPLIMENT_DELTA (account-wide: "All characters in one account
              share the same amount of Manner Points"), persisted (debounced store save).
              A second compliment the same day is therefore refused whoever it is for; the
              exit criterion "once per day per pair" holds a fortiori.
  report      target = the online session with that uid AND name, else ANY stored character
              of that name (online or not)                              -> 0x95 {2}
              one of the reporter's own characters: dropped (the client already refuses
              its own name; there is no code for it)
              the reporter's account reported today                     -> 0x95 {7}
              the server's fee (the client's value is only logged when it differs); gold
              locked in a trade offer cannot pay it (trade-escrow-guards) -> 0x15 line
              stored gold < fee (the label was ahead)                   -> 0x3F resync + 0x15
              else pay, report_day = today, append reports.jsonl       -> 0x95 {1, gold}
              and one "[Warning] Report: A -> B (category)" line to every online GM.
"Today" is the server's local date (YYYY-MM-DD strings, as social.ensure_account keeps them).
Penalties stay a GM decision: /manner (C2S 0x06 sub 0x0A) or `!manner` (S2C 0x97).

Manner on the wire (social_friend F10 step 5-7)
-----------------------------------------------
S2C 0x96 {target_uid, delta, complimenter_name} to the TARGET only, always 25 B
(packets.DEFAULT_ASSUME 0x96): its client adds the delta to its entity +0x15D8 and to the
local manner (scene+0xEE0, the Status window) and prints the green "<A> added 1 of your
manner points." S2C 0x97 {uid, delta} to every other client that HOLDS the target
(presence.to_holders, the giver included), so their copy's +0x15D8 - name tag colour and
Player Info - follows; the GM /manner path does the same (P6 stage 4). touch() makes an
in-flight spawn record rebuild with the new karma, and every later 0x02 / 0x04 / 0x05 /
0x07 / 0x52 carries the absolute account value (records.py), so a relog agrees.

Locks (world.py "Locks"): compliment: store.lock only (both accounts' limits and the target's
manner are checked and moved in one step, so two givers racing for the last received slot
cannot both pass), packets sent after it is released. Report: a first once-a-day read under
store.lock alone (F9 order: before the fee checks), then Trades.lock in gold_refusal, then
the reporter's combat_lock (its wallet is mutated under it everywhere) -> store.lock, where
the once-a-day rule is checked again and the fee paid. The audit file has its own leaf lock.

Audit: reports.jsonl next to accounts.json (git-ignored user data), one JSON line per accepted
report; `!reports [n]` shows the last ones to a GM, `!rep` a player's manner and limits.
"""
import datetime
import json
import logging
import os
import threading
import time

import chat as chatmod
import packets as P
import presence
import records as R
import social
import trade as trademod

log = logging.getLogger('WS')

LOG_NAME = 'reports.jsonl'

# ---- S2C 0x94 ComplimentResult (2008 SubHandler3 0x471B61, 2009 0x47D0FF; 3 / 4 / 0 / > 7
# are silently ignored by the client, so only these are ever sent) ----
COMPLIMENT_OK = 1              # + str[17] name: "<%s> has been complimented"
COMPLIMENT_NOT_FOUND = 2       # "The character doesn't exist. Please check and try again."
COMPLIMENT_TARGET_CAPPED = 5   # "The player was got enough compliment for today. ..."
COMPLIMENT_SAME_PLAYER = 6     # "You can't make a compliment of a same player twice."
COMPLIMENT_ONCE_A_DAY = 7      # "You can make a compliment only once a day."

# ---- S2C 0x95 ReportPlayerResult (2008 0x471DA3, 2009 0x47D2BF; others ignored) ----
REPORT_OK = 1                  # + u64 absolute gold: "Reported successively."
REPORT_NOT_FOUND = 2           # "The character doesn't exist. Please check and try again."
REPORT_ONCE_A_DAY = 7          # "You can report a player only once a day."

# Defaults of the config keys (config.py documents them).
COMPLIMENT_DELTA = 1           # social_friend 3.5 (the client prints whatever delta 0x96 carries)
COMPLIMENT_DAILY_CAP = 5       # received per account per day (proposed; retail value unknown, Q9)
COMPLIMENT_REPEAT_DAYS = 7     # UI 8944 "the same person only once a week"

# The client's fee (chat_mail_gm 1.6 step 2, FUN_00473bb0 @0x473C8D): ceil(level / 10) * 100
# from its own level; the live log saw 100 at level 1. The server's minimum keeps a level-0
# record from reporting for free.
REPORT_FEE_STEP = 100
REPORT_FEE_MIN = 100

# Window 0x434 category radios 0x15..0x1B -> CMessenger+0xD8 = ctrl - 0x15, labels from the
# 14-byte string table 0x706888 (2008). The byte is not reset between reports, so anything
# above 6 is clamped (F9 step 3.4).
REPORT_CATEGORIES = ('Abuse', 'Spamming', 'Item Fraud', 'Real Trading', 'Assuming a GM',
                     'Leakage Info', 'Hacking Tools')
REPORT_TEXT_MAX = 255          # u8 content_len

# 0x15 texts (notice_fields adds "[Warning] "; the client buffer is 88 bytes).
SHORT_OF_GOLD_TEXT = 'You are short of gold.'        # the client's own pre-check text


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ================================================================ pure rules ===
def today(now=None):
    """The server's local date as stored in the account `social` dict ('2026-09-25')."""
    return time.strftime('%Y-%m-%d', time.localtime(time.time() if now is None else float(now)))


def days_between(earlier, later):
    """Whole days from one stored date to another, or None when either is not a date."""
    try:
        a = datetime.date.fromisoformat(str(earlier))
        b = datetime.date.fromisoformat(str(later))
    except ValueError:
        return None
    return (b - a).days


def report_fee(level):
    """The fee window 0x434 shows and pre-checks: ceil(level / 10) * 100, at least 100."""
    level = max(0, _int(level))
    return max(REPORT_FEE_MIN, -(-level // 10) * REPORT_FEE_STEP)


def clamp_category(value):
    return min(max(0, _int(value)), len(REPORT_CATEGORIES) - 1)


def category_name(value):
    return REPORT_CATEGORIES[clamp_category(value)]


def compliment_refusal(giver, target_account, target, day, cap=COMPLIMENT_DAILY_CAP,
                       repeat_days=COMPLIMENT_REPEAT_DAYS):
    """The S2C 0x94 refusal code for a compliment from the account whose `social` dict is
    `giver` to the account `target_account` (username) whose dict is `target`, on `day`; None
    when it may go ahead. The order is F10 step 2's: 7, 6, 5."""
    if giver.get('compliment_given_day') == day:
        return COMPLIMENT_ONCE_A_DAY
    last = (giver.get('complimented_accounts') or {}).get(target_account)
    if last is not None and repeat_days > 0:
        gap = days_between(last, day)
        if gap is None or gap < repeat_days:        # an unreadable date counts as recent
            return COMPLIMENT_SAME_PLAYER
    got = target.get('compliments_received') or {}
    if got.get('day') == day and _int(got.get('count')) >= cap:
        return COMPLIMENT_TARGET_CAPPED
    return None


def apply_compliment(giver, target_account, target, day, repeat_days=COMPLIMENT_REPEAT_DAYS):
    """Record an accepted compliment in both `social` dicts (the manner itself is the store's
    adjust_manner). Entries the weekly rule no longer needs are pruned, so the dict stays
    at most one week of givings."""
    giver['compliment_given_day'] = day
    seen = giver.get('complimented_accounts')
    if not isinstance(seen, dict):
        seen = giver['complimented_accounts'] = {}
    for account, when in list(seen.items()):
        gap = days_between(when, day)
        if gap is not None and gap >= max(1, repeat_days):
            del seen[account]
    seen[target_account] = day
    got = target.get('compliments_received') or {}
    count = _int(got.get('count')) if got.get('day') == day else 0
    target['compliments_received'] = {'day': day, 'count': count + 1}


def reset_limits(soc):
    """Clear an account's compliment / report limits (dev `!rep reset`, live re-tests)."""
    soc.update(social.default_account_social())


# ================================================================= runtime ===
class Reputation:
    def __init__(self, server):
        self.server = server
        self._log_lock = threading.Lock()
        self.clock = time.time          # tests pin the day through this

    # ------------------------------------------------------------ plumbing ---
    @property
    def store(self):
        return self.server.store

    @property
    def log_path(self):
        """reports.jsonl next to accounts.json (offline tests: their temp directory)."""
        return os.path.join(os.path.dirname(os.path.abspath(self.store.path)), LOG_NAME)

    def today(self):
        return today(self.clock())

    def _config(self, key, default):
        return _int(self.server.config.get(key, default), default)

    def _send(self, sock, session, key, fields):
        """A reply on the requester's own connection (its handler thread)."""
        P.send(self.server, sock, session, key, fields)

    def _find_online(self, session, uid, wanted):
        """The in-world session the popup meant: the uid when its character has the typed
        name (a stale uid is ignored, the name wins, as in messenger.friend_add), else the
        name; a GM in shadow is nobody to a non-GM (presence.visible_to)."""
        world = self.server.world
        live = world.find(uid) if uid else None
        if live is not None and wanted and social.key(live.get('char_name')) != social.key(wanted):
            live = None
        if live is None and wanted:
            live = world.find(wanted)
        if live is None or not live.get('char_name') or not presence.visible_to(live, session):
            return None
        return live

    # ============================================================== F10 ===
    def compliment(self, sock, session, rec):
        """C2S 0x6D {target_id, target_name} -> S2C 0x94 to the giver (+ 0x96 to the target
        and 0x97 to its holders on success). See the module docstring for the rules."""
        me = session.get('char_name')
        if not session.get('in_world') or not me:
            log.info(f'[MANNER] compliment from {session.get("username")!r} outside the world - dropped')
            return
        uid = _int(rec.get('target_id'))
        wanted = chatmod.name_text(rec.get('target_name', b''))
        target = self._find_online(session, uid, wanted)
        if target is None or target is session or target.get('username') == session.get('username'):
            why = 'not online' if target is None else 'own account'
            return self._compliment_refused(sock, session, COMPLIMENT_NOT_FOUND, wanted, why)
        other, other_account = target['char_name'], target.get('username')
        day = self.today()
        delta = self._config('COMPLIMENT_DELTA', COMPLIMENT_DELTA)
        cap = self._config('COMPLIMENT_DAILY_CAP', COMPLIMENT_DAILY_CAP)
        repeat = self._config('COMPLIMENT_REPEAT_DAYS', COMPLIMENT_REPEAT_DAYS)
        with self.store.lock:
            giver_acc = self.store.account(session.get('username'))
            target_acc = self.store.account(other_account)
            if giver_acc is None or target_acc is None:
                code, why = COMPLIMENT_NOT_FOUND, 'no account record'
            else:
                giver, got = social.ensure_account(giver_acc), social.ensure_account(target_acc)
                code = compliment_refusal(giver, other_account, got, day, cap, repeat)
                why = {COMPLIMENT_ONCE_A_DAY: f'already complimented today, {day}',
                       COMPLIMENT_SAME_PLAYER: f'complimented {other_account!r} on '
                                               f'{(giver.get("complimented_accounts") or {}).get(other_account)} '
                                               f'(once per {repeat} day(s))',
                       COMPLIMENT_TARGET_CAPPED: f'{other_account!r} got {cap} compliment(s) today'}.get(code)
            if code is None:
                apply_compliment(giver, other_account, got, day, repeat)
                before = self.store.manner(other_account)
                value = self.store.adjust_manner(other_account, delta)
                self.store.mark_dirty(f'compliment {me} -> {other}')
        if code is not None:
            return self._compliment_refused(sock, session, code, other, why)
        target_uid = P.session_uid(target) or 0
        self._send(sock, session, '0x94', {'result': COMPLIMENT_OK, 'target_name': chatmod.name_bytes(other)})
        told = self.server._push(target, '0x96', {'target_uid': target_uid, 'manner_delta': delta,
                                                  'complimenter_name': chatmod.name_bytes(me)}, 'MANNER')
        seen = presence.to_holders(self.server, target, '0x97', {'uid': target_uid, 'manner_delta': delta},
                                   'MANNER')
        log.info(f'[MANNER] {me!r} compliments {other!r} (account {other_account!r}): manner {before} -> '
                 f'{value} (0x94 {{1}}, 0x96 {"queued" if told else "NOT sent"}, 0x97 to {seen} holder(s))')
        return value

    def _compliment_refused(self, sock, session, code, name, why):
        fields = {'result': code}
        self._send(sock, session, '0x94', fields)
        log.info(f'[MANNER] {session.get("char_name")!r} compliment of {name!r}: 0x94 {{{code}}} ({why})')
        return None

    # =============================================================== F9 ===
    def report(self, sock, session, rec):
        """C2S 0x6E {target_uid, target_name, report_fee, category, content_len, content} ->
        S2C 0x95 (see the module docstring for the rules and their order)."""
        me = session.get('char_name')
        if not session.get('in_world') or not me:
            log.info(f'[REPORT] report from {session.get("username")!r} outside the world - dropped')
            return
        uid = _int(rec.get('target_uid'))
        wanted = chatmod.name_text(rec.get('target_name', b''))
        sent_fee = _int(rec.get('report_fee'))
        raw_category = _int(rec.get('category'))
        content = P.to_bytes(rec.get('content', b''))[:REPORT_TEXT_MAX]
        live = self._find_online(session, uid, wanted)
        found = self.store.character_by_name(live['char_name'] if live is not None else wanted) \
            if (live is not None or wanted) else None
        if found is None:
            self._send(sock, session, '0x95', {'result': REPORT_NOT_FOUND})
            log.info(f'[REPORT] {me!r} report on {wanted!r} (uid {uid}): 0x95 {{2}} (no such character)')
            return
        target_account, _acc, target_char = found
        other = target_char.get('name') or wanted
        if target_account == session.get('username'):
            log.info(f'[REPORT] {me!r} reported its own account\'s {other!r} - dropped (the client blocks it)')
            return
        day = self.today()
        # F9 step 3.3 before the fee and gold checks (3.4-3.5): a second report the same day
        # gets 0x95 {7} even while its fee could not be paid. A first read only - the
        # authoritative check is the one under combat_lock -> store.lock below.
        with self.store.lock:
            acc = self.store.account(session.get('username'))
            reported = acc is not None and social.ensure_account(acc).get('report_day') == day
        if reported:
            self._send(sock, session, '0x95', {'result': REPORT_ONCE_A_DAY})
            log.info(f'[REPORT] {me!r} report on {other!r} (uid {uid}): 0x95 {{7}} (already reported today, {day})')
            return
        char = self.server._session_char(session)
        level = R.level_of(char)
        fee = report_fee(level)
        if sent_fee != fee:
            log.warning(f'[REPORT] {me!r} (Lv.{level}): client fee {sent_fee} != server fee {fee}; charging {fee}')
        why = self.server.trade.gold_refusal(session, fee)
        if why is not None:
            # trade-escrow-guards: gold locked by a C2S 0x23 offer is not spendable. The client
            # deducted nothing, so its label needs no resync.
            self.server._notice(sock, session, trademod.GOLD_IN_TRADE_TEXT, 'warn')
            log.info(f'[REPORT] {me!r} report on {other!r} refused: {why}')
            return
        wallet = self.server._wallet_of(session)
        code = short = None
        with self.server._combat_lock(session):
            with self.store.lock:
                acc = self.store.account(session.get('username'))
                soc = social.ensure_account(acc) if acc is not None else {}
                if soc.get('report_day') == day:
                    code = REPORT_ONCE_A_DAY
                elif wallet is None or not wallet.pay(gold=fee):
                    short = f'gold {wallet.gold if wallet is not None else "?"} < fee {fee}'
                else:
                    soc['report_day'] = day
                    gold = wallet.gold
        if code is not None:
            self._send(sock, session, '0x95', {'result': code})
            log.info(f'[REPORT] {me!r} report on {other!r} (uid {uid}): 0x95 {{7}} (already reported today, {day})')
            return
        if short is not None:
            # The client pre-checked gold >= fee against its label: the label was ahead of the
            # store. 0x95 has no code for it (F9 step 3.5), so the label is put right (0x3F)
            # and the client's own text explains it.
            self.server._send_gold(sock, session)
            self.server._notice(sock, session, SHORT_OF_GOLD_TEXT, 'warn')
            log.info(f'[REPORT] {me!r} report on {other!r} refused: {short} (0x3F resync)')
            return
        self.server._wallet_commit(session, wallet, 'report fee')
        category = clamp_category(raw_category)
        entry = {'t': time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(self.clock())), 'day': day,
                 'reporter': {'account': session.get('username'), 'name': me, 'uid': P.session_uid(session) or 0,
                              'map': session.get('current_map')},
                 'target': {'account': target_account, 'name': other, 'uid_sent': uid,
                            'online': live is not None},
                 'category': category, 'category_name': REPORT_CATEGORIES[category],
                 'category_sent': raw_category, 'content': content.decode('cp949', 'replace'),
                 'fee': fee, 'fee_sent': sent_fee, 'gold_after': gold}
        entry['id'] = self.audit(entry)
        self._send(sock, session, '0x95', {'result': REPORT_OK, 'gold': gold})
        alerted = self.alert_gms(f'Report #{entry["id"]}: {me} -> {other} ({REPORT_CATEGORIES[category]})')
        log.info(f'[REPORT] #{entry["id"]} {me!r} -> {other!r} ({REPORT_CATEGORIES[category]}, '
                 f'{len(content)} B): fee {fee} -> gold {gold}; 0x95 {{1}}, {alerted} GM(s) alerted')
        return entry

    def alert_gms(self, text):
        """One [Warning] S2C 0x15 line to every in-world GM (F9 step 3.9). Returns how many."""
        sent = 0
        for s in self.server.world.in_world_sessions():
            if s.get('gm') and self.server._push(s, '0x15', self.server.notice_fields(text, 'warn'), 'REPORT'):
                sent += 1
        return sent

    # ------------------------------------------------------------ audit ---
    def audit(self, entry):
        """Append one report to reports.jsonl and return its number (1-based line count).
        A write failure is logged, never raised: the report is already paid for."""
        with self._log_lock:
            try:
                n = len(self.read_reports()) + 1
                entry = dict(entry, id=n)
                with open(self.log_path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + '\n')
                return n
            except (OSError, TypeError, ValueError) as e:
                log.warning(f'[REPORT] audit line not written: {e}')
                return 0

    def read_reports(self):
        """Every stored report (oldest first); an unreadable line is skipped."""
        out = []
        try:
            with open(self.log_path, encoding='utf-8') as f:
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return out

    # ---------------------------------------------------------- dev views ---
    def describe(self, username, name=None):
        """Lines for `!rep`: the account's manner and today's compliment / report state."""
        day = self.today()
        with self.store.lock:
            acc = self.store.account(username)
            if acc is None:
                return [f'no account {username!r}']
            soc = social.ensure_account(acc)
            got = soc.get('compliments_received') or {}
            recent = ', '.join(f'{k} {v}' for k, v in sorted((soc.get('complimented_accounts') or {}).items()))
            return [f'{name or username} (account {username}): manner {self.store.manner(username)}',
                    f'complimented today: {"yes" if soc.get("compliment_given_day") == day else "no"}; '
                    f'received today: {_int(got.get("count")) if got.get("day") == day else 0}'
                    f'/{self._config("COMPLIMENT_DAILY_CAP", COMPLIMENT_DAILY_CAP)}',
                    f'reported today: {"yes" if soc.get("report_day") == day else "no"}; '
                    f'recent: {recent or "-"}']

    def reset(self, username):
        """Clear an account's limits (dev `!rep reset`). False for an unknown account."""
        with self.store.lock:
            acc = self.store.account(username)
            if acc is None:
                return False
            reset_limits(social.ensure_account(acc))
        self.store.mark_dirty(f'reputation limits reset {username}')
        return True

    def report_lines(self, count=5):
        """The last `count` reports, one short line each (`!reports`)."""
        rows = self.read_reports()[-max(1, int(count)):]
        lines = []
        for r in rows:
            who = f'{(r.get("reporter") or {}).get("name")} > {(r.get("target") or {}).get("name")}'
            lines.append(f'#{r.get("id")} {str(r.get("t", ""))[5:16].replace("T", " ")} {who} '
                         f'{r.get("category_name")}: {r.get("content", "")}')
        return lines or ['no reports']
