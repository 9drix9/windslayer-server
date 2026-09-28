#!/usr/bin/env python3
"""
stall.py - personal item stalls, the persisted model and pure rules (P7 stage 2)
===============================================================================
shop_storage-stall-registry (the persisted escrow) and the pure halves of
shop_storage-stall-open-close / -browse-buy / -presence (docs/systems/shop_storage.md 1.6,
F9-F14, 3.1-3.5). No sessions and no sockets here: market.py is the runtime half (the
registry of open stalls, the handlers, the hooks). The store's migration imports this
module, so it must not import presence / records (records -> store -> stall would be a
cycle at load; the social.py rule).

    import stall as ST
    ST.ensure(char)                          # char['stall_escrow'] = [] (normalized)
    title, rows, why = ST.parse_open(rec)    # decoded C2S 0x5E -> (title, [Entry], refusal)
    ST.list_fields(uid, title, entries)      # S2C 0x87 {1, n, owner, title, rows}
    ST.sale_fields(gold, entry, qty)         # S2C 0x88 {1, ...} (buyer) / 0x89 (seller)

Every stall packet is wire-identical in the two builds (spec_2009 C2S 0x475A25/0x5E,
0x473E14/0x5F, 0x473E14/0x60, 0x4507FC/0x61, 0x473E14/0x62 and S2C 0x82..0x89: "identical
(fingerprint)"); the 2009 C2S 0x82/0x83/0x85/0x86 are pet requests, a different direction.

Persisted shape (character record; store migration, one-time accounts.json.bak-pre-p7)
-------------------------------------------------------------------------------------
    stall_escrow  [{id, qty, price, w?}]   the items of the character's OPEN stall, in list
                                           order: they left the bag at 0x5E (the client took
                                           them out of its own bag at registration,
                                           FUN_00467140) and go back on close / map load /
                                           disconnect (the client's FUN_004665e0 ->
                                           FUN_00467200). `w` (equipment only) is the stored
                                           6-word block. Empty whenever no stall is open: a
                                           non-empty list at login is a crash leftover (or
                                           what a full bag could not take back) and is merged
                                           into the bag then (F14.5, market.Market.recover).

Client limits (both builds, read from the decompilation)
--------------------------------------------------------
- MAX_ITEMS 6: registering refuses "No more items can be added." once window 0x259's list
  holds more than 5 nodes (2008 FUN_00469090 case 100 / 2009 FUN_00472f40 case 100:
  `5 < list count`), and one registration adds at most one node (FUN_00467300 merges a
  stack into the same-id node up to 999 / 99 and appends one node for the rest). This
  replaces the design's provisional STALL_MAX_ITEMS = 20 (shop_storage Q10).
- SIGN_SPRITE 1: live trade#16 drew the signboard, the orange aura and the orange name for
  S2C 0x85 sprite 1; 0x168 (the design's provisional Q9 value) and 0 draw no board (C48).
- Title: str[25] edit buffer, so at most 24 bytes + NUL (0x85 / 0x87 / the 0x04 record).
- Stall use needs manner >= -79 (FUN_00466e30; FUN_00467680 with npc flag 0). Stall
  purchases get no manner discount (FUN_00467680 param_7 = 0): price x qty, in u64 here
  (the client's own check is 32-bit).
"""
from dataclasses import dataclass

import en_content as EC
import inventory as invmod
import packets as P

# ---- shop_storage.md 3.4 constants (see the module docstring for the sources) ----
STALL_MAPS = frozenset((EC.STALL_MAP,))      # 9701 stage97_01 - the only flea market in EN
OPEN_STALL_SKILL = 194                       # item 194 "Open Stall" (2009 "Private Shop")
SIGN_SPRITE = 1
MAX_ITEMS = 6
TITLE_MAX = 24
MANNER_FLOOR = -79
QTY_MAX = {EC.TYPE_CONSUMABLE: 999, EC.TYPE_EQUIPMENT: 1, EC.TYPE_ETC: 99}
PRICE_MAX = 0xFFFFFFFF                       # u32 unit_price

