#!/usr/bin/env python3
"""
inventory.py - the persistent bag, equipment grid and wallet (item_inventory-model-persist,
shop_storage-wallet, trade-economy-persist, item_inventory-kind-slot-table)
============================================================================================
One model, stored in the character record, mirroring the rules the client itself applies
(item_inventory.md 1.2/1.3/3.2). Nothing lives on the session any more: a portal, a relog
or a crash cannot lose or duplicate an item, and the wallet the client shows is the wallet
on disk (S2-11, shop_storage B2/B3).

    import inventory as INV
    bag = INV.Inventory(char)                 # binds to char['inventory'] / ['equipped']
    bag.add(5, 3)                             # Herb x3 -> consume tab
    bag.remove(179, 1)                        # exact-block match for equipment
    bag.equip(179, [0]*6)                     # -> (slot 5, displaced entry or None)
    INV.Wallet(char).gold += 250              # u64, clamped
    P.send(..., '0x1D', INV.item_fields('0x1D', 179, words, uid=uid))

Persisted shape (roadmap F4 "character / items", written by store.migrate_character)
------------------------------------------------------------------------------------
    char['gold']      = 0                       # u64, the only gold there is
    char['victy']     = 0                       # u32 (D16: the character currency, not Wind Cash)
    char['inventory'] = {'equip':   [{'id': 179, 'w': [0, 0, 0, 0, 0, 0]}, ...],
                         'consume': {5: 12},    # id -> total quantity
                         'etc':     {281: 40},
                         'tab_slots': [35, 35, 35]}
    char['equipped']  = {5: {'id': 179, 'w': [0, 0, 0, 0, 0, 0]}}     # grid slot -> instance

Why `consume`/`etc` are a total and not a slot list: the roadmap F4 schema is
`consume:{id:qty}`, and JSON keeps that small and diffable. The client, though, splits a
total over stacks of at most 999 (type 0, FUN_00423b70) / 99 (type 2, FUN_00423e70), and
capacity is counted in SLOTS. So the stack list is derived on demand by `slots()` in
insertion order, which is also the order S2C 0x03 must list them in
(item_inventory-seed-03-07). Equipment does not stack, so that tab stays an ordered list
of instances, compacted on removal exactly as the client compacts it (live-verified with
S2C 0x24: the later entries move down).

Item instance = `u16 id` + six option words `w[0..5]`. w0..w4 are socket/option words and
must stay PACKED from w0 (the client stops counting at the first zero, FUN_004241d0), and
w5 is the trailing extra word. Equipment is matched by id AND the exact block everywhere
(0x19/0x23 remove, 0x1D bag remove, 0x1E/0x24 grid remove), so the model compares all six.

KIND -> equipment slot (item_inventory-kind-slot-table, Q1 answered)
--------------------------------------------------------------------
`KIND_TO_SLOT` is FUN_00426680 read out of the decompilation (corpus/decomp/
00426680_FUN_00426680.c): a switch on the item's Kind (def+0x1CC) writing one u16 of the
entity's equip array (entity+0x13C) and, for the seven Kinds that carry an option block,
copying three dwords into entity+0x16E at slot*3. `param_6` is the item's Cash flag
(def+0x1F0): a cash item of the same Kind goes to the costume slots 16..22 instead.
Kind 17 (rings) is the one Kind that uses two slots: the client shifts slot 13 into slot
14 before writing the new ring into 13. Kinds 1, 2, 4 and 13 hit the switch default and
have no slot at all (in EN they are Type 5 cash-only records).
The old `_KIND_TO_GRID` guessed from PySlayer's KR order and put Kinds 15/16 in slot 0.

Appearance (the look words the records carry)
---------------------------------------------
The client draws a player from the appearance array alone (one sprite number per layer) and
never derives it from the grid: S2C 0x02 / 0x07 / 0x04 / 0x05 copy the words verbatim, and
only 0x1D / 0x1E / 0x24 recompose ONE layer, through 2009 FUN_004282c0 = 2008 FUN_00426d50.
`compose` is that routine, and `wear` / `take_off` / `recompose` apply it to the stored
`look` (+ `look_ext` in 2009) right after the grid change, as the client does - so the next
0x07 / 0x02 carries exactly what the client is showing (the worn hat, clothes, weapon...).
`compose_all` replays the worn items over a look stored before that: the idempotent
migration GameServer._compose_looks runs at every login.
"""
import logging

import en_content as EC

log = logging.getLogger('WS')

TABS = ('equip', 'consume', 'etc')
# Client stack limits per tab (item_inventory.md 1.2).
STACK_MAX = {'equip': 1, 'consume': 999, 'etc': 99}
# Bag capacities: the 3 bytes S2C 0x03 sends (scene+0x3A9..+0x3AB). The client refuses
# every add/remove path when a byte is > 45 and shows "There isn't empty space in the
# inventory" when it is 0, so a stored value is clamped into 1..45 on load.
MIN_CAPACITY, MAX_CAPACITY = 1, 45
DEFAULT_CAPACITY = 35
OPTION_WORDS = 6              # w0..w4 sockets/options + w5 extra
WIRE_OPTION_WORDS = 5         # the client reads at most 5 words (packets.OPTION_LIST_MAX)
ZERO_BLOCK = [0] * OPTION_WORDS

# Equip grid (entity+0x13C): 16 regular slots, then 9 cash/costume slots.
EQUIP_SLOTS = 16
CASH_EQUIP_SLOTS = 9
MAX_EQUIP_SLOT = EQUIP_SLOTS + CASH_EQUIP_SLOTS - 1

# hii Kind -> equip grid slot, from FUN_00426680 (see the module docstring).
KIND_TO_SLOT = {
    10: 0,    # hat / head
    3: 1,     # glasses
    7: 2,
    0: 3,
    6: 4,     # shirt (option block)
    11: 5,    # weapon (option block)
    12: 6,    # shield (option block)
    14: 7,
    5: 8,     # pants (option block)
    8: 9,     # (option block)
    9: 10,    # shoes (option block)
    15: 11,
    16: 12,
    17: 13,   # ring: writes slot 13 and shifts the old one into 14
    18: 15,
}
# Same switch with param_6 (def+0x1F0 Cash) != 0: the costume half of the grid.
KIND_TO_CASH_SLOT = {10: 16, 6: 17, 11: 18, 12: 19, 5: 20, 8: 21, 9: 22}
# The Kinds whose entry also carries the 12-byte option block (the dword triples in
# FUN_00426680). Everything else stores an id only.
KINDS_WITH_BLOCK = frozenset((5, 6, 8, 9, 10, 11, 12))
RING_KIND = 17
RING_SLOTS = (13, 14)
WEAPON_KIND = 11
WEAPON_SLOT = KIND_TO_SLOT[WEAPON_KIND]
SHIELD_KIND = 12

