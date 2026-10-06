#!/usr/bin/env python3
"""
registry.py - C2S handler registry and reply policy (roadmap 1.3 F2: arch-handler-registry)
==========================================================================================
GameServer.ROUTES maps each C2S opcode to a Route; GameServer._dispatch hands every
decoded client packet to dispatch() here. dispatch():

  1. decodes the payload with packets.parse (every send-site variant of the opcode) and
     logs the decoded fields, or the hex when no grammar matches;
  2. runs the route: a GameServer method (legacy handlers get the raw payload bytes,
     style='rec' handlers get the packets.Record), a log-only consume, or nothing
     (unhandled: logged with hex and decoded fields);
  3. applies the reply policy from a `finally`, so it holds even when the handler raises:
     - MUST_REPLY: the request opened "Waiting for the server to response." or set a
       client lock, and the patched exe removed the timer-2 fallback (0x43ED30), so no
       reply means a permanent hang (S1-09). If the handler sent nothing to this session,
       the minimal refusal from the roadmap 1.3 table is sent (exactly one reply).
     - NEVER_REPLY: answering corrupts client state (0x05 resets the world, a 0x0D echo
       desyncs the cipher). A handler that sends anything is logged as a policy error.

"The handler sent something" is tracked per thread: GameServer._send_encrypted calls
note_send() for every packet, which counts only packets to the session whose request is
being dispatched on the calling thread (the combat driver / admin injector / tick threads
never mark a request as answered).

A handler exception is logged and the connection stays up (before the registry it
escaped to _handle_fireway, which closed the socket and left the client hung on
"Waiting for the server").

Changing a route (stage 3 misroute fixes, new handlers) is a one-line edit of
GameServer.ROUTES. A legacy route whose own reply logic is still incomplete sets
fallback=False; deleting that flag (or the route) turns the refusal on.

Interim stubs (P0: pvp-modal-guard, social_friend-note-stub, shop_storage-stall-stub,
item_inventory-interim-craft-replies) answer with the MUST_REPLY refusal itself: a log-only
route lets the policy send it (logged as "stub route"), and a handler with side effects
calls send_refusal() so the reply bytes live in this one table. When the owner item lands,
its handler answers every request itself and the row stays as the exception backstop (P4
stage 3: C2S 0x67 / 0x68 / 0x69 are GameServer._handle_craft_complete / _reinforce_ /
_gather_ now; P7 stage 2: C2S 0x5E..0x62 are the stall handlers of market.py; P8 stage 4:
C2S 0x72 is GameServer._handle_stone_extract).
"""
import contextlib
import logging
import os
import threading
from dataclasses import dataclass

import packets as P

log = logging.getLogger('WS')

STYLE_RAW = 'raw'     # handler(sock, session, payload: bytes, no_enc)
STYLE_REC = 'rec'     # handler(sock, session, rec: packets.Record (rec.raw = payload), no_enc)


@dataclass(frozen=True)
class Route:
    """How one C2S opcode is handled.

    handler:  GameServer method name, or None (log-only consume / unhandled).
    style:    STYLE_RAW (every pre-registry handler) or STYLE_REC. A STYLE_REC handler is
              not called for a payload that matches no C2S grammar (the MUST_REPLY refusal
              still goes out).
    log:      info line logged when the route has no handler (consume-only opcodes).
    fallback: False suppresses the MUST_REPLY refusal for this route: the legacy handler
              answers on some paths and its fix belongs to a later item (see note).
    quiet:    skip the per-packet decoded-fields debug line (0x0D movement, 0x05 keepalive).
    note:     why the route is what it is (bug ids, owning items).
    """
    handler: str = None
    style: str = STYLE_RAW
    log: str = None
    fallback: bool = True
    quiet: bool = False
    note: str = ''


@dataclass(frozen=True)
class MustReply:
    """state: what the client is left in without a reply. refusal: fn(server, session, rec)
    -> [(S2C key, fields dict | raw payload bytes, assume dict | None)], or None when the
    minimal refusal needs infrastructure that does not exist yet. text: the refusal as the
    roadmap 1.3 table writes it. owner: item that implements the full handler."""
    state: str
    refusal: object
    text: str
    owner: str


def _reply(key, **fields):
    return lambda server, session, rec: [(key, fields, None)]


def _field(rec, name, default=0):
    return default if rec is None else int(rec.get(name, default))


def _enter_world_refusal(server, session, rec):
    # lc-enter-world F5 step 2: an unknown character name gets the 0x02 character list,
    # which closes window 0x16 and reloads the select screen. _build_login_success is the
    # one 0x02 builder (lc-charlist: records.character_list + the grammar), so this
    # refusal and the login reply are the same bytes.
    account = server.accounts.get(session.get('username'))
    if account is None:
        return []
    return [('0x02', server._build_login_success(session, account), None)]


