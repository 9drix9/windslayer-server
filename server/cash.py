#!/usr/bin/env python3
"""
cash.py - the premium cash model: wallet, catalog, cash inventory, owned-list sync (P8 stage 1)
==============================================================================================
premium_cash-wallet-model, premium_cash-catalog, premium_cash-cash-inventory-api and
premium_cash-owned-list-sync (docs/systems/premium_cash.md 1.2 / 1.3 / 1.7 / F1 / 3.1-3.3;
roadmap P8 order 1). The mall itself (0x6A enter, 0x42 close, 0x43 buy, 0x45 delete, 0x47
gift) and the cash-item uses (0x48 .. 0x4C, 0x70 / 0x71) are the later P8 stages; they build
on this module and nothing here sends a mall packet except the owned list and the balance.

    import cash as CASH
    CASH.ensure_account(acc)                 # wallet + box + gift inbox (store migration)
    CASH.ensure(char)                        # char['cash_items'] (store migration)
    d = CASH.cash_def(1894)                  # the client's own item-table row (hii)
    inv = CASH.CashInventory(store)          # GameServer.cash
    rec = inv.grant(char, 1894, 3)           # a serial-keyed record, persisted (debounced)
    inv.find(char, 1894)                     # FUN_0045E760: the record a use would pick
    inv.consume(char, rec['serial'])         # FUN_00464380: kind 1 -1 (gone at 0)
    CASH.owned_list_packets(char, build)     # S2C 0x6F field dicts (2009: pages)

This module is pure model (store.py imports it for the migration), so it must not import
presence / records / world. Both client builds share every rule below; only the 0x6F framing
differs (2009 `mode` + u8 count paging) and the item table (4248 vs 4322 ids).

Currencies (premium_cash.md 1.3, D16)
-------------------------------------
Wind Cash ("Spark"/cash in the 2009 UI) and Mileage live only in the client's mall object
(2008 mall+0x588 / +0x58C, 2009 +0x590 / +0x594) and are written only by absolute values in
S2C 0x6A / 0x6C / 0x70 / 0x71. They belong to the ACCOUNT (the mall box is account storage),
never to a character, and have no link to the character currency Victy (WSP; char['victy'],
S2C 0x03 / 0x18 `winnie_points`). The wire fields are u32; the store clamps to 0..CASH_MAX
(0x7FFFFFFF) so a signed compare in the client can never see a negative balance.

Persisted shape (store migration; one-time accounts.json.bak-pre-p8)
--------------------------------------------------------------------
account:   cash (Wind Cash), mileage, first_purchase_done (bool), first_purchase_notice
           (bool: the S2C 0x02 cash_first_purchase_flag / 0x70 first_purchase_bonus popup is
           owed), cash_box [record] (the mall box, mall+0x28), gift_inbox [{sender, message,
           item_id, serial, delivered}] (S2C 0x6D source; `!gift` has written it since P5).
           second_password stays optional and is never written (P4).
character: cash_items [record + equipped] (the owned cash list, mall+0x04 / +0x38 when worn).
           The tab capacities are inventory.tab_slots (35..45) and bank_slots (bank_tabs.py)
           already; stats and the look words exist since P1.
record:    {serial, item_id, kind, qty, expire, origin} (+ equipped on a character record):
           the 28-byte client record of premium_cash.md 1.2. kind = the client limit_type
           (1 count, 2 period, 0 permanent, 3 pet), expire = ISO local time text or None (on
           the wire a SYSTEMTIME, None = 16 zero bytes), origin 0 cash / 1 mileage / 2 gift /
           3 event-GM (only 0 and 3 mean anything to the client). A pet record (kind 3) also
           carries pet {exp, awake, level, gauge, name} (below) and is always quantity 1.
Serials are u32, unique across every account and never reused: max(every stored serial) + 1,
at least 0x1000 (ids.CASH_SERIAL), allocated under the store lock.

Pet records (ROADMAP_2009_ADDENDUM C1 / C2; systems_2009/pet.md 2.1-2.3, 3 "Pet record in cash
lists", 7 H5)
----------------------------------------------------------------------------------------------
A 2009 pet is a hii Type 6 / Kind 14 cash item (4294 Picky, 4299 Ulie, 4304 ChikaPuka, 4309
GuriGuri). Its hii Cash_T is 0, but on the wire its record's limit_type must be 3 (CashDef.kind
special-cases Type 6): the S2C 0x6A / 0x6F grammars branch on it and read the pet fields
`u16 exp, u8 awake, u8 level, u8 gauge, str[15] name` in place of quantity..origin - the same
28 bytes. The client uses that record AS the pet's pet_info (local +0x1628), so a kind-0 pet is
a dead record. S2C 0x6C / 0x79 read the 28 bytes raw (one GetDataFromPacket(char*, 0x1C) each,
spec 0x6C / 0x79), so record_bytes() lays a pet out the same way there (raw_fields() gives the
flat field names; the origin byte sits at +0x1A, name[13], past the NUL of a <= 12-byte name).
level 0 = a box pet not yet bound to a character (the client refuses to bag a Type-6 box record
whose level != 0, FUN_00465890 @0x466537); bind_pet() makes it level 1, awake, gauge 100 when
it comes onto a character (pet F13). A pet on the character that is not worn sits in the
client's EQUIPMENT tab (FUN_00464e00 case 6), so inventory.Inventory counts it there; the worn
one (`equipped`) is grid slot 15 (records.cash_rows_2009) and every 0x6F carries it FIRST
(owned_list_packets): 0x6F frees every record, including the one local +0x1628 points at, and
only an equipped kind-3 record in the new list re-binds it (C2, hazard H5). The 2008 client has
no pets and its grammars no limit_type-3 branch: a 2008 list leaves a kind-3 record out.

Catalog source (premium_cash-catalog)
-------------------------------------
The client's own item table is the authority, per build: `hs/windslayer.hii` of the 2008
install (4248 records) and of WindSlayer2009 (4322), read by en_content. Its mall columns are
Cash (def+0x1F0: 1 = sold in the mall, and the flag the client refuses sell / drop / trade /
deposit by), Cash_Cls (def+0x1F4 use type: 1 hair style, 2 hair dye, 14 utility, 15
communication, 17-19 new in 2009), Cash_T (def+0x1F6: 0 permanent, 1 counted, 2 period),
Cash_P (def+0x1FC: the price in Wind Cash - the client compares the balance against THIS
value before it sends a buy), Cash_V (count or days), Cash_B (badge 0..5), Cash_ST (0/1/2/5,
unknown) and Gender (1 male-only, 2 female-only). No other client file carries mall data:
the 2009 install was searched (hs/*.h?i, the .lng tables, list.hcd = the zipped curse-word
list, gameconfigs.ogc = launcher settings, setting.ini / VASData.ini); the mall window lists
the hii rows with Cash 1 itself. Counts: 2008 474 mall items (Cash 1, all priced), 2009 530
(the 474 plus 56 new ids 4253..4322, the 2008 rows' mall columns unchanged). The KR
gamedef.sqlite3 is NOT a price source: it disagrees with the 2008 hii on 6 prices (3206,
3210, 3436, 3437, 3951, 3952), and above 4248 its ids are not the EN ones at all - the EN
2009 hii inserts 4 event items at 4249..4252, so EN id = KR id + 4 there (ROADMAP_2009_ADDENDUM
C3; e.g. EN 4286 counted x250 is KR 4282, while KR 4286 is a permanent Type 1; shifted, all 56
new mall rows equal their KR rows in Type / Cash / Cash_T / Cash_P). So packets'
period gate (S2C 0x72) reads the hii first as well (packets.cash_duration_type), and its
gamedef fallback shifts a 2009 id above 4252 by -4 (4249..4252: no KR row).
The slot extensions 1884..1889 are Cash 0 with Cash_P 4600: the client sells them from its
own slot-extension picker (window 0x1FF), so they are in the catalog but not the mall list.

Owned-list sync (premium_cash-owned-list-sync, F1)
--------------------------------------------------
S2C 0x6F is the full owned list; its handler first frees the client's lists and purges every
cash item (def+0x1F0 != 0) from every bag, so it always carries the whole set. The map load
sends it after the own 0x07 and the 0x28 / 0x44 (GameServer._send_owned_cash): 0x03 memsets
the cash bag tab (2008 scene+0x830, 2009 ci+0x800) and the 0x6F tail rebuilds the period list
only while a local player exists (0x441D85). Expired period records are left out (the expiry
stage removes them with S2C 0x93). One Fireway frame is at most 0x7FF bytes (11-bit length),
so a 0x6F holds at most 70 of its 29-byte rows: 2008 one packet (i32 count), so the owned list
is capped at OWNED_MAX = 70 records in both builds (grant() refuses more); 2009 u8 count, pages
of PAGE_2009 = 70 (mode 0 alone; else 1 first, 2 middle, 3 last). The bag gate (every tab
capacity <= 45) always passes: inventory.MAX_CAPACITY is 45 (packets DEFAULT_ASSUME / _2009 0x6F).
"""
import datetime
import logging
import threading
from dataclasses import dataclass

