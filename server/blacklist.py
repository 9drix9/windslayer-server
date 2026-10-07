#!/usr/bin/env python3
"""
blacklist.py - the 2009 messenger Blacklist (P12 bl-1..bl-3; re_tools/docs/systems_2009/
blacklist_channels.md Part A, ROADMAP_2009_ADDENDUM P12)

    bl = Blacklist(server)                       # GameServer.__init__; register() adds its hooks
    bl.send_list(sock, session)                  # C2S 0x2F -> S2C 0xBD (resync.STAGE_FRIENDS, F-B1)
    bl.add(sock, session, rec)                   # C2S 0x93 -> S2C 0xBE (F-B2)
    bl.remove(sock, session, rec)                # C2S 0x94 -> S2C 0xBF (F-B3)
    bl.gate(target, actor[, by_uid])             # None | 'silent' | 'refuse' (F-B4 / F-B5)
    bl.hides(receiver, sender)                   # the 0x16 / 0x91 fan-out skip (F-B4)
    bl.dev_blacklist(session, 'filter refuse')   # `!blacklist` (GameServer._dev_blacklist)

Client (EN 2009 Build 14 only; the 2008 client has no blacklist - no 0x93/0x94 send site and
no 0xBD..0xBF handler - so every packet and every server-side filter here is gated by build,
`supported()`): the list lives on CMessenger (M+0x34, count +0x40) beside the friends. Every
S2C 0x03 frees it (FUN_0047a850 from the messenger reset FUN_0047a1a0) and the client answers
each 0x03 with C2S 0x2F, so 0xBD goes out after EVERY 0x2F [V blch A.1] - right after the
friend list, in the arch09-resync-bundle (resync.py). 0xBD/0xBE/0xBF are read by SubHandler3,
which does nothing before the first 0x03: they only go to a session the messenger counts as
online (Messenger.is_online: entered the world on this connection).

The id-0 hazard [V blch A.4]: the client's lookup FUN_00484190 matches `node+0x18 == id OR
strcmp(name)`, and most callers pass id 0, so ONE row with char_id 0 blocks every whisper,
chat line, trade and party invite from everyone. No row with uid 0 is ever stored or sent
(ensure / resolve drop it, list_fields asserts it).

Ids: char_id is the target's ACCOUNT uid (the uid every other id-matched path carries: 0x0D
request_id, 0x10 inviter_id, the party popup uid - blch A.4 [I]). So the id-matched client
paths also block the target's sibling characters (open question E-A3); the name-matched ones
(whisper, chat, trade, the 0x4E party invite: id 0 at 0x45BD0E) do not. The server mirrors the
two id-matched INCOMING checks it gates - S2C 0x0D request_id (FUN_00484190 at 0x47C180) and
0x10 inviter_id (0x47C5CD) - with gate(by_uid=True) on C2S 0x30 / 0x33 (messenger.friend_add /
room_invite): a request from a sibling of a listed character is filtered like one from the
character itself, so it leaves no pending request / invite either (P12 review: without it the
receiving client dropped it by uid while the server kept the pending entry - bl-3's "no pending
state"). Stored per CHARACTER (char['blacklist'], the client list is
per character: reset by 0x03, re-sent after 0x2F; E-A4) as
    [{'name': canonical name (<= 16 bytes), 'uid': account uid (!= 0), 'cid': stable id}, ...]
in add order, <= BLACKLIST_MAX. `cid` (ROADMAP_2009_ADDENDUM C4 / X14) is the key: a renamed
character keeps its rows (the store rewriter rename_references + the ON_RENAME re-send), a
deleted one is dropped lazily at the next 0x2F (A.8), and a retired cid never resolves to an
unrelated character (store.retired_cid). The name and the uid are re-resolved from the cid on
every 0x2F, so a hand-edited row {"name": "Bob"} works too (resolve fills uid and cid).

Server-side mirror (bl-3, F-B4; config BLACKLIST_FILTER):
  client   nothing server-side: the receiving client drops the packet itself (retail look),
           which leaves the requester's pending state (trade.py's one prompt per invitee
           answers every other requester "busy" for 30 s, blch A.6);
  silent   (default) the packet is not delivered and NO pending state is created; the
           requester gets what it would get had the receiver's client dropped it: a whisper
           still its 0x09 "<To: X> text" echo, every other request nothing;
  refuse   the consumers' existing refusals (privacy.py lists them): whisper 0x09 0x67, trade
           0x47 4, party 0x15, friend 0x0C 4, chat invite 0x0F 4, mentor 0x7A 0x66.
The mode can be switched at run time with `!blacklist filter client|silent|refuse` (GM; mode()
reads the config on every call, like `!channel scale`; config.json keeps its value for the next
start).
F-B5: the reverse direction (A blacklisted B, then A's client sends A->B anyway - only a
forged client does, the real one refuses it with "Blacklisted user can't use this.") is
dropped the silent way in both server modes. The fan-outs (map chat 0x16, friend chat 0x91)
only skip receivers that blacklisted the speaker (the client does not filter 0x91 at all).

Locks: the records are edited under store.lock (Group.lock -> store.lock, world.py "Locks");
nothing here takes a group lock, so the gate may run under Trades / Party / Messenger locks.
"""
import logging

