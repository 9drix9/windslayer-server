#!/usr/bin/env python3
"""
mall.py - the Item Mall (2008 "Premium Shop" / 2009 "Spark Shop"), the runtime half (P8 stage 2)
==============================================================================================
premium_cash-mall-enter, -mall-exit, -balance-refresh, -buy, -box-delete, -gift and
chat_mail_gm-gift-inbox (docs/systems/premium_cash.md F2-F7, chat_mail_gm.md F8; roadmap P8
order 2). cash.py holds the persisted model (wallet, box, owned list, serials); this module
runs the mall on top of it for both client builds.

    mall = Mall(server)                     # GameServer.mall
    mall.enter(session)                     # `!mall` -> 0x6F, [0x6D], 0x6A (or a refusal text)
    mall.close(sock, session, payload)      # C2S 0x42 -> moves, 0x6B, the map-load replay
    mall.refresh(sock, session)             # C2S 0x46 -> 0x70 (one per request)
    mall.buy(sock, session, payload)        # C2S 0x43 -> one 0x6C per item, or 0x6C {0 | 0x0E}
    mall.delete(sock, session, payload)     # C2S 0x45 -> 0x6E {1, mileage, serial} / {0}
    mall.gift(sock, session, rec)           # C2S 0x47 -> 0x71; the recipient's box + inbox,
                                            #   0x79 / 0x6D to him when online
    mall.send_gift_queue(sock, session)     # every map load: the 0x6D gift notifications
    mall.dev_gift(sender, found, item, msg) # `!gift`: the same gift, free
    mall.mileage_event(username, n, why)    # event Mileage -> 0x70 + 0x98 to an online owner
                                            #   (P8 stage 4; buy / gift while MILEAGE_EVENT_PCT)
    mall.grant_box(username, item, n, why)  # C7: an origin-3 record into the account's box ->
                                            #   S2C 0x6C to an online owner (mileage unchanged)
    mall.sale_offer(sock, session, rec)     # C8: 2009 C2S 0x80 -> 0x71 {is_trade 1, 0x17}
    mall.sale_reply(sock, session, rec)     # C8: 2009 C2S 0x81 (1 -> 0x71 {1, 0x16}; 0 / 2: none)
    mall.add_option(sock, session, rec)     # C8: 2009 C2S 0x4E -> 0xC4 {0}

How the client enters the mall (RE 2026-09-28, both builds; the task's first question)
------------------------------------------------------------------------------------
It cannot ask. Every path the client has ends in a no-op, so the server always starts the
mall by pushing S2C 0x6A, and `!mall` (any player, not only a GM) is the way in:
  - HUD "Spark Shop" button (2009) / "Premium Shop" (2008) = window 3 control 1 (2009 hui
    window 3 control 1: text 9303 "Spark Shop", tooltip 9304 "... Coming Soon!!!"). The
    dispatcher FUN_00448730 switches window 3 on `control - 2` through the table at
    0x44EA54 (controls 2..10 only; 5, 6, 7, 9 -> 0x44E5D9, the plain return), so control 1
    falls through `JA 0x44E5D9` at 0x44B4D1: no C2S, nothing opens. 2008: the same shape
    at 0x44BBC0 (premium_cash.md 1.4; LIVE: "Coming Soon" tooltip, no packet). Control 6
    ("Receive a gift", lit by S2C 0x6D) is the same no-op.
  - The "Shop Assistant" NPC Luxary (hni idx 82 = 0x52, UI 0, a client-spawned town NPC on
    201/401/501/601/701/801/901/1001/1101; NPCLngKo 155/156 "Click on me, and move to the
    Premium Shop!"): the NPC click handler 2009 FUN_00446000 (2008 FUN_004447d0) tests the
    clicked entity's hni idx `CMP EAX,0x52` at 0x44611B (2008 +0xE60) and RETURNS - not
    even its talk text opens, and nothing is sent. The retail server must have driven that
    NPC some other way; the Build 14 client has no hook for it.
  - A player marked "in the cash shop" (S2C 0x75: 2009 entity+0xEA = 0xC5, 2008 +0xE2 =
    0x81) opens dialog 0x133 "Enter the cash shop?" when clicked; its Yes re-dispatches
    FUN_00448730(window 3, control 1) at 0x449115..0x44913B - the same no-op.
So `!mall` is the only entry; the live check confirms the three no-ops by their silence.

Entering (F2)
-------------
Refused (one S2C 0x15 line, never a 0x6A) outside the world or mid map load - the 0x6A
handler dereferences the local player (scene+0x970 / 2009 +0x988) with no null check, so
the own 0x07 must be out - while dead, trading or selling at a stall, or already inside.
The mall replaces the client's world (the preview map, a preview avatar as local player,
scene mode 4), so the session LEAVES its map exactly as a map load does: off the shared
monsters (_clear_map_monsters: no chase / respawn / wander reaches it), in_world False (no
tick, regen, driver sample or map broadcast), world.depart + on_map_change (presence: the
old map's peers get S2C 0x06 and the client's held set is forgotten, since 0x6A destroyed
those entities too). Messenger, party and trade identity stay: the session is still online.
The way back is remembered first: the current map and the server's position estimate on the
floor (presence.floor_point): the exit's map load lands him ON that floor, as every map load
does (_arrival_point, livetest bug 5 - a point above it left the 2009 idle local player floating).
Then 0x6F (the owned list: 0x6A snapshots it into +0x58), the gift queue 0x6D (its popups
open at the END of 0x6A: FUN_0045E260 / 2009 FUN_004646c0), and 0x6A with the account's
Wind Cash, Mileage and box - 2008 {u32 cash, u32 mileage, i32 count, rows}, 2009 {u8 mode 0,
u8 count, cash, mileage, rows} (spec_2009 0x6A: mode 0 = one page). Every gift that was in
the client's queue has popped at that 0x6A: it is marked delivered.

Leaving (F7): C2S 0x42 {deleted flag, to_storage serials, to_character serials}
------------------------------------------------------------------------------
The client moved the records itself (box window 0x1FC -> the bag, the bag -> the box); 0x42
reports what differs from the 0x6A snapshot, and repeats the same serials if it closes again
without a fresh 0x6A, so every move is idempotent (a serial already on its side is a no-op,
an unknown one is logged). A box record comes onto the character only when it is a cash-bag
item (cash.GRANT_TYPES, Type 5) with a free cash-tab slot (CASH_TAB_SLOTS), or a 2009 pet / pet
gear record with a free EQUIPMENT-tab slot ("Pets" below), and the owned list has room
(cash.OWNED_MAX); else it stays in the box (the exit replay's 0x6F puts the client back in
step). Then S2C 0x6B (the wish list; it does NOT restore the world - live followup
premium_cash#18: the client stays on the black preview scene) and the full map-load replay
to the remembered point (GameServer._map_transfer: 0x08, 0x03, 0x07, 0x28, 0x44, 0x6F, the
monsters). The replay runs from a `finally`: a move that raises must not strand the client.

Balance refresh (F3): C2S 0x46 -> S2C 0x70
------------------------------------------
Sent only while the mall's charge-pending flag (mall+0x5AC; 2009 cleared at 0x46BA05) is set, by
the charge button (which minimises the game and opens the dead Yahoo top-up page): each
minimise->restore then sends one 0x46 (live premium_cash#29) until a 0x70 arrives, and any
0x70 clears the flag. So every 0x46 gets exactly ONE 0x70 - never zero (the resend would go
on at every restore) and never more (the client asks nothing back). first_purchase_bonus is
the account's owed first-purchase popup, cleared once shown.

Buying (F4): C2S 0x43 {u8 pay_with_mileage, u8 count, count x {u16 item [, u8 option 2009]}}
-------------------------------------------------------------------------------------------
Three send sites share the grammar: single buy (0x1F8), cart (0x1F7, <= CART_MAX) and the
slot-extension picker (0x1FF). The price is the SERVER's (cash.CashDef.price = the client's
own hii Cash_P, the value its dialogs compare the balance against). Refused as a whole with
0x6C {0} ("You failed to buy the item.") when an item is not sold (hii Cash 1 with a price,
or a slot extension), is not a cash-bag item (cash costumes, Type 1, are not modelled yet:
premium_cash Q14 - a box record of one could never come onto the character; the 2009 pets and
pet gear are, since P15 pet-s7: "Pets" below), carries a 2009 paid option (spec_2009 0x43 `option` 1..4, +1000
each; what it grants is not traced), would take a tab past its client cap, or the box past
BOX_MAX; with 0x6C {0x0E} ("You are short of cash.") when the balance does not cover the
total. Mileage pays when the box is ticked and mileage covers the total; a 2009 client that
ticked it without enough mileage sends anyway after its own CASH check passed (spec_2009
0x43 gates: "the CASH check runs anyway"), so there the cash pays. Otherwise one 0x6C {1,
running cash, running mileage, record} per item, in order (the handler reads one record):
  - a slot extension 1884..1889 (0x75C..0x761) is applied, not stored: tab capacity + 5
    (inventory tab_slots, bank_tabs bank_slots), persisted, so the next 0x03 / 0x65 carries
    it. The client adds +5 itself on the 0x6C only while the tab is below 45 (bank 60)
    (2009 FUN_00464F40, 2008 FUN_0045E9F0 at 0x45EA16), so the server refuses a purchase
    whose +5 would pass that cap instead of clamping - a tab stays a multiple of 5 and both
    sides agree (exit criterion 6: capped at 45);
  - anything else becomes a new box record (serial store-wide, kind / quantity from the
    hii, origin 0 cash / 1 mileage - only a cash purchase is refundable).
Cash payments credit MILEAGE_BONUS_PCT % of the price as mileage (config, default 0; the
client pops "50% bonus mileage has been deposited." whenever mileage rises).

Deleting (F5): C2S 0x45 {u16 item, u32 serial} -> S2C 0x6E
---------------------------------------------------------
The box record with that serial and item, else 0x6E {0}. The refund is the client dialog's
own number (live followup premium_cash#26: 150 Mileage for a 500 item): 30 % of the price
as MILEAGE, only for a cash-bought record that is not partly used or activated.

Gifts (F6) and the gift inbox (chat_mail_gm F8)
-----------------------------------------------
2008: the row's Gift button opens the password dialog 0x201 (while scene+0x218 is set):
C2S 0x51 {password, 0x1F9} -> GameServer._handle_password_verify -> S2C 0x80 {1, 0x1F9}
(session pw_ok) -> gift dialog 0x1F9 -> C2S 0x47. 2009: the 0x5A handler forces the flag
(scene+0x21E) to 0 at 0x451C8E, so FUN_00463ee0 skips the password dialog and opens 0x1F9
at once (FUN_004640e0) - a 2009 gift has no password step, and the server does not require
one. C2S 0x47 {0, u16 item [, u8 option 2009], str[17] recipient, u8 len, message} -> S2C
0x71 {result [, cash, mileage]} (2009: a leading bool is_trade = 0). Checks in the design's
order, the first failure answers: in the mall, a sold cash-bag item without option, (2008)
a password unlock younger than GIFT_PASSWORD_SECS -> 0x00; the recipient character exists
-> 0x02; it is not on the sender's own account -> 0x0C; the item's hii Gender matches the
recipient's gender flag -> 0x14; the recipient's box has room -> 0x00; the cash covers the
price -> 0x0E. Success: the sender pays (+ the mileage bonus), the record goes into the
RECIPIENT account's box (origin 2: no refund) with an inbox entry {sender, message, item,
serial, delivered False}, 0x71 {1, cash, mileage}. An online recipient (in the world or in
the mall) gets S2C 0x79 {record} when he is in the mall (it appears in his box) and the 0x6D.

The inbox is delivered with S2C 0x6D, whose records the client only APPENDS to its queue
(cash ctx +0x5F4 / 2009 +0x600; 0x03 does not clear it), so a gift goes out once per
connection (session gifts_on_client) - on the map load that follows it (enter world, a
portal) or live - and is marked delivered at the next 0x6A, which pops it. A later map
load resets the HUD gift button; while the queue still holds gifts a 0x6D with count 0
lights it again (spec correction C20). The queue gate (chat_mail_gm F8.2): the item exists
in the client and is a cash item (def+0x1F0) or a gift card 3327..3332, and the sender name
is 1..16 bytes without a space - anything else would stop the client's queue for good
(FUN_0045E260), so such an entry becomes a memo instead. A P5 `!gift` entry without a box
record (serial 0) gets its record when it is queued.

Mileage events (F16; premium_cash-mileage-event, P8 stage 4)
-----------------------------------------------------------
Event mileage is credited to the account and told with S2C 0x70 {cash, mileage, 0} (the
labels; no popup) and then S2C 0x98 (empty: the client's own "※Mileage Event※ You got bonus
mileage." chat line) to an owner in the world or in the mall; one at character select, mid
map load or offline is credited silently. Two sources: GM `!mileage <n> [name|all]`, and a
purchase event - while MILEAGE_EVENT_PCT (or `!mileage event <pct>`) is set, every Wind Cash
buy / gift earns that percent of its price after its own 0x6C / 0x71 (mileage payments earn
nothing). It is separate from MILEAGE_BONUS_PCT, which rides inside the 0x6C / 0x71 running
balance and so pops the client's "50% bonus mileage has been deposited." instead.

Presence (F17; premium_cash-presence, P8 stage 4)
-------------------------------------------------
A player in the mall is HIDDEN from his map, and he comes back only through his own exit:
entering departs the map (the peers holding him get S2C 0x06, a late arrival gets no record
of him - he is in no MapInstance and in_world is False, which presence._can_show also
checks with in_cash_shop), and the 0x42 replay arrives again (S2C 0x05 to every peer there
at that time, 0x04 of them to him). The design's retail marker - keep him drawn with the
"cash shop" signboard, S2C 0x75 {uid} on entry / 0x5C {uid} on exit, room_no 0x81 (2009
entity+0xEA = 0xC5) in 0x04/0x05/0x07 records - is NOT sent: it needs the entity on the
peers' clients, which the mall's world-leave removes, and the marker's only extra is the
"Enter the cash shop?" prompt, whose Yes is the same client no-op as the HUD button. Every
other server-driven map load (GM `!warp` / `/go`, warp stones, revive) is refused while he is
inside (GameServer._map_transfer): a 0x08 into the preview scene would put the client back
in the world with the mall still open server-side, so he would reappear without an exit.

Pets (2009; P15 pet-s7, pet.md 2.1 / 2.3 / F13; ROADMAP_2009_ADDENDUM C1)
-------------------------------------------------------------------------
The Spark Shop's pet tabs are the hii rows with Cash_Cls 17 (the four pets 4294 / 4299 / 4304 /
4309, 4900 each; the Pet Bell 4285 is Cash 0: an NPC item, cp-3), 18 (the sixteen pet gear rows
4290..4308, Type 1 Kind 15 / 16) and 19 (Pet Food 4286..4289, the name ticket 4322, Type 5) -
EN ids in both exe variants: the mall window lists the hii rows itself and C2S 0x43 carries the
row id, so no exe constant (cp-2, CLIENT_ITEM_IDS) is involved. The prices are the hii Cash_P.
Food and tickets are ordinary counted cash-bag records (Type 5) and always sold; pets and pet
gear are sold and gifted while config MALL_PETS is on (the default since pet-s7: a bought pet
can be worn, C2S 0x82, and its gear put on, C2S 0x0F).
  - A pet is a pet record (cash.py, limit_type 3 with the pet fields), in the box as in the
    owned list: 0x6A / 0x6F carry it in the grammar's pet branch, 0x6C / 0x79 as the raw 28
    bytes (cash.record_bytes). A BOX pet is level 0 (unbound, asleep, gauge 100): the client
    moves a Type-6 box record to the bag only at level 0 ("You can't move your current pet to
    the bag.", FUN_00465890 @0x466537), after its own "...it can't be moved to other
    characters." confirm.
  - The 0x42 box -> character move BINDS it (F13: cash.bind_pet - level 1, EXP 0, gauge 100,
    awake, named after its species) when the EQUIPMENT tab, where the client files a bagged
    pet (0x6F FUN_00464e00 case 6), has a free slot; the exit replay's 0x6F hands the client the
    bound record.
  - A bound pet never goes back to the box (F13 [I]: the box is account-wide, so a bound pet
    there could reach another character, and the client would refuse to bag it again at level
    >= 1): a pet serial in the 0x42 to-box list is left on the character and logged; the exit
    replay's 0x6F re-files it in the bag. A worn record (a pet or its gear) never moves.
  - Pet gear is a permanent (Cash_T 0, kind 0) record that the 0x6F also files in the
    EQUIPMENT tab (case 1; inventory.pet_slots counts it): it moves box -> character with a
    free slot there and, not worn, back like any record.
With MALL_PETS off a pet / pet gear buy or gift is refused (0x6C {0} / 0x71 {0}); records
already in a box still move.

Event records (C7; events_bosses A5 G2 / E6)
-------------------------------------------
grant_box() is the internal API ev-e6 (P13) and GM tools use to deliver a cash item as an EVENT
record: a new record with origin 3 in the ACCOUNT's box (a pet as an unbound box pet), then S2C
0x6C {1, cash, mileage, record} to an owner in the world or in the mall with the balances
UNCHANGED - a higher mileage would also pop "50% bonus mileage has been deposited." - and the
client shows "Congratulation. You received an event item.." and appends the record to its box
list (2009 0x46B60C..; 2008 0x462273). An owner offline, at character select or mid map load
is not told: the next 0x6A lists the record. In the mall the 0x6A snapshot rule of the gift
push holds (one or the other, never both: 0x6C appends with no duplicate check). Not for a
slot extension (applied, never stored), an item that is no cash item, one the 0x42 move never
takes out of the box (a cash costume, a Cash 0 item: an event record nobody could use), or a
full box. A pet is granted as a level-0 box pet and pet gear as a permanent record (both move
into the equipment tab, "Pets"). A counted quantity above STACK_MAX becomes several records, as grant().

The 2009-only cash opcodes (C8; ROADMAP_2009_ADDENDUM 1 item 4; the premium_cash group owns
them). The cash-item-for-gold sale is not offered; each request gets a safe answer:
  - C2S 0x80 CashItemSaleOffer (window 0x4B8 control 3): Send, then the waiting box "Waiting
    for the server to respond." (FUN_0049ebc0(.., 3, 3, 3, 0) at 0x4689B6) - MUST-REPLY. S2C
    0x71 {is_trade 1, 0x17} rewrites that box ("Your target user does not exist in the
    server.") and hides windows 0x4B9 and 0x4B8 (2009 FUN_0046a2e0 case 0x71 0x46B32E ->
    0x46B2EB); registry BUILD_MUST_REPLY 0x80 is the same bytes as the backstop.
  - C2S 0x81 CashItemSaleReply: Add + Send (0x468D84) and return - no waiting box. Reply 1 is
    the seller's Cancel (window 0x4B8 control 4), which does not close the window, and window
    1208 = 0x4B8 "Pop-Up (Present Premium to Gold)" has no close box (hui: OK 3, Cancel 4,
    list 6, the option menu 12..17 - no "End" control, UILngKo 491) - so it gets S2C 0x71
    {1, 0x16} ("Target user have cancelled trade transaction.", hides 0x4B9 / 0x4B8).
    Replies 0 / 2 come from the buyer's windows 0x4B9 / 0x4BA, which only S2C 0xA9 opens: no
    offer is ever pending, so they get no reply (logged).
  - S2C 0xA9 CashItemSaleOffer / 0xAA CashItemSaleCompleted: never sent (no sale; 0xA9 would
    open the buyer's offer popup, 0xAA overwrites the absolute gold).
  - S2C 0xC4 CashItemQuantityPurchaseResult: only as the refusal {0} ("You failed to buy the
    item.") to C2S 0x4E CashItemAddOption (window 0x4DA control 5), which opens no waiting box
    (0x468706: Add + Send, return) - the answer is feedback, not a lock release.

Locks: Mall.lock -> store.lock (db_lock) -> send_lock. Every balance / box / inbox change is
made under store.lock (one RLock over the whole store, so a gift's two accounts change
together) and persisted with mark_dirty; replies are sent after it is released.
"""
import collections
import logging
import threading
import time