# EN 2009 (client-2009-world): FUN_00427af0, the 2009 counterpart of FUN_00426680 (called
# from the S2C 0x1D path 0x4581C5 with the item def's +0x1CC Kind and +0x1F0 Cash flag, from
# the pet path 0x4577B5 with (0xE, cash 1) and from FUN_00462f20). The 2009 hii keeps the
# 2008 Kind values (belt 14, earrings 15, necklace 16, ring 17 - item 1343 "Old Gold Ring"
# is Kind 17 in both), but the switch was renumbered around the new pet kinds:
#   14 cash -> slot 15 (the pet, Type 6), 15 / 16 cash -> 23 / 24 (pet hats / glasses:
#   FUN_0044FE20 "Register the pet first."), 17 -> 7, 18 -> 11, 19 -> 12, 20 -> the ring pair
#   13/14; non-cash 14 / 15 / 16 hit a case with no write, so the 2009 client wears no belt,
#   earrings or necklace, and a Kind-17 ring takes the single slot 7 (the pair logic sits on
#   Kind 20, which no 2009 hii record has). The model mirrors exactly that.
# Kinds 0, 3, 7 and 17..20 have no Cash test in the switch: a cash item of those Kinds goes to
# the same slot, so they sit in both tables.
KIND_TO_SLOT_2009 = {10: 0, 3: 1, 7: 2, 0: 3, 6: 4, 11: 5, 12: 6, 17: 7, 5: 8, 8: 9, 9: 10,
                     18: 11, 19: 12, 20: 13}
KIND_TO_CASH_SLOT_2009 = {10: 16, 6: 17, 11: 18, 12: 19, 5: 20, 8: 21, 9: 22, 14: 15, 15: 23, 16: 24,
                          0: 3, 3: 1, 7: 2, 17: 7, 18: 11, 19: 12, 20: 13}
RING_KIND_2009 = 20
_KIND_TABLES = {
    '2009': (KIND_TO_SLOT_2009, KIND_TO_CASH_SLOT_2009, RING_KIND_2009),
}


# cash.KIND_PET (cash imports this module): a 2009 pet record in char['cash_items'].
PET_RECORD_KIND = 3


PET_BUILD = '2009'           # the only client with pets (en_content.ItemCatalog.client_build)


def bagged_pets(char):
    """Pet records (cash.KIND_PET) the character owns but does not wear: the 2009 0x6F puts
    each one in the client's EQUIPMENT tab (cash.bagged_pets is the same count). Build-blind:
    pet_slots() is what a bag counts."""
    return sum(1 for r in list((char or {}).get('cash_items') or [])
               if isinstance(r, dict) and _int(r.get('kind')) == PET_RECORD_KIND and not r.get('equipped'))


# P15 pet-s5: the 2009 pet gear - hii Type 1, Cash 1, Kind 15 (headgear) / 16 (apparel), the
# 16 rows 4290..4308 around the four pets (pet.md 2.1). FUN_00427af0 writes these Kinds only
# for a cash item (KIND_TO_CASH_SLOT_2009 15 -> 23, 16 -> 24; a non-cash 15 / 16 hits the case
# with no write). Like every cash item it lives as a record of char['cash_items'] (the 0x6F
# purges every cash item from every bag and re-files the records: Type 1 into the EQUIPMENT
# tab, FUN_00464e00 case 1), never in `inventory`; worn, it is `equipped` and its grid slot.
PET_GEAR_KINDS = (15, 16)


def is_pet_gear(item_id, catalog=None):
    """A 2009 pet gear item (Type 1, Cash, Kind 15 / 16) of the item table `catalog` (None =
    the loaded one). False under any other table: the 2008 hii has no such row."""
    catalog = catalog if catalog is not None else EC.items()
    if getattr(catalog, 'client_build', None) != PET_BUILD:
        return False
    d = catalog.get(_int(item_id))
    return (d is not None and d.type == 1 and bool(getattr(d, 'cash', 0))
            and getattr(d, 'kind', -1) in PET_GEAR_KINDS)


def bagged_pet_gear(char, catalog=None):
    """Pet gear records (is_pet_gear) the character owns but does not wear: each one is an
    equipment-tab slot of the 2009 client (the 0x6F files a Type 1 record there)."""
    catalog = catalog if catalog is not None else EC.items()
    return sum(1 for r in list((char or {}).get('cash_items') or [])
               if isinstance(r, dict) and _int(r.get('kind')) != PET_RECORD_KIND and not r.get('equipped')
               and is_pet_gear(r.get('item_id'), catalog))


def pet_slots(char, catalog=None):
    """Equipment-tab slots the character's bagged pets - and, since P15 pet-s5, its bagged pet
    gear records - take in the client whose item table `catalog` is (None = the loaded one):
    bagged_pets + bagged_pet_gear under the 2009 table, 0 under any other.
    Both builds can share one accounts.json (config CLIENT_BUILD), and the 2008 0x6F leaves a
    pet record out (cash.owned_list_packets), so the 2008 client's tab never holds one - a
    count there would be a phantom full slot refusing pickups and trades the client allows."""
    catalog = catalog if catalog is not None else EC.items()
    if getattr(catalog, 'client_build', None) != PET_BUILD:
        return 0
    return bagged_pets(char) + bagged_pet_gear(char, catalog)


def kind_tables(catalog=None):
    """(Kind -> slot, cash Kind -> slot, ring Kind) of the client build whose item table
    `catalog` is (en_content.ItemCatalog.client_build; 2008 when unset)."""
    build = getattr(catalog, 'client_build', None)
    return _KIND_TABLES.get(build, (KIND_TO_SLOT, KIND_TO_CASH_SLOT, RING_KIND))


# ---------------------------------------------------------------- appearance ---
# The appearance array (2008 entity+0x120 u16[14], 2009 entity+0x12E u16[17]) holds one
# SPRITE FILE NUMBER per draw layer, and the draw loop (2009 FUN_00401e90 via FUN_004332f0)
# skips a layer whose word is 0. For a worn item the word is the item's hii Spr_Num: the
# gender is already in the number ((F) sets are 0xx, (M) sets 1xx). Layer names are the
# client's own folder names (2009 table 0x526008; 2008 draws the first 14).
LAYER_NAMES = ('cloak', 'body', 'head', 'eye', 'hair', 'pant', 'shirt', 'face', 'glove',
               'shoes', 'helm', 'weapon', 'shield', 'bhair', 'pet', 'pet_helm', 'pet_wear')