import chat as chatmod
import config as cfgmod
import gm
import ids
import packets as P
import social
import world as worldmod

log = logging.getLogger('WS')

BUILD_2009 = '2009'
CHAR_FIELD = 'blacklist'
# [V] FUN_0047b3e0 prints "(%u/%u)" with (count, 10); FUN_0047afb0 refuses an add at count 10.
BLACKLIST_MAX = 10
NAME_MAX = chatmod.NAME_MAX                 # str[17] on the wire: 16 bytes + NUL
UID_MAX = 0xFFFFFFFF
# S2C 0xBE / 0xBF result: 1 = done; any other value pops "Wrong user name.\r\nPlease, check
# again." (0x47CF48 / 0x47CFF5) and reads nothing more [V].
RESULT_OK, RESULT_FAILED = 1, 0
# config BLACKLIST_FILTER (bl-3, blch A.7 F-B4).
MODE_CLIENT, MODE_SILENT, MODE_REFUSE = 'client', 'silent', 'refuse'
FILTER_MODES = (MODE_CLIENT, MODE_SILENT, MODE_REFUSE)
DEFAULT_FILTER = MODE_SILENT
# Session flag: a 0xBD went out since the last S2C 0x03 (the ON_RENAME re-send only goes to
# such a client; one still loading gets the new list from its own coming 0x2F).
SYNCED_KEY = 'blacklist_synced'


def supported(client_build):
    """The 2009 client is the only one with a blacklist (no 2008 send site or handler)."""
    return str(client_build) == BUILD_2009


def _int(value, default=0):
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _valid_cid(value):
    # store.valid_cid without the import (store imports this module for its migration).
    return isinstance(value, int) and not isinstance(value, bool) and value in ids.CHARACTER


def entry_name(value):
    """A stored / compared name: NUL-cut, <= 16 bytes (cp949), str."""
    return social.name_text(value) if value is not None else ''


def key(name):
    return social.key(name)


# ------------------------------------------------------------------ records ---
def ensure(char):
    """Create or repair char['blacklist'] (structural, idempotent; store migration and
    new_character): a list of {'name', 'uid', 'cid'} dicts, each with a non-empty name; uid an
    int in u32 (0 = not resolved yet: resolve() fills or drops it, nothing ever sends it); cid
    kept only when valid. A name or cid listed twice keeps its first row; at most
    BLACKLIST_MAX rows (the client's cap). Returns the list."""
    out, names, cids = [], set(), set()
    value = char.get(CHAR_FIELD)
    for entry in value if isinstance(value, list) else []:
        if isinstance(entry, (str, bytes)):
            entry = {'name': entry}
        if not isinstance(entry, dict):
            continue
        name = entry_name(entry.get('name'))
        if not name or key(name) in names:
            continue
        uid = _int(entry.get('uid'))
        row = {'name': name, 'uid': uid if 0 <= uid <= UID_MAX else 0}
        cid = entry.get('cid')
        if _valid_cid(cid):
            if cid in cids:
                continue
            cids.add(cid)
            row['cid'] = cid
        names.add(key(name))
        out.append(row)
        if len(out) >= BLACKLIST_MAX:
            break
    char[CHAR_FIELD] = out
    return out


