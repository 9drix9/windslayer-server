#!/usr/bin/env python3
"""
bank_tabs.py - the bank (storage) model and the client's own bank-tab algorithms
(shop_storage-bank-model; shop_storage.md 1.5, 3.1, 3.6)
===============================================================================
The client keeps its bank in one block (2008 scene+0x980..+0xE77, 2009 +0x998..; zeroed and
refilled by S2C 0x65) and, on every S2C 0x66 / 0x67, re-runs its OWN tab algorithm on that
copy. So the server has to run the identical algorithm on its copy - a "looser" or "nicer"
rule here is a desync the next time the player opens the window. The ports below are line
for line (decomp read for this stage, 2008 and 2009):

    consume add/remove  FUN_00427740 (2009 FUN_00428cb0, identical)
    etc add/remove      FUN_00427940 (2009 FUN_00428eb0, two differences - see etc_change)
    equip add/remove    FUN_00427610 + FUN_00427390 12-byte memcmp (2009 FUN_00428b80)
    space pre-check     FUN_00427B70 (2009 FUN_004290d0, identical)

    import bank_tabs as BT
    bank = BT.Bank(char, build='2009')          # binds char['bank'] / char['bank_slots']
    ok = bank.add(3, 10)                         # the client's add; False = it would fail
    bank.remove(179, 1, words)                   # exact id + 12-byte block
    P.send(..., '0x65', BT.fields_for_65(char))

Persisted shape (character record; migration writes it, store backup .bak-pre-p4)
---------------------------------------------------------------------------------
    char['bank_slots'] = [35, 35, 35]            # S2C 0x65 capacities: equip, consume, etc
    char['bank'] = {'equip':   [{'id': 179, 'w': [w0..w4, extra]}, None, ...],
                    'consume': [{'id': 3, 'qty': 10}, None, ...],     # qty 1..999
                    'etc':     [None, {'id': 281, 'qty': 40}, ...],   # qty 1..99
                    'gold':    0}                                     # u64 (player_info+0xE28)
Each tab is SLOT-ordered, exactly the u16[60] arrays the client holds (None = the id 0 of an
empty slot; trailing empties are not stored). The order matters: the add fills the first
empty slot, the remove walks from the last slot back and closes only the first hole, and
S2C 0x65 lists the slots in this order. Bank scope is per character (shop_storage.md Q4,
premium_cash §3.1 `bank_slots`).

Capacities are 0..60 (a byte > 60 makes every client operation fail; the arrays are 60
long). Unlike the design note ("keep consume == etc"), nothing forces the two equal: the
2008 etc-tab quirk that reads the CONSUME cap is ported as it is, so the server's result is
the client's result whatever the caps are.
"""
import logging

import en_content as EC
import inventory as invmod

log = logging.getLogger('WS')

SLOTS = 60                      # u16[60] per tab (scene+0x984 / +0xCCC / +0xDBC)
MAX_CAPACITY = 60               # FUN_00427610: `if (0x3c < cap) return 0`; 0x65 caps <= 60
DEFAULT_CAPACITY = EC.BANK_DEFAULT_SLOTS
TABS = ('equip', 'consume', 'etc')
TAB_OF_TYPE = {EC.TYPE_EQUIPMENT: 'equip', EC.TYPE_CONSUMABLE: 'consume', EC.TYPE_ETC: 'etc'}
STACK_MAX = {'consume': 999, 'etc': 99}
WORDS = invmod.OPTION_WORDS     # w0..w4 options + w5 extra = the 12-byte record
ZERO = (0,) * WORDS
GOLD_MAX = invmod.GOLD_MAX
EQUIP_RECORD_BYTES = 12


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _words(words):
    """A 12-byte equipment record as 6 u16 words (w0..w4 packed from w0 like the bag model:
    the client sends only leading non-zero words, so a gap can never reach the bank)."""
    return tuple(invmod.pack_words(words))


# ============================================================ client ports ===
# Every function takes the three capacities (equip, consume, etc) and COPIES of the slot
# arrays, and returns (ok, ids, values) - the arrays after the client's code ran, including
# the partial writes it leaves behind on a failure (the caller simply discards them).
def _pad(values, fill):
    values = list(values)[:SLOTS]
    return values + [fill] * (SLOTS - len(values))


def _compact_first_hole(ids, vals, cap, stop_cap=None):
    """The tail of FUN_00427740 / FUN_00427940 after a successful removal: find the FIRST
    empty slot i < cap and shift the following slots left until a slot is empty or the end
    (index `stop_cap`, which the 2008 etc code reads from the CONSUME cap) - one hole only.
    The loop outside restarts the scan when the shift runs past `cap` without meeting either
    stop (only possible when stop_cap > cap)."""
    stop_cap = cap if stop_cap is None else stop_cap
    i = 0
    while i < cap:
        if ids[i] == 0:
            j = i + 1
            while j <= cap:
                nxt = ids[j] if j < SLOTS else 0
                if nxt == 0 or j == stop_cap:
                    ids[j - 1] = 0
                    vals[j - 1] = 0
                    return True
                ids[j - 1] = nxt
                vals[j - 1] = vals[j] if j < SLOTS else 0
                j += 1
        i += 1
    return True