import bank_tabs as BT
import cash as CASH
import en_content as EC
import inventory as invmod
import packets as P
import presence
import records as R
import registry
import social
import world as worldmod

log = logging.getLogger('WS')

BUILD_2009 = '2009'
MALL_NAME = {'2008': 'Item Mall', '2009': 'Spark Shop'}

# ---- wire limits (one Fireway frame carries a 0x6A / 0x6D: cash.FRAME_PAYLOAD_MAX) ----
RECORD_BYTES = 28
# 2008 0x6A = {u32 cash, u32 mileage, i32 count} + 28 B rows in ONE frame -> 72 rows; the
# 2009 mode-0 page {u8 mode, u8 count, cash, mileage} holds the same 72. One cap for both
# builds: a buy or gift that would pass it is refused, a move to the box is left undone.
BOX_MAX = (CASH.FRAME_PAYLOAD_MAX - 12) // RECORD_BYTES
# 0x6D rows: 2008 sender[17] message[91] u16 item bytes[16] = 126 B; 2009 adds u8 +0x00,
# u8 +0x6D, u8 option, u8 +0x71 = 130 B (spec_2009 0x6D). u8 count first.
GIFT_ROW_BYTES = {'2008': 126, '2009': 130}
CART_MAX = 28                        # premium_cash F4: the cart dialog 0x1F7 lists <= 28 items
CASH_TAB_SLOTS = 45                  # the cash bag tab: scene+0x830 u16 x45 (2009 ci+0x800)
TAB_EXT_STEP = 5                     # FUN_00464F40 / FUN_0045E9F0: +5 per extension
TAB_CAP = {'tab': invmod.MAX_CAPACITY, 'bank': BT.MAX_CAPACITY}       # 45 / 60: the client's own test
REFUND_PCT = 30                      # the delete dialog's refund: double 0.3 at 0x6F0C20 (2008)
GIFT_PASSWORD_SECS = 120.0           # 2008: C2S 0x51 {.., 0x1F9} unlocks one gift this long
GIFT_HISTORY = 20                    # delivered inbox entries kept per account (the rest dropped)