def entries(char):
    value = (char or {}).get(CHAR_FIELD)
    return value if isinstance(value, list) else []


def _lookup(accounts):
    """(by cid, by lower name) -> (account, character) over an accounts dict."""
    by_cid, by_name = {}, {}
    for acc in accounts.values():
        if not isinstance(acc, dict):
            continue
        for ch in acc.get('characters') or []:
            if not isinstance(ch, dict):
                continue
            if _valid_cid(ch.get('cid')):
                by_cid.setdefault(ch['cid'], (acc, ch))
            if ch.get('name'):
                by_name.setdefault(key(ch['name']), (acc, ch))
    return by_cid, by_name


def resolve_rows(char, by_cid, by_name):
    """Re-resolve one character's rows against the store (F-B1 / A.8): a row with a cid
    follows that character (its current name, its account uid) or is dropped when the cid is
    gone - never re-matched by name, so a deleted character's row can never land on a new
    character that took the name; a row without a cid (hand-edited) is matched by name and
    gets the cid. Dropped too: the character itself or its own account's characters (the
    uid would be the receiver's own), an account uid of 0, a repeat. Returns (rows, changes,
    dropped); the caller writes rows back when changes is non-empty."""
    me_cid = char.get('cid')
    own = by_cid.get(me_cid) if _valid_cid(me_cid) else by_name.get(key(char.get('name')))
    own_uid = _int((own or ({}, {}))[0].get('uid'))
    rows, changes, dropped, seen = [], [], [], set()
    for row in entries(char):
        cid = row.get('cid')
        found = by_cid.get(cid) if _valid_cid(cid) else by_name.get(key(row.get('name')))
        if found is None:
            dropped.append(row.get('name'))
            continue
        acc, target = found
        uid = _int(acc.get('uid'))
        if target is char or uid == 0 or (own_uid and uid == own_uid) or target.get('cid') in seen:
            dropped.append(row.get('name'))
            continue
        fixed = {'name': entry_name(target.get('name')), 'uid': uid}
        if _valid_cid(target.get('cid')):
            fixed['cid'] = target['cid']
            seen.add(target['cid'])
        if fixed != row:
            changes.append(f'{row.get("name")} -> {fixed["name"]} (uid {uid})')
        rows.append(fixed)
    if dropped:
        changes.append(f'dropped {dropped}')
    return rows[:BLACKLIST_MAX], changes, dropped


def resolve_all(accounts):
    """The store migration's pass (store.migrate_accounts, after the cids exist): every row
    resolved (uid != 0, cid, canonical name) or dropped, so no stored row has uid 0. Idempotent:
    a resolved file gives no change. Returns the change list."""
    by_cid, by_name = _lookup(accounts)
    out = []
    for username, acc in accounts.items():
        for char in (acc or {}).get('characters') or []:
            if not entries(char):
                continue
            rows, changes, _dropped = resolve_rows(char, by_cid, by_name)
            if changes:
                char[CHAR_FIELD] = rows
                out.extend(f'{username}/{char.get("name", "?")}: blacklist {c}' for c in changes)
    return out


def rename_references(store, username, char, old, new):
    """The rename hook's STORED half (ROADMAP_2009_ADDENDUM C4 / X14, store.rename_rewriters):
    every other character's row of the renamed character (by cid, else the old name) takes
    the new name. Stored data only, under db_lock (no group lock, no packet). Returns the
    records rewritten."""
    cid = char.get('cid')
    old_key, touched = key(old), 0
    for acc in store.accounts.values():
        for other in acc.get('characters') or []:
            if other is char:
                continue
            changed = False
            for row in entries(other):
                if (_valid_cid(cid) and row.get('cid') == cid) or (not _valid_cid(row.get('cid'))
                                                                    and key(row.get('name')) == old_key):
                    row['name'] = entry_name(new)
                    changed = True
            touched += int(changed)
    return touched


