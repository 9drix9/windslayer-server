#!/usr/bin/env python3
"""
social.py - the messenger's persisted model and pure rules (P6 stage 2)
======================================================================
social_friend-persistence plus the pure halves of social_friend-friend-list-sync / -friend-add
/ -presence-status / -mentor / -mentor-exp-share / -memos / -slot-expand,
chat_mail_gm-memo-delivery and chat_mail_gm-note-reply-9999 (docs/systems/social_friend.md
F1-F12, chat_mail_gm.md F6/F7). No sessions and no sockets here: messenger.py (the runtime
half: who is online, pending requests, rooms, the pushes) and the store's migration import
this module, so it must not import presence/records (records -> store -> social would be a
cycle at load).

    import social
    social.ensure(char)                         # friends, friend_capacity, mentor, mentees, memos
    social.ensure_account(acc)                  # account-wide `social` limits (P7 compliments)
    memo = social.add_memo(char, 'TestHero', b'hi')                 # {id, from, text, t}
    rows = social.memo_rows([memo], client_build='2009')            # S2C 0x78 records
    social.clamp_status(7) == 0                 # C2S 0x37: only 0 / 2 / 3

Persisted shape (character record; store migration, one-time accounts.json.bak-pre-p6)
-------------------------------------------------------------------------------------
    friends          ["Watcher", ...]   ordered canonical character names (<= 16 bytes), the
                                        DIRECTED list this character sees (social_friend 3.5).
                                        A name whose character is gone is dropped lazily on the
                                        next C2S 0x2F (F1 step 3)
    friend_capacity  20..50 step 5      S2C 0x0B / 0x9D; UI placeholder 2662 "(20/20)" = 20,
                                        C2S 0x73 +5 per Frenaiga purchase, UI 8991 "Maximum of 50"
    mentor           "Name" | null      this character's mentor (only a Novice registers one)
    mentees          ["Name", ...]      the characters that registered this one as mentor
    memos            [{id, from, text, t}]   <= 100, oldest dropped (chat_mail_gm F6.1);
                                        t = SYSTEMTIME [year, month, day_of_week (0 = Sunday),
                                        day, hour, minute, second, ms] in server local time
    memo_seq         int                the last memo id handed out: ids are never reused, so
                                        the C2S 0x44 delete guard (the ids THIS client was
                                        shown) can never delete a newer memo (chat_mail_gm F6.5)
Account (manner already exists since P1):
    social           {compliment_given_day, complimented_accounts{}, compliments_received
                      {day, count}, report_day} - the once-a-day / once-a-week limits of the
                      P7 compliment and report items (social_friend 3.5), created empty here
                      so P7 needs no schema step of its own.

Client constants (social_friend.md 1.5, both builds: spec_2009 0x0B / 0x0C / 0x60 / 0x7A..0x7E
/ 0x9D "identical"; only 0x78 changed, see memo_rows)
-------------------------------------------------------------------------------------
- Friend status byte (node +0x20, 2009 +0x1C): 0 online same channel (0x60 prints
  "<%s> has logged in. (Friend)", 2009 0x5441BC), 1 offline (grey, sorted last, no /f, no
  invite), 2 Busy / 3 AFK (C2S 0x37 dropdown), 4 another channel. Online rows carry the
  friend's channel number (never 0: /f skips channel 0).
- Mentor/mentee channel sentinels: 100 = "same channel" in 0x7A/0x7B/0x7D/0x7E (the client
  substitutes its own), 0x66 = offline in 0x7D and in the S2C 0x03 mentor block; the 0x03
  block compares the channel with its own channel_id, so an online mentor goes out with the
  real channel number there (2008 0x44E52F / 2009 FUN_00451960 case 3 read it after the
  messenger reset FUN_0046f860 / FUN_0047a1a0, so a re-sent 0x03 never duplicates it).
"""
import time

import chat as chatmod
import packets as P

# ---- friend list (social_friend 3.5 constants) ----
FRIEND_CAP_DEFAULT = 20        # UI 2662 "(20/20)"
FRIEND_CAP_MAX = 50            # client gate on C2S 0x73, UI 8991 "Maximum of 50."
FRIEND_SLOT_STEP = 5           # UI 8990 "+5 slots"
FRIEND_SLOT_PRICE = 50000      # the client's own gold gate before C2S 0x73 (config FRIEND_SLOT_PRICE)
LIST_MAX = 255                 # u8 counts (0x0B friend_count, 0x7E / 0x78 count)