# S2C 0x82 ItemStallOpenResult: 1 opens selling mode; 2 "Item stall opening failed."; 9
# "Invalid information transaction. This violation will be recorded" (a tampered list).
OPEN_OK, OPEN_FAILED, OPEN_TAMPERED = 1, 2, 9
# S2C 0x87 StallItemListResponse (C31): 1 the list; 6 "Open stall is opened. Close the Open
# stall and try again."; anything else (and 1 with count 0) "The shop is closed or adjusting."
LIST_OK, LIST_CLOSED, LIST_OWN_STALL = 1, 0, 6
# S2C 0x88 StallBuyResult: 1 bought; anything else "You failed to buy the items."
BUY_OK, BUY_FAILED = 1, 0


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def cut_title(title):
    """The stall title as the wire carries it: raw bytes (cp949), NUL-cut, at most 24 bytes
    without splitting a double-byte character (packets.cut_text)."""
    return P.cut_text(P.to_bytes(title or b'').split(b'\x00', 1)[0], TITLE_MAX)


# ------------------------------------------------------------------ entries ---
@dataclass
class Entry:
    """One node of a stall list: `words` is the stored 6-word block the bag instance is
    matched by (equipment; a stack carries none and is matched by id alone)."""
    item_id: int
    qty: int
    price: int
    equip: bool
    words: list

    def wire(self):
        """(opts, extra) of the item descriptor: the leading non-zero option words and the
        trailing word, exactly what the client's bag record sends (inventory.wire_words; a
        stack's quantity dialog sends 0 / 0)."""
        if not self.equip:
            return [], 0
        return invmod.wire_words(self.words), invmod.pack_words(self.words)[5]

    def bag_words(self):
        return self.words if self.equip else None

    def matches(self, item_id, words=None):
        """The buyer's C2S 0x62 names this node: same id, and for equipment the exact block
        (the client compares all 12 bytes on every removal, shop_storage 1.2)."""
        return self.item_id == int(item_id) and (not self.equip or invmod.same_block(self.words, words))

    def row(self):
        """One S2C 0x87 row {item_id, qty, price, opt_count, opt[], item_ext}."""
        opts, extra = self.wire()
        return {'item_id': self.item_id & 0xFFFF, 'qty': self.qty & 0xFFFF, 'price': self.price & 0xFFFFFFFF,
                'opt_count': len(opts), 'repeat[opt_count]': [{'opt': w} for w in opts],
                'item_ext': extra & 0xFFFF}

    def as_record(self):
        out = {'id': self.item_id, 'qty': self.qty, 'price': self.price}
        if self.equip:
            out['w'] = list(invmod.pack_words(self.words))
        return out

    @classmethod
    def from_record(cls, rec, catalog=None):
        """An Entry from a persisted `stall_escrow` row, or None for a malformed one."""
        if not isinstance(rec, dict):
            return None
        item_id, qty = _int(rec.get('id')), _int(rec.get('qty'), 1)
        if item_id <= 0 or qty <= 0:
            return None
        catalog = catalog if catalog is not None else EC.items()
        equip = catalog.type_of(item_id) == EC.TYPE_EQUIPMENT if catalog.exists(item_id) else 'w' in rec
        return cls(item_id, qty, max(0, _int(rec.get('price'))), bool(equip),
                   invmod.pack_words(rec.get('w')) if equip else list(invmod.ZERO_BLOCK))

    def describe(self):
        return f'{EC.item_name(self.item_id) or self.item_id} x{self.qty} @ {self.price}'


