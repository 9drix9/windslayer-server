#!/usr/bin/env python3
"""
quests.py - persistent quest state, exactly as the client keeps it
(quest_cards_misc-quest-state-model)
==================================================================
The client is the authoritative bookkeeper for quests (quest_cards_misc.md 1.3): it writes
its own 3 active slots, progress bytes and completed list from S2C 0x26/0x27/0x38/0x59 and
re-reads them from S2C 0x03 on every map load. This module is the server-side mirror of
that state, stored in the character record so a portal or a relog cannot lose it (Q-B8:
today the log is wiped by every 0x03, and the server state only lived on the session).

    import quests as Q
    st = Q.QuestState(char)
    st.accept(26)            # -> slot index 0 (client slot 1), or None when the log is full
    st.slot_of(26)           # 0
    st.set_progress(0, 1)    # the ReqPro kill counter of that slot
    st.complete(26)          # clears the slot and appends/increments the completed list
    st.times(26)             # 1
    st.exhausted(26, qdef)   # True: Repeat 0 means once ever (FUN_00477FF0)

Persisted shape (roadmap F4 "character / quests", quest_cards_misc.md 3.2)
--------------------------------------------------------------------------
    char['quests'] = {'active':    [26, 0, 0],       # u16 x3, index = client slot - 1
                      'progress':  [1, 0, 0],        # u8 x3, the ReqPro kill counter
                      'completed': [[26, 1], ...]}   # ordered [id, times <= 99], <= 85
    char['card_deck'] = [2030, 2031]                 # ordered, unique, <= 51 card ids

Invariants (3.2): an id is never both active and exhausted-completed, and `active[i] == 0`
implies `progress[i] == 0`. The completed list follows FUN_004264A0 exactly: an existing id
is incremented (capped at 99), else the first free entry is filled, and when all 85 are
used the list shifts left by one and the id is appended.

Item demands are NOT tracked here. The client re-checks them against the bag on every
S2C 0x59 (FUN_00426500), so the bag model is the single truth for them; the progress byte
is only the kill counter (Q-B10: the old code wrote collected item counts into it).
"""
import logging

log = logging.getLogger('WS')

MAX_ACTIVE = 3                # client active log: 3 slots (scene+0x29E / +0x2A4)
MAX_COMPLETED = 85            # completed ids u16[85] at +0x262
MAX_COMPLETED_TIMES = 99      # the times byte saturates
MAX_PROGRESS = 0xFF           # u8 progress byte
MAX_DECK = 51                 # card deck capacity: u16 slots before the manner i32 (2008 +0xE98)


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _apply(data, normalized):
    """Write `normalized` into the live dict, and only when it differs (inventory._apply:
    never empty it, so a reader on another thread cannot find a missing key)."""
    if data == normalized:
        return False
    data.update(normalized)
    for key in [k for k in list(data) if k not in normalized]:
        del data[key]
    return True


def ensure(char):
    """Normalize (and create) the quest fields of a character record in place. Idempotent:
    the store migration and every QuestState() call run it.

    Like inventory.ensure it keeps the identity of char['quests'] and char['card_deck']: a
    QuestState built earlier holds them, and replacing the dict would orphan it. And like
    it, a conforming record is left completely untouched and every container it reads is
    copied with one C call first: the unconditional clear() + update() rewrote live nested
    dicts of a character another thread was playing, and the store's save was serializing
    them ("dictionary changed size during iteration")."""
    if not isinstance(char, dict):
        raise TypeError('character record must be a dict')
    data = char.get('quests')
    if not isinstance(data, dict):
        data = char['quests'] = {}
    active = [_int(v) & 0xFFFF for v in list(data.get('active') or [])][:MAX_ACTIVE]
    active += [0] * (MAX_ACTIVE - len(active))
    progress = [max(0, min(MAX_PROGRESS, _int(v)))
                for v in list(data.get('progress') or [])][:MAX_ACTIVE]
    progress += [0] * (MAX_ACTIVE - len(progress))
    # An empty slot can never carry progress (3.2 invariant).
    progress = [p if active[i] else 0 for i, p in enumerate(progress)]
    completed, seen = [], set()
    for entry in list(data.get('completed') or []):
        if isinstance(entry, dict):
            quest_id, times = _int(entry.get('id')), _int(entry.get('times'), 1)
        elif isinstance(entry, (list, tuple)) and entry:
            quest_id, times = _int(entry[0]), _int(entry[1], 1) if len(entry) > 1 else 1
        else:
            quest_id, times = _int(entry), 1
        if quest_id <= 0 or quest_id in seen:
            continue
        seen.add(quest_id)
        completed.append([quest_id & 0xFFFF, max(1, min(MAX_COMPLETED_TIMES, times))])
    _apply(data, {'active': active, 'progress': progress,
                  'completed': completed[:MAX_COMPLETED]})
    deck = char.get('card_deck')
    if not isinstance(deck, list):
        deck = char['card_deck'] = []
    cards, seen_cards = [], set()
    for card in list(deck):
        card = _int(card)
        if card > 0 and card not in seen_cards:
            seen_cards.add(card)
            cards.append(card)
    cards = cards[:MAX_DECK]
    if deck != cards:
        deck[:] = cards
    return data


