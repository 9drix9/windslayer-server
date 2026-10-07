#!/usr/bin/env python3
"""
world.py - the world registry (roadmap 1.6 F5: world-registry; aliases party-mp-registry,
trade-mp-registry, chat_mail_gm-online-registry, social_friend-session-identity)
====================================================================================
One index of the sessions that are logged in and in the world, by account uid, by
character name and by map, the lifecycle hooks the groups register on, and the per-session
outbound queue that makes a send to ANOTHER session safe from any thread.

    world = World()                             # its own leaf lock (see "Locks" below)
    old = world.claim(session, uid, username)   # login (lc-uid-online): None = this session owns uid
    world.enter(session, 'TestHero')            # C2S 0x2B: by_name
    world.arrive(session, 102)                  # after the own 0x07: MapInstance 102
    world.peers(session)                        # in-world sessions on the same map, not itself
    world.find('testhero') / world.find(2)      # the online views: in-world session or None
                                                # (a str is always a name, an int a uid)
    world.depart(session)                       # a map load starts / the session leaves the world
    world.drop(session)                         # connection finally / kick: by_uid, by_name
    world.map(102).monsters                     # the map's ONE monster set (MapMonsters)
    world.alloc_monster_uids(8)                 # server-wide monster uids (ids.MONSTER)

Monsters (world-shared-monsters, P5 stage 3)
--------------------------------------------
Every MapInstance owns a MapMonsters: uid -> Monster, shared by every player on the map, with
its own lock and the book of which client holds which monster uid. GameServer attaches a
session to it after the session's map load (session['monsters'] is that dict) and detaches
it on the next map load / disconnect; the monster lifecycle, combat and AI live in
GameServer (the PHASE 1 COMBAT and MONSTER AGGRO sections). The registry only stores it.

Online views (what chat, party, trade and friends resolve targets with)
------------------------------------------------------------------------
- `find(name_or_uid)`: the IN-WORLD session of a character name (a str, case-insensitive,
  the whisper "online_by_lname" fallback) or an account uid (an int) - whisper 0x02, party
  invite 0x4E, trade 0x20, friend add 0x30 and GM /go all need a target that has its own
  0x07 out. A decimal string is a NAME: names.ALLOWED_RE allows a character called '12'.
  `find(..., in_world=False)` also returns a session at character select or mid map load.
- `map_sessions(map)`, `peers(session)`: the audience of a map broadcast (0x05/0x06
  presence, 0x1B movement, 0x16 map chat, 0x3B/0x40/0x58 observer packets, ground items).
  Never the source itself and never a session with the source's uid (a record carrying the
  receiver's own uid re-registers its local player: social_friend 3.4).
- `in_world_sessions()`: every reachable in-world session (/not, 0x99 notices).
- `online_names(names)`: {lower(name): session} of those characters in world (friend list
  presence, 0x0B / 0x60).
"Reachable" = in_world and on a map, a socket, not kicked, connection not closed.

Lifecycle hooks (F5; groups register callbacks, nothing is inlined in handlers)
-------------------------------------------------------------------------------
Every hook is called as fn(server, session, **kw); callbacks take **_ so new keywords can
be added. The server fires them (MapTransfer and the connection's finally), in this order:

  before_server_map_load  map_code, reason       before a server map load's lead packet:
                                                 packets the mover must get while the old
                                                 map is loaded (stall close 0x84, trade 0x49)
  on_map_change           old_map, map_code,     an in-world session has left old_map for a
                          reason                 server map load (it is on no map, in_world
                                                 False; old_map == map_code is a reload in
                                                 place). The old map's peers are told here
                                                 (0x06, world-presence); party HUD map change.
  on_enter_world          map_code, reason,      after the own 0x07 of EVERY map load (enter
                          old_map, first, hop    world, portal, warp, revive), the session is
                                                 on map_code and in its peers() already.
                                                 first=True on the connection's first entry.
                                                 hop: on that first entry, the continuity.Hop
                                                 when this connection CONTINUES the character
                                                 from another one (a 2009 channel change,
                                                 arch09-session-continuity), else None: a
                                                 once-per-login effect runs on first and not
                                                 hop (or hop.logout_announced).
  on_leave_world          map_code, reason,      the session is on no map any more and not
                          superseded             coming back on this connection: disconnect,
                                                 kick, idle reap, character deleted. superseded
                                                 = a newer session of the same account (2009
                                                 relogin) already owns the uid: do not despawn
                                                 that uid on the peers' screens.
  on_disconnect           reason                 the connection is gone (after on_leave_world
                                                 when it was in the world): party removal,
                                                 trade cancel, save.
  on_logout               reason, char_name      the character's login is over for good -
                                                 fired once per world login by continuity.py:
                                                 at once on a plain disconnect / kick / delete,
                                                 or held back while a 2009 channel hop may
                                                 continue it and never when it does. The
                                                 once-per-logout LINES go here (friend 0x60
                                                 offline + mentor, the P14 guild sub 21); the
                                                 session may be long closed.

Outbound path (F5 "Outbound path"; party-mp-registry `_send_to`, trade-mp-registry)
-----------------------------------------------------------------------------------
GameServer._send_encrypted encodes each packet under the RECEIVER's send_lock and appends
the bytes to the receiver's Outbox, so the queue order is the cipher order (send_seq and
the MT stream). The receiver's own connection thread writes at once (a reply is on the wire
before its handler returns, as it always was). Any other thread - another player's handler,
the tick scheduler, the combat driver, a hook - only queues and wakes the receiver's writer
thread: it never blocks on a peer socket, so a stalled client can never freeze the sender
while it holds its own combat lock or the world lock. Nothing is written while send_lock is
held, so taking a peer's send_lock under a combat lock is always short.

Locks: GameServer order world_lock -> combat_lock -> MapMonsters.lock -> db_lock ->
send_lock -> Outbox internals. World is NOT on that ladder: it has its own private index lock (World.lock), a
LEAF - every World method takes it only to read or write its own dicts, never calls out
(no hook, no send, no session lock) while holding it, and returns snapshots. So any thread
may call World while holding any of the locks above: the tick scheduler runs its callbacks
under world_lock and they take session combat locks (_tick_debuffs, _tick_buffs,
_tick_monster_ai, _tick_regen), while a handler or the combat driver holding its combat
lock asks World for its map audience (world.peers from _send_to_observers, map_sessions
from _ground_sessions). Were World guarded by world_lock, those two orders would deadlock
(P5 stage 1 review: skill-cast observers, kill loot, drop-worn vs the 0.05 s debuff tick).
Hooks are fired by the caller, outside the index lock.
"""
import collections
import logging
import re
import socket
import threading
import time

