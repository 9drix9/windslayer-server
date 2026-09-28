#!/usr/bin/env python3
"""
shop.py - the NPC-shop rules the client applies before C2S 0x0B / 0x3C (shop_storage-npc-buy)
============================================================================================
Pure functions, no packets. The client decides what a merchant offers and what a purchase
costs on its own and only then sends C2S 0x0B {item_id, qty, npc_id}; the server re-derives
the same answers from the same data and must never trust the client's arithmetic
(shop_storage.md 1.3/1.4, F1):

    import shop as SH
    SH.offered([3, 5, 260], 261, learned=[260])   # 260: the row that shows 261
    d = SH.discount('2008', item_type=0, manner=250)          # 0.9
    SH.cost(50, 3, d)                                           # 135
    SH.bank_fee(0)                                              # 50

Sources (decomp, both builds read for this stage):
  listing   FUN_00465a80 (2008) - window 0xD rows from the merchant's hni `item:` column
            (record+0x278, 2009 +0x288). A Type-3 row whose def+0x142 (skills.family_max_level)
            is > 0 shows the NEXT level of the family: for j in 0..maxlv-1, the first learned
            id base+j turns the row into base+j+1, or base+j itself at the family's top
            level. Only the shown id can be selected, so it is the only id the row sells.
  price     FUN_00467680 (2008) / FUN_00471450 (2009): cost = ROUND(qty x price x d) for the
            gold price (hii Buy, def+0x1E0) and the Victy price (hii PMoney, def+0x1E8), with
            d = 0.9 for an NPC purchase (npc flag != 0) of a non-skill item (Type != 3) when
            manner (player_info+0xE98 / 2009 +0xEB0) > 199, else 1.0; a discounted cost that
            rounds to 0 becomes 1; a price of 0 is free (the check is skipped). ROUND is the
            x87 default (round half to even) = Python's round() on the same double.
            2009 multiplies d by 0.95 once for each of equip grid slots 1 and 2 (entity
            +0x152 / +0x154 = the 0x07 equip ids of slots 1, 2) whose option word 0
            (+0x18E / +0x19A = equip_item_attr j 0) is 2 (FUN_00471450 @0x4714A0..0x4714E4).
  bank fee  FUN_00469090 case 0x1A8 (2008) / FUN_00472f40 (2009, same bytes): manner >= 100
            pays 0, 30..99 pays 25, below 30 pays 50 - `((manner < 30) - 1 & 0xE7) + 0x32`.
The client's own qty*price is a 32-bit multiply it only checks when the high dword of gold is
0 (FUN_00467680); here every value is a Python int (u64 and beyond), so a forged quantity can
never wrap into a cheap purchase.
"""
import en_content as EC
import skills as SK

TYPE_SKILL = EC.TYPE_SKILL
# FUN_00467680 / FUN_00471450: the manner above which an NPC purchase gets 10 % off.
DISCOUNT_MANNER = 199
DISCOUNT = 0.9
# 2009 only (FUN_00471450): the equip grid slots whose option word 0 == 2 take 5 % more each.
DISCOUNT_2009_SLOTS = (1, 2)
DISCOUNT_2009_WORD = 2
DISCOUNT_2009 = 0.95
# Quantity the purchase / deposit dialogs allow per item Type (dialog 0x10 / 0x1A8:
# "999  is maximum number." / "99  is maximum number."; every other Type buys exactly 1).
QTY_MAX = {EC.TYPE_CONSUMABLE: 999, EC.TYPE_ETC: 99}
# FUN_00469090 case 0x1A8: the deposit fee by account manner.
BANK_FEE_FREE_MANNER = 100
BANK_FEE_LOW_MANNER = 30
BANK_FEE_MID, BANK_FEE_HIGH = 25, 50


def qty_limit(item_type):
    """The largest quantity one purchase / deposit / withdraw of this Type can name."""
    return QTY_MAX.get(item_type, 1)


def shown_id(base, learned, catalog=None):
    """The id FUN_00465a80 shows (and therefore sells) for stock entry `base`, or None when
    the client files no row for it (no item def: the row is skipped)."""
    catalog = EC.items() if catalog is None else catalog
    d = catalog.get(base)
    if d is None:
        return None
    if d.type != TYPE_SKILL:
        return base
    maxlv = SK.family_max_level(base)
    known = set(int(s) for s in (learned or ()))
    for j in range(maxlv):
        if base + j in known:
            return base + j if j == maxlv - 1 else base + j + 1
    return base


def offered(stock, item_id, learned=(), catalog=None):
    """The stock entry whose row sells `item_id` for a player who knows `learned`, or None.
    A skill master's row sells exactly one level: the next one (shown_id), so a request for
    any other level of the family - or an id the NPC does not list - is a forged packet."""
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return None
    for base in stock or ():
        if shown_id(base, learned, catalog) == item_id:
            return base
    return None


def family_row(stock, item_id, catalog=None):
    """The stocked skill row whose family `item_id` belongs to (base <= id < base + maxlv),
    or None. Lets a refused level be answered with the client's own reason ("You already
    have learned this skill." / level) instead of a bare "not for sale" - e.g. a repeated
    0x0B for a level the first one just taught."""
    catalog = EC.items() if catalog is None else catalog
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return None
    for base in stock or ():
        d = catalog.get(base)
        if d is not None and d.type == TYPE_SKILL and base <= item_id < base + SK.family_max_level(base):
            return base
    return None


def discount(client_build, item_type, manner, equipped=None, npc=True):
    """The price multiplier of FUN_00467680 (2008) / FUN_00471450 (2009). `equipped` is the
    character's grid {slot: {'id', 'w'}} (inventory model), read only by the 2009 build.
    npc=False is the stall purchase (C2S 0x62), which never gets a discount."""
    if not npc or item_type == TYPE_SKILL:
        return 1.0
    try:
        manner = int(manner)
    except (TypeError, ValueError):
        manner = 0
    d = DISCOUNT if manner > DISCOUNT_MANNER else 1.0
    if str(client_build) == '2009':
        for slot in DISCOUNT_2009_SLOTS:
            entry = (equipped or {}).get(slot) or (equipped or {}).get(str(slot))
            words = (entry or {}).get('w') or [0]
            if entry and int(entry.get('id') or 0) and int(words[0]) == DISCOUNT_2009_WORD:
                d *= DISCOUNT_2009
    return d


def cost(price, qty, d=1.0):
    """What the client charges for `qty` at unit `price` with multiplier `d` (an int, u64
    and up). A price of 0 costs nothing; a discounted cost that rounds to 0 costs 1."""
    price, qty = max(0, int(price)), max(0, int(qty))
    if price == 0:
        return 0
    value = int(round(float(qty * price) * d))
    if value == 0 and d < 1.0:
        return 1
    return value


def bank_fee(manner):
    """The gold a bank item deposit costs (the fee dialog 0x1AC shows it; S2C 0x66 carries
    the balance after it)."""
    try:
        manner = int(manner)
    except (TypeError, ValueError):
        manner = 0
    if manner >= BANK_FEE_FREE_MANNER:
        return 0
    return BANK_FEE_MID if manner >= BANK_FEE_LOW_MANNER else BANK_FEE_HIGH