def _create_room_refusal(server, session, rec):
    # pvp-modal-guard: room_type 4 is a play room (0xA2 "too many play rooms"), every
    # other type an arena (0x30 "too many arena channels"). Both replace the waiting text
    # in message box 0x16 with a dismissible error.
    return [('0xA2' if _field(rec, 'room_type') == 4 else '0x30', {}, None)]


def _join_room_refusal(field):
    def refusal(server, session, rec):
        # 0x34 result 2 rewrites message box 0x16 with an error; both bytes are always read.
        return [('0x34', {'result': 2, 'room_type': _field(rec, field) & 0xFF}, None)]
    return refusal


def _cash_balance_refusal(server, session, rec):
    # premium_cash-balance-refresh backstop: S2C 0x70 with the account's REAL balances (the
    # P8 stage 1 wallet, cash.balance_fields) and no first-purchase popup. Any 0x70 clears
    # the client's charge-pending flag (mall+0x5AC), which stops the 0x46 re-send on every
    # window restore; the Wind Cash label shows what the store holds, not 0.
    import cash as cashmod
    acc = server.accounts.get(session.get('username')) if hasattr(server, 'accounts') else None
    return [('0x70', cashmod.balance_fields(acc), None)]


GIFT_REPLY_NOTE_ID = 9999


def _note_refusal(server, session, rec):
    # Both 0x4B send sites have the same shape. Item 9999 is the gift thank-you reply
    # (0x46098E), which shows no waiting box: a 0x77 {0} there would only pop "Failed to
    # send the message." (chat_mail_gm.md 1.4; chat_mail_gm-note-reply-9999 owns it).
    item = _field(rec, 'note_item_id', _field(rec, 'item_id'))
    if item == GIFT_REPLY_NOTE_ID:
        return []
    return [('0x77', {'result': 0}, None)]


SKILL_CAST_KEY = '0x44C239/0x15'
# The skill send site of each build (2009: spec_2009 0x44FC61/0x15, same grammar).
SKILL_CAST_KEYS = (SKILL_CAST_KEY, '0x44FC61/0x15')


def is_skill_cast(server, rec):
    """C2S 0x15 came from the client's Type-3 branch (FUN_0044c090), which set the
    scene+0x258 cast lock. The 6 B form (Booby Trap / Puppet x,y) exists only at the skill
    send site; a 2 B payload decodes as both sites, so the client's own item Type decides
    (EN catalog def+0x154, server._client_item_type)."""
    if rec is None:
        return False
    if len(rec.candidates) == 1 and rec.candidates[0] in SKILL_CAST_KEYS:
        return True
    item = rec.get('skill_id', rec.get('item_id', 0))
    return server._client_item_type(item) == 3


BUILD_2009 = '2009'


def _build_of(server):
    return getattr(server, 'client_build', None)


def _skill_cast_refusal(server, session, rec):
    # cs-cast-unlock (S1-10): 0x5F clears scene+0x258 with no effect, so casting and
    # equip/unequip work again. The 0x5F handler derefs the scene with no null check
    # (spec 0x5F gates): in world only. Consumables (Type 0) need no refusal. Since
    # cs-skill-cast, GameServer._handle_cast_skill sends it for every refused cast and this
    # policy entry is the backstop for a cast handler that raises.
    # 2009 (client-2009-world): there is no pending-skill word to clear - the send site
    # FUN_0044F070 gates a cast on scene+0x259 != 7 and its own 1 s stamp scene+0x260 only,
    # and S2C 0x5F has no handler at all (s2c_format_diff gone_in_2009). Nothing is owed.
    if _build_of(server) == BUILD_2009:
        return []
    if not session.get('in_world') or not is_skill_cast(server, rec):
        return []
    return [('0x5F', {}, None)]


def _gift_refusal(server, session, rec):
    # premium_cash-gift: 0x71 {0} "generic failure". spec_2009 0x71 adds a leading bool
    # is_trade (0 = the cash-shop gift reply C2S 0x47 waits for, 1 = a cash item sale).
    if _build_of(server) == BUILD_2009:
        return [('0x71', {'is_trade': 0, 'result': 0}, None)]
    return [('0x71', {'result': 0}, None)]


def _stall_stop_refusal(server, session, rec):
    # shop_storage F12 (backstop of market.Market.stop_edit since P7 stage 2): 0x83 {1} only
    # while the client can be selling - the selling mirror (set by 0x5E) or an open stall.
    # With both clear this 0x5F crossed our own 0x83 / 0x84, and 0x83 {1} sets ctx+0x1C in
    # ANY mode (C8, live shop_storage#18): a later buyer window would put a display-only
    # copy of the stall it browsed into the bag (P7 stage 2 review).
    market = getattr(server, 'market', None)
    if not session.get('stall_client_selling') and not (market is not None and market.selling(session)):
        return []
    return [('0x83', {'result': 1}, None)]


