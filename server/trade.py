#!/usr/bin/env python3
"""
trade.py - player-to-player trade (P7 stage 1; docs/systems/trade.md 1-3), both client builds
===========================================================================================
trade-request-accept, trade-offer, trade-lock-commit, trade-cancel-lifecycle,
trade-escrow-guards, trade-audit-log.

    trades = Trades(server)                  # GameServer.__init__; register() adds its hooks
    trades.request(session, target_uid)      # C2S 0x20 -> S2C 0x45 to B, or 0x47 {4|6} / 0x15
    trades.accept(session, requester_name)   # C2S 0x21 -> S2C 0x46 to both
    trades.add(session, item, qty, opts, x)  # C2S 0x22 -> S2C 0x4B to both / 0x4D / 0x15
    trades.remove(session, item, qty, ...)   # C2S 0x26 -> S2C 0x4C to the partner only
    trades.lock_offer(session, gold)         # C2S 0x23 -> S2C 0x48 {uid, gold} to both
    trades.confirm(session, echo)            # C2S 0x25 -> nothing yet / S2C 0x4A to both / 0x49
    trades.cancel(session, reason)           # C2S 0x24, disconnect, map load, death -> 0x49
    trades.escrow_refusal(session, item, n)  # trade-escrow-guards (sell/use/equip/drop/bank/quest)

Every packet is wire-identical in the two builds (spec_2009 C2S 0x44A9D6/0x20, 0x47394E/0x21,
0x473E14/0x22 + 0x476BF5/0x22, 0x473ABA/0x23, 0x473B5B/0x24, 0x473E14/0x25, 0x476E62/0x26
and S2C 0x45..0x4D: "identical"; the 2009 0x45 adds a client-side blacklist gate that needs
nothing from the server). Parsing goes through packets.parse with the server's build, so the
2009 send-site keys are the ones decoded there. Known gap until a blacklist model exists
(ROADMAP_2009_ADDENDUM P12, blacklist_channels A.7): a 2009 invitee who blacklisted the
requester drops the 0x45 without a dialog, but the server's one pending prompt per invitee
stays, so other requesters get 0x47 {6} "Trading." for up to INVITE_TTL.

Who moves what (trade.md 0, 1.4)
--------------------------------
The CLIENT moves items in its own bag; the server never sends an item packet for a trade:
  S2C 0x4B {owner_uid = offerer}   to BOTH: the offerer's client takes the item out of its
                                   bag into its own grid (self path), the partner's appends
                                   it to the partner grid;
  S2C 0x49                         the client puts its own offered items back in its bag;
  S2C 0x4A {new absolute gold}     the client adds its partner list to the bag and writes
                                   the gold ("Trade completed.").
So the server holds the offers in ESCROW - a list on the TradeState, the character record
untouched - and changes the two records only in the commit, exactly once. A 0x18 / 0x19 /
0x23 / 0x3F for traded goods would duplicate them in the client's bag view.

State (all under Trades.lock)
-----------------------------
  trades  {tid: Trade}                  active trades (OPEN -> CONFIRMING; DONE / CANCELED
                                        ones are dropped at once). Memory only: a restart
                                        mid-trade loses nothing, escrow never touched a record.
  invites {invitee uid: Invite}         ONE pending 0x45 per invitee: window 0x70 has no busy
                                        check, so a second request would re-label the dialog
                                        under the first requester's eyes (trade.md 2.1 step 4).
Session key: trade (the Trade the session is a side of; checked against `trades` on every use).
Nothing is persisted but the commit itself and trade_log.jsonl: no schema change, no migration.

Reply policy (registry.MUST_REPLY 0x24 / 0x25): every 0x24 gets a 0x49; a 0x25 gets the 0x4A
of the commit, a 0x49, or - the FIRST of the two confirms - nothing yet, declared with
registry.defer_reply so the policy does not cancel a trade the partner is about to complete.

Lock order (world.py "Locks"): world_lock -> combat_lock -> MapMonsters.lock ->
Trades.lock -> store.lock (db) -> send_lock. The commit needs BOTH players' combat locks
(their bags and wallets are mutated under them everywhere else: the combat driver credits
gold and loot under them, the quest accept / turn-in mirrors its items and gold under them):
it takes world_lock first, then the two combat locks in (uid, id) order - only the stall buy
(market.py) nests two combat locks too, in the same order, and every combat-lock holder that
wants Trades.lock (a death cancels the trade) holds just one - then Trades.lock and
store.lock. Market.open re-checks busy() while it holds Market.lock (Market.lock ->
Trades.lock; the reverse never happens: selling() reads the stall table lock-free). Nothing
here takes a combat lock, world_lock or Market.lock while holding Trades.lock, and every
packet is QUEUED (flush=False), also a reply on the requester's own thread.

Commit counter: every commit bumps session[COMMITS_KEY] of both sides (commits()), so a map
load that built its 0x03 before a partner's confirm committed can tell and rebuild it
(GameServer._map_transfer).

The escrow guards (trade-escrow-guards, trade.md 3.6) use available = owned - offered: an
item can be sold, used, equipped, dropped, banked or handed in for a quest only from the part
of the bag that is not on offer, and gold locked by 0x23 cannot be spent on a buy or a bank
deposit. The client itself cannot even reach an offered item (the 0x4B self path took it out
of its bag view), so a refusal here means a desync or a forged packet. Crafting, gathering,
reinforcement and the Card Deck are not guarded: the client applies them to its own bag view
(offered items are not in it), and the commit re-validates ownership, gold and capacity under
the locks anyway - a trade whose goods vanished is cancelled, never half-applied.

Audit (trade-audit-log): trade_log.jsonl next to accounts.json, one JSON object per event
(open / commit / cancel) with both uids and names, the offers, the gold and - for a commit -
both characters' bag totals and gold before and after. `!trade all` dumps the live trades.
"""
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field

import chat as chatmod
import en_content as EC
import inventory as invmod
import packets as P
import presence
import registry
import store as storemod
import world as worldmod

log = logging.getLogger('WS')