def _stack_remove(ids, vals, cap, item, qty):
    """The removal half both stack functions share: needs total >= qty (a u16 sum), then
    takes from the LAST slot backwards; returns False when there is not enough."""
    total = 0
    for i in range(cap):
        if ids[i] == item:
            total = (total + vals[i]) & 0xFFFF
    if qty > total:
        return False
    delta = -qty
    for i in range(cap - 1, -1, -1):
        if ids[i] == item:
            delta += vals[i]
            if delta < 0:
                vals[i] = 0
                ids[i] = 0
            else:
                vals[i] = delta
                if delta == 0:
                    ids[i] = 0
                delta = 0
    return delta == 0


def consume_change(caps, ids, qtys, item, delta):
    """FUN_00427740: add (delta > 0) or remove (delta < 0) `item` in the consumable tab
    (u16 quantities, stacks of 999). delta must be in -999..999 and non-zero."""
    ids, qtys = _pad(ids, 0), _pad(qtys, 0)
    item, delta = _int(item) & 0xFFFF, _int(delta)
    cap = max(0, min(SLOTS, _int(caps[1])))
    if delta == 0 or not -999 <= delta <= 999:
        return False, ids, qtys
    if delta < 0:
        if not _stack_remove(ids, qtys, cap, item, -delta):
            return False, ids, qtys
        return _compact_first_hole(ids, qtys, cap), ids, qtys
    # add: the FIRST same-id slot below 999 absorbs; at 1000+ it is set to 999 (even when
    # the spill then finds no empty slot - the client leaves that write behind) and the
    # rest goes to the FIRST empty slot.
    for i in range(cap):
        if ids[i] == item and qtys[i] < 999:
            total = (qtys[i] + delta) & 0xFFFF
            if total < 1000:
                qtys[i] = total
                return True, ids, qtys
            qtys[i] = 999
            delta = total - 999
            break
    if delta:
        for i in range(cap):
            if ids[i] == 0:
                qtys[i] = delta
                ids[i] = item
                return True, ids, qtys
    return False, ids, qtys


def etc_change(caps, ids, qtys, item, delta, client_build='2008'):
    """FUN_00427940 (2008) / FUN_00428eb0 (2009): the misc tab (u8 quantities, stacks of 99).

    The two builds differ in exactly two places (decomp diff):
      - add: 2008 lets the FIRST same-id slot absorb even when it is already at 99 (so the
        whole amount spills into an empty slot and a later same-id slot below 99 is never
        topped up); 2009 takes the first same-id slot BELOW 99, like the consumable tab.
      - compaction after a removal: 2008 stops the shift on the CONSUME cap (caps[1]) - the
        quirk shop_storage.md 1.5 names; 2009 uses the etc cap."""
    ids, qtys = _pad(ids, 0), _pad(qtys, 0)
    item, delta = _int(item) & 0xFFFF, _int(delta)
    cap = max(0, min(SLOTS, _int(caps[2])))
    if delta == 0 or not -99 <= delta <= 99:
        return False, ids, qtys
    if delta < 0:
        if not _stack_remove(ids, qtys, cap, item, -delta):
            return False, ids, qtys
        stop = cap if str(client_build) == '2009' else max(0, _int(caps[1]))
        return _compact_first_hole(ids, qtys, cap, stop), ids, qtys
    for i in range(cap):
        if ids[i] == item and (str(client_build) != '2009' or qtys[i] < 99):
            total = (qtys[i] + delta) & 0xFFFF
            if total < 100:
                qtys[i] = total
                return True, ids, qtys
            qtys[i] = 99
            delta = total - 99
            break
    if delta:
        for i in range(cap):
            if ids[i] == 0:
                ids[i] = item
                qtys[i] = delta & 0xFF
                return True, ids, qtys
    return False, ids, qtys