import en_content as EC
import ids
import inventory as invmod
import packets as P
import social

log = logging.getLogger('WS')

# ---- wire limits ----
CASH_MAX = 0x7FFFFFFF          # u32 on the wire; kept in the positive i32 range (see above)
QTY_MAX = 0xFFFF               # record +0x08 u16
# The cash bag shows the record quantity's LOW BYTE as the stack count (spec 0x6F quantity),
# so a grant merges into an existing record only up to this; more makes further records.
STACK_MAX = 255
# One Fireway frame is at most 0x7FF bytes: the header's size field is 11 bits (PROTOCOL.md
# framing, windslayer_server.SIZE_MASK; the 8-byte header and the opcode byte included), so an
# S2C payload holds at most 2038 bytes. A 0x6F row is is_equipped u8 + the 28-byte record.
FRAME_PAYLOAD_MAX = 0x7FF - 8 - 1
ROW_BYTES = 29
# spec_2009 0x6F: u8 mode + u8 count + rows per page -> 70 rows (the u8 count's 255 would be a
# 7397-byte frame whose length field wraps: a corrupt TCP stream).
PAGE_2009 = (FRAME_PAYLOAD_MAX - 2) // ROW_BYTES
# The owned list's size in BOTH builds: 2008 has no paging (i32 count + rows in one frame ->
# 70), and one cap keeps the builds alike. grant() refuses a new record past it; it is well
# above the cash tab's 45 slots (inventory.MAX_CAPACITY).
OWNED_MAX = (FRAME_PAYLOAD_MAX - 4) // ROW_BYTES

# ---- record fields (premium_cash.md 1.2) ----
KIND_PERMANENT, KIND_COUNT, KIND_PERIOD = 0, 1, 2
KIND_PET = 3                                 # 2009 only: spec_2009 0x6A / 0x6F `limit_type == 3`
RECORD_KINDS = (KIND_PERMANENT, KIND_COUNT, KIND_PERIOD, KIND_PET)
ORIGIN_CASH, ORIGIN_MILEAGE, ORIGIN_GIFT, ORIGIN_EVENT = 0, 1, 2, 3
RECORD_BYTES = 28
ORIGIN_OFFSET = 0x1A                         # record+0x1A (0x6C: origin 3 pops the event text)
SERIAL_MIN = ids.CASH_SERIAL.lo
# spec_2009 0x6F mode: 0 = single page (clear + records + finalize), 1 = first page (clear),
# 2 = middle page (append), 3 = last page (append + finalize).
MODE_SINGLE, MODE_FIRST, MODE_MIDDLE, MODE_LAST = 0, 1, 2, 3

# ---- item constants (premium_cash.md 3.3; [bin] order of FUN_0045E9F0 for SLOT_EXT) ----
SLOT_EXT = {1884: ('tab', 0), 1885: ('tab', 1), 1886: ('tab', 2),
            1887: ('bank', 0), 1888: ('bank', 1), 1889: ('bank', 2)}
STAT_RESET = frozenset({3214, 3215, 3234, 3235})
NAME_CHANGE = 1895
NOTES = social.NOTE_ITEMS                    # 1894 Message Pad, 3320 11 Message Pads
TYPE_PET = 6                                 # 2009 hii Type 6 (4294 / 4299 / 4304 / 4309)
# What grant() may put in the owned list as an ordinary record until the mall / equip stages
# model cash items in the regular tabs: the cash bag's own category (Type 5) only. The 0x6F
# handler (2008 FUN_0045e8e0) bags a record by its template category (0, 1, 5), so a Type 1
# cash costume lands in the client's EQUIPMENT tab in a slot the server's bag model thinks is
# free (later adds and equips drift apart). A 2009 Type 6 pet is granted as a pet record
# (kind 3, ROADMAP_2009_ADDENDUM C1), and since P15 pet-s5 the 2009 pet gear (Type 1, Kind
# 15 / 16: inventory.is_pet_gear) as a permanent record - inventory.Inventory counts both in
# that tab (pet_slots). Every other Type 1 costume stays refused (premium_cash Q14).
GRANT_TYPES = frozenset({EC.TYPE_CASH})