# ---- trade.md 2.1-2.4 / roadmap F8 constants ----
INVITE_TTL = 30.0            # seconds a 0x45 can be accepted (window 0x70 "No" sends nothing)
SPAM_SECS = 10.0             # a repeat request to the same player inside this is ignored
OFFER_MAX = 12               # the client's own grid cap ("There are no more spaces in trade slot")
QTY_MAX = {EC.TYPE_CONSUMABLE: 999, EC.TYPE_EQUIPMENT: 1, EC.TYPE_ETC: 99}   # dialog 0x74 limits
MANNER_FLOOR = -60           # the client refuses to trade at manner <= -60 (FUN 0x20 gate)
RESULT_REFUSING = 4          # S2C 0x47 4 "The player is rejecting trade."
RESULT_BUSY = 6              # S2C 0x47 6 "Trading."
LOG_NAME = 'trade_log.jsonl'
COMMITS_KEY = 'trade_commits'    # session: trades this session committed (commits())

OPEN, CONFIRMING, DONE, CANCELED = 'OPEN', 'CONFIRMING', 'DONE', 'CANCELED'

# Server-worded S2C 0x15 lines. NOT_HERE is the client's own text for the same gate.
NOT_HERE_TEXT = 'Both players must be in same field to trade items.'
EXPIRED_TEXT = 'The trade request has expired.'
CANT_NOW_TEXT = "You can't trade right now."
LOCKED_TEXT = 'The offer is locked. Cancel the trade to change it.'
NO_TRADE_ITEM_TEXT = "This item can't be traded."
NOT_OWNED_TEXT = "You don't have that many of that item."
SHORT_OF_GOLD_TEXT = 'You are short of gold.'
MISMATCH_TEXT = 'The trade did not match on both sides and was canceled.'
FAILED_TEXT = 'The trade could not be completed.'
IN_TRADE_TEXT = 'That item is offered in a trade.'
GOLD_IN_TRADE_TEXT = 'That gold is offered in a trade.'


# ------------------------------------------------------------------ records ---
@dataclass
class Offer:
    """One entry of a side's offer list, in 0x4B order. `words` is the stored 6-word block
    (inventory.pack_words) the bag instance is matched by (equipment only; stacks carry
    none), `opts` / `extra` the option words exactly as the client sent them in C2S 0x22 -
    echoed in the 0x4B, whose self path finds the bag item by a 12-byte compare."""
    item_id: int
    qty: int
    equip: bool
    words: list
    opts: list
    extra: int

    def matches(self, item_id, qty, words):
        return (self.item_id == item_id and self.qty == qty
                and (not self.equip or invmod.same_block(self.words, words)))

    def fields(self, owner_uid):
        """S2C 0x4B {item_id, count, owner_uid, opt_count, opt[], opt_extra} (11..21 B)."""
        opts = [int(w) & 0xFFFF for w in self.opts][:P.OPTION_LIST_MAX]
        return {'item_id': self.item_id & 0xFFFF, 'count': self.qty & 0xFFFF,
                'owner_uid': int(owner_uid) & 0xFFFFFFFF, 'opt_count': len(opts),
                'repeat[opt_count]': [{'opt': w} for w in opts], 'opt_extra': self.extra & 0xFFFF}

    def echo_row(self):
        """The row the client's C2S 0x25 carries for this entry (dev `!trade confirm`)."""
        opts = [int(w) & 0xFFFF for w in self.opts][:P.OPTION_LIST_MAX]
        return {'item_id': self.item_id, 'amount': self.qty, 'opt_count': len(opts),
                'repeat[opt_count]': [{'opt': w} for w in opts], 'attr_0c': self.extra}

    def as_log(self):
        out = {'id': self.item_id, 'qty': self.qty}
        if self.equip:
            out['w'] = list(self.words)
        return out


@dataclass
class Side:
    session: dict = field(repr=False)
    uid: int
    name: str
    offer: list = field(default_factory=list)
    gold: int = 0
    locked: bool = False
    final: bool = False


@dataclass
class Trade:
    tid: int
    a: Side                  # the requester
    b: Side                  # the one who accepted
    state: str = OPEN
    created: float = 0.0

    def side(self, session):
        if self.a.session is session:
            return self.a
        if self.b.session is session:
            return self.b
        return None

    def other(self, session):
        if self.a.session is session:
            return self.b
        if self.b.session is session:
            return self.a
        return None

    def sides(self):
        return (self.a, self.b)


@dataclass
class Invite:
    """A pending S2C 0x45: who asked (name as sent + session identity), whom, when. Session
    identities, not uids: a 2009 relogin's new session must not answer a prompt its old
    client showed (the party / messenger rule)."""
    name: str
    session: dict = field(repr=False)
    target: dict = field(repr=False)
    t: float


# ------------------------------------------------------------------ helpers ---
def uid_of(session):
    return P.session_uid(session) or 0


def name_of(session):
    return str((session or {}).get('char_name') or '')


def key(name):
    """Invite key of a character name (names are unique case-insensitively, names.py)."""
    return chatmod.name_text(name).lower()


def commits(session):
    """How many trades `session` has committed (bumped under Trades.lock by the commit).
    GameServer._map_transfer reads it before it builds the 0x03 and again after the map-load
    hooks: a different value means a partner's confirm committed in between, so the bag and
    gold the 0x03 was built from are stale."""
    return int((session or {}).get(COMMITS_KEY) or 0)


def descriptor(rec):
    """(item_id, qty, count, opts, extra) of a decoded C2S 0x22 / 0x26 item descriptor.
    Both send sites of 0x22 share the grammar family: the equipment drag (2008 0x46C6B5,
    2009 0x476BF5; 0x26 2008 0x46C92C / 2009 0x476E62) names the fields quantity /
    enchant_count / repeat{enchant} / enchant_last; the stackable quantity dialog 0x74
    (2008 0x469D9C, 2009 0x473E14) qty / socket_count (always 0) / item_extra. A 7-byte
    payload decodes as both; parse() returns the dialog form first, same values. `count` is
    the u8 as sent (the caller refuses > 5: the 0x4B echo would overflow the client's
    12-byte stack buffer, trade.md 1.1)."""
    item = int(rec.get('item_id', 0))
    qty = int(rec.get('quantity', rec.get('qty', 0)))
    count = int(rec.get('enchant_count', rec.get('socket_count', 0)))
    rows = rec.get('repeat[enchant_count]', rec.get('repeat[socket_count]', [])) or []
    opts = [int(next(iter(e.values()), 0)) if isinstance(e, dict) else int(e) for e in rows]
    extra = int(rec.get('enchant_last', rec.get('item_extra', 0)))
    return item, qty, count, opts, extra


