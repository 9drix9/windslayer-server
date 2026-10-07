#!/usr/bin/env python3
"""
boards.py - the 2009 Guild Plaza advertisement boards (P15 guild-g6; re_tools/docs/systems_2009/
guild.md 1.6, 3, F0 step 3, F13; ROADMAP_2009_ADDENDUM P15 + 3.0 arch09-channel-key / X7 / X8;
CLIENT_PATCH_SET_RE_2026-10-06 8.5)
==========================================================================================
    gs.boards = Boards(gs)                       # GameServer.__init__, after gs.guilds
    install(gs.resync, gs.boards)                # resync step 'guild_boards' (0xBB in 9702)
    gs.boards.place(sock, session, rec)          # C2S 0x88 -> 0xBA to 9702 + 0xB3 sub 185
    gs.boards.echo(sock, session, item)          # C2S 0x15 {board} after sub 185 -> S2C 0x25
    gs.boards.remove_guild(gid, why)             # a disband: 0xB8 {gid} to 9702
    gs.boards.tick(now)                          # 'guild-boards' (GameServer.start): expiry -> 0xB8
    gs.boards.dev(session, words)                # `!guild board ...`

The client (EN 2009 Build 14 only; the 2008 client has no 0xB8 / 0xBA / 0xBB handler and no
guild, so `supported` is False there, no file is opened and nothing is ever sent):
- Boards draw only on map 9702 "Guild Plaza" (FUN_004330c0, map code 0x25E6) [V]. Their list
  lives on CMessenger M+0x114 and EVERY S2C 0x03 empties it (FUN_0047a980, called from the
  0x03 handler) [V guild 1.1] - so a player entering 9702 needs an S2C 0xBB after that map
  load (guild F0 step 3, F13 step 7): resync step 'guild_boards' sends it right after the 0x8A
  reply (sub 3 / 15 + sub 4) and before 'gm_tag' (arch09-resync-bundle). The 0xBB handler
  calls FUN_0047a980 itself before it adds (0x45DC03) [V], so an 0xBB REPLACES the list: an
  0xBA that reached a client in mid map load is not doubled by its 0xBB.
- S2C 0xBA (87 B) appends one record with NO duplicate check (spec 0xBA hazards: the same
  board twice draws twice); 0xB8 {u16 guild_id} removes every board of that guild (there is no
  per-board id on the wire). Hence ONE live board per guild and channel, and every [0xBB build
  + send] and [add + 0xBA fan-out] runs under Boards.lock: a board is either in a client's
  0xBB or comes as an 0xBA after it, never both.
- The board item field (board+0x42) picks the sprite: the exe compares it with its two
  hard-coded ids (0x45DBC3 / 0x45DD3B 0x145, 0x45DBD2 / 0x45DD4A 0x146) - KR 4280 / 4279 on the
  stock exe, EN 4284 / 4283 on the cp-2 exe (config CLIENT_ITEM_IDS; guild.board_items). A
  board is stored by KIND ('board' / 'premium') and gets its exe id at send time, so the
  config can follow a client swap without touching the file.
- master_char_id must be the placing master's uid: the client's "You can't make double
  billboard." gate compares board+0 with its own scene+0x224 (FUN_00484d70) [V].

Placement (C2S 0x88, guild F13; the cp-2 exe only - CLIENT_ITEM_IDS 'en')
------------------------------------------------------------------------
The cp-2 exe opens dialog 0x4B7 when EN 4284 "Guild Billboard" (a Type-0 bag item) is used in
9702 (FUN_0044f070 0x44FCB8; anywhere else "Guild billboard can only be open in guild
plaza." and nothing is sent) or EN 4283 "Premium Guild Billboard" (a cash-bag record) is used
there (FUN_0046d6f0 switch slot 0); its OK sends C2S 0x88 (87 B, the 0xBA field order) after
its own gates (non-empty text + CheckString, master by name, no own board, >= sqrt(25000) px
from every other board, an item selected: FUN_00480430 case 0x4B7 / FUN_00484d70). The server
re-checks in order: 'en' exe, in world on 9702, the MASTER (grade 5) of a guild - else 0xB3
sub 185 {0} "Guild master can only do this." -, a board id of this exe, a non-empty text, one
board per guild ("You can't make double billboard."), >= 158 px from the others ("You are too
close from other billboard.") at the position the client sent (it is where the client draws
it), and owns the item. Then the item is taken (server side), the board stored with expires =
now + GUILD_BOARD_MINUTES (60) or GUILD_PREMIUM_BOARD_MINUTES (1440) [I: the KR item text
"usable for 1 hour / 24 hours"], 0xBA goes to everyone on 9702 (the master too: the 0x88
draws nothing locally) and the master gets sub 185 {1, the exe's id of that item}.
- Guild Billboard (bag): taken from the bag model at the 0x88. Sub 185 {1, 4284} makes the
  client send C2S 0x15 {4284} (0x47E846: it re-reads the packet's own field, CLIENT_PATCH_SET_RE
  8.3); that echo is answered S2C 0x25 {4284} - the client's own consume of one bag unit -
  WITHOUT a second server-side removal (session BOARD_ECHO_KEY, BOARD_ECHO_SECS). A client
  that never echoes is rebuilt from the bag model at its next login: one fewer, as stored.
- Premium (cash record): consumed at the 0x88 and told with the 0x72 owner form {uid, 4283,
  serial} - the client's consume by serial (FUN_0046df70; the megaphone's proven path), since
  sub 185 {1, 4283} makes the client do nothing (flag 1, another item).
A refusal after the item was taken (the commit re-check: a race with a disband or another
board) puts it back, server side only: the client never removed it.
Refusals are 0x15 lines in the client's own words (the 0x88 opens no waiting box: the dialog
closes before the Send) and sub 185 {0} for a non-master. The stock exe ('kr') never reaches
0x88 (its 4279 / 4280 are EN bows), so a 0x88 there is refused and boards are GM-seeded
(G-CP default: "Plaza boards are GM-seeded only").

Moiba's sale (C2S 0x0B from the guild menu 0x4BB control 5, send site 0x474327) is
GameServer._buy_guild_board: on 'en' it sells the Guild Billboard EN 4284 (the item KR's
Moiba sells as 4280 [V guild 1.6]; the EN hni lists 4283, the premium cash board, which the
client's own quantity dialog prices at its hii Buy 0 - deliberate deviation [I], see there).
That send site's npc_id is always 0 on the wire (the dialog's close zeroes +0x11C before the
OK handler reads it; p15 live triage 1): GameServer._board_sale_npc names Moiba from the map
(9702) and the item (a board id).

Joining from a board: a click opens the join dialog 0x4B6 mode 0 and its OK sends C2S 0x89
{u16 guild_id} (0x482DCD) - guild.Guilds.apply, unchanged (it tells the two forms by length).

Expiry / disband: 'guild-boards' (every SCAN_SECS) drops boards past expires_at and sends 0xB8
{gid} to 9702; a disband (guild F6, Moiba's "Break Guild" or `!guild disband`) drops the
guild's board with the same 0xB8 (guild.Guilds._disband sent it before g6 already).

Persistence (arch09-channel-key): GUILD_BOARDS_FILE (default guild_boards.json next to
accounts.json; '' = memory only), one row per live board keyed (channel, map, guild_id):
    {"version": 1, "boards": [{"channel": 1, "map": 9702, "guild_id": 2, "kind": "board",
      "master_uid": 1, "master_name": "TestHero", "guild_name": "Testers", "emblem_fg": 18,
      "emblem_bg": 18, "ad_text": "Join us", "pos_x": 2200.0, "pos_y": 1300.0,
      "placed_at": 1790000000.0, "expires_at": 1790003600.0, "placed_by": 4}]}
UTC epoch times, so a board keeps its remaining time across a restart; a load drops expired
rows and rows of guilds that no longer exist. The channel is the map instance's
(channels.world_channel() until P18 ch-2 keys map instances by channel: every channel shares
the one world today), exactly as the boss ledger (bosses.Bosses.channel_of). Writes follow the
boss ledger / guilds.json rules (atomic, from the tick thread, one writer, an older snapshot
never replaces a newer one, a failed write stays dirty and retries). accounts.json is never
touched (no schema change there). A file that cannot be read stops the server start
(BoardStoreError) instead of being replaced; a malformed one is moved to <file>.bad-<time>.

Locks: Guilds.flow_lock -> Boards.lock -> {Guilds.lock, BoardStore.lock (db.lock), World.lock}
(each a leaf: taken and let go with nothing else taken under it) -> send_lock. Boards.wire()
reads the guild's current name / emblem through guilds.get(), so Guilds.lock IS taken under
Boards.lock (send_list, _place, seed): no Boards method may ever be called while Guilds.lock is
held (guild.py Guilds._drop_boards runs outside it and says so). Boards.lock never takes a
combat, store or world_lock: the item is taken under the session's combat lock
(bag) or store.lock (cash record) BEFORE flow_lock (guild.py "Locks": no combat lock under
it), and given back after it on a failed commit. Nothing writes a socket under Boards.lock:
every send queues (flush=False) and Boards._held flushes the queued sessions once its outermost
hold is let go (through Guilds._flush, which defers further while a Guilds._flow is open). The tick runs under world_lock (ticks.py): world_lock ->
Boards.lock, no inversion (nothing under Boards.lock takes world_lock).
"""
import contextlib
import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass

import cash as CASH
import clientview as cview
import config as cfgmod
import en_content as EC
import gm
import guild as G
import packets as P
import store as storemod

log = logging.getLogger('WS')

PLAZA = G.GUILD_PLAZA_MAP                       # 9702: FUN_004330c0 draws boards only here
MOIBA_TILE = (2200.0, 1300.0)                   # Moiba's tile on stage97_02 (guild.md 1.5)
KIND_BOARD, KIND_PREMIUM = 'board', 'premium'
KINDS = (KIND_BOARD, KIND_PREMIUM)              # the index into guild.board_items() / BOARD_ITEMS_EN
EN_ITEM = dict(zip(KINDS, G.BOARD_ITEMS_EN))    # 4284 Guild Billboard (bag), 4283 Premium (cash)
MIN_DIST_SQ = 25000.0                           # FUN_00484d70: another board within sqrt(25000) px
AD_TEXT_MAX = 24                                # str[25] (0x4B7 edit; NUL-terminated)
# One S2C 0xBB frame holds u8 n + n x 87 B of the 2038-byte payload (guild.board_list): more
# live boards than that could never reach a client entering 9702, so the plaza holds no more.
MAX_BOARDS = (2038 - 1) // 87                   # 23
STORE_VERSION = 1
SCAN_SECS = 5.0                                 # expiry resolution of the 'guild-boards' tick
RETRY_SECS = 1.0
POS_LIMIT = 1.0e6                               # a finite world coordinate (maps are < 20000 px)
# The C2S 0x15 echo of sub 185 {1, board}: the session's pending one, answered with 0x25 and no
# second removal (module docstring). The client sends it at once; a stale one is dropped.
BOARD_ECHO_KEY = 'guild_board_echo'
BOARD_ECHO_SECS = 30.0
# The UTC wall clock a new Boards starts with (expires_at, the load's expiry): a module seam so
# an offline test can load a store at a fake time (test_guild_boards); time.time otherwise.
CLOCK = time.time