def lists(char, other):
    """True when `char`'s rows hold the character record `other` (by cid, else by name)."""
    if not isinstance(char, dict) or not isinstance(other, dict):
        return False
    cid, name = other.get('cid'), key(other.get('name'))
    for row in entries(char):
        if _valid_cid(cid) and _valid_cid(row.get('cid')):
            if row['cid'] == cid:
                return True
        elif name and key(row.get('name')) == name:
            return True
    return False


def lists_uid(char, uid):
    """True when one of `char`'s rows carries account uid `uid` (!= 0): the client's id half of
    FUN_00484190 (`node+0x18 == id`), which also matches the listed character's siblings."""
    uid = _int(uid)
    return bool(uid) and isinstance(char, dict) and any(_int(row.get('uid')) == uid for row in entries(char))


def list_fields(rows):
    """S2C 0xBD {count, [{char_id, name}]} (id FIRST, spec_2009 0xBD), <= BLACKLIST_MAX rows,
    never a char_id 0 (A.4: such a node matches every lookup)."""
    out = []
    for row in rows[:BLACKLIST_MAX]:
        uid = _int(row.get('uid'))
        if not 0 < uid <= UID_MAX:
            raise ValueError(f'blacklist row {row!r}: char_id must be a non-zero u32 (blch A.4)')
        out.append({'char_id': uid, 'name': chatmod.name_bytes(row['name'])})
    return {'count': len(out), 'repeat[count]': out}


def add_result(row=None):
    """S2C 0xBE: {1, char_id, name} for an added row, else {0} (1 byte)."""
    if row is None:
        return {'result': RESULT_FAILED}
    uid = _int(row.get('uid'))
    if not 0 < uid <= UID_MAX:
        raise ValueError(f'blacklist row {row!r}: char_id must be a non-zero u32 (blch A.4)')
    return {'result': RESULT_OK, 'char_id': uid, 'name': chatmod.name_bytes(row['name'])}


def remove_result(name=None):
    """S2C 0xBF: {1, name} (the client unlinks every row with exactly that name), else {0}."""
    sent = chatmod.name_bytes(name or b'')
    if not sent:
        return {'result': RESULT_FAILED}
    return {'result': RESULT_OK, 'name': sent}