def simulate(char, give, take, catalog=None):
    """None when `char` can hand over `give` and then receive `take` (Offers), else why not.
    Runs the model's own remove/add on a SNAPSHOT of the bag (store.snapshot: safe against a
    concurrent mutation), in the commit's order - the gives free the slots the takes use, so
    a swap of two full tabs still fits (trade.md 3.6 can_receive). The snapshot holds
    `inventory` alone, so the bag gets the character's pet count (a 2009 bagged pet takes an
    equipment-tab slot: inventory.pet_slots) - else an offer into a pet-filled tab passes
    here and the commit's real add refuses it."""
    scratch = {'inventory': storemod.snapshot(char.get('inventory') or {}), 'equipped': {},
               'gold': 0, 'victy': 0}
    bag = invmod.Inventory(scratch, catalog, pets=invmod.pet_slots(char, catalog))
    for o in give:
        words = o.words if o.equip else None
        if bag.remove(o.item_id, o.qty, words) != o.qty:
            return f'{EC.item_name(o.item_id) or o.item_id} x{o.qty} is no longer in the bag'
    for o in take:
        why = bag.fits(o.item_id, o.qty, o.words if o.equip else None)
        if why is not None:
            return why
        bag.add(o.item_id, o.qty, o.words if o.equip else None)
    return None


def _echo_rows(rec, which):
    return list(rec.get(f'repeat[{which}_item_count]', []) or [])


def echo_mismatch(offer, rows):
    """None when the C2S 0x25 rows (one side as the client's window 0x291 lists it) are the
    server's offer list: same order, id, amount and - equipment - block; else the first
    difference. Stack blocks are not compared: the quantity dialog always sends 0 / 0 while
    the client's list copies the bag record, and a stack has no instance to identify."""
    if len(rows) != len(offer):
        return f'{len(rows)} entries, the server has {len(offer)}'
    for i, (o, r) in enumerate(zip(offer, rows)):
        item, amount = int(r.get('item_id', 0)), int(r.get('amount', 0))
        if item != o.item_id or amount != o.qty:
            return f'entry {i}: {item} x{amount}, the server has {o.item_id} x{o.qty}'
        if o.equip:
            words = [int(next(iter(e.values()), 0)) if isinstance(e, dict) else int(e)
                     for e in r.get('repeat[opt_count]', []) or []]
            if len(words) > P.OPTION_LIST_MAX:
                return f'entry {i}: {len(words)} option words'
            if not invmod.same_block(invmod.block_from_wire(words, r.get('attr_0c', 0)), o.words):
                return f'entry {i}: block {words}/{r.get("attr_0c", 0)}, the server has {o.words}'
    return None