# ---- pets (2009; module docstring "Pet records", systems_2009/pet.md 2.1 / 2.2) ----
# EN 2009 hii ids. The unpatched exe hard-codes the KR-numbered ones (4 lower: pet.md B2,
# cp-2 moves them), so on it these reach the server only through the generic C2S 0x48.
PET_FOOD = frozenset(range(4286, 4290))      # Pet Food 250 / 100 / 50 / 20 ea. (Type 5, counted)
PET_NAME_TICKET = 4322                       # "Pet name making" (Type 5, counted x1)
PET_BELL = 4285                              # Type 0 bag item (the bell is no cash record)
PET_EXP_TABLE = (0, 120, 360, 840, 1800, 3720, 7560, 15240, 30600)     # u16 table 0x5259C4
PET_EXP_MAX = PET_EXP_TABLE[-1]              # 0x7788: the pet window shows 100 %
PET_LEVEL_MAX = 9
PET_GAUGE_MAX = 100                          # percent (the client's pet "HP" / stamina)
PET_BIND_GAUGE = 100                         # a pet bound to a character starts full [I, pet F13]
PET_NAME_MAX = 12                            # bytes: str[13] in 0xAB/0xAD/0xB0/0xC0, str[15] here
PET_NAME_FIELD = 15                          # the record's name field (+0x0D .. +0x1B)
PET_NAME_OFFSET = 0x0D                       # ... its offset: name[13] is the origin byte +0x1A
PET_FIELDS = ('exp', 'awake', 'level', 'gauge', 'name')
MEGA_SUPER = frozenset({3377, 3378, 3379})
MEGA = frozenset({3380, 3381})
REGION_STONE = frozenset({3430, 3432, 3434})
FRIEND_STONE = frozenset({3429, 3431, 3433})
RANDOM_LOOK = frozenset({0xD39, 0xD3A, 0xD50})
GIFT_CARDS = frozenset(range(3327, 3333))

ACCOUNT_FIELDS = ('cash', 'mileage', 'first_purchase_done', 'first_purchase_notice', 'cash_box',
                  'gift_inbox')
CHAR_FIELDS = ('cash_items',)
GIFT_MESSAGE_MAX = 90                        # the 0x6D / 0x3EF popup text (premium_cash F6 step 5)
EXPIRE_FORMAT = '%Y-%m-%dT%H:%M:%S'


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clamp_cash(value):
    return max(0, min(CASH_MAX, _int(value)))


# ----------------------------------------------------------------- catalog ---
@dataclass(frozen=True)
class CashDef:
    """The mall columns of one client item-table row (hii; see the module docstring)."""
    id: int
    type: int
    cash: int                  # def+0x1F0
    cls: int                   # def+0x1F4 Cash_Cls
    duration_type: int         # def+0x1F6 Cash_T
    price: int                 # def+0x1FC Cash_P (Wind Cash)
    value: int                 # Cash_V: count (duration 1) or days (duration 2)
    badge: int                 # Cash_B
    st: int                    # Cash_ST
    gender: int                # 1 male-only, 2 female-only

    @property
    def name(self):
        return EC.item_name(self.id)

    @property
    def is_cash(self):
        """A cash item: flagged Cash (def+0x1F0), or a Type 5 record (the cash bag's own
        category, which the slot extensions and gift cards are although their Cash is 0)."""
        return self.cash != 0 or self.type == EC.TYPE_CASH

    @property
    def sold(self):
        """In the mall list (Cash 1 with a price) or the slot-extension picker (1884..1889)."""
        return (self.cash == 1 and self.price > 0) or self.id in SLOT_EXT

    @property
    def is_pet(self):
        """A 2009 pet (hii Type 6, Kind 14): its records are pet records (kind 3)."""
        return self.type == TYPE_PET

    @property
    def kind(self):
        """The record limit_type a new record of this item gets: 3 for a pet (Type 6, whose hii
        Cash_T is 0: ROADMAP_2009_ADDENDUM C1 / X6), else Cash_T (1 counted, 2 period; 0 for
        anything else)."""
        if self.is_pet:
            return KIND_PET
        return self.duration_type if self.duration_type in (KIND_COUNT, KIND_PERIOD) else KIND_PERMANENT

    def quantity(self, count=None):
        """The quantity a purchase / grant of this item carries: a counted item its Cash_V (a
        "5 Megaphones" record is quantity 5, and a stat reset's quantity is also its allowance,
        live C13) or the explicit count; anything else 1 (2009 forces a period record without
        a date to 1 anyway). grant() splits a counted quantity into records of <= STACK_MAX."""
        if self.kind == KIND_COUNT:
            n = _int(count) if count is not None else (self.value if self.value > 0 else 1)
            return max(1, min(QTY_MAX, n))
        return 1


def cash_def(item_id, catalog=None):
    """The CashDef of an id the configured client's item table has, or None. Every id is
    answered; callers check .is_cash (a cash-bag item) / .sold (offered for Wind Cash)."""
    catalog = catalog if catalog is not None else EC.items()
    d = catalog.get(item_id)
    if d is None or not catalog.exists(item_id):
        return None
    return CashDef(d.id, d.type, d.cash, d.cash_cls, d.cash_t, d.cash_p, d.cash_v, d.cash_b, d.cash_st,
                   d.gender)


def is_cash_item(item_id, catalog=None):
    d = cash_def(item_id, catalog)
    return d is not None and d.is_cash


def duration_type(item_id, catalog=None):
    """def+0x1F6 of this build's client (hii Cash_T), or None for an id it does not have."""
    d = cash_def(item_id, catalog)
    return None if d is None else d.duration_type


def mall_catalog(catalog=None):
    """Every row the client's mall and slot-extension picker offer, by id."""
    catalog = catalog if catalog is not None else EC.items()
    out = []
    for item_id in sorted(catalog.defs):
        d = cash_def(item_id, catalog)
        if d is not None and d.sold:
            out.append(d)
    return out


def has_cash_columns(catalog=None):
    """True when `catalog` (default: the loaded item table) is a client .hii, the only source
    with the mall columns. The fallback en_item_catalog.json (a CLIENT_DIR without hs/) has
    Type only, so every CashDef read from it says Cash 0 / Cash_T 0: a Note granted from it
    became a limit_type 0 record the client's 0x77 never uses up (the 2026-09-25 live bug).
    packets._client_cash_t makes the same check for the S2C 0x72 period gate."""
    catalog = catalog if catalog is not None else EC.items()
    return str(getattr(catalog, 'source', '') or '').lower().endswith('.hii')


def catalog_source(catalog=None):
    catalog = catalog if catalog is not None else EC.items()
    mall = sum(1 for d in mall_catalog(catalog) if d.cash)
    return f'{catalog.source} ({len(catalog)} items, {mall} in the mall)'