def _stall_close_refusal(server, session, rec):
    # shop_storage F13 (backstop of market.Market.close_request since P7 stage 2): 0x84 {1}
    # only while the server mirrors the client's selling flag (set by 0x5E). The client
    # re-sends 0x60 automatically (after S2C 0x08, and from FUN_0042cf20 while +0x15B4 != 8)
    # until a reply lands; those duplicates are ignored.
    if not session.get('stall_client_selling'):
        return []
    return [('0x84', {'result': 1}, None)]


def _sell_refusal(server, session, rec):
    # shop_storage-sell-parse (F2 step 5): the 0x18 {gold, victy, 0, 0} resync puts the gold
    # label back and grants nothing, the 0x15 warning says why. Never a 0x19 with a count: it
    # removes client items. GameServer._handle_sell_item sends the same pair with a specific
    # reason; this generic form covers a payload that does not decode and a handler crash.
    return server._shop_refusal_replies(session, "The item could not be sold.")


def _buy_refusal(server, session, rec):
    # shop_storage-npc-buy (F1 step 5): the same resync + warning pair as a sell. Never a
    # 0x18 with a non-zero item: it would grant it. GameServer._handle_buy_item sends the
    # pair with a specific reason; this generic form covers a payload that does not decode
    # and a handler crash.
    return server._shop_refusal_replies(session, 'The item could not be bought.')


def _revive_refusal(server, session, rec):
    # cs-player-death: the death dialog closes CLIENT-SIDE on the click (live combat_skill#21),
    # so a dead player whose revive failed (the handler raised, or the MapTransfer was refused)
    # would be left dead with no UI at all. S2C 0x3E re-opens window 0x79 so he can ask again
    # (it re-applies the death processing, which changes nothing on a corpse). 0x3E derefs
    # scene+0x970 with no null check: in world only. Alive (a repeated click after the revive
    # landed) needs nothing.
    if session.get('dead') and session.get('in_world'):
        return [('0x3E', {}, None)]
    return []


def _cash_item_use_refusal(server, session, rec):
    # 0x72 for the local player closes the waiting box. item_id 0 has no item def, so the
    # grammar stops after the serial: {uid, 0, 0} = 10 B (roadmap 1.3).
    return [('0x72', {'player_uid': P.session_uid(session) or 0, 'item_id': 0, 'item_serial': 0},
             {'item_def(item_id) == null': True})]


def _stat_reset_refusal(server, session, rec):
    # premium_cash-stat-reset (P8 stage 3, cashuse.py): the 18-byte owner form of S2C 0x76 with
    # the stats the client holds (the stored char['str'/'dex'/'int'/'spr'] every 0x07 / 0x03
    # carries since P1), serial 0 and count 0 - it closes the waiting box and consumes nothing.
    # The handler sends the same bytes for every refused request; this is its backstop.
    cashuse = getattr(server, 'cashuse', None)
    if cashuse is None:
        return []
    return [('0x76', cashuse.refusal_76(session), {'target_uid == local_player_uid': True})]


def _trade_refusal(server, session, rec):
    # trade-cancel-lifecycle backstop (trade.md 2.8): a trade handler that raised (or a 0x24 /
    # 0x25 whose payload does not decode) leaves the sender's window with End/Cancel
    # disabled (0x24) or its confirm waiting (0x25). S2C 0x49 releases it - and since the
    # server's TradeState can no longer be trusted, the partner's trade is cancelled with it
    # (its own 0x49). A session with no trade gets the 0x49 alone (harmless without a
    # window: trade.md A7).
    trades = getattr(server, 'trade', None)
    if trades is not None:
        trades.cancel(session, 'a trade handler failed', notify_self=False)
    return [('0x49', {}, None)]