# S2C 0x6C results (spec 0x6C; live followup premium_cash#14)
BUY_OK, BUY_FAILED, BUY_SHORT = 1, 0, 0x0E
# S2C 0x71 results (spec 0x71 / spec_2009 0x71; live premium_cash#17)
GIFT_OK, GIFT_FAILED, GIFT_NO_CHAR, GIFT_OWN_CHAR, GIFT_SHORT, GIFT_GENDER = 1, 0, 0x02, 0x0C, 0x0E, 0x14
# 2009 is_trade forms (FUN_0046a2e0 case 0x71): 0x16 "Target user have cancelled trade
# transaction." / 0x17 "Your target user does not exist in the server.", each hiding windows
# 0x4B9 and 0x4B8 (C8).
TRADE_CANCELLED, TRADE_NO_TARGET = 0x16, 0x17
SALE_CANCEL, SALE_ACCEPT, SALE_DECLINE = 1, 0, 2            # C2S 0x81 reply values
PASSWORD_WINDOW_GIFT = 0x1F9


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def mall_name(client_build=None):
    return MALL_NAME.get(str(client_build or '2008'), MALL_NAME['2008'])


# ------------------------------------------------------------ pure rules ---
def cart_items(rec):
    """[(item_code, option)] of a decoded C2S 0x43 (the cart and single-buy grammars; 2008
    has no option byte)."""
    if rec is None:
        return []
    rows = rec.get('repeat[item_count]')
    if rows is None:
        rows = [rec] if 'item_code' in rec else []
    return [(_int(r.get('item_code')), _int(r.get('option'))) for r in rows]


def is_pet_item(d):
    """A 2009 pet (hii Type 6) or pet gear row (Type 1 cash, Kind 15 / 16: inventory.is_pet_gear)
    - the Cash_Cls 17 / 18 tabs behind config MALL_PETS (module docstring "Pets"). False for
    every 2008 row."""
    return d is not None and (d.is_pet or invmod.is_pet_gear(d.id))


def sale_refusal(d, item_id, option=0, pets=False):
    """Why `item_id` cannot be bought or gifted in the mall, or None. A slot extension is
    sold (its own picker), never gifted: the caller checks that. A pet or pet gear only with
    `pets` (config MALL_PETS; module docstring "Pets")."""
    if d is None:
        return f'item {item_id} is not in the client item table'
    if not d.sold:
        return f'{d.name or d.id} ({d.id}) is not sold (hii Cash {d.cash}, price {d.price})'
    pet_item = is_pet_item(d)
    if pet_item and not pets:
        return (f'{d.name or d.id} ({d.id}) is a {"pet" if d.is_pet else "pet gear item"}: pet sales are off '
                f'(config MALL_PETS)')
    if d.id not in CASH.SLOT_EXT and d.type not in CASH.GRANT_TYPES and not pet_item:
        return f'{d.name or d.id} ({d.id}) is a cash costume (Type {d.type}: premium_cash Q14), not modelled yet'
    if option:
        return f'paid option {option} on {d.id} (spec_2009 0x43 `option`: not modelled)'
    return None


def gender_refusal(d, gender_flag):
    """The client's gender rule (inventory.equip_refusal: hii Gender 1 needs the flag set, 2
    needs it clear), or None."""
    g = getattr(d, 'gender', 0)
    if (g == 1 and not gender_flag) or (g == 2 and gender_flag):
        return f'hii Gender {g} vs the recipient flag {int(bool(gender_flag))}'
    return None


def delete_refund(rec, d):
    """Mileage a deleted box record returns: 30 % of the price (rounded half up) for a record
    bought with cash that is neither partly used nor activated, else 0 (the delete dialog)."""
    if d is None or not d.price or int(rec.get('origin', 0)) != CASH.ORIGIN_CASH or CASH.is_activated(rec):
        return 0
    if int(rec.get('kind', 0)) == CASH.KIND_COUNT and _int(rec.get('qty')) < d.quantity():
        return 0
    return (int(d.price) * REFUND_PCT * 2 + 100) // 200


def gift_display_refusal(gift):
    """chat_mail_gm F8.2: why this inbox entry would block the client's gift queue, or None.
    FUN_0045E260 pops the head only when its item def exists with def+0x1F0 != 0 (or it is
    a gift card 3327..3332) and the sender name passes FUN_0043df40 (non-empty, no space)."""
    d = CASH.cash_def(_int(gift.get('item_id')))
    if d is None:
        return f'item {gift.get("item_id")} is not in the client item table'
    if not (d.cash or d.id in CASH.GIFT_CARDS):
        return f'{d.name or d.id} ({d.id}) has def+0x1F0 0 (not a cash item or gift card)'
    sender = P.to_bytes(gift.get('sender') or '')
    if not sender or b' ' in sender or len(sender) > 16:
        return f'sender name {gift.get("sender")!r}'
    return None


def gift_row(gift):
    """One S2C 0x6D record. The names are the same in both builds; the 2009 record's extra
    bytes (+0x00, +0x6D, item_option, +0x71, the 16 at +0x72) have no known meaning and go
    out as 0 (spec_2009 0x6D). The grammar's str[17] / str[91] clamps keep both strings
    NUL-terminated inside their fields, which the popup's unbounded copies need."""
    return {'sender_name': gift.get('sender') or '', 'message': gift.get('message') or '',
            'item_id': _int(gift.get('item_id')) & 0xFFFF}


def gift_pages(gifts, client_build=None):
    """0x6D field dicts for `gifts`: as many rows per packet as one frame holds (2008 16,
    2009 15), count 0 for none (the relight form, C20)."""
    build = str(client_build or '2008')
    per = (CASH.FRAME_PAYLOAD_MAX - 1) // GIFT_ROW_BYTES.get(build, 126)
    rows = [gift_row(g) for g in gifts]
    pages = [rows[i:i + per] for i in range(0, len(rows), per)] or [[]]
    return [{'gift_count': len(p), 'repeat[gift_count]': p} for p in pages]