# The client's own words (EN 2009 strings, guild.md 3 / FUN_00484d70 / FUN_0044f070).
TEXT_DOUBLE = "You can't make double billboard."
TEXT_TOO_CLOSE = 'You are too close from other billboard. Please, move to other location.'
TEXT_PLAZA_ONLY = 'Guild billboard can only be open in guild plaza.'
TEXT_NO_ITEM = "You don't have that item."
TEXT_UNAVAILABLE = 'Guild billboards are not available.'
TEXT_EMPTY = 'Please enter the advertisement.'
TEXT_FULL = 'There is no more room for a billboard in the guild plaza.'   # server text (MAX_BOARDS)


def supported(client_build):
    return G.supported(client_build)


def _int(value, default=0):
    return G._int(value, default)


def _num(value, default=0.0):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def kind_of_exe(item, client_item_ids='en'):
    """'board' / 'premium' for one of the installed exe's board ids, else None."""
    ids = G.board_items(client_item_ids)
    item = _int(item)
    return KINDS[ids.index(item)] if item in ids else None


def exe_item(kind, client_item_ids='en'):
    """The board item id the installed exe draws `kind` with (0xBA / 0xBB board+0x42, sub 185)."""
    return G.board_items(client_item_ids)[KINDS.index(kind)]


# ==================================================================== a board ===
@dataclass
class Board:
    channel: int
    map_code: int
    guild_id: int
    kind: str
    master_uid: int
    master_name: str
    guild_name: str
    emblem_fg: int
    emblem_bg: int
    ad_text: str
    pos_x: float
    pos_y: float
    placed_at: float
    expires_at: float
    placed_by: int = 0                  # the placing character's cid (0 = a GM seed)

    @property
    def key(self):
        """arch09-channel-key: (channel, map, guild) - one board per guild and channel (0xB8)."""
        return (self.channel, self.map_code, self.guild_id)

    def left(self, now):
        return max(0.0, float(self.expires_at) - float(now))

    def to_json(self):
        return {'channel': self.channel, 'map': self.map_code, 'guild_id': self.guild_id, 'kind': self.kind,
                'master_uid': self.master_uid, 'master_name': self.master_name, 'guild_name': self.guild_name,
                'emblem_fg': self.emblem_fg, 'emblem_bg': self.emblem_bg, 'ad_text': self.ad_text,
                'pos_x': round(float(self.pos_x), 3), 'pos_y': round(float(self.pos_y), 3),
                'placed_at': round(float(self.placed_at), 3), 'expires_at': round(float(self.expires_at), 3),
                'placed_by': self.placed_by}

    @classmethod
    def from_json(cls, row):
        if not isinstance(row, dict):
            raise ValueError('not an object')
        kind = str(row.get('kind', KIND_BOARD))
        if kind not in KINDS:
            raise ValueError(f'kind {kind!r} is not one of {KINDS}')
        gid = int(row['guild_id'])
        if not G.GUILD_ID_MIN <= gid <= G.GUILD_ID_MAX:
            raise ValueError(f'guild id {gid} outside {G.GUILD_ID_MIN}..{G.GUILD_ID_MAX}')
        fg, bg = int(row.get('emblem_fg', 0)), int(row.get('emblem_bg', 0))
        if not G.emblem_ok(fg, bg):
            raise ValueError(f'emblem {fg}/{bg} has no sprite frame')
        x, y = float(row['pos_x']), float(row['pos_y'])
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError('position is not finite')
        return cls(channel=int(row['channel']), map_code=int(row.get('map', PLAZA)), guild_id=gid, kind=kind,
                   master_uid=int(row.get('master_uid', 0)) & 0xFFFFFFFF,
                   master_name=G.text(row.get('master_name', ''), G.NAME_MAX),
                   guild_name=G.text(row.get('guild_name', ''), G.NAME_MAX), emblem_fg=fg, emblem_bg=bg,
                   ad_text=G.text(row.get('ad_text', ''), AD_TEXT_MAX), pos_x=x, pos_y=y,
                   placed_at=float(row.get('placed_at', 0.0)), expires_at=float(row['expires_at']),
                   placed_by=int(row.get('placed_by', 0)))


# ===================================================================== store ===
class BoardStoreError(storemod.StoreError):
    """The boards file exists but cannot be read or moved aside: the server refuses to start
    rather than replace it (bosses.LedgerError's rule)."""