LAYER_HAIR, LAYER_HELM, LAYER_WEAPON, LAYER_BACK_HAIR = 4, 10, 11, 13
LOOK_WORDS = 14               # 2008 (entity+0x120)
LOOK_WORDS_2009 = 17          # 2009 (entity+0x12E): `look` + store `look_ext`
# The client never builds a look from the equipped ids: S2C 0x02 / 0x07 / 0x04 / 0x05 copy
# the words verbatim, and only S2C 0x1D (equip), 0x1E / 0x24 (unequip) run the ONE
# composition routine, 2009 FUN_004282c0 = 2008 FUN_00426d50 (same logic; the Kind limit
# and the item-table offset differ). `compose` is that routine; the server applies it to the
# stored look at the same events, so the next 0x07 / 0x02 carries what the client shows.
# Kind -> (regular grid slot, cash grid slot) the routine READS (jump table 2009 0x42856C,
# 2008 0x426FFC); FUN_00427af0 / FUN_00426680 write exactly these slots (KIND_TO_SLOT*).
COMPOSE_READ = {5: (8, 20), 6: (4, 17), 8: (9, 21), 9: (10, 22), 10: (0, 16), 11: (5, 18),
                12: (6, 19)}
# "0x10 < Kind -> return" (2009) / "0xd < Kind" (2008): 2008 composes no belt, earring,
# necklace or ring (Kinds 14..17 do have 2008 grid slots), 2009 draws the pet layers 14..16.
COMPOSE_KIND_LIMIT = 14
COMPOSE_KIND_LIMIT_2009 = 17
CLEAR_HAT = 0xD5C             # item 3420 "Clear Hat" (cash Kind 10): the helm layer shows the hair
# What an emptied pant / shirt layer shows: the underwear sprite of the gender bool the
# records send (0 -> 1, 1 -> 101). The creation clothes (2 / 102) are not items, so the
# client never brings them back after an unequip, and neither does the server.
UNDERWEAR = (1, 101)


def compose_limits(catalog=None):
    """(look words, Kind limit) of the build whose item table `catalog` is."""
    if getattr(catalog, 'client_build', None) == '2009':
        return LOOK_WORDS_2009, COMPOSE_KIND_LIMIT_2009
    return LOOK_WORDS, COMPOSE_KIND_LIMIT


def _job(item_def, index):
    job = list(getattr(item_def, 'job', None) or [])
    return _int(job[index]) if index < len(job) else 0


def _show_costume(look, kind, costume):
    if costume.id == CLEAR_HAT:
        look[LAYER_HELM] = look[LAYER_HAIR]
    else:
        look[kind] = costume.spr_num & 0xFFFF


def compose(look, grid, kind, cash, spr, gender, job0, lookup, kind_limit=COMPOSE_KIND_LIMIT_2009):
    """2009 FUN_004282c0 / 2008 FUN_00426d50: recompose ONE layer of `look` (a list,
    changed in place and returned) after the grid change for an item of `kind`.

    grid:   the 25 grid item ids AFTER the change (the client writes / clears the slot first:
            0x1D 0x4581C5 then 0x458215; 0x1E/0x24 0x458F45 then 0x458F80).
    cash:   the changed item's Cash flag (def+0x1F0); spr its Spr_Num (def+0x1D0) on an
            equip and 0 on an unequip (PUSH 0 at 0x458F68); job0 its Job[0] (def+0x168).
    gender: the entity's gender bool (2009 +0x11B, 2008 +0x113): the one the records send.
    lookup: item id -> item def (None for an unknown id, as FUN_00404750 returns NULL).
    Every call site in both clients passes param_8 = 0, the only mode ported here."""
    kind = _int(kind, -1)
    if not 0 <= kind < min(kind_limit, len(look)):
        return look
    spr = _int(spr) & 0xFFFF
    regular_id = costume_id = 0
    bare = False                    # a weapon / shield Kind with no real one worn
    if kind in COMPOSE_READ:
        reg_slot, cash_slot = COMPOSE_READ[kind]
        regular_id, costume_id = _int(grid[reg_slot]), _int(grid[cash_slot])
        bare = kind in (WEAPON_KIND, SHIELD_KIND) and not regular_id
    if bare and cash:
        return look                 # a weapon / shield skin with nothing under it: no change
    costume = lookup(costume_id) if costume_id else None
    regular = lookup(regular_id) if regular_id else None
    if regular is None:
        if costume is not None and not bare:
            _show_costume(look, kind, costume)
            return look
    elif costume is not None:
        if (kind == WEAPON_KIND and _job(regular, 0) != _job(costume, 0)
                and (_job(regular, 1) == 0 or _job(costume, 1) == 0)):
            look[LAYER_WEAPON] = regular.spr_num & 0xFFFF   # a skin of another class: the real one
        else:
            _show_costume(look, kind, costume)              # the costume beats the regular item
        return look
    elif not spr:
        if cash:
            look[kind] = regular.spr_num & 0xFFFF           # costume off: the regular one again
            return look
    elif cash and _int(job0) != _job(regular, 0):
        return look                 # (unreachable from 0x1D: kept for fidelity)
    if kind == LAYER_HAIR:
        if look[LAYER_HELM] == look[LAYER_HAIR]:
            look[LAYER_HELM] = spr                          # a helm layer that showed the hair
        look[LAYER_BACK_HAIR] = spr
        look[LAYER_HAIR] = spr
    else:
        look[kind] = spr
    if not spr:
        if kind in (5, 6):
            look[kind] = UNDERWEAR[1 if gender else 0]
        elif kind == LAYER_HELM:
            look[LAYER_HELM] = look[LAYER_HAIR]             # a bare head shows the hair
    return look


# Currency wire limits: gold is u64 (player_info+0x228) and Victy a u32 (0x03/0x18).
GOLD_MAX = 0xFFFFFFFFFFFFFFFF
VICTY_MAX = 0xFFFFFFFF


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------ option blocks ---
def pack_words(words):
    """A stored 6-word block: w0..w4 packed toward w0 (no zero gaps - the client stops
    counting at the first zero, so a word behind a gap could never be referenced again)
    and w5 kept where it is."""
    words = [_int(w) & 0xFFFF for w in (words or [])][:OPTION_WORDS]
    words += [0] * (OPTION_WORDS - len(words))
    head = [w for w in words[:WIRE_OPTION_WORDS] if w]
    head += [0] * (WIRE_OPTION_WORDS - len(head))
    return head + [words[5]]