# ------------------------------------------------------------------ runtime ---
class Blacklist:
    def __init__(self, server):
        self.server = server

    @property
    def store(self):
        return self.server.store

    @property
    def enabled(self):
        return supported(getattr(self.server, 'client_build', None))

    def mode(self):
        """config BLACKLIST_FILTER (client / silent / refuse); silent when unset."""
        value = self.server.config.get('BLACKLIST_FILTER', DEFAULT_FILTER)
        return value if value in FILTER_MODES else DEFAULT_FILTER

    def _online(self, session):
        return self.server.messenger.is_online(session)

    def _char(self, target):
        """The character record of a session (its named character only) or a record itself."""
        if not isinstance(target, dict):
            return None
        if 'username' in target or 'sock' in target or 'char_name' in target:
            return self.server._session_char(target) if target.get('char_name') else None
        return target if target.get('name') else None

    def _send(self, sock, session, key_, fields):
        P.send(self.server, sock, session, key_, fields)

    # ============================================================ F-B1 (bl-1) ===
    def send_list(self, sock, session):
        """C2S 0x2F (resync.STAGE_FRIENDS, after the friend list): S2C 0xBD with every row,
        re-resolved first (A.8: a deleted character's row goes, a renamed one's carries the
        new name, the uid is the account's current one). Count 0 is sent too: it frees the
        already-empty list and redraws [V]. Returns the number of rows sent, None when nothing
        was sent (2008, or before the first 0x03)."""
        if not self.enabled:
            return None
        if not self._online(session):
            log.info(f'[BLACKLIST] 0x2F from {session.get("username")!r} before entering the world - no 0xBD')
            return None
        with self.store.lock:
            char = self._char(session)
            if char is None:
                return None
            ensure(char)
            rows, changes = [], []
            if entries(char):                    # the store walk only for a list that has rows
                by_cid, by_name = _lookup(self.store.accounts)
                rows, changes, _dropped = resolve_rows(char, by_cid, by_name)
                if changes:
                    char[CHAR_FIELD] = rows
            fields = list_fields(rows)
        if changes:
            self.store.mark_dirty(f'blacklist resync {char.get("name")}')
            log.info(f'[BLACKLIST] {char.get("name")!r}: rows re-resolved: {changes}')
        self._send(sock, session, '0xBD', fields)
        session[SYNCED_KEY] = True
        log.info(f'[BLACKLIST] {char.get("name")!r}: 0xBD {fields["count"]}/{BLACKLIST_MAX} '
                 f'{[r["name"] for r in rows]}')
        return fields['count']

    # ============================================================ F-B2 (bl-2) ===
    def add(self, sock, session, rec):
        """C2S 0x93 {name} (dialog 0x4C9, no waiting box) -> S2C 0xBE. Checks in the F-B2 order,
        each failure {0} ("Wrong user name.\\r\\nPlease, check again."): not a character, the
        player's own character (or one of its account: the uid would be its own), already
        listed (by cid: a case variant of a listed name too - the client appends with no
        duplicate check), the list full, a friend / the mentor / a mentee (the client refuses
        friends, and everything while it has a mentor). Success: the row is appended and
        persisted, and {1, uid, name} goes out ONCE. Returns the reason string."""
        if not self.enabled:
            return 'not this build'
        me = session.get('char_name')
        if not self._online(session):
            log.info(f'[BLACKLIST] 0x93 from {session.get("username")!r} outside the world - dropped')
            return 'not in world'
        wanted = entry_name(rec.get('name', b''))
        why, row = None, None
        with self.store.lock:
            char = self._char(session)
            if char is None:
                return 'no character'
            ensure(char)
            found = self.store.character_by_name(wanted) if wanted else None
            if found is None:
                why = 'unknown name'
            else:
                _user, acc, target = found
                uid = _int(acc.get('uid'))
                own = self.store.character_by_name(char.get('name'))
                own_uid = _int(own[1].get('uid')) if own else 0
                if target is char:
                    why = 'own character'
                elif uid == own_uid:
                    why = 'a character of its own account'
                elif lists(char, target):
                    why = 'already listed'
                elif len(entries(char)) >= BLACKLIST_MAX:
                    why = f'list full ({BLACKLIST_MAX})'
                elif social.has_friend(char, target['name']):
                    why = 'a friend'
                elif char.get('mentor') and key(char['mentor']) == key(target['name']):
                    why = 'the mentor'
                elif any(key(n) == key(target['name']) for n in char.get('mentees') or []):
                    why = 'a mentee'
                elif not 0 < uid <= UID_MAX:
                    why = 'no account uid'
                else:
                    row = {'name': entry_name(target['name']), 'uid': uid}
                    if _valid_cid(target.get('cid')):
                        row['cid'] = target['cid']
                    char[CHAR_FIELD].append(row)
        if row is None:
            self._send(sock, session, '0xBE', add_result())
            log.info(f'[BLACKLIST] {me!r} add {wanted!r}: 0xBE 0 ({why})')
            return why
        self.store.mark_dirty(f'blacklist {me} + {row["name"]}')
        self._send(sock, session, '0xBE', add_result(row))
        log.info(f'[BLACKLIST] {me!r} add {row["name"]!r} (uid {row["uid"]}): 0xBE 1, '
                 f'{len(entries(char))}/{BLACKLIST_MAX}')
        return 'added'

    # ============================================================ F-B3 (bl-2) ===
    def remove(self, sock, session, rec):
        """C2S 0x94 {char_id, name} (dialog 0x4CA) -> S2C 0xBF. Keyed on the NAME (the client
        removes by name; char_id is ignored): every row of that name - or of the character now
        called so (cid) - goes and is persisted. {1, name as sent} even when nothing was
        listed (idempotent; it also clears a stale client row), {0} only for an empty name.
        Returns the names removed, or None when refused."""
        if not self.enabled:
            return None
        me = session.get('char_name')
        if not self._online(session):
            log.info(f'[BLACKLIST] 0x94 from {session.get("username")!r} outside the world - dropped')
            return None
        sent = chatmod.name_bytes(rec.get('name', b''))
        wanted = entry_name(sent)
        if not wanted:
            self._send(sock, session, '0xBF', remove_result())
            log.info(f'[BLACKLIST] {me!r} remove with an empty name: 0xBF 0')
            return None
        with self.store.lock:
            char = self._char(session)
            if char is None:
                return None
            ensure(char)
            found = self.store.character_by_name(wanted)
            cid = found[2].get('cid') if found is not None else None
            keep, gone = [], []
            for row in entries(char):
                hit = key(row.get('name')) == key(wanted) or (_valid_cid(cid) and row.get('cid') == cid)
                (gone if hit else keep).append(row)
            if gone:
                char[CHAR_FIELD] = keep
        if gone:
            self.store.mark_dirty(f'blacklist {me} - {wanted}')
        self._send(sock, session, '0xBF', remove_result(sent))
        log.info(f'[BLACKLIST] {me!r} remove {wanted!r}: 0xBF 1 '
                 + (f'(removed {[r["name"] for r in gone]})' if gone else '(was not listed)'))
        return [r['name'] for r in gone]

    # ============================================================ F-B4 (bl-3) ===
    def blocks(self, receiver, sender):
        """True when `receiver`'s list holds `sender` (sessions or character records) - the
        pure model check, whatever the filter mode or the build."""
        with self.store.lock:
            return lists(self._char(receiver), self._char(sender))

    def gate(self, target, actor, by_uid=False):
        """How a request of `actor` to `target` is filtered: None (deliver as usual), 'silent'
        (drop: no packet to the target, no pending state, the requester gets what a dropping
        client would give it) or 'refuse' (the consumer's own refusal reply). The target
        blacklisted the actor -> the configured mode; the actor blacklisted the target (F-B5,
        only a forged client sends it) -> 'silent'. None in 'client' mode and on 2008.
        by_uid: the target's client matches this request by id OR name (S2C 0x0D / 0x10, module
        docstring "Ids"), so a row with the actor's ACCOUNT uid - a sibling character listed -
        counts too."""
        if not self.enabled or target is None or actor is None:
            return None
        mode = self.mode()
        if mode == MODE_CLIENT:
            return None
        with self.store.lock:
            t, a = self._char(target), self._char(actor)
            if t is None or a is None or t is a:
                return None
            if lists(t, a):
                return mode
            if by_uid and lists_uid(t, self._account_uid(a)):
                return mode
            if lists(a, t):
                return MODE_SILENT
        return None

    def _account_uid(self, char):
        """Caller holds store.lock: the account uid of a character record (0: unknown)."""
        found = self.store.character_by_name(char.get('name')) if isinstance(char, dict) else None
        return _int(found[1].get('uid')) if found else 0

    def hides(self, receiver, sender):
        """The fan-out skip (0x16 map chat, 0x91 friend chat): `receiver` blacklisted `sender`
        and the server mirrors the filter (silent / refuse). The reverse direction is not
        filtered here (F-B5 covers requests only)."""
        if not self.enabled or self.mode() == MODE_CLIENT:
            return False
        return self.blocks(receiver, sender)

    # ======================================================== rename (C4) ===
    def renamed(self, session, old, new):
        """The ON_RENAME live half: the store rewriter already renamed every row; each online
        client that holds the renamed character in its list (synced: a 0xBD went out since its
        last 0x03) gets its whole 0xBD again - 0xBD replaces the list, so its rows (and the
        client-side name filter, FUN_00484190) carry the new name. Returns the sessions told."""
        if not self.enabled:
            return []
        told = []
        with self.store.lock:
            subject = self._char(session)
        for s in self.server.world.online():
            if s is session or not s.get(SYNCED_KEY) or not self._online(s):
                continue
            with self.store.lock:
                char = self._char(s)
                if char is None or not lists(char, subject):
                    continue
                fields = list_fields(entries(char))
            if self.server._push(s, '0xBD', fields, 'BLACKLIST'):
                told.append(s)
        if told:
            log.info(f'[BLACKLIST] {old!r} is now {new!r}: 0xBD re-sent to '
                     f'{[s.get("char_name") for s in told]}')
        return told

    # ============================================= `!blacklist` (live triage BL) ===
    def dev_blacklist(self, session, args):
        """`!blacklist` (registered below; GameServer._dev_blacklist): the caller's list and the
        filter mode; `!blacklist filter` prints the mode, `!blacklist filter client|silent|refuse`
        switches it at run time - no restart (mode() reads server.config on every call, the
        `!channel scale` pattern). The new mode applies from the next request on; config.json
        keeps its own value for the next start."""
        reply = self.server._gm_reply
        words = str(args or '').split()
        sub = words[0].lower() if words else ''
        if sub in ('', 'list', 'me'):
            reply(session, self.describe(session))
            return
        if sub != 'filter':
            raise gm.DevCommandError(f'unknown sub-command {sub!r}')
        if not self.enabled:
            reply(session, f'No blacklist on the {self.server.client_build} client (the filter does nothing).',
                  'warn')
            return
        old = self.mode()
        if len(words) < 2:
            reply(session, f'BLACKLIST_FILTER is {old!r} (client | silent | refuse).')
            return
        new = words[1].lower()
        if new not in cfgmod.BLACKLIST_FILTERS:
            # short: the router appends the usage (the modes) and one 0x15 line holds ~77 bytes
            raise gm.DevCommandError(f'{words[1]!r}: not a mode')
        self.server.config['BLACKLIST_FILTER'] = new
        who = session.get('char_name') or session.get('username')
        log.info(f'[BLACKLIST] filter {old} -> {new} by {who!r} (until a restart; config.json is unchanged)')
        reply(session, f'BLACKLIST_FILTER {old} -> {new} (until a restart).')

    def describe(self, session):
        """A line for `!msgr`: the stored rows and the filter mode."""
        with self.store.lock:
            rows = [f'{r["name"]}({r.get("uid")})' for r in entries(self._char(session))]
        if not self.enabled:
            return f'blacklist: none on the {self.server.client_build} client'
        return f'blacklist {len(rows)}/{BLACKLIST_MAX} (filter {self.mode()}): {", ".join(rows) or "-"}'


