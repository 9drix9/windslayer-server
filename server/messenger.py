#!/usr/bin/env python3
"""
messenger.py - the messenger at run time (P6 stage 2; docs/systems/social_friend.md F1-F12,
chat_mail_gm.md F6/F7): friend list sync, add / accept / delete, presence and my-status,
chat rooms, mentors and the mentor exp share, notes / memos and friend slots, for both
client builds (spec_2009: every packet here is wire-identical except S2C 0x78, social.py).

    msgr = Messenger(server)                 # GameServer.__init__; register() adds its hooks
    msgr.friend_list(sock, session)          # C2S 0x2F -> 0x0B [+ 0x7E] [+ 0x78]
    msgr.is_online(session)                  # this character counts as online to its friends

One client object, CMessenger (2008 game_state+0x4E4, 2009 +0x508), takes every packet here
through SubHandler3, whose socket copy is set by the first S2C 0x03 - so nothing is ever sent
to a session before it entered the world (is_online), and every S2C 0x03 wipes the client's
friend / mentor / mentee / memo lists, its room id and its status. The client answers each
0x03 with a bare C2S 0x2F: that is the resync (F1), and the only place lists are re-sent.

Who is online (social_friend 3.2/3.3): a session is "online" from its first world entry on
this connection (on_enter_world; not per map load) until it leaves the world for good or the
connection closes; `msgr_name` / `msgr_uid` keep the identity for the offline push after a
character delete cleared char_name. A GM in shadow counts as offline for non-GM friends
(presence.visible_to, the whisper rule).

The login / logout LINES are once per login, not per connection (P12
arch09-session-continuity, continuity.py): a 2009 channel hop (a disconnect plus a relogin
with the same session key) repeats neither. go_online(hop=...) announces only what the hop
changed (the channel number, S2C 0x60); the offline push is the world hook ON_LOGOUT
(logged_out), which continuity fires at once on a plain disconnect and holds back while a hop
may follow - meanwhile the friend rows keep the character online (continuity.ghost).
go_offline only leaves the room and forgets the pending prompts, at once, as before.
`msgr_announced` marks a session whose login the watchers were told (or that continues one).

Server state (all under Messenger.lock):
  requests  {target uid: {requester uid: Pending}}   friend requests, FRIEND_REQUEST_TTL
  invites   {invitee uid: {inviter uid: Pending}}    chat room invites, ROOM_INVITE_TTL
  rooms     {room id: [session, ...]}                ordered members, <= chat.ROOM_MAX;
            session['room_id'] is the member's room. Room ids count from 1 (0 = "no room").
Session keys: msgr_online, msgr_name, msgr_uid, msgr_status (0/2/3), room_id, msgr_synced
(a 0x2F answered since the last 0x03), memo_ids_on_client (the memo ids that client was
shown: C2S 0x44 deletes only those). The Notes themselves are records of the character's
cash inventory (P8 stage 1, cash.py via server.cash): a sent note names and uses up one.

Lock order (world.py "Locks"): world_lock -> combat_lock -> MapMonsters.lock ->
Messenger.lock -> store.lock (db) -> send_lock. Nothing here takes a combat or monster lock
while holding Messenger.lock; the mentor exp share, which credits ANOTHER player (his combat
lock), runs from the tick scheduler after the kill released the killer's locks.
"""
import logging
import threading
import time
from dataclasses import dataclass, field

import blacklist as blmod
import chat as chatmod
import packets as P
import presence
import social
import world as worldmod

log = logging.getLogger('WS')


@dataclass
class Pending:
    """One friend request or room invite: who asked (name + session identity), whom, when.
    Identities, not uids: a 2009 relogin's new session must not answer (or be answered for)
    a prompt its old client showed."""
    name: str
    session: dict = field(repr=False)
    target: dict = field(repr=False)
    t: float


def _uid(session):
    return P.session_uid(session) or 0