# ----------------------------------------------------------------- records ---
def parse_expire(value):
    """A stored expiry (ISO local time text) as a naive datetime, or None."""
    if isinstance(value, datetime.datetime):
        return value.replace(microsecond=0)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value).replace(microsecond=0, tzinfo=None)
    except ValueError:
        return None


def format_expire(when):
    when = parse_expire(when)
    return None if when is None else when.strftime(EXPIRE_FORMAT)


# ------------------------------------------------------------------- pets ---
def pet_level_for(exp):
    """The level a pet's EXP gives (pet.md F4): the first L in 1..8 with exp < T[L], else 9."""
    exp = max(0, _int(exp))
    for level in range(1, PET_LEVEL_MAX):
        if exp < PET_EXP_TABLE[level]:
            return level
    return PET_LEVEL_MAX


def clip_pet_name(value):
    """A pet name as stored: cut at the NUL, at most PET_NAME_MAX bytes of cp949 without
    splitting a double-byte character (every pet packet strcpy's it: pet.md H4)."""
    return P.cut_text(value or b'', PET_NAME_MAX).decode('cp949', 'replace')


def species_name(item_id):
    """The pet item's own EN name (hii Title -> ITMLngKo: 4294 'Picky'), '' when the loaded item
    table does not have it (en_content.item_name's 'item <id>' fallback)."""
    name = EC.item_name(item_id)
    return '' if not name or name.startswith('item ') else clip_pet_name(name)


def normalize_pet(value):
    """The pet fields of a stored pet record, clamped to their wire ranges. Structural (no
    catalog lookup, like every normalizer the store migration runs)."""
    value = value if isinstance(value, dict) else {}
    return {'exp': max(0, min(PET_EXP_MAX, _int(value.get('exp')))),
            'awake': bool(value.get('awake')),
            'level': max(0, min(PET_LEVEL_MAX, _int(value.get('level')))),
            'gauge': max(0, min(PET_GAUGE_MAX, _int(value.get('gauge'), PET_GAUGE_MAX))),
            'name': clip_pet_name(value.get('name'))}


def new_pet(item_id, bound=False):
    """The pet fields of a new pet record: a box pet (level 0 = not bound to a character yet,
    asleep) or, bound, one that came straight onto a character (bind_pet's values)."""
    pet = {'exp': 0, 'awake': False, 'level': 0, 'gauge': PET_BIND_GAUGE, 'name': species_name(item_id)}
    if bound:
        pet.update(level=1, awake=True)
    return pet


def bind_pet(rec):
    """Box -> character (pet F13 [I]): a level-0 pet becomes level 1, EXP 0, gauge 100, awake,
    named after its species unless it has a name. Returns True when the record changed; a
    pet already bound (level >= 1) is left as it is."""
    if not is_pet_record(rec):
        return False
    pet = normalize_pet(rec.get('pet'))
    if pet['level'] >= 1:
        rec['pet'] = pet
        return False
    pet.update(level=1, exp=0, gauge=PET_BIND_GAUGE, awake=True)
    pet['name'] = pet['name'] or species_name(rec.get('item_id'))
    rec['pet'] = pet
    return True


def is_pet_record(rec):
    return isinstance(rec, dict) and _int(rec.get('kind')) == KIND_PET


def foreign_to_client(item_id, client_build, catalog=None):
    """True for an item id the `client_build` client has no hii row for: a store shared with a
    2009 server (accounts.json, config CLIENT_BUILD) can hold 2009-only items - pet gear 4290..
    4308 since the Spark Shop sells it (MALL_PETS) - that must never reach a 2008 client's box,
    owned list or record rows. Judged by the loaded item table when it is that build's (any
    id it lacks); otherwise only the 2009 pet gear is known to be foreign to a 2008 client."""
    if str(client_build or '2008') == '2009':
        return False
    catalog = catalog if catalog is not None else EC.items()
    item_id = _int(item_id)
    if getattr(catalog, 'client_build', None) == str(client_build or '2008'):
        return item_id != 0 and catalog.get(item_id) is None
    return invmod.is_pet_gear(item_id, catalog)


def equipped_pet(char):
    """The character's worn pet record (grid slot 15: records.cash_rows_2009), or None. At most
    one exists (_records clears `equipped` on any later one). Read-only over a snapshot of the
    list - the record builders call it without the store lock, so it must never normalize
    (ensure() may replace the list a concurrent grant appends to)."""
    for rec in list((char or {}).get('cash_items') or []) if isinstance(char, dict) else []:
        if is_pet_record(rec) and rec.get('equipped'):
            return rec
    return None


# Pet records on the character that are not worn: each takes one slot of the 2009 client's
# EQUIPMENT tab (0x6F FUN_00464e00 case 6), which inventory.Inventory.used_slots counts through
# inventory.pet_slots (the 2009 item table only: the 2008 0x6F leaves pets out).
bagged_pets = invmod.bagged_pets
assert invmod.PET_RECORD_KIND == KIND_PET


def _record(entry, char_side):
    """One stored record, normalized, or None when it cannot be one (no serial, no item, a
    used-up counted record: the client frees those at 0 too, FUN_00464380)."""
    if not isinstance(entry, dict):
        return None
    serial, item_id = _int(entry.get('serial')), _int(entry.get('item_id'))
    if serial not in ids.CASH_SERIAL or not 1 <= item_id <= 0xFFFF:
        return None
    # limit_type: 1 count, 2 period, 3 a 2009 pet (spec_2009 0x6F: the pet fields replace
    # quantity..origin); anything else is a permanent record (0).
    kind = _int(entry.get('kind'))
    kind = kind if kind in RECORD_KINDS else KIND_PERMANENT
    if kind == KIND_PET:
        rec = {'serial': serial, 'item_id': item_id, 'kind': kind, 'qty': 1, 'expire': None,
               'origin': _int(entry.get('origin')) & 0xFF, 'pet': normalize_pet(entry.get('pet'))}
        if char_side:
            rec['equipped'] = bool(entry.get('equipped'))
        return rec
    qty = max(0, min(QTY_MAX, _int(entry.get('qty'), 1)))
    if kind == KIND_COUNT and qty <= 0:
        return None
    rec = {'serial': serial, 'item_id': item_id, 'kind': kind, 'qty': qty,
           'expire': format_expire(entry.get('expire')),
           'origin': _int(entry.get('origin')) & 0xFF}
    if char_side:
        rec['equipped'] = bool(entry.get('equipped'))
    return rec


def _records(value, char_side):
    out, seen = [], set()
    worn_pet = False
    for entry in list(value) if isinstance(value, list) else []:
        rec = _record(entry, char_side)
        if rec is None or rec['serial'] in seen:
            continue
        if char_side and rec['kind'] == KIND_PET and rec['equipped']:
            # one pet slot (grid 15): a second worn pet record is a bagged one
            rec['equipped'], worn_pet = not worn_pet, True
        seen.add(rec['serial'])
        out.append(rec)
    return out