class QuestState:
    """The 3 active slots, their progress bytes and the completed list of one character.
    Slots are indexes 0..2 here; the wire (S2C 0x59) counts them from 1."""

    def __init__(self, char):
        self.data = ensure(char)
        self.char = char

    # ----------------------------------------------------------------- read ---
    @property
    def active(self):
        return self.data['active']

    @property
    def progress(self):
        return self.data['progress']

    @property
    def completed(self):
        return self.data['completed']

    def slot_of(self, quest_id):
        """The slot index holding this quest, or None."""
        quest_id = _int(quest_id)
        for i, held in enumerate(self.active):
            if held and held == quest_id:
                return i
        return None

    def is_active(self, quest_id):
        return self.slot_of(quest_id) is not None

    def first_free_slot(self):
        """The first empty slot, which is the slot the client itself would use for the next
        S2C 0x26 (1.3: "it writes the first empty slot"). None when the log is full."""
        for i, held in enumerate(self.active):
            if not held:
                return i
        return None

    def active_ids(self):
        return [q for q in self.active if q]

    def times(self, quest_id):
        """How often this quest has been completed (0 when never)."""
        quest_id = _int(quest_id)
        for entry in self.completed:
            if entry[0] == quest_id:
                return entry[1]
        return 0

    def exhausted(self, quest_id, quest_def=None):
        """The client's FUN_00477FF0 "already done" rule: completed at least once AND
        (Repeat == 0 or times >= Repeat). A repeatable quest (152 of the 291 EN quests)
        comes back once its Repeat count is not yet reached (Q-B4)."""
        times = self.times(quest_id)
        if not times:
            return False
        repeat = _int(getattr(quest_def, 'repeat', 0))
        return repeat == 0 or times >= repeat

    # ------------------------------------------------------------- mutation ---
    def accept(self, quest_id, needs_slot=True):
        """Take a quest. Returns the slot index, or -1 for a talk-only quest (which the
        client files straight into the completed list and gives no slot, Q-B3), or None
        when the log is full or the quest is already held."""
        quest_id = _int(quest_id)
        if quest_id <= 0 or self.is_active(quest_id):
            return None
        if not needs_slot:
            self.complete_append(quest_id)
            return -1
        slot = self.first_free_slot()
        if slot is None:
            return None
        self.active[slot] = quest_id & 0xFFFF
        self.progress[slot] = 0
        return slot

    def abandon(self, quest_id):
        """Drop a held quest (C2S 0x1E -> S2C 0x38 zeroes the slot and its progress).
        Returns the freed slot index, or None when it was not held."""
        slot = self.slot_of(quest_id)
        if slot is None:
            return None
        self.active[slot] = 0
        self.progress[slot] = 0
        return slot

    def set_progress(self, slot, value):
        """Write a slot's ReqPro counter (the value S2C 0x59 carries)."""
        slot = _int(slot, -1)
        if not 0 <= slot < MAX_ACTIVE or not self.active[slot]:
            return None
        self.progress[slot] = max(0, min(MAX_PROGRESS, _int(value)))
        return self.progress[slot]

    def credit_kill(self, npccode, catalog):
        """One kill of `npccode` against the held ReqPro quests, exactly as the client's own
        P2P path does (FUN_0041A230, quest_cards_misc F3): the FIRST slot whose quest wants
        this monster and is not yet full is incremented, and nothing else. Returns
        (slot, new progress) or None."""
        for slot, quest_id in enumerate(self.active):
            if not quest_id:
                continue
            q = catalog.get(quest_id)
            if q is None or not q.reqpro or q.npc != npccode:
                continue
            if self.progress[slot] >= q.reqpro:
                continue
            self.progress[slot] = min(MAX_PROGRESS, self.progress[slot] + 1)
            return slot, self.progress[slot]
        return None

    def complete_append(self, quest_id):
        """FUN_004264A0: increment an existing entry (capped at 99), else fill the first
        free entry with times 1, else shift the list left by one and append. Returns the
        new times count."""
        quest_id = _int(quest_id) & 0xFFFF
        for entry in self.completed:
            if entry[0] == quest_id:
                entry[1] = min(MAX_COMPLETED_TIMES, entry[1] + 1)
                return entry[1]
        if len(self.completed) >= MAX_COMPLETED:
            del self.completed[0]
        self.completed.append([quest_id, 1])
        return 1

    def complete(self, quest_id):
        """Turn-in bookkeeping: free the slot (if it used one) and append/increment the
        completed list. Returns (slot or None, times)."""
        slot = self.abandon(quest_id)
        return slot, self.complete_append(quest_id)

    # ------------------------------------------------------------ card deck ---
    @property
    def deck(self):
        return self.char['card_deck']

    def has_card(self, card_id):
        return _int(card_id) in self.deck

    def add_card(self, card_id):
        """Register a card. Returns the new deck size, or None when it is already in the
        deck or the deck is full (S2C 0x8B result 6 / a refusal)."""
        card_id = _int(card_id)
        if card_id <= 0 or self.has_card(card_id) or len(self.deck) >= MAX_DECK:
            return None
        self.deck.append(card_id)
        return len(self.deck)


