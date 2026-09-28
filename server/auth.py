#!/usr/bin/env python3
"""
auth.py - account password storage (login_character.md F2 / 3.1; lc-login-errors)
=================================================================================
The client sends the password in clear (C2S 0x01 `str[21]`, C2S 0x12 `confirm_password`,
C2S 0x51 `password`): the wire format is fixed, so nothing can be done there. What this
module fixes is the password **at rest**: accounts.json used to hold `"password": "test"`,
which anyone reading the file (or a crash dump, or the repo) could use.

    stored = auth.hash_password('test')      # 'pbkdf2_sha256$120000$<salt>$<key>'
    auth.verify(stored, 'test')              # True
    auth.verify('test', 'test')              # True  - legacy plaintext record
    auth.needs_upgrade('test')               # True  - store.load() re-writes it hashed

Rules
-----
- One format string per record, so a file can hold both shapes while it migrates
  (store.migrate_accounts hashes every plaintext password once, idempotently).
- `verify` accepts a legacy plaintext record, because the operator may hand-add an
  account by editing accounts.json; the next load hashes it.
- Comparison is constant time (hmac.compare_digest) for both shapes.
- The client's field is `str[21]`, so a password is at most 20 bytes; longer input is a
  tampered packet, not a user, and is rejected rather than truncated.
- Nothing here logs a password; callers must not either (the old [LOGIN] line printed it).
"""
import base64
import hashlib
import hmac
import os

ALGORITHM = 'pbkdf2_sha256'
ITERATIONS = 120_000
SALT_BYTES = 16
# C2S 0x01 str[21] / C2S 0x12 confirm_password str[21] / C2S 0x51 str[21], NUL included.
MAX_PASSWORD_BYTES = 20


def _b64(raw):
    return base64.b64encode(raw).decode('ascii')


def _to_bytes(password):
    if isinstance(password, (bytes, bytearray)):
        return bytes(password).split(b'\x00', 1)[0]
    return str(password).split('\x00', 1)[0].encode('utf-8', 'surrogateescape')


def is_hashed(stored):
    return isinstance(stored, str) and stored.startswith(ALGORITHM + '$')


def hash_password(password, *, salt=None, iterations=ITERATIONS):
    """`pbkdf2_sha256$<iterations>$<b64 salt>$<b64 key>` for accounts.json."""
    raw = _to_bytes(password)
    salt = os.urandom(SALT_BYTES) if salt is None else bytes(salt)
    key = hashlib.pbkdf2_hmac('sha256', raw, salt, iterations)
    return f'{ALGORITHM}${iterations}${_b64(salt)}${_b64(key)}'


def verify(stored, password):
    """True when `password` is the account's. `stored` is a hash or a legacy plaintext
    value; an over-long password (> the client's str[21]) is refused outright."""
    raw = _to_bytes(password)
    if len(raw) > MAX_PASSWORD_BYTES or not raw:
        # The login screen refuses an empty field (0x4481C9), so an empty password can only
        # come from a built packet; a record with no password must never accept one either.
        return False
    if stored is None or stored == '':
        return False
    if not is_hashed(stored):
        # Legacy / hand-edited record. Constant time all the same: a wrong password must
        # not be distinguishable from a wrong account (F2 2d sends 0x11 for both).
        return hmac.compare_digest(_to_bytes(stored if stored is not None else ''), raw)
    try:
        _, iterations, salt_b64, key_b64 = stored.split('$', 3)
        salt, key = base64.b64decode(salt_b64), base64.b64decode(key_b64)
        want = hashlib.pbkdf2_hmac('sha256', raw, salt, int(iterations))
    except (ValueError, TypeError):
        return False                       # corrupt record: refuse, never accept anything
    return hmac.compare_digest(want, key)


def needs_upgrade(stored):
    """True when this record should be re-written hashed (plaintext, or an older cost)."""
    if not is_hashed(stored):
        return True
    try:
        return int(stored.split('$', 2)[1]) < ITERATIONS
    except (ValueError, IndexError):
        return True
