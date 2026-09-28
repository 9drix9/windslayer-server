#!/usr/bin/env python3
"""
chat.py - whisper, friend chat and the messenger room builders (P6 stage 1)
===========================================================================
chat_mail_gm-whisper (F2), chat_mail_gm-friend-chat-relay (F3) and
chat_mail_gm-room-builders (F5, the display half social_friend F5 drives). This module holds
the pure rules - field sets, clamps, name order, the anti-spoof - so the GameServer
handlers only resolve sessions and push; packets.build does the byte work with the build's
own spec (every packet here has the same wire format in 2008 and 2009: spec_2009 0x09 /
0x0A / 0x0F / 0x61 / 0x91 and C2S 0x4475C7/0x02, 0x47ACE9/0x6B "identical").

    import chat
    target, text = chat.whisper_request(rec)                   # C2S 0x02
    fields = chat.whisper_received('TestHero', text)           # S2C 0x0A to the target
    fields = chat.whisper_echo('Watcher', text)                # S2C 0x09 to the sender
    fields = chat.whisper_failed(chat.WHISPER_NOT_FOUND, name) # S2C 0x09 18 B
    fields = chat.room_joined(7, ['TestHero', 'Watcher'], receiver='Watcher')   # S2C 0x0F
    fields, assume = chat.room_line('TestHero', b'hi')         # S2C 0x61
    line = chat.friend_line('TestHero', b'TestHero : hi')      # S2C 0x91 text

Whisper (chat_mail_gm F2; spec 0x09 / 0x0A)
-------------------------------------------
- The client prints nothing of its own whisper: the sender sees "<To: name> msg" only from
  S2C 0x09 status 0x65, the target "<From: name> msg" from S2C 0x0A channel 0x65. Any other
  status/channel value is printed as "[Channel-N]" - a single-channel server always uses
  0x65 (WHISPER_OK).
- 0x09 status 0x66 "<name>can not be found." (offline, unknown, at character select) and
  0x67 "<name>is rejecting whispers." carry no message (18 B).
- Text clamp (0x09 / 0x0A sprintf_s into 88-byte buffers, spec gates): the same text goes
  to both, so it is cut to min(60, 78 - len(sender), 80 - len(target)) bytes.

Friend chat (chat_mail_gm F3; spec 0x46FE14/0x6B, 2009 0x47ACE9/0x6B)
---------------------------------------------------------------------
The client prints its own green line and sends "<own name> : <text>" with the (channel,
friend_id) of every online friend in ITS list. The server trusts neither: recipients are
the in-world sessions of those uids whose character is in the sender's STORED friend list
(social_friend 3.5 `char['friends']`), the line is re-prefixed with the real name if it
does not start with it (anti-spoof), clamped to 87 (S2C 0x91 88-byte buffer), and nothing
goes back to the sender (registry.NEVER_REPLY 0x6B: an echo would print the line twice).

Messenger room display (chat_mail_gm F5; spec 0x0F / 0x61; spec correction C52)
-------------------------------------------------------------------------------
- 0x0F subtype 1 {room_id, member_count, names}: the list INCLUDES the receiver, and with
  1 or 2 names the client prints "<last name> has entered.", so the receiver goes first and
  the other member last (C52, live chat_mail_gm#07). member_count 0 stores room_id only (no
  window, no rows): the T-0F-restore form. Nodes are not zeroed and not de-duplicated, so
  every name is NUL-terminated within 17 bytes (packets.build cuts str[17] to 16) and a full
  list is only sent to a member that has no rows yet.
- The failure subtypes are modal popups: only 2..6 and 8 have text (0, 7, 9+ print an
  uninitialised buffer). 5 ("room full") ignores the name (C52).
- 0x61 is read only while the receiver's room id (+0xCC, 2009 mgr+0x180) is non-zero, so it
  is built with that assumption and must only go to room members; text <= 58 (sprintf_s
  "  %s" into 61 bytes).
"""
import packets as P