def wire_words(words):
    """The leading non-zero words the wire carries (packets.canonical_options' rule)."""
    out = []
    for w in pack_words(words)[:WIRE_OPTION_WORDS]:
        if not w:
            break
        out.append(w)
    return out


def block_from_wire(words, extra=0):
    """The stored 6-word block for a descriptor that arrived on the wire as `n` option
    words plus the separate trailing word (C2S 0x0F/0x11/0x13/0x14, and the echo the
    server sends back in 0x19/0x23/0x1D)."""
    head = [_int(w) & 0xFFFF for w in (words or [])][:WIRE_OPTION_WORDS]
    head += [0] * (WIRE_OPTION_WORDS - len(head))
    return pack_words(head + [_int(extra) & 0xFFFF])


def same_block(a, b):
    """Equipment is matched by the byte-exact 12-byte block everywhere in the client."""
    return pack_words(a) == pack_words(b)


def is_zero_block(words):
    return not any(pack_words(words))


# --------------------------------------------------------------- normalizing ---
# The normalizers read the LIVE record of a character another thread may be playing, so
# every container is copied with one C call (list(d.items()), list(l)) before it is walked:
# a Python-level loop over a dict a handler thread is adding a stack to raises "dictionary
# changed size during iteration" (the same rule store.snapshot follows).
def _entry(item_id, words=None):
    return {'id': _int(item_id) & 0xFFFF, 'w': pack_words(words)}


def _apply(data, normalized):
    """Write `normalized` into the live dict, and only when it differs. Never empties it:
    the clear() this replaces left a window in which char['inventory'] had no 'consume' at
    all, which a reader on another thread saw as a KeyError. update() is one C call, so
    after it every key is there; the loop then drops what the shape does not have."""
    if data == normalized:
        return False
    data.update(normalized)
    for key in [k for k in list(data) if k not in normalized]:
        del data[key]
    return True


def _as_qty_map(value):
    """{id: qty} from whatever JSON round-tripping left behind (string keys, a list of
    {'id','qty'} entries from an older shape). Insertion order = slot order."""
    out = {}
    if isinstance(value, dict):
        items = list(value.items())
    elif isinstance(value, list):
        items = [(e.get('id'), e.get('qty', e.get('count', 0))) for e in list(value)
                 if isinstance(e, dict)]
    else:
        items = []
    for key, qty in items:
        item_id, qty = _int(key), _int(qty)
        if item_id > 0 and qty > 0:
            out[item_id] = out.get(item_id, 0) + qty
    return out


def _as_equip_list(value):
    out = []
    for e in list(value) if isinstance(value, list) else []:
        if isinstance(e, dict) and _int(e.get('id')) > 0:
            out.append(_entry(e.get('id'), e.get('w')))
        elif isinstance(e, int) and e > 0:
            out.append(_entry(e))
    return out


def _as_capacities(value):
    caps = [_int(v, DEFAULT_CAPACITY) for v in list(value or [])][:len(TABS)]
    caps += [DEFAULT_CAPACITY] * (len(TABS) - len(caps))
    return [max(MIN_CAPACITY, min(MAX_CAPACITY, c)) for c in caps]


def ensure(char):
    """Normalize (and create) the item and wallet fields of a character record in place.
    Idempotent: the store migration and every Inventory() / Wallet() call run it.

    It keeps the IDENTITY of char['inventory'] and char['equipped']: an Inventory built
    earlier holds a reference to them, so replacing the dicts here would silently orphan it
    and its next add() would write into a record nobody saves.

    A conforming record is left completely untouched: every _bag() / _wallet_of() /
    QuestState() on a handler thread runs this, and the clear() + update() it used to do
    unconditionally emptied a live nested dict the store's save could be serializing at
    that moment ("dictionary changed size during iteration", store.snapshot). Repairing is
    the cold path - the load-time migration - so the comparison costs nothing that matters."""
    if not isinstance(char, dict):
        raise TypeError('character record must be a dict')
    data = char.get('inventory')
    if not isinstance(data, dict):
        data = char['inventory'] = {}
    normalized = {
        'equip': _as_equip_list(data.get('equip')),
        'consume': _as_qty_map(data.get('consume')),
        'etc': _as_qty_map(data.get('etc')),
        'tab_slots': _as_capacities(data.get('tab_slots')),
    }
    _apply(data, normalized)
    worn = char.get('equipped')
    if not isinstance(worn, dict):
        worn = char['equipped'] = {}
    grid = {}
    for slot, entry in list(worn.items()):
        slot = _int(slot, -1)
        if not 0 <= slot <= MAX_EQUIP_SLOT:
            continue
        if isinstance(entry, dict):
            item_id, words = entry.get('id'), entry.get('w')
        else:
            item_id, words = entry, None
        if _int(item_id) > 0:
            grid[slot] = _entry(item_id, words)
    _apply(worn, grid)
    for key, limit in (('gold', GOLD_MAX), ('victy', VICTY_MAX)):
        stored = char.get(key)
        value = max(0, min(limit, _int(stored)))
        # Written only when it really changes (see the docstring). A stored "100" or 100.0
        # is a repair even though it compares equal, so the type is part of the test.
        if value != stored or not isinstance(stored, int) or isinstance(stored, bool):
            char[key] = value
    return data


# -------------------------------------------------------------------- wallet ---
class Wallet:
    """char['gold'] (u64) and char['victy'] (u32) - the single source of truth for every
    absolute currency value the client displays (0x02, 0x03, 0x18, 0x19, 0x3F, 0x66,
    0x68/0x69, 0x88/0x89). The 999999 session defaults and the 0x03 constant 100000 are
    gone: they made the first economy packet jump the label (shop_storage B2/B3)."""

    def __init__(self, char):
        ensure(char)
        self.char = char

    @property
    def gold(self):
        return _int(self.char.get('gold'))

    @gold.setter
    def gold(self, value):
        self.char['gold'] = max(0, min(GOLD_MAX, _int(value)))

    @property
    def victy(self):
        return _int(self.char.get('victy'))

    @victy.setter
    def victy(self, value):
        self.char['victy'] = max(0, min(VICTY_MAX, _int(value)))

    def can_pay(self, gold=0, victy=0):
        return self.gold >= _int(gold) and self.victy >= _int(victy)

    def pay(self, gold=0, victy=0):
        """Charge the wallet. False (and no change) when it cannot pay the full price."""
        if not self.can_pay(gold, victy):
            return False
        self.gold -= _int(gold)
        self.victy -= _int(victy)
        return True

    def earn(self, gold=0, victy=0):
        self.gold += _int(gold)
        self.victy += _int(victy)
        return self.gold

    def as_tuple(self):
        return self.gold, self.victy