# --------------------------------------------------- wire fields (0x03/0x8A/0x59) ---
# quest_cards_misc-03-quest-card-fields: the quest and card half of S2C 0x03, plus the two
# packets that re-arm the client after it (F7). The world group's 0x03 builder merges
# fields_for_03() into its own record; nothing here builds a packet by itself.
def fields_for_03(char):
    """The 0x03 quest/card fields of one character (quest_cards_misc.md F7 step 1).

    The grammar has two `repeat(3)` blocks - the active ids and their progress bytes - and
    packets.build keys BOTH of them 'repeat[3]', so one row carries both fields: the
    encoder reads `active_quest_id` from the rows for the first block and
    `active_quest_progress` from the same rows for the second.

    `unk_e78` is the card deck count: S2C 0x8A writes charinfo+0xE30 = scene+0xE78, which
    is the byte this field lands in (quest doc 1.4), so the window-8 label is right before
    the 0x63 round trip even happens."""
    state = QuestState(char)
    rows = [{'active_quest_id': qid, 'active_quest_progress': state.progress[i]}
            for i, qid in enumerate(state.active)]
    # <= 85 entries: the client reads them into u16[85] at scene+0x2AA with no bound check,
    # and anything past it corrupts the gold/quest state that follows (spec 0x03 hazards).
    done = [{'completed_quest_id': qid, 'completed_quest_times': times}
            for qid, times in state.completed[:MAX_COMPLETED]]
    return {f'repeat[{MAX_ACTIVE}]': rows,
            'completed_quest_count': len(done),
            'repeat[completed_quest_count]': done,
            'unk_e78': len(state.deck) & 0xFF}


def deck_fields(char):
    """S2C 0x8A CardDeckList fields (F9): the whole deck, in registration order."""
    deck = QuestState(char).deck[:MAX_DECK]
    return {'deck_count': len(deck), 'repeat[deck_count]': [{'card_item_id': c} for c in deck]}


def progress_rows(char):
    """[(wire slot 1..3, progress)] for every non-empty slot, for the S2C 0x59 burst that
    re-arms the client's ready flags after a 0x03 (F7 step 3.2). S2C 0x03 zeroes those
    flags (scene+0x2A7..0x2A9) and does not carry them, so without this the green "ready to
    complete" state is lost on every portal and relog."""
    state = QuestState(char)
    return [(i + 1, state.progress[i]) for i, qid in enumerate(state.active) if qid]
