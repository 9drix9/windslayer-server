#!/usr/bin/env python3
"""
channels.py - channel identity, listeners and channel operations (P12 ch-1, ch-5,
arch09-channel-key)
==================================================================================
Roadmap items (re_tools/docs/ROADMAP_2009_ADDENDUM.md P12; design
re_tools/docs/systems_2009/blacklist_channels.md Part B, "blch" below):

    ch-1   one game listener per channel in ONE process; session['channel'] is the listener
           the connection arrived on; S2C 0x01 lists each channel's own (ip, port), its own
           user count and leaves a closed / down channel out ("(inspection)"); the 0x03
           channel_id and the 0x99 sub 8 "In channel N." come from that listener.
    ch-5   0x02 result 6 when a channel is full (max_users), LOAD_SCALE, and the admin / GM
           open-close of a channel (an omitted entry = "(inspection)").
    arch09-channel-key   the keying rule below.

    chans = Channels(config)                 # GameServer.channels; VersionServer shares it
    chans.all()                              # [Channel] from config CHANNELS (read live)
    chans.listeners()                        # [(port, [Channel])]: one socket per port
    chans.resolve(port, local_ip)            # the channel of an accepted connection
    chans.advertised()                       # the S2C 0x01 entries: open AND listener up
    chans.set_open(3, False)                 # admin / `!channel close 3`
    chans.note_version_fetch(ip)             # VersionServer accept (continuity hint)

Why one process (blch B.8): C2S 0x01 carries no channel number, so the listening socket is
the ONLY channel identity (R1); the in-game channel change is a socket drop plus a relogin
with the same session key within ~2 s that must find the account (R3: World.claim with
replace=True sees every channel because there is one World); whisper / friends / guild /
blacklist are cross-channel by design (B.6); accounts.json has one writer.

The S2C 0x01 channel table (spec 0x01 / spec_2009 0x01, ParseChannelList FUN_00440a00 [V]):
    u8 slot_count (<= 10), u8 entry_count, then per entry u8 no, u16 users, u32 ip
    (host order) [2009: + u32 port], channel_no strictly ascending and <= slot_count.
A slot <= slot_count that has no entry is drawn "Channel - N (inspection)" with no load label
and cannot be selected (B.1 slot state 2): per-channel maintenance is free, and it is how a
closed channel AND a channel whose listener failed to bind are shown. R8: a channel whose
port refuses the connection leaves the player on an undismissable "Waiting for the server
to respond." box, so only channels whose listener is UP are ever advertised. slot_count is
the highest configured channel number (config: 1..10 ascending), so every configured channel
has its slot whether it is advertised or not.

Load label (B.2, FUN_00442080 0x4421DE..0x442276 [V]): user_count / 200 clamped at 2 ->
"Idle" green / "Normal" yellow / "Busy" red (B14 prints "Idle", not the later "Low").
user_count is display-only (read by nothing else, [V]), so it is the channel's own online
count x LOAD_SCALE: a small server can show Normal / Busy.

2008 (EN 2008 client, hard-coded game port 7022 at VA 0x44080E): every channel is on
GAME_PORT and needs its own IP (loopback aliases 127.0.0.x work); one listener on GAME_PORT
takes them all and resolve() picks the channel by the local address the connection arrived
on (the IP the client was told). The 0x01 entries carry no port (7-byte entries).

arch09-channel-key (P12 = the rule, P18 = the enforcement)
-----------------------------------------------------------
Every NEW per-map, per-room or per-ledger structure is keyed (channel, ...) even while only
one channel exists - key(channel, *parts) builds such a key - so P18 ch-2 (map instances
keyed (channel, map)) is mechanical. The boss ledger already is (bosses.make_key). Until
ch-2 every channel shares ONE world: the channel such a structure keys by is the map
instance's channel, which is world_channel() (the first configured channel) for every map
today - NOT the listener of the player who touched it, or two channels would book one shared
boss twice. Global by design (B.7, never keyed by channel): accounts and characters, the
identity indexes (one online session per account across all channels), whisper routing,
friends / presence, mentors, `/f`, memos, blacklists, guilds, GM tools, the continuity
departures (continuity.py: keyed by account uid - a hop crosses channels by definition).
"""
import logging
import threading
import time
from dataclasses import dataclass

import config as cfgmod
import gm

log = logging.getLogger('WS')

