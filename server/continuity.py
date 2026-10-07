#!/usr/bin/env python3
"""
continuity.py - channel-change robustness and session continuity (P12 ch-4,
arch09-session-continuity)
==========================================================================
Roadmap items (re_tools/docs/ROADMAP_2009_ADDENDUM.md P12 + 3.0; design
re_tools/docs/systems_2009/blacklist_channels.md B.5 rules R1-R8, "blch" below).

The 2009 client has NO channel-change packet (blch B.5 [V]). Change Channel / Change Avatar
(system window 0x1AE ctrl 6 / 5, FUN_00448730 case 0x1AE) fetch the version list from 7011,
CLOSE the game socket, and the client logs in again on the chosen listener with C2S 0x01
carrying the SAME u32 session key (scene+0x154 is not refreshed on that path). On S2C 0x02
success with gs+0x408 set it sends C2S 0x2B at once with the character still in scene+0x20C
and arms timer 2 for 5.5 s ("No response from the server." unless an S2C 0x03 kills it).
Change Avatar sets gs+0x404 instead and lands on the character select of the same channel.

What the server guarantees (blch B.5 table):
  R1  the channel is the listener (channels.py; session['channel']).
  R2  the old connection closes with no goodbye: the normal disconnect path runs (save,
      0x06 despawn to the map peers, party / trade / room / stall cleanup) - only the
      once-per-login LINES are held back (session continuity below).
  R3  the relogin with the live key is accepted even while the old socket is not reaped
      (World.claim replace=True; one World sees every channel).
  R4  the account uid is the store's, the same on every channel.
  R5  0x02 is answered only after every closing session of the account finished its
      disconnect path, SAVE included (GameServer._await_closing, CLOSE_WAIT_SECS), so the
      relogin's immediate 0x2B loads the saved map / position / HP; the 0x03 then goes out
      at once (timer 2 starts at the 0x02, so that wait costs it nothing). A 0x03 later
      than RELOGIN_REPLY_SECS after the 0x02 is logged as a WARNING.
  R6  0x03 channel_id, 0x99 sub 8 and the friend rows all read GameServer._channel_no.
  R7  the version server is this process.
  R8  only listeners that are up are advertised (channels.Channels.advertised).

G-CHP (blch E-B2) - DECIDED: a party does NOT survive a channel change. The hop's old
connection closes and the member leaves the party on disconnect (party.py on_disconnect,
0x51 to the others), exactly like any disconnect. Why: the party frames 0x4F/0x51/0x54/0x55
carry no channel or map, party formation needs a visible entity (popup 0x50) on the same
channel, and P18 ch-2 isolates the channels - a party split over two channels could neither
share exp (same-map rule) nor see its members. The default the addendum names; T-C12 records
what both clients show.

The stale gs+0x408 flag (blch E-B5): the client clears gs+0x404/0x408 only in FUN_0042dbf0
(the next Change Avatar / Channel), so after one channel change EVERY later S2C 0x02 success
in that client run takes the auto-0x2B path - it skips character select and does not read
the 0x02 tail, i.e. not the account uid and not a new session key. Server side:
  - any 0x2B right after any 0x02 is taken (the 0x2B handler never needs a select step);
  - the session key is ADOPTED, never rotated under such a client: a login that presents a
    non-zero key while the account is offline keeps that key as the live one (GameServer.
    _login_session_key), so the client's scene+0x154 - which it may not have refreshed -
    always equals the server's key and its next channel change is a relogin, not a
    duplicate login (0x02 result 4). Only key 0 (the launcher's first login) mints a key;
  - a 0x2B within STALE_408_SECS of a FRESH login's 0x02 is logged (the client skipped
    character select: a stale gs+0x408), for the live check.

Session continuity (arch09-session-continuity)
----------------------------------------------
A channel HOP = a relogin with the account's live session key within CHANNEL_HOP_SECS of
the old socket closing (or replacing a socket not yet reaped), whose first world entry is
the same character. A hop repeats no once-per-login effect:
  - the 0x15 welcome line (GameServer._hook_welcome),
  - the events' entry announcement, login gift and Event News popup (events.after_resync),
  - the friend "<X> has logged in. (Friend)" / offline lines and the mentor lines
    (messenger.py) - the watchers' rows stay online through the hop,
  - the P14 guild login / logout lines (sub 20 / 21) and the pending guild points.
It still sends what describes the NEW connection: 0x99 sub 8 "In channel N." (a new
connection), and - when the channel number changed - one S2C 0x60 with the new channel to
the watchers (presence 0 prints "<X> has logged in. (Friend)" once: the client has no silent
channel update; P18 ch-3 makes it presence 4 "(Friend, Channel N)" for other-channel
watchers). A hop back to the SAME channel (Change Avatar, or Change Channel to the current
one) sends nothing to the watchers.

The once-per-LOGOUT effects are the ON_LOGOUT world hook (world.py). The old connection
closes first and the relogin comes seconds later, so a logout that may be a hop is HELD
BACK: a Departure is recorded and ON_LOGOUT fires
  - at once when no hop is possible: the 2008 client (no session key), CHANNEL_HOP_SECS 0,
    a close the server or the TCP stack made (a kick / duplicate login / maintenance close:
    session 'kicked' / 'kick_pending'; the idle reaper: 'idle reap'; the dead-peer keepalive:
    'connection timed out'; a handler error: 'error: ...' - SERVER_CLOSES), a character
    deleted in the world, or - with CHANNEL_HOP_VERSION_HINT (default) - no version fetch
    from the client's IP around the close (a plain quit or crash: only Change Channel /
    Avatar fetch the list right before they close, FUN_00448730 case 0x1AE steps 2-3).
    'recv error' (WSAECONNRESET) stays a CLIENT close: Windows sends RST instead of FIN when
    the client's closesocket finds unread data in its receive buffer, which a Change Channel
    teardown on a busy map can - so it is decided by the version hint like 'client closed';
  - when CHANNEL_HOP_SECS pass with no relogin (the 'channel-hop' tick);
  - when the relogin enters ANOTHER character (Change Avatar) or closes at select;
  - when the relogin sits at character select for CHANNEL_HOP_SECS (Change Avatar, then
    idle): the 'channel-hop' tick re-armed at the relogin fires it, so the watchers do not
    keep a stale online row for as long as the player stays there; the departure is kept,
    so entering the same character later is still a hop (logout_announced: the login line
    goes out again);
  - never, when the relogin enters the same character within that time (the hop).
While a logout is held back the friend rows (S2C 0x0B) still show the character online on
its old channel (ghost()), so a watcher's portal in the gap does not grey a row that no
0x60 will light again. A logout that already fired (no hint) still lets a relogin within
the window count as a hop for the welcome / events, and the watchers then get the normal
login line (Hop.logout_announced: correct state beats cosmetics).
Known limit (documented, cosmetic): the hint is read ONCE, when the close is handled, and
only fetches booked by then count. The version accept (the 7011 listener thread) and the game
FIN (this connection's thread) arrive back to back, so when the FIN is handled first - a
REMOTE client's non-blocking connect, or, rarely, two threads scheduled the other way round
on a local one - the hop shows one logout/login pair (Hop.logout_announced keeps the state
right). A grace window after the close would need the logout deferred by a tick for every
quit (the old VERSION_HINT_AFTER_SECS looked AFTER the close but was checked at the close, so
it never caught this race: removed, P12 review). Set CHANNEL_HOP_VERSION_HINT false where it
matters (every 2009 logout line then waits CHANNEL_HOP_SECS).

    cont = Continuity(server)              # GameServer.continuity
    register(server.world.hooks, cont)     # right after the messenger's hooks (packet order)
    cont.relogged(session, uid, relogin, replaced)   # _handle_login, after the close wait
    cont.entering(session, 'TestHero')     # _handle_enter_world: -> Hop or None
    cont.ghost('TestHero')                 # Departure held back, or None (friend rows)

Locks: Continuity.lock is a leaf - hooks (ON_LOGOUT) and timers are fired / armed outside
it. The 'channel-hop' tick runs under world_lock like every tick (ticks.py).
"""
import logging
import threading
import time
from dataclasses import dataclass, field