# C2S opcode -> MustReply. Rows and refusals are roadmap 1.3 (bug S1-09); byte layouts come
# from the S2C grammars in protocol_spec.json. The refusal is the minimum that releases the
# client; the owner item replaces it with real handling.
MUST_REPLY = {
    # Not in the roadmap 1.3 table: the old server always answered a create with the 0x02
    # list, so it never hung. lc-create answers 0x1C instead, and a create that raises
    # would otherwise leave window 0x16 up - result 3 is the client's "Server process is
    # running. Please try again in a few minutes (DB)" box (live login_character#06).
    0x0E: MustReply('modal (message box 0x16) over the create window', _reply('0x1C', result=3),
                    '0x1C {3} "try again (DB)"', 'lc-create'),
    0x12: MustReply('modal (message box 0x16)', _reply('0x1F', result=2), '0x1F {2} "try again (DB)"',
                    'lc-delete'),
    0x2B: MustReply('enter-world wait dialog; no timeout in the patched exe', _enter_world_refusal,
                    're-send the 0x02 character list', 'lc-enter-world'),
    0x15: MustReply('scene+0x258 skill lock: no casts, equip refused', _skill_cast_refusal,
                    '0x5F for skill (Type 3) requests in world', 'cs-skill-cast'),
    0x18: MustReply('modal', _create_room_refusal, '0x30 (type<4) / 0xA2 (type 4)', 'pvp-room-create'),
    0x1A: MustReply('modal', _join_room_refusal('room_type'), '0x34 {2, type}', 'pvp-room-join'),
    0x1B: MustReply('modal', _join_room_refusal('room_kind'), '0x34 {2, type}', 'pvp-room-join'),
    0x1C: MustReply('modal', _join_room_refusal('room_category'), '0x34 {2, type}', 'pvp-room-join'),
    0x39: MustReply('modal', lambda server, session, rec: [('0x39', {}, None),
                                                           ('0x20', {'waiting_count': 0}, None)],
                    '0x39 + 0x20 {0}', 'pvp-battlefield-queue'),
    0x3A: MustReply('modal', _reply('0x20', waiting_count=0), '0x20 {0}', 'pvp-battlefield-queue'),
    0x47: MustReply('modal', _gift_refusal, '0x71 {0 generic failure} (2009: is_trade 0 first)',
                    'premium_cash-gift'),
    0x48: MustReply('modal', _cash_item_use_refusal, '0x72 {uid, 0, 0}', 'premium_cash-use-generic'),
    0x49: MustReply('modal', _reply('0x73', result=0), '0x73 {0}', 'premium_cash-rename'),
    # 0x76 local form with the stats the client holds (the stored ones, sent in every 0x07)
    # - since P8 stage 3 the backstop of the stat-reset handler (cashuse.py).
    0x4A: MustReply('modal', _stat_reset_refusal, '0x76 local form, unchanged stats, serial 0, count 0',
                    'premium_cash-stat-reset'),
    0x4B: MustReply('modal (Note item send; the 9999 gift reply has none)', _note_refusal,
                    '0x77 {0} (not for note item 9999)', 'social_friend-memos'),
    # The account's Wind Cash / Mileage (P8 stage 1 wallet; D16: Wind Cash is not victy).
    # Answering stops the client re-sending 0x46 on every window restore.
    0x46: MustReply('re-sent on every restore', _cash_balance_refusal,
                    '0x70 {cash, mileage, 0}', 'premium_cash-balance-refresh'),
    0x51: MustReply('dialog closes, nothing opens', _reply('0x80', result=0), '0x80 {0}',
                    'shop_storage-password-gate'),
    # Backstop only since P4 stage 4: GameServer._handle_card_register answers every 0x64
    # (cards.register: 0x8B 1/2/3/6). {3} is the generic "Failure to register card." box,
    # which closes the waiting box and changes nothing on either side.
    0x64: MustReply('modal', _reply('0x8B', result=3), '0x8B {3}', 'quest_cards_misc-card-register'),
    # Backstops only since P7 stage 2: market.py answers every 0x5E..0x62 itself (0x82 1/2/9,
    # 0x83 {1}, 0x84 {1}, 0x87 1/0/6, 0x88 1/0 + 0x89). Never another 0x82 / 0x83 source (C8).
    0x5E: MustReply('ctx+0x20 selling; window cannot close', _reply('0x82', result=2), '0x82 {2}',
                    'shop_storage-stall-open-close'),
    0x5F: MustReply('ctx+0x20 selling; window cannot close', _stall_stop_refusal,
                    '0x83 {1} while a 0x5E is on record', 'shop_storage-stall-open-close'),
    0x60: MustReply('ctx+0x20 selling; window cannot close', _stall_close_refusal,
                    '0x84 {1} while a 0x5E is on record', 'shop_storage-stall-open-close'),
    0x61: MustReply('stall window waits', _reply('0x87', result=0), '0x87 {0}',
                    'shop_storage-stall-browse-buy'),
    0x62: MustReply('stall window waits', _reply('0x88', result=0), '0x88 {0}',
                    'shop_storage-stall-browse-buy'),
    0x67: MustReply('busy flag ctx+0x2C', lambda server, session, rec: [
                        ('0x8D', {'result': 0x0F, 'product_item_id': _field(rec, 'product_item_id')}, None)],
                    '0x8D {0x0F, id}', 'item_inventory-crafting'),
    0x68: MustReply('busy flag ctx+0x2C', lambda server, session, rec: [
                        ('0x8E', {'result': 0x11, 'equip_item_id': _field(rec, 'equip_item_id'),
                                  'stone_item_id': _field(rec, 'stone_item_id')}, None)],
                    '0x8E {0x11, eq, stone}', 'item_inventory-reinforcement'),
    # 0x67 / 0x68 / 0x69: backstops only - the P4 stage 3 handlers answer every request
    # (crafting.py). The no-change results: 0x8D 0x0F consumes nothing and 0x8E 0x11 changes
    # nothing; 0x8F removes one tool client-side on ANY result, which the handler mirrors in
    # the bag model - this raised-handler fallback cannot, so the next 0x03 gives it back.
    0x69: MustReply('busy flag ctx+0x28', lambda server, session, rec: [
                        ('0x8F', {'result': 2, 'tool_item_id': _field(rec, 'tool_item_id')}, None)],
                    '0x8F {2, tool}', 'item_inventory-gathering'),
    # P8 stage 4: GameServer._handle_stone_extract answers every in-world request itself; the
    # 0x9C {0} (nothing changes, no tool consumed) stays the backstop for a request from
    # outside the world and a handler that raises.
    0x72: MustReply('busy flag', _reply('0x9C', result=0), '0x9C {0}', 'item_inventory-stone-extraction'),
    # P7 stage 1 (trade.py): both handlers answer every request themselves (0x49 / 0x4A, or
    # the deliberate wait of a first 0x25: defer_reply). The refusal is the exception
    # backstop: the whole trade is cancelled, so the partner gets its 0x49 too.
    0x24: MustReply('trade Cancel/X disabled', _trade_refusal, '0x49 (the partner too)', 'trade-cancel-lifecycle'),
    0x25: MustReply('trade confirm waits', _trade_refusal, '0x49 (the partner too)', 'trade-lock-commit'),
    0x0B: MustReply('gold label stale', _buy_refusal, '0x18 {gold, victy, 0, 0} resync + 0x15',
                    'shop_storage-npc-buy'),
    0x0C: MustReply('gold label stale', _sell_refusal, '0x18 {gold, victy, 0, 0} resync + 0x15',
                    'shop_storage-sell-parse'),
    0x5D: MustReply('window waits', _reply('0x81', result=0), '0x81 {0}', 'world-village-transfer'),
    0x70: MustReply('window waits', _reply('0x9A', result=0), '0x9A {0}', 'premium_cash-region-warp'),
    0x71: MustReply('window waits', _reply('0x9B', result=0), '0x9B {0}', 'premium_cash-friend-warp'),
    # GameServer._handle_revive_request answers with the MapTransfer to the revive point; the
    # refusal is the backstop that gives a dead player the dialog back if that fails.
    0x2E: MustReply('death dialog closed client-side: player dead forever', _revive_refusal,
                    "0x3E (re-open the death dialog) while dead", 'cs-player-death'),
}