# ----------------------------------------------------------------- inventory ---
class Inventory:
    """The three bag tabs plus the equipment grid of one character. Every mutation writes
    straight into the character record; the caller marks the store dirty.

    `pets`: the equipment-tab slots bagged pets take, for a SCRATCH bag built over a copy of
    `inventory` alone (crafting._scratch, trade.simulate, the quest room check): pass the real
    bag's pet_slots(), or the copy would count none and pass an add the real bag refuses.
    None (a character's own bag) counts the character's records live (pet_slots)."""

    def __init__(self, char, catalog=None, pets=None):
        self.data = ensure(char)
        self.char = char
        self.catalog = catalog if catalog is not None else EC.items()
        self._pets = None if pets is None else max(0, _int(pets))

    def pet_slots(self):
        """Equipment-tab slots this bag's bagged pets take (module pet_slots; fixed for a
        scratch bag built with `pets`)."""
        return pet_slots(self.char, self.catalog) if self._pets is None else self._pets

    # ------------------------------------------------------------- content ---
    def tab_of(self, item_id):
        """'equip' / 'consume' / 'etc' for an EN id, or None when the client files it in no
        bag tab (unknown id, skill book Type 3, class change Type 4, cash Type 5)."""
        tab = self.catalog.tab_of(item_id)
        return tab if tab in TABS else None

    def capacity(self, tab):
        return self.data['tab_slots'][TABS.index(tab)]

    def set_capacity(self, tab, value):
        self.data['tab_slots'][TABS.index(tab)] = max(MIN_CAPACITY, min(MAX_CAPACITY, _int(value)))

    def capacities(self):
        return list(self.data['tab_slots'])

    def slots(self, tab):
        """The tab as the client holds it: one entry per SLOT, in slot order.
        equip   -> [{'id', 'w'}]           (one instance per slot)
        consume -> [{'id', 'qty'}]         (a total over 999 occupies several slots)
        etc     -> [{'id', 'qty'}]         (the same at 99)"""
        if tab == 'equip':
            return [dict(e) for e in self.data['equip']]
        limit = STACK_MAX[tab]
        out = []
        for item_id, qty in self.data[tab].items():
            while qty > 0:
                take = min(limit, qty)
                out.append({'id': item_id, 'qty': take})
                qty -= take
        return out

    def used_slots(self, tab):
        """Slots the client's tab holds. The 2009 equipment tab also holds every pet record of
        the character that is not worn (pet_slots): the 2009 0x6F files a pet there
        (FUN_00464e00 case 6), after the 0x03 list - so a full tab refuses the same add on both
        sides (a scratch bag gets the count through `pets`)."""
        n = len(self.slots(tab))
        return n + self.pet_slots() if tab == 'equip' else n

    def free_slots(self, tab):
        return max(0, self.capacity(tab) - self.used_slots(tab))

    def count(self, item_id, words=None):
        """How many of this item the bag holds. For equipment with `words`, how many
        instances carry that exact block; without `words`, every instance of the id."""
        item_id = _int(item_id)
        tab = self.tab_of(item_id)
        if tab is None:
            return 0
        if tab != 'equip':
            return self.data[tab].get(item_id, 0)
        return sum(1 for e in self.data['equip']
                   if e['id'] == item_id and (words is None or same_block(e['w'], words)))

    def has(self, item_id, count=1, words=None):
        return self.count(item_id, words) >= _int(count, 1)

    def totals(self):
        """Flat {id: count} over all three tabs (logs, tests, quest demand checks)."""
        out = {}
        for e in self.data['equip']:
            out[e['id']] = out.get(e['id'], 0) + 1
        for tab in ('consume', 'etc'):
            for item_id, qty in self.data[tab].items():
                out[item_id] = out.get(item_id, 0) + qty
        return out

    # ------------------------------------------------------------- mutation ---
    def fits(self, item_id, count=1, words=None):
        """None when `count` of this item can be added, else the reason it cannot. Mirrors
        the client's own refusal ("There isn't empty space in the inventory"), so the server
        never mutates the model for an add the client would drop."""
        count = _int(count, 1)
        if not self.catalog.exists(item_id):
            return f'item {item_id} is not in the EN client catalog (1..{self.catalog.max_id})'
        if count <= 0:
            return f'count {count}'
        tab = self.tab_of(item_id)
        if tab is None:
            return (f'item {item_id} has Type {self.catalog.type_of(item_id)}: the client '
                    f'consumes it on receipt and files it in no bag tab')
        if tab == 'equip':
            need = count
        else:
            item_id = _int(item_id)
            limit = STACK_MAX[tab]
            have = self.data[tab].get(item_id, 0)
            need = _stack_count(have + count, limit) - _stack_count(have, limit)
        if need > self.free_slots(tab):
            return (f'{tab} tab is full ({self.used_slots(tab)}/{self.capacity(tab)} slots, '
                    f'{need} more needed)')
        return None

    def add(self, item_id, count=1, words=None):
        """Add `count` of an item. Returns the new total for the id, or None when the add
        was refused (unknown id, no bag tab, tab full) - the model must never hold what the
        client silently dropped (S2-11, item_inventory B4)."""
        why = self.fits(item_id, count, words)
        if why is not None:
            return None
        item_id, count = _int(item_id), _int(count, 1)
        tab = self.tab_of(item_id)
        if tab == 'equip':
            for _ in range(count):
                self.data['equip'].append(_entry(item_id, words))
        else:
            self.data[tab][item_id] = self.data[tab].get(item_id, 0) + count
        return self.count(item_id)

    def remove(self, item_id, count=1, words=None):
        """Remove up to `count`; returns how many were actually removed. Stacks are drained
        in slot order and the equip tab is compacted (the later instances move down, which
        S2C 0x24 live-verified)."""
        item_id, count = _int(item_id), _int(count, 1)
        tab = self.tab_of(item_id)
        if tab is None or count <= 0:
            return 0
        if tab == 'equip':
            removed, kept = 0, []
            for e in self.data['equip']:
                if removed < count and e['id'] == item_id and (words is None or same_block(e['w'], words)):
                    removed += 1
                    continue
                kept.append(e)
            self.data['equip'] = kept
            return removed
        have = self.data[tab].get(item_id, 0)
        removed = min(have, count)
        if have - removed > 0:
            self.data[tab][item_id] = have - removed
        else:
            self.data[tab].pop(item_id, None)
        return removed

    def take(self, item_id, count=1, words=None):
        """All-or-nothing remove: True only when the bag held the full `count`."""
        if not self.has(item_id, count, words):
            return False
        return self.remove(item_id, count, words) == _int(count, 1)

    def instance(self, item_id, words=None):
        """The stored equip-tab instance matching id (and block), or None."""
        item_id = _int(item_id)
        for e in self.data['equip']:
            if e['id'] == item_id and (words is None or same_block(e['w'], words)):
                return e
        return None

    # ------------------------------------------------------------ equipment ---
    def slot_for(self, item_id):
        """The grid slot this item's Kind occupies (item_inventory-kind-slot-table), or
        None when the client's FUN_00426680 has no case for it."""
        d = self.catalog.get(item_id)
        if d is None or d.kind < 0:
            return None
        slots, cash_slots, _ring = kind_tables(self.catalog)     # per build (client-2009-world)
        table = cash_slots if d.is_cash else slots
        return table.get(d.kind)

    def equipped(self, slot):
        return self.char['equipped'].get(_int(slot))

    def equipped_items(self):
        return dict(self.char['equipped'])

    def equip(self, item_id, words=None):
        """Put an item in its grid slot. Returns (slot, displaced entry or None), or
        (None, None) when the item has no slot. The block is stored as given; the client
        only copies one for KINDS_WITH_BLOCK, but S2C 0x07 carries a block for every one of
        the 16 regular slots, so the model keeps what it was handed."""
        slot = self.slot_for(item_id)
        if slot is None:
            return None, None
        worn = self.char['equipped']
        entry = _entry(item_id, words)
        d = self.catalog.get(item_id)
        ring_kind = kind_tables(self.catalog)[2]
        if d is not None and d.kind == ring_kind and (not d.is_cash or ring_kind == RING_KIND_2009):
            # FUN_00426680 case 0x11: the old ring in slot 13 moves to slot 14, and only a
            # ring that was already in 14 while 13 was occupied is displaced back to the bag.
            # 2009 FUN_00427af0 case 0x14 (any Cash flag) does the same shift: 14 = 13 and
            # the old 14 is returned when 14 was empty or 13 held a ring.
            first, second = RING_SLOTS
            old_second = worn.get(second)
            displaced = None
            if worn.get(first) is not None:
                worn[second] = worn.pop(first)
                displaced = old_second
            worn[first] = entry
            return first, displaced
        displaced = worn.get(slot)
        worn[slot] = entry
        return slot, displaced

    def unequip(self, slot):
        """Clear a grid slot and return what was in it (None when it was empty)."""
        return self.char['equipped'].pop(_int(slot), None)

    def worn(self, item_id, words=None):
        """(slot, entry) of the worn instance matching id (and the exact block), or
        (None, None). Read-only: the grid lookup S2C 0x1E/0x24 do by memcmp."""
        item_id = _int(item_id)
        for slot, entry in self.char['equipped'].items():
            if entry['id'] == item_id and (words is None or same_block(entry['w'], words)):
                return slot, entry
        return None, None

    def unequip_item(self, item_id, words=None):
        """Clear the slot holding this instance (id + exact block) and return (slot, entry),
        or (None, None)."""
        slot, entry = self.worn(item_id, words)
        if entry is not None:
            del self.char['equipped'][slot]
        return slot, entry

    def wear(self, item_id, words=None, gender=None):
        """The full equip move in the client's own order (item_inventory F3 steps 4-5,
        live-proven by T-S1D/#05): the bag entry goes first and the tab compacts, THEN the
        previously worn instance lands in the first empty slot - which is why the freed
        slot is always there for it - and the stored look is recomposed exactly as the S2C
        0x1D handler recomposes the client's (0x458215, after the grid write). Returns
        (slot, displaced entry or None); (None, None) when the item has no grid slot or the
        bag holds no matching instance, with the model untouched.

        gender: the bool this character's records send (records.record_gender); None falls
        back to the stored per-character value (see look_gender)."""
        slot = self.slot_for(item_id)
        if slot is None or self.instance(item_id, words) is None:
            return None, None
        self.remove(item_id, 1, words)
        slot, displaced = self.equip(item_id, words)
        if displaced is not None and self.add(displaced['id'], 1, displaced['w']) is None:
            # Unreachable: the removal above freed a slot and a ring swap displaces at most
            # one. Logged rather than silently dropped, because the client WOULD keep it
            # (S2C 0x1D adds the old item back with no room check).
            log.error(f'[ITEM] equip {item_id}: no bag room for the displaced item '
                      f'{displaced["id"]}; the client keeps it and the model does not')
        d = self.catalog.get(item_id)
        self.recompose(item_id, d.spr_num if d is not None else 0, gender)
        return slot, displaced

    def take_off(self, item_id, words=None, gender=None):
        """The unequip move (item_inventory F4 steps 2-3): the instance leaves the grid and
        is appended to the bag equip tab, and the stored look is recomposed with Spr 0 as
        the S2C 0x1E handler does (0x458F80). Returns (slot, entry) or (None, None) when no
        worn instance matches id + block, or the bag has no room (the client refuses to send
        then, so a request that still arrives is dropped with the model untouched)."""
        if self.fits(item_id, 1, words) is not None:
            return None, None
        slot, entry = self.unequip_item(item_id, words)
        if entry is None:
            return None, None
        self.add(entry['id'], 1, entry['w'])
        self.recompose(item_id, 0, gender)
        return slot, entry

    # ----------------------------------------------------------- appearance ---
    def look_gender(self, gender=None):
        """The gender bool to compose with: the caller's (the value the records send,
        records.record_gender), else the character's own stored 2009 `gender`, else 0."""
        if gender is None:
            gender = self.char.get('gender')
        return 1 if _int(gender) else 0

    def look_words(self, gender=None):
        """The stored appearance of this build: `look` (14 words), + `look_ext` (words
        14..16) for a 2009 catalog. A malformed `look` is padded with the creation defaults
        of the gender, as records.appearance sends it."""
        size, _limit = compose_limits(self.catalog)
        look = [_int(v) & 0xFFFF for v in list(self.char.get('look') or [])][:LOOK_WORDS]
        if len(look) < LOOK_WORDS:
            import store as storemod          # late: store imports this module
            look += storemod.default_look(self.look_gender(gender))[len(look):]
        if size > LOOK_WORDS:
            ext = [_int(v) & 0xFFFF for v in list(self.char.get('look_ext') or [])][:size - LOOK_WORDS]
            look += ext + [0] * (size - LOOK_WORDS - len(ext))
        return look

    def grid_ids(self):
        """The 25 grid item ids the composition reads - the ones the owner's records carry
        (records.equip_grid / cash half), so an observer that composes from its own copy of
        the grid lands on the same words: a stored slot, else for the cash half 16..24 the
        legacy +0x15C `cash_equip` list those ids used to travel in."""
        grid = [0] * (MAX_EQUIP_SLOT + 1)
        for slot, entry in list(self.char['equipped'].items()):
            if 0 <= _int(slot, -1) <= MAX_EQUIP_SLOT:
                grid[_int(slot)] = _int(entry.get('id')) & 0xFFFF
        legacy = [_int(v) & 0xFFFF for v in list(self.char.get('cash_equip') or [])][:CASH_EQUIP_SLOTS]
        for i, item_id in enumerate(legacy):
            if not grid[EQUIP_SLOTS + i]:
                grid[EQUIP_SLOTS + i] = item_id
        return grid

    def _store_look(self, words):
        """Write the composed words back: `look` (0..13) and, for 2009, `look_ext` (14..16),
        each only when it changed (a new list, never an in-place edit of the one a save may
        be serializing). `weapon` stays a derived copy of layer 11 on records that have it,
        so an older server reading the same accounts.json still renders the same hand."""
        changed = False
        look = list(words[:LOOK_WORDS])
        if self.char.get('look') != look:
            self.char['look'] = look
            changed = True
        if len(words) > LOOK_WORDS:
            ext = list(words[LOOK_WORDS:])
            stored = self.char.get('look_ext')
            if (stored or [0] * len(ext)) != ext:
                self.char['look_ext'] = ext
                changed = True
        if ('weapon' in self.char or look[LAYER_WEAPON]) and self.char.get('weapon') != look[LAYER_WEAPON]:
            self.char['weapon'] = look[LAYER_WEAPON]
        return changed

    def recompose(self, item_id, spr, gender=None):
        """Apply the client's composition (module `compose`) for one grid change of
        `item_id` to the stored look: spr = its Spr_Num after an equip, 0 after an unequip.
        Must run AFTER the grid change, as in the client. True when a stored word changed."""
        d = self.catalog.get(item_id)
        if d is None or d.kind < 0:
            return False
        gender = self.look_gender(gender)
        _size, limit = compose_limits(self.catalog)
        words = self.look_words(gender)
        compose(words, self.grid_ids(), d.kind, d.cash, spr, gender, _job(d, 0), self.catalog.get, limit)
        return self._store_look(words)

    def compose_all(self, gender=None):
        """Replay every worn instance with its own Spr_Num over the stored look: the
        one-time (and idempotent) migration of a look that was stored before the server
        composed it. For a look the client composed, the replay gives it back unchanged
        whatever the order the items were worn in (3000 random equip/unequip sequences on
        the 2009 hii), so it is safe on every login. Layers with nothing worn keep their
        stored words (the creation clothes). True when a stored word changed."""
        changed = False
        for _slot, entry in sorted(list(self.char['equipped'].items()), key=lambda kv: _int(kv[0])):
            d = self.catalog.get(entry.get('id'))
            if d is not None and d.kind >= 0:
                changed = self.recompose(d.id, d.spr_num, gender) or changed
        return changed