# blch B.2: the client's load label is min(user_count / 200, 2) into this table (0x54B214).
LOAD_LABELS = ('Idle', 'Normal', 'Busy')
LOAD_LABEL_STEP = 200
U16_MAX = 0xFFFF
# continuity hint (config CHANNEL_HOP_VERSION_HINT): a version fetch from the closing client's
# IP at most this long before its game socket closed is the Change Channel / Change Avatar
# teardown (FUN_00448730 case 0x1AE: step 2 ConnectToVersionServer, step 3 Close(g_sock_game),
# blch B.5 [V]). The steps are back to back; 5 s also covers the fade before step 2.
VERSION_HINT_SECS = 5.0
# Only fetches booked when the close is handled count: the hint is read once, at the close
# (continuity.Continuity._may_defer). An accept booked a few ms AFTER the game FIN was handled
# is missed - the documented limit in continuity.py (the hop then shows a logout/login pair);
# the former VERSION_HINT_AFTER_SECS window could never see such an accept and was removed.
# Version fetches remembered per IP are pruned after this long.
FETCH_KEEP_SECS = 60.0


@dataclass(frozen=True)
class Channel:
    """One configured channel (config CHANNELS entry, defaults resolved)."""
    no: int
    name: str
    ip: str
    port: int
    max_users: int
    configured_open: bool

    @property
    def label(self):
        return f'channel {self.no} ({self.ip}:{self.port})'


def key(channel, *parts):
    """arch09-channel-key: the (channel, ...) key of a per-map / per-room / per-ledger
    structure (module docstring)."""
    return (int(channel),) + tuple(parts)