# C2S opcode -> why the server must never answer it (roadmap 1.3 "Never reply to").
NEVER_REPLY = {
    0x05: 'keepalive: a reply resets in-game state',
    0x0D: 'movement: an echo to the mover desyncs the cipher',
    0x2D: 'arena room list close',
    0x75: 'play room list close',
    0x40: 'privacy flags',
    0x37: 'messenger status',
    0x44: 'memo delete',
    0x7C: 'dungeon exit (log only until instances exist)',
    # chat_mail_gm F3 / spec 0x46FE14/0x6B (2009 0x47ACE9/0x6B): the client prints its own
    # green line before sending; any S2C 0x91 back to the sender would show it twice. The
    # relay goes to the friends only (GameServer._handle_friend_chat).
    0x6B: 'friend chat: the client echoes the line itself',
}

# Per-build changes to the two tables (client-2009-world). A None value removes the 2008
# row: the 2009 client has no such send site (c2s_format_diff gone_in_2009: 0x74/0x75 play
# room list open/close, 0x7C) or the opcode means something else now (0x2D is the instance
# dungeon room kick; the list close is C2S 0x2C u8 0, which is answered by nothing).
BUILD_NEVER_REPLY = {
    BUILD_2009: {
        0x2D: None,
        0x75: None,
        0x7C: None,
        # spec_2009 0x45DDFB/0x9E: the X-Trap answer to S2C 0xC5, which the server never
        # sends (packets.FORBIDDEN_S2C); one arriving anyway is consumed silently.
        0x9E: 'X-Trap response (the server never sends S2C 0xC5)',
    },
}
BUILD_MUST_REPLY = {
    BUILD_2009: {
        # The 2009-only cash / pet requests P8 owns (ROADMAP_2009_ADDENDUM C5 / C8; mall.py "The
        # 2009-only cash opcodes", pets.py). Their handlers answer every request themselves;
        # these rows are the exception backstop (the same bytes):
        #   0x4E CashItemAddOption (window 0x4DA, send 0x468706): NO waiting box (Add + Send,
        #        return) - S2C 0xC4 {0} "You failed to buy the item." is the feedback the
        #        dialog would otherwise never get (spec_2009 0xC4: failures rewrite box 0x16).
        #   0x80 CashItemSaleOffer (window 0x4B8): Send, then the waiting box FUN_0049ebc0(..,
        #        3, 3, 3, 0) at 0x4689B6; 0x71 {is_trade 1, 0x17} rewrites it ("Your target user
        #        does not exist in the server.") and hides windows 0x4B9 / 0x4B8 (2009
        #        FUN_0046a2e0 case 0x71, 0x46B32E).
        #   0x4D PetRename (window 0x4CB, cp-2 patched exe only): "Waiting for the server to
        #        respond." (0x52CF5C); the planned refusal is S2C 0x73 {0} (pet F9, C5), which
        #        closes box 0x16 first.
        # C2S 0x81 CashItemSaleReply is no row: it opens no waiting box (0x468D84 Send, return);
        # mall.Mall.sale_reply closes the seller's window 0x4B8 on a Cancel (reply 1) and leaves
        # the buyer's replies 0 / 2 - which need an S2C 0xA9 the server never sends - unanswered.
        0x4E: MustReply('dialog 0x4DA gets no feedback (no waiting box)', _reply('0xC4', result=0), '0xC4 {0}',
                        'premium_cash cash item options (ROADMAP_2009_ADDENDUM C8; mall.add_option)'),
        0x80: MustReply('window 0x4B8 waiting box', _reply('0x71', is_trade=1, result=0x17),
                        '0x71 {is_trade 1, 0x17}',
                        'premium_cash cash item sale (ROADMAP_2009_ADDENDUM C8; mall.sale_offer)'),
        0x4D: MustReply('modal', _reply('0x73', result=0), '0x73 {0}',
                        'pet rename (ROADMAP_2009_ADDENDUM C5; pets.PetStub.rename, P15 pet-s6)'),
    },
}