def _stack_count(total, limit):
    """How many slots a total occupies at `limit` per stack."""
    return 0 if total <= 0 else (total + limit - 1) // limit


# ------------------------------------------------------------- equip gates ---
def equip_refusal(item_def, level=0, class_id=0, gender_flag=0):
    """None when a character of this level / class / account gender may wear `item_def`
    (an en_content.ItemDef), else the client's own message for the gate that failed.

    The three wearer gates of FUN_0044C2C0, in the client's order (spec 0x44C481/0x0F
    gates_and_hazards):
      gender  def+0x1D4: 1 needs scene+0x130 != 0, 2 needs scene+0x130 == 0, 0 is unisex.
              scene+0x130 is the S2C 0x02 `account_gender_flag` (spec 0x02), which is why
              this takes the ACCOUNT's gender and not anything on the character.
      level   scene+0x979 (the exp-derived level) >= def+0x1D8 `Lv`.
      job     def+0x168 + class*4, i.e. `Job[class]`, or `Job[0]` = every class. Class 0
              (Novice) and the any-class flag are the same word, so a Novice wears exactly
              the Job[0] items.
    The client runs all three BEFORE it sends, so a refusal here means a forged packet or a
    hii the client does not share. C2S 0x0F leaves no pending client state, so the caller
    just drops the request (item_inventory F3 step 3)."""
    d = item_def
    if d is None:
        return 'no EN catalog record'
    if (d.gender == 1 and not gender_flag) or (d.gender == 2 and gender_flag):
        return f'hii Gender {d.gender} vs account flag {int(bool(gender_flag))}: ' \
               f'"You can not equip other gender\'s item."'
    if _int(level) < d.lv:
        return f'level {_int(level)} < hii Lv {d.lv}: "You need higher level"'
    job = list(d.job or [])
    # An index outside the 7-flag row is allowed on purpose: the client would read whatever
    # follows the row, and a wrong refusal would strand an item the player can really wear.
    if job and not job[0] and 0 <= _int(class_id) < len(job) and not job[_int(class_id)]:
        return f'hii Job{job} excludes class {_int(class_id)}: "You can\'t equip other class items."'
    return None


