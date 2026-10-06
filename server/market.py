#!/usr/bin/env python3
"""
market.py - personal item stalls on the flea market 9701, the runtime half (P7 stage 2)
=====================================================================================
shop_storage-stall-registry, -stall-open-close, -stall-browse-buy, -stall-presence
(docs/systems/shop_storage.md 1.6, F9-F14, 3.2); stall.py holds the persisted escrow and
the pure rules. Both client builds: every stall packet is wire-identical (stall.py).

    market = Market(server)                  # GameServer.__init__; register() adds its hooks
    market.open(session, payload)            # C2S 0x5E -> 0x82 {1} + 0x85 to the map, or 0x82 {2|9}
    market.stop_edit(session)                # C2S 0x5F -> 0x83 {1} (+ 0x86 to the map)
    market.close_request(session)            # C2S 0x60 -> 0x84 {1} (+ 0x86), escrow back in the bag
    market.visit(session, owner_uid)         # C2S 0x61 -> 0x87 {1, list} / {1, 0} / {0} / {6}
    market.buy(session, rec)                 # C2S 0x62 -> 0x88 {1} to the buyer + 0x89 to the
                                             #   seller, or 0x88 {0} (+ a fresh 0x87 to the buyer)
    market.sign_fields(subject)              # the 0x85 presence.py sends after a seller's record
    market.recover(account)                  # login: a crash-left escrow back into the bag (F14.5)

Who moves what (shop_storage 1.6)
--------------------------------
The CLIENT takes a registered item out of its own bag (FUN_00467140) before it sends 0x5E,
and puts the unsold ones back itself when window 0x259 leaves selling or setup mode
(FUN_004665e0 -> FUN_00467200: on 0x84 {1}, on the map load's 0x08). So the server moves
the listed items out of the bag model into the persisted escrow (char['stall_escrow']) when
the stall opens and back when it closes - the model then always equals what the client
shows, also across the next 0x03. A sale moves gold between the two wallets and one unit
count from the seller's escrow into the buyer's bag; the seller's bag is never touched.

Kept rows: what a full bag cannot take back at a close stays in char['stall_escrow'] (the
client silently drops it, FUN_00467200). No stall is open then, so every row there is a kept
one; the next Start puts them back into the bag model first and refuses (0x82 {2} + the
bag-full line) while they still do not fit - its new escrow must never overwrite them (P7
stage 2 review, fix 1). Login does the same (recover). The client shows restored rows from
the next 0x03 on (map load / login): no packet adds a socketed block to its bag.

Stop for edit returns the escrow at once (a deliberate step away from F12's "keep the
escrow"): after 0x83 {1} the client is in setup mode, where closing the window is LOCAL (it
sends 0x60 only while +0x20 selling is set, F13 step 1) and puts the listed items back into
its bag - an escrow kept in 'edit' state would then hold items the client already shows in
the bag, until the next map load. Setup mode is the state before the first Start, where the
items are in the bag model too, and the next Start re-escrows the (possibly edited) list.

State
-----
  stalls  {owner uid: Stall}            the open stalls, all maps (the escrow is also on the
                                        record, persisted on every change). Keyed by the
                                        account uid (world-uid-alloc: one per account); the
                                        Stall keeps its owner SESSION and every use checks
                                        identity, so a 2009 relogin's new session never acts
                                        on its old session's stall.
Session keys: stall_client_selling (the client's ctx+0x20 mirror: set by every 0x5E whatever
the result, because a failed Start leaves +0x20 = 1 and its Close must get 0x84 {1} - F9
step 3, F13 step 5; cleared by 0x83 / 0x84), rebuild_03 (set for GameServer._map_transfer
when the map-load hook returned an escrow: the 0x03 it built earlier lacks those items).

Presence (shop_storage-stall-presence, F14.2; fixes B13)
-------------------------------------------------------
Open: S2C 0x85 {uid, SIGN_SPRITE, title} to every peer whose client holds the seller
(presence.to_holders; the record revision is bumped, so a record still in flight is rebuilt).
Close / stop / map load / disconnect: 0x86 {uid} to those holders (the old map's, before its
0x06). A player who arrives later gets the seller's ordinary record and right after it the
same 0x85 (presence.spawn / show_peers_to ask sign_fields under the receiver's lock). The
0x85 path is the one live-verified to draw the board, the aura and the orange name (trade#16,
sprite 1); the record's own shop block drew the title but no board (C48), so the records keep
shop_open 0 - a record with the flag set would make the following 0x85 a no-op.

Locks (world.py "Locks"; the trade.py ladder): world_lock -> combat_lock (the seller's; a
buy takes both players' in (uid, id) order, like the trade commit) -> Market.lock ->
store.lock -> send_lock. open() also asks Trades.busy() under Market.lock (Market.lock ->
Trades.lock, never the reverse: Trades.selling reads the stall table lock-free), after
store.lock is released. Nothing here takes a presence lock while holding Market.lock (the
0x85 / 0x86 broadcasts run after it is released), and sign_fields takes no ladder lock -
only World's leaf lock, in world.map_of - because presence calls it under a receiver's lock.
Packets to the seller about its own stall (0x82 / 0x83 / 0x84, a sale's 0x89) are queued
under Market.lock, so a 0x89 can never overtake the 0x82 {1} that opened selling mode.

No trade while selling (P7 stage 2 review fix 3; the open race, P7 review): open() registers
the stall FIRST and only then asks Trades.busy(); Trades.accept checks selling() and creates
the trade inside one Trades.lock section. Whichever runs second sees the other: an accept
after the registration refuses, a trade that opened before the busy() check makes open()
release the stall again (escrow back) and answer 0x82 {2}. The stall was in the table while
busy() waited for Trades.lock, so a peer's presence record built then may have carried its
0x85 sign: the rollback sends the 0x86 too, after the locks (to a holder that never got the
0x85 it is a no-op, as in gone()).

Audit: stall_log.jsonl next to accounts.json, one JSON line per open / close / sale; a
rolled-back open writes its 'open' line (rolled_back: true) before its 'close'.
"""
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field

