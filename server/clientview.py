#!/usr/bin/env python3
"""
clientview.py - arch09-receiver-mirror (ROADMAP_2009_ADDENDUM 3.0, owner P14): what ONE
receiving client holds, for the builders whose wire shape depends on it
=======================================================================================
Some 2009 packets change length with state only the receiving client knows, and a server
that guesses wrong desyncs the stream or crashes the client (ADDENDUM R3):

  client_guild_id  the receiver's OWN local entity +0x12 (guild.md 1.2): S2C 0x21 carries the
                   u32 guild_points tail exactly when it is > 1 (0x4591F6..0x459236 tests
                   `word [[scene+0x988]+0x12] > 1`, guild.md 3). 1 = the "Game Master" tag (no
                   tail), 0xFFFF = sub 15 result 1 (tail!). Its writers, in wire order: the own
                   0x07 record's gm_or_guild_id, 0xB3 sub 1 (result 1) / 3 / 6 / 8 / 15 / 37 (own
                   uid) and 0xB4 / 0xB6 / 0xB7 on the own uid (guild.Guilds.send_sub / send_tag).
  pet_info_seen    the uids whose pet_info this client holds (pet.md 4, 7 H1-H3): a 0xAC / 0xAD(0)
                   to a viewer without it is a NULL write (0x457B3C / 0x457C37). P15 pet-s1 keeps
                   it with the bytes (presence.py, pets.py, under the receiver's presence lock):
                   a remote record (0x04 / 0x05) sets it to its has_pet (every appear allocates a
                   fresh entity, so has_pet 0 = no pet_info), a remote 0xAB with has_pet_info 1 or
                   an 0xAD(1) adds it, a 0xAC drops it (pet.md F2 step 4; the client keeps the
                   block, so this errs on the safe side), a 0x06 drops it (FUN_004241c0 frees it)
                   and the map load clears it. 0xAD(0) keeps it: the block survives asleep.
  had_03           the connection's first S2C 0x03 went out: before it CMessenger+0x130 is NULL
                   and SubHandler3 drops 0xB3 / 0xBD.. unhandled (guild.md 1.1, blch A.1).
  own_07           the own 0x07 of the CURRENT map load went out: subs 1 / 3 / 6 / 8 / 15 write
                   [scene+0x988]+0x12 with no NULL check (0x47DB55, 0x47DA58) - never before it.
  guild_rows       a 0xB3 sub 3 with members went out since the last 0x03: sub 3 APPENDS to the
                   member list (no clear), so a second one duplicates every member (guild.md 2).
  guild_list       the guild id whose window (CMessenger M: name, members, max, points) this client
                   holds, 0 for none: set by sub 3 (any n - the create's n = 0 leaves the pre-filled
                   own record), cleared by the 0x03, sub 1 failures / sub 6 (left) / sub 8 {1} /
                   sub 15 (all reset M, FUN_0047a8c0). The member DELTAS (sub 10 / 12 / 13 / 14 /
                   17 / 20 / 21 / 22) go only to a client that holds that list: one in a map load
                   gets the whole state from its coming sub 3 instead, and a delta on top of that
                   sub 3 would list a new member twice (P14 guild-g3).
  apps_held        the client's application list M+0xE0 mirrors the guild's FIFO: set when the
                   master sync (sub 4, possibly empty) went out after this 0x03, or the guild was
                   just created (both empty); cleared by the 0x03 (FUN_0047aaa0). A new application
                   (sub 2 result 0x10) is pushed only then - otherwise the coming sub 4 carries it,
                   and pushing it too would put it twice in the list the client pops FIFO
                   (0x8B / 0x8C pop its FIRST record, guild.md F3).
  apps_stale       the client still holds an application list that no longer mirrors the FIFO:
                   it was the master's when a master change (0x91) made another member master -
                   sub 12 does not clear M+0xE0. Until the next 0x03 (which frees that list)
                   nothing is pushed to it, a re-promotion sends it no sub 4 (it would append to
                   the stale rows) and its 0x8C pops nothing server-side (P14 review).

Map load (GameServer._map_transfer): the 0x03 frees every entity and resets CMessenger, so
`on_map_load` clears the per-map state (pet_info_seen, own_07, guild_rows, client_guild_id 0)
and sets had_03; `on_own_record` records the own 0x07's gm_or_guild_id and sets own_07. A new
connection (a relogin, a 2009 channel hop) is a new session dict, so a new view.

Order: a mirror is only right if it changes in the order the bytes reach the client. Every
writer that sends a mirrored packet holds `lock(session)` across [decide, enqueue, update]:
the map load around its 0x03 + 0x07, the guild sends, and the 0x21 builder around its
build + send (GameServer.grant_exp: a kill on another thread can never build its tail from a
value the client no longer holds). Lock order: ... -> clientview lock -> send_lock (the
_send_encrypted queue); nothing is taken under it but the receiver's send_lock, and no
callback runs under it. pet_info_seen is the exception that proves the rule: its writers are
the receiver's RECORD and pet-packet senders, which decide and queue under the receiver's
presence lock (presence.py) - the lock the map load's presence.clear also takes before that
0x03 - so the order there is presence lock -> clientview lock (only to touch the set) ->
send_lock, and nothing holding a clientview lock ever takes a presence lock.
"""
import threading