# Friend status / presence byte (friend node +0x20; 2009 +0x1C).
STATUS_ONLINE = 0
STATUS_OFFLINE = 1
STATUS_BUSY = 2
STATUS_AFK = 3
MY_STATUSES = (STATUS_ONLINE, STATUS_BUSY, STATUS_AFK)    # C2S 0x37 dropdown: Online / Busy / AFK

# S2C 0x0C FriendAddResult. The success layout is taken for 1 and 0x0B only; any value
# outside this set pops a box built from an uninitialised stack buffer (F2 hazard).
ADD_OK = 1                     # to the requester: "<B> has been registered in your friend list."
ADD_NOT_ONLINE = 2             # "<name> is not on line."
ADD_REFUSED = 3                # "<B> has refused to register you as a friend."
ADD_FRIEND_OFF = 4             # "<B> has turned friend function off." (privacy 'friend')
ADD_THEIR_LIST_FULL = 5        # "<B> doesn't have a empty spot in friend list."
ADD_ALREADY = 6                # "<B> is already in your friend list."
ADD_MY_LIST_FULL = 7           # "There are no empty slots in your friend list."
ADD_OK_ACCEPTER = 0x0B         # to the accepter: same success layout and line
ADD_ERROR = 0x15               # "An error occurred while adding <name>." (unknown name)
ADD_RESULTS = frozenset({ADD_OK, ADD_NOT_ONLINE, ADD_REFUSED, ADD_FRIEND_OFF, ADD_THEIR_LIST_FULL,
                         ADD_ALREADY, ADD_MY_LIST_FULL, ADD_OK_ACCEPTER, ADD_ERROR})

# S2C 0x7A MentorRegisterResult (18 B for the two errors: no uid).
MENTOR_OK = 0x64               # = SAME_CHANNEL: "<M> is registered as your mentor." (+ u32 uid)
MENTOR_NOVICE = 0x65           # "<M> is a novice. Only a class-upgraded player can become a mentor."
MENTOR_NOT_FOUND = 0x66        # "<M> can not be found."
SAME_CHANNEL = 100             # 0x7A / 0x7B / 0x7D / 0x7E: the receiver's own channel
OFFLINE_CHANNEL = 0x66         # 0x7D and the 0x03 mentor block: offline

# ---- pending requests (social_friend F2 step 3, F5) ----
FRIEND_REQUEST_TTL = 120.0     # seconds a friend request can be answered
ROOM_INVITE_TTL = 60.0         # seconds a chat room invite can be answered

# ---- notes / memos (social_friend F9, chat_mail_gm F6/F7) ----
NOTE_ITEMS = frozenset({1894, 3320})      # "Note (x1)" / "Notes x11": using one opens window 0x3FD
GIFT_REPLY_NOTE_ID = 9999                 # the gift popup's thank-you reply (no item, no wait box)
MEMO_MAX = 100                            # per character; the oldest is dropped
# Memo body bytes. 2008 text is str[93]; the 2009 record's str[96] shares its shape with the
# 0x6D gift record whose item id sits at +0x6E, so the body must end (NUL) by +0x6D: 91
# chars. 90 keeps both builds and the note's own str[91] contents field NUL-terminated.
MEMO_TEXT_MAX = 90
NOTE_RESULT_FAILED = 0         # S2C 0x77: "Failed to send the message ."
NOTE_RESULT_SENT = 1           # S2C 0x77: "You successfully sent the message." (+ serial)

CHAR_FIELDS = ('friends', 'friend_capacity', 'mentor', 'mentees', 'memos', 'memo_seq')
NAME_MAX = chatmod.NAME_MAX


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def name_text(value):
    """A character name as stored and compared: NUL-cut, <= 16 bytes, str."""
    return chatmod.name_text(value) if value is not None else ''


def key(name):
    """Names are compared case-insensitively (the store's name index, lc-create uniqueness);
    the client compares the canonical spelling with strcmp, so packets always carry that."""
    return name_text(name).lower()


def _name_list(value, exclude=None):
    """Clean ordered unique names (first spelling kept) from whatever is stored."""
    out, seen = [], set()
    skip = key(exclude) if exclude else None
    for entry in value if isinstance(value, list) else []:
        if isinstance(entry, dict):
            entry = entry.get('name')
        if not isinstance(entry, (str, bytes)):
            continue
        name = name_text(entry)
        k = name.lower()
        if not name or k in seen or k == skip:
            continue
        seen.add(k)
        out.append(name)
    return out