def _gift(entry):
    if not isinstance(entry, dict):
        return None
    item_id = _int(entry.get('item_id'))
    if not 1 <= item_id <= 0xFFFF:
        return None
    serial = _int(entry.get('serial'))
    return {'sender': str(entry.get('sender') or '')[:16],
            'message': str(entry.get('message') or '')[:GIFT_MESSAGE_MAX],
            'item_id': item_id,
            # 0 = no box record yet (a P5 `!gift` entry); the gift stage gives it one.
            'serial': serial if serial in ids.CASH_SERIAL else 0,
            'delivered': bool(entry.get('delivered'))}


_MISSING = object()


def ensure_account(acc):
    """Create / normalize the account's wallet, box and gift inbox in place (idempotent; a
    conforming account is left untouched). Returns the account."""
    inbox = acc.get('gift_inbox')
    gifts = [_gift(e) for e in (list(inbox) if isinstance(inbox, list) else [])]
    fixed = {'cash': clamp_cash(acc.get('cash')), 'mileage': clamp_cash(acc.get('mileage')),
             'first_purchase_done': bool(acc.get('first_purchase_done')),
             'first_purchase_notice': bool(acc.get('first_purchase_notice')),
             'cash_box': _records(acc.get('cash_box'), False),
             'gift_inbox': [g for g in gifts if g is not None]}
    for key, value in fixed.items():
        stored = acc.get(key, _MISSING)
        # The type is part of the test: a stored "5" or 5.0 is a repair although it compares equal.
        if stored != value or type(stored) is not type(value):
            acc[key] = value
    return acc


def ensure(char):
    """Create / normalize char['cash_items'] in place (idempotent). Returns the list."""
    value = char.get('cash_items')
    rows = _records(value, True)
    if value != rows:
        char['cash_items'] = rows
    return char['cash_items']


def make_record(serial, item_id, kind, qty, *, expire=None, origin=ORIGIN_EVENT, equipped=None, pet=None):
    """A new record. kind 3 makes a pet record (quantity 1, no expiry) with `pet` (default: a
    new unbound box pet, new_pet)."""
    kind = int(kind) if int(kind) in RECORD_KINDS else KIND_PERMANENT
    if kind == KIND_PET:
        rec = {'serial': int(serial), 'item_id': int(item_id), 'kind': kind, 'qty': 1, 'expire': None,
               'origin': int(origin) & 0xFF,
               'pet': normalize_pet(pet if pet is not None else new_pet(item_id))}
    else:
        rec = {'serial': int(serial), 'item_id': int(item_id), 'kind': kind,
               'qty': max(0, min(QTY_MAX, int(qty))), 'expire': format_expire(expire),
               'origin': int(origin) & 0xFF}
    if equipped is not None:
        rec['equipped'] = bool(equipped)
    return rec


def new_record(serial, d, origin, count=None, *, bound=False, equipped=None):
    """A new record of CashDef `d` as a purchase / grant delivers it: kind and quantity from
    the hii (CashDef.kind / quantity), a pet as a pet record (unbound in the box, bound on a
    character)."""
    pet = new_pet(d.id, bound=bound) if d.kind == KIND_PET else None
    return make_record(serial, d.id, d.kind, d.quantity(count), origin=origin, equipped=equipped, pet=pet)


def is_activated(rec):
    """A period record with a date: the client neither bags nor uses it (FUN_0045E760)."""
    return rec.get('kind') == KIND_PERIOD and parse_expire(rec.get('expire')) is not None


def local_now(now=None):
    """`now` (None = the server clock, an epoch number, a datetime or ISO text) as a naive
    local datetime - the clock the stored expiries are written in."""
    if now is None:
        return datetime.datetime.now().replace(microsecond=0)
    if isinstance(now, (int, float)):
        return datetime.datetime.fromtimestamp(now).replace(microsecond=0)
    return parse_expire(now) or datetime.datetime.now().replace(microsecond=0)


def is_expired(rec, now=None):
    """A period record whose date has passed (the client prints '[x] is expired.' on 0x93)."""
    when = parse_expire(rec.get('expire'))
    if rec.get('kind') != KIND_PERIOD or when is None:
        return False
    return when <= local_now(now)


def usable(rec):
    """FUN_0045E760's match: quantity > 0 and not an activated period record."""
    return _int(rec.get('qty')) > 0 and not is_activated(rec)


def systemtime(rec):
    """The record's expiry as the 8 u16 of a SYSTEMTIME (0 = Sunday), zeros when none."""
    when = parse_expire(rec.get('expire'))
    if when is None:
        return [0] * 8
    return [when.year, when.month, (when.weekday() + 1) % 7, when.day, when.hour, when.minute,
            when.second, 0]


SYSTEMTIME_FIELDS = ('expire_year', 'expire_month', 'expire_day_of_week', 'expire_day', 'expire_hour',
                     'expire_minute', 'expire_second', 'expire_milliseconds')


PET_WIRE_FIELDS = ('pet_exp', 'pet_summoned', 'pet_level', 'pet_gauge_pct', 'pet_name')


def wire_record(rec):
    """The 28-byte record's fields in the S2C 0x6A / 0x6F grammars: a pet record (kind 3) in
    the spec_2009 `limit_type == 3` branch (pet_exp, pet_summoned, pet_level, pet_gauge_pct,
    str[15] pet_name), any other the quantity / SYSTEMTIME / origin form. The flat 0x79
    grammar (and the 2008 ones) take raw_fields(). Every form is the same 28 bytes as
    record_bytes(): the pet branch's pet_name is record +0x0D..+0x1B raw (P.RawChars), so it
    carries the origin at name[13] (+0x1A) - the byte the box delete dialog reads (0 = cash
    bought, a refund shown) - as 0x6C / 0x79 do, and an event or gifted pet listed by the
    next 0x6A shows no refund mall.delete_refund would not pay."""
    fields = {'serial': int(rec['serial']) & 0xFFFFFFFF, 'item_id': int(rec['item_id']) & 0xFFFF,
              'limit_type': int(rec.get('kind', 0)) & 0xFF, 'unk_07': 0}
    if is_pet_record(rec):
        pet = normalize_pet(rec.get('pet'))
        name = P.RawChars(record_bytes(rec)[PET_NAME_OFFSET:PET_NAME_OFFSET + PET_NAME_FIELD])
        fields.update(zip(PET_WIRE_FIELDS, (pet['exp'], int(pet['awake']), pet['level'], pet['gauge'], name)))
        return fields
    fields['quantity'] = max(0, min(QTY_MAX, _int(rec.get('qty'))))
    fields.update(zip(SYSTEMTIME_FIELDS, systemtime(rec)))
    fields.update({'origin': int(rec.get('origin', 0)) & 0xFF, 'unk_1b': 0})
    return fields