def load_label(users):
    """The client's own label for an S2C 0x01 user_count (blch B.2)."""
    return LOAD_LABELS[min(max(0, int(users)) // LOAD_LABEL_STEP, len(LOAD_LABELS) - 1)]


class Channels:
    """The channel table of one server: config CHANNELS (read live, so a test or `!channel`
    sees a config change at once) plus the runtime state - the admin open / close overrides,
    whether each listener is up, and the version fetches the continuity hint reads."""

    def __init__(self, config, clock=time.monotonic):
        self.config = config
        self.clock = clock
        self.lock = threading.Lock()        # a leaf: never held while calling out
        self._open = {}                     # channel no -> bool (admin / GM override)
        self._up = {}                       # channel no -> bool (listener bound); absent = not started
        self._fetches = {}                  # ip -> [monotonic() of recent version fetches]

    # ------------------------------------------------------------- the table ---
    @property
    def build(self):
        return str(self.config.get('CLIENT_BUILD', cfgmod.BUILD_2008))

    def all(self):
        """Every configured channel, ascending. A 2008 channel's port is always GAME_PORT
        (the client hard-codes 7022, VA 0x44080E; config warns about a "port")."""
        cfg = self.config
        game_port = int(cfg['GAME_PORT'])
        out = []
        for c in cfg['CHANNELS']:
            no = int(c['no'])
            port = int(c.get('port') or game_port) if self.build == cfgmod.BUILD_2009 else game_port
            out.append(Channel(no=no, name=str(c.get('name') or f'Channel {no}'),
                               ip=str(c.get('ip') or cfg['PUBLIC_IP']), port=port,
                               max_users=int(c.get('max_users') or cfg['MAX_ONLINE']),
                               configured_open=bool(c.get('open', True))))
        return out

    def get(self, no):
        """The Channel numbered `no`, or None."""
        for ch in self.all():
            if ch.no == int(no):
                return ch
        return None

    def first(self):
        chans = self.all()
        return chans[0] if chans else None

    def world_channel(self):
        """The channel every map instance belongs to until P18 ch-2 (arch09-channel-key):
        the first configured one - all channels share that one world today."""
        first = self.first()
        return first.no if first is not None else 1

    def slot_count(self):
        """S2C 0x01 channel_slot_count: the highest configured channel number, so every
        configured channel has its slot (and an omitted one reads "(inspection)")."""
        chans = self.all()
        return max(ch.no for ch in chans) if chans else 0

    # --------------------------------------------------- open / up (ch-5, R8) ---
    def is_open(self, no):
        ch = self.get(no)
        if ch is None:
            return False
        with self.lock:
            return self._open.get(ch.no, ch.configured_open)

    def set_open(self, no, flag):
        """Open or close channel `no` (admin {"channel_open": [n, 0|1]} / `!channel`). A
        closed channel is left out of the next S2C 0x01 ("(inspection)") and refuses new
        logins; the players on it stay. Returns the Channel; KeyError for an unknown one."""
        ch = self.get(no)
        if ch is None:
            raise KeyError(f'no channel {no} (configured: {[c.no for c in self.all()]})')
        with self.lock:
            self._open[ch.no] = bool(flag)
        log.warning(f'[CHANNEL] {ch.label} {"opened" if flag else "CLOSED (inspection)"}')
        return ch

    def mark_up(self, nos, up):
        with self.lock:
            for no in nos:
                self._up[int(no)] = bool(up)

    def is_up(self, no):
        """False only for a channel whose listener failed to bind. Before the server binds
        (offline tests, a standalone VersionServer) every channel counts as up."""
        with self.lock:
            return self._up.get(int(no), True)

    def advertised(self):
        """The S2C 0x01 entries: open channels whose listener is up (R8), ascending."""
        return [ch for ch in self.all() if self.is_open(ch.no) and self.is_up(ch.no)]

    # -------------------------------------------------------- listeners (ch-1) ---
    def listeners(self):
        """[(port, [Channel])]: one listening socket per distinct port, in config order. 2009
        channels normally each have their own port; channels sharing a port (different IPs,
        and every 2008 channel) share one socket and resolve() tells them apart."""
        groups = {}
        for ch in self.all():
            groups.setdefault(ch.port, []).append(ch)
        return list(groups.items())

    def resolve(self, port, local_ip=None):
        """The channel number of a connection accepted on `port` whose local address is
        `local_ip`: the channel of that port with that IP, else the port's first channel.
        None when no configured channel uses the port (the config changed under a running
        server: the caller then falls back to the first channel)."""
        group = [ch for ch in self.all() if ch.port == int(port)]
        if not group:
            return None
        for ch in group:
            if local_ip and ch.ip == local_ip:
                return ch.no
        return group[0].no

    # ------------------------------------------------------ users (ch-1 / ch-5) ---
    def counts(self, sessions, channel_of):
        """{channel no: logged-in sessions on it} over `sessions` (one per account: the
        world's by_uid), each placed by channel_of(session)."""
        out = {ch.no: 0 for ch in self.all()}
        for s in sessions:
            no = channel_of(s)
            out[no] = out.get(no, 0) + 1
        return out

    def displayed_users(self, online):
        """S2C 0x01 user_count: online x LOAD_SCALE, clamped to the u16 (blch C CH-5)."""
        scale = float(self.config.get('LOAD_SCALE', 1.0))
        return max(0, min(U16_MAX, int(int(online) * scale)))

    def is_full(self, no, online):
        """ch-5: the channel holds `online` other accounts already and takes no more (S2C
        0x02 result 6 "The server is full.\\r\\nPlease try another server.")."""
        ch = self.get(no)
        return ch is not None and int(online) >= ch.max_users

    # ------------------------------------------------- continuity hint (ch-4) ---
    def note_version_fetch(self, ip, t=None):
        """The version server accepted a connection from `ip` (VersionServer.start)."""
        t = self.clock() if t is None else t
        with self.lock:
            seen = [f for f in self._fetches.get(ip, []) if t - f < FETCH_KEEP_SECS]
            seen.append(t)
            self._fetches[ip] = seen[-8:]
            for other in [k for k, v in self._fetches.items() if v and t - v[-1] >= FETCH_KEEP_SECS]:
                del self._fetches[other]

    def version_fetched(self, ip, t, before=VERSION_HINT_SECS):
        """A version fetch from `ip` was booked within [t - before, t] (t = the close)."""
        if not ip:
            return False
        with self.lock:
            return any(t - before <= f <= t for f in self._fetches.get(ip, ()))

    # ------------------------------------------------------------- reporting ---
    def describe(self, counts=None):
        """One line per channel for `!channel` / the admin port."""
        counts = counts or {}
        lines = []
        for ch in self.all():
            online = int(counts.get(ch.no, 0))
            shown = self.displayed_users(online)
            state = ('open' if self.is_open(ch.no) else 'CLOSED') + ('' if self.is_up(ch.no) else ' DOWN')
            lines.append(f'ch{ch.no} {ch.name} {ch.ip}:{ch.port} {state}, {online}/{ch.max_users} online, '
                         f'shows {shown} ({load_label(shown) if ch in self.advertised() else "(inspection)"})')
        return lines


# The '!' command (gm.register: no edit of GameServer.DEV_COMMANDS); its handler is the thin
# GameServer._dev_channel. The admin port has the same switch: {"channel_open": [n, 0|1]}.
if 'channel' not in gm.COMMANDS:
    gm.register('channel', gm.DevCommand(
        '_dev_channel', '!channel [list] | open <n> | close <n> | scale <x>',
        'the channels: listener, open / closed / down, users and the load label the version list '
        'shows; close = "(inspection)" on the next version fetch and new logins refused (the players '
        'on it stay); scale sets LOAD_SCALE until a restart',
        owner='ch-1 / ch-5 (P12 stage 2)', aliases=('channels', 'ch')))
