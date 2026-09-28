#!/usr/bin/env python3
"""
names.py - character name rules (login_character.md 3.5.4; lc-create)
=====================================================================
The client checks a new name locally in `FUN_0043DF40(name, 1)` before it sends C2S 0x0E,
but that check is client side: a patched or hand-built packet reaches the server with
anything in `str[17]`. The server re-runs the same rules, because a name that breaks them
breaks the client that later receives it (S1-13: `str[17]` fields feed client `strcpy` /
`sprintf` stack buffers) and breaks every name-addressed packet (0x2A, 0x2B, 0x4E, 0x30,
0x45, whisper) once two characters share a name.

    names.check('Nova')        -> None                  (usable)
    names.check('two words')   -> 'contains a space'    (S2C 0x1C result 4)
    names.check('windy')       -> "reserved word 'wind'"

`check` mirrors the client's own refusals, in its order, plus the server policy the design
adds (login_character.md 3.5.4):
  - 1..16 bytes up to the first NUL (the field is str[17]; 17 bytes would leave no NUL);
  - no 0x20 space and no CP949 full-width space (A1 A1);
  - the lowercased name contains none of RESERVED_WORDS (the client's list, including the
    quote and backslash it also refuses);
  - policy: `[A-Za-z0-9]` only. The client's extra `Curse_Engine_Clean` word list is not
    available (open question Q12), and this is stricter than it anyway.

Uniqueness is not here: it is a store lookup (`Store.name_owner`, case-insensitive), which
lc-create checks separately because it maps to a different reply (0x1C result 2, "The name
is already being used", instead of result 4 "Invalid name").

premium_cash-rename (D8) validates its new name with this same module.
"""
import re

# str[17] on the wire: at most 16 bytes plus the NUL the encoder writes (packets.name17).
MAX_NAME_BYTES = 16
# Server policy (Q12): ASCII letters and digits. cp949 names would also break the
# [A-Za-z0-9] assumption of every name-keyed index.
ALLOWED_RE = re.compile(r'^[A-Za-z0-9]+$')
# FUN_0043DF40's own list, checked against the lowercased name.
RESERVED_WORDS = ("'", '\\', 'gamemaster', 'yahoo', 'wind', 'windmaster', 'windy',
                  'winsle', 'master', 'avocade', 'administrator', 'hamelin')
# CP949 full-width space; the client refuses it exactly like 0x20.
CP949_SPACE = b'\xa1\xa1'


def clean(raw):
    """The name as the client meant it: bytes/str cut at the first NUL, as text."""
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).split(b'\x00', 1)[0].decode('cp949', 'replace')
    return str(raw).split('\x00', 1)[0]


def check(name):
    """None when the name is usable, else a short reason for the log (the caller answers
    S2C 0x1C result 4 "Invalid name. Please try another name.")."""
    name = clean(name)
    raw = name.encode('cp949', 'replace')
    if not raw:
        return 'empty'
    if len(raw) > MAX_NAME_BYTES:
        return f'{len(raw)} bytes, the client field holds {MAX_NAME_BYTES}'
    if ' ' in name or CP949_SPACE in raw:
        return 'contains a space'
    lowered = name.lower()
    for word in RESERVED_WORDS:
        if word in lowered:
            return f'reserved word {word!r}'
    if not ALLOWED_RE.match(name):
        return 'not [A-Za-z0-9]'
    return None


def is_valid(name):
    return check(name) is None