import config as cfgmod
import world as worldmod

log = logging.getLogger('WS')

# R5 (blch B.5 step 9): timer 2 of the relogin path.
RELOGIN_REPLY_SECS = 5.5
# How long a login waits for the closing sessions of its account (GameServer._await_closing).
# A disconnect path takes milliseconds; past this the login goes on (logged) and the hop
# bookkeeping links the late departure itself (Continuity.departed / relogged).
CLOSE_WAIT_SECS = 1.5
# E-B5 diagnostic: a 0x2B this soon after a fresh login's 0x02 skipped character select.
STALE_408_SECS = 1.0
# session['closing'] reasons (GameServer._handle: close_reason) that the server or the TCP stack
# made - never a Change Channel / Avatar teardown, so never held back (module docstring).
SERVER_CLOSES = ('idle reap', 'connection timed out')


@dataclass
class Departure:
    """A character that left the world on a connection that may be continued by a hop."""
    uid: int
    char_name: str
    channel: int
    t: float                                            # monotonic() of the close
    session: dict = field(repr=False)                   # the closed session (ON_LOGOUT subject)
    fired: bool = False                                 # ON_LOGOUT has run for it
    held_by: object = field(default=None, repr=False)   # the relogin session continuing it
    timer: object = field(default=None, repr=False)
    why: str = ''