# ---------------------------------------------------------------- persisted ---
def ensure(char):
    """Create / normalize char['stall_escrow'] in place (idempotent; store migration). A
    malformed row is dropped; every well-formed one is kept with its order: these are real
    items the character owns."""
    value = char.get('stall_escrow')
    rows = []
    for rec in list(value) if isinstance(value, list) else []:
        if not isinstance(rec, dict):
            continue
        item_id, qty = _int(rec.get('id')), _int(rec.get('qty'), 1)
        if item_id <= 0 or qty <= 0:
            continue
        row = {'id': item_id, 'qty': qty, 'price': max(0, _int(rec.get('price')))}
        if 'w' in rec:
            row['w'] = invmod.pack_words(rec.get('w'))
        rows.append(row)
    if value != rows:
        char['stall_escrow'] = rows
    return char['stall_escrow']


def escrow_entries(char, catalog=None):
    return [e for e in (Entry.from_record(r, catalog) for r in ensure(char)) if e is not None]


# ------------------------------------------------------------------ 0x5E parse ---
def _rows(rec):
    return list(rec.get('repeat[item_count]', []) or [])


def _opts(row):
    return [_int(next(iter(e.values()), 0)) if isinstance(e, dict) else _int(e)
            for e in row.get('repeat[socket_count]', []) or []]


def parse_open(rec, catalog=None):
    """(title bytes, [Entry], None) for a decoded C2S 0x5E StallOpen {u8 item_count,
    str[25] shop_name, item_count x {id, qty, unit_price, n, opt[n], extra}}, or
    (title, [], (result, why)) when it must be refused (F9 step 3.4): no item or more than
    MAX_ITEMS, an option list over 5 words (the S2C echoes would overflow the client's
    6-word buffer), price 0 (the client's own gate, "The Item price must be higher than
    0."), an id outside the EN catalog, a Type other than 0/1/2, a Cash item (def+0x1F0,
    which the client never lists), or a quantity outside 1..999 / 1 / 1..99 - result 9
    ("Invalid information transaction"). A KR gamedef NotTrade item gets 2 instead: the EN
    client does not know that flag, so listing one is no tampering (trade.py refuses it too).
    Ownership is market.py's (it needs the bag)."""
    catalog = catalog if catalog is not None else EC.items()
    title = cut_title(rec.get('shop_name', b''))
    rows = _rows(rec)
    count = _int(rec.get('item_count'))
    if not 1 <= count <= MAX_ITEMS or len(rows) != count:
        return title, [], (OPEN_TAMPERED, f'{count} item(s) listed (1..{MAX_ITEMS})')
    out = []
    for i, row in enumerate(rows):
        item, qty, price = _int(row.get('item_id')), _int(row.get('qty')), _int(row.get('unit_price'))
        n, opts, extra = _int(row.get('socket_count')), _opts(row), _int(row.get('item_extra')) & 0xFFFF
        what = f'entry {i}: {EC.item_name(item) or item} x{qty} @ {price}'
        if n > P.OPTION_LIST_MAX or len(opts) > P.OPTION_LIST_MAX:
            return title, [], (OPEN_TAMPERED, f'{what}: {n} option words (max {P.OPTION_LIST_MAX})')
        if not 0 < price <= PRICE_MAX:
            return title, [], (OPEN_TAMPERED, f'{what}: price must be > 0')
        info = catalog.get(item) if catalog.exists(item) else None
        if info is None:
            return title, [], (OPEN_TAMPERED, f'{what}: not in the EN client catalog')
        if info.type not in QTY_MAX:
            return title, [], (OPEN_TAMPERED, f'{what}: Type {info.type} is no bag item')
        if info.is_cash:
            return title, [], (OPEN_TAMPERED, f'{what}: cash item (itemdef+0x1F0, C24)')
        if not 1 <= qty <= QTY_MAX[info.type]:
            return title, [], (OPEN_TAMPERED, f'{what}: qty outside 1..{QTY_MAX[info.type]}')
        row_kr = EC.gamedef_item(item) if item <= EC.item_max_id() else None
        if row_kr and _int(row_kr.get('NotTrade')):
            return title, [], (OPEN_FAILED, f'{what}: KR gamedef NotTrade')
        equip = info.type == EC.TYPE_EQUIPMENT
        words = invmod.block_from_wire(opts, extra) if equip else list(invmod.ZERO_BLOCK)
        out.append(Entry(item, qty, price, equip, words))
    return title, out, None