import ids

log = logging.getLogger('WS')

# Lifecycle hook names (roadmap F5 "Lifecycle hooks"). Groups register callbacks instead of
# inlining their cleanup in a handler, which is what makes one MapTransfer able to serve
# portals, warps, revive and room exits without every group editing it.
BEFORE_SERVER_MAP_LOAD = 'before_server_map_load'   # before 0x08/0x03/0x5E/0xA3/0xA4
ON_MAP_CHANGE = 'on_map_change'                     # left the old map (no map, in_world False)
ON_ENTER_WORLD = 'on_enter_world'                   # after the session's own 0x07
ON_LEAVE_WORLD = 'on_leave_world'                   # off every map for good (disconnect, delete)
ON_DISCONNECT = 'on_disconnect'                     # the connection is gone
# arch09-session-continuity (P12, continuity.py): the once-per-logout effects of a character -
# friend presence offline, the P14 guild logout line - fired once per world login, at once on a
# plain disconnect and held back for a possible 2009 channel hop (never fired when it is one).
ON_LOGOUT = 'on_logout'
# The rename hook (ROADMAP_2009_ADDENDUM C4, premium_cash-rename): a character changed its
# name. Fired by cashuse.CashUse.rename after the store renamed the record (its stable `cid`
# stays, store.py) and the renamed client got its 0x73 / 0x74; fn(server, session, old=, new=,
# cid=) tells the OTHER clients that show the old name: the messenger (friends' 0x0B, the
# mentor's 0x7B) and the party frames now, P14 guild (0xB3 sub 22) and P12 blacklist later.
# The stored references moved already (store.Store.rename_rewriters, which run under
# db_lock and so touch stored data only: no group lock, no packet, no I/O). Everything live -
# group locks (Group.lock -> store.lock order), session state, packets - belongs here: the
# hook fires after db_lock is let go.
ON_RENAME = 'on_rename'
HOOK_NAMES = (BEFORE_SERVER_MAP_LOAD, ON_MAP_CHANGE, ON_ENTER_WORLD, ON_LEAVE_WORLD, ON_DISCONNECT, ON_RENAME,
              ON_LOGOUT)