def equip_change(caps, ids, recs, item, words, delta):
    """FUN_00427610: add (delta >= 1) one equipment record to the first empty slot, or remove
    (delta < 1) the first slot whose id AND 12-byte record (FUN_00427390 memcmp) match, then
    shift the following slots left until an empty one. A cap above 60 fails outright."""
    ids, recs = _pad(ids, 0), _pad(recs, ZERO)
    item, rec = _int(item) & 0xFFFF, _words(words)
    cap = _int(caps[0])
    if cap > MAX_CAPACITY:
        return False, ids, recs
    if delta >= 1:
        for i in range(cap):
            if ids[i] == 0:
                ids[i] = item
                recs[i] = rec
                return True, ids, recs
        return False, ids, recs
    for i in range(cap):
        if ids[i] == item and tuple(recs[i]) == rec:
            j = i + 1
            while j < cap and ids[j] != 0:
                ids[j - 1] = ids[j]
                recs[j - 1] = recs[j]
                j += 1
            ids[j - 1] = 0
            recs[j - 1] = ZERO
            return True, ids, recs
    return False, ids, recs


def space_ok(caps, tabs, item_type, item, qty):
    """FUN_00427B70, the check the deposit dialog runs BEFORE the fee dialog ("There are no
    empty space in Bank."). True = the client lets the deposit through. It is looser than the
    add itself (a same-id slot with room for the whole amount, or ANY empty slot), which is
    why the server simulates the add as well (Bank.fits)."""
    item, qty = _int(item) & 0xFFFF, _int(qty)
    if item_type == EC.TYPE_CONSUMABLE:
        ids, vals, cap, limit = tabs['consume'][0], tabs['consume'][1], _int(caps[1]), 1000
    elif item_type == EC.TYPE_ETC:
        ids, vals, cap, limit = tabs['etc'][0], tabs['etc'][1], _int(caps[2]), 100
    elif item_type == EC.TYPE_EQUIPMENT:
        cap = _int(caps[0])
        return cap <= MAX_CAPACITY and any(i == 0 for i in tabs['equip'][0][:cap])
    else:
        return False
    if cap > MAX_CAPACITY or qty >= limit:
        return False
    for i in range(cap):
        if ids[i] == item and vals[i] + qty < limit:
            return True
    return any(i == 0 for i in ids[:cap])


# =================================================================== model ===
def _as_caps(value):
    caps = [_int(v, DEFAULT_CAPACITY) for v in list(value)] if isinstance(value, list) else []
    caps = caps[:len(TABS)] + [DEFAULT_CAPACITY] * (len(TABS) - len(caps[:len(TABS)]))
    return [max(0, min(MAX_CAPACITY, c)) for c in caps]


def _as_tab(value, tab):
    """Slot list of one tab: dicts or None, at most 60 slots, no trailing empties."""
    out = []
    for e in list(value)[:SLOTS] if isinstance(value, list) else []:
        item = _int(e.get('id')) & 0xFFFF if isinstance(e, dict) else 0
        if not item:
            out.append(None)
        elif tab == 'equip':
            out.append({'id': item, 'w': list(_words(e.get('w')))})
        else:
            qty = max(0, min(STACK_MAX[tab], _int(e.get('qty'))))
            out.append({'id': item, 'qty': qty} if qty else None)
    while out and out[-1] is None:
        out.pop()
    return out


def ensure(char):
    """Create / normalize char['bank_slots'] and char['bank'] in place (store migration and
    every Bank()). Structural only - no catalog lookups - so a server started without the
    EN files never drops a banked item. A conforming record is left untouched (same objects:
    a Bank built earlier keeps writing into the saved record)."""
    if not isinstance(char, dict):
        raise TypeError('character record must be a dict')
    caps = _as_caps(char.get('bank_slots'))
    if char.get('bank_slots') != caps:
        char['bank_slots'] = caps
    bank = char.get('bank')
    if not isinstance(bank, dict):
        bank = char['bank'] = {}
    stored = bank.get('gold')
    gold = max(0, min(GOLD_MAX, _int(stored)))
    normalized = {tab: _as_tab(bank.get(tab), tab) for tab in TABS}
    normalized['gold'] = gold
    if bank != normalized or not isinstance(stored, int) or isinstance(stored, bool):
        bank.update(normalized)
        for key in [k for k in list(bank) if k not in normalized]:
            del bank[key]
    return bank