def ownership_refusal(bag, entries):
    """None when `bag` (inventory.Inventory) holds every listed item - same-id stack
    quantities summed, equipment counted per exact block - else why not (F9 step 3.5)."""
    need = {}
    for e in entries:
        key = (e.item_id, tuple(invmod.pack_words(e.words))) if e.equip else (e.item_id, None)
        need[key] = need.get(key, 0) + e.qty
    for (item, words), qty in need.items():
        have = bag.count(item, list(words) if words is not None else None)
        if have < qty:
            what = f'block {list(words)}' if words is not None else 'the id'
            return f'{EC.item_name(item) or item} x{qty} listed, the bag holds {have} ({what})'
    return None


def return_to_bag(bag, entries):
    """Put escrowed entries back into `bag`: all of each that fits, else as much of a stack
    as fits (the client's FUN_00467200 silently drops what its full bag cannot take, so the
    server keeps the rest in escrow instead of losing it). Returns the entries (or remainders)
    that did not fit."""
    left = []
    for e in entries:
        if bag.add(e.item_id, e.qty, e.bag_words()) is not None:
            continue
        fit = 0
        if not e.equip:
            lo, hi = 0, e.qty - 1                   # the whole stack does not fit
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if bag.fits(e.item_id, mid) is None:
                    lo = mid
                else:
                    hi = mid - 1
            fit = lo
            if fit and bag.add(e.item_id, fit) is None:
                fit = 0
        left.append(Entry(e.item_id, e.qty - fit, e.price, e.equip, list(e.words)))
    return left


# ------------------------------------------------------------------ packets ---
def sign_fields(uid, title, sprite=SIGN_SPRITE):
    """S2C 0x85 StallOpenedBroadcast {u32 owner_uid, u16 sign_sprite_id, str[25] title}
    (packets.DEFAULT_ASSUME: the receiver holds the entity and its stall flag is clear; the
    flag set, the client reads the uid and stops)."""
    return {'owner_uid': int(uid) & 0xFFFFFFFF, 'sign_sprite_id': int(sprite) & 0xFFFF,
            'stall_title': cut_title(title)}


def list_fields(uid, title, entries):
    """S2C 0x87 {1, n, owner_uid, title, rows}; {1, 0} for a stall with nothing left (the
    client shows "The shop is closed or adjusting.", C31). The client clears window 0x259's
    list before it reads the rows (FUN_00466390(0)), so a re-send never duplicates them."""
    if not entries:
        return {'result': LIST_OK, 'item_count': 0}
    return {'result': LIST_OK, 'item_count': len(entries), 'owner_uid': int(uid) & 0xFFFFFFFF,
            'stall_name': cut_title(title), 'repeat[item_count]': [e.row() for e in entries]}


def _descriptor(entry, qty):
    opts, extra = entry.wire()
    return {'item_id': entry.item_id & 0xFFFF, 'qty': int(qty) & 0xFFFF, 'opt_count': len(opts),
            'repeat[opt_count]': [{'opt': w} for w in opts], 'item_ext': extra & 0xFFFF}


def bought_fields(gold, entry, qty):
    """S2C 0x88 {1, u64 buyer gold, descriptor} (buyer: gold set, its list node decremented,
    the item added with "you've received %s. (Count:%u)")."""
    return {'result': BUY_OK, 'gold': max(0, min(invmod.GOLD_MAX, int(gold))), **_descriptor(entry, qty)}


def sold_fields(gold, entry, qty):
    """S2C 0x89 {u64 seller gold, descriptor} (seller: gold set, its own list node found by
    id and qty >= n then decremented - freed at 0 -, "Sold %u (%s)."; the bag is not touched,
    the item left it at registration). Only ever to the real selling owner: the handler
    matches whatever list window 0x259 still holds, a stale buyer list included (C9)."""
    return {'gold': max(0, min(invmod.GOLD_MAX, int(gold))), **_descriptor(entry, qty)}