class Messenger:
    def __init__(self, server):
        self.server = server
        self.lock = threading.RLock()
        self.requests = {}
        self.invites = {}
        self.rooms = {}
        self._next_room_id = 1

    # ------------------------------------------------------------ identities ---
    @property
    def store(self):
        return self.server.store

    @staticmethod
    def is_online(session):
        """Online for the messenger: entered the world on this connection (so its client has
        CMessenger's socket, the first 0x03) and not closed / kicked."""
        return bool(session is not None and session.get('msgr_online') and session.get('sock') is not None
                    and not session.get('closed') and not session.get('kicked'))

    @staticmethod
    def name_of(session):
        return session.get('msgr_name') or session.get('char_name') or ''

    def find(self, target, viewer=None):
        """The online session of a character name (case-insensitive) or an account uid, as
        `viewer` may see it (a GM in shadow is offline for a non-GM), or None."""
        if target is None or target == '' or target == 0:
            return None
        s = self.server.world.find(target, in_world=False)
        if not self.is_online(s):
            return None
        if viewer is not None and not presence.visible_to(s, viewer):
            return None
        return s

    def char_of(self, session):
        """The character record of an online / entering session (never the account's first
        character as a fallback: the messenger is always about a named character)."""
        name = session.get('char_name')
        return self.server._session_char(session) if name else None

    def channel(self, session):
        return self.server._channel_no(session)

    def _row_of(self, session):
        """The S2C 0x0B / 0x0C row of an online character."""
        return social.friend_row(self.name_of(session), self.channel(session), _uid(session),
                                 session.get('msgr_status', social.STATUS_ONLINE))

    # Every packet is QUEUED (flush=False), also a reply to the requester on its own
    # connection thread: the messenger sends under its lock - so that e.g. a 0x0B built from
    # the online state and a 0x60 about a change of it reach a client in the order the state
    # changed - and a socket write under that lock would let one stalled client stall every
    # other player's friend traffic. The session's outbox writer sends it at once.
    def _send(self, sock, session, key, fields, assume=None):
        """A reply to the requester (queued on its own outbox)."""
        P.send(self.server, sock, session, key, fields, assume, flush=False)

    def _push(self, target, key, fields, tag='MSGR', assume=None):
        """A packet to an online session (queued on its outbox, never raised)."""
        if not self.is_online(target):
            return False
        return self.server._push(target, key, fields, tag, assume, flush=False)

    def watchers(self, subject):
        """The online sessions that list `subject` in their STORED friend list and may see
        it (social_friend F4: "watchers of X")."""
        name = social.key(self.name_of(subject))
        out = []
        for w in self.server.world.online():
            if w is subject or not self.is_online(w) or w.get('uid') == subject.get('uid'):
                continue
            with self.store.lock:
                char = self.char_of(w)
                listed = char is not None and social.has_friend(char, name)
            if listed and presence.visible_to(subject, w):
                out.append(w)
        return out

    def _presence(self, subject, online, status=social.STATUS_ONLINE):
        """S2C 0x60 {uid, name, channel, presence} to every watcher (always the full 23 B:
        only real watchers get it, packets.DEFAULT_ASSUME 0x60). Presence 0 prints
        "<name> has logged in. (Friend)" on the watcher; 1 greys the entry silently."""
        fields = {'friend_uid': _uid(subject) or subject.get('msgr_uid') or 0,
                  'friend_name': chatmod.name_bytes(self.name_of(subject)),
                  'channel': self.channel(subject) if online else 0,
                  'presence': status if online else social.STATUS_OFFLINE}
        told = [w for w in self.watchers(subject) if self._push(w, '0x60', fields, 'FRIEND')]
        return told

    # ================================================================ F1 ===
    def friend_list(self, sock, session):
        """C2S 0x2F (sent by the client after EVERY S2C 0x03) -> S2C 0x0B always (even
        empty: without it the capacity stays 0 and Add Friend says "Can't be add. Visit
        Frenaiga."), then 0x7E with the ONLINE mentees and 0x78 with the stored memos only
        when there are any (a count 0 would light the Msg button). A name whose character is
        gone is dropped from the lists here (F1 step 3)."""
        if not self.is_online(session):
            log.info(f'[MSGR] 0x2F from {session.get("username")!r} before entering the world - ignored')
            return
        with self.lock:
            with self.store.lock:
                char = self.char_of(session)
                if char is None:
                    return
                social.ensure(char)
                rows, keep, dropped = self._friend_rows(session, char)
                mentees, keep_mentees = [], []
                for name in list(char['mentees']):
                    found = self.store.character_by_name(name)
                    if found is None or social.key(found[2].get('mentor')) != social.key(char['name']):
                        dropped.append(f'mentee {name}')
                        continue
                    keep_mentees.append(found[2]['name'])
                    live = self.find(found[2]['name'], viewer=session)
                    if live is not None:
                        mentees.append({'channel': social.SAME_CHANNEL,
                                        'mentee_name': chatmod.name_bytes(self.name_of(live)),
                                        'mentee_uid': _uid(live)})
                if char.get('mentor') and self.store.character_by_name(char['mentor']) is None:
                    dropped.append(f'mentor {char["mentor"]}')
                    char['mentor'] = None
                if dropped:
                    char['friends'], char['mentees'] = keep, keep_mentees
                memos = [dict(m) for m in char['memos']]
                cap = social.capacity(char)
            if dropped:
                self.store.mark_dirty(f'messenger drops {char.get("name")}')
                log.info(f'[MSGR] {char.get("name")!r}: dropped deleted character(s) {dropped}')
            rows = rows[:social.LIST_MAX]
            self._send(sock, session, '0x0B', {'friend_capacity': cap, 'friend_count': len(rows),
                                               'repeat[friend_count]': rows})
            mentees = mentees[:social.LIST_MAX]
            if mentees:
                self._send(sock, session, '0x7E', {'count': len(mentees), 'repeat[count]': mentees})
            for fields in social.memo_packets(memos, self.server.client_build):
                self._send(sock, session, '0x78', fields)
            session['memo_ids_on_client'] = {m['id'] for m in memos}
            session['msgr_synced'] = True
        online = sum(1 for r in rows if r['status'] != social.STATUS_OFFLINE)
        log.info(f'[MSGR] {self.name_of(session)!r}: 0x0B {len(rows)}/{cap} friend(s) ({online} online)'
                 + (f', 0x7E {len(mentees)} mentee(s)' if mentees else '')
                 + (f', 0x78 {len(memos)} memo(s)' if memos else ''))

    def _friend_rows(self, session, char):
        """(S2C 0x0B rows, the canonical names kept, the names whose character is gone) of
        `char`'s stored friend list as `session` may see it. Caller holds self.lock and
        store.lock."""
        rows, keep, dropped = [], [], []
        for name in list(char['friends']):
            found = self.store.character_by_name(name)
            if found is None:
                dropped.append(name)
                continue
            _user, acc, fchar = found
            canonical = fchar['name']
            keep.append(canonical)
            live = self.find(canonical, viewer=session)
            if live is not None:
                rows.append(self._row_of(live))
                continue
            # arch09-session-continuity: a logout held back for a possible channel hop keeps
            # the row online (no 0x60 offline went out, and none lights it again after a hop).
            held = self._held(canonical, session)
            if held is not None:
                rows.append(social.friend_row(canonical, held.channel, held.uid or acc.get('uid') or 0,
                                              held.session.get('msgr_status', social.STATUS_ONLINE)))
            else:
                rows.append(social.friend_row(canonical, 0, acc.get('uid') or 0, social.STATUS_OFFLINE))
        return rows, keep, dropped

    def _held(self, name, viewer):
        """The continuity.Departure of `name` whose logout is held back (a possible 2009
        channel hop) and that `viewer` may see, or None."""
        cont = getattr(self.server, 'continuity', None)
        dep = cont.ghost(name) if cont is not None else None
        if dep is None or not dep.session.get('msgr_announced'):
            return None
        return dep if presence.visible_to(dep.session, viewer) else None

    def renamed(self, session, old, new):
        """premium_cash-rename (P8 stage 3; the world.ON_RENAME hook, ROADMAP_2009_ADDENDUM C4):
        the store already points every friend list, mentor and mentee name at the new name
        (store.rewrite_social_references). Each online watcher whose client has its list
        (msgr_synced: a 0x2F was answered since its last 0x03 - one still loading gets the new
        list from its own coming 0x2F) is sent its whole S2C 0x0B again: 0x0B replaces the
        list, while 0x60 finds an entry by NAME (the old one) and could never rename it.
        The mentor: an online, synced mentor's client holds this character as a mentee row
        found by UID, which S2C 0x7B zeroes and refills in place (spec 0x7B) - so the row takes
        the new name (the client also prints its "<name> has logged in. (Menti)" line; no
        packet renames the row silently). The mentees: their client holds the mentor from the
        S2C 0x03 mentor block (mentor_fields_03) and nothing renames it in place (0x7D sets
        the status only, 0x7A appends a second record), so they see the new name at their
        next map load. Returns the sessions told (watchers, then the mentor)."""
        told = []
        with self.lock:
            for w in self.watchers(session):
                if not w.get('msgr_synced'):
                    continue
                with self.store.lock:
                    char = self.char_of(w)
                    if char is None:
                        continue
                    social.ensure(char)
                    rows, _keep, _dropped = self._friend_rows(w, char)
                    cap = social.capacity(char)
                rows = rows[:social.LIST_MAX]
                if self._push(w, '0x0B', {'friend_capacity': cap, 'friend_count': len(rows),
                                          'repeat[friend_count]': rows}, 'FRIEND'):
                    told.append(w)
            with self.store.lock:
                own = self.char_of(session)
                mentor = (own or {}).get('mentor')
            m = self.find(mentor, viewer=session) if mentor else None
            mentor_told = False
            if m is not None and m.get('msgr_synced'):
                mentor_told = self._push(m, '0x7B', {'mentee_uid': session.get('msgr_uid') or _uid(session),
                                                     'channel': social.SAME_CHANNEL,
                                                     'mentee_name': chatmod.name_bytes(new)}, 'MENTOR')
                if mentor_told:
                    told.append(m)
        log.info(f'[MSGR] {old!r} is now {new!r}: 0x0B friend list re-sent to '
                 f'{[self.name_of(w) for w in told if w is not m]}'
                 + (f', 0x7B mentee row renamed on mentor {self.name_of(m)!r}' if mentor_told else ''))
        return told

    # ================================================================ F2 ===
    def friend_add(self, sock, session, rec):
        """C2S 0x30 {target_uid, target_name} (window 0x179: uid 0; popup 0x50 "Add as
        Friend": the clicked entity's uid) -> S2C 0x0D to the target, or a 0x0C refusal
        (0x15 unknown, 6 already listed, 7 own list full, 2 not online, 4 refuses friends,
        5 their list full). A request already pending is not prompted twice."""
        if not self.is_online(session):
            return
        me = self.name_of(session)
        wanted = social.name_text(rec.get('target_name', b''))
        uid = int(rec.get('target_uid', 0) or 0)
        with self.lock:
            live = self.find(uid, viewer=session) if uid else None
            if live is not None and wanted and social.key(self.name_of(live)) != social.key(wanted):
                live = None                            # the uid moved on: trust the name
            found = self.store.character_by_name(self.name_of(live) if live is not None else wanted)
            if found is None:
                return self._add_refused(sock, session, social.ADD_ERROR, wanted, 'unknown name')
            other = found[2]['name']
            if social.key(other) == social.key(me):
                log.info(f'[FRIEND] {me!r} asked to add itself - dropped (the client blocks it)')
                return
            with self.store.lock:
                char = self.char_of(session)
                already, full = social.has_friend(char, other), social.is_full(char)
            if already:
                return self._add_refused(sock, session, social.ADD_ALREADY, other, 'already listed')
            if full:
                return self._add_refused(sock, session, social.ADD_MY_LIST_FULL, other,
                                         f'own list full ({social.capacity(char)})')
            target = live if live is not None else self.find(other, viewer=session)
            if target is None:
                return self._add_refused(sock, session, social.ADD_NOT_ONLINE, other, 'not online')
            if self.server.blacklist_drops(target, session, by_uid=True):
                # P12 bl-3, BLACKLIST_FILTER 'silent' (blch F-B4): the target's client would drop
                # the 0x0D unread (FUN_00484190 at 0x47C180, by request_id OR name: a listed
                # character's sibling too - by_uid) - no prompt, no pending request, nothing
                # back to the requester.
                log.info(f'[FRIEND] {me!r} -> {other!r}: blacklisted - dropped (no prompt, no pending request)')
                return
            if self.server.refuses(target, 'friend', session, by_uid=True):
                return self._add_refused(sock, session, social.ADD_FRIEND_OFF, other,
                                         'refuses friend requests (privacy flag or blacklist refuse mode)')
            with self.store.lock:
                their_full = social.is_full(self.char_of(target))
            if their_full:
                return self._add_refused(sock, session, social.ADD_THEIR_LIST_FULL, other, 'their list full')
            now = time.monotonic()
            self._prune(self.requests, social.FRIEND_REQUEST_TTL, now)
            old = self._pending(self.requests, target, session)
            if old is not None and now - old.t < social.FRIEND_REQUEST_TTL:
                log.info(f'[FRIEND] {me!r} -> {other!r}: request already pending - not prompted again')
                return
            self.requests.setdefault(_uid(target), {})[_uid(session)] = Pending(me, session, target, now)
            self._push(target, '0x0D', {'request_id': _uid(session), 'requester_name': chatmod.name_bytes(me)},
                       'FRIEND')
        log.info(f'[FRIEND] {me!r} asks {other!r} to be friends (0x0D)')

    def _add_refused(self, sock, session, result, name, why):
        self._send(sock, session, '0x0C', social.add_result(result, name))
        log.info(f'[FRIEND] {self.name_of(session)!r} add {social.name_text(name)!r}: 0x0C {result:#x} ({why})')

    def friend_reply(self, sock, session, rec):
        """C2S 0x31 {requester_id, requester_name, accept} from the prompt 0x17C. Refuse:
        0x0C 3 to the requester. Accept: both capacities are checked again, then the MUTUAL
        add is persisted and each side gets 0x0C with the other's row - 1 to the requester,
        0x0B to the accepter - but only a side whose list really grew (0x0C appends a node
        with no duplicate check)."""
        if not self.is_online(session):
            return
        rid = int(rec.get('requester_id', 0) or 0)
        accept = bool(rec.get('accept'))
        me = self.name_of(session)
        with self.lock:
            req = self.requests.get(_uid(session), {}).pop(rid, None)
            if req is None or req.target is not session or time.monotonic() - req.t >= social.FRIEND_REQUEST_TTL:
                log.info(f'[FRIEND] {me!r} answered a friend request of uid {rid} that is not pending - dropped')
                return
            asker = req.session if self.is_online(req.session) else None
            if asker is None:
                self._send(sock, session, '0x0C', social.add_result(social.ADD_NOT_ONLINE, req.name))
                log.info(f'[FRIEND] {me!r} answered {req.name!r}, who left: 0x0C 2')
                return
            if not accept:
                self._push(asker, '0x0C', social.add_result(social.ADD_REFUSED, me), 'FRIEND')
                log.info(f'[FRIEND] {me!r} refused {req.name!r} (0x0C 3 to {req.name!r})')
                return
            with self.store.lock:
                mine, theirs = self.char_of(session), self.char_of(asker)
                if mine is None or theirs is None:
                    return
                if social.is_full(theirs) and not social.has_friend(theirs, me):
                    self._push(asker, '0x0C', social.add_result(social.ADD_MY_LIST_FULL, me), 'FRIEND')
                    self._send(sock, session, '0x0C', social.add_result(social.ADD_THEIR_LIST_FULL, req.name))
                    log.info(f'[FRIEND] {me!r} accepted {req.name!r}, whose list is full now')
                    return
                if social.is_full(mine) and not social.has_friend(mine, req.name):
                    self._push(asker, '0x0C', social.add_result(social.ADD_THEIR_LIST_FULL, me), 'FRIEND')
                    self._send(sock, session, '0x0C', social.add_result(social.ADD_MY_LIST_FULL, req.name))
                    log.info(f'[FRIEND] {me!r} accepted {req.name!r} with a full list')
                    return
                added_theirs = social.add_friend(theirs, me)
                added_mine = social.add_friend(mine, self.name_of(asker))
            self.store.mark_dirty(f'friends {req.name} + {me}')
            if added_theirs:
                self._push(asker, '0x0C', social.add_result(social.ADD_OK, me, self._row_of(session)), 'FRIEND')
            if added_mine:
                self._send(sock, session, '0x0C', social.add_result(social.ADD_OK_ACCEPTER, req.name,
                                                                    self._row_of(asker)))
        log.info(f'[FRIEND] {req.name!r} and {me!r} are friends (0x0C 1 to {req.name!r}: {added_theirs}, '
                 f'0x0C 0x0B to {me!r}: {added_mine})')

    # ================================================================ F3 ===
    def friend_delete(self, sock, session, rec):
        """C2S 0x32 {friend_id, friend_name} -> S2C 0x0E {1, name} (the client removes the
        node by strcmp on the name it sent, so it is echoed as sent) or {0, name} when the
        name is not listed. Directed: the other side's list and client are untouched (0x0E
        always pops a box; social_friend 7.1)."""
        if not self.is_online(session):
            return
        sent = chatmod.name_bytes(rec.get('friend_name', b''))
        with self.lock:
            with self.store.lock:
                char = self.char_of(session)
                gone = social.remove_friend(char, sent.decode('cp949', 'replace')) if char is not None else None
            if gone is not None:
                self.store.mark_dirty(f'friend delete {self.name_of(session)} - {gone}')
            self._send(sock, session, '0x0E', {'success': int(gone is not None), 'name': sent})
        log.info(f'[FRIEND] {self.name_of(session)!r} deletes {social.name_text(sent)!r}: '
                 + ('removed (0x0E 1)' if gone is not None else 'not in the list (0x0E 0)'))

    # ================================================================ F4 ===
    def set_status(self, session, rec):
        """C2S 0x37 {status}: 0 Online / 2 Busy / 3 AFK (anything else is Online), stored and
        pushed as S2C 0x60 {uid, name, channel, status} to the watchers. Never answered to
        the sender (registry.NEVER_REPLY 0x37)."""
        if not self.is_online(session):
            return
        status = social.clamp_status(rec.get('status', 0))
        with self.lock:
            session['msgr_status'] = status
            told = self._presence(session, True, status)
        log.info(f'[FRIEND] {self.name_of(session)!r} status {status} -> 0x60 to {len(told)} friend(s)')

    def go_online(self, session, hop=None):
        """First world entry of this connection (on_enter_world): watchers get S2C 0x60
        presence 0 ("<X> has logged in. (Friend)"), the mentor 0x7B (this mentee, "(Menti)"),
        each online mentee 0x7D {uid, 100} ("<X> has logged in.(Mentor)").

        hop (continuity.Hop, arch09-session-continuity): this connection continues the login
        of a 2009 channel change. The watchers saw no logout, so no login line: only a 0x60
        with the new channel when the channel number changed (the client has no silent
        channel update; P18 ch-3 makes it presence 4 for watchers on another channel), and
        nothing for a hop back to the same channel. A hop whose logout already went out
        (hop.logout_announced) is announced like a login."""
        char = self.char_of(session)
        if char is None:
            return
        quiet = hop is not None and not hop.logout_announced
        with self.lock:
            session.update(msgr_online=True, msgr_name=char['name'], msgr_uid=_uid(session),
                           msgr_status=social.STATUS_ONLINE, msgr_synced=False, msgr_announced=True)
            session['memo_ids_on_client'] = set()
            if quiet:
                told = self._presence(session, True) if hop.changed_channel else []
                log.info(f'[FRIEND] {char["name"]!r} continues its login on channel {hop.to_channel} '
                         f'(channel hop from {hop.from_channel}): '
                         + (f'0x60 with the new channel to {len(told)} friend(s)' if hop.changed_channel
                            else 'same channel, nothing to the friends'))
                return
            told = self._presence(session, True)
            with self.store.lock:
                mentor, mentees = char.get('mentor'), list(char.get('mentees') or [])
            m = self.find(mentor, viewer=session) if mentor else None
            if m is not None:
                self._push(m, '0x7B', {'mentee_uid': _uid(session), 'channel': social.SAME_CHANNEL,
                                       'mentee_name': chatmod.name_bytes(char['name'])}, 'MENTOR')
            pupils = [s for s in (self.find(n, viewer=session) for n in mentees) if s is not None]
            for s in pupils:
                self._push(s, '0x7D', {'mentor_uid': _uid(session), 'channel_or_state': social.SAME_CHANNEL},
                           'MENTOR')
        log.info(f'[FRIEND] {char["name"]!r} online: 0x60 to {len(told)} friend(s)'
                 + (f', 0x7B to mentor {mentor!r}' if m is not None else '')
                 + (f', 0x7D to {len(pupils)} mentee(s)' if pupils else ''))

    def go_offline(self, session, reason='', superseded=False):
        """The character leaves the world on this connection (disconnect, kick, delete): its
        room is left (0x62 to the rest) and pending requests and invites go, at once.
        Idempotent. The offline LINES are logged_out (ON_LOGOUT), which continuity.py fires
        right after this on a plain disconnect and holds back while a 2009 channel hop (or
        the relogin that superseded this session) may continue the login."""
        with self.lock:
            if not session.get('msgr_online'):
                self._forget(session)
                return
            session['msgr_online'] = False
            self.leave_room(session, reason or 'left the world')
            self._forget(session)
            if superseded:
                log.info(f'[FRIEND] {self.name_of(session)!r} replaced by a newer session: offline push '
                         f'left to its continuation')

    def logged_out(self, session, reason=''):
        """ON_LOGOUT (continuity.py): the login is over for good - watchers get 0x60 {uid,
        name, 0, 1}, the mentor 0x7C (mentees are shown online only), each mentee 0x7D
        {uid, 0x66}. Only for a session whose login the watchers were told (msgr_announced),
        once."""
        with self.lock:
            if not session.pop('msgr_announced', False):
                return
            told = self._presence(session, False)
            with self.store.lock:
                char = self.store.character_by_name(self.name_of(session))
                char = char[2] if char else {}
                mentor, mentees = char.get('mentor'), list(char.get('mentees') or [])
            m = self.find(mentor) if mentor else None
            if m is not None:
                self._push(m, '0x7C', {'mentee_uid': session.get('msgr_uid') or _uid(session)}, 'MENTOR')
            pupils = [s for s in (self.find(n) for n in mentees) if s is not None]
            for s in pupils:
                self._push(s, '0x7D', {'mentor_uid': session.get('msgr_uid') or _uid(session),
                                       'channel_or_state': social.OFFLINE_CHANNEL}, 'MENTOR')
        log.info(f'[FRIEND] {self.name_of(session)!r} offline ({reason}): 0x60 to {len(told)} friend(s)'
                 + (f', 0x7C to mentor {mentor!r}' if m is not None else '')
                 + (f', 0x7D to {len(pupils)} mentee(s)' if pupils else ''))

    def _forget(self, session):
        """Drop every pending request / invite this session received (its prompt is gone with
        its client). One it MADE stays until the target answers - the answer then gets the
        "is not on line." refusal (0x0C 2 / 0x0F 8, social_friend F2 step 6 / F5 step 4) -
        or the target leaves; the TTL covers the rest."""
        for table in (self.requests, self.invites):
            for target_uid, pend in list(table.items()):
                for asker_uid, p in list(pend.items()):
                    if p.target is session:
                        del pend[asker_uid]
                if not pend:
                    del table[target_uid]

    @staticmethod
    def _prune(table, ttl, now):
        """Drop the entries of `table` older than `ttl` (called on every new request, so a
        prompt nobody answers never lingers)."""
        for target_uid, pend in list(table.items()):
            for asker_uid, p in list(pend.items()):
                if now - p.t >= ttl:
                    del pend[asker_uid]
            if not pend:
                del table[target_uid]

    @staticmethod
    def _pending(table, target, asker):
        """The live Pending of `asker` for `target` (same session identities, not expired)."""
        p = table.get(_uid(target), {}).get(_uid(asker))
        return p if p is not None and p.session is asker and p.target is target else None

    def before_map_load(self, session, reason=''):
        """Before every server-sent S2C 0x03 (F4 step 4, F5 step 9, chat_mail_gm F6.4): the
        0x03 zeroes the client's room id and status without a C2S 0x36 / 0x37, so the room is
        left here (0x62 to the rest), a Busy / AFK status goes back to Online (0x60 to the
        watchers - else the player could never return to Online: the client only sends 0x37
        for a CHANGE from its reset 0), and the memo view is invalid until the next 0x2F."""
        with self.lock:
            session['msgr_synced'] = False
            session['memo_ids_on_client'] = set()
            if not self.is_online(session):
                return
            self.leave_room(session, reason or 'map load')
            if session.get('msgr_status'):
                session['msgr_status'] = social.STATUS_ONLINE
                told = self._presence(session, True)
                log.info(f'[FRIEND] {self.name_of(session)!r} map load resets the status to Online '
                         f'(0x60 to {len(told)} friend(s))')

    # ================================================================ F5 ===
    def room_of(self, session):
        room = self.rooms.get(session.get('room_id'))
        return room if room is not None and any(m is session for m in room) else None

    def room_invite(self, sock, session, rec):
        """C2S 0x33 {target_uid, target_name} (window 0x176 "Converse", room window invite
        0x178 - which can send an empty name - or the popup) -> S2C 0x10 to the invitee
        ("<A> would like to have a chat with you. Will you accept?"), or a 0x0F refusal to
        the inviter: 5 own room full, 8 not online, 4 refuses talk (privacy), 6 already in
        a room. A pending invite is not repeated."""
        if not self.is_online(session):
            return
        me = self.name_of(session)
        wanted = social.name_text(rec.get('target_name', b''))
        uid = int(rec.get('target_uid', 0) or 0)
        with self.lock:
            room = self.room_of(session)
            if room is not None and len(room) >= chatmod.ROOM_MAX:
                return self._room_refused(session, chatmod.ROOM_FULL, wanted, 'room full')
            target = self.find(uid, viewer=session) if uid else None
            if target is not None and wanted and social.key(self.name_of(target)) != social.key(wanted):
                target = None
            if target is None:
                target = self.find(wanted, viewer=session) if wanted else None
            if target is None:
                return self._room_refused(session, chatmod.ROOM_NOT_ONLINE, wanted, 'not online')
            if target is session or _uid(target) == _uid(session):
                log.info(f'[ROOM] {me!r} invited itself - dropped')
                return
            other = self.name_of(target)
            if self.server.blacklist_drops(target, session, by_uid=True):
                # P12 bl-3 'silent': the invitee's client would drop the 0x10 (FUN_00484190 at
                # 0x47C5CD, by inviter_id OR name: a listed character's sibling too - by_uid) -
                # no window, no pending invite, nothing back.
                log.info(f'[ROOM] {me!r} -> {other!r}: blacklisted - dropped (no invite, no pending state)')
                return
            if self.server.refuses(target, 'talk', session, by_uid=True):
                return self._room_refused(session, chatmod.ROOM_REJECTING, other,
                                          'refuses chatting (privacy flag or blacklist refuse mode)')
            if self.room_of(target) is not None:
                return self._room_refused(session, chatmod.ROOM_BUSY, other, 'already in a room')
            now = time.monotonic()
            self._prune(self.invites, social.ROOM_INVITE_TTL, now)
            old = self._pending(self.invites, target, session)
            if old is not None and now - old.t < social.ROOM_INVITE_TTL:
                log.info(f'[ROOM] {me!r} -> {other!r}: invite already pending - not repeated')
                return
            self.invites.setdefault(_uid(target), {})[_uid(session)] = Pending(me, session, target, now)
            self._push(target, '0x10', {'inviter_id': _uid(session), 'inviter_name': chatmod.name_bytes(me)}, 'ROOM')
        log.info(f'[ROOM] {me!r} invites {other!r} (0x10)')

    def _room_refused(self, session, subtype, name, why):
        self.server._room_refused(session, subtype, name, flush=False)
        log.info(f'[ROOM] {self.name_of(session)!r} invite {social.name_text(name)!r}: 0x0F {subtype} ({why})')

    def room_reply(self, sock, session, rec):
        """C2S 0x34 {inviter_id, accept} from window 0x17D. Refuse: 0x0F 3 to the inviter.
        Accept: a new room [inviter, invitee] (each gets 0x0F 1 with itself first, so each
        prints "<other> has entered.") or the inviter's room (the joiner gets the full list,
        every member 0x0F 1 {joiner}: "<joiner> has entered."). Inviter gone: 0x0F 8 to the
        invitee; inviter's room full: 0x0F 5."""
        if not self.is_online(session):
            return
        rid = int(rec.get('inviter_id', 0) or 0)
        accept = bool(rec.get('accept'))
        me = self.name_of(session)
        with self.lock:
            inv = self.invites.get(_uid(session), {}).pop(rid, None)
            if inv is None or inv.target is not session or time.monotonic() - inv.t >= social.ROOM_INVITE_TTL:
                log.info(f'[ROOM] {me!r} answered an invite of uid {rid} that is not pending - dropped')
                return
            host = inv.session if self.is_online(inv.session) else None
            if not accept:
                if host is not None:
                    self.server._room_refused(host, chatmod.ROOM_REFUSED, me, flush=False)
                log.info(f'[ROOM] {me!r} refused the chat with {inv.name!r} (0x0F 3)')
                return
            if host is None:
                return self._room_refused(session, chatmod.ROOM_NOT_ONLINE, inv.name, 'the inviter left')
            room = self.room_of(host)
            if room is not None and sum(1 for m in room if m is not session) >= chatmod.ROOM_MAX:
                return self._room_refused(session, chatmod.ROOM_FULL, inv.name, 'the room is full')
            # A member of another room (its client only opens 0x17D with no room, so this is
            # a server-side leftover) leaves it first.
            self.leave_room(session, 'joined another room')
            room = self.room_of(host)
            if room is None:
                room_id = self._new_room_id()
                room = self.rooms[room_id] = [host, session]
                host['room_id'] = session['room_id'] = room_id
                names = [self.name_of(m) for m in room]
                self.server._room_joined(host, room_id, names, flush=False)
                self.server._room_joined(session, room_id, names, flush=False)
            else:
                room_id = host['room_id']
                room.append(session)
                session['room_id'] = room_id
                self.server._room_joined(session, room_id, [self.name_of(m) for m in room], flush=False)
                for m in room[:-1]:
                    self.server._room_joined(m, room_id, [me], flush=False)
        log.info(f'[ROOM] {me!r} joins room {room_id} of {inv.name!r}: {[self.name_of(m) for m in room]}')

    def _new_room_id(self):
        room_id = self._next_room_id
        while room_id in self.rooms or room_id == 0:
            room_id = room_id % 0xFFFFFFFF + 1
        self._next_room_id = room_id % 0xFFFFFFFF + 1
        return room_id

    def room_chat(self, sock, session, rec):
        """C2S 0x35 {text_len, text} -> S2C 0x61 {sender, text <= 58} to EVERY member,
        sender included (the room window never prints its own line). Outside a room: dropped."""
        text = P.to_bytes(rec.get('text', b'')).split(b'\x00', 1)[0]
        with self.lock:
            room = self.room_of(session) if self.is_online(session) else None
            if room is None or not text:
                log.info(f'[ROOM] line from {self.name_of(session)!r} dropped: '
                         f'{"not in a room" if room is None else "empty"}')
                return
            heard = [self.name_of(m) for m in list(room)
                     if self.server._room_line(m, self.name_of(session), text, flush=False)]
        log.info(f'[ROOM] {self.name_of(session)}: {P.cut_text(text, chatmod.ROOM_TEXT_MAX)!r} -> {heard}')

    def room_leave(self, session):
        """C2S 0x36 (window 0x177 hidden; sent even with no room)."""
        with self.lock:
            left = self.leave_room(session, 'closed the room window')
        if not left:
            log.info(f'[ROOM] {self.name_of(session)!r} closed the room window (in no room)')

    def leave_room(self, session, why):
        """Take the session out of its room: S2C 0x62 {name} ("<A> has logged out..") to each
        remaining member; a room with nobody left is deleted, one with 1 member kept (that
        member can invite again). True when it was in a room."""
        with self.lock:
            room_id = session.pop('room_id', None)
            room = self.rooms.get(room_id)
            if room is None:
                return False
            room[:] = [m for m in room if m is not session]
            name = chatmod.name_bytes(self.name_of(session))
            for m in room:
                self._push(m, '0x62', {'member_name': name}, 'ROOM')
            if not room:
                del self.rooms[room_id]
        log.info(f'[ROOM] {self.name_of(session)!r} left room {room_id} ({why}); '
                 f'{len(room)} member(s) left')
        return True

    # =========================================================== F6 - F8 ===
    def mentor_fields_03(self, session, char):
        """The S2C 0x03 mentor block of a mentee (F8.1): mentor_id = the mentor's account
        uid, name, channel = this channel when online (the client compares it with its own
        channel_id) else 0x66. No mentor (or a deleted one): mentor_id 0."""
        name = (char or {}).get('mentor')
        found = self.store.character_by_name(name) if name else None
        if found is None:
            return {'mentor_id': 0}
        _user, acc, mchar = found
        live = self.find(mchar['name'], viewer=session)
        return {'mentor_id': int(acc.get('uid') or 0),
                'mentor_name': chatmod.name_bytes(mchar['name']),
                'mentor_channel': self.channel(session) if live is not None else social.OFFLINE_CHANNEL}

    def mentor_register(self, sock, session, rec):
        """C2S 0x5C {mentor_name} from dialog 0x1FE (a Novice only): 0x7A 0x66 when the
        mentor is not online, 0x65 when he is a Novice; else the link is persisted, the
        mentee gets 0x7A {0x64, name, uid} ONCE (the client appends with no duplicate check)
        and the mentor 0x7B ("<R> has logged in. (Menti)")."""
        if not self.is_online(session):
            return
        me = self.name_of(session)
        wanted = social.name_text(rec.get('mentor_name', b''))
        with self.lock:
            with self.store.lock:
                char = self.char_of(session)
                if char is None:
                    return
                cls, has = int(char.get('class', 0) or 0), char.get('mentor')
            if cls != 0 or has:
                log.info(f'[MENTOR] {me!r} (class {cls}, mentor {has!r}) register {wanted!r} - dropped '
                         f'(the client only offers it to a Novice without a mentor)')
                return
            mentor = self.find(wanted, viewer=session) if wanted else None
            if mentor is None:
                self._send(sock, session, '0x7A', {'result': social.MENTOR_NOT_FOUND,
                                                   'mentor_name': chatmod.name_bytes(wanted)})
                log.info(f'[MENTOR] {me!r} register {wanted!r}: not online (0x7A 0x66)')
                return
            if mentor is session or _uid(mentor) == _uid(session):
                return
            other = self.name_of(mentor)
            gate = self.server.blacklist.gate(mentor, session)
            if gate == blmod.MODE_SILENT:
                # P12 bl-3 (blch F-B4 / F-B5): one side blacklisted the other - no link, no 0x7B,
                # nothing back (0x5C opens no waiting box).
                log.info(f'[MENTOR] {me!r} register {other!r}: blacklisted - dropped (silent)')
                return
            if gate == blmod.MODE_REFUSE:
                self._send(sock, session, '0x7A', {'result': social.MENTOR_NOT_FOUND,
                                                   'mentor_name': chatmod.name_bytes(other)})
                log.info(f'[MENTOR] {me!r} register {other!r}: blacklisted the mentee (0x7A 0x66, refuse mode)')
                return
            with self.store.lock:
                mchar = self.char_of(mentor)
                if mchar is None:
                    return
                novice = int(mchar.get('class', 0) or 0) == 0
                if not novice:
                    char['mentor'] = other
                    if not any(social.key(n) == social.key(me) for n in mchar.get('mentees') or []):
                        mchar.setdefault('mentees', []).append(me)
            if novice:
                self._send(sock, session, '0x7A', {'result': social.MENTOR_NOVICE,
                                                   'mentor_name': chatmod.name_bytes(other)})
                log.info(f'[MENTOR] {me!r} register {other!r}: a Novice (0x7A 0x65)')
                return
            self.store.mark_dirty(f'mentor {me} -> {other}')
            self._send(sock, session, '0x7A', {'result': social.MENTOR_OK, 'mentor_name': chatmod.name_bytes(other),
                                               'mentor_uid': _uid(mentor)})
            self._push(mentor, '0x7B', {'mentee_uid': _uid(session), 'channel': social.SAME_CHANNEL,
                                        'mentee_name': chatmod.name_bytes(me)}, 'MENTOR')
        log.info(f'[MENTOR] {me!r} registered {other!r} as mentor (0x7A 0x64, 0x7B to {other!r})')

    def mentor_remove(self, session):
        """C2S 0x50 (window 0x200 OK; the client already cleared its mentor): the link is
        removed on both records and the online mentor gets 0x7C {mentee uid}. No reply."""
        if not self.is_online(session):
            return
        me = self.name_of(session)
        with self.lock:
            with self.store.lock:
                char = self.char_of(session)
                mentor = (char or {}).get('mentor')
                if not mentor:
                    log.info(f'[MENTOR] {me!r} removes a mentor it does not have - dropped')
                    return
                char['mentor'] = None
                found = self.store.character_by_name(mentor)
                if found is not None:
                    found[2]['mentees'] = [n for n in found[2].get('mentees') or []
                                           if social.key(n) != social.key(me)]
            self.store.mark_dirty(f'mentor removed {me} -> {mentor}')
            live = self.find(mentor)
            if live is not None:
                self._push(live, '0x7C', {'mentee_uid': _uid(session)}, 'MENTOR')
        log.info(f'[MENTOR] {me!r} removed mentor {mentor!r}' + (' (0x7C)' if live is not None else ''))

    def mentee_exp(self, mentee, gained):
        """A kill gave this session `gained` exp (GameServer._kill_monster): credit its online
        mentor MENTOR_EXP_SHARE_PCT of it with S2C 0x7F {delta, menti_id} (social_friend-
        mentor-exp-share / lc-menti-exp; the client prints "You've received (+%d) experience
        points.(Menti[%s])" when menti_id is in its mentee list). The kill holds the killer's
        combat and monster locks, and the mentor's grant takes the MENTOR's combat lock, so
        it runs from the tick scheduler (world_lock first) instead of nesting them."""
        pct = int(self.server.config.get('MENTOR_EXP_SHARE_PCT', 10) or 0)
        if gained <= 0 or pct <= 0 or not self.is_online(mentee):
            return False
        with self.store.lock:
            char = self.char_of(mentee)
            mentor = (char or {}).get('mentor')
        if not mentor:
            return False
        self.server.ticks.call_later(0, self._share_exp, mentee, int(gained), pct, name='mentor-exp')
        return True

    def _share_exp(self, mentee, gained, pct):
        with self.store.lock:
            char = self.char_of(mentee) if mentee.get('char_name') else None
            mentor = (char or {}).get('mentor')
        m = self.find(mentor) if mentor else None
        if m is None or not m.get('in_world'):
            log.info(f'[MENTOR] {self.name_of(mentee)!r} +{gained} exp: mentor {mentor!r} not in world, no share')
            return 0
        bonus = max(1, gained * pct // 100)
        # arch09-exp-pipeline source 'mentor': `gained` already went through the event stage,
        # so the share is not scaled again (events.EXP_UNSCALED_SOURCES); 0x7F, never a tail.
        applied = self.server.award_exp(m, bonus, 'mentor', menti_id=mentee.get('msgr_uid') or _uid(mentee))
        log.info(f'[MENTOR] {self.name_of(m)!r} gets {applied:+d} exp from mentee {self.name_of(mentee)!r} '
                 f'({pct}% of {gained}, 0x7F)')
        return applied

    # ================================================================ F9 ===
    def deliver(self, char, memo):
        """Push one stored memo to its recipient when online and synced (S2C 0x78 count 1),
        and remember its id for the 0x44 guard. While the client has not re-synced since its
        last 0x03 (msgr_synced False) the coming 0x2F delivers it instead - else it would
        show twice."""
        live = self.find(char['name'])
        if live is None or not live.get('msgr_synced'):
            return False
        for fields in social.memo_packets([memo], self.server.client_build):
            if not self._push(live, '0x78', fields, 'MEMO'):
                return False
        live.setdefault('memo_ids_on_client', set()).add(memo['id'])
        return True

    def store_memo(self, recipient, sender, text):
        """Store a memo on the character named `recipient` (online or not) and deliver it
        live. Returns (canonical recipient name, memo, delivered) or None (unknown name or
        empty text)."""
        with self.lock:
            found = self.store.character_by_name(recipient)
            if found is None:
                return None
            char = found[2]
            with self.store.lock:
                memo = social.add_memo(char, sender, text)
            if memo is None:
                return None
            self.store.mark_dirty(f'memo {char["name"]}')
            delivered = self.deliver(char, memo)
        return char['name'], memo, delivered

    def note(self, sock, session, rec):
        """C2S 0x4B. Note item 1894 / 3320 (window 0x3FD, which waits behind "Waiting for the
        server to response." until S2C 0x77): the memo is stored for the recipient, online or
        offline, delivered live when online, and the sender gets 0x77 {1, serial of the used
        note} - or 0x77 {0} for an unknown / own recipient, an empty text or a note the
        player does not own. Item 9999 is the gift popup's thank-you (chat_mail_gm F7): the
        same memo, no item, no reply (that path shows no wait box)."""
        item = social.note_item(rec)
        wanted = social.name_text(rec.get('recipient_name', b''))
        text = social.note_text(rec)
        me = self.name_of(session)
        if item == social.GIFT_REPLY_NOTE_ID:
            return self._gift_reply(session, wanted, text)
        serial = self._note_serial(session, item) if item in social.NOTE_ITEMS else None
        why = None
        if serial is None:
            why = f'item {item} is not a Note' if item not in social.NOTE_ITEMS else f'no Note {item} owned'
        elif not self.is_online(session):
            why = 'not in world'
        elif not text:
            why = 'empty text'
        elif social.key(wanted) == social.key(me) or not wanted:
            why = 'own name' if wanted else 'no recipient'
        stored = None
        if why is None:
            stored = self.store_memo(wanted, me, text)
            if stored is None:
                why = 'unknown recipient'
        if why is not None:
            self._send(sock, session, '0x77', {'result': social.NOTE_RESULT_FAILED})
            log.info(f'[NOTE] {me!r} note {item} to {wanted!r}: 0x77 {{0}} ({why})')
            return
        self._use_note(session, item, serial)
        self._send(sock, session, '0x77', {'result': social.NOTE_RESULT_SENT, 'cash_item_serial': serial})
        name, memo, delivered = stored
        log.info(f'[NOTE] {me!r} -> {name!r}: memo {memo["id"]} ({len(text)}B) stored'
                 f'{", delivered (0x78)" if delivered else " for the next 0x2F"}; 0x77 {{1, serial {serial}}}')

    def _gift_reply(self, session, wanted, text):
        me = self.name_of(session)
        why = None
        if not self.is_online(session):
            why = 'not in world'
        elif not wanted or social.key(wanted) == social.key(me):
            why = 'own or empty name'
        elif not text:
            why = 'empty text'
        stored = self.store_memo(wanted, me, text) if why is None else None
        if why is None and stored is None:
            why = 'unknown recipient'
        if why is not None:
            log.info(f'[NOTE] {me!r} gift thank-you to {wanted!r} dropped ({why}); no reply (no wait box)')
            return
        name, memo, delivered = stored
        log.info(f'[NOTE] {me!r} gift thank-you -> {name!r}: memo {memo["id"]} stored'
                 f'{", delivered (0x78)" if delivered else ""}; no reply (chat_mail_gm F7)')

    def _note_serial(self, session, item):
        """The cash serial of the Note the player uses, or None when he owns none
        (premium_cash-cash-inventory-api, P8 stage 1): the record the client's own lookup picks
        (cash.CashInventory.find = FUN_0045E760: that id, quantity > 0). The P6 dev flag
        DEV_FREE_NOTES, which let a player without one send with serial 0, is retired (P8
        stage 4, roadmap P8 "Turn off DEV_FREE_NOTES"; config.RETIRED_KEYS): no Note, no
        memo - the caller answers 0x77 {0}."""
        rec = self.server.cash.find(self.char_of(session), item)
        return None if rec is None else rec['serial']

    def _use_note(self, session, item, serial):
        """The sent note is used up on both sides: the 0x77 success names its serial and the
        client's consume-by-serial (2008 FUN_00464380, 2009 FUN_0046df70) takes one off a
        limit_type 1 record and frees it at 0 - the model does the same (cash.consume)."""
        if not serial:
            return
        left = self.server.cash.consume(self.char_of(session), serial, 1, what=f'note {item}')
        state = 'not in the model' if left is None else f'{left} left' if left else 'record gone'
        log.info(f'[NOTE] {self.name_of(session)!r} used Note {item} (serial {serial:#x}): {state}')

    def memo_delete(self, session):
        """C2S 0x44 (the memo window closed with "All the messages will be deleted"): the
        memos this client was shown since its last 0x03 go; one stored after that survives
        (chat_mail_gm F6.5). Never answered (registry.NEVER_REPLY 0x44)."""
        ids = set(session.get('memo_ids_on_client') or ())
        session['memo_ids_on_client'] = set()
        if not ids:
            log.info(f'[MEMO] {self.name_of(session)!r} closed the memo window: nothing shown, nothing deleted')
            return
        with self.lock, self.store.lock:
            char = self.char_of(session)
            gone = social.delete_memos(char, ids) if char is not None else 0
            left = len((char or {}).get('memos') or [])
        if gone:
            self.store.mark_dirty(f'memos deleted {self.name_of(session)}')
        log.info(f'[MEMO] {self.name_of(session)!r} deleted {gone} memo(s), {left} kept')

    # =============================================================== F12 ===
    def slot_expand(self, sock, session):
        """C2S 0x73 (Frenaiga, window 0x47B OK): +5 friend slots for FRIEND_SLOT_PRICE gold
        up to 50 -> S2C 0x9D {1, capacity, absolute gold} (the client sets the label and
        the gold HUD from it), else 0x9D {0} ("You failed to added him/her on your friend
        list.")."""
        if not self.is_online(session):
            return
        price = int(self.server.config.get('FRIEND_SLOT_PRICE', social.FRIEND_SLOT_PRICE))
        wallet = self.server._wallet_of(session)
        with self.store.lock:
            char = self.char_of(session)
            cap = social.capacity(char)
            ok = char is not None and wallet is not None and cap < social.FRIEND_CAP_MAX and wallet.pay(gold=price)
            if ok:
                cap = char['friend_capacity'] = min(social.FRIEND_CAP_MAX, cap + social.FRIEND_SLOT_STEP)
        if not ok:
            self._send(sock, session, '0x9D', {'result': 0})
            log.info(f'[FRIEND] {self.name_of(session)!r} slot expansion refused (capacity {cap}, gold '
                     f'{wallet.gold if wallet is not None else "?"} < {price}?)')
            return
        gold, _victy = self.server._wallet_commit(session, wallet, 'friend slots')
        self._send(sock, session, '0x9D', {'result': 1, 'friend_capacity': cap, 'gold': gold})
        log.info(f'[FRIEND] {self.name_of(session)!r} friend list -> {cap} slots for {price} gold (now {gold})')

    # ------------------------------------------------------------- dev view ---
    def describe(self, session):
        """Lines for the `!msgr` dev command: the server's view of this character."""
        with self.lock, self.store.lock:
            char = self.char_of(session) or {}
            rows = []
            for name in char.get('friends') or []:
                live = self.find(name, viewer=session)
                rows.append(f'{name}({"on" if live is not None else "off"}'
                            f'{"," + str(live.get("msgr_status")) if live is not None and live.get("msgr_status") else ""})')
            room = self.room_of(session)
            lines = [f'friends {len(rows)}/{social.capacity(char)}: {", ".join(rows) or "-"}',
                     f'mentor: {char.get("mentor") or "-"}; mentees: {", ".join(char.get("mentees") or []) or "-"}',
                     f'memos: {len(char.get("memos") or [])} stored, {len(session.get("memo_ids_on_client") or ())} '
                     f'shown; status {session.get("msgr_status", 0)}; room '
                     + (f'{session.get("room_id")} {[self.name_of(m) for m in room]}' if room else '-')]
        return lines


# ------------------------------------------------------------------ hooks ---
def register(hooks, messenger):
    """The lifecycle hooks (world.py): a map load resets what 0x03 resets, the first entry
    announces the character (a channel hop only its new channel), leaving the world / closing
    leaves its room, the end of the login (ON_LOGOUT, continuity.py) announces it gone, a
    rename renames its rows on the friends' and the mentor's clients (ON_RENAME, the C4
    hook)."""
    def before_map_load(server, session, reason=None, **_):
        messenger.before_map_load(session, reason or '')

    def on_enter_world(server, session, hop=None, **_):
        if not session.get('msgr_online'):
            messenger.go_online(session, hop=hop)

    def on_leave_world(server, session, reason=None, superseded=False, **_):
        messenger.go_offline(session, reason or '', superseded=superseded)

    def on_disconnect(server, session, reason=None, **_):
        messenger.go_offline(session, reason or '', superseded=server.world.superseded(session))

    def on_rename(server, session, old=None, new=None, **_):
        messenger.renamed(session, old, new)

    def on_logout(server, session, reason=None, **_):
        messenger.logged_out(session, reason or '')

    hooks.register(worldmod.BEFORE_SERVER_MAP_LOAD, before_map_load)
    hooks.register(worldmod.ON_ENTER_WORLD, on_enter_world)
    hooks.register(worldmod.ON_LEAVE_WORLD, on_leave_world)
    hooks.register(worldmod.ON_DISCONNECT, on_disconnect)
    hooks.register(worldmod.ON_RENAME, on_rename)
    hooks.register(worldmod.ON_LOGOUT, on_logout)
    return before_map_load, on_enter_world, on_leave_world, on_disconnect, on_rename, on_logout