# The client's own UDP P2P port (C2S 0x2B p2p_udp_port): 42907 = WindSlayer_patched.exe,
# 42908 = WindSlayer_p2.exe (2008 project_multiclient; 2009 patch_2009.py --p2). The only
# thing that tells the server which of the two exes a session is, whatever account it
# logged in with - `wsdev sendspec ... --to c2` targets by it.
CLIENT_P2P_PORT_BASE = 42907


def client_number(session):
    """1 for WindSlayer_patched.exe, 2 for WindSlayer_p2.exe, ... from the P2P port the
    session sent in C2S 0x2B; None before enter world or for another port."""
    p2p = session.get('p2p')
    try:
        port = int(p2p[1])
    except (TypeError, ValueError, IndexError):
        return None
    n = port - CLIENT_P2P_PORT_BASE + 1
    return n if 1 <= n <= 9 else None


# 'c:2' / 'client:2': the colon keeps the form apart from every character and account name
# (names.ALLOWED_RE is [A-Za-z0-9]+), so a character called 'c2' or 'client2' stays
# targetable by name.
_CLIENT_TARGET_RE = re.compile(r'^(?:c|client)\s*:\s*(\d+)$', re.IGNORECASE)


def parse_client_target(target):
    """'c:2' / 'client:2' -> 2 (a client exe, see client_number); else None. 'c2' is a
    name like any other, and a bare number stays an account uid (lc-uid-online: test = 1,
    admin = 2)."""
    if not isinstance(target, str):
        return None
    m = _CLIENT_TARGET_RE.match(target.strip())
    return int(m.group(1)) if m else None


def reachable(session):
    """A session a packet may be sent to right now: its own 0x07 is out and it is on a map
    (World.arrive), it has a socket, and it was neither kicked nor closed."""
    return bool(session.get('in_world')) and session.get('world_map') is not None \
        and session.get('sock') is not None and not session.get('kicked') and not session.get('closed')


class Hooks:
    """Named session-lifecycle callbacks, called in registration order.

    A hook body may send packets (the stall close 0x84 that must precede 0x08, the 0x06
    that tells the old map's peers the player left), so the caller decides WHERE in its
    sequence it fires. A failing callback is logged and skipped: one group's cleanup must
    never leave a map transfer half done, because the client would sit on the loading
    overlay for ever (spec 0x08 hazard C1)."""

    def __init__(self):
        self._by_name = {name: [] for name in HOOK_NAMES}

    def register(self, name, fn):
        if name not in self._by_name:
            raise KeyError(f'{name}: not a lifecycle hook ({", ".join(HOOK_NAMES)})')
        self._by_name[name].append(fn)
        return fn

    def unregister(self, name, fn):
        """Remove one registration of fn (tests; a group that shuts down). True if found."""
        try:
            self._by_name[name].remove(fn)
            return True
        except (KeyError, ValueError):
            return False

    def registered(self, name):
        return list(self._by_name[name])

    def fire(self, name, *args, **kwargs):
        """Run every callback for `name`. Returns the number that ran without raising."""
        ran = 0
        for fn in self.registered(name):
            try:
                fn(*args, **kwargs)
                ran += 1
            except Exception:
                log.exception(f'[WORLD] {name} hook {getattr(fn, "__name__", fn)!r} failed; '
                              f'continuing (the sequence must not stop half way)')
        return ran


