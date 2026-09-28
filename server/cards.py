#!/usr/bin/env python3
"""
cards.py - Monster Card registration and the card EXP bonus (P4 stage 4:
quest_cards_misc-card-register, quest_cards_misc-card-exp-bonus)
=====================================================================================
quest_cards_misc.md 1.4 / F9 / F10 / F11. The deck itself is quests.QuestState's
`char['card_deck']` (ordered, unique, <= 51 ids, persisted by store.py and listed in S2C
0x8A after every C2S 0x63); this module decides a C2S 0x64 register request and the kill
bonus. Nothing here sends a packet or touches a session: GameServer._handle_card_register
sends the one S2C 0x8B, persists and logs; GameServer._kill_monster asks bonus_exp().

Client flow (live quest_cards_misc#07 / #13 / #14, 2008; the 2009 C2S 0x64 and S2C 0x8A /
0x8B bytes are "identical", spec_2009 0x45FB50/0x64, 0x8A, 0x8B - 2009 SubHandler5
FUN_0045fb80, deck at charinfo+0xE48 instead of +0xE30):

    drag a card from the bag (Miscellanies) onto the Card Deck window (hotkey B)
      -> client gates: hii Type 2 with the card flag, not already in its own list
      -> window 0x292 "Once the card is registered ..." OK
      -> C2S 0x64 {u16 card_item_id} + the waiting box "Waiting for the server to response."
      <- S2C 0x8B {result [, new_deck_count, card_item_id]}  closes the box for EVERY result

    result 1   the CLIENT removes one card from its own bag (FUN_00470f00), stores the id
               at slot new_deck_count and prints "Registered %s in the Card Deck."
    result 2   "Failed to receive dictionary information. Please log in again."
    result 6   "Already registered."
    other      "Failure to register card. Please try again in a few minutes."

So the server mirrors exactly the client's success change (one card out of the bag model,
id appended to the deck) and sends NO 0x23/0x19 for the card and no fresh 0x8A (F10 step 5:
a removal packet would take a second card). new_deck_count must be old + 1 and >= 1 (the
client writes the id at charinfo+0xE30 + 2*count with no bounds check); the 51 cap keeps it
off the i32 manner field at +0xE98 (2009: +0xE4A..+0xEB0, the same 51 u16 slots).

The label total is 880 in 2008 / 900 in 2009 (items with CardNpc != 0, live #13), but only
the 64 hii Type 2 records with CardSpr != 0 are registrable Monster Cards (en_content
CardCatalog): the client's +0x14C card flag gate. `card_npc` is the monster's hni npccode,
which is what the EXP bonus keys on.
"""
import logging
from dataclasses import dataclass

import en_content as EC
import inventory as INV
import quests as Q

log = logging.getLogger('WS')

# S2C 0x8B result bytes (quest_cards_misc.md F10; live #14).
RESULT_REGISTERED = 1
RESULT_NO_DECK = 2            # "Failed to receive dictionary information. Please log in again."
RESULT_FAILED = 3             # any value but 1/2/6: "Failure to register card. ..."
RESULT_DUPLICATE = 6          # "Already registered."

# "Card effect: EXP + 10%" (the tooltip FUN_00485F40 prints on every Monster Card, E4).
EXP_BONUS_PCT = 10


@dataclass(frozen=True)
class RegisterOutcome:
    result: int
    card_id: int
    deck_count: int           # the deck size after the request (the 0x8B new_deck_count on 1)
    why: str

    @property
    def registered(self):
        return self.result == RESULT_REGISTERED


def register(char, card_id, catalog=None):
    """Decide one C2S 0x64 and, on success, apply the client's own change to the record:
    one card out of the bag model and the id appended to the deck. The order is F10 step 3:
    no deck -> 2; not a Monster Card -> 3; already in the deck -> 6; deck full (51) -> 3;
    no card in the bag model -> 3 (the client would still remove one of its own if it had
    it, so the two bags agree only if the server refuses here); else 1."""
    card_id = int(card_id or 0) & 0xFFFF
    if char is None:
        return RegisterOutcome(RESULT_NO_DECK, card_id, 0, 'no character: the deck is not loaded')
    state = Q.QuestState(char)
    count = len(state.deck)
    catalog = catalog if catalog is not None else EC.cards()
    card = catalog.get(card_id)
    if card is None:
        return RegisterOutcome(RESULT_FAILED, card_id, count,
                               f'{card_id} is not a Monster Card (hii Type 2 with CardSpr)')
    if state.has_card(card_id):
        return RegisterOutcome(RESULT_DUPLICATE, card_id, count, 'already in the deck')
    if count >= Q.MAX_DECK:
        return RegisterOutcome(RESULT_FAILED, card_id, count,
                               f'the deck is full ({count}/{Q.MAX_DECK}: slot {count + 1} would '
                               f'overwrite the manner field)')
    bag = INV.Inventory(char)
    if not bag.has(card_id):
        return RegisterOutcome(RESULT_FAILED, card_id, count,
                               'the card is not in the bag model (client and server bags disagree)')
    bag.remove(card_id, 1)
    new_count = state.add_card(card_id)
    return RegisterOutcome(RESULT_REGISTERED, card_id, new_count,
                           f'registered in slot {new_count} (monster npccode {card.card_npc})')


def result_fields(outcome):
    """S2C 0x8B fields: {result} alone for a refusal (1 B), {1, count, id} on success (4 B)."""
    if not outcome.registered:
        return {'result': outcome.result}
    return {'result': RESULT_REGISTERED, 'new_deck_count': outcome.deck_count & 0xFF,
            'card_item_id': outcome.card_id}


def card_for_npc(char, npccode, catalog=None):
    """The registered card that belongs to this monster npccode, or None."""
    if char is None or not npccode:
        return None
    catalog = catalog if catalog is not None else EC.cards()
    deck = Q.QuestState(char).deck
    for card_id in catalog.for_npc(int(npccode)):
        if card_id in deck:
            return card_id
    return None


def bonus_exp(char, npccode, exp, catalog=None):
    """(exp, card) for a kill of `npccode` (F11, quest_cards_misc-card-exp-bonus): +10% when
    the monster's card is registered, else (exp, None).

    Rounding: 10% floored, but never less than 1 point. The design's plain floor(exp * 1.1)
    gives NOTHING for every monster under 10 exp (the 2008 Pupu has 5, so P4 exit criterion
    6 could never show the bonus there), while the tooltip promises one on every card. From
    10 exp up the two rules agree (2009 Ssiyo 10 -> 11). Whether retail applied the bonus at
    all is unproven (Q2), hence config CARD_EXP_BONUS."""
    exp = int(exp or 0)
    if exp <= 0:
        return exp, None
    card = card_for_npc(char, npccode, catalog)
    if card is None:
        return exp, None
    return exp + max(1, exp * EXP_BONUS_PCT // 100), card