def clamp_capacity(value):
    """20..50 in steps of 5 from the default (a hand-edited value is rounded down)."""
    cap = _int(value, FRIEND_CAP_DEFAULT)
    cap = max(FRIEND_CAP_DEFAULT, min(FRIEND_CAP_MAX, cap))
    return FRIEND_CAP_DEFAULT + (cap - FRIEND_CAP_DEFAULT) // FRIEND_SLOT_STEP * FRIEND_SLOT_STEP


def systemtime(now=None):
    """SYSTEMTIME [year, month, day_of_week (0 = Sunday), day, hour, minute, second, ms] of the
    server's local time - the eight u16 of an S2C 0x78 record (the renderer prints
    "%u/%u/%u %u:%u" from year, month, day, hour, minute; day_of_week is skipped)."""
    now = time.time() if now is None else float(now)
    t = time.localtime(now)
    return [t.tm_year, t.tm_mon, (t.tm_wday + 1) % 7, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec,
            int((now - int(now)) * 1000) % 1000]


def _memo(entry):
    """One stored memo, normalized, or None when it cannot be shown."""
    if not isinstance(entry, dict):
        return None
    memo_id = _int(entry.get('id'))
    sender = name_text(entry.get('from'))
    if memo_id <= 0 or not sender:
        return None
    text = P.cut_text(entry.get('text', ''), MEMO_TEXT_MAX).decode('cp949', 'replace')
    t = entry.get('t')
    t = [_int(v) & 0xFFFF for v in t][:8] if isinstance(t, list) else []
    t += [0] * (8 - len(t))
    return {'id': memo_id, 'from': sender, 'text': text, 't': t}


def ensure(char):
    """Create or repair the messenger fields of a character record in place (store migration
    and new_character; structural and idempotent: a conforming record is left untouched)."""
    if not isinstance(char, dict):
        raise TypeError('character record must be a dict')
    own = char.get('name')
    friends = _name_list(char.get('friends'), exclude=own)
    if char.get('friends') != friends:
        char['friends'] = friends
    cap = clamp_capacity(char.get('friend_capacity', FRIEND_CAP_DEFAULT))
    if char.get('friend_capacity') != cap:
        char['friend_capacity'] = cap
    mentor = name_text(char.get('mentor')) if isinstance(char.get('mentor'), (str, bytes)) else ''
    mentor = mentor if mentor and key(mentor) != key(own) else None
    if 'mentor' not in char or char.get('mentor') != mentor:
        char['mentor'] = mentor
    mentees = _name_list(char.get('mentees'), exclude=own)
    if char.get('mentees') != mentees:
        char['mentees'] = mentees
    memos, seen = [], set()
    for entry in char.get('memos') if isinstance(char.get('memos'), list) else []:
        memo = _memo(entry)
        if memo is not None and memo['id'] not in seen:
            seen.add(memo['id'])
            memos.append(memo)
    memos = memos[-MEMO_MAX:]
    if char.get('memos') != memos:
        char['memos'] = memos
    seq = max([_int(char.get('memo_seq'))] + [m['id'] for m in memos] + [0])
    if char.get('memo_seq') != seq:
        char['memo_seq'] = seq
    return char


def default_account_social():
    return {'compliment_given_day': None, 'complimented_accounts': {},
            'compliments_received': {'day': None, 'count': 0}, 'report_day': None}


def ensure_account(acc):
    """Create or repair the account's `social` limits (social_friend 3.5; P7 fills them)."""
    raw = acc.get('social')
    base = default_account_social()
    if isinstance(raw, dict):
        day = raw.get('compliment_given_day')
        base['compliment_given_day'] = day if isinstance(day, str) else None
        seen = raw.get('complimented_accounts')
        base['complimented_accounts'] = ({str(k): v for k, v in seen.items() if isinstance(v, str)}
                                         if isinstance(seen, dict) else {})
        got = raw.get('compliments_received')
        if isinstance(got, dict):
            day = got.get('day')
            base['compliments_received'] = {'day': day if isinstance(day, str) else None,
                                            'count': max(0, _int(got.get('count')))}
        day = raw.get('report_day')
        base['report_day'] = day if isinstance(day, str) else None
    if raw != base:
        acc['social'] = base
    return acc['social']


# ------------------------------------------------------------------ friends ---
def has_friend(char, name):
    return key(name) in {key(n) for n in (char or {}).get('friends') or []}


def friend_count(char):
    return len((char or {}).get('friends') or [])


def capacity(char):
    return clamp_capacity((char or {}).get('friend_capacity', FRIEND_CAP_DEFAULT))