class Outbox:
    """The ordered outbound byte queue of ONE session (roadmap F5 "Outbound path").

    put() is called under the session's send_lock right after the packet was encoded, so
    the queue order is the encode order. Writing happens under `_wlock` only: whoever holds
    it (the connection thread in flush(), or this outbox's writer thread) pops from the
    front and sendall()s, so bytes reach the socket in queue order even when both write.
    kick() never blocks: the writer thread is started on first use and waits for work.

    A write error, the socket's own timeout (a client that stopped reading for
    RECV_TIMEOUT_SECS) or a backlog over MAX_BACKLOG_BYTES means the connection is dead -
    a partly written packet has broken the client's framing anyway - so the outbox closes,
    drops what is queued and shuts the socket down; the connection thread's recv then ends
    and the normal disconnect path runs (save, hooks)."""

    MAX_BACKLOG_BYTES = 8 * 1024 * 1024

    def __init__(self, sock, label=''):
        self.sock = sock
        self.label = label
        self._queue = collections.deque()
        self._bytes = 0
        self._cv = threading.Condition(threading.Lock())
        self._wlock = threading.Lock()
        self._thread = None
        self.closed = False
        self.sent_packets = 0

    def put(self, data):
        """Queue one encoded packet. Raises OSError once the connection is closed (the
        same error a send to a closed socket always raised)."""
        data = bytes(data)
        overflow = False
        with self._cv:
            if self.closed:
                raise OSError(f'outbox {self.label}: connection closed')
            self._queue.append(data)
            self._bytes += len(data)
            overflow = self._bytes > self.MAX_BACKLOG_BYTES
        if overflow:
            self._fail(f'{self._bytes} B queued (> {self.MAX_BACKLOG_BYTES}): the client stopped reading')
            raise OSError(f'outbox {self.label}: backlog over {self.MAX_BACKLOG_BYTES} B')

    def pending(self):
        with self._cv:
            return len(self._queue)

    def flush(self):
        """Write everything queued on the calling thread (the session's own connection
        thread). Propagates the socket error, as a direct sendall did."""
        with self._wlock:
            try:
                self._drain()
            except OSError as e:
                self._fail(f'write failed: {e}')
                raise

    def kick(self):
        """Have the writer thread write what is queued. Never blocks on the socket."""
        with self._cv:
            if self.closed:
                return
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=f'outbox-{self.label}',
                                                daemon=True)
                self._thread.start()
            self._cv.notify()

    def close(self):
        """Stop writing: drop the backlog and let the writer thread exit."""
        with self._cv:
            self.closed = True
            self._queue.clear()
            self._bytes = 0
            self._cv.notify_all()

    def wait_idle(self, timeout=2.0):
        """True once nothing is queued (tests and diagnostics)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._cv:
                if not self._queue:
                    return True
            time.sleep(0.005)
        return False

    # --- internals ---
    def _drain(self):
        while True:
            with self._cv:
                if not self._queue:
                    return
                data = self._queue.popleft()
                self._bytes -= len(data)
            self.sock.sendall(data)
            self.sent_packets += 1

    def _run(self):
        while True:
            with self._cv:
                while not self._queue and not self.closed:
                    self._cv.wait()
                if self.closed:
                    return
            try:
                with self._wlock:
                    self._drain()
            except OSError as e:
                self._fail(f'write failed: {e}')
                return

    def _fail(self, why):
        with self._cv:
            if self.closed:
                return
        log.warning(f'[WORLD] outbox {self.label}: {why}; closing the connection')
        self.close()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class MapMonsters(dict):
    """uid -> Monster: the ONE set of template entities of a map, shared by every player on
    it (world-shared-monsters, P5 stage 3; world_movement_npc.md F6 / 3.1 `entities`).

    The dict itself is what an attached session's session['monsters'] points to, so every
    handler that looks a monster up by the uid a client named (hit report, trap, contact,
    skill box) finds the same object whichever player named it. Besides the monsters it
    keeps who sees them:

      viewers  {viewer key: session} of the sessions attached to this map: their client got
               this map's S2C 0x1A burst after its own 0x03/0x07 (GameServer.
               _spawn_map_monsters) and they left on their next map load or disconnect;
      held     {viewer key: set of monster uids} - what each viewer's client holds right now
               (alive or corpse). No client handler de-duplicates a 0x1A uid, a 0x06 for an
               unknown uid is only a no-op, so every monster packet is sent per viewer
               against this book: 0x1A only for a uid it does not hold, 0x06 / 0x29 / 0x2A /
               0x9E / 0x1B only for one it does;
      uids     the server-wide uid block (ids.MONSTER, World.alloc_monster_uids) of this map's
               spawn list, allocated on the first population and reused when a discarded
               instance is rebuilt, so a map's monsters keep their uids for the server's life.

    `lock` guards all of it and every Monster in it. Lock order (GameServer): world_lock ->
    session combat_lock -> this lock -> store.lock -> send_lock. It is taken under the combat
    lock of the session whose action touches the monsters (hit, skill, trap, contact, death,
    map load), or alone by the tick thread (monster AI, corpse despawn / respawn, discard).
    Nothing takes a session combat lock while holding it except for a session whose combat
    lock the thread already holds (the killer's exp / drop on a kill)."""

    def __init__(self, map_code):
        super().__init__()
        self.map_code = int(map_code)
        self.lock = threading.RLock()
        self.populated = False       # the spawn list was built (False again once discarded)
        self.viewers = {}
        self.held = {}
        self.uids = []

    # dict equality compares the contents (an empty instance == {}); identity is what the
    # server compares sessions by (session['monsters'] is mons), so no __eq__ override.
    __hash__ = object.__hash__

    def __repr__(self):
        return (f'MapMonsters({self.map_code}, {len(self)} monster(s), '
                f'{len(self.viewers)} viewer(s))')


class MapInstance:
    """One field map's live state (F5; world_movement_npc.md 3.1): the in-world sessions and,
    since world-shared-monsters, the map's monsters (MapMonsters). A map nobody stands on
    keeps its monsters - dead ones stay dead until their respawn - for MOB_MAP_KEEP_SECS
    after `empty_since` (the kill -> leave -> return free-respawn exploit, world F6 step 5);
    GameServer._tick_monster_maps discards them after that."""

    def __init__(self, map_code):
        self.map_code = int(map_code)
        self.sessions = {}          # uid -> session: arrived (own 0x07 out), not departed
        self.empty_since = time.monotonic()
        self.monsters = MapMonsters(self.map_code)

    def __repr__(self):
        return f'MapInstance({self.map_code}, {len(self.sessions)} session(s), {len(self.monsters)} monster(s))'


class World:
    def __init__(self):
        # The index lock is private and a leaf (module docstring "Locks"): never the
        # server's world_lock, which the tick scheduler holds while it takes combat locks.
        self.lock = threading.RLock()
        self.by_uid = {}          # account uid -> session (logged in: character select or world)
        self.by_name = {}         # lower(character name) -> session (entered world)
        self.maps = {}            # map code -> MapInstance (created on first arrival)
        self.hooks = Hooks()      # F5 lifecycle hooks (fired by MapTransfer / the recv loop)
        # world-shared-monsters: the server-wide monster uid allocator (ids.MONSTER, F3).
        self._next_monster_uid = ids.MONSTER.lo

    # ---------------------------------------------------------------- identity ---
    def claim(self, session, uid, username, *, replace=False):
        """Make `session` the online session of account `uid`. When another session already
        holds the account, it is removed from the indexes and returned; `session` is NOT
        registered (the caller closes the old one and refuses this login with 0x02 result 4,
        login_character.md F2 step 2f).

        replace=True is the 2009 relogin (C2S 0x01 carrying the account's live session key,
        client-2009-login): the old session is removed and returned as well, but `session`
        takes the account over at once - the client is reconnecting after a channel change
        and must not be refused while the server has not reaped its old socket yet."""
        with self.lock:
            old = self.by_uid.get(uid)
            if old is not None and old is not session:
                self.drop(old)
                if not replace:
                    return old
                self.by_uid[uid] = session
                session['uid'] = uid
                session['username'] = username
                return old
            self.by_uid[uid] = session
            session['uid'] = uid
            session['username'] = username
            return None

    def enter(self, session, char_name):
        """Index the character a logged-in session entered the world with. False when the
        session no longer owns its account (kicked while its request was in flight)."""
        with self.lock:
            uid = session.get('uid')
            if uid is None or self.by_uid.get(uid) is not session:
                return False
            for key in [k for k, s in self.by_name.items() if s is session]:
                del self.by_name[key]
            self.by_name[str(char_name).lower()] = session
            return True

    def leave(self, session):
        """Remove only this session's character-name index entry: it is back at character
        select (or the character was just deleted) but still logged in, so `by_uid` stays.
        True when something was removed."""
        with self.lock:
            keys = [k for k, s in self.by_name.items() if s is session]
            for key in keys:
                del self.by_name[key]
            return bool(keys)

    def drop(self, session):
        """Remove `session` from the identity indexes (by_uid, by_name), identity-checked so
        a stale call for a kicked session never removes its replacement. True when anything
        was removed. Map membership is NOT touched: a session leaves its MapInstance through
        depart(), on the connection's own thread, where on_leave_world fires exactly once;
        until then reachable() already skips a kicked or closed session."""
        with self.lock:
            removed = False
            uid = session.get('uid')
            if uid is not None and self.by_uid.get(uid) is session:
                del self.by_uid[uid]
                removed = True
            for key in [k for k, s in self.by_name.items() if s is session]:
                del self.by_name[key]
                removed = True
            return removed

    def session(self, uid):
        with self.lock:
            return self.by_uid.get(uid)

    def by_char_name(self, name):
        with self.lock:
            return self.by_name.get(str(name).lower())

    def online(self):
        """Snapshot of the logged-in sessions."""
        with self.lock:
            return list(self.by_uid.values())

    def is_online(self, session):
        uid = session.get('uid')
        with self.lock:
            return uid is not None and self.by_uid.get(uid) is session

    def superseded(self, session):
        """True when a DIFFERENT session now owns this session's account uid (2009 relogin):
        anything addressed by that uid now means the new session."""
        uid = session.get('uid')
        with self.lock:
            owner = self.by_uid.get(uid) if uid is not None else None
            return owner is not None and owner is not session

    # ------------------------------------------------------------------- maps ---
    def map(self, map_code):
        """The MapInstance of `map_code`, created on first use."""
        code = int(map_code)
        with self.lock:
            inst = self.maps.get(code)
            if inst is None:
                inst = self.maps[code] = MapInstance(code)
            return inst

    def arrive(self, session, map_code, now=None):
        """Put an in-world session on `map_code` (after its own 0x07: roadmap F5 in_world).
        It leaves any other map first. False (nothing indexed) when the session no longer
        owns its account uid - a kicked or replaced session must never become a peer."""
        now = time.monotonic() if now is None else now
        with self.lock:
            uid = session.get('uid')
            if uid is None or self.by_uid.get(uid) is not session:
                return False
            self.depart(session, now)
            inst = self.map(map_code)
            inst.sessions[uid] = session
            inst.empty_since = None
            session['world_map'] = inst.map_code
            return True

    def depart(self, session, now=None):
        """Take the session off its map (a map load starts, it leaves the world). Returns the
        map code it was on, or None when it was on none. The index entry is removed only if
        it is still this session (a relogin's new session may have arrived with the uid)."""
        with self.lock:
            code = session.pop('world_map', None)
            if code is None:
                return None
            inst = self.maps.get(code)
            uid = session.get('uid')
            if inst is not None and inst.sessions.get(uid) is session:
                del inst.sessions[uid]
                if not inst.sessions:
                    inst.empty_since = time.monotonic() if now is None else now
            return code

    def map_of(self, session):
        """The map the session is registered on (== current_map while in world), or None."""
        with self.lock:
            return session.get('world_map')

    def instances(self):
        """Snapshot of every MapInstance created so far."""
        with self.lock:
            return list(self.maps.values())

    def monster_maps(self):
        """Snapshot of the MapMonsters whose spawn list is built (populated)."""
        return [inst.monsters for inst in self.instances() if inst.monsters.populated]

    def alloc_monster_uids(self, n):
        """`n` consecutive monster uids from the server-wide space 0x000F0000..0x001FFFFF
        (ids.MONSTER; world-shared-monsters: "server-wide uid allocator"). Every map gets its
        block once (MapMonsters.uids), so no two monsters on the server ever share a uid."""
        n = int(n)
        with self.lock:
            lo = self._next_monster_uid
            if n <= 0:
                return []
            if lo + n - 1 > ids.MONSTER.hi:
                raise ids.IdSpaceExhausted(f'{ids.MONSTER.name}: {n} more uids do not fit '
                                           f'(next {lo:#x}, last {ids.MONSTER.hi:#x})')
            self._next_monster_uid = lo + n
            return list(range(lo, lo + n))

    def map_sessions(self, map_code, exclude=None):
        """Reachable sessions on `map_code` (snapshot), minus `exclude` and any session with
        its uid."""
        with self.lock:
            inst = self.maps.get(map_code)
            members = list(inst.sessions.values()) if inst is not None else []
        skip_uid = exclude.get('uid') if exclude is not None else None
        return [s for s in members if s is not exclude and reachable(s)
                and (skip_uid is None or s.get('uid') != skip_uid)]

    def peers(self, session, map_code=None):
        """The other reachable sessions on the session's map (or on `map_code`): the
        audience of everything the session does that others see. Never the session."""
        code = self.map_of(session) if map_code is None else map_code
        if code is None:
            return []
        return self.map_sessions(code, exclude=session)

    # ----------------------------------------------------------- online views ---
    def in_world_sessions(self):
        """Every reachable in-world session (snapshot)."""
        with self.lock:
            online = list(self.by_uid.values())
        return [s for s in online if reachable(s)]

    def find(self, target, *, in_world=True):
        """The session of a character name (a str, case-insensitive) or an account uid (an
        int), or None. A decimal string is a name, never a uid: names.ALLOWED_RE allows a
        character called '12', and a whisper to it must not reach account 12. in_world=True
        (the default) returns only a reachable in-world session: a character at select or
        mid map load cannot be whispered, invited or traded with."""
        if isinstance(target, bool) or target is None:
            return None
        with self.lock:
            if isinstance(target, int):
                found = self.by_uid.get(target)
            else:
                found = self.by_name.get(str(target).strip().lower())
        if found is None or (in_world and not reachable(found)):
            return None
        return found

    def online_names(self, names):
        """{lower(name): session} for the given character names that are in world."""
        wanted = {str(n).lower() for n in names}
        with self.lock:
            hits = {k: s for k, s in self.by_name.items() if k in wanted}
        return {k: s for k, s in hits.items() if reachable(s)}

    def describe(self):
        """{map_code: [character names]} of the occupied maps (logs, `!who`)."""
        with self.lock:
            return {code: sorted(str(s.get('char_name') or s.get('username') or '?')
                                 for s in inst.sessions.values())
                    for code, inst in sorted(self.maps.items()) if inst.sessions}