VIEW_KEY = 'client_view'
_CREATE = threading.Lock()


class ClientView:
    __slots__ = ('lock', 'guild_id', 'pet_info_seen', 'had_03', 'own_07', 'guild_rows', 'guild_list',
                 'apps_held', 'apps_stale', 'loads')

    def __init__(self):
        self.lock = threading.RLock()
        self.guild_id = 0
        self.pet_info_seen = set()
        self.had_03 = False
        self.own_07 = False
        self.guild_rows = False
        self.guild_list = 0
        self.apps_held = False
        self.apps_stale = False
        self.loads = 0

    def snapshot(self):
        with self.lock:
            return {'guild_id': self.guild_id, 'pet_info_seen': sorted(self.pet_info_seen),
                    'had_03': self.had_03, 'own_07': self.own_07, 'guild_rows': self.guild_rows,
                    'guild_list': self.guild_list, 'apps_held': self.apps_held,
                    'apps_stale': self.apps_stale, 'loads': self.loads}


def view(session):
    """The session's ClientView (created on first use)."""
    v = session.get(VIEW_KEY)
    if v is None:
        with _CREATE:
            v = session.get(VIEW_KEY)
            if v is None:
                v = session[VIEW_KEY] = ClientView()
    return v


def lock(session):
    """The view's lock (an RLock): hold it across a mirrored packet's build + send."""
    return view(session).lock


def guild_id(session):
    """The receiver's own entity+0x12 as its client holds it (0 before any 0x07)."""
    return int(view(session).guild_id)


def on_map_load(session):
    """An S2C 0x03 is going out (caller holds lock(session)): every entity, the guild window,
    its applications and boards are gone on the client."""
    v = view(session)
    with v.lock:
        v.had_03 = True
        v.own_07 = False
        v.guild_rows = False
        v.guild_list = 0
        v.apps_held = False
        v.apps_stale = False
        v.guild_id = 0
        v.pet_info_seen.clear()
        v.loads += 1


def on_own_record(session, gm_or_guild_id):
    """The own 0x07 is going out with this gm_or_guild_id (2008: gm_level, same entity word)."""
    v = view(session)
    with v.lock:
        v.own_07 = True
        v.guild_id = int(gm_or_guild_id) & 0xFFFF


def set_guild_id(session, value):
    v = view(session)
    with v.lock:
        v.guild_id = int(value) & 0xFFFF


def note_pet_info(session, uid):
    """P15 pet-s1: this viewer got the pet_info of `uid`."""
    v = view(session)
    with v.lock:
        v.pet_info_seen.add(int(uid))


def set_pet_info(session, uid, held):
    """P15 pet-s1: a remote record of `uid` went out with has_pet `held` (a fresh entity: its
    pet_info exists exactly when the record carried the block)."""
    v = view(session)
    with v.lock:
        if held:
            v.pet_info_seen.add(int(uid))
        else:
            v.pet_info_seen.discard(int(uid))


def forget_pet_info(session, uid):
    """P15 pet-s1: `uid`'s pet_info is gone on this client (its 0x06 freed it) - or may be (a
    0xAC: F2 step 4 drops it, so no later 0xAC / 0xAD(0) goes out on a guess). A session with no
    view yet (every 2008 one: presence calls this on each 0x06) holds nothing to forget."""
    v = session.get(VIEW_KEY)
    if v is None:
        return
    with v.lock:
        v.pet_info_seen.discard(int(uid))


def has_pet_info(session, uid):
    v = view(session)
    with v.lock:
        return int(uid) in v.pet_info_seen