def expire_bytes(rec):
    """The SYSTEMTIME as the 16 raw bytes S2C 0x6C / 0x72 carry (`bytes[16] expire_time`)."""
    return b''.join(int(v & 0xFFFF).to_bytes(2, 'little') for v in systemtime(rec))


def record_bytes(rec):
    """The 28-byte client record exactly as S2C 0x6C / 0x79 deliver it (both read it with one
    GetDataFromPacket(char*, 0x1C) into the record: spec 0x6C 'ONE 28-byte buffer', 0x79
    'copies the payload raw'). A pet record is laid out as the 0x6A / 0x6F pet branch reads it
    (+0x08 u16 exp, +0x0A awake, +0x0B level, +0x0C gauge, +0x0D char[15] name) with the origin
    in its usual byte +0x1A - name[13], past the NUL of a <= 12-byte name - so an event pet
    (origin 3) still pops the 0x6C event text. [I: the raw reads are V, a pet through them is
    untested live - pet stage 7 / T-E6.]"""
    head = ((int(rec['serial']) & 0xFFFFFFFF).to_bytes(4, 'little') + (int(rec['item_id']) & 0xFFFF).to_bytes(2, 'little')
            + bytes((int(rec.get('kind', 0)) & 0xFF, 0)))
    origin = int(rec.get('origin', 0)) & 0xFF
    if is_pet_record(rec):
        pet = normalize_pet(rec.get('pet'))
        body = bytearray(pet['exp'].to_bytes(2, 'little') + bytes((int(pet['awake']), pet['level'], pet['gauge']))
                         + P.cut_text(pet['name'], PET_NAME_MAX).ljust(PET_NAME_FIELD, b'\x00'))
        body[ORIGIN_OFFSET - 8] = origin
        return head + bytes(body)
    qty = max(0, min(QTY_MAX, _int(rec.get('qty'))))
    return head + qty.to_bytes(2, 'little') + expire_bytes(rec) + bytes((origin, 0))


def raw_fields(rec):
    """record_bytes() under the flat field names of the 0x79 grammar (and the 2008 0x6A / 0x6F
    rows): the same as wire_record() for every record but a pet, whose bytes it carries in
    place (quantity = its exp, the SYSTEMTIME words = awake .. name[12], origin = +0x1A)."""
    raw = record_bytes(rec)
    fields = {'serial': int.from_bytes(raw[0:4], 'little'), 'item_id': int.from_bytes(raw[4:6], 'little'),
              'limit_type': raw[6], 'unk_07': raw[7], 'quantity': int.from_bytes(raw[8:10], 'little')}
    fields.update(zip(SYSTEMTIME_FIELDS, (int.from_bytes(raw[i:i + 2], 'little') for i in range(10, 26, 2))))
    fields.update({'origin': raw[26], 'unk_1b': raw[27]})
    return fields


def owned_records(char, now=None):
    """The records the next 0x6F carries: the character's list minus expired period records."""
    return [r for r in ensure(char) if not is_expired(r, now)]


def pet_first(records):
    """`records` with the worn pet record first (stable otherwise): it goes out in the FIRST
    0x6F page - the one whose mode 0 / 1 frees every record, local +0x1628's included - so
    the pointer is re-bound by the same packet that freed it (ROADMAP_2009_ADDENDUM C2, pet
    H5), never across a frame boundary the game loop could run between."""
    return sorted(records, key=lambda r: not (is_pet_record(r) and r.get('equipped')))


def owned_list_packets(char, client_build, now=None, records=None):
    """S2C 0x6F field dicts for the whole owned list. 2008: one packet (i32 count, 0 = none)
    of at most OWNED_MAX records - one Fireway frame; any more are left out with an error
    (grant() never makes them). 2009: pages of <= PAGE_2009 records (u8 count) - mode 0 for a
    single page, else 1 / 2.. / 3 - with the worn pet record first (pet_first, C2). A pet
    record is never left out of a 2009 list (the whole list always goes); a 2008 list leaves
    one out (no pets, no limit_type-3 branch in its grammar: store data from a 2009 server),
    and any record of an item the 2008 hii lacks (foreign_to_client: 2009 pet gear)."""
    records = list(owned_records(char, now) if records is None else records)
    build = str(client_build or '2008')
    if build != '2009':
        pets = [r for r in records if is_pet_record(r)]
        if pets:
            # WARNING, not ERROR: a store shared with a 2009 server (config CLIENT_BUILD) logs
            # this on every map load of the character; the 2008 bag counts no pet either
            # (inventory.pet_slots), so nothing is out of step.
            log.warning(f'[CASH] {(char or {}).get("name")!r}: {len(pets)} pet record(s) '
                        f'{[hex(r["serial"]) for r in pets]} left out of the 2008 0x6F (the 2008 client has no pets)')
            records = [r for r in records if not is_pet_record(r)]
        foreign = [r for r in records if foreign_to_client(r.get('item_id'), build)]
        if foreign:
            # the same store sharing: 2009-only items (pet gear bought in the 2009 Spark Shop)
            log.warning(f'[CASH] {(char or {}).get("name")!r}: {len(foreign)} record(s) of item(s) the 2008 '
                        f'client lacks {[(r.get("item_id"), hex(_int(r.get("serial")))) for r in foreign]} '
                        f'left out of the 2008 0x6F')
            records = [r for r in records if not foreign_to_client(r.get('item_id'), build)]
    else:
        records = pet_first(records)
    rows = [{'is_equipped': 1 if r.get('equipped') else 0, **wire_record(r)} for r in records]
    if build != '2009':
        if len(rows) > OWNED_MAX:
            # Last line of defence: a longer 0x6F wraps the 11-bit frame length and corrupts
            # the stream on every map load (the records are persisted, so every later one too).
            log.error(f'[CASH] {(char or {}).get("name")!r} owns {len(rows)} cash records; the 2008 0x6F '
                      f'carries {OWNED_MAX} (one frame): left out serials '
                      f'{[hex(r["serial"]) for r in rows[OWNED_MAX:]]}')
            rows = rows[:OWNED_MAX]
        return [{'count': len(rows), 'repeat[count]': rows}]
    pages = [rows[i:i + PAGE_2009] for i in range(0, len(rows), PAGE_2009)] or [[]]
    out = []
    for n, page in enumerate(pages):
        if len(pages) == 1:
            mode = MODE_SINGLE
        else:
            mode = MODE_FIRST if n == 0 else MODE_LAST if n == len(pages) - 1 else MODE_MIDDLE
        out.append({'mode': mode, 'count': len(page), 'repeat[count]': page})
    return out