# ---- whisper (spec 0x09 status / 0x0A channel) ----
WHISPER_OK = 0x65              # delivered; printed without a channel tag
WHISPER_NOT_FOUND = 0x66       # "<name>can not be found."
WHISPER_REFUSED = 0x67         # "<name>is rejecting whispers."
WHISPER_TEXT_MAX = 60          # 61-byte pre-zeroed message buffers
# chat_mail_gm F2 step 3.4: at most 10 whispers per 5 s per session (a patched client
# flooding a player); the real client does not rate-limit whispers at all (F1.1 step 7).
WHISPER_RATE_COUNT = 10
WHISPER_RATE_SECS = 5.0
# F2 step 4: the target is the sender (the client blocks its own exact name; a case variant
# reaches the server). S2C 0x15 type 2 "[Warning] ..." (red).
SELF_WHISPER_TEXT = "You can't send a message to yourself."

# ---- friend chat ----
FRIEND_LINE_MAX = 87           # S2C 0x91 88-byte zeroed buffer
FRIEND_LINE_SEP = b' : '       # the client's "%s : %s" (player name + typed text)

# ---- messenger rooms ----
ROOM_JOINED = 1
ROOM_OTHER_CHANNEL = 2         # "<name>is in other channel. You can't have a personal chat."
ROOM_REFUSED = 3               # "<name>has refused to have a personal chat."
ROOM_REJECTING = 4             # "<name>is rejecting chatting." (privacy flag 'talk')
ROOM_FULL = 5                  # "You cannot invite more players. The chatting room is full."
ROOM_BUSY = 6                  # "<name> is in conversation with other player right now."
ROOM_NOT_ONLINE = 8            # "<name>is not on line." (+ marks the friend offline)
ROOM_REFUSALS = frozenset({ROOM_OTHER_CHANNEL, ROOM_REFUSED, ROOM_REJECTING, ROOM_FULL,
                           ROOM_BUSY, ROOM_NOT_ONLINE})
ROOM_MAX = 10                  # members; the client's +0xC8 check (social_friend 3.5)
ROOM_TEXT_MAX = 58
ROOM_LINE_ASSUME = {'client.messenger_room_id != 0': True}

NAME_MAX = 16


def name_bytes(name):
    """A character name as the client holds it: NUL-cut, at most 16 bytes (str[17])."""
    return P.cut_text(name, NAME_MAX)


def name_text(name):
    """The same name as text for lookups and logs (names are [A-Za-z0-9], names.py)."""
    return name_bytes(name).decode('cp949', 'replace')


# ------------------------------------------------------------------- whisper ---
def whisper_request(rec):
    """(target name bytes, message bytes) of a decoded C2S 0x02 WhisperChat: NUL-cut (the
    whisper-tab path can send an all-zero name, F2 step 1 quirk 2)."""
    target = name_bytes(rec.get('target_name', b''))
    text = P.to_bytes(rec.get('message', b'')).split(b'\x00', 1)[0]
    return target, text


def whisper_limit(sender_name, target_name):
    """Bytes of whisper text both S2C 0x0A (sender name + text <= 78) and 0x09 status 0x65
    (target name + text <= 80) can print (chat_mail_gm F2 step 5)."""
    return max(0, min(WHISPER_TEXT_MAX, 78 - len(name_bytes(sender_name)),
                      80 - len(name_bytes(target_name))))


def whisper_text(sender_name, target_name, text):
    """The whisper text clamped for both packets (cp949-safe cut)."""
    return P.cut_text(text, whisper_limit(sender_name, target_name))


def whisper_received(sender_name, text):
    """S2C 0x0A to the target: "<From: sender> text" (channel 0x65: no channel tag)."""
    text = P.to_bytes(text)
    return {'channel': WHISPER_OK, 'sender_name': name_bytes(sender_name),
            'msg_len': len(text), 'message': text}