def enter_fields(acc, client_build=None):
    """S2C 0x6A fields for the account's balances and box (at most BOX_MAX rows). 2009 carries
    a pet record in the grammar's pet branch (cash.wire_record); 2008 has no pets and no other
    2009-only item either (cash.foreign_to_client: the pet gear)."""
    cash, mileage = CASH.balance(acc)
    box = CASH.ensure_account(acc)['cash_box'][:BOX_MAX]
    if str(client_build or '2008') != BUILD_2009:
        box = [r for r in box if not CASH.is_pet_record(r)]
        # a store shared with a 2009 server: its Spark Shop puts pet gear (and any other
        # 2009-only item) into the box, which a 2008 client has no hii row for
        foreign = [CASH.foreign_to_client(r.get('item_id'), client_build) for r in box]
        if any(foreign):
            log.warning(f'[MALL] {sum(foreign)} box record(s) of item(s) the 2008 client lacks '
                        f'{[r.get("item_id") for r, f in zip(box, foreign) if f]} left out of the 2008 0x6A')
            box = [r for r, f in zip(box, foreign) if not f]
    rows = [CASH.wire_record(r) for r in box]
    if str(client_build or '2008') == BUILD_2009:
        return {'mode': CASH.MODE_SINGLE, 'box_count': len(rows), 'cash_balance': cash,
                'mileage_balance': mileage, 'repeat[box_count]': rows}
    return {'cash_balance': cash, 'mileage_balance': mileage, 'box_count': len(rows),
            'repeat[box_count]': rows}


def buy_fields(cash, mileage, rec):
    """S2C 0x6C {1, cash, mileage, record}: the 28-byte record the handler reads raw
    (cash.record_bytes - a pet record in its pet layout, C1), expiry as raw SYSTEMTIME."""
    raw = CASH.record_bytes(rec)
    return {'result': BUY_OK, 'cash_balance': CASH.clamp_cash(cash), 'mileage_balance': CASH.clamp_cash(mileage),
            'item_serial': int.from_bytes(raw[0:4], 'little'), 'item_id': int.from_bytes(raw[4:6], 'little'),
            'item_kind': raw[6], 'unk_07': raw[7], 'quantity': int.from_bytes(raw[8:10], 'little'),
            'expire_time': raw[10:26], 'origin': raw[26], 'unk_1b': raw[27]}


def gift_result_fields(result, client_build=None, cash=None, mileage=None):
    """S2C 0x71 (2009: the leading is_trade 0 of a cash-shop gift)."""
    fields = {'result': int(result)}
    if result == GIFT_OK:
        fields.update(cash_balance=CASH.clamp_cash(cash), mileage_balance=CASH.clamp_cash(mileage))
    if str(client_build or '2008') == BUILD_2009:
        fields = {'is_trade': 0, **fields}
    return fields


def cash_tab_used(char):
    """Records that take a cash-bag slot: Type 5, not worn, usable (FUN_0045E8E0 bags a
    record only with a quantity and not an activated period item)."""
    used = 0
    for rec in CASH.ensure(char):
        d = CASH.cash_def(rec['item_id'])
        if d is not None and d.type in CASH.GRANT_TYPES and CASH.usable(rec) and not rec.get('equipped'):
            used += 1
    return used