def register(hooks, blacklist):
    """A server map load clears the synced flag (the coming 0x03 frees the client's list); a
    rename re-sends the lists that hold the character (ON_RENAME, the C4 hook)."""
    def before_map_load(server, session, **_):
        session[SYNCED_KEY] = False

    def on_rename(server, session, old=None, new=None, **_):
        blacklist.renamed(session, old, new)

    hooks.register(worldmod.BEFORE_SERVER_MAP_LOAD, before_map_load)
    hooks.register(worldmod.ON_RENAME, on_rename)
    return before_map_load, on_rename


# The '!' command (gm.register: no edit of GameServer.DEV_COMMANDS); its handler is the thin
# GameServer._dev_blacklist, which hands over to server.blacklist (live triage BL: the filter
# mode without a config edit and a restart).
if 'blacklist' not in gm.COMMANDS:
    gm.register('blacklist', gm.DevCommand(
        '_dev_blacklist', '!blacklist [filter [client|silent|refuse]]',
        'your blacklist rows and the server filter (BLACKLIST_FILTER); filter <mode> switches it '
        'until a restart: client = the receiving client drops, silent = the server drops (no '
        'pending state), refuse = the consumers\' refusals ("<X> is rejecting whispers.")',
        owner='bl-3 (P12; live triage BL)', aliases=('bl',)))