class Bank:
    """The bank of one character, applied with the client's own algorithms. Every mutation
    writes straight into the record; the caller marks the store dirty (like inventory.py)."""

    def __init__(self, char, catalog=None, build='2008'):
        self.data = ensure(char)
        self.char = char
        self.catalog = catalog if catalog is not None else EC.items()
        self.build = str(build or '2008')

    # ------------------------------------------------------------- reading ---
    @property
    def caps(self):
        return list(self.char['bank_slots'])

    @property
    def gold(self):
        return _int(self.data.get('gold'))

    @gold.setter
    def gold(self, value):
        self.data['gold'] = max(0, min(GOLD_MAX, _int(value)))

    def tab_of(self, item_id):
        d = self.catalog.get(item_id)
        return None if d is None else TAB_OF_TYPE.get(d.type)

    def arrays(self, tab):
        """(ids[60], values[60]) of a tab as the client holds them: values are quantities, or
        6-word records for the equipment tab."""
        slots = self.data[tab]
        ids = [(e or {}).get('id', 0) if e else 0 for e in slots]
        if tab == 'equip':
            vals = [_words(e['w']) if e else ZERO for e in slots]
            return _pad(ids, 0), _pad(vals, ZERO)
        return _pad(ids, 0), _pad([e['qty'] if e else 0 for e in slots], 0)

    def _store(self, tab, ids, vals):
        out = []
        for item, val in zip(ids, vals):
            if not item:
                out.append(None)
            elif tab == 'equip':
                out.append({'id': item, 'w': list(val)})
            else:
                out.append({'id': item, 'qty': int(val)})
        while out and out[-1] is None:
            out.pop()
        self.data[tab] = out

    def total(self, item_id):
        """Quantity of a stackable id (u16 sum over the tab, as the client counts), or the
        number of equipment records with that id."""
        tab = self.tab_of(item_id)
        if tab is None:
            return 0
        ids, vals = self.arrays(tab)
        cap = self.caps[TABS.index(tab)]
        if tab == 'equip':
            return sum(1 for i in ids[:cap] if i == item_id)
        return sum(v for i, v in zip(ids[:cap], vals[:cap]) if i == item_id)

    def equip_records(self, item_id):
        ids, recs = self.arrays('equip')
        return [recs[i] for i in range(min(SLOTS, self.caps[0])) if ids[i] == item_id]

    def pick_equip(self, item_id, words):
        """shop_storage.md F5 step 5.2: the stored record a withdraw of `item_id` removes -
        the exact (id, block) slot, else (a path-b request with an all-zero block) the first
        zero-block slot with that id, else the first slot with that id. None when the bank
        holds none. The 0x67 must carry THIS record: the client memcmps it against its slot."""
        records = self.equip_records(item_id)
        want = _words(words)
        if want in records:
            return want
        if want == ZERO:
            return records[0] if records else None
        return None

    # ----------------------------------------------------------- mutation ---
    def _change(self, item_id, qty, words, sign, commit):
        tab = self.tab_of(item_id)
        if tab is None:
            return False
        ids, vals = self.arrays(tab)
        caps = self.caps
        if tab == 'equip':
            ok, ids, vals = equip_change(caps, ids, vals, item_id, words, sign)
        elif tab == 'consume':
            ok, ids, vals = consume_change(caps, ids, vals, item_id, sign * _int(qty))
        else:
            ok, ids, vals = etc_change(caps, ids, vals, item_id, sign * _int(qty), self.build)
        if ok and commit:
            self._store(tab, ids, vals)
        return ok

    def fits(self, item_id, qty=1, words=None):
        """Would the client's own add succeed (simulated on a copy; nothing changes)?"""
        return self._change(item_id, qty, words, 1, commit=False)

    def space_ok(self, item_id, qty=1):
        """FUN_00427B70 on this bank (the client's looser pre-check)."""
        d = self.catalog.get(item_id)
        if d is None:
            return False
        tabs = {tab: self.arrays(tab) for tab in TABS}
        return space_ok(self.caps, tabs, d.type, item_id, qty)

    def add(self, item_id, qty=1, words=None):
        return self._change(item_id, qty, words, 1, commit=True)

    def remove(self, item_id, qty=1, words=None):
        return self._change(item_id, qty, words, -1, commit=True)


# ------------------------------------------------------------------- S2C 0x65 ---
def fields_for_65(char, build='2008'):
    """S2C 0x65 BankContents: the three capacities, the three slot-ordered lists (id 0 =
    empty slot, trailing empties not sent) and bank_gold. The handler memsets the whole
    block first, so this packet IS the bank; every list is clamped to 60 entries and every
    option list to 5 words (packets.LIST_CAPS: > 5 spills into the next slot)."""
    bank = Bank(char, build=build)
    caps = bank.caps
    fields = {'bank_equip_slots': caps[0], 'bank_consume_slots': caps[1], 'bank_misc_slots': caps[2]}
    for tab, count_field in zip(TABS, ('equip_count', 'consume_count', 'misc_count')):
        rows = []
        for entry in bank.data[tab][:SLOTS]:
            if tab == 'equip':
                w = list(_words(entry['w'])) if entry else list(ZERO)
                sent = invmod.wire_words(w)
                rows.append({'item_id': entry['id'] if entry else 0, 'option_count': len(sent),
                             'repeat[option_count]': [{'option': v} for v in sent],
                             'equip_attr_last': w[5]})
            else:
                rows.append({'item_id': entry['id'] if entry else 0,
                             'quantity': entry['qty'] if entry else 0})
        fields[count_field] = len(rows)
        fields[f'repeat[{count_field}]'] = rows
    fields['bank_gold'] = bank.gold
    return fields