def _merged(base, overrides):
    """base itself when a build changes nothing (so a test patching MUST_REPLY/NEVER_REPLY
    still acts on the 2008 dispatch), else a fresh merged copy (a few dozen rows)."""
    if not overrides:
        return base
    table = dict(base)
    for op, row in overrides.items():
        if row is None:
            table.pop(op, None)
        else:
            table[op] = row
    return table


def must_reply_table(client_build=None):
    """MUST_REPLY of a client build (2008 = MUST_REPLY; later builds apply BUILD_MUST_REPLY)."""
    return _merged(MUST_REPLY, BUILD_MUST_REPLY.get(str(client_build or '2008')))


def never_reply_table(client_build=None):
    """NEVER_REPLY of a client build (2008 = NEVER_REPLY; later builds apply BUILD_NEVER_REPLY)."""
    return _merged(NEVER_REPLY, BUILD_NEVER_REPLY.get(str(client_build or '2008')))


# --------------------------------------------------------- request tracking ---
_REQUEST = threading.local()


class _Request:
    __slots__ = ('session', 'opcode', 'sent', 'deferred')

    def __init__(self, session, opcode):
        self.session = session
        self.opcode = opcode
        self.sent = []
        self.deferred = None


def defer_reply(why):
    """The request dispatched on this thread is answered by design with NO packet now: its
    answer comes later, from another session's request. The one case (trade-lock-commit,
    trade.md 2.7 step 3): the first of the two final confirms (C2S 0x25) waits in silence -
    window 0x291 keeps Cancel enabled - until the partner's confirm commits and sends the
    S2C 0x4A to both. A MUST_REPLY refusal there (0x49) would cancel a trade the partner is
    about to complete, and deciding it in the refusal function is racy: by then the partner's
    thread may already have committed and cleared the trade. The exception backstop still
    applies when the handler raises afterwards."""
    req = getattr(_REQUEST, 'current', None)
    if req is not None:
        req.deferred = str(why or 'deferred')


def note_send(session, opcode):
    """GameServer._send_encrypted calls this for every packet it sends. Marks the request
    dispatched on this thread as answered when the packet goes to that request's session."""
    req = getattr(_REQUEST, 'current', None)
    if req is not None and req.session is session:
        req.sent.append(opcode)


@contextlib.contextmanager
def detached():
    """Packets sent inside this block are server EVENTS the request caused, not replies to it:
    they neither answer a MUST_REPLY request nor break a NEVER_REPLY one. The case it exists
    for is C2S 0x0D: the rule is "never echo the mover's movement back" (the old S2C 0x0D echo
    desynced the cipher), but a 0x0D that reports a trap catch or a monster hit legitimately
    leads to S2C 0x3C / 0x29 / 0x21 / 0x28 / 0x3E (cs-traps, cs-player-death)."""
    outer = getattr(_REQUEST, 'current', None)
    _REQUEST.current = None
    try:
        yield
    finally:
        _REQUEST.current = outer