class BoardStore:
    """guild_boards.json: the live boards by key. `lock` is a leaf for the dict (Boards.lock is
    the flow lock above it); the file write happens outside both (bosses.BossLedger)."""

    def __init__(self, path):
        self.path = path or ''
        self.lock = threading.Lock()
        self.boards = {}
        self.dirty = False
        self.saves = 0
        self._changes = 0
        self._gen = 0
        self._disk_gen = 0
        self._save_mutex = threading.Lock()

    def load(self, now, guild_ids=None):
        """Read the file (missing = no boards). Rows that do not parse, expired rows (`now`) and
        rows of guilds not in `guild_ids` (None = keep all) are dropped and the file marked
        dirty, so the next save rewrites it without them. Returns the number kept."""
        if not self.path or not os.path.exists(self.path):
            return 0
        try:
            with open(self.path, 'rb') as f:
                raw = f.read()
        except OSError as e:
            raise BoardStoreError(f'{self.path}: cannot be read ({e}); fix it or point GUILD_BOARDS_FILE '
                                  f'elsewhere - the server will not replace it') from e
        try:
            data = json.loads(raw.decode('utf-8'))
            rows = data.get('boards') if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise ValueError('no "boards" list')
        except ValueError as e:
            bad = f'{self.path}.bad-{int(time.time())}'
            try:
                os.replace(self.path, bad)
            except OSError as moved:
                raise BoardStoreError(f'{self.path}: malformed ({e}) and cannot be moved aside ({moved})') from e
            log.error(f'[BOARD] {self.path} is malformed ({e}); moved to {bad}, no boards')
            return 0
        boards, dropped = {}, []
        for row in rows:
            try:
                b = Board.from_json(row)
            except (KeyError, TypeError, ValueError) as e:
                dropped.append(f'{row!r}: {e}')
                continue
            if b.expires_at <= now:
                dropped.append(f'{b.guild_name!r} ({b.guild_id}) expired while the server was down')
            elif guild_ids is not None and b.guild_id not in guild_ids:
                dropped.append(f'{b.guild_name!r} ({b.guild_id}): no such guild any more')
            elif b.key in boards:
                dropped.append(f'{b.guild_name!r} ({b.guild_id}): a second board of its key {b.key}')
            else:
                boards[b.key] = b
        with self.lock:
            self.boards = boards
            if dropped:
                self._mark()
        for why in dropped:
            log.info(f'[BOARD] load: dropped {why}')
        log.info(f'[BOARD] {len(boards)} live board(s) from {self.path}')
        return len(boards)

    def _mark(self):
        """Caller holds self.lock."""
        self.dirty = True
        self._changes += 1

    def snapshot(self):
        with self.lock:
            return {'version': STORE_VERSION,
                    'boards': [b.to_json() for b in sorted(self.boards.values(), key=lambda b: b.key)]}

    def flush(self, delays=None, wait=None):
        """Write the file if it changed (bosses.BossLedger.flush's rules). True when this snapshot
        or a newer one is on disk; raises OSError (kept dirty) when the write failed."""
        if not self.path:
            with self.lock:
                self.dirty = False
            return False
        with self.lock:
            if not self.dirty:
                return False
            self._gen += 1
            gen, covers = self._gen, self._changes
            snap = {'version': STORE_VERSION,
                    'boards': [b.to_json() for b in sorted(self.boards.values(), key=lambda b: b.key)]}
        data = json.dumps(snap, indent=1).encode('utf-8')
        wrote = False
        if wait is None:
            self._save_mutex.acquire()
        elif not self._save_mutex.acquire(timeout=max(0.0, float(wait))):
            raise TimeoutError(f'another save is writing {self.path}')
        try:
            if gen > self._disk_gen:
                storemod.atomic_write(self.path, data, delays)
                self._disk_gen = gen
                wrote = True
        finally:
            self._save_mutex.release()
        with self.lock:
            if wrote:
                self.saves += 1
            if self._changes == covers:
                self.dirty = False
        return True


def boards_path(cfg, accounts_path):
    """GUILD_BOARDS_FILE next to the accounts file ('' = memory only); tests run on a temp one."""
    name = str(cfg.get('GUILD_BOARDS_FILE', cfgmod.DEFAULTS['GUILD_BOARDS_FILE']) or '')
    if not name:
        return ''
    if os.path.isabs(name):
        return name
    return os.path.join(os.path.dirname(os.path.abspath(accounts_path)), name)


class Refused(Exception):
    """A placement refusal: (log reason, 0x15 text or None, sub 185 {0} instead of a line)."""

    def __init__(self, why, text=None, master_only=False):
        super().__init__(why)
        self.why, self.text, self.master_only = why, text, master_only


