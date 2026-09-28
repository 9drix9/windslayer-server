#!/usr/bin/env python3
"""
privacy.py - the five refuse flags (chat_mail_gm-privacy-flags; aliases party-privacy,
trade-privacy-flags, quest_cards_misc-privacy-flags; roadmap D13)
=========================================================================================
The Options window's five "refuse" checkboxes reach the server twice, with the same five
u8 in the same order in both client builds:
  - C2S 0x2B EnterWorld bytes 0-4 (2008 0x42F904/0x2B, 2009 0x4315D7/0x2B and the relogin
    sender 0x452133/0x2B): the values the client read from its registry key
    HKCU\\Software\\Hamelin\\WindSlayer\\<character>;
  - C2S 0x40 SetPrivacyRefuseOptions (2008 0x4414BA/0x40, 2009 0x44206A/0x40 "identical"),
    sent when the player applies the Options window. Never answered (registry.NEVER_REPLY).
Wire order = checkbox control ids 0x10..0x14: whisper, exchange, party, talk, friend (talk
comes before friend although COption keeps friend first in memory, spec 0x4414BA/0x40).

The server keeps them in session['refuse'] (the live values: the client is the owner) and
in the character record `refuse` (F4 schema, chat_mail_gm-privacy-flags), so a check
against a character that is offline or between connections still has an answer.

Consumers ask refuses(target, kind) and never read the packets again (D13). The replies
belong to the consumer items:
  whisper   S2C 0x09 status 0x67 "<name>is rejecting whispers." (a GM sender is not
            refused: chat_mail_gm F2 step 4 - pass actor=)        chat_mail_gm-whisper
            (done: GameServer._handle_whisper, P6 stage 1)
  exchange  S2C 0x47 result 4 "The player is rejecting trade."    trade-request-accept
  party     S2C 0x15 "<B> is refusing party invitations." (the EN client has no refuse
            text of its own: party.md F2)                          party-invite
  talk      S2C 0x0F subtype 4 "is rejecting chatting."            social_friend-chat-room
  friend    S2C 0x0C result 4                                      social_friend-friend-add
"""

KINDS = ('whisper', 'exchange', 'party', 'talk', 'friend')

# Kinds a GM actor is exempt from (chat_mail_gm F2 step 4: "... and the sender is not GM").
# Only whisper is specified; the other groups' designs refuse a GM like anyone else.
GM_EXEMPT = frozenset({'whisper'})


def default():
    """Nothing refused: what a character that never sent its options gets."""
    return {kind: False for kind in KINDS}


def normalize(value):
    """A clean {kind: bool} for all five kinds from whatever is stored (missing kinds and
    anything that is not a dict read as "not refused")."""
    flags = default()
    if isinstance(value, dict):
        for kind in KINDS:
            flags[kind] = bool(value.get(kind))
    return flags


def from_record(rec):
    """The flags of a decoded C2S 0x2B / 0x40 record (fields refuse_<kind>)."""
    return {kind: bool(rec.get(f'refuse_{kind}')) for kind in KINDS}


def ensure(char):
    """Create or repair the character record's `refuse` dict (store migration and
    new_character; structural, idempotent)."""
    char['refuse'] = normalize(char.get('refuse'))
    return char['refuse']


def flags_of(target):
    """The flags of a session (its live copy, else its character's) or a character record."""
    if not isinstance(target, dict):
        return default()
    if isinstance(target.get('refuse'), dict):
        return normalize(target['refuse'])
    char = target.get('char')
    if isinstance(char, dict):
        return normalize(char.get('refuse'))
    return default()


def refuses(target, kind, actor=None):
    """True when `target` (a session or a character record) refuses `kind` from `actor`
    (the requesting session; a GM is exempt from the GM_EXEMPT kinds). None refuses
    nothing: an unresolved target is the consumer's "not found" case, not a refusal."""
    if kind not in KINDS:
        raise ValueError(f'{kind!r}: not a refuse kind ({", ".join(KINDS)})')
    if target is None:
        return False
    if actor is not None and kind in GM_EXEMPT and (actor.get('gm') or 0):
        return False
    return flags_of(target)[kind]


def describe(flags):
    """'whisper,party' / 'none' for log lines."""
    on = [kind for kind in KINDS if (flags or {}).get(kind)]
    return ','.join(on) if on else 'none'