class Trades:
    def __init__(self, server):
        self.server = server
        self.lock = threading.RLock()
        self.trades = {}
        self.invites = {}
        self._next_tid = 1
        self._log_lock = threading.Lock()

    # ------------------------------------------------------------------ views ---
    def trade_of(self, session):
        """The live Trade `session` is a side of, or None (a DONE / CANCELED one is gone)."""
        if not isinstance(session, dict):
            return None
        with self.lock:
            t = session.get('trade')
            if t is None or self.trades.get(t.tid) is not t or t.side(session) is None:
                return None
            return t

    def busy(self, session):
        return self.trade_of(session) is not None

    def selling(self, session):
        """`session` has a flea-market stall open (market.py). No trade opens then: a trade
        filling the seller's bag is what makes a stall close keep items back (P7 stage 2
        review); the stall side refuses a Start while trading (market._open_gate).
        Market.selling is lock-free, so it may be asked under Trades.lock."""
        market = getattr(self.server, 'market', None)
        return bool(market is not None and session is not None and market.selling(session))

    def reachable(self, session):
        """A side / requester that can still take part: in world on this connection, not
        closed, kicked or replaced by a newer session of its account (2009 relogin)."""
        return bool(session is not None and worldmod.reachable(session)
                    and not self.server.world.superseded(session))

    def same_map(self, a, b):
        code = self.server.world.map_of(a)
        return code is not None and code == self.server.world.map_of(b)

    @property
    def log_path(self):
        """trade_log.jsonl next to the accounts file (offline tests: their temp directory)."""
        return os.path.join(os.path.dirname(os.path.abspath(self.server.store.path)), LOG_NAME)

    # --------------------------------------------------------------- sending ---
    def _push(self, target, key_, fields):
        """Queue one packet on `target` (never a socket write under Trades.lock)."""
        if target is None or target.get('sock') is None or target.get('closed') or target.get('kicked'):
            return False
        return self.server._push(target, key_, fields, 'TRADE', flush=False)

    def _notice(self, target, text):
        return self._push(target, '0x15', self.server.notice_fields(text, 'warn'))

    # ============================================================ 2.1 request ===
    def request(self, session, target_uid):
        """C2S 0x20 {target_uid} (popup 0x50 "Trade"; the client printed "You requested <B>
        to make an transaction." and waits for nothing). trade.md 2.1 in order: the target
        must be another reachable player on the requester's map (else 0x15 with the client's
        own "same field" text); refusing trades (privacy 'exchange') -> 0x47 {4}; trading or
        holding another player's prompt -> 0x47 {6}; the requester trading -> 0x47 {6} too;
        either side selling at a stall -> 0x15 "can't trade" to a selling requester, 0x47 {6}
        for a selling target; a repeat inside SPAM_SECS is ignored; else B gets 0x45 {A's
        name} (window 0x70)."""
        me = name_of(session)
        if not session.get('in_world') or not me:
            log.info(f'[TRADE] request from {session.get("username")!r} not in world - dropped')
            return None
        uid = int(target_uid or 0)
        with self.lock:
            if self.busy(session):
                self._push(session, '0x47', {'result': RESULT_BUSY})
                log.info(f'[TRADE] {me!r} requests uid {uid} while trading (0x47 6)')
                return 'requester busy'
            if self.selling(session):
                self._notice(session, CANT_NOW_TEXT)
                log.info(f'[TRADE] {me!r} requests uid {uid} while selling at a stall (0x15)')
                return 'selling'
            target = self.server.world.find(uid) if uid else None
            if target is not None and not presence.visible_to(target, session):
                target = None
            if (target is None or target is session or uid_of(target) == uid_of(session)
                    or not self.same_map(session, target) or self.server.world.superseded(target)):
                self._notice(session, NOT_HERE_TEXT)
                log.info(f'[TRADE] {me!r} requests uid {uid}: not on this map (0x15)')
                return 'not here'
            other = name_of(target)
            if session.get('dead') or target.get('dead'):
                self._notice(session, CANT_NOW_TEXT)
                log.info(f'[TRADE] {me!r} requests {other!r}: a corpse cannot trade (0x15)')
                return 'dead'
            if self._manner(session) <= MANNER_FLOOR or self._manner(target) <= MANNER_FLOOR:
                # The client gates both manners itself (trade.md 1.2); a forged request.
                log.warning(f'[TRADE] {me!r} requests {other!r}: manner <= {MANNER_FLOOR} - dropped')
                return 'manner'
            if self.server.blacklist_drops(target, session):
                # P12 bl-3, BLACKLIST_FILTER 'silent' (blch A.6 / F-B4): the target's client would
                # drop the 0x45 (FUN_00484190 at 0x478524). No prompt is recorded, so no other
                # requester is told "busy" (0x47 6) for INVITE_TTL; nothing back to this one.
                log.info(f'[TRADE] {me!r} requests {other!r}: blacklisted - dropped (no prompt recorded)')
                return 'blacklisted'
            if self.server.refuses(target, 'exchange', session):
                # The privacy flag, or BLACKLIST_FILTER 'refuse' (blacklist.py).
                self._push(session, '0x47', {'result': RESULT_REFUSING})
                log.info(f'[TRADE] {me!r} requests {other!r}: refuses trades (0x47 4)')
                return 'refused'
            now = time.monotonic()
            self._prune(now)
            pending = self.invites.get(uid_of(target))
            if pending is not None and pending.target is not target:
                pending = None                      # a prompt of the account's old session
            if (self.busy(target) or self.selling(target)
                    or (pending is not None and pending.session is not session)):
                self._push(session, '0x47', {'result': RESULT_BUSY})
                log.info(f'[TRADE] {me!r} requests {other!r}: busy (0x47 6)')
                return 'busy'
            if pending is not None and now - pending.t < SPAM_SECS:
                log.info(f'[TRADE] {me!r} requests {other!r} again after {now - pending.t:.1f} s - ignored')
                return 'spam'
            self.invites[uid_of(target)] = Invite(me, session, target, now)
            self._push(target, '0x45', {'requester_name': chatmod.name_bytes(me)})
        log.info(f'[TRADE] {me!r} (uid {uid_of(session)}) requests a trade with {other!r} (0x45)')
        return 'requested'

    def _manner(self, session):
        try:
            return int(self.server.store.manner(session.get('username')) or 0)
        except (AttributeError, TypeError, ValueError):
            return 0

    def _prune(self, now):
        for target_uid, inv in list(self.invites.items()):
            if now - inv.t >= INVITE_TTL:
                del self.invites[target_uid]

    def _forget(self, session):
        """Drop every prompt `session` made or received (its client or dialog is gone)."""
        for target_uid, inv in list(self.invites.items()):
            if inv.target is session or inv.session is session:
                del self.invites[target_uid]

    # ============================================================= 2.2 accept ===
    def accept(self, session, requester_name):
        """C2S 0x21 {requester_name} (window 0x70 OK echoes the label 0x45 set; "No" sends
        nothing). The prompt must be this session's, name-matched and younger than
        INVITE_TTL; the requester still reachable on the same map; neither side trading or
        selling at a stall (0x15 "can't trade" to a selling accepter, 0x47 {6} otherwise).
        Then the Trade opens and each client gets 0x46 {partner name} (window 0x4E "TRADE",
        both lists freed, gold 0). Never a 0x46 to a session whose trade is open: it frees
        the offer lists WITHOUT returning the items (trade.md 2.2 step 3)."""
        me = name_of(session)
        wanted = chatmod.name_text(requester_name)
        if not session.get('in_world') or not me:
            log.info(f'[TRADE] accept from {session.get("username")!r} not in world - dropped')
            return None
        with self.lock:
            now = time.monotonic()
            inv = self.invites.get(uid_of(session))
            if (inv is None or inv.target is not session or key(inv.name) != key(wanted)
                    or now - inv.t >= INVITE_TTL):
                self._notice(session, EXPIRED_TEXT)
                log.info(f'[TRADE] {me!r} accepts {wanted!r}: no live prompt (0x15)')
                return 'expired'
            del self.invites[uid_of(session)]
            host = inv.session
            if self.busy(session) or (host is not None and self.busy(host)):
                self._push(session, '0x47', {'result': RESULT_BUSY})
                log.info(f'[TRADE] {me!r} accepts {inv.name!r}: already trading (0x47 6)')
                return 'busy'
            if self.selling(session):
                self._notice(session, CANT_NOW_TEXT)
                log.info(f'[TRADE] {me!r} accepts {inv.name!r} while selling at a stall (0x15)')
                return 'selling'
            if self.selling(host):
                self._push(session, '0x47', {'result': RESULT_BUSY})
                log.info(f'[TRADE] {me!r} accepts {inv.name!r}, who is selling at a stall (0x47 6)')
                return 'busy'
            if not self.reachable(host) or not self.same_map(session, host):
                self._notice(session, NOT_HERE_TEXT)
                log.info(f'[TRADE] {me!r} accepts {inv.name!r}, who left the map (0x15)')
                return 'requester gone'
            if session.get('dead') or host.get('dead'):
                self._notice(session, CANT_NOW_TEXT)
                log.info(f'[TRADE] {me!r} accepts {inv.name!r}: a corpse cannot trade (0x15)')
                return 'dead'
            t = Trade(self._new_tid(), Side(host, uid_of(host), name_of(host)),
                      Side(session, uid_of(session), me), OPEN, now)
            self.trades[t.tid] = t
            host['trade'] = t
            session['trade'] = t
            self._push(host, '0x46', {'partner_name': chatmod.name_bytes(me)})
            self._push(session, '0x46', {'partner_name': chatmod.name_bytes(t.a.name)})
        log.info(f'[TRADE] #{t.tid} open: {t.a.name!r} (uid {t.a.uid}) <-> {me!r} (uid {t.b.uid}) (0x46 both)')
        self.audit('open', t)
        return 'open'

    def _new_tid(self):
        tid = self._next_tid
        while tid in self.trades:
            tid += 1
        self._next_tid = tid + 1
        return tid

    # ============================================================== 2.4 add ===
    def add(self, session, item, qty, opts=(), extra=0, count=None):
        """C2S 0x22 (a bag item dropped on window 0x4E: equipment at once with qty 1, a stack
        through the quantity dialog 0x74). Refused in silence plus a 0x15 line (the client
        changes nothing before the 0x4B, trade.md 2.4 step 6): no open trade, either side
        locked (Q4: no unlock packet exists), a Type other than 0/1/2, a Cash item (def+0x1F0,
        C24) or a KR NotTrade one, a quantity outside the dialog's limits, more than the bag
        holds beyond what is already offered (equipment: an instance with that exact block),
        12 entries. The partner's bag not fitting the swap -> S2C 0x4D to the offerer ("no
        more spaces in the other player's item slot"). Success: the entry is escrowed and
        BOTH get 0x4B {owner_uid = offerer}."""
        me = name_of(session)
        item, qty, extra = int(item), int(qty), int(extra) & 0xFFFF
        opts = [int(w) & 0xFFFF for w in (opts or [])]
        count = len(opts) if count is None else int(count)
        what = f'{EC.item_name(item) or item} x{qty}'
        with self.lock:
            t = self.trade_of(session)
            if t is None:
                log.info(f'[TRADE] {me!r} offers {what} with no open trade - ignored')
                return 'no trade'
            side, other = t.side(session), t.other(session)
            if t.state != OPEN or side.locked or other.locked:
                self._notice(session, LOCKED_TEXT)
                log.info(f'[TRADE] #{t.tid} {me!r} offers {what} after a lock - refused (Q4)')
                return 'locked'
            why = self._offer_refusal(session, side, item, qty, opts, extra, count)
            if why is not None:
                reason, text = why
                if text:
                    self._notice(session, text)
                log.info(f'[TRADE] #{t.tid} {me!r} offers {what}: refused - {reason}')
                return 'refused'
            equip = EC.items().type_of(item) == EC.TYPE_EQUIPMENT
            words = invmod.block_from_wire(opts, extra) if equip else list(invmod.ZERO_BLOCK)
            offer = Offer(item, qty, equip, words, opts, extra)
            char = self.server._session_char(other.session)
            full = simulate(char, other.offer, side.offer + [offer]) if char is not None else 'no character'
            if full is not None:
                self._push(session, '0x4D', {})
                log.info(f'[TRADE] #{t.tid} {me!r} offers {what}: {other.name!r} has no room ({full}) -> 0x4D')
                return 'no room'
            side.offer.append(offer)
            fields = offer.fields(side.uid)
            self._push(session, '0x4B', fields)
            self._push(other.session, '0x4B', fields)
            entry = len(side.offer) - 1
        log.info(f'[TRADE] #{t.tid} {me!r} offers {what}' + (f' block {words}' if equip else '')
                 + f' (entry {entry}; 0x4B to both)')
        return 'added'

    def _offer_refusal(self, session, side, item, qty, opts, extra, count):
        """(log reason, player text or None) when this 0x22 must be refused, else None."""
        if count > P.OPTION_LIST_MAX or len(opts) > P.OPTION_LIST_MAX:
            return f'{count} option words (max {P.OPTION_LIST_MAX}: the 0x4B echo overflows)', None
        info = EC.items().get(item)
        if info is None:
            return f'item not in the EN client catalog (1..{EC.item_max_id()})', None
        if info.type not in QTY_MAX:
            return f'Type {info.type} is no tradeable bag item', NO_TRADE_ITEM_TEXT
        if info.is_cash:
            return 'cash item (hii Cash, itemdef+0x1F0 != 0: the client never offers one, C24)', NO_TRADE_ITEM_TEXT
        # the KR row of this EN id (en_content.gamedef_item: 2009 ids above 4248 are KR + 4, C3)
        row = EC.gamedef_item(item) if item <= EC.item_max_id() else None
        if row and int(row.get('NotTrade') or 0):
            return 'KR gamedef NotTrade', NO_TRADE_ITEM_TEXT
        limit = QTY_MAX[info.type]
        if not 1 <= qty <= limit:
            return f'qty {qty} outside 1..{limit} for Type {info.type}', None
        if len(side.offer) >= OFFER_MAX:
            return f'{OFFER_MAX} entries already (the client grid cap)', None
        bag = self.server._bag(session)
        if bag is None:
            return 'no character', None
        words = invmod.block_from_wire(opts, extra) if info.type == EC.TYPE_EQUIPMENT else None
        have = bag.count(item, words)
        offered = self._offered(side, item, words)
        if have - offered < qty:
            what = f'block {words}' if words is not None else 'the id'
            return f'the bag holds {have} ({what}), {offered} already offered', NOT_OWNED_TEXT
        return None

    @staticmethod
    def _offered(side, item, words=None):
        """How many of `item` this side has on offer (equipment with `words`: only entries
        with that exact block)."""
        return sum(o.qty for o in side.offer
                   if o.item_id == item and (words is None or not o.equip or invmod.same_block(o.words, words)))

    # =========================================================== 2.5 remove ===
    def remove(self, session, item, qty, opts=(), extra=0, slot=None):
        """C2S 0x26 {item, qty, trade_slot_index, block}: the client ALREADY put the item back
        in its bag and dropped the node. Found by index when that entry matches, else the
        first entry with the same content (a multi-node drag counts every index against the
        list as it was before the drag); the partner gets 0x4C {the entry's current index}
        - never the offerer, whose own grid is already right. Removing after a lock cannot
        be undone client-side (no unlock packet), so it cancels the trade (Q4)."""
        me = name_of(session)
        item, qty, extra = int(item), int(qty), int(extra) & 0xFFFF
        opts = [int(w) & 0xFFFF for w in (opts or [])]
        what = f'{EC.item_name(item) or item} x{qty}'
        with self.lock:
            t = self.trade_of(session)
            if t is None:
                log.info(f'[TRADE] {me!r} takes back {what} with no open trade - ignored')
                return 'no trade'
            side, other = t.side(session), t.other(session)
            after_lock = t.state != OPEN or side.locked or other.locked
            if not after_lock:
                words = invmod.block_from_wire(opts, extra)
                pos = None
                if slot is not None and 0 <= int(slot) < len(side.offer) \
                        and side.offer[int(slot)].matches(item, qty, words):
                    pos = int(slot)
                if pos is None:
                    pos = next((i for i, o in enumerate(side.offer) if o.matches(item, qty, words)), None)
                if pos is None:
                    log.warning(f'[TRADE] #{t.tid} {me!r} takes back {what} (slot {slot}) that is not '
                                f'on offer - ignored (the 0x25 echo cancels a real desync)')
                    return 'not offered'
                del side.offer[pos]
                self._push(other.session, '0x4C', {'slot_index': pos})
        if after_lock:
            self.cancel(session, f'{what} taken back after a lock (the client already changed its grid)',
                        text=LOCKED_TEXT)
            return 'canceled'
        log.info(f'[TRADE] #{t.tid} {me!r} takes back {what} (entry {pos}; 0x4C to {other.name!r})')
        return 'removed'

    # ============================================================= 2.6 lock ===
    def lock_offer(self, session, gold):
        """C2S 0x23 {u64 gold} (window 0x4E OK; the client checked the gold against its own
        copy and disabled the button). More than the stored wallet - or a negative entry,
        which _atol sign-extends to >= 2^63 - has no "lock refused" packet, so the trade is
        cancelled (0x49 both) with a line to the locker. Else the side locks and BOTH get
        0x48 {locker uid, gold}: the self echo is what opens window 0x291 on the second
        locker. Both locked -> CONFIRMING."""
        me = name_of(session)
        gold = int(gold)
        with self.lock:
            t = self.trade_of(session)
            if t is None or t.state != OPEN:
                log.info(f'[TRADE] {me!r} locks {gold} gold with no open trade - ignored')
                return 'no trade'
            side, other = t.side(session), t.other(session)
            if side.locked:
                log.info(f'[TRADE] #{t.tid} {me!r} locks twice - ignored (its OK button is disabled)')
                return 'already locked'
            wallet = self.server._wallet_of(session)
            have = wallet.gold if wallet is not None else 0
            short = gold >= 1 << 63 or gold > have
            if not short:
                side.gold, side.locked = gold, True
                fields = {'confirmer_uid': side.uid & 0xFFFFFFFF, 'gold': gold}
                self._push(session, '0x48', fields)
                self._push(other.session, '0x48', fields)
                if other.locked:
                    t.state = CONFIRMING
        if short:
            self.cancel(session, f'locked {gold} gold with {have}', text=SHORT_OF_GOLD_TEXT)
            return 'short of gold'
        log.info(f'[TRADE] #{t.tid} {me!r} locks {len(side.offer)} entr(y/ies) + {gold} gold (0x48 to both)'
                 + ('; both locked -> CONFIRMING (window 0x291)' if t.state == CONFIRMING else ''))
        return 'locked'

    # ========================================================= 2.7 confirm ===
    def confirm(self, session, rec):
        """C2S 0x25 {my_gold, my items, partner_gold, partner items} from window 0x291
        "Trade": the WHOLE deal as this client shows it. Checked against the server's offers
        (echo_mismatch; gold exact) - a mismatch is a desync (a missed 0x4B / 0x4C, the
        0x26 bag-full hazard, the gold box edited after the lock, or a forged packet) and
        cancels the trade. The first confirm waits in silence (registry.defer_reply: 0x291
        keeps Cancel enabled); the second commits (commit()) and both get 0x4A {their new
        absolute gold}. No trade on record -> 0x49 (a stale window, trade.md 2.8 step 2.2).

        Locks: world_lock, then both players' combat locks in (uid, id) order, then
        Trades.lock (module docstring) - taken for every confirm, so the commit sees both
        bags and wallets frozen and nothing else can change them until it is persisted."""
        me = name_of(session)
        t = self.trade_of(session)
        if t is None:
            self._push(session, '0x49', {})
            log.info(f'[TRADE] {me!r} confirms with no trade on record -> 0x49 (stale window)')
            return 'no trade'
        partner = t.other(session).session
        first, second = sorted((session, partner), key=lambda s: (uid_of(s), id(s)))
        world_lock = getattr(self.server, 'world_lock', None) or threading.RLock()
        entry, why = None, None
        with world_lock, self.server._combat_lock(first), self.server._combat_lock(second):
            with self.lock:
                if self.trade_of(session) is not t:
                    # Cancelled between the look-up and the locks: that cancel queued this
                    # client's 0x49 already.
                    registry.defer_reply('the trade was cancelled meanwhile (its 0x49 answered)')
                    log.info(f'[TRADE] #{t.tid} {me!r} confirms a trade cancelled meanwhile')
                    return 'canceled'
                side, other = t.side(session), t.other(session)
                if side.final:
                    registry.defer_reply('a repeated confirm: the first one is waiting')
                    log.info(f'[TRADE] #{t.tid} {me!r} confirms twice - ignored')
                    return 'waiting'
                if t.state != CONFIRMING:
                    why = f'confirm in state {t.state} (both sides must be locked first)'
                elif not (self.reachable(side.session) and self.reachable(other.session)):
                    # a side mid-way out (a 2009 relogin replaced it, its socket closed): its
                    # own leave hook cancels too; never commit to a client that cannot see it
                    why = 'a side is no longer in the world'
                elif (int(rec.get('my_gold', 0)) != side.gold
                      or int(rec.get('partner_gold', 0)) != other.gold):
                    why = (f'gold {rec.get("my_gold")}/{rec.get("partner_gold")} on the client, '
                           f'{side.gold}/{other.gold} locked')
                else:
                    diff = echo_mismatch(side.offer, _echo_rows(rec, 'my'))
                    if diff is not None:
                        why = f'own list: {diff}'
                    else:
                        diff = echo_mismatch(other.offer, _echo_rows(rec, 'partner'))
                        why = None if diff is None else f'partner list: {diff}'
                if why is None:
                    side.final = True
                    if not other.final:
                        registry.defer_reply("first confirm: the partner's confirm commits (0x4A to both)")
                        log.info(f'[TRADE] #{t.tid} {me!r} confirms; waiting for {other.name!r}')
                        return 'waiting'
                    refused, entry = self.commit(t)
                    if refused is not None:
                        why = f'commit refused: {refused}'
        if why is None:
            log.info(f'[TRADE] #{t.tid} committed: {t.a.name!r} gave {len(t.a.offer)} entr(y/ies) + '
                     f'{t.a.gold} gold, {t.b.name!r} gave {len(t.b.offer)} + {t.b.gold} gold; persisted; '
                     f'0x4A to both')
            self.audit('commit', t, **entry)
            return 'committed'
        log.warning(f'[TRADE] #{t.tid} {me!r} confirm refused: {why} - cancel')
        self.cancel(session, why, text=FAILED_TEXT if why.startswith('commit') else MISMATCH_TEXT)
        return 'canceled'

    def commit(self, t):
        """Apply a trade whose two sides are final (caller holds world_lock, both combat locks
        and Trades.lock). Re-validated first - ownership of every offer, gold <= wallet,
        both bags' capacity after the swap (simulate, gives before takes) and the u64 gold
        ceiling - because inventories can move between the lock and the confirm. Then both
        records change under store.lock and are PERSISTED (one atomic accounts.json write:
        the file holds the whole trade or none of it) BEFORE the 0x4A goes out. The trade
        is DONE and forgotten in the same critical section, so it runs exactly once.
        Returns (None, audit fields) or (why, None) - the caller cancels."""
        server, store = self.server, self.server.store
        ca, cb = server._session_char(t.a.session), server._session_char(t.b.session)
        if ca is None or cb is None:
            return 'a side has no character', None
        with store.lock:
            wa, wb = invmod.Wallet(ca), invmod.Wallet(cb)
            for s, w in ((t.a, wa), (t.b, wb)):
                if s.gold > w.gold:
                    return f'{s.name!r} locked {s.gold} gold and holds {w.gold}', None
            why = simulate(ca, t.a.offer, t.b.offer) or simulate(cb, t.b.offer, t.a.offer)
            if why is not None:
                return why, None
            gold_a, gold_b = wa.gold - t.a.gold + t.b.gold, wb.gold - t.b.gold + t.a.gold
            if max(gold_a, gold_b) > invmod.GOLD_MAX:
                return 'the gold would overflow u64', None
            before = {t.a.name: self._holdings(ca), t.b.name: self._holdings(cb)}
            saved = [(char, storemod.snapshot(char['inventory'])) for char in (ca, cb)]
            try:
                for char, give, take in ((ca, t.a.offer, t.b.offer), (cb, t.b.offer, t.a.offer)):
                    bag = invmod.Inventory(char)
                    for o in give:
                        if bag.remove(o.item_id, o.qty, o.words if o.equip else None) != o.qty:
                            raise RuntimeError(f'{o.item_id} x{o.qty} vanished mid-commit')
                    for o in take:
                        if bag.add(o.item_id, o.qty, o.words if o.equip else None) is None:
                            raise RuntimeError(f'{o.item_id} x{o.qty} did not fit mid-commit')
            except Exception as e:                  # noqa: BLE001 - never half a trade
                # simulate() ran the same code on the same state, so this is a bug; put both
                # bags back as they were (dict identity kept: live Inventory objects hold it)
                for char, inv in saved:
                    char['inventory'].update(inv)
                log.exception(f'[TRADE] #{t.tid} commit rolled back')
                return f'rolled back ({e})', None
            wa.gold, wb.gold = gold_a, gold_b
            store.dirty = True
            try:
                # The short replace backoff and writer wait only: this runs under world_lock
                # and store.lock, and a failure or a busy file is retried by the store
                # (review of livetest bug 7).
                store.save_now(delays=storemod.QUICK_REPLACE_DELAYS,
                               wait=storemod.QUICK_REPLACE_BUDGET_SECS)
            except Exception:                       # noqa: BLE001 - the model is committed; the next flush retries
                log.exception(f'[TRADE] #{t.tid}: the accounts.json save failed; kept dirty for the next flush')
                store.mark_dirty(f'trade {t.tid}')
            after = {t.a.name: self._holdings(ca), t.b.name: self._holdings(cb)}
        for s in t.sides():
            s.session[COMMITS_KEY] = commits(s.session) + 1
        t.state = DONE
        self._close(t)
        self._push(t.a.session, '0x4A', {'gold': gold_a})
        self._push(t.b.session, '0x4A', {'gold': gold_b})
        return None, {'before': before, 'after': after}

    @staticmethod
    def _holdings(char):
        """{'gold', 'items'} of a record for the audit log (bag totals by id)."""
        bag = invmod.Inventory(char)
        return {'gold': invmod.Wallet(char).gold,
                'items': {str(k): v for k, v in sorted(bag.totals().items())}}

    def _close(self, t):
        """Forget a finished trade (caller holds Trades.lock)."""
        self.trades.pop(t.tid, None)
        for s in t.sides():
            if s.session.get('trade') is t:
                s.session.pop('trade', None)

    # ========================================================== 2.8 cancel ===
    def cancel_request(self, session):
        """C2S 0x24 (End / Cancel on 0x4E or 0x291; the client disabled both and waits):
        the trade is cancelled - 0x49 to both, the escrow simply released (the records were
        never touched). With no trade on record the sender still gets its 0x49, or its window
        could never close (trade.md 2.8 step 2.2)."""
        if self.cancel(session, 'canceled by ' + (name_of(session) or '?')):
            return 'canceled'
        self._push(session, '0x49', {})
        log.info(f'[TRADE] {name_of(session)!r} cancels with no trade on record -> 0x49 (stale window)')
        return 'no trade'

    def cancel(self, session, reason, *, notify_self=True, text=None):
        """Cancel `session`'s trade: 0x49 to both sides still connected (notify_self False:
        not to `session` - its socket is gone, or the caller answers it), an optional 0x15
        line to `session`, the Trade forgotten. Server inventories are untouched: offered
        items were only in escrow, and each client puts its own offered items back itself.
        Returns True when there was a trade. Never takes a combat lock: a death (combat lock
        held) and every hook call it."""
        with self.lock:
            t = self.trade_of(session)
            if t is None:
                return False
            t.state = CANCELED
            self._close(t)
            told = []
            for s in t.sides():
                if s.session is session and not notify_self:
                    continue
                if self._push(s.session, '0x49', {}):
                    told.append(s.name)
            if text and notify_self:
                self._notice(session, text)
        log.info(f'[TRADE] #{t.tid} {t.a.name!r} <-> {t.b.name!r} canceled ({reason}); 0x49 to {told}')
        self.audit('cancel', t, reason=reason)
        return True

    def gone(self, session, reason):
        """Leaving the world for good / the connection closed (trade.md 2.9 step 1): the
        partner gets its 0x49, and every prompt naming the session is dropped."""
        with self.lock:
            self._forget(session)
        self.cancel(session, reason, notify_self=False)

    def map_load(self, session, reason):
        """A server map load (trade.md 2.9 step 2), BEFORE its lead packet: both sides get
        their 0x49 (the mover's while window 0x4E still exists to put its offered items back),
        and every prompt the session made or received is dropped - the 0x08 / 0x03 close the
        invitee's window 0x70 with the requester's entity, and a prompt left behind would
        answer later requesters 0x47 {6} or open a trade across two maps. Returns True when a
        trade was cancelled."""
        with self.lock:
            self._forget(session)
        return self.cancel(session, reason)

    # ================================================== trade-escrow-guards ===
    def offered(self, session, item, words=None):
        """How many of `item` (equipment with `words`: that exact block) `session` has on
        offer in its trade."""
        with self.lock:
            t = self.trade_of(session)
            return 0 if t is None else self._offered(t.side(session), int(item), words)

    def escrow_refusal(self, session, item, count=1, words=None):
        """None when `count` of `item` (equipment with `words`: that exact block) can leave
        the bag without touching what is offered, else why not (available = owned - offered,
        trade.md 3.6)."""
        n = self.offered(session, item, words)
        if not n:
            return None
        bag = self.server._bag(session)
        have = bag.count(item, words) if bag is not None else 0
        if have - n >= int(count):
            return None
        t = self.trade_of(session)
        return (f'{n} of the {have} x {EC.item_name(item) or item} are offered in trade '
                f'#{t.tid if t else "?"} (available {max(0, have - n)}, needs {count})')

    def gold_escrow(self, session):
        """The gold `session` locked into its trade (0 before its 0x23)."""
        with self.lock:
            t = self.trade_of(session)
            side = t.side(session) if t is not None else None
            return side.gold if side is not None and side.locked else 0

    def gold_refusal(self, session, cost):
        """None when `cost` gold can be spent without touching the locked trade gold."""
        locked = self.gold_escrow(session)
        if not locked:
            return None
        wallet = self.server._wallet_of(session)
        have = wallet.gold if wallet is not None else 0
        if have - locked >= int(cost):
            return None
        return f'{locked} of {have} gold are locked in a trade (needs {cost})'

    # ===================================================== trade-audit-log ===
    def audit(self, event, t, **extra):
        """One JSON line per trade event in trade_log.jsonl (append-only, for dupe forensics):
        both sides' uid / name / account / offers / gold / flags, plus the event's own fields
        (a commit's before / after holdings, a cancel's reason). A write failure is logged,
        never raised: the trade itself is already decided."""
        entry = {'t': time.strftime('%Y-%m-%dT%H:%M:%S'), 'event': event, 'trade': t.tid, 'state': t.state}
        for label, s in (('a', t.a), ('b', t.b)):
            entry[label] = {'uid': s.uid, 'name': s.name, 'account': s.session.get('username'),
                            'gold': s.gold, 'locked': s.locked, 'final': s.final,
                            'offer': [o.as_log() for o in s.offer]}
        entry.update(extra)
        try:
            line = json.dumps(entry, ensure_ascii=False)
            with self._log_lock, open(self.log_path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except (OSError, TypeError, ValueError) as e:
            log.warning(f'[TRADE] audit line for #{t.tid} {event} not written: {e}')

    # ------------------------------------------------------------- dev view ---
    def describe(self, session):
        """Lines for `!trade`: this session's trade (both sides, entries, gold, flags) and
        the prompt waiting for it. One side per line (an S2C 0x15 line holds 88 bytes)."""
        lines = []
        with self.lock:
            t = self.trade_of(session)
            if t is None:
                lines.append('no trade')
            else:
                lines.append(f'trade #{t.tid} {t.state}, {time.monotonic() - t.created:.0f} s:')
                for s in t.sides():
                    offer = ', '.join(f'{o.item_id}x{o.qty}' for o in s.offer) or '-'
                    flags = ' '.join(f for f, on in (('locked', s.locked), ('final', s.final)) if on)
                    lines.append(f' {s.name}: {offer} + {s.gold}g {flags}'.rstrip())
            inv = self.invites.get(uid_of(session))
            if inv is not None and inv.target is session:
                left = INVITE_TTL - (time.monotonic() - inv.t)
                lines.append(f'prompt from {inv.name} ({max(0.0, left):.0f} s)')
        return lines

    def describe_all(self):
        """Lines for `!trade all` (the admin dump of trade-audit-log): every live trade."""
        with self.lock:
            trades = sorted(self.trades.values(), key=lambda t: t.tid)
            lines = [f'{len(trades)} live trade(s), {len(self.invites)} prompt(s) pending']
            for t in trades:
                sides = ' <-> '.join(
                    f'{s.name}[{len(s.offer)}+{s.gold}g{" L" if s.locked else ""}{" F" if s.final else ""}]'
                    for s in t.sides())
                lines.append(f'#{t.tid} {t.state}: {sides}')
        return lines


# ------------------------------------------------------------------- hooks ---
def register(hooks, trades):
    """The lifecycle hooks (trade.md 2.9, roadmap F5): a server map load (portal, warp,
    village transfer, revive, a repeated C2S 0x2B) cancels BEFORE its lead packet - 0x08 /
    0x03 destroy the partner's entity and window 0x4E, and the 0x49 has to land while the
    client can still put its offered items back; leaving the world or closing the
    connection cancels too (the partner's window closes). Each also drops every prompt that
    names the session (Trades.map_load / gone)."""
    def before_server_map_load(server, session, map_code=None, reason=None, **_):
        trades.map_load(session, f'map load ({reason}) to {map_code}')

    def on_leave_world(server, session, reason=None, **_):
        trades.gone(session, reason or 'left the world')

    def on_disconnect(server, session, reason=None, **_):
        trades.gone(session, reason or 'disconnect')

    hooks.register(worldmod.BEFORE_SERVER_MAP_LOAD, before_server_map_load)
    hooks.register(worldmod.ON_LEAVE_WORLD, on_leave_world)
    hooks.register(worldmod.ON_DISCONNECT, on_disconnect)
    return before_server_map_load, on_leave_world, on_disconnect