# -------------------------------------------------- per-opcode wire adapters ---
# (id, w[6]) -> the field names each grammar uses for the same item descriptor
# (item_inventory-buff-and-gold-builders, item_inventory.md 3.5). Value =
# (count field, word field, tail field); `always_five` marks the ground-item records whose
# buffer is zeroed once per packet, so they must always carry 5 words.
BLOCK_FIELDS = {
    '0x1D': ('stone_count', 'stone_id', 'block_tail'),          # S2C EquipItem
    '0x1E': ('option_count', 'option_value', 'option_extra'),   # S2C UnequipItemToBag
    '0x24': ('option_count', 'option_value', 'option_extra'),   # S2C UnequipItemRemove
    '0x23': ('opt_count', 'opt', 'opt_extra'),                  # S2C InventoryItemRemove
    '0x19': ('opt_count', 'opt', 'opt_extra'),                  # S2C SellItemResult
    '0x66': ('opt_count', 'opt', 'item_ext'),                   # S2C BankDepositItemResult
    '0x67': ('opt_count', 'opt', 'item_ext'),                   # S2C BankWithdrawItemResult
    '0x11': ('opt_count', 'opt_word', 'opt_tail'),              # S2C ground item list
    '0x12': ('opt_count', 'opt_word', 'opt_tail'),              # S2C ground item drop
    '0x44C481/0x0F': ('stone_count', 'stone_id', 'extra_option'),   # C2S EquipItem
    '0x46A679/0x0C': ('socket_count', 'socket_stone_id', 'item_extra'),  # C2S NpcShopSell
}
ALWAYS_FIVE_WORDS = ('0x11', '0x12')
# Opcodes whose record starts with the entity uid.
UID_FIELD = {'0x1D': 'uid', '0x1E': 'uid', '0x24': 'uid'}