def whisper_echo(target_name, text):
    """S2C 0x09 status 0x65 to the sender: "<To: target> text" (the target's canonical
    name, not the case the sender typed)."""
    text = P.to_bytes(text)
    return {'status': WHISPER_OK, 'target_name': name_bytes(target_name),
            'msg_len': len(text), 'message': text}


def whisper_failed(status, target_name):
    """S2C 0x09 0x66 / 0x67 (18 B: the grammar stops after the name)."""
    if status not in (WHISPER_NOT_FOUND, WHISPER_REFUSED):
        raise ValueError(f'whisper failure status must be 0x66 or 0x67, not {status!r}')
    return {'status': status, 'target_name': name_bytes(target_name)}


# --------------------------------------------------------------- friend chat ---
def stored_friends(char):
    """Lower-case names of the character's stored friend list (social_friend 3.5
    `char['friends']`: ordered names, social.py since P6 stage 2; a {name: ...} entry is read
    by its name)."""
    out = set()
    for entry in (char or {}).get('friends') or []:
        name = entry.get('name') if isinstance(entry, dict) else entry
        if name:
            out.add(name_text(name).lower())
    return out


def friend_line(sender_name, message):
    """The S2C 0x91 text for a C2S 0x6B message: "<real name> : <text>", clamped to 87.
    A message that does not start with "<sender> : " (a patched client, or a spoofed name)
    has everything before its first " : " replaced by the real name, or the prefix added
    when it has none (chat_mail_gm F3 step 3.2)."""
    name = name_bytes(sender_name)
    text = P.to_bytes(message).split(b'\x00', 1)[0]
    prefix = name + FRIEND_LINE_SEP
    if not text.startswith(prefix):
        head, sep, rest = text.partition(FRIEND_LINE_SEP)
        text = prefix + (rest if sep else text)
    return P.cut_text(text, FRIEND_LINE_MAX)


def friend_ids(rec):
    """The distinct friend_id values of a decoded C2S 0x6B, in the client's order."""
    seen = []
    for entry in rec.get('repeat[recipient_count]') or []:
        uid = int(entry.get('friend_id', 0)) if isinstance(entry, dict) else 0
        if uid and uid not in seen:
            seen.append(uid)
    return seen


# ----------------------------------------------------------- messenger rooms ---
def room_joined(room_id, names, receiver=None):
    """S2C 0x0F subtype 1 fields. `names` are the room members; with `receiver` given it is
    moved to the front, so a two-member room ends with the OTHER name and the client prints
    "<other> has entered." (C52). No names = the restore form (room id only)."""
    room_id = int(room_id)
    if not 0 < room_id <= 0xFFFFFFFF:
        raise ValueError(f'room id must be a non-zero u32 (0 means "no room"), not {room_id}')
    members = [name_bytes(n) for n in names]
    if len(members) > ROOM_MAX:
        raise ValueError(f'{len(members)} room members: the client room holds {ROOM_MAX}')
    if any(not n for n in members):
        raise ValueError('an empty member name would print " has entered."')
    if receiver is not None:
        me = name_bytes(receiver)
        if me in members:
            members.remove(me)
            members.insert(0, me)
    return {'subtype': ROOM_JOINED, 'room_id': room_id, 'member_count': len(members),
            'repeat[member_count]': [{'member_name': n} for n in members]}


def room_refused(subtype, name=b''):
    """S2C 0x0F failure popup fields (subtypes 2..6 and 8 only)."""
    if subtype not in ROOM_REFUSALS:
        raise ValueError(f'0x0F subtype {subtype!r} has no client text (only {sorted(ROOM_REFUSALS)})')
    return {'subtype': int(subtype), 'target_name': name_bytes(name)}


def room_line(sender_name, text):
    """(fields, assume) of S2C 0x61: the room line "<sender> : " + "  text" (text cut at NUL
    and clamped to 58). Only for receivers in a room (the assume)."""
    text = P.cut_text(text, ROOM_TEXT_MAX)
    return ({'sender_name': name_bytes(sender_name), 'msg_len': len(text), 'message': text},
            dict(ROOM_LINE_ASSUME))