# ------------------------------------------------------------------ decoding ---
def decode(opcode, payload, client_build=None):
    """(packets.Record or None, error text or None). None + None: no C2S spec exists.
    client_build: the receiving server's build (its C2S grammars, client-2009-login)."""
    try:
        return P.parse(opcode, payload, client_build=client_build), None
    except KeyError:
        return None, None
    except P.ParseError as e:
        return None, str(e)
    except Exception as e:                              # noqa: BLE001 - a decoder bug must not drop the client
        return None, f'decoder error {type(e).__name__}: {e}'


def _fmt_value(v):
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).split(b'\x00', 1)[0].decode('latin-1')[:40]
    if isinstance(v, str):
        return v.split('\x00', 1)[0][:40]
    if isinstance(v, list):
        return f'[{len(v)} entries]'
    return v


def _neutral_name(names, count):
    """One name for several send sites that decoded the same bytes: their common name
    without the per-site '(...)' note, else their common prefix, else a plain count."""
    bases = {n.split(' (', 1)[0].strip() for n in names}
    if len(bases) == 1 and '' not in bases:
        return bases.pop()
    prefix = os.path.commonprefix(list(names)).rstrip(' (-_/,:')
    return prefix or f'one of {count} send sites'


def record_label(rec, client_build=None, *, with_key=True):
    """'<name> (<key>)' of a decoded C2S for the log (with_key=False: the name alone).
    packets.parse returns the FIRST send site whose grammar decodes the payload and lists
    every one that did in rec.candidates: same-grammar sites cannot be told apart by their
    bytes (livetest bug 12: the 2009 W-key pickup 0x43DA4E/0x1F was logged as
    "GroundItemPickupRequest (pet auto-loot)" because the pet's 0x42EA76/0x1F comes first in
    the spec). Such a record always gets a neutral name and every candidate key, so the log
    never names a site it cannot know."""
    if rec is None:
        return ''
    keys = tuple(rec.candidates or ()) or (rec.key,)
    if len(keys) <= 1:
        return f'{rec.name} ({rec.key})' if with_key else str(rec.name)
    names = []
    for key in keys:
        try:
            names.append(str(P.spec(key, 'C2S', client_build=client_build).get('name') or ''))
        except KeyError:
            names.append('')
    return f'{_neutral_name(names, len(keys))} ({" | ".join(keys)})'


def format_fields(rec):
    """Short one-line rendering of a decoded record for the log."""
    if rec is None:
        return ''
    text = ', '.join(f'{k}={_fmt_value(v)!r}' for k, v in rec.items())
    return text if len(text) <= 300 else text[:297] + '...'


# ------------------------------------------------------------------ dispatch ---
def check_routes(routes, cls):
    """Problems in a route table: handler names cls lacks, unknown styles."""
    problems = []
    for op, route in routes.items():
        if not isinstance(op, int) or not 0 <= op <= 0xFF:
            problems.append(f'{op!r}: opcode must be an int 0..255')
        if route.style not in (STYLE_RAW, STYLE_REC):
            problems.append(f'0x{op:02X}: unknown style {route.style!r}')
        if route.handler is not None and not callable(getattr(cls, route.handler, None)):
            problems.append(f'0x{op:02X}: {cls.__name__} has no handler {route.handler!r}')
        if route.handler is not None and route.log:
            problems.append(f'0x{op:02X}: a route has either a handler or a log line, not both')
    return problems