def balance(acc):
    """(Wind Cash, Mileage) of an account record."""
    acc = acc or {}
    return clamp_cash(acc.get('cash')), clamp_cash(acc.get('mileage'))


def balance_fields(acc, first_purchase_bonus=0):
    """S2C 0x70 CashMileageBalance (identical in both builds): both absolute balances; the
    bonus byte 1 pops "Thank you for your first purchase..." (live-verified T-70)."""
    cash, mileage = balance(acc)
    return {'cash_balance': cash, 'mileage_balance': mileage,
            'first_purchase_bonus': 1 if first_purchase_bonus else 0}


# ---------------------------------------------------------------- runtime ---
class CashError(ValueError):
    pass


class CashInventory:
    """The cash inventory API over the store (GameServer.cash). Every mutation runs under
    store.lock (db_lock) and marks the store dirty; nothing here sends a packet."""

    def __init__(self, store):
        self.store = store
        self._counter = None
        self._counter_lock = threading.Lock()

    # ------------------------------------------------------------- serials ---
    def stored_serials(self):
        """Every serial in the store: boxes, gift inboxes and characters' lists."""
        out = []
        with self.store.lock:
            for acc in self.store.accounts.values():
                out.extend(_int(r.get('serial')) for r in acc.get('cash_box') or [] if isinstance(r, dict))
                out.extend(_int(g.get('serial')) for g in acc.get('gift_inbox') or [] if isinstance(g, dict))
                for char in acc.get('characters') or []:
                    out.extend(_int(r.get('serial')) for r in char.get('cash_items') or []
                               if isinstance(r, dict))
        return [s for s in out if s in ids.CASH_SERIAL]

    def next_serial(self):
        """A fresh serial: max(stored) + 1, at least 0x1000, never reused (F4 step 6).
        Lock order store.lock -> _counter_lock, the same as grant(): the first call seeds the
        counter from the store, so taking _counter_lock first could deadlock against a grant."""
        with self.store.lock, self._counter_lock:
            if self._counter is None:
                used = self.stored_serials()
                start = max([SERIAL_MIN - 1] + used) + 1
                self._counter = ids.Counter(ids.CASH_SERIAL, start=start)
            return self._counter.next()

    # ---------------------------------------------------------------- reads ---
    def items(self, char):
        with self.store.lock:
            return ensure(char)

    def by_serial(self, char, serial):
        serial = _int(serial)
        with self.store.lock:
            for rec in ensure(char):
                if rec['serial'] == serial:
                    return rec
        return None

    def find(self, char, item_id):
        """The record a use of `item_id` picks - the client's own lookup (2008 FUN_0045E760):
        the first one with that id, quantity > 0 and not an activated period item; a worn
        record (+0x38 list) is not in the owned list the lookup walks."""
        item_id = _int(item_id)
        if char is None:
            return None
        with self.store.lock:
            for rec in ensure(char):
                if rec['item_id'] == item_id and usable(rec) and not rec.get('equipped'):
                    return rec
        return None

    # ------------------------------------------------------------ mutations ---
    def consume(self, char, serial, n=1, what='use'):
        """Mirror the client's consume-by-serial (2008 FUN_00464380, 2009 FUN_0046df70): a
        counted record loses `n` and is freed at 0; a period or permanent record is left as it
        is (the client acts only on limit_type 1 / 2, and a period item is activated, not
        used up). Returns the quantity left (0 = gone), or None for an unknown serial."""
        serial, n = _int(serial), max(0, _int(n))
        if char is None:
            return None
        with self.store.lock:
            items = ensure(char)
            for i, rec in enumerate(items):
                if rec['serial'] != serial:
                    continue
                if rec['kind'] == KIND_COUNT and n:
                    rec['qty'] = max(0, rec['qty'] - n)
                    if rec['qty'] == 0:
                        del items[i]
                    left = rec['qty']
                    break
                return rec['qty']
            else:
                return None
        self.store.mark_dirty(f'cash {what} {char.get("name")}')
        return left

    def grant(self, char, item_id, count=None, *, origin=ORIGIN_EVENT, merge=True, what='grant'):
        """Put a cash item on the character (its owned list) as the mall would deliver it: a
        counted item merges into a usable record of the same id and origin up to STACK_MAX
        (one bag slot, one serial), anything else is a new record; a count above STACK_MAX
        becomes several records of <= STACK_MAX (the bag shows only the quantity's low byte).
        A 2009 pet (Type 6) becomes one bound pet record (_grant_pet, C1), a 2009 pet gear item
        (Type 1 cash, Kind 15 / 16) one permanent record (_grant_gear, P15 pet-s5).
        Returns the (first) record written.
        Raises CashError when the loaded item table is not a client .hii (has_cash_columns:
        no Cash_T, so every record would be limit_type 0), for an id the client has not got,
        that is no cash item, that is not a cash-bag item (GRANT_TYPES: costumes) or a pet, a
        slot extension or a Cash 0 item (the gift certificates, Add Quick Slot), when the new
        records would take the owned list past OWNED_MAX (one 0x6F frame), and for a pet when
        the equipment tab is full."""
        catalog = EC.items()
        if not has_cash_columns(catalog):
            raise CashError(f'the loaded item table ({getattr(catalog, "source", None)}) is not a client .hii: it has '
                            f'no cash columns, so every record would be limit_type 0 (never used up by the client); '
                            f'check CLIENT_DIR')
        d = cash_def(item_id, catalog)
        if d is None:
            raise CashError(f'item {item_id} is not in the client item table')
        if not d.is_cash:
            raise CashError(f'{d.name or item_id} ({d.id}) is not a cash item (Type {d.type}, Cash {d.cash})')
        if d.id in SLOT_EXT:
            raise CashError(f'{d.name or item_id} ({d.id}) is a slot extension: it raises a tab, it is not kept')
        if d.is_pet:
            return self._grant_pet(char, d, origin, what)
        if invmod.is_pet_gear(d.id, catalog):
            return self._grant_gear(char, d, origin, what)
        if d.type not in GRANT_TYPES:
            raise CashError(f'{d.name or item_id} ({d.id}) is a cash costume (Type {d.type}): the client puts it '
                            f'in the equipment tab, which the cash model does not track yet (P8 mall / equip)')
        if not d.cash:
            raise CashError(f'{d.name or item_id} ({d.id}) is a Cash 0 item: the client 0x6F purges only Cash '
                            f'items (def+0x1F0 != 0) before it re-inserts the list, so every in-world re-send '
                            f'would add a second icon of it; not granted until the mall / gift stages model it')
        qty = d.quantity(count)
        with self.store.lock:
            items = ensure(char)
            rec = None
            if merge and d.kind == KIND_COUNT:
                for cand in items:
                    if (cand['item_id'] == d.id and cand['origin'] == origin and usable(cand)
                            and not cand.get('equipped') and cand['qty'] + qty <= STACK_MAX):
                        rec = cand
                        break
            if rec is not None:
                rec['qty'] += qty
                serials = [rec['serial']]
            else:
                chunks = ([STACK_MAX] * (qty // STACK_MAX) + ([qty % STACK_MAX] if qty % STACK_MAX else [])
                          if d.kind == KIND_COUNT else [qty])
                carried = len(owned_records(char))
                if carried + len(chunks) > OWNED_MAX:
                    raise CashError(f'owned list full: at most {OWNED_MAX} cash records (one S2C 0x6F frame); '
                                    f'{char.get("name")} has {carried}, {len(chunks)} more do not fit')
                new = [make_record(self.next_serial(), d.id, d.kind, n, origin=origin, equipped=False)
                       for n in chunks]
                items.extend(new)
                rec, serials = new[0], [r['serial'] for r in new]
        self.store.mark_dirty(f'cash {what} {char.get("name")}')
        log.info(f'[CASH] {what}: {char.get("name")!r} +{qty} x {d.name or d.id} ({d.id}) -> serial(s) '
                 f'{", ".join(f"{s:#x}" for s in serials)} kind {rec["kind"]} qty {rec["qty"]}')
        return rec

    def _grant_pet(self, char, d, origin, what):
        """A pet onto the character (ROADMAP_2009_ADDENDUM C1): a BOUND pet record (kind 3, level
        1, awake, gauge 100: pet F13), not worn - the client files it in its EQUIPMENT tab
        (0x6F FUN_00464e00 case 6), so that tab needs a free slot (inventory.Inventory counts
        the bagged pets in it). Wearing it is C2S 0x82 -> S2C 0xAB (P15 pet-s2)."""
        with self.store.lock:
            items = ensure(char)
            carried = len(owned_records(char))
            if carried + 1 > OWNED_MAX:
                raise CashError(f'owned list full: at most {OWNED_MAX} cash records (one S2C 0x6F frame); '
                                f'{char.get("name")} has {carried}')
            bag = invmod.Inventory(char)
            if bag.free_slots('equip') < 1:
                raise CashError(f'{d.name or d.id} ({d.id}) is a pet: the client puts it in the equipment tab, '
                                f'which is full ({bag.used_slots("equip")}/{bag.capacity("equip")})')
            rec = new_record(self.next_serial(), d, origin, bound=True, equipped=False)
            items.append(rec)
        self.store.mark_dirty(f'cash {what} {char.get("name")}')
        log.info(f'[CASH] {what}: {char.get("name")!r} + pet {d.name or d.id} ({d.id}) -> serial '
                 f'{rec["serial"]:#x} kind 3 {rec["pet"]}')
        return rec

    def _grant_gear(self, char, d, origin, what):
        """2009 pet gear onto the character (P15 pet-s5; inventory.is_pet_gear): one permanent
        record (Cash_T 0 -> kind 0, quantity 1), not worn. The 0x6F files a Type 1 cash record in
        the client's EQUIPMENT tab (FUN_00464e00 case 1), so that tab needs a free slot
        (inventory.pet_slots counts the bagged gear records there). Worn through the normal
        C2S 0x0F -> S2C 0x1D (pets.Pets.equip_gear), which moves the client's record from the
        owned to the equipped list (FUN_00462b70) - the record's `equipped`."""
        with self.store.lock:
            items = ensure(char)
            carried = len(owned_records(char))
            if carried + 1 > OWNED_MAX:
                raise CashError(f'owned list full: at most {OWNED_MAX} cash records (one S2C 0x6F frame); '
                                f'{char.get("name")} has {carried}')
            bag = invmod.Inventory(char)
            if bag.free_slots('equip') < 1:
                raise CashError(f'{d.name or d.id} ({d.id}) is pet gear: the client puts it in the equipment tab, '
                                f'which is full ({bag.used_slots("equip")}/{bag.capacity("equip")})')
            rec = new_record(self.next_serial(), d, origin, equipped=False)
            items.append(rec)
        self.store.mark_dirty(f'cash {what} {char.get("name")}')
        log.info(f'[CASH] {what}: {char.get("name")!r} + pet gear {d.name or d.id} ({d.id}) -> serial '
                 f'{rec["serial"]:#x} kind {rec["kind"]}')
        return rec

    def set_balance(self, acc, cash=None, mileage=None, what='balance'):
        """Write Wind Cash and / or Mileage (absolute, clamped). Returns (cash, mileage)."""
        with self.store.lock:
            ensure_account(acc)
            if cash is not None:
                acc['cash'] = clamp_cash(cash)
            if mileage is not None:
                acc['mileage'] = clamp_cash(mileage)
            result = balance(acc)
        self.store.mark_dirty(f'cash {what}')
        return result

    # ----------------------------------------------------------------- views ---
    def owned_packets(self, char, client_build, now=None):
        """(0x6F field dicts, record count) of the character's owned list, under the lock. The
        count is what the pages carry (a 2008 list leaves a pet record out)."""
        with self.store.lock:
            records = owned_records(char, now)
            pages = owned_list_packets(char, client_build, now, records=[dict(r) for r in records])
        return pages, sum(int(p['count']) for p in pages)

    def describe(self, acc, char):
        """Lines for `!cash`: the wallet, the box and the owned list."""
        with self.store.lock:
            cash, mileage = balance(acc)
            box = list(ensure_account(acc)['cash_box'])
            owned = list(ensure(char)) if char is not None else []
        lines = [f'Wind Cash {cash}, Mileage {mileage}; box {len(box)}, owned {len(owned)}.']
        for rec in owned[:8]:
            d = cash_def(rec['item_id'])
            tail = f' until {rec["expire"]}' if rec.get('expire') else ''
            if is_pet_record(rec):
                pet = normalize_pet(rec.get('pet'))
                tail = (f' pet {pet["name"]!r} Lv{pet["level"]} exp {pet["exp"]} {pet["gauge"]}% '
                        f'{"awake" if pet["awake"] else "asleep"}{", worn" if rec.get("equipped") else ""}')
            lines.append(f'{rec["serial"]:#x} {(d.name if d else "") or rec["item_id"]} ({rec["item_id"]}) '
                         f'kind {rec["kind"]} x{rec["qty"]}{tail}')
        if len(owned) > 8:
            lines.append(f'... and {len(owned) - 8} more')
        return lines