import en_content as EC
import inventory as invmod
import packets as P
import presence
import registry
import skills as SK
import stall as ST
import store as storemod
import world as worldmod

log = logging.getLogger('WS')

LOG_NAME = 'stall_log.jsonl'
# 0x15 lines: "[Warning] " + text must fit the 88-byte client buffer (notice_fields)
BAG_FULL_TEXT = "Bag full: stall items are kept until there is room (next Start or login)."
RESTORED_TEXT = "Kept stall items are back in your bag (shown after your next map change)."


@dataclass
class Stall:
    session: dict = field(repr=False)
    uid: int
    name: str
    map_code: int
    title: bytes
    entries: list
    char: dict = field(repr=False)
    sign: int = ST.SIGN_SPRITE
    viewers: dict = field(default_factory=dict)
    opened: float = 0.0


def uid_of(session):
    return P.session_uid(session or {}) or 0


def name_of(session):
    return str((session or {}).get('char_name') or '')


class Market:
    def __init__(self, server):
        self.server = server
        self.lock = threading.RLock()
        self.stalls = {}
        self._log_lock = threading.Lock()

    # ------------------------------------------------------------------ views ---
    def stall_of(self, session):
        """The open Stall `session` owns, or None (identity-checked: a 2009 relogin's new
        session does not own its old session's stall)."""
        st = self.stalls.get(uid_of(session)) if isinstance(session, dict) else None
        return st if st is not None and st.session is session else None

    def selling(self, session):
        return self.stall_of(session) is not None

    def sign_fields(self, subject):
        """S2C 0x85 fields for `subject`'s open stall on the map it is on, else None.
        No ladder lock: presence.spawn / show_peers_to call it under a receiver's presence
        lock, under which only send_lock may be taken. It takes only World's private leaf
        lock (world.map_of), which any thread may take under any lock (world.py "Locks");
        the rest is a dict get and attribute reads, which are atomic. open() registers a
        stall before its 0x85 broadcast and every close unregisters it before its 0x86, so a
        record decided under that lock either sees the stall and follows it with 0x85 or
        does not and gets the broadcast (market docstring)."""
        st = self.stalls.get((subject or {}).get('uid'))
        if st is None or st.session is not subject or self.server.world.map_of(subject) != st.map_code:
            return None
        return ST.sign_fields(st.uid, st.title, st.sign)

    @property
    def log_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(self.server.store.path)), LOG_NAME)

    # ---------------------------------------------------------------- sending ---
    def _push(self, target, key, fields):
        """Queue one packet on `target` (never a socket write under Market.lock)."""
        if target is None or target.get('sock') is None or target.get('closed') or target.get('kicked'):
            return False
        return self.server._push(target, key, fields, 'STALL', flush=False)

    def _notice(self, target, text):
        return self._push(target, '0x15', self.server.notice_fields(text, 'warn'))

    def _sign_off(self, st):
        """S2C 0x86 {uid} to the peers on the stall's map whose client holds the seller (the
        sign goes; a buyer window on that stall closes with "The shop is closed or
        adjusting.", spec 0x86). Works while the seller is already off the map (disconnect):
        presence.to_map_holders does not need it reachable."""
        return presence.to_map_holders(self.server, st.session, st.map_code, '0x86',
                                       {'owner_uid': st.uid & 0xFFFFFFFF}, 'STALL')

    # ============================================================ F9: open ===
    def open(self, session, payload):
        """C2S 0x5E StallOpen (Start in window 0x259; the client set +0x20 selling). F9 in
        order: the selling mirror first (whatever the outcome); an undecodable list (the
        client counts NULL list nodes, F9 3.3) -> 2; not on the flea market, manner below
        -79, Open Stall (194) not learned, dead or trading -> 2; the list itself (stall.
        parse_open) -> 9 / 2; the seller's bag not holding every listed item -> 9. A stall
        this uid still has (a lost 0x83, a repeated Start) gives its escrow back first (F9
        3.7); rows a full bag kept at an earlier close come back next, and while some still do
        not fit -> 2 (module docstring "Kept rows"). Commit: the items leave the bag into
        char['stall_escrow'], persisted, the stall is registered, and - no trade opened
        meanwhile (module docstring "No trade while selling"; else released again -> 2, 0x86
        to the map) - 0x82 {1} to the seller and 0x85 to every peer holding it."""
        session['stall_client_selling'] = True
        server, me = self.server, name_of(session)
        rec, err = registry.decode(0x5E, bytes(payload), server.client_build)
        if rec is None:
            return self._refuse_open(session, ST.OPEN_FAILED, f'the list does not decode ({err or "no grammar"})')
        title, entries, why = ST.parse_open(rec)
        gate = self._open_gate(session)
        if gate is not None:
            return self._refuse_open(session, ST.OPEN_FAILED, gate)
        if why is not None:
            return self._refuse_open(session, *why)
        uid, map_code = uid_of(session), server.world.map_of(session)
        store = server.store
        replaced = rolled_back = st = None
        result, kept, left = ST.OPEN_TAMPERED, [], []
        with server._combat_lock(session):
            with self.lock:
                old = self.stalls.get(uid)
                if old is not None:
                    self._release(old, 'replaced by a new Start')
                    replaced = old
                char = server._session_char(session)
                with store.lock:
                    bag = invmod.Inventory(char)
                    # Before anything overwrites char['stall_escrow']: no stall of this char is
                    # open here (the old one was just released), so its rows are kept ones.
                    kept, left = self._restore_kept(char, bag)
                    if left:
                        result = ST.OPEN_FAILED
                        why = (f'kept stall items still do not fit the bag: '
                               f'{", ".join(e.describe() for e in left)}')
                    else:
                        why = ST.ownership_refusal(bag, entries)
                    if why is None:
                        saved = storemod.snapshot(char['inventory'])
                        for e in entries:
                            if bag.remove(e.item_id, e.qty, e.bag_words()) != e.qty:
                                # ownership_refusal checked the same model under the same
                                # locks, so this is a bug: never half an escrow
                                char['inventory'].update(saved)
                                why = f'{e.describe()} vanished while escrowing'
                                break
                    if why is None:
                        char['stall_escrow'] = [e.as_record() for e in entries]
                        self._save(f'stall open {me}')
                    elif kept:
                        self._save(f'stall kept items back {me}')
                if why is None:
                    st = Stall(session, uid, me, map_code, title, list(entries), char, opened=time.monotonic())
                    self.stalls[uid] = st
                    # Registered first, THEN the trade check (module docstring "No trade
                    # while selling"): a trade another thread's accept opened since
                    # _open_gate is seen here, and any later accept sees the stall.
                    trades = getattr(server, 'trade', None)
                    if trades is not None and trades.busy(session):
                        # The entries just left this bag, so they all fit back (_release).
                        # The stall was visible (sign_fields) while busy() waited, so a peer's
                        # presence record may carry its 0x85: sign it off below like `replaced`.
                        # Its 'open' audit line keeps every 'close' paired with one.
                        self.audit('open', st, rolled_back=True)
                        self._release(st, 'a trade opened while the stall was opening')
                        rolled_back, st, result = st, None, ST.OPEN_FAILED
                        why = 'trading (a trade opened while the stall was opening; escrow back)'
                    else:
                        self._push(session, '0x82', {'result': ST.OPEN_OK})
        if replaced is not None:
            self._sign_off(replaced)
        if rolled_back is not None:
            # A 0x86 to a holder that never got the 0x85 is a no-op (gone() relies on it too).
            self._sign_off(rolled_back)
        if kept:
            back = sum(e.qty for e in kept) - sum(e.qty for e in left)
            log.info(f'[STALL] {me!r}: kept stall items ({", ".join(e.describe() for e in kept)}): '
                     f'{back} unit(s) back in the bag'
                     + (f', still kept {", ".join(e.describe() for e in left)}' if left else ''))
            if back > 0:
                self._notice(session, RESTORED_TEXT)
        if why is not None:
            refused = self._refuse_open(session, result, why)
            if left:
                self._notice(session, BAG_FULL_TEXT)
            return refused
        sent = presence.to_holders(server, session, '0x85', ST.sign_fields(uid, title, st.sign))
        log.info(f'[STALL] {me!r} (uid {uid}) opens {title!r} on map {map_code}: '
                 f'{", ".join(e.describe() for e in entries)} escrowed; 0x82 {{1}}, 0x85 to {sent} peer(s)')
        self.audit('open', st)
        return 'open'

    @staticmethod
    def _restore_kept(char, bag):
        """(kept, left): put the rows a full bag left in char['stall_escrow'] back into `bag`
        - as much as fits (stall.return_to_bag) - and keep only what still does not fit
        there. Caller holds Market.lock and store.lock with no stall of `char` open (every
        row is then a kept one), and saves the record."""
        kept = ST.escrow_entries(char)
        if not kept:
            return [], []
        left = [e for e in ST.return_to_bag(bag, kept) if e.qty > 0]
        char['stall_escrow'] = [e.as_record() for e in left]
        return kept, left

    def _open_gate(self, session):
        """Why this seller may not open a stall now (result 2), else None."""
        server = self.server
        if not session.get('in_world') or server._session_char(session) is None:
            return 'not in world'
        code = server.world.map_of(session)
        if code not in ST.STALL_MAPS:
            return f'not on the flea market (map {code})'
        manner = self._manner(session)
        if manner < ST.MANNER_FLOOR:
            return f'manner {manner} < {ST.MANNER_FLOOR} (FUN_00466e30)'
        if ST.OPEN_STALL_SKILL not in SK.learned(server._session_char(session)):
            return f'Open Stall ({ST.OPEN_STALL_SKILL}) is not learned (the window opens only on its 0x25)'
        if session.get('dead'):
            return 'dead'
        trades = getattr(server, 'trade', None)
        if trades is not None and trades.busy(session):
            return 'trading (the trade escrow and the stall would share the bag)'
        return None

    def _refuse_open(self, session, result, why):
        self._push(session, '0x82', {'result': result})
        (log.warning if result == ST.OPEN_TAMPERED else log.info)(
            f'[STALL] {name_of(session)!r} open refused with 0x82 {{{result}}}: {why}')
        return 'refused'

    def _manner(self, session):
        try:
            return int(self.server.store.manner(session.get('username')) or 0)
        except (AttributeError, TypeError, ValueError):
            return 0

    # ================================================ F12 / F13: stop, close ===
    def stop_edit(self, session):
        """C2S 0x5F (Stop): 0x83 {1} while the selling mirror is set or a stall is open (any
        other value strands the client in selling mode, F12 step 4; C8: only a 0x5F ever
        gets a 0x83). An open stall closes: escrow back into the bag (module docstring),
        0x86 to the map. With the mirror clear the 0x5F crossed our own 0x83 / 0x84 (a Stop
        pressed while a portal's 0x84 {1} was on the wire) and is ignored like 0x60's
        re-sends: 0x83 {1} sets ctx+0x1C in ANY mode (C8, live shop_storage#18), so a later
        buyer window would close a display-only copy of the stall it browsed into that
        client's bag. The client's +0x20 is only ever set while the mirror is, so ignoring
        never strands a window."""
        with self.server._combat_lock(session), self.lock:
            st = self.stall_of(session)
            left = self._release(st, 'stop for edit') if st is not None else []
            answer = bool(session.get('stall_client_selling')) or st is not None
            if answer:
                self._push(session, '0x83', {'result': 1})
                session['stall_client_selling'] = False
        if not answer:
            log.info(f'[STALL] {name_of(session)!r} stop with no stall and the selling mirror '
                     'clear - ignored (it crossed our 0x83 / 0x84)')
            return 'ignored'
        self._after_close(session, st, left, 'stop for edit -> 0x83 {1}')
        return 'stopped' if st is not None else 'no stall'

    def close_request(self, session):
        """C2S 0x60 (Close, or the window closed while selling; also auto-sent after S2C 0x08
        and from FUN_0042cf20). An open stall closes (escrow back, 0x86); 0x84 {1} goes out
        while the selling mirror is set, once - the automatic re-sends are ignored (F13)."""
        with self.server._combat_lock(session), self.lock:
            st = self.stall_of(session)
            left = self._release(st, 'closed') if st is not None else []
            answer = bool(session.get('stall_client_selling')) or st is not None
            if answer:
                self._push(session, '0x84', {'result': 1})
                session['stall_client_selling'] = False
        if not answer:
            log.info('[STALL] close with no stall open on record - ignored (automatic re-send)')
            return 'ignored'
        self._after_close(session, st, left, 'close -> 0x84 {1}')
        return 'closed' if st is not None else 'window closed'

    def _release(self, st, reason):
        """Unregister `st` and put its escrow back into the owner's bag (caller holds
        Market.lock; the owner's combat lock where it can). What a full bag cannot take stays
        in char['stall_escrow'] for the next Start or login (stall.return_to_bag; Start's
        _restore_kept). Persisted at once.
        Returns the entries that did not fit."""
        if self.stalls.get(st.uid) is st:
            del self.stalls[st.uid]
        st.viewers.clear()
        server = self.server
        with server.store.lock:
            left = ST.return_to_bag(invmod.Inventory(st.char), st.entries)
            left = [e for e in left if e.qty > 0]
            st.char['stall_escrow'] = [e.as_record() for e in left]
            st.entries = []
            self._save(f'stall {reason} {st.name}')
        self.audit('close', st, reason=reason, kept=[e.as_record() for e in left])
        return left

    def _after_close(self, session, st, left, what):
        if st is None:
            log.info(f'[STALL] {name_of(session)!r} {what} (no stall open)')
            return
        sent = self._sign_off(st)
        if left:
            self._notice(session, BAG_FULL_TEXT)
        log.info(f'[STALL] {st.name!r} {what}: escrow back in the bag'
                 + (f' except {", ".join(e.describe() for e in left)}' if left else '')
                 + f'; 0x86 to {sent} peer(s)')

    # ======================================================== F10: browse ===
    def visit(self, session, owner_uid):
        """C2S 0x61 {owner_uid} (a click on a remote player whose stall flag is set). The
        viewer's own stall open -> 0x87 {6}; no open stall of that uid on the viewer's map,
        or the viewer's own -> {0}; nothing left -> {1, 0}; else the list, and the viewer is
        noted (`!stall`)."""
        with self.lock:
            fields, why = self._list_for(session, int(owner_uid or 0))
            self._push(session, '0x87', fields)
        log.info(f'[STALL] {name_of(session)!r} visits uid {owner_uid}: 0x87 {{{fields["result"]}'
                 f'{", " + str(fields.get("item_count")) if fields["result"] == ST.LIST_OK else ""}}} ({why})')
        return why

    def _list_for(self, viewer, owner_uid):
        """(0x87 fields, log reason) - caller holds Market.lock."""
        if self.stall_of(viewer) is not None:
            return {'result': ST.LIST_OWN_STALL}, 'the viewer has its own stall open'
        st = self.stalls.get(owner_uid)
        if st is None or st.session is viewer or st.uid == uid_of(viewer):
            return {'result': ST.LIST_CLOSED}, 'no open stall'
        world = self.server.world
        if not worldmod.reachable(st.session) or world.map_of(viewer) != st.map_code \
                or world.map_of(st.session) != st.map_code:
            return {'result': ST.LIST_CLOSED}, 'not on the viewer\'s map'
        st.viewers[uid_of(viewer)] = time.monotonic()
        return ST.list_fields(st.uid, st.title, st.entries), f'{len(st.entries)} entr(y/ies)'

    # ========================================================== F11: buy ===
    def buy(self, session, rec):
        """C2S 0x62 {seller_uid, id, qty, descriptor} (Purchase + amount dialog 0x10). Under
        world_lock, both players' combat locks ((uid, id) order - the trade commit's rule),
        Market.lock and store.lock: the node is the first one with this id (equipment: this
        exact block) holding >= qty (the seller client's own 0x89 match rule, live
        shop_storage); the PRICE is the server's (0x62 carries none), the total u64. Refused:
        the buyer's own stall or a stall of its own uid, a stall not open on the buyer's
        map, the seller gone, qty 0 / above the node / equipment != 1, an option list over 5
        words, gold short (or locked in a trade), the seller's wallet overflowing, manner
        below -79, no room. Commit, persisted in ONE accounts.json write: buyer gold - total,
        seller gold + total, the node decremented (dropped at 0), the item into the buyer's
        bag -> 0x88 {1, gold, ...} to the buyer, 0x89 {gold, ...} to the seller. A refusal
        -> 0x88 {0}, then a fresh 0x87 of the stall to THAT buyer only (its window is open:
        it just clicked Purchase; F11 step 5)."""
        server = self.server
        seller_uid = int(rec.get('seller_uid', 0) or 0)
        item, qty, n, opts, extra = P.item_descriptor(rec)
        what = f'{EC.item_name(item) or item} x{qty} from uid {seller_uid}'
        st0 = self.stalls.get(seller_uid)
        seller = st0.session if st0 is not None else None
        if seller is None or seller is session:
            with self.lock:
                self._push(session, '0x88', {'result': ST.BUY_FAILED})
            log.info(f'[STALL] {name_of(session)!r} buys {what}: no open stall of that uid -> 0x88 {{0}}')
            return 'no stall'
        first, second = sorted((session, seller), key=lambda s: (uid_of(s), id(s)))
        world_lock = getattr(server, 'world_lock', None) or threading.RLock()
        refresh = None
        with world_lock, server._combat_lock(first), server._combat_lock(second):
            with self.lock:
                st = self.stalls.get(seller_uid)
                if st is not None and st.session is not seller:
                    st = None
                why, entry, total = self._buy_refusal(session, st, item, qty, n, opts, extra)
                if why is None:
                    why, gold_b, gold_s = self._commit_sale(session, st, entry, qty, total)
                if why is None:
                    self._push(session, '0x88', ST.bought_fields(gold_b, entry, qty))
                    self._push(st.session, '0x89', ST.sold_fields(gold_s, entry, qty))
                    left = len(st.entries)
                else:
                    self._push(session, '0x88', {'result': ST.BUY_FAILED})
                    if st is not None and self.stall_of(session) is None:
                        refresh, _ = self._list_for(session, seller_uid)
                        if refresh.get('result') == ST.LIST_OK:
                            self._push(session, '0x87', refresh)
                        else:
                            refresh = None
        if why is not None:
            log.info(f'[STALL] {name_of(session)!r} buys {what}: refused ({why}) -> 0x88 {{0}}'
                     + (' + a fresh 0x87' if refresh is not None else ''))
            return 'refused'
        log.info(f'[STALL] {name_of(session)!r} bought {what} for {total} gold from {st.name!r}: '
                 f'buyer gold {gold_b}, seller gold {gold_s}, {left} entr(y/ies) left; persisted; '
                 f'0x88 {{1}} + 0x89')
        self.audit('sale', st, buyer={'uid': uid_of(session), 'name': name_of(session),
                                      'account': session.get('username')},
                   item=item, qty=qty, total=total, gold_buyer=gold_b, gold_seller=gold_s)
        return 'bought'

    def _buy_refusal(self, buyer, st, item, qty, n, opts, extra):
        """(why, entry, total): why None when the buy may commit (caller holds every lock)."""
        server = self.server
        if not buyer.get('in_world') or server._session_char(buyer) is None:
            return 'the buyer is not in world', None, 0
        if self.stall_of(buyer) is not None:
            return 'the buyer has its own stall open', None, 0
        if st is None:
            return 'no open stall of that uid (closed meanwhile)', None, 0
        if st.session is buyer or st.uid == uid_of(buyer):
            return 'a player cannot buy from its own stall', None, 0
        world = server.world
        if not worldmod.reachable(st.session) or server.world.superseded(st.session):
            return 'the seller is no longer in the world', None, 0
        if world.map_of(buyer) != st.map_code or world.map_of(st.session) != st.map_code:
            return f'not on the stall\'s map {st.map_code}', None, 0
        if n > P.OPTION_LIST_MAX or len(opts) > P.OPTION_LIST_MAX:
            return f'{n} option words (max {P.OPTION_LIST_MAX}): tampered', None, 0
        if qty < 1:
            return f'qty {qty}: tampered', None, 0
        words = invmod.block_from_wire(opts, extra)
        entry = next((e for e in st.entries if e.matches(item, words) and e.qty >= qty), None)
        if entry is None:
            listed = sum(e.qty for e in st.entries if e.matches(item, words))
            return f'no node of that item with >= {qty} (listed {listed}): stale window or tampered', None, 0
        if entry.equip and qty != 1:
            return f'equipment qty {qty}: tampered', entry, 0
        total = entry.price * qty
        manner = self._manner(buyer)
        if manner < ST.MANNER_FLOOR:
            return f'buyer manner {manner} < {ST.MANNER_FLOOR}', entry, total
        if buyer.get('dead'):
            return 'the buyer is dead', entry, total
        trades = getattr(server, 'trade', None)
        locked = trades.gold_refusal(buyer, total) if trades is not None else None
        if locked is not None:
            return locked, entry, total
        wallet_b, wallet_s = server._wallet_of(buyer), invmod.Wallet(st.char)
        if wallet_b.gold < total:
            return f'costs {total}, the buyer holds {wallet_b.gold}', entry, total
        if wallet_s.gold + total > invmod.GOLD_MAX:
            return 'the seller\'s gold would overflow u64', entry, total
        room = server._bag(buyer).fits(entry.item_id, qty, entry.bag_words())
        if room is not None:
            return f'no room: {room}', entry, total
        return None, entry, total

    def _commit_sale(self, buyer, st, entry, qty, total):
        """Apply one checked sale (caller holds every lock). Returns (why, buyer gold, seller
        gold); why is None on success. The two records change under store.lock and are
        persisted in one atomic write before any packet goes out."""
        server, store = self.server, self.server.store
        char_b = server._session_char(buyer)
        with store.lock:
            bag = invmod.Inventory(char_b)
            if bag.add(entry.item_id, qty, entry.bag_words()) is None:
                return 'the buyer\'s bag refused the add (checked a moment ago: a bug)', 0, 0
            wallet_b, wallet_s = invmod.Wallet(char_b), invmod.Wallet(st.char)
            wallet_b.gold = wallet_b.gold - total
            wallet_s.gold = wallet_s.gold + total
            entry.qty -= qty
            if entry.qty <= 0:
                st.entries.remove(entry)
            st.char['stall_escrow'] = [e.as_record() for e in st.entries]
            self._save(f'stall sale {st.name} -> {name_of(buyer)}')
            return None, wallet_b.gold, wallet_s.gold

    def _save(self, reason):
        """One atomic accounts.json write now (caller holds store.lock); a failed write keeps
        the store dirty for the next flush - the model is already committed. Only the short
        replace backoff (QUICK_REPLACE_DELAYS) and as short a wait for another writer:
        every handler waits on store.lock meanwhile, and the store's own retry takes a
        failure or a busy file from there (review of livetest bug 7)."""
        store = self.server.store
        store.dirty = True
        try:
            store.save_now(delays=storemod.QUICK_REPLACE_DELAYS, wait=storemod.QUICK_REPLACE_BUDGET_SECS)
        except Exception:                               # noqa: BLE001 - the next flush retries
            log.exception(f'[STALL] {reason}: the accounts.json save failed; kept dirty')
            store.mark_dirty(reason)

    # ============================================ F14: map load / leave / login ===
    def map_load(self, session, map_code=None, reason=None):
        """`before_server_map_load` (F14.1): the stall closes BEFORE the lead. The escrow goes
        back into the bag model (so the 0x03 of this load lists it - session['rebuild_03']
        makes _map_transfer rebuild the one it built earlier), the old map's holders get
        0x86, and a client in selling mode gets 0x84 {1} now: its window closes and puts the
        listed items back while the OLD bag is loaded, and +0x20 is cleared so the 0x08 sends
        no 0x60. Sent after the 0x08 / 0x03 instead, it would add them a second time on top
        of the reloaded bag. A client in setup mode only needs nothing: the 0x08 closes its
        window locally before the 0x03."""
        with self.server._combat_lock(session), self.lock:
            st = self.stall_of(session)
            left = self._release(st, f'map load ({reason})') if st is not None else []
            answer = (bool(session.get('stall_client_selling')) or st is not None) and session.get('sock') is not None
            session['stall_client_selling'] = False
            if answer:
                self._push(session, '0x84', {'result': 1})
            if st is not None:
                session['rebuild_03'] = True
        if answer or st is not None:
            self._after_close(session, st, left, f'map load ({reason}) to {map_code}: 0x84 {{1}} before the lead')
        return st is not None

    def gone(self, session, reason, map_code=None):
        """on_leave_world / on_disconnect (F14.3-4): the seller's stall closes (escrow back,
        persisted, 0x86 to the peers still holding it - this hook runs BEFORE presence's
        0x06), and the session stops being anyone's viewer."""
        with self.server._combat_lock(session), self.lock:
            st = self.stall_of(session)
            left = self._release(st, f'left the world ({reason})') if st is not None else []
            session['stall_client_selling'] = False
            me = uid_of(session)
            for other in self.stalls.values():
                other.viewers.pop(me, None)
        if st is not None:
            sent = self._sign_off(st)
            log.info(f'[STALL] {st.name!r} left the world ({reason}): escrow back in the record'
                     + (f' except {", ".join(e.describe() for e in left)}' if left else '')
                     + f'; 0x86 to {sent} peer(s)')

    def recover(self, account):
        """Login (F14.5): a character whose record still holds a stall escrow and has no open
        stall (the server stopped while it was selling, or a full bag kept part of it) gets
        it back into the bag - as much as fits; the rest waits for the next Start or login.

        A stall of this account that is still registered to a session the login just
        replaced (2009 relogin: world.superseded) or kicked (duplicate login) is released
        first: its escrow goes back into the bag here instead of whenever the old
        connection's cleanup reaches Market.gone, so the new session's 0x03 always lists the
        items. Its 0x86 goes out after Market.lock is released. The old cleanup then finds
        no stall (stall_of is identity-checked) and changes nothing."""
        back = []
        chars = [c for c in list((account or {}).get('characters') or []) if isinstance(c, dict)]
        mine = {id(c) for c in chars}
        stale = []
        with self.lock:
            world = self.server.world
            for st in list(self.stalls.values()):
                if id(st.char) in mine and (st.session.get('kicked') or world.superseded(st.session)):
                    left = self._release(st, 'its session was replaced by a new login')
                    stale.append((st, left))
            released = {id(st.char) for st, _ in stale}
            live = {id(st.char) for st in self.stalls.values()}
            store = self.server.store
            with store.lock:
                for char in chars:
                    if not char.get('stall_escrow') or id(char) in live or id(char) in released:
                        continue
                    entries = ST.escrow_entries(char)
                    left = [e for e in ST.return_to_bag(invmod.Inventory(char), entries) if e.qty > 0]
                    char['stall_escrow'] = [e.as_record() for e in left]
                    back.append((char.get('name'), entries, left))
                if back:
                    self._save('stall escrow recovered at login')
        for st, left in stale:
            sent = self._sign_off(st)
            log.info(f'[STALL] {st.name!r}: stall of a replaced session released at login: escrow back '
                     f'in the bag' + (f' except {", ".join(e.describe() for e in left)}' if left else '')
                     + f'; 0x86 to {sent} peer(s)')
        for name, entries, left in back:
            log.warning(f'[STALL] {name!r}: a stall escrow left in the record '
                        f'({", ".join(e.describe() for e in entries)}) is back in the bag'
                        + (f' except {", ".join(e.describe() for e in left)}' if left else '') + ' (F14.5)')
        return back

    # ================================================================ audit ===
    def audit(self, event, st, **extra):
        """One JSON line per stall event in stall_log.jsonl (append-only, for dupe forensics).
        A write failure is logged, never raised: the event itself is already decided."""
        entry = {'t': time.strftime('%Y-%m-%dT%H:%M:%S'), 'event': event,
                 'seller': {'uid': st.uid, 'name': st.name, 'account': st.session.get('username')},
                 'map': st.map_code, 'title': P.to_bytes(st.title).decode('cp949', 'replace'),
                 'entries': [e.as_record() for e in st.entries]}
        entry.update(extra)
        try:
            line = json.dumps(entry, ensure_ascii=False)
            with self._log_lock, open(self.log_path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except (OSError, TypeError, ValueError) as e:
            log.warning(f'[STALL] audit line for {st.name!r} {event} not written: {e}')

    # ------------------------------------------------------------- dev view ---
    def describe(self, session):
        """Lines for `!stall`: this session's stall (title, map, entries) or 'no stall'."""
        with self.lock:
            st = self.stall_of(session)
            if st is None:
                return [f'no stall (selling mirror {bool(session.get("stall_client_selling"))})']
            lines = [f'stall {P.to_bytes(st.title).decode("cp949", "replace")!r} on {st.map_code}, '
                     f'{time.monotonic() - st.opened:.0f} s, {len(st.viewers)} viewer(s):']
            lines += [f' {e.describe()}' for e in st.entries] or [' (nothing left)']
        return lines

    def describe_all(self):
        """Lines for `!stall all`: every open stall."""
        with self.lock:
            stalls = sorted(self.stalls.values(), key=lambda s: s.uid)
            lines = [f'{len(stalls)} open stall(s)']
            for st in stalls:
                items = ', '.join(f'{e.item_id}x{e.qty}@{e.price}' for e in st.entries) or '-'
                lines.append(f'{st.name} (uid {st.uid}) map {st.map_code}: {items}')
        return lines


# ------------------------------------------------------------------- hooks ---
def register(hooks, market):
    """on_leave_world / on_disconnect (F14.3-4). Registered BEFORE presence's hooks, so the
    0x86 reaches the peers while their client still holds the seller (0x86 finds the entity by
    uid; after the 0x06 it is a no-op). The map-load close is GameServer._hook_stall_close
    (before_server_map_load), which delegates to Market.map_load."""
    def on_leave_world(server, session, map_code=None, reason=None, **_):
        market.gone(session, reason or 'left the world', map_code)

    def on_disconnect(server, session, reason=None, **_):
        market.gone(session, reason or 'disconnect')

    hooks.register(worldmod.ON_LEAVE_WORLD, on_leave_world)
    hooks.register(worldmod.ON_DISCONNECT, on_disconnect)
    return on_leave_world, on_disconnect