def dispatch(server, routes, sock, session, opcode, payload, no_enc=False):
    """Run one C2S packet through its route and apply the reply policy."""
    payload = bytes(payload)
    route = routes.get(opcode)
    rec, err = decode(opcode, payload, getattr(server, 'client_build', None))
    if err is not None:
        log.warning(f'[C2S] 0x{opcode:02X} {len(payload)}B matches no C2S grammar: '
                    f'{payload.hex(" ")} ({err[:200]})')
    elif rec is not None and route is not None and not route.quiet:
        log.debug(f'[C2S] 0x{opcode:02X} {record_label(rec, _build_of(server))} {format_fields(rec)}')

    dead = getattr(server, 'DEAD_C2S_KEYS', {}).get(_build_of(server) or '2008', {})
    if rec is not None and rec.key in dead:
        # A send site the client can never reach (room-host code compiled into the exe; its
        # sockets are never created). It shares an opcode with a real request (2009: 0x0C
        # shop sell / 0x0E create character), so the real handler must not see its fields,
        # and no refusal is owed: nothing was waiting for it.
        log.warning(f'[DISPATCH] C2S 0x{opcode:02X} {rec.key} {rec.name}: {dead[rec.key]}; '
                    f'from {session.get("username")!r} - dropped (modified client or framing error?)')
        return []

    req = _Request(session, opcode)
    outer = getattr(_REQUEST, 'current', None)
    _REQUEST.current = req
    failed = False
    try:
        if route is None:
            if rec is not None:
                name = record_label(rec, _build_of(server), with_key=False)
            else:
                name = '(no C2S spec)' if err is None else '(malformed)'
            log.info(f'[FIREWAY] Unhandled opcode 0x{opcode:02X} {name} {len(payload)}B: '
                     f'{payload.hex(" ") or "(empty)"}' + (f' {{{format_fields(rec)}}}' if rec else ''))
        elif route.handler is None:
            if route.log:
                log.info(route.log)
        elif route.style == STYLE_REC and rec is None:
            log.warning(f'[DISPATCH] 0x{opcode:02X} {route.handler} not called: payload does not decode')
        else:
            fn = getattr(server, route.handler)
            if route.style == STYLE_REC:
                rec.raw = payload
                fn(sock, session, rec, no_enc)
            else:
                fn(sock, session, payload, no_enc)
    except Exception:                                   # noqa: BLE001 - one bad handler must not drop the client
        failed = True
        log.exception(f'[DISPATCH] handler for C2S 0x{opcode:02X} raised')
    finally:
        _REQUEST.current = outer
        _apply_reply_policy(server, sock, session, opcode, route, rec, req.sent, failed, req.deferred)
    return req.sent


def _send_replies(server, sock, session, opcode, replies):
    sent = []
    for key, fields, assume in replies:
        try:
            if isinstance(fields, (bytes, bytearray)):
                server._send_encrypted(sock, session,
                                       P.opcode(key, client_build=getattr(server, 'client_build', None)),
                                       bytes(fields),
                                       use_by_array=session.get('no_enc', True))
            else:
                P.send(server, sock, session, key, fields, assume)
            sent.append(key)
        except Exception:                               # noqa: BLE001
            log.exception(f'[MUST-REPLY] sending refusal {key} for C2S 0x{opcode:02X} failed')
    return sent


def send_refusal(server, sock, session, opcode, rec=None):
    """Send the MUST_REPLY refusal for this request from a handler: interim stubs add their
    side effects and log line, while the reply bytes stay in the one MUST_REPLY table the
    exception fallback also uses. Returns the S2C keys sent ([] when this variant needs
    none)."""
    policy = must_reply_table(_build_of(server)).get(opcode)
    if policy is None or policy.refusal is None:
        raise KeyError(f'C2S 0x{opcode:02X} has no automatic refusal')
    return _send_replies(server, sock, session, opcode, policy.refusal(server, session, rec) or [])


def _apply_reply_policy(server, sock, session, opcode, route, rec, sent, failed, deferred=None):
    never = never_reply_table(_build_of(server))
    if opcode in never:
        if sent:
            log.error(f'[DISPATCH] policy violation: C2S 0x{opcode:02X} must never be answered '
                      f'({never[opcode]}) but the handler sent '
                      f'{", ".join(f"0x{o:02X}" for o in sent)}')
        return
    policy = must_reply_table(_build_of(server)).get(opcode)
    if policy is None or sent:
        return
    if deferred and not failed:
        log.debug(f'[MUST-REPLY] C2S 0x{opcode:02X} answered later by design: {deferred}')
        return
    if failed:
        how = 'handler raised'
    elif route is not None and route.handler is None and route.log:
        how = 'stub route'            # log-only route: the refusal is the designed answer
    else:
        how = 'no reply'
    if route is not None and not route.fallback:
        log.debug(f'[MUST-REPLY] C2S 0x{opcode:02X} {how}; refusal suppressed on this legacy route '
                  f'({route.note or policy.owner})')
        return
    if not session.get('username'):
        # Every must-reply request comes from a logged-in UI; answering a pre-login socket
        # would only feed a client that cannot have asked.
        log.warning(f'[MUST-REPLY] C2S 0x{opcode:02X} {how} before login; no refusal sent')
        return
    if policy.refusal is None:
        log.warning(f'[MUST-REPLY] C2S 0x{opcode:02X} {how} and has no automatic refusal yet '
                    f'({policy.text}; owner {policy.owner}): client left in: {policy.state}')
        return
    try:
        replies = policy.refusal(server, session, rec)
    except Exception:                                   # noqa: BLE001
        log.exception(f'[MUST-REPLY] building the refusal for C2S 0x{opcode:02X} failed')
        return
    if not replies:
        log.debug(f'[MUST-REPLY] C2S 0x{opcode:02X} {how}: this variant needs no refusal')
        return
    _send_replies(server, sock, session, opcode, replies)
    log.info(f'[MUST-REPLY] C2S 0x{opcode:02X} {how}: sent refusal {policy.text} '
             f'(full handler: {policy.owner})')