# =================================================================== runtime ===
class Boards:
    """The Guild Plaza boards of one GameServer (server.boards)."""

    def __init__(self, server, clock=None):
        self.server = server
        self.supported = supported(getattr(server, 'client_build', None))
        self.clock = clock if clock is not None else CLOCK   # UTC: expires_at survives a restart
        self.db = BoardStore(boards_path(server.config, server.store.path) if self.supported else '')
        # Serializes [0xBB build + send] with [add / remove + 0xBA / 0xB8 fan-out] (module
        # docstring): each client sees each board exactly once. Re-entrant (a GM replace).
        self.lock = threading.RLock()
        self._tls = threading.local()               # _held(): depth + the sessions to flush after it
        self._plock = threading.Lock()
        self._pending = None
        self._failures = 0
        if self.supported:
            guilds = getattr(server, 'guilds', None)
            ids = {g.id for g in guilds.all()} if guilds is not None else None
            self.db.load(self.clock(), ids)
            if self.db.dirty:
                self.request_save()

    # ------------------------------------------------------------- config ---
    def client_item_ids(self):
        return self.server.config.get('CLIENT_ITEM_IDS', cfgmod.DEFAULTS['CLIENT_ITEM_IDS'])

    def placement_open(self):
        """The cp-2 exe ('en') reaches C2S 0x88 and uses the EN ids; the stock exe never does."""
        return self.supported and self.client_item_ids() == 'en'

    def minutes(self, kind):
        key_ = 'GUILD_PREMIUM_BOARD_MINUTES' if kind == KIND_PREMIUM else 'GUILD_BOARD_MINUTES'
        return float(self.server.config.get(key_, cfgmod.DEFAULTS[key_]))

    def channel_of(self, session=None):
        """arch09-channel-key: the channel of the plaza's map instance - its own once P18 ch-2
        keys map instances by channel, today the one every channel's players share
        (channels.world_channel, as bosses.Bosses.channel_of) - never the listener a session
        arrived on, or one shared plaza would show each board on one channel only."""
        chans = getattr(self.server, 'channels', None)
        if chans is not None:
            return int(chans.world_channel())
        channels = self.server.config.channels()
        return int(channels[0][0]) if channels else 1

    # ------------------------------------------------------------ queries ---
    def live(self, channel=None, now=None):
        """The live boards of `channel` (default: the plaza's) on 9702, oldest first."""
        now = self.clock() if now is None else now
        channel = self.channel_of() if channel is None else channel
        with self.db.lock:
            rows = [b for b in self.db.boards.values()
                    if b.channel == channel and b.map_code == PLAZA and b.expires_at > now]
        return sorted(rows, key=lambda b: (b.placed_at, b.guild_id))

    def of_guild(self, gid, channel=None):
        with self.db.lock:
            return self.db.boards.get((self.channel_of() if channel is None else channel, PLAZA, _int(gid)))

    def wire(self, b):
        """The builders' field dict of board `b` for the installed exe: the board item is the
        exe's id of its kind; the guild name / emblem the guild's current ones (a stored board
        of a renamed / re-emblemed guild never shows stale ones)."""
        g = self.server.guilds.get(b.guild_id) if getattr(self.server, 'guilds', None) is not None else None
        name, fg, bg = (g.name, g.emblem_fg, g.emblem_bg) if g is not None else (b.guild_name, b.emblem_fg,
                                                                               b.emblem_bg)
        return {'master_char_id': b.master_uid, 'master_name': b.master_name,
                'board_item_id': exe_item(b.kind, self.client_item_ids()), 'guild_id': b.guild_id,
                'guild_name': name, 'emblem_symbol': fg, 'emblem_bg': bg, 'ad_text': b.ad_text,
                'pos_x': float(b.pos_x), 'pos_y': float(b.pos_y)}

    # -------------------------------------------------------------- sends ---
    def _flush(self, session):
        """Guilds._flush: the requester's own connection is written once flow_lock is let go
        (inside a Guilds._flow) or at once; another session's writer thread was woken."""
        self.server.guilds._flush(session)

    @contextlib.contextmanager
    def _held(self):
        """self.lock with every flush deferred to its outermost release (guild.Guilds._flow's
        rule: no socket is written while Boards.lock is held - a stalled requester must not hold
        the plaza). Re-entrant, like the lock."""
        tl, pending = self._tls, None
        try:
            with self.lock:
                depth = getattr(tl, 'depth', 0)
                if depth == 0:
                    tl.pending = []
                tl.depth = depth + 1
                try:
                    yield
                finally:
                    tl.depth = depth
                    if depth == 0:
                        pending, tl.pending = tl.pending, None
        finally:                                    # what was queued goes out, even on a raise
            for s in pending or ():
                try:
                    self._flush(s)
                except OSError as e:                # that connection just closed
                    log.debug(f'[BOARD] deferred flush to {s.get("char_name")!r} failed: {e}')

    def _queued(self, session):
        """Inside _held(): `session` is flushed at its release."""
        tl = self._tls
        if getattr(tl, 'depth', 0):
            if all(s is not session for s in tl.pending):
                tl.pending.append(session)
        else:
            self._flush(session)

    def _fan(self, key_, fields, why):
        """Inside _held(): one packet to every session on 9702 (queued). Returns the names."""
        told = []
        for s in self.server.world.map_sessions(PLAZA):
            if not cview.view(s).had_03:
                continue
            if self.server._push(s, key_, fields, tag='BOARD', flush=False):
                told.append(s.get('char_name'))
                self._queued(s)
        log.info(f'[BOARD] {why}: {key_} to {len(told)} client(s) on {PLAZA} {told}')
        return told

    def send_list(self, sock, session):
        """resync step 'guild_boards' (guild F0 step 3 / F13 step 7): S2C 0xBB with the live
        boards to a client whose map load is 9702 - the 0x03 emptied its list. Nothing on
        another map, nothing with no board (the 0x03 left the list empty already)."""
        if not self.supported or sock is None or _int(session.get('current_map')) != PLAZA:
            return 0
        ids = self.client_item_ids()
        with self._held():
            rows = self.live(self.channel_of(session))
            if not rows:
                return 0
            if len(rows) > MAX_BOARDS:              # a hand-edited file: what one frame holds
                log.warning(f'[BOARD] {len(rows)} live boards; the 0xBB carries the first {MAX_BOARDS}')
                rows = rows[:MAX_BOARDS]
            key_, fields = G.board_list([self.wire(b) for b in rows], ids)
            P.send(self.server, sock, session, key_, fields, flush=False)
            self._queued(session)
        log.info(f'[BOARD] 0x8A reply to {session.get("char_name")!r} on {PLAZA}: 0xBB with {len(rows)} board(s) '
                 f'{[b.guild_name for b in rows]}')
        return len(rows)

    # --------------------------------------------------------- placement ---
    def place(self, sock, session, rec):
        """C2S 0x88 GuildAdBoardPlace (87 B; module docstring "Placement"). Returns the Board
        placed, or None (refused: logged, told)."""
        who = session.get('char_name') or session.get('username')
        try:
            return self._place(sock, session, rec)
        except Refused as r:
            log.info(f'[BOARD] 0x88 from {who!r} refused: {r.why}')
            try:
                if r.master_only:
                    self.server.guilds._reply(sock, session, G.sub185(0))
                elif r.text:
                    self.server._notice(sock, session, r.text, 'warn')
            except OSError as e:
                log.debug(f'[BOARD] refusal to {who!r} not sent: {e}')
            return None

    def _place(self, sock, session, rec):
        server, guilds = self.server, self.server.guilds
        ids = self.client_item_ids()
        item = _int(rec.get('board_item_id'))
        if not self.supported:
            raise Refused('not a 2009 server')
        if ids != 'en':
            raise Refused(f"CLIENT_ITEM_IDS {ids!r}: the stock exe has no board item (its {G.board_items('kr')} "
                          f"are EN bows) - boards are GM-seeded (`!guild board place`)", TEXT_UNAVAILABLE)
        if not session.get('in_world') or session.get('in_cash_shop'):
            raise Refused('not in the world')
        if _int(session.get('current_map')) != PLAZA:
            raise Refused(f'on map {session.get("current_map")}, not {PLAZA}', TEXT_PLAZA_ONLY)
        char, g, me = guilds._me(session)
        if g is None or me is None or me.get('grade') != G.GRADE_MASTER:
            raise Refused(f'not a guild master ({g.name if g else "no guild"})', master_only=True)
        if _int(rec.get('guild_id')) != g.id:
            log.warning(f'[BOARD] 0x88 from {char.get("name")!r}: guild_id {rec.get("guild_id")} is not its guild '
                        f'{g.id} (the server decides)')
        kind = kind_of_exe(item, ids)
        if kind is None:
            raise Refused(f'item {item} is no board id of this exe {G.board_items(ids)}', TEXT_NO_ITEM)
        ad = G.text(rec.get('ad_text'), AD_TEXT_MAX).strip()
        if not ad or any(ord(ch) < 0x20 for ch in ad):
            raise Refused(f'advertisement text {ad!r}: empty or control bytes', TEXT_EMPTY)
        x, y = _num(rec.get('pos_x'), math.nan), _num(rec.get('pos_y'), math.nan)
        if not (math.isfinite(x) and math.isfinite(y)) or abs(x) > POS_LIMIT or abs(y) > POS_LIMIT:
            raise Refused(f'position ({rec.get("pos_x")}, {rec.get("pos_y")}) is not a world point')
        channel = self.channel_of(session)
        self._clash(g.id, channel, x, y)                    # early: nothing taken for a sure refusal
        taken = self._take(session, char, kind)             # under its own lock, never flow_lock
        uid = P.session_uid(session) or 0
        now = self.clock()
        board = Board(channel=channel, map_code=PLAZA, guild_id=g.id, kind=kind, master_uid=uid,
                      master_name=G.text(char.get('name'), G.NAME_MAX), guild_name=g.name, emblem_fg=g.emblem_fg,
                      emblem_bg=g.emblem_bg, ad_text=ad, pos_x=x, pos_y=y, placed_at=now,
                      expires_at=now + self.minutes(kind) * 60.0, placed_by=_int(char.get('cid')))
        committed = False
        try:
            with guilds._flow():
                live = guilds.get(g.id)
                mine = live.member(char.get('cid')) if live is not None else None
                if mine is None or mine['grade'] != G.GRADE_MASTER:
                    raise Refused(f'no longer the master of {g.name!r} at the commit', master_only=True)
                with self._held():
                    self._clash(g.id, channel, x, y, now)
                    with self.db.lock:
                        # _clash passed, so a row under this key is the guild's EXPIRED board
                        # that the tick (every SCAN_SECS) has not dropped yet
                        stale = self.db.boards.get(board.key)
                        self.db.boards[board.key] = board
                        self.db._mark()
                    committed = True        # from here the item paid for a stored board
                    if stale is not None:
                        # the tick never sees it once the new board holds its key: its 0xB8 now,
                        # before the 0xBA (0xBA has no duplicate check: both would draw)
                        self._fan(*G.board_remove(g.id), f'expired, replaced before its tick: the {stale.guild_name!r} '
                                                         f'{stale.kind} "{stale.ad_text}" (ch{stale.channel})')
                    self._fan(*G.board_add(self.wire(board), ids), f'{char.get("name")!r} placed a {kind} for '
                                                                    f'{g.name!r} at ({x:g}, {y:g})')
                    if kind == KIND_PREMIUM:
                        # the client's consume by serial (sub 185 {1, 4283} does nothing client-side);
                        # Cash_T 1: no expiry bytes (spec 0x72 grammar)
                        P.send(server, sock, session, '0x72', {'player_uid': uid, 'item_id': exe_item(kind, ids),
                                                              'item_serial': taken['serial']}, flush=False)
                        self._queued(session)
                    else:
                        # set before the sub 185 goes out: its C2S 0x15 echo may come at once
                        session[BOARD_ECHO_KEY] = (exe_item(kind, ids), time.monotonic())
                guilds._reply(sock, session, G.sub185(1, exe_item(kind, ids), ids))
        except BaseException:                       # Refused at the commit, or the connection closed
            if not committed:
                self._give_back(session, char, kind, taken)
            else:
                self.request_save()
            raise
        self.request_save()
        log.info(f'[BOARD] {char.get("name")!r} ({uid}) placed the {g.name!r} {kind} "{ad}" at ({x:g}, {y:g}) '
                 f'ch{channel}: expires in {self.minutes(kind):g} min; sub 185 {{1, {exe_item(kind, ids)}}}'
                 f'{"; 0x72 consumed the cash record" if kind == KIND_PREMIUM else "; the 0x15 echo gets 0x25"}')
        return board

    def _clash(self, gid, channel, x, y, now=None):
        """Raise Refused for a second board of the guild or one within sqrt(MIN_DIST_SQ) px of
        another live board on the channel (the client's own FUN_00484d70 gates)."""
        rows = self.live(channel, now)
        if len(rows) >= MAX_BOARDS and all(b.guild_id != gid for b in rows):
            raise Refused(f'{len(rows)} live boards: one 0xBB frame holds {MAX_BOARDS}', TEXT_FULL)
        for b in rows:
            if b.guild_id == gid:
                raise Refused(f'guild {gid} has a live board already ({b.ad_text!r})', TEXT_DOUBLE)
            if (b.pos_x - x) ** 2 + (b.pos_y - y) ** 2 < MIN_DIST_SQ:
                raise Refused(f'within {math.sqrt(MIN_DIST_SQ):.0f} px of the {b.guild_name!r} board at '
                              f'({b.pos_x:g}, {b.pos_y:g})', TEXT_TOO_CLOSE)

    def _take(self, session, char, kind):
        """Take one board item (server side). Returns what _give_back needs. Raises Refused."""
        server = self.server
        en_id = EN_ITEM[kind]
        if kind == KIND_BOARD:
            with server._combat_lock(session):
                bag = server._bag(session)
                if bag is None or not bag.has(en_id, 1):
                    raise Refused(f'no {EC.item_name(en_id)} ({en_id}) in the bag', TEXT_NO_ITEM)
                escrow = server._escrow_refusal(session, en_id, 1)
                if escrow is not None:
                    raise Refused(escrow[0], escrow[1])
                bag.remove(en_id, 1)
                server.store.mark_dirty(f'guild board {en_id}')
            return {'item': en_id}
        record = server.cash.find(char, en_id)             # the client's own pick (FUN_0045E760)
        if record is None:
            raise Refused(f'no usable {EC.item_name(en_id)} ({en_id}) cash record', TEXT_NO_ITEM)
        before = dict(record)
        # consume by serial, its own store.lock hold (the master's own record: only his own
        # requests and GM aids change it)
        if server.cash.consume(char, before['serial'], 1, what='guild board') is None:
            raise Refused(f'{EC.item_name(en_id)} record {before["serial"]:#x} vanished', TEXT_NO_ITEM)
        return {'item': en_id, 'serial': before['serial'], 'record': before}

    def _give_back(self, session, char, kind, taken):
        """Undo _take after a failed commit - server side only: the client removed nothing yet
        (no 0x25 / 0x72 went out)."""
        server = self.server
        try:
            if kind == KIND_BOARD:
                with server._combat_lock(session):
                    bag = server._bag(session)
                    if bag is not None:
                        bag.add(taken['item'], 1)
                        server.store.mark_dirty(f'guild board {taken["item"]} back')
                return
            with server.store.lock:
                items = CASH.ensure(char)
                live = next((r for r in items if r['serial'] == taken['serial']), None)
                if live is not None:
                    live['qty'] += 1
                else:
                    items.append(dict(taken['record']))
            server.store.mark_dirty(f'guild board cash record {taken["serial"]:#x} back')
        except Exception:                               # noqa: BLE001 - the refusal still goes out
            log.exception(f'[BOARD] giving the {kind} back to {char.get("name")!r} failed')

    # ------------------------------------------------------------ the echo ---
    def echo(self, sock, session, item):
        """C2S 0x15 {item} right after sub 185 {1, item}: S2C 0x25 {item} - the client's own
        consume of the bag unit the 0x88 took already - and nothing else. True when handled;
        False lets GameServer._handle_use_item go on (no pending echo of this id)."""
        pending = session.get(BOARD_ECHO_KEY)
        if not pending or pending[0] != _int(item):
            return False
        session.pop(BOARD_ECHO_KEY, None)
        if time.monotonic() - pending[1] > BOARD_ECHO_SECS:
            log.info(f'[BOARD] stale 0x15 echo of {item} from {session.get("char_name")!r}: the normal use path')
            return False
        P.send(self.server, sock, session, '0x25', {'item_or_skill_id': _int(item) & 0xFFFF})
        log.info(f'[BOARD] {session.get("char_name")!r} C2S 0x15 {item} (the sub 185 echo) -> 0x25: the client '
                 f'drops the billboard the 0x88 used (no second removal)')
        return True

    # ------------------------------------------------------- removal / ticks ---
    def remove_guild(self, gid, why, always=False):
        """Drop every board of guild `gid` (any channel) and send 0xB8 {gid} to 9702 - also with
        no board when `always` (the disband's 0xB8 of P14). Returns the boards dropped."""
        if not self.supported:
            return []
        gid = _int(gid)
        with self._held():
            with self.db.lock:
                gone = [b for b in self.db.boards.values() if b.guild_id == gid]
                for b in gone:
                    del self.db.boards[b.key]
                if gone:
                    self.db._mark()
            if gone or always:
                self._fan(*G.board_remove(gid), f'{why}: guild {gid} ({len(gone)} board(s))')
        if gone:
            self.request_save()
        return gone

    def tick(self, now=None):
        """'guild-boards' (every SCAN_SECS): boards past expires_at -> 0xB8 {gid} to 9702."""
        if not self.supported:
            return []
        now = self.clock() if now is None else now
        with self._held():
            with self.db.lock:
                gone = [b for b in self.db.boards.values() if b.expires_at <= now]
                for b in gone:
                    del self.db.boards[b.key]
                if gone:
                    self.db._mark()
            for b in gone:
                self._fan(*G.board_remove(b.guild_id), f'expired: the {b.guild_name!r} {b.kind} '
                                                       f'"{b.ad_text}" (ch{b.channel})')
        if gone:
            self.request_save()
        return gone

    # -------------------------------------------------------------- GM aid ---
    def seed(self, guild_name, ad_text, kind=KIND_BOARD, pos=None, minutes=None, channel=None):
        """`!guild board place|premium` (and the 'kr' exe's only source of boards, G-CP default):
        a board of guild `guild_name` in the master's name, replacing that guild's board.
        Returns the Board. Raises ValueError."""
        if not self.supported:
            raise ValueError('boards need the 2009 client (CLIENT_BUILD 2009)')
        g = self.server.guilds.get(guild_name)
        if g is None:
            raise ValueError(f'no guild named {guild_name!r}')
        ad = G.text(ad_text, AD_TEXT_MAX).strip()
        if not ad:
            raise ValueError('needs an advertisement text')
        master = g.master()
        found = self.server.guilds._resolve_chars([master['cid']]) if master else {}
        acc, char = found.get(master['cid'], ({}, {})) if master else ({}, {})
        x, y = pos if pos is not None else MOIBA_TILE
        now = self.clock()
        mins = self.minutes(kind) if minutes is None else float(minutes)
        channel = self.channel_of() if channel is None else int(channel)
        board = Board(channel=channel, map_code=PLAZA, guild_id=g.id, kind=kind, master_uid=_int(acc.get('uid')),
                      master_name=G.text(char.get('name', master['name'] if master else ''), G.NAME_MAX),
                      guild_name=g.name, emblem_fg=g.emblem_fg, emblem_bg=g.emblem_bg, ad_text=ad,
                      pos_x=float(x), pos_y=float(y), placed_at=now, expires_at=now + mins * 60.0, placed_by=0)
        ids = self.client_item_ids()
        with self._held():
            if self.of_guild(g.id, channel) is not None:
                self.remove_guild(g.id, 'GM replace')
            elif len(self.live(channel)) >= MAX_BOARDS:
                raise ValueError(f'the plaza holds {MAX_BOARDS} boards already (one 0xBB frame)')
            with self.db.lock:
                self.db.boards[board.key] = board
                self.db._mark()
            self._fan(*G.board_add(self.wire(board), ids), f'GM seed: the {g.name!r} {kind} board')
        self.request_save()
        return board

    def set_left(self, guild_name, secs):
        """`!guild board ttl <guild> <secs>`: the board expires `secs` from now (live tests)."""
        g = self.server.guilds.get(guild_name)
        if g is None:
            raise ValueError(f'no guild named {guild_name!r}')
        with self.lock, self.db.lock:
            b = self.db.boards.get((self.channel_of(), PLAZA, g.id))
            if b is None:
                raise ValueError(f'{g.name} has no live board')
            b.expires_at = self.clock() + max(0.0, float(secs))
            self.db._mark()
        self.request_save()
        return b

    def describe(self, now=None):
        now = self.clock() if now is None else now
        rows = self.live(now=now)
        lines = [f'{len(rows)} live board(s) on {PLAZA} (ch{self.channel_of()}), exe ids '
                 f'{G.board_items(self.client_item_ids())} ({self.client_item_ids()}):']
        for b in rows:
            left = int(b.left(now))
            lines.append(P.cut_text(f'{b.guild_name} {b.kind} "{b.ad_text}" ({b.pos_x:g},{b.pos_y:g}) '
                                    f'{left // 60}m{left % 60:02d}s left, by {b.master_name}', 80).decode('latin-1'))
        return lines

    def dev(self, session, words):
        """`!guild board [list] | place <guild> <text...> | premium <guild> <text...> | ttl <guild>
        <secs> | expire <guild|all>` (guild.Guilds.dev_guild hands over here)."""
        reply = self.server._gm_reply
        sub = words[0].lower() if words else 'list'
        if sub == 'list':
            for line in self.describe():
                reply(session, line)
        elif sub in ('place', 'premium'):
            if len(words) < 3:
                raise gm.DevCommandError(f'board {sub} <guild> <text...>')
            pos = None
            if _int(session.get('current_map')) == PLAZA and session.get('pos'):
                pos = tuple(float(v) for v in session['pos'][:2])
            b = self.seed(words[1], ' '.join(words[2:]), KIND_PREMIUM if sub == 'premium' else KIND_BOARD, pos)
            reply(session, f'{b.guild_name}: {b.kind} "{b.ad_text}" at ({b.pos_x:g},{b.pos_y:g}) for '
                           f'{(b.expires_at - b.placed_at) / 60:g} min (0xBA to {PLAZA}).')
        elif sub == 'ttl':
            if len(words) < 3:
                raise gm.DevCommandError('board ttl <guild> <secs>')
            b = self.set_left(words[1], gm.parse_int(words[2], 'secs', 0, 31 * 86400))
            reply(session, f'{b.guild_name}: the board expires in {int(b.left(self.clock()))} s.')
        elif sub == 'expire':
            if len(words) < 2:
                raise gm.DevCommandError('board expire <guild|all>')
            if words[1].lower() == 'all':
                gids = sorted({b.guild_id for b in self.live()})
            else:
                g = self.server.guilds.get(words[1])
                if g is None:
                    raise ValueError(f'no guild named {words[1]!r}')
                gids = [g.id]
            n = sum(len(self.remove_guild(gid, 'GM expire')) for gid in gids)
            reply(session, f'{n} board(s) removed (0xB8 to {PLAZA}).')
        else:
            raise gm.DevCommandError(f'unknown board sub-command {sub!r}')

    # ---------------------------------------------------------------- save ---
    def request_save(self):
        """Write the file from the tick thread (guild.Guilds.request_save); without a scheduler
        (unit tests) at once."""
        if not self.db.path:
            return
        ticks = getattr(self.server, 'ticks', None)
        if ticks is None:
            self.flush()
            return
        with self._plock:
            if self._pending is not None:
                return
            self._pending = ticks.call_later(0, self._scheduled_flush, name='guild-boards-save')

    def _scheduled_flush(self):
        with self._plock:
            self._pending = None
        self.tick_flush()

    def tick_flush(self):
        return self.flush(delays=storemod.QUICK_REPLACE_DELAYS, wait=0)

    def flush(self, delays=None, wait=None):
        try:
            done = self.db.flush(delays=delays, wait=wait)
        except OSError as e:
            self._failures += 1
            if self._failures == 1:
                log.exception(f'[BOARD] saving {self.db.path} failed; kept dirty, retrying in {RETRY_SECS:g} s')
            else:
                log.warning(f'[BOARD] saving {self.db.path} failed again ({self._failures} in a row): {e}')
            ticks = getattr(self.server, 'ticks', None)
            if ticks is not None:
                with self._plock:
                    if self._pending is None:
                        self._pending = ticks.call_later(RETRY_SECS, self._scheduled_flush,
                                                         name='guild-boards-retry')
            return False
        self._failures = 0
        return done


def install(rs, boards):
    """The guild-g6 step of the arch09-resync-bundle: 0xBB after the 0x8A reply's sub 3 / 15 and
    sub 4, before the GM tag (sub 37 stays last)."""
    import resync as resyncmod

    def step(server, sock, session, ctx):
        boards.send_list(sock, session)

    rs.register(resyncmod.STAGE_GUILD, 'guild_boards', step, builds={G.BUILD_2009}, after='guild_apps',
                note='0xBB with the live boards to a client entering 9702 (guild-g6)')
    return rs