@dataclass(frozen=True)
class Hop:
    """session['channel_hop']: this connection continues `char_name` of an earlier one."""
    char_name: str
    from_channel: int
    to_channel: int
    gap: float
    logout_announced: bool

    @property
    def changed_channel(self):
        return self.from_channel != self.to_channel


def _key(name):
    return str(name or '').strip().lower()


class Continuity:
    def __init__(self, server):
        self.server = server
        self.lock = threading.RLock()
        self.departures = {}            # account uid -> Departure (global: a hop crosses channels)
        self.mono = time.monotonic      # tests replace it

    # -------------------------------------------------------------- config ---
    def window(self):
        return float(self.server.config.get('CHANNEL_HOP_SECS', 30.0))

    def supported(self):
        """Only the 2009 client reconnects by itself with a session key."""
        return getattr(self.server, 'client_build', cfgmod.BUILD_2008) == cfgmod.BUILD_2009

    # ------------------------------------------------- the old connection goes ---
    def departed(self, session, reason=''):
        """ON_LEAVE_WORLD / ON_DISCONNECT (both: a session mid map load has no map, so only
        on_disconnect reaches it). The first call per world entry decides; the rest do
        nothing. Fires ON_LOGOUT now or records a Departure that a hop may continue."""
        name = session.pop('login_char', None)
        if name is None:
            self._holder_gone(session)
            return
        server = self.server
        uid = session.get('uid')
        dep = Departure(uid=uid, char_name=name, channel=server._channel_no(session), t=self.mono(),
                        session=session, why=str(reason or ''))
        if uid is None or not self.supported():
            self._fire(dep, reason)
            return
        if server.world.superseded(session):
            # R3: a relogin with the live key already owns the account (its socket closed
            # after the new login): the successor resolves this one at its first entry.
            if self.window() <= 0:
                # hop detection off: the P6 rule - a superseded session announces nothing,
                # its successor's entry announces the login
                log.info(f'[HOP] {name!r} (uid {uid}) replaced by a relogin: no logout (CHANNEL_HOP_SECS 0)')
                return
            self._superseded(dep, server.world.session(uid))
            return
        defer, why = self._may_defer(session, dep.t)
        if not defer:
            self._fire(dep, reason)
            if self.window() > 0 and self.supported():
                # Kept for the window anyway: a relogin within it is still a hop for the
                # welcome and the events (its watchers get the login line again).
                self._store(dep, expire=True)
            log.info(f'[HOP] {name!r} (uid {uid}) logged out at once ({why})')
            return
        self._store(dep, expire=True)
        log.info(f'[HOP] {name!r} (uid {uid}) left channel {dep.channel} ({reason}; {why}): logout held '
                 f'{self.window():g} s for a channel hop')

    def _superseded(self, dep, owner):
        """departed() of a session a relogin replaced: the owner continues it. Normally the
        owner's login waited for this close (GameServer._await_closing) and relogged() comes
        next; when that wait ran out, relogged() is already past - the departure is linked
        to the owner here, or, if the owner has entered meanwhile, dropped (it entered the
        same character: one login) or logged out (another character)."""
        dep.held_by = owner
        dep.why = 'replaced by a relogin'
        # expire: the 'channel-hop' tick, should the relogin sit at character select (relogged()
        # re-arms it when it comes after this)
        self._store(dep, expire=True)
        late = None
        with self.lock:
            if owner is not None and owner.get('relogged') and self.departures.get(dep.uid) is dep:
                if owner.get('login_char') is None:
                    owner['hop_from'] = dep                 # entering() will resolve it
                else:
                    del self.departures[dep.uid]
                    self._cancel(dep)
                    late = owner['login_char']
        if late is None:
            log.info(f'[HOP] {dep.char_name!r} (uid {dep.uid}) replaced by a relogin on channel '
                     f'{self.server._channel_no(owner or {})}: logout held for its entry')
        elif _key(late) == _key(dep.char_name):
            log.info(f'[HOP] {dep.char_name!r} (uid {dep.uid}): its relogin entered already - one login')
        else:
            self._fire(dep, f'the relogin entered {late!r} instead')

    def _may_defer(self, session, now):
        """(hold the logout back?, why) for a closing session (module docstring)."""
        server = self.server
        if self.window() <= 0:
            return False, 'CHANNEL_HOP_SECS 0'
        if not session.get('closing'):
            return False, 'left the world, the connection stays'
        if session.get('kicked') or session.get('kick_pending'):
            return False, 'closed by the server'
        closing = str(session.get('closing') or '')
        if closing in SERVER_CLOSES or closing.startswith('error:'):
            return False, f'closed by the server / TCP stack ({closing})'
        uid = session.get('uid')
        if not uid or not getattr(server, 'session_keys', {}).get(uid):
            return False, 'no live session key'
        if server.config.get('CHANNEL_HOP_VERSION_HINT', True):
            ip = (session.get('addr') or ('',))[0]
            if not server.channels.version_fetched(ip, now):
                return False, f'no version fetch from {ip} around the close (a quit)'
            return True, f'version fetch from {ip} (Change Channel / Avatar teardown)'
        return True, 'CHANNEL_HOP_VERSION_HINT off'

    def _store(self, dep, expire=False):
        old = None
        with self.lock:
            prev = self.departures.get(dep.uid)
            if prev is not None and prev is not dep:
                self._cancel(prev)
                old = prev if not prev.fired else None
            self.departures[dep.uid] = dep
        if old is not None:
            self._fire(old, 'superseded by a newer departure')
        if expire:
            dep.timer = self.server.ticks.call_later(self.window(), self._expire, dep, name='channel-hop')

    @staticmethod
    def _cancel(dep):
        if dep.timer is not None:
            dep.timer.cancel()
            dep.timer = None

    def _expire(self, dep):
        """'channel-hop' tick: the window passed. A held-back logout fires now. One a relogin
        session holds (armed again by relogged(): it sits at character select) fires too, but
        the departure stays for that session: entering the same character is still a hop -
        with logout_announced, so the watchers get the login line again - and closing at
        select or entering another character fires nothing twice (P12 review: before, the old
        character stayed online to its watchers for as long as the player idled at select)."""
        with self.lock:
            if self.departures.get(dep.uid) is not dep:
                return
            dep.timer = None
            holder = dep.held_by
            kept = holder is not None and not holder.get('closed')
            if not kept:
                del self.departures[dep.uid]
        if not dep.fired:
            if kept:
                log.info(f'[HOP] {dep.char_name!r} (uid {dep.uid}): its relogin is at character select '
                         f'{self.window():g} s - logout (a later entry of it is still a hop)')
                self._fire(dep, 'the relogin stayed at character select')
            else:
                log.info(f'[HOP] {dep.char_name!r} (uid {dep.uid}): no relogin within {self.window():g} s - logout')
                self._fire(dep, 'no channel hop within the window')

    def _holder_gone(self, session):
        """A relogin session that held a departure closed without entering the world (Change
        Avatar, then Exit at character select): the held logout fires now - unless yet
        another relogin of the account replaced it, which then holds it instead."""
        dep = session.pop('hop_from', None)
        if dep is None:
            return
        world = self.server.world
        with self.lock:
            if self.departures.get(dep.uid) is not dep:
                return
            if world.superseded(session):
                dep.held_by = world.session(dep.uid)
                return
            del self.departures[dep.uid]
            self._cancel(dep)
        if not dep.fired:
            self._fire(dep, 'the relogin closed at character select')

    def _fire(self, dep, reason):
        """ON_LOGOUT for a departure, once (outside the lock: the hooks push packets)."""
        with self.lock:
            if dep.fired:
                return
            dep.fired = True
        self.server.world.hooks.fire(worldmod.ON_LOGOUT, self.server, dep.session,
                                     reason=str(reason or ''), char_name=dep.char_name)

    # ------------------------------------------------- the relogin arrives ---
    def relogged(self, session, uid, relogin, replaced=None):
        """GameServer._handle_login, after the close wait: a relogin with the live key within
        the window holds the account's departure (the hop is decided at the first 0x2B);
        any other login of the account releases it (its logout fires: a fresh login is no
        hop). `replaced`: the session claim(replace=True) took the account from - used when
        its close is still running and recorded nothing yet. A held departure's 'channel-hop'
        timer is re-armed from the relogin (_expire: logout after the window at select)."""
        now = self.mono()
        release = None
        with self.lock:
            session['relogged'] = True                      # _superseded may link to it from now
            dep = self.departures.get(uid)
            if (dep is None and relogin and replaced is not None and replaced.get('login_char')
                    and self.supported() and self.window() > 0):
                name = replaced.pop('login_char')
                dep = Departure(uid=uid, char_name=name, channel=self.server._channel_no(replaced), t=now,
                                session=replaced, held_by=session, why='replaced (its close still running)')
                self.departures[uid] = dep
            if dep is None:
                return None
            holder = dep.held_by
            busy = holder is not None and holder is not session and not holder.get('closed') \
                and not holder.get('kicked')
            ok = (relogin and self.supported() and self.window() > 0 and not busy
                  and (holder is session or now - dep.t <= self.window()))
            if ok:
                dep.held_by = session
                self._cancel(dep)
                session['hop_from'] = dep
            else:
                del self.departures[uid]
                self._cancel(dep)
                release = dep
        if release is not None:
            if not release.fired:
                self._fire(release, 'a fresh login of the account')
            return None
        # Re-armed from now (outside the lock, like every timer here): a relogin that stays at
        # character select (Change Avatar, then idle) logs the old character out after the
        # window (_expire). entering() cancels it; one it missed finds no departure and returns.
        dep.timer = self.server.ticks.call_later(self.window(), self._expire, dep, name='channel-hop')
        log.info(f'[HOP] relogin of uid {uid} {now - dep.t:.1f} s after {dep.char_name!r} left channel '
                 f'{dep.channel}: a channel hop if it enters {dep.char_name!r}')
        return dep

    def entering(self, session, char_name):
        """C2S 0x2B of `char_name` accepted (World.enter done): returns the Hop when this is
        the connection's first entry and continues that character, else None. Sets
        session['channel_hop'] and session['login_char'] (what departed() reads)."""
        with self.lock:                                     # atomic against _superseded's link
            session['channel_hop'] = None
            session['login_char'] = char_name
            dep = session.pop('hop_from', None)
            if dep is None:
                return None
            if self.departures.get(dep.uid) is dep:
                del self.departures[dep.uid]
            self._cancel(dep)
        if _key(char_name) != _key(dep.char_name) or session.get('entered_once'):
            if not dep.fired:
                self._fire(dep, f'the relogin entered {char_name!r} instead')
            log.info(f'[HOP] uid {dep.uid}: the relogin enters {char_name!r}, not {dep.char_name!r} - '
                     f'a login (the old character logged out)')
            return None
        hop = Hop(char_name=str(char_name), from_channel=dep.channel, to_channel=self.server._channel_no(session),
                  gap=max(0.0, self.mono() - dep.t), logout_announced=dep.fired)
        session['channel_hop'] = hop
        log.info(f'[HOP] {char_name!r} (uid {dep.uid}) channel {hop.from_channel} -> {hop.to_channel} '
                 f'after {hop.gap:.1f} s: session continued (no welcome / event / friend / guild '
                 f'login lines{"; its logout already went out, so the login line goes too" if dep.fired else ""})')
        return hop

    # ------------------------------------------------------------- views ---
    def ghost(self, name):
        """The held-back Departure of character `name` (the friend rows keep it online), or
        None."""
        wanted = _key(name)
        with self.lock:
            for dep in self.departures.values():
                if not dep.fired and _key(dep.char_name) == wanted:
                    return dep
        return None

    def describe(self):
        now = self.mono()
        with self.lock:
            deps = list(self.departures.values())
        return [f'hop window: {d.char_name} (uid {d.uid}) left ch{d.channel} {now - d.t:.0f} s ago, '
                + ('logout sent' if d.fired else 'logout held')
                + (', relogin pending' if d.held_by is not None else '') for d in deps]


def register(hooks, continuity):
    """The departure hooks, registered right AFTER the messenger's so an immediate logout's
    S2C 0x60 keeps its place in the disconnect sequence (after the messenger's room leave)."""
    def on_leave_world(server, session, reason=None, **_):
        continuity.departed(session, reason or '')

    def on_disconnect(server, session, reason=None, **_):
        continuity.departed(session, reason or '')

    hooks.register(worldmod.ON_LEAVE_WORLD, on_leave_world)
    hooks.register(worldmod.ON_DISCONNECT, on_disconnect)
    return on_leave_world, on_disconnect