def _block_adapter(key):
    try:
        return BLOCK_FIELDS[key]
    except KeyError:
        raise KeyError(f'{key}: no item-descriptor adapter (add it to BLOCK_FIELDS)') from None


def block_fields(key, words=None):
    """The option-block fields of `key` for a stored 6-word block. Never emits more than
    5 words (packets.LIST_CAPS refuses more; >= 7 corrupts the client stack)."""
    count_field, word_field, tail_field = _block_adapter(key)
    packed = pack_words(words)
    if key in ALWAYS_FIVE_WORDS:
        sent = packed[:WIRE_OPTION_WORDS]
    else:
        sent = wire_words(packed)
    return {count_field: len(sent),
            f'repeat[{count_field}]': [{word_field: w} for w in sent],
            tail_field: packed[5]}


def echo_block_fields(key, words, extra=0):
    """The option block of `key` as it arrived on the wire: the same words, in the same
    order, with the same count. No packing.

    An echo is not a stored block. The client removes an item only when the 12 bytes it
    gets back are the 12 bytes it sent (S2C 0x19 sell, 0x23 remove), and pack_words
    closes zero gaps: a request carrying n=2 [0, 7] echoed as n=1 [7] matches nothing, so
    the gold moves and the item stays in the bag. Rebuilding the block through
    block_fields is right for everything the SERVER decides to send, and wrong for
    everything it hands back."""
    count_field, word_field, tail_field = _block_adapter(key)
    sent = [_int(w) & 0xFFFF for w in (words or [])][:WIRE_OPTION_WORDS]
    return {count_field: len(sent),
            f'repeat[{count_field}]': [{word_field: w} for w in sent],
            tail_field: _int(extra) & 0xFFFF}


def item_fields(key, item_id, words=None, count=None, uid=None, block=None):
    """A full item-descriptor record for `key`: the id, the option block under that
    grammar's field names, and the uid / count fields the opcode carries. `block` takes an
    already-built block (echo_block_fields) instead of packing `words`."""
    rec = {'item_id': _int(item_id) & 0xFFFF}
    uid_field = UID_FIELD.get(key)
    if uid_field:
        rec[uid_field] = _int(uid) & 0xFFFFFFFF
    if count is not None:
        rec['count'] = _int(count) & 0xFFFF
    rec.update(block_fields(key, words) if block is None else block)
    return rec


def gold_fields(wallet_or_gold):
    """S2C 0x3F GoldUpdate {u64 gold}: the gold-only change (quest money, fees, refunds).
    Live-verified: the label changes with no chat line and no item (T-S3F)."""
    gold = wallet_or_gold.gold if isinstance(wallet_or_gold, Wallet) else _int(wallet_or_gold)
    return {'gold': max(0, min(GOLD_MAX, gold))}


def currency_fields(wallet, item_id=0, count=0):
    """S2C 0x18 GetItem {u64 gold, u32 victy, u16 item_id, u16 count}: the absolute wallet
    plus an optional grant. item_id 0 is the pure resync every shop refusal sends."""
    gold, victy = wallet.as_tuple() if isinstance(wallet, Wallet) else (wallet[0], wallet[1])
    return {'gold': max(0, min(GOLD_MAX, _int(gold))), 'victy': max(0, min(VICTY_MAX, _int(victy))),
            'item_id': _int(item_id) & 0xFFFF, 'count': _int(count) & 0xFFFF}


def item_buff_fields(target_uid, item_id, source_uid=0):
    """S2C 0x41 / 0x42 ItemBuffApply {u32 target_uid, u32 source_uid, u16 item_id}. 0x42 is
    the owner's copy (it also refreshes the stat panel), 0x41 the observers'. The buff runs
    for the item's hii `Con` ms (def+0x1C8, proven live: item 148 Con 10000 -> a 10 s
    countdown in the entity's buff record)."""
    return {'target_uid': _int(target_uid) & 0xFFFFFFFF,
            'source_uid': _int(source_uid) & 0xFFFFFFFF,
            'item_id': _int(item_id) & 0xFFFF}


def fields_for_03(char, catalog=None):
    """The wallet, tab capacities and the three item lists of S2C 0x03
    (item_inventory-seed-03-07, item_inventory.md F1 step 2).

    The handler memsets all four client tabs before it reads these lists, so the packet is
    the whole bag: an empty list IS "wipe that tab", which is what the four hardcoded zero
    counts did on every portal (B5). Every list is capped at the capacity byte it is sent
    with and at 45, because the client reads them into u16[45] arrays with no bound check.

    Equipment carries its real 12-byte option block (`option_count` leading non-zero words
    + `item_extra` = w5). Sending zeros there is what let a reinforced item be equipped
    twice: 0x1E could not match the stored block, so it added a bag copy instead of
    clearing the slot (S1-16)."""
    bag = Inventory(char, catalog)
    wallet = Wallet(char)
    fields = {'gold': max(0, min(GOLD_MAX, wallet.gold)),
              'winnie_points': max(0, min(VICTY_MAX, wallet.victy))}
    caps = [max(MIN_CAPACITY, min(MAX_CAPACITY, c)) for c in bag.capacities()]
    cap_fields = ('equip_tab_slots', 'consume_tab_slots', 'etc_tab_slots')
    fields.update(dict(zip(cap_fields, caps)))
    for tab, limit, count_field in zip(TABS, caps,
                                       ('equip_item_count', 'consume_item_count', 'etc_item_count')):
        slots = bag.slots(tab)
        if len(slots) > limit:
            # Only reachable from a hand-edited record: add() refuses a full tab.
            log.warning(f'[ITEM] {tab} tab holds {len(slots)} slots but its capacity is '
                        f'{limit}; the extra slots are not sent (the client would read '
                        f'past its own array)')
            slots = slots[:limit]
        rows = []
        for entry in slots:
            if tab == 'equip':
                words = wire_words(entry['w'])
                rows.append({'item_id': entry['id'], 'option_count': len(words),
                             'repeat[option_count]': [{'option_value': w} for w in words],
                             'item_extra': pack_words(entry['w'])[5]})
            else:
                rows.append({'item_id': entry['id'], 'quantity': entry['qty']})
        fields[count_field] = len(rows)
        fields[f'repeat[{count_field}]'] = rows
    return fields