# ------------------------------------------------------------------ Mall ---
class Mall:
    """The mall of one GameServer (GameServer.mall). Session keys it owns:
      in_cash_shop     True from the 0x6A to the 0x42 replay
      mall             {'back': {map, x, y}, 'box': {serials}, 'char': {serials}, 't', 'open'}:
                       the way back, the 0x6A snapshot (diagnostics / 0x42 validation) and
                       whether the 0x6A is out (a live gift's 0x79 only after it)
      gifts_on_client  [serials] of the gifts this connection's client has queued (0x6D) and
                       not popped at a 0x6A yet
      mall_refreshes   C2S 0x46 answered on this connection (log / `!mall status` aid)
      pw_ok            {window: monotonic()} (GameServer._handle_password_verify): 2008 gifts"""

    def __init__(self, server):
        self.server = server
        self.lock = threading.RLock()
        # GM `!mileage event <pct|off>`: the purchase mileage event rate until a restart
        # (None = config MILEAGE_EVENT_PCT). Server memory only, like any GM event switch.
        self.event_pct_override = None

    # ---------------------------------------------------------------- helpers ---
    @property
    def build(self):
        return str(getattr(self.server, 'client_build', None) or '2008')

    @property
    def store(self):
        return self.server.store

    def _account(self, session):
        return self.store.account(session.get('username'))

    def _send(self, sock, session, key, fields, flush=None):
        """P.send; flush=False only queues the packet on the receiver's outbox. Every send made
        while holding self.lock passes it: the lock is server-wide (every map load of every
        player may take it), so a client that stopped reading must never hold it through a
        blocking socket write (packets.send; P8 review). _flush() writes after the lock."""
        return P.send(self.server, sock, session, key, fields, **({} if flush is None else {'flush': flush}))

    def _flush(self, session):
        """Write what this thread queued with flush=False, once self.lock is let go: on the
        session's own connection thread its outbox is written now, as a reply always is (the
        queue keeps the order, so the gift / 0x6A ordering holds); on any other thread the
        packets were only queued and the outbox's writer thread kicked. A failed write closes
        the connection (world.Outbox), whose normal disconnect path runs; logged here."""
        outbox = session.get('outbox')
        if outbox is None or not self.server._is_connection_thread(session):
            return
        try:
            outbox.flush()
        except OSError as e:
            log.info(f'[MALL] write to {session.get("char_name")!r} failed: {e}')

    def _bonus(self, price):
        pct = max(0, _int(self.server.config.get('MILEAGE_BONUS_PCT', 0)))
        return int(price) * pct // 100

    @property
    def pets(self):
        """config MALL_PETS: pets and pet gear are sold / gifted (module docstring "Pets")."""
        return bool(self.server.config.get('MALL_PETS', True))

    def _new_record(self, d, origin):
        """A new box record of `d` (a pet: an unbound box pet, cash.new_record)."""
        return CASH.new_record(self.server.cash.next_serial(), d, origin)

    # ---------------------------------------------------------- mileage event ---
    @property
    def event_pct(self):
        """The running purchase mileage event: percent of a Wind Cash price (0 = none)."""
        if self.event_pct_override is not None:
            return self.event_pct_override
        return max(0, _int(self.server.config.get('MILEAGE_EVENT_PCT', 0)))

    def _online_session(self, username):
        """The account's session a mileage notice can reach: in the world or in the mall. At
        character select or mid map load it is None (the balance is persisted anyway and
        the next 0x6A / 0x70 shows it)."""
        target = self.server.world.session(self.store.uid_of(username))
        if target is None or target.get('sock') is None or target.get('closed') or target.get('kicked'):
            return None
        return target if (worldmod.reachable(target) or target.get('in_cash_shop')) else None

    def mileage_event(self, username, amount, reason='event'):
        """premium_cash-mileage-event (F16): credit `amount` event Mileage to the account
        (persisted, clamped to CASH_MAX), then tell an online owner S2C 0x70 {cash, mileage,
        0} - the absolute balances: the mall's labels follow and no popup opens (bonus byte
        0, live T-70) - and S2C 0x98, whose empty body makes the client print its own chat
        line "※Mileage Event※ You got bonus mileage." (spec 0x98, SubHandler4 2008 0x46301A
        / 2009 0x46C550: its context is set by the 0x5A key exchange, and the line is a no-op
        while no chat window exists). Returns (credited, new mileage, told)."""
        acc = self.store.account(username)
        if acc is None or int(amount) <= 0:
            return 0, CASH.balance(acc)[1] if acc else 0, False
        with self.store.lock:
            before = CASH.balance(CASH.ensure_account(acc))[1]
            acc['mileage'] = CASH.clamp_cash(before + int(amount))
            credited, mileage = acc['mileage'] - before, acc['mileage']
            fields = CASH.balance_fields(acc)
        if credited:
            self.store.mark_dirty(f'mileage event {username}')
        target = self._online_session(username)
        told = False
        if target is not None and credited:
            told = self.server._push(target, '0x70', fields, 'MALL') and \
                self.server._push(target, '0x98', {}, 'MALL')
        log.info(f'[MALL] mileage event ({reason}): {username!r} +{credited} Mileage -> {mileage}'
                 + ('; 0x70 + 0x98 "Mileage Event" line' if told else
                    '; not told (offline / not in the world or the mall)' if credited else
                    ' (already at the cap)'))
        return credited, mileage, told

    def _purchase_event(self, session, paid_cash, what):
        """A Wind Cash payment while a mileage event runs (event_pct): its share as event
        mileage, AFTER the purchase's own replies (0x6C / 0x71 carry the running balances
        without it; the 0x70 that follows carries the total)."""
        pct = self.event_pct
        bonus = int(paid_cash) * pct // 100 if pct and paid_cash > 0 else 0
        if bonus:
            self.mileage_event(session.get('username'), bonus, f'{what}: {pct}% of {paid_cash} cash')
        return bonus

    # ------------------------------------------------------------------ enter ---
    def enter_refusal(self, session):
        """Why this session cannot enter the mall now (a 0x15 line), or None."""
        srv = self.server
        if session.get('in_cash_shop'):
            return f'You are in the {mall_name(self.build)} already.'
        if srv._session_char(session) is None or not session.get('char_name'):
            return 'No character in this session.'
        if not session.get('in_world') or session.get('current_map') is None:
            # the own 0x07 is not out (enter world / a map load in flight): the 0x6A handler
            # would dereference a missing local player (premium_cash F2 hazard)
            return 'Not while changing maps.'
        if session.get('dead'):
            return 'Not while you are dead.'
        # Every mall exit is a full map load: the portal debounce (PORTAL_COOLDOWN_SECS after
        # the last map change, the exit's included) keeps a patched client from looping `!mall`
        # + 0x42 into back-to-back map loads (P8 review).
        cooldown = float(getattr(srv, 'PORTAL_COOLDOWN_SECS', 0.0) or 0.0)
        since = time.monotonic() - float(session.get('last_transfer_t') or 0.0)
        if since < cooldown:
            return 'Please wait a moment after a map change.'
        if srv.trade.busy(session):
            return 'Not during a trade.'
        if session.get('stall_client_selling') or srv.market.selling(session):
            return 'Close your shop first.'
        return None

    def enter(self, session, why='!mall'):
        """Open the mall for an in-world session (F2). Returns None, or the refusal text the
        caller sends as a 0x15 line (no mall packet then)."""
        srv = self.server
        sock = session.get('sock')
        refusal = self.enter_refusal(session)
        if refusal is not None or sock is None:
            log.info(f'[MALL] {session.get("char_name")!r} {why}: refused - {refusal or "no socket"}')
            return refusal or 'No connection.'
        acc = self._account(session)
        char = srv._session_char(session)
        cur = int(session.get('current_map'))
        x, y = presence.floor_point(session)
        back = {'map': cur, 'x': float(x), 'y': float(y)}             # on the floor (livetest bug 5)
        # P13 (events.Events.suspend): an event effect the tick is delivering (0x15, a 0x99 sub 9
        # gift, a quest push) finishes before the 0x6F / 0x6A below, and none starts again until
        # the exit's map load's C2S 0x63 - as the before_server_map_load hook does for a map load.
        events = getattr(srv, 'events', None)
        if events is not None:
            events.suspend(session)
        # Leave the map as a map load does (module docstring "Entering").
        srv._clear_map_monsters(session)
        session['in_world'] = False
        srv._driver_epoch += 1
        old = srv.world.depart(session)
        if old is not None:
            srv.world.hooks.fire(worldmod.ON_MAP_CHANGE, srv, session, old_map=old, map_code=None,
                                 reason='mall')
        session['in_cash_shop'] = True
        session['mall'] = {'back': back, 't': time.monotonic(), 'box': set(), 'char': set(), 'open': False}
        srv._send_owned_cash(sock, session, reason='mall enter')
        with self.lock:
            # One hold of the mall lock from the gift queue to `open`. A gift committed for this
            # account before the snapshot below is in this 0x6A's box, and its _push_gift -
            # which may still be on its way to this lock - finds the serial in mall['box'] and
            # sends no 0x79; one committed after it is pushed as a 0x79 once `open` is set.
            # Never both (a 0x79 behind the 0x6A would list the record twice: 0x79 appends
            # without a duplicate check). The sends only queue (flush=False): written below.
            queued = self.send_gift_queue(sock, session, reason='mall enter', flush=False)
            with self.store.lock:
                fields = enter_fields(acc, self.build)
                box_serials = {r['serial'] for r in fields['repeat[box_count]']}
                owned = {r['serial'] for r in CASH.owned_records(char)}
                stored = len(CASH.ensure_account(acc)['cash_box'])
            self._send(sock, session, '0x6A', fields, flush=False)
            session['mall'].update(box=box_serials, char=owned, open=True)
            shown = self._mark_gifts_shown(session, queued)
        self._flush(session)
        self._clear_first_purchase_notice(session, acc)
        if stored > BOX_MAX:
            log.error(f'[MALL] {session.get("username")!r} has {stored} box records; the 0x6A carries '
                      f'{BOX_MAX} (one frame)')
        log.info(f'[MALL] {session.get("char_name")!r} enters the {mall_name(self.build)} ({why}) from map '
                 f'{cur} at ({back["x"]:.0f}, {back["y"]:.0f}): cash {fields["cash_balance"]}, mileage '
                 f'{fields["mileage_balance"]}, box {len(box_serials)}, owned {len(owned)}, gift popups {shown}')
        return None

    def _clear_first_purchase_notice(self, session, acc):
        """premium_cash.md 1.1: the S2C 0x02 cash_first_purchase_flag is shown once, by the
        next 0x6A, which clears it client-side. When this connection's 0x02 carried it
        (session['first_purchase_popup_owed'], set by the character-list builder) the 0x6A just
        showed it, so the account's flag goes too - else the popup came back at the first
        `!mall` of every login until a charge-button 0x46 cleared it (P8 review). A notice
        set after this connection's 0x02 is left to the 0x46 path (mall.refresh)."""
        if not session.pop('first_purchase_popup_owed', False) or acc is None:
            return False
        with self.store.lock:
            owed = bool(acc.get('first_purchase_notice'))
            acc['first_purchase_notice'] = False
        if owed:
            self.store.mark_dirty(f'first purchase notice shown {session.get("username")}')
            log.info(f'[MALL] {session.get("username")!r}: the 0x6A showed the first-purchase popup; cleared')
        return owed

    # ------------------------------------------------------------------ close ---
    def close(self, sock, session, payload):
        """C2S 0x42 (F7): apply the client's moves, 0x6B, replay the world. Returns True
        when the session left the mall."""
        if not session.get('in_cash_shop'):
            # Only a 0x6A opens the mall windows, and the replay below closed them: a second
            # close has nothing left to do (its serials were applied the first time).
            log.warning(f'[MALL] 0x42 from {session.get("char_name")!r} outside the mall; ignored')
            return False
        try:
            rec, err = registry.decode(0x42, payload, self.build)
            if rec is None:
                log.warning(f'[MALL] 0x42 {bytes(payload).hex(" ")} does not decode ({err}); no move applied')
            else:
                to_box = [_int(r.get('item_serial')) for r in rec.get('repeat[to_storage_count]') or []]
                to_char = [_int(r.get('item_serial')) for r in rec.get('repeat[to_character_count]') or []]
                self.apply_moves(session, to_box, to_char, deleted=_int(rec.get('storage_deleted_flag')))
        finally:
            self._leave(sock, session)
        return True

    def apply_moves(self, session, to_box, to_char, deleted=0):
        """Apply the 0x42 lists idempotently (module docstring "Leaving"). Returns
        {'box': moved to the box, 'char': moved to the character, 'kept': left, 'unknown'}."""
        srv = self.server
        acc, char = self._account(session), srv._session_char(session)
        out = collections.defaultdict(list)
        if acc is None or char is None:
            return out
        with self.store.lock:
            box = CASH.ensure_account(acc)['cash_box']
            items = CASH.ensure(char)
            for serial in dict.fromkeys(to_box):
                rec = next((r for r in items if r['serial'] == serial), None)
                if rec is None:
                    out['same' if any(r['serial'] == serial for r in box) else 'unknown'].append(serial)
                    continue
                if rec.get('equipped') or len(box) >= BOX_MAX or CASH.is_pet_record(rec):
                    # a worn record, a full box, or a pet: a pet on a character is bound and
                    # never goes back to the box (pet F13 [I]; the client refuses to bag a
                    # Type-6 box record whose level != 0); the exit replay's 0x6F re-bags it
                    if CASH.is_pet_record(rec) and not rec.get('equipped'):
                        out['bound'].append(serial)
                    out['kept'].append(serial)
                    continue
                items.remove(rec)
                rec.pop('equipped', None)
                box.append(rec)
                out['box'].append(serial)
            # counted once: ensure() may swap the list object, which `items` must stay
            used, owned = cash_tab_used(char), len(CASH.owned_records(char))
            items = char['cash_items']
            for serial in dict.fromkeys(to_char):
                rec = next((r for r in box if r['serial'] == serial), None)
                if rec is None:
                    out['same' if any(r['serial'] == serial for r in items) else 'unknown'].append(serial)
                    continue
                d = CASH.cash_def(rec['item_id'])
                pet = d is not None and d.is_pet and CASH.is_pet_record(rec)
                if pet or (d is not None and invmod.is_pet_gear(d.id) and not CASH.is_pet_record(rec)):
                    # P15 pet-s7: a pet - bound on the way (F13: level 1, awake, gauge 100) - or
                    # pet gear takes a slot of the EQUIPMENT tab, where the client files both
                    # (0x6F FUN_00464e00 cases 6 / 1; inventory.pet_slots counts the record just
                    # appended, so a second one in the same 0x42 sees the slot taken)
                    if owned >= CASH.OWNED_MAX or invmod.Inventory(char).free_slots('equip') < 1:
                        out['kept'].append(serial)
                        continue
                    box.remove(rec)
                    moved = {**rec, 'equipped': False}
                    if pet and CASH.bind_pet(moved):
                        out['bound_now'].append(serial)
                    items.append(moved)
                    owned += 1
                    out['char'].append(serial)
                    continue
                # A Cash 0 record (a `!gift` gift certificate) stays in the box, as grant()
                # refuses it: the client's 0x6F purges only Cash items before re-inserting the
                # list, so an in-world re-send would duplicate its cash-tab icon (P8 minor).
                if (d is None or d.type not in CASH.GRANT_TYPES or not d.cash or used >= CASH_TAB_SLOTS
                        or owned >= CASH.OWNED_MAX):
                    out['kept'].append(serial)
                    continue
                box.remove(rec)
                items.append({**rec, 'equipped': False})
                used += int(CASH.usable(rec))
                owned += 1
                out['char'].append(serial)
        if out['box'] or out['char']:
            self.store.mark_dirty(f'mall moves {session.get("username")}')
        hx = lambda serials: [f'{s:#x}' for s in serials]  # noqa: E731
        log.info(f'[MALL] {session.get("char_name")!r} 0x42 moves: to the box {hx(out["box"])}, to the '
                 f'character {hx(out["char"])}, already there {hx(out["same"])}, left {hx(out["kept"])}, '
                 f'unknown {hx(out["unknown"])} (deleted flag {deleted})'
                 + (f'; pets bound to the character (F13) {hx(out["bound_now"])}' if out['bound_now'] else ''))
        if out['bound']:
            log.info(f'[MALL] {session.get("char_name")!r}: bound pet(s) {hx(out["bound"])} stay on the character '
                     f'(a bound pet never goes back to the box, pet F13); the exit 0x6F re-bags them')
        if out['kept'] or out['unknown']:
            log.warning(f'[MALL] {session.get("char_name")!r}: serials {hx(out["kept"] + out["unknown"])} '
                        f'not moved (no room, not a cash-bag item, worn, or unknown); the exit 0x6F resyncs')
        return out

    def _leave(self, sock, session):
        """0x6B, then the map-load replay to the remembered point (F7 steps 6-8)."""
        srv = self.server
        state = session.pop('mall', None) or {}
        back = state.get('back') or {}
        session['in_cash_shop'] = False
        try:
            self._send(sock, session, '0x6B', {})
        except OSError as e:
            log.info(f'[MALL] 0x6B to {session.get("char_name")!r} not sent: {e}')
            return
        code = back.get('map', session.get('current_map'))
        if code is None:
            code = srv.config.START_MAP
        pos = session.get('pos') or (srv.config.START_X, srv.config.START_Y)
        x, y = back.get('x', pos[0]), back.get('y', pos[1])
        log.info(f'[MALL] {session.get("char_name")!r} leaves the {mall_name(self.build)}: back to map {code} '
                 f'at ({x:.0f}, {y:.0f})')
        srv._map_transfer(sock, session, code, x, y, reason='mall exit', no_enc=session.get('no_enc', True))

    # ---------------------------------------------------------------- refresh ---
    def refresh(self, sock, session):
        """C2S 0x46 -> exactly one S2C 0x70 {cash, mileage, first_purchase_bonus}."""
        acc = self._account(session)
        bonus = False
        if acc is not None:
            with self.store.lock:
                CASH.ensure_account(acc)
                bonus = bool(acc.get('first_purchase_notice'))
                if bonus:
                    acc['first_purchase_notice'] = False
            if bonus:
                self.store.mark_dirty(f'first purchase notice shown {session.get("username")}')
        fields = CASH.balance_fields(acc, bonus)
        self._send(sock, session, '0x70', fields)
        n = session['mall_refreshes'] = int(session.get('mall_refreshes') or 0) + 1
        log.info(f'[MALL] 0x46 balance refresh #{n} for {session.get("username")!r} -> 0x70 cash '
                 f'{fields["cash_balance"]}, mileage {fields["mileage_balance"]}, bonus {int(bonus)} '
                 f'(clears the charge flag: no resend until the next charge)')
        return fields

    # -------------------------------------------------------------------- buy ---
    def buy(self, sock, session, payload):
        """C2S 0x43 (F4). Returns the S2C 0x6C field dicts sent."""
        def refuse(code, why):
            log.info(f'[MALL] {session.get("char_name")!r} 0x43 {bytes(payload).hex(" ")} refused '
                     f'(0x6C {{{code:#x}}}): {why}')
            fields = {'result': code}
            self._send(sock, session, '0x6C', fields)
            return [fields]

        if not session.get('in_cash_shop'):
            return refuse(BUY_FAILED, 'not in the mall')
        rec, err = registry.decode(0x43, payload, self.build)
        if rec is None:
            return refuse(BUY_FAILED, f'payload does not decode ({err})')
        cart = cart_items(rec)
        if not 1 <= len(cart) <= CART_MAX or len(cart) != _int(rec.get('item_count')):
            return refuse(BUY_FAILED, f'{len(cart)} item(s) (1..{CART_MAX})')
        defs = []
        for code, option in cart:
            d = CASH.cash_def(code)
            why = sale_refusal(d, code, option, pets=self.pets)
            if why is not None:
                return refuse(BUY_FAILED, why)
            defs.append(d)
        srv = self.server
        acc, char = self._account(session), srv._session_char(session)
        if acc is None or char is None:
            return refuse(BUY_FAILED, 'no account / character')
        pay_mileage = bool(_int(rec.get('pay_with_mileage')))
        total = sum(int(d.price) for d in defs)
        with self.store.lock:
            # Checked and committed under one hold of the store lock (the wallet, the box
            # and the tab capacities change together or not at all); sent after it.
            refusal, sent, caps, with_mileage = self._buy_commit(acc, char, defs, total, pay_mileage)
        if refusal is not None:
            return refuse(*refusal)
        self.store.mark_dirty(f'mall buy {session.get("username")}')
        wanted = any(d.id in CASH.SLOT_EXT for d in defs)
        for d, fields in sent:
            self._send(sock, session, '0x6C', fields)
        log.info(f'[MALL] {session.get("char_name")!r} bought {", ".join(f"{d.name or d.id} ({d.id})" for d in defs)} '
                 f'for {total} {"mileage" if with_mileage else "cash"}: serials '
                 f'{[hex(f["item_serial"]) for _, f in sent]}; cash {acc["cash"]}, mileage {acc["mileage"]}'
                 + (f'; caps tab {caps["tab"]} bank {caps["bank"]}' if wanted else ''))
        if not with_mileage:
            self._purchase_event(session, total, 'buy')
        return [f for _, f in sent]

    def _buy_commit(self, acc, char, defs, total, pay_mileage):
        """The checks that read the wallet / box / tabs and, when they pass, the whole
        purchase (caller holds store.lock). Returns (refusal (code, why) | None, [(def, 0x6C
        fields)], capacities {'tab', 'bank'}, paid with mileage)."""
        box = CASH.ensure_account(acc)['cash_box']
        cash, mileage = CASH.balance(acc)
        bag = invmod.Inventory(char)
        BT.ensure(char)
        caps = {'tab': bag.capacities(), 'bank': list(char['bank_slots'])}
        wanted = collections.Counter(CASH.SLOT_EXT[d.id] for d in defs if d.id in CASH.SLOT_EXT)
        for (kind, i), n in wanted.items():
            if caps[kind][i] + TAB_EXT_STEP * n > TAB_CAP[kind]:
                return ((BUY_FAILED, f'{kind} tab {i} is at {caps[kind][i]}: +{TAB_EXT_STEP * n} would pass '
                                     f'the client cap {TAB_CAP[kind]}'), [], caps, False)
        records = sum(1 for d in defs if d.id not in CASH.SLOT_EXT)
        if len(box) + records > BOX_MAX:
            return ((BUY_FAILED, f'box full ({len(box)} + {records} > {BOX_MAX} records: one 0x6A frame)'),
                    [], caps, False)
        if pay_mileage and mileage >= total:
            with_mileage = True
        elif (not pay_mileage or self.build == BUILD_2009) and cash >= total:
            # 2009: the client's own fallback - a short mileage balance falls through to its
            # cash check and the packet still says pay_with_mileage 1 (spec_2009 0x43 gates)
            with_mileage = False
        else:
            return ((BUY_SHORT, f'total {total}: cash {cash}, mileage {mileage} '
                                f'(pay with {"mileage" if pay_mileage else "cash"})'), [], caps, False)
        origin = CASH.ORIGIN_MILEAGE if with_mileage else CASH.ORIGIN_CASH
        sent = []
        for d in defs:
            if with_mileage:
                mileage -= d.price
            else:
                cash -= d.price
                mileage = CASH.clamp_cash(mileage + self._bonus(d.price))
            rec = self._new_record(d, origin)
            if d.id in CASH.SLOT_EXT:
                # applied, not stored: the client frees the record after its own +5
                kind, i = CASH.SLOT_EXT[d.id]
                caps[kind][i] += TAB_EXT_STEP
                if kind == 'tab':
                    bag.set_capacity(invmod.TABS[i], caps[kind][i])
                else:
                    char['bank_slots'][i] = caps[kind][i]
            else:
                box.append(rec)
            sent.append((d, buy_fields(cash, mileage, rec)))
        acc['cash'], acc['mileage'] = CASH.clamp_cash(cash), CASH.clamp_cash(mileage)
        if not with_mileage:
            acc['first_purchase_done'] = True
        return None, sent, caps, with_mileage

    # ----------------------------------------------------------------- delete ---
    def delete(self, sock, session, payload):
        """C2S 0x45 (F5) -> 0x6E. Returns the fields sent."""
        def refuse(why):
            log.info(f'[MALL] {session.get("char_name")!r} 0x45 {bytes(payload).hex(" ")} refused (0x6E {{0}}): {why}')
            self._send(sock, session, '0x6E', {'result': 0})
            return {'result': 0}

        if not session.get('in_cash_shop'):
            return refuse('not in the mall')
        rec, err = registry.decode(0x45, payload, self.build)
        if rec is None:
            return refuse(f'payload does not decode ({err})')
        serial, item = _int(rec.get('cash_item_serial')), _int(rec.get('item_code'))
        acc = self._account(session)
        if acc is None:
            return refuse('no account')
        fields = None
        with self.store.lock:
            box = CASH.ensure_account(acc)['cash_box']
            found = next((r for r in box if r['serial'] == serial), None)
            if found is not None and found['item_id'] == item:
                refund = delete_refund(found, CASH.cash_def(item))
                box.remove(found)
                acc['mileage'] = CASH.clamp_cash(CASH.balance(acc)[1] + refund)
                fields = {'result': 1, 'mileage_balance': acc['mileage'], 'item_serial': serial}
        if fields is None:
            return refuse(f'no box record {serial:#x} of item {item}')
        self.store.mark_dirty(f'mall delete {session.get("username")}')
        self._send(sock, session, '0x6E', fields)
        log.info(f'[MALL] {session.get("char_name")!r} deleted box record {serial:#x} ({EC.item_name(item) or item}): '
                 f'refund {refund} mileage -> {fields["mileage_balance"]}')
        return fields

    # ------------------------------------------------------------------- gift ---
    def gift(self, sock, session, rec):
        """C2S 0x47 (F6) -> 0x71. Returns the fields sent."""
        def answer(result, why=None, cash=None, mileage=None):
            fields = gift_result_fields(result, self.build, cash, mileage)
            self._send(sock, session, '0x71', fields)
            if why:
                log.info(f'[MALL] {session.get("char_name")!r} gift refused (0x71 {{{result:#x}}}): {why}')
            return fields

        item, option = _int(rec.get('item_code')), _int(rec.get('option'))
        if not session.get('in_cash_shop'):
            return answer(GIFT_FAILED, 'not in the mall')
        if self.build != BUILD_2009:
            # 2008: every Gift click passes the password dialog first, so one unlock pays for
            # exactly one attempt (consumed here, whatever the attempt's result).
            unlocked = (session.get('pw_ok') or {}).pop(PASSWORD_WINDOW_GIFT, None)
            if unlocked is None or time.monotonic() - unlocked > GIFT_PASSWORD_SECS:
                return answer(GIFT_FAILED, f'no password unlock of window 0x1F9 in the last '
                                           f'{GIFT_PASSWORD_SECS:.0f} s (C2S 0x51)')
        d = CASH.cash_def(item)
        why = sale_refusal(d, item, option, pets=self.pets)
        if why is None and d.id in CASH.SLOT_EXT:
            why = 'a slot extension is applied to a tab, never gifted'
        if why is not None:
            return answer(GIFT_FAILED, why)
        wanted = social.name_text(rec.get('recipient_name', b''))
        found = self.store.character_by_name(wanted) if wanted else None
        if found is None:
            return answer(GIFT_NO_CHAR, f'no character {wanted!r}')
        username, racc, rchar = found
        if username == session.get('username'):
            return answer(GIFT_OWN_CHAR, f'{rchar.get("name")!r} is on the sender\'s own account')
        flag = R.record_gender(rchar, racc, self.build)
        why = gender_refusal(d, flag)
        if why is not None:
            return answer(GIFT_GENDER, why)
        acc = self._account(session)
        text = P.cut_text(rec.get('message') or b'', CASH.GIFT_MESSAGE_MAX).decode('cp949', 'replace')
        sender = session.get('char_name') or ''
        refusal = None
        with self.store.lock:
            # Both accounts change under one hold of the store lock (the whole file is one
            # RLock): the sender pays exactly when the recipient's box gets the record.
            cash, mileage = CASH.balance(acc)
            rbox = CASH.ensure_account(racc)['cash_box']
            if len(rbox) >= BOX_MAX:
                refusal = (GIFT_FAILED, f'{username!r} box full ({len(rbox)} records)')
            elif cash < d.price:
                refusal = (GIFT_SHORT, f'price {d.price}, cash {cash}')
            else:
                cash -= d.price
                mileage = CASH.clamp_cash(mileage + self._bonus(d.price))
                gift_rec = self._new_record(d, CASH.ORIGIN_GIFT)
                rbox.append(gift_rec)
                racc['gift_inbox'].append({'sender': sender, 'message': text, 'item_id': d.id,
                                           'serial': gift_rec['serial'], 'delivered': False})
                acc['cash'], acc['mileage'] = CASH.clamp_cash(cash), mileage
        if refusal is not None:
            return answer(*refusal)
        self.store.mark_dirty(f'mall gift {session.get("username")} -> {username}')
        fields = answer(GIFT_OK, cash=acc['cash'], mileage=acc['mileage'])
        log.info(f'[MALL] {sender!r} gifts {d.name or d.id} ({d.id}) to {rchar.get("name")!r} ({username}): '
                 f'box serial {gift_rec["serial"]:#x}; cash {acc["cash"]}, mileage {acc["mileage"]}')
        self._push_gift(username, gift_rec)
        self._purchase_event(session, d.price, 'gift')
        return fields

    def dev_gift(self, sender, found, item_id, message):
        """`!gift <name> <item> <msg>`: the same gift as C2S 0x47, free (test aid). Returns
        (username, record). Raises CASH.CashError for an item that cannot be a gift."""
        username, racc, rchar = found
        d = CASH.cash_def(item_id)
        entry = {'sender': sender, 'message': P.cut_text(message, CASH.GIFT_MESSAGE_MAX).decode('cp949', 'replace'),
                 'item_id': item_id}
        why = gift_display_refusal(entry)
        if why is None and (not d.is_cash or (is_pet_item(d) and not self.pets) or d.id in CASH.SLOT_EXT):
            why = f'{d.name or d.id} ({d.id}) cannot be a box record' + (' (config MALL_PETS)' if is_pet_item(d) else '')
        if why is not None:
            raise CASH.CashError(f'not a gift: {why}')
        with self.store.lock:
            rbox = CASH.ensure_account(racc)['cash_box']
            if len(rbox) >= BOX_MAX:
                raise CASH.CashError(f'{username}\'s box is full ({len(rbox)} records)')
            rec = self._new_record(d, CASH.ORIGIN_GIFT)
            rbox.append(rec)
            racc['gift_inbox'].append({**entry, 'serial': rec['serial'], 'delivered': False})
        self.store.mark_dirty(f'gift {username}')
        log.info(f'[MALL] !gift {sender!r} -> {rchar.get("name")!r} ({username}): {d.name or d.id} ({d.id}), '
                 f'box serial {rec["serial"]:#x}')
        self._push_gift(username, rec)
        return username, rec

    def _push_gift(self, username, rec):
        """An online recipient account (in the world or in the mall) sees the gift now: 0x79
        in the mall (the record joins his box) unless his 0x6A listed it already, and the 0x6D
        notification. The gift itself is committed before this runs: a recipient whose
        connection closed meanwhile gets nothing now (logged at debug, as GameServer._push
        does) and the undelivered entry at his next login."""
        srv = self.server
        target = srv.world.session(self.store.uid_of(username))
        if target is None or target.get('sock') is None or target.get('closed') or target.get('kicked'):
            return False
        if not (target.get('in_world') or target.get('in_cash_shop')):
            return False                  # at character select / mid map load: its next map load
        with self.lock:
            state = target.get('mall') or {}
            try:
                if target.get('in_cash_shop') and state.get('open'):
                    listed = state.get('box')
                    if isinstance(listed, set) and rec['serial'] in listed:
                        # committed before his enter() took the 0x6A snapshot: in his box already
                        log.info(f'[MALL] gift serial {rec["serial"]:#x} for {username!r} is in the 0x6A he '
                                 f'got; no 0x79')
                    else:
                        srv._push(target, '0x79', CASH.raw_fields(rec), 'MALL', flush=False)
                        if isinstance(listed, set):
                            listed.add(rec['serial'])
                self.send_gift_queue(target.get('sock'), target, reason='gift', push=True, flush=False)
            except OSError as e:
                log.debug(f'[MALL] gift {rec["serial"]:#x} to {username!r} not pushed: {e}')
                return False
        self._flush(target)
        return True

    # ------------------------------------------------------------- gift inbox ---
    def send_gift_queue(self, sock, session, reason='map load', *, relight=False, push=False, flush=True):
        """S2C 0x6D for the account's undelivered gifts this connection's client has not
        queued yet (module docstring "Gifts"). relight: a map load, which reset the HUD
        button - with nothing new but gifts still queued, a count-0 0x6D lights it again
        (C20). The sends only queue under self.lock; flush=True writes them after it (a
        caller that holds the lock itself passes False and flushes after its own `with`).
        An account with no undelivered gift and nothing queued on this client returns at
        once without the lock: every map load of every player calls this. Returns the
        serials now in the client's queue."""
        srv = self.server
        acc, char = self._account(session), srv._session_char(session)
        if acc is None or char is None or sock is None:
            return []
        with self.store.lock:
            pending = any(isinstance(g, dict) and not g.get('delivered') for g in acc.get('gift_inbox') or [])
        if not pending and not session.get('gifts_on_client'):
            return []
        memos, new = [], []
        with self.lock:
            on_client = session.setdefault('gifts_on_client', [])
            with self.store.lock:
                inbox = CASH.ensure_account(acc)['gift_inbox']
                box = acc['cash_box']
                changed = False
                for g in list(inbox):
                    if g.get('delivered') or (g.get('serial') and g['serial'] in on_client):
                        continue
                    why = gift_display_refusal(g)
                    if why is not None:
                        inbox.remove(g)
                        memos.append((g, why))
                        changed = True
                        continue
                    if not g.get('serial'):
                        # a P5 `!gift` entry: its box record comes with its notification
                        d = CASH.cash_def(g['item_id'])
                        if not d.is_cash or (is_pet_item(d) and not self.pets) or len(box) >= BOX_MAX:
                            log.warning(f'[MALL] gift {d.name or d.id} for {session.get("username")!r} kept '
                                        f'pending: no box record possible (box {len(box)}/{BOX_MAX})')
                            continue
                        rec = self._new_record(d, CASH.ORIGIN_GIFT)
                        box.append(rec)
                        g['serial'] = rec['serial']
                        changed = True
                    new.append(dict(g))
            if changed:
                self.store.mark_dirty(f'gift inbox {session.get("username")}')
            if new:
                for fields in gift_pages(new, self.build):
                    self._send(sock, session, '0x6D', fields, flush=False)
                on_client.extend(g['serial'] for g in new)
            elif relight and on_client:
                self._send(sock, session, '0x6D', gift_pages([], self.build)[0], flush=False)
            queued = list(on_client)
        if flush:
            self._flush(session)
        for g, why in memos:
            text = f'[Gift] {EC.item_name(g["item_id"]) or g["item_id"]}: {g.get("message") or ""}'.strip()
            stored = srv.messenger.store_memo(session.get('char_name'), g.get('sender') or 'GM', text)
            log.warning(f'[MALL] gift {g} for {session.get("username")!r} would block the client\'s gift queue '
                        f'({why}): {"converted to a memo" if stored else "dropped (no memo possible)"}')
        if new:
            log.info(f'[MALL] {reason}: 0x6D {len(new)} gift(s) -> {session.get("char_name")!r} '
                     f'({"live" if push else "queued"}; popup at the next 0x6A): '
                     f'{[(g["sender"], g["item_id"], hex(g["serial"])) for g in new]}')
        return queued

    def _mark_gifts_shown(self, session, serials):
        """The 0x6A just popped every gift of the client's queue: delivered for good (a later
        connection does not send them again). Keeps the newest GIFT_HISTORY delivered ones."""
        serials = set(serials or ())
        if not serials:
            return 0
        acc = self._account(session)
        with self.lock:
            session['gifts_on_client'] = []
            if acc is None:
                return 0
            with self.store.lock:
                inbox = CASH.ensure_account(acc)['gift_inbox']
                n = 0
                for g in inbox:
                    if g.get('serial') in serials and not g.get('delivered'):
                        g['delivered'] = True
                        n += 1
                done = [g for g in inbox if g.get('delivered')]
                for g in done[:max(0, len(done) - GIFT_HISTORY)]:
                    inbox.remove(g)
        if n:
            self.store.mark_dirty(f'gifts shown {session.get("username")}')
        return n

    # ----------------------------------------------------- event records (C7) ---
    def grant_box(self, username, item_id, count=None, *, origin=CASH.ORIGIN_EVENT, reason='event'):
        """ROADMAP_2009_ADDENDUM C7 (events_bosses A5 G2 / E6): put `item_id` into `username`'s
        cash box as a new record of `origin` (default 3, the event / GM grant: no refund) and
        tell an owner in the world or in the mall with S2C 0x6C {1, cash, mileage, record} -
        the balances unchanged (module docstring "Event records"). A counted quantity above
        CASH.STACK_MAX becomes several records of <= STACK_MAX, as CashInventory.grant splits
        one (the bag shows only the quantity's low byte), each told by its own 0x6C.
        Returns (the first record, told).
        Only what the 0x42 move can take out of the box (apply_moves) is granted: a cash-bag
        item (CASH.GRANT_TYPES with Cash != 0), a pet (a level-0 box pet) or pet gear (P15
        pet-s7). Raises CASH.CashError for an unknown account, an id the client has not got, no
        cash item, a slot extension, a cash costume (Type 1 / 7: apply_moves keeps it in the box
        for good, an event record nobody could ever use), a Cash 0 item (kept the same way), or
        a full box."""
        acc = self.store.account(username)
        if acc is None:
            raise CASH.CashError(f'no account {username!r}')
        d = CASH.cash_def(item_id)
        if d is None:
            raise CASH.CashError(f'item {item_id} is not in the client item table')
        if not d.is_cash:
            raise CASH.CashError(f'{d.name or d.id} ({d.id}) is not a cash item (Type {d.type}, Cash {d.cash})')
        if d.id in CASH.SLOT_EXT:
            raise CASH.CashError(f'{d.name or d.id} ({d.id}) is a slot extension: the 0x6C applies it, it is never stored')
        if not is_pet_item(d) and d.type not in CASH.GRANT_TYPES:
            raise CASH.CashError(f'{d.name or d.id} ({d.id}) is a cash costume (Type {d.type}): the 0x42 '
                                 f'move never takes it out of the box (not modelled yet), so nobody could use it')
        if not is_pet_item(d) and not d.cash:
            raise CASH.CashError(f'{d.name or d.id} ({d.id}) is a Cash 0 item: the 0x42 move keeps it in the box '
                                 f'(CashInventory.grant refuses it too)')
        qty = d.quantity(count)
        if d.kind == CASH.KIND_COUNT:
            chunks = [CASH.STACK_MAX] * (qty // CASH.STACK_MAX) + ([qty % CASH.STACK_MAX] if qty % CASH.STACK_MAX else [])
        else:
            chunks = [count]
        with self.store.lock:
            box = CASH.ensure_account(acc)['cash_box']
            if len(box) + len(chunks) > BOX_MAX:
                raise CASH.CashError(f'{username}\'s box is full ({len(box)} records, {len(chunks)} more: at most '
                                     f'{BOX_MAX}, one 0x6A frame)')
            recs = [CASH.new_record(self.server.cash.next_serial(), d, origin, n) for n in chunks]
            box.extend(recs)
            cash, mileage = CASH.balance(acc)
            sends = [(rec, buy_fields(cash, mileage, rec)) for rec in recs]
        self.store.mark_dirty(f'box grant {username}')
        told = False
        for rec, fields in sends:
            told = self._push_box_record(username, rec, fields) or told
        serials = ', '.join(f'{r["serial"]:#x}' for r in recs)
        log.info(f'[MALL] box grant ({reason}): {username!r} + {d.name or d.id} ({d.id}) x{sum(r["qty"] for r in recs)} '
                 f'origin {recs[0]["origin"]} -> box serial(s) {serials}'
                 + ('; 0x6C (balances unchanged)' if told else '; not told (the next 0x6A lists it)'))
        return recs[0], told

    def _push_box_record(self, username, rec, fields):
        """The 0x6C of a box grant to an online owner: in the world (the handler has no state gate
        but the mall context the login set up, SubHandler4 [mall+0x14/+0x18]) or in the mall once
        its 0x6A is out and did not list the record (the gift push's snapshot rule)."""
        srv = self.server
        target = srv.world.session(self.store.uid_of(username))
        if target is None or target.get('sock') is None or target.get('closed') or target.get('kicked'):
            return False
        with self.lock:
            if target.get('in_cash_shop'):
                state = target.get('mall') or {}
                listed = state.get('box')
                if not state.get('open') or (isinstance(listed, set) and rec['serial'] in listed):
                    return False                 # the 0x6A being built / sent carries it
                if isinstance(listed, set):
                    listed.add(rec['serial'])
            elif not worldmod.reachable(target):
                return False                     # character select / mid map load: the next 0x6A
            told = srv._push(target, '0x6C', fields, 'MALL', flush=False)
        self._flush(target)
        return told

    # ------------------------------------------------ 2009 cash opcodes (C8) ---
    def sale_offer(self, sock, session, rec):
        """2009 C2S 0x80 CashItemSaleOffer {item, option, buyer, u64 price} -> S2C 0x71 {is_trade
        1, 0x17}: the waiting box needs an answer, and the sale is not offered (module docstring
        "The 2009-only cash opcodes"). Returns the fields sent."""
        fields = {'is_trade': 1, 'result': TRADE_NO_TARGET}
        self._send(sock, session, '0x71', fields)
        log.info(f'[MALL] {session.get("char_name")!r} 0x80 cash item sale offer: item {_int(rec.get("item_code"))} '
                 f'option {_int(rec.get("option"))} to {social.name_text(rec.get("buyer_name", b""))!r} for '
                 f'{_int(rec.get("price"))} gold refused (0x71 {{1, 0x17}}: cash item sales are not offered)')
        return fields

    def sale_reply(self, sock, session, rec):
        """2009 C2S 0x81 CashItemSaleReply {u8 reply}: 1 (the seller's Cancel of window 0x4B8,
        which only the server closes) -> S2C 0x71 {is_trade 1, 0x16}; 0 / 2 (the buyer's
        answer to an 0xA9 offer, which is never sent) -> no reply. Returns the fields sent or
        None."""
        reply = _int(rec.get('reply'))
        if reply == SALE_CANCEL:
            fields = {'is_trade': 1, 'result': TRADE_CANCELLED}
            self._send(sock, session, '0x71', fields)
            log.info(f'[MALL] {session.get("char_name")!r} 0x81 {{1}}: sale window cancelled - 0x71 {{1, 0x16}} closes '
                     f'window 0x4B8 (no offer is ever pending)')
            return fields
        log.info(f'[MALL] {session.get("char_name")!r} 0x81 {{{reply}}}: no cash item offer is pending (the server '
                 f'never sends S2C 0xA9); no reply - C2S 0x81 opens no waiting box')
        return None

    def add_option(self, sock, session, rec):
        """2009 C2S 0x4E CashItemAddOption {pay_with_mileage, item, option, serial} (window 0x4DA)
        -> S2C 0xC4 {0} "You failed to buy the item." (paid options are not modelled; the
        request opens no waiting box, the answer is feedback). Returns the fields sent."""
        fields = {'result': BUY_FAILED}
        self._send(sock, session, '0xC4', fields)
        log.info(f'[MALL] {session.get("char_name")!r} 0x4E add option {_int(rec.get("option"))} to item '
                 f'{_int(rec.get("item_id"))} (serial {_int(rec.get("serial")):#x}) refused (0xC4 {{0}}: paid '
                 f'options are not modelled)')
        return fields

    # ---------------------------------------------------------------- views ---
    def describe(self, session):
        """Lines for `!mall status`."""
        acc = self._account(session) or {}
        with self.store.lock:
            cash, mileage = CASH.balance(acc)
            box = list(CASH.ensure_account(acc)['cash_box']) if acc else []
            inbox = [g for g in (acc.get('gift_inbox') or []) if not g.get('delivered')]
        state = session.get('mall') or {}
        back = state.get('back')
        lines = [f'{mall_name(self.build)}: {"inside" if session.get("in_cash_shop") else "outside"}; '
                 f'Wind Cash {cash}, Mileage {mileage}; box {len(box)}/{BOX_MAX}, gifts pending {len(inbox)}, '
                 f'queued on this client {len(session.get("gifts_on_client") or [])}.']
        if back:
            lines.append(f'way back: map {back["map"]} at ({back["x"]:.0f}, {back["y"]:.0f})')
        for rec in box[:6]:
            lines.append(f'{rec["serial"]:#x} {EC.item_name(rec["item_id"]) or rec["item_id"]} ({rec["item_id"]}) '
                         f'x{rec["qty"]} origin {rec["origin"]}')
        return lines