def is_full(char):
    return friend_count(char) >= capacity(char)


def add_friend(char, name):
    """Append a canonical name to the directed list. True when it was not there yet."""
    if has_friend(char, name) or key(name) == key(char.get('name')):
        return False
    char.setdefault('friends', []).append(name_text(name))
    return True


def remove_friend(char, name):
    """Remove by name (case-insensitive). Returns the stored spelling, or None."""
    for i, stored in enumerate(char.get('friends') or []):
        if key(stored) == key(name):
            del char['friends'][i]
            return stored
    return None


def friend_row(name, channel, friend_id, status):
    """One S2C 0x0B / 0x0C row {name, channel, friend_id, status}."""
    return {'name': chatmod.name_bytes(name), 'channel': int(channel) & 0xFF,
            'friend_id': int(friend_id) & 0xFFFFFFFF, 'status': int(status) & 0xFF}


def add_result(result, name, row=None):
    """S2C 0x0C fields: the success layout (1 / 0x0B) with the other's row, else the 18 B
    failure layout. Only the codes the client has text for (ADD_RESULTS)."""
    if result not in ADD_RESULTS:
        raise ValueError(f'0x0C result {result!r} has no client text (only {sorted(ADD_RESULTS)})')
    if result in (ADD_OK, ADD_OK_ACCEPTER):
        if row is None:
            raise ValueError('a 0x0C success carries the new friend row')
        return {'result': result, **row}
    return {'result': result, 'target_name': chatmod.name_bytes(name)}


def clamp_status(value):
    """C2S 0x37: 0 / 2 / 3; anything else is Online (social_friend F4 step 3)."""
    value = _int(value)
    return value if value in MY_STATUSES else STATUS_ONLINE


# -------------------------------------------------------------------- memos ---
def add_memo(char, sender, text, now=None):
    """Store one memo on a character record (the caller holds the store lock and marks it
    dirty). Returns the memo, or None for an empty text (after the NUL cut)."""
    body = P.cut_text(text, MEMO_TEXT_MAX)
    if not body:
        return None
    seq = max(_int(char.get('memo_seq')), max([_int(m.get('id')) for m in char.get('memos') or []] + [0])) + 1
    memo = {'id': seq, 'from': name_text(sender), 'text': body.decode('cp949', 'replace'),
            't': systemtime(now)}
    memos = char.get('memos')
    if not isinstance(memos, list):
        memos = char['memos'] = []
    memos.append(memo)
    if len(memos) > MEMO_MAX:
        del memos[:len(memos) - MEMO_MAX]
    char['memo_seq'] = seq
    return memo


def memo_row(memo, client_build=None):
    """One S2C 0x78 record. 2009 (spec_2009 0x78 diff_vs_2008): 130 B - a new leading u8
    (no reader found: 0) and a 96-byte text; the body is clamped to MEMO_TEXT_MAX either way."""
    t = list(memo.get('t') or [])[:8]
    t += [0] * (8 - len(t))
    row = {'sender_name': chatmod.name_bytes(memo.get('from')),
           'text': P.cut_text(memo.get('text', ''), MEMO_TEXT_MAX)}
    for field, value in zip(('year', 'month', 'day_of_week', 'day', 'hour', 'minute', 'second',
                             'millisecond'), t):
        row[field] = int(value) & 0xFFFF
    if str(client_build) == '2009':
        row = {'unk_00': 0, **row}
    return row


def memo_packets(memos, client_build=None):
    """S2C 0x78 field sets for these memos, <= 255 records each. Empty = no packet at all:
    a count 0 still lights the Msg button (chat_mail_gm F6.2)."""
    rows = [memo_row(m, client_build) for m in memos]
    return [{'count': len(chunk), 'repeat[count]': chunk}
            for chunk in (rows[i:i + LIST_MAX] for i in range(0, len(rows), LIST_MAX))]


def delete_memos(char, ids):
    """Remove the memos whose id is in `ids` (C2S 0x44 guard). Returns how many went."""
    ids = set(ids or ())
    memos = char.get('memos') or []
    keep = [m for m in memos if _int(m.get('id')) not in ids]
    gone = len(memos) - len(keep)
    if gone:
        char['memos'] = keep
    return gone


def note_text(rec):
    """The body of a C2S 0x4B (Note contents or gift-reply message): NUL-cut, clamped."""
    return P.cut_text(rec.get('contents', rec.get('message', b'')), MEMO_TEXT_MAX)


def note_item(rec):
    return _int(rec.get('item_id', rec.get('note_item_id', 0)))
